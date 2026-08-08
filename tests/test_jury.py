"""Tests for orchestration assurance, storage, and reviewer-citation boundaries."""

from __future__ import annotations

import json
import stat

import pytest

from double_gate import jury, models

CONTENT = "This is a sufficiently detailed review target with a bounded scope and clear intent."


def test_offline_panel_is_explicitly_limited_and_not_auto_accepted(tmp_path):
    result = jury.Jury(codebase_root=str(tmp_path)).submit(
        CONTENT, risk_level="low", offline=True
    )
    assert result["status"] == "completed_with_limitations"
    assert result["panel_integrity"]["review_mode"] == "offline-simulation"
    assert result["delivery_status"] == "human-review-required"
    assert result["auto_accepted"] == []
    assert result["human_escalations"]
    assert "content" not in result["adjudication"]
    assert result["hallucination_guard"]["summary"]["models_checked"] == 1


def test_guard_scans_reviewer_text_not_submitted_content(tmp_path, monkeypatch):
    class CitationClient(models.BaseClient):
        vendor = "citation-test"

        def chat(self, *args, **kwargs):
            body = {
                "verdict": "minor-issues",
                "score": 70,
                "findings": [
                    {
                        "severity": "minor",
                        "category": "correctness",
                        "summary": "Review cites ghost_review.py",
                        "detail": "The reviewer claims ghost_review.py must be changed.",
                        "suggestion": "Verify ghost_review.py before acting.",
                    }
                ],
                "strengths": [],
                "confidence": 0.8,
            }
            return models.ModelResponse(
                content=json.dumps(body),
                model="citation-test-model",
                vendor=self.vendor,
                success=True,
            )

    CitationClient.register()
    monkeypatch.setenv(
        "DOUBLE_GATE_PANELS_JSON",
        json.dumps(
            {
                "quick": {
                    "description": "citation boundary test",
                    "models": [
                        {
                            "vendor": "citation-test",
                            "provider": "test-provider",
                            "model": "citation-test-model",
                            "role": "primary",
                        }
                    ],
                }
            }
        ),
    )

    result = jury.Jury(codebase_root=str(tmp_path)).submit(
        "The submitted text only mentions submission_only.py.", risk_level="low"
    )
    names = {
        row["artifact"]["name"]
        for row in result["hallucination_guard"]["detailed_results"]
    }
    assert "ghost_review.py" in names
    assert "submission_only.py" not in names
    assert result["adjudication"]["finding_evidence"]["F1"][0]["verdict"] == "LIKELY_HALLUCINATION"


def test_persistence_is_opt_in_and_owner_only(tmp_path):
    no_store = jury.Jury(codebase_root=str(tmp_path))
    no_store_result = no_store.submit(CONTENT, risk_level="low", offline=True)
    assert no_store.get_result(no_store_result["submission_id"]) is None

    store_path = tmp_path / "private" / "reports.json"
    persisted = jury.Jury(codebase_root=str(tmp_path), store_path=str(store_path))
    result = persisted.submit(CONTENT, risk_level="low", offline=True)
    assert store_path.is_file()
    assert persisted.get_result(result["submission_id"])["submission_id"] == result["submission_id"]
    assert stat.S_IMODE(store_path.stat().st_mode) & 0o077 == 0


def test_malformed_store_is_not_overwritten(tmp_path):
    store_path = tmp_path / "reports.json"
    store_path.write_text("this is not json", encoding="utf-8")
    instance = jury.Jury(codebase_root=str(tmp_path), store_path=str(store_path))
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        instance.submit(CONTENT, risk_level="low", offline=True)
    assert store_path.read_text(encoding="utf-8") == "this is not json"


def test_invalid_panel_environment_fails_loudly(monkeypatch):
    monkeypatch.setenv("DOUBLE_GATE_PANELS_JSON", "not-json")
    with pytest.raises(jury.PanelConfigurationError, match="invalid JSON"):
        jury.catalog()
