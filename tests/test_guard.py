"""Tests for the five-layer hallucination guard.

Run from the project root:

    PYTHONPATH=src python3 -m pytest tests/test_guard.py -q
"""

from __future__ import annotations

import pytest

from double_gate.guard import (
    Artifact,
    ArtifactType,
    GuardReport,
    Verdict,
    build_verification_prompt,
    cross_model_correlation,
    extract_artifacts,
    generate_verification_prompt,
    guard,
    inject_guard_into_report,
    run_hallucination_guard,
    scan_artifact,
    score_artifacts,
)

# ---------------------------------------------------------------------------
# Layer 1: extraction
# ---------------------------------------------------------------------------


def test_extract_artifacts_kinds():
    text = (
        "In this review I inspected `config.yaml`, then confirmed the endpoint "
        "GET /api/v1/reports/{report_id}. The module uses `json` via "
        "`import json`, and the function `_find_file_by_basename` is used by "
        "`scan_artifact()`. The record also carries `parseField` and `user_id`."
    )
    artifacts = extract_artifacts(text)
    names = {(a.type, a.name) for a in artifacts}
    assert (ArtifactType.FILE, "config.yaml") in names
    assert (ArtifactType.API, "/api/v1/reports/{report_id}") in names
    assert (ArtifactType.IMPORT, "json") in names
    assert (ArtifactType.CODE_SYMBOL, "scan_artifact") in names
    assert (ArtifactType.FIELD, "parseField") in names
    assert (ArtifactType.FIELD, "user_id") in names


def test_extract_artifacts_stamps_source():
    artifacts = extract_artifacts(
        "check `pipeline.yaml`", source_model="reviewer-a", source_vendor="acme"
    )
    assert artifacts
    for artifact in artifacts:
        assert artifact.source_model == "reviewer-a"
        assert artifact.source_vendor == "acme"


def test_extract_artifacts_skips_urls_and_data_uris():
    text = (
        "docs live at https://example.com/spec.pdf and the logo is embedded as "
        "data:image/png;base64,AAAABBBB the config is `app.yaml`."
    )
    names = {a.name for a in extract_artifacts(text)}
    assert "spec.pdf" not in names
    assert "app.yaml" in names


def test_extract_artifacts_empty():
    assert extract_artifacts("") == []
    assert extract_artifacts(None) == []


# ---------------------------------------------------------------------------
# Layer 2: scanning
# ---------------------------------------------------------------------------


def test_real_file_verified(tmp_path):
    (tmp_path / "config.yaml").write_text("setting: true\n")
    artifact = Artifact(name="config.yaml", type=ArtifactType.FILE)
    result = scan_artifact(artifact, codebase_roots=[str(tmp_path)])
    assert result.verdict == Verdict.VERIFIED
    assert result.confidence == pytest.approx(0.95)


def test_absolute_path_verified(tmp_path):
    target = tmp_path / "nested" / "assets.db"
    target.parent.mkdir()
    target.write_text("{}")
    artifact = Artifact(name=str(target), type=ArtifactType.FILE)
    result = scan_artifact(artifact, codebase_roots=[str(tmp_path)])
    assert result.verdict == Verdict.VERIFIED


def test_absolute_path_outside_approved_root_is_not_probed(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    secret = tmp_path / "outside.txt"
    secret.write_text("not part of the scanned repository")
    artifact = Artifact(name=str(secret), type=ArtifactType.FILE)
    result = scan_artifact(artifact, codebase_roots=[str(root)])
    assert result.verdict == Verdict.UNVERIFIABLE
    assert "outside" in result.evidence


def test_relative_traversal_outside_approved_root_is_not_probed(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside.py").write_text("secret = True\n")
    artifact = Artifact(name="../outside.py", type=ArtifactType.FILE)
    result = scan_artifact(artifact, codebase_roots=[str(root)])
    assert result.verdict == Verdict.UNVERIFIABLE
    assert "escapes" in result.evidence


def test_file_found_by_basename(tmp_path):
    (tmp_path / "nested" / "assets.db").parent.mkdir()
    (tmp_path / "nested" / "assets.db").write_text("x")
    artifact = Artifact(name="assets.db", type=ArtifactType.FILE)
    result = scan_artifact(artifact, codebase_roots=[str(tmp_path)])
    assert result.verdict == Verdict.VERIFIED
    assert result.confidence == pytest.approx(0.7)
    assert "basename" in result.evidence


def test_missing_file_likely_hallucination(tmp_path):
    artifact = Artifact(name="no_such_file.py", type=ArtifactType.FILE)
    result = scan_artifact(artifact, codebase_roots=[str(tmp_path)])
    assert result.verdict == Verdict.LIKELY_HALLUCINATION


def test_empty_artifact_unverifiable(tmp_path):
    artifact = Artifact(name="", type=ArtifactType.FILE)
    result = scan_artifact(artifact, codebase_roots=[str(tmp_path)])
    assert result.verdict == Verdict.UNVERIFIABLE


def test_import_verified(tmp_path):
    (tmp_path / "mod.py").write_text("import json\n")
    artifact = Artifact(name="json", type=ArtifactType.IMPORT)
    result = scan_artifact(artifact, codebase_roots=[str(tmp_path)])
    assert result.verdict == Verdict.VERIFIED


def test_code_symbol_definition_verified(tmp_path):
    (tmp_path / "mod.py").write_text("def helper():\n    pass\n")
    artifact = Artifact(name="helper", type=ArtifactType.CODE_SYMBOL)
    result = scan_artifact(artifact, codebase_roots=[str(tmp_path)])
    assert result.verdict == Verdict.VERIFIED
    assert "definition" in result.evidence


def test_api_route_verified(tmp_path):
    (tmp_path / "app.py").write_text("@app.get('/api/v1/reports/42')\ndef view(): pass\n")
    artifact = Artifact(name="/api/v1/reports/{report_id}", type=ArtifactType.API)
    result = scan_artifact(artifact, codebase_roots=[str(tmp_path)])
    assert result.verdict == Verdict.VERIFIED


def test_no_rg_fallback(tmp_path, monkeypatch):
    (tmp_path / "mod.py").write_text("import json\n\ndef helper():\n    pass\n")

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("rg not available")

    monkeypatch.setattr("double_gate.guard.subprocess.run", fake_run)

    import_artifact = Artifact(name="json", type=ArtifactType.IMPORT)
    result = scan_artifact(import_artifact, codebase_roots=[str(tmp_path)])
    assert result.verdict == Verdict.VERIFIED
    assert any("text" in entry or "walk" in entry for entry in result.search_log)

    symbol_artifact = Artifact(name="helper", type=ArtifactType.CODE_SYMBOL)
    result = scan_artifact(symbol_artifact, codebase_roots=[str(tmp_path)])
    assert result.verdict == Verdict.VERIFIED


# ---------------------------------------------------------------------------
# Layer 3: scoring
# ---------------------------------------------------------------------------


def _result_for(verdict):
    return type(
        "FakeResult",
        (),
        {"verdict": verdict, "confidence": 0.9},
    )()


def test_score_artifacts_any_likely_is_high():
    scored = score_artifacts(
        [
            _result_for(Verdict.VERIFIED),
            _result_for(Verdict.LIKELY_HALLUCINATION),
        ]
    )
    assert scored["total"] == 2
    assert scored["hallucination"] == 1
    assert scored["overall_risk"] == "high"


def test_score_artifacts_rate_based_risk():
    verified = [_result_for(Verdict.VERIFIED) for _ in range(18)]
    suspicious = [_result_for(Verdict.SUSPICIOUS) for _ in range(2)]
    scored = score_artifacts(verified + suspicious)
    assert scored["hallucination"] == 0
    assert scored["hallucination_rate"] == pytest.approx(0.0)
    assert scored["overall_risk"] == "low"


def test_score_artifacts_empty_is_low():
    scored = score_artifacts([])
    assert scored["total"] == 0
    assert scored["overall_risk"] == "low"


# ---------------------------------------------------------------------------
# Layer 4: cross-model correlation
# ---------------------------------------------------------------------------


def _scan_result(name, verdict, model):
    artifact = Artifact(name=name, type=ArtifactType.FILE, source_model=model)
    return type(
        "FakeResult",
        (),
        {
            "artifact": artifact,
            "verdict": verdict,
            "confidence": 0.9,
            "evidence": "",
            "search_log": [],
        },
    )()


def test_cross_model_shared_hallucination(tmp_path):
    texts = {
        "model_a": "The config file `phantom_settings.yaml` is loaded at startup.",
        "model_b": "I also confirmed `phantom_settings.yaml` exists in the repo.",
    }
    result = run_hallucination_guard(texts, codebase_roots=[str(tmp_path)])
    warnings = result["layer_4_cross_model"]["shared_hallucination_warnings"]
    file_warnings = [w for w in warnings if w["artifact"] == "phantom_settings.yaml"]
    assert len(file_warnings) == 1
    warning = file_warnings[0]
    assert set(warning["models"]).issuperset({"model_a", "model_b"})
    assert warning["hallucination_count"] >= 2
    assert result["summary"]["overall_hallucination_risk"] == "high"


def test_cross_model_no_shared_warning_when_verified(tmp_path):
    (tmp_path / "real.yaml").write_text("setting: true\n")
    all_artifacts = {
        "model_a": [_scan_result("real.yaml", Verdict.VERIFIED, "model_a")],
        "model_b": [_scan_result("real.yaml", Verdict.VERIFIED, "model_b")],
    }
    correlation = cross_model_correlation(all_artifacts)
    assert correlation["shared_hallucination_count"] == 0


# ---------------------------------------------------------------------------
# Layer 5: feedback prompts
# ---------------------------------------------------------------------------


def test_generate_verification_prompt():
    result = _scan_result("ghost.py", Verdict.LIKELY_HALLUCINATION, "model_a")
    prompt = generate_verification_prompt([result])
    assert "ghost.py" in prompt
    assert "confirm" in prompt.lower()


def test_generate_verification_prompt_empty_when_clean():
    result = _scan_result("real.py", Verdict.VERIFIED, "model_a")
    assert generate_verification_prompt([result]) == ""
    assert generate_verification_prompt([]) == ""


def test_build_verification_prompt():
    prompt = build_verification_prompt(
        "ghost.py does not exist", "The module ghost.py is loaded."
    )
    assert "ORIGINAL REVIEW CONTENT" in prompt
    assert "VERIFICATION RESPONSE" in prompt
    assert "ghost.py" in prompt


# ---------------------------------------------------------------------------
# Orchestration + jury integration
# ---------------------------------------------------------------------------


def test_pipeline_clean_review_is_low_risk(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "core.py").write_text("import json\n\ndef parse_field():\n    pass\n")
    text = "The helper `parse_field` is defined in `lib/core.py` and uses `json`."
    result = run_hallucination_guard({"reviewer": text}, codebase_roots=[str(tmp_path)])
    assert result["summary"]["hallucination"] == 0
    assert result["summary"]["overall_hallucination_risk"] == "low"
    assert result["layer_5_feedback_prompts"] == {}


def test_run_hallucination_guard_report_shape(tmp_path):
    result = run_hallucination_guard(
        {"model_a": "check `missing.py`", "model_b": "check `missing.py` too"},
        codebase_roots=[str(tmp_path)],
    )
    assert set(result.keys()) == {
        "summary",
        "layer_1_extraction",
        "layer_3_scoring",
        "layer_4_cross_model",
        "layer_5_feedback_prompts",
        "detailed_results",
    }
    assert result["summary"]["models_checked"] == 2
    assert result["summary"]["total_artifacts"] > 0
    assert "overall_hallucination_risk" in result["summary"]


def test_guard_and_inject_into_report(tmp_path):
    adjudication = {"content": "The module `phantom.py` is imported at the top."}
    report = guard(adjudication, codebase_root=str(tmp_path))
    assert isinstance(report, GuardReport)
    merged = inject_guard_into_report(adjudication, report)
    assert "guard" in merged
    assert "guard_score" in merged
    assert "overall_hallucination_risk" in merged
    assert merged["hallucination_guard"]["risk_level"] in ("low", "medium", "high")
    assert merged["hallucinated_artifacts"] >= 1


def test_guard_accepts_model_texts(tmp_path):
    adjudication = {"model_texts": {"reviewer_a": "check `missing.py`"}}
    report = guard(adjudication, codebase_root=str(tmp_path))
    assert report.summary["models_checked"] == 1


def test_guard_preserves_structured_source_vendor(tmp_path):
    report = guard(
        {"model_texts": {"reviewer-a": {"content": "check `missing.py`", "vendor": "provider-a"}}},
        codebase_root=str(tmp_path),
    )
    assert report.layer_1_extraction[0]["vendor"] == "provider-a"
    assert report.detailed_results[0]["artifact"]["source_vendor"] == "provider-a"
