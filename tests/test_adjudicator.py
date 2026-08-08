"""Tests for the deterministic adjudication engine (double_gate.adjudicator)."""

import json

from double_gate import adjudicator


def _review(verdict, score, findings, strengths=None, confidence=0.8):
    return json.dumps(
        {
            "verdict": verdict,
            "score": score,
            "findings": findings,
            "strengths": strengths or [],
            "confidence": confidence,
        }
    )


def test_majority_clustering_and_verdict():
    findings_a = [
        {
            "severity": "minor",
            "category": "clarity",
            "summary": "Section 3 is confusing",
            "detail": "detail A",
            "suggestion": "fix A",
        }
    ]
    findings_b = [
        {
            "severity": "minor",
            "category": "clarity",
            "summary": "Section 3 is confusing",
            "detail": "detail B",
            "suggestion": "fix B",
        }
    ]
    findings_c = [
        {
            "severity": "major",
            "category": "logic",
            "summary": "Unsupported core claim",
            "detail": "detail C",
            "suggestion": "fix C",
        }
    ]
    panel = [
        _review("minor-issues", 72, findings_a),
        _review("minor-issues", 70, findings_b),
        _review("serious-issues", 55, findings_c),
    ]
    report = adjudicator.adjudicate({"id": "S1"}, panel)

    # serious-issues present among verdicts -> serious wins
    assert report["verdict"] == "serious-issues"
    assert report["submission_id"] == "S1"
    assert report["review_count"] == 3

    merged = {f["summary"]: f for f in report["findings"]}
    assert merged["Section 3 is confusing"]["panel_agreement"] == 2
    assert merged["Section 3 is confusing"]["consensus"] == "majority"
    assert merged["Section 3 is confusing"]["severity"] == "minor"
    assert "detail A" in merged["Section 3 is confusing"]["detail"]
    assert "detail B" in merged["Section 3 is confusing"]["detail"]

    assert merged["Unsupported core claim"]["panel_agreement"] == 1
    assert merged["Unsupported core claim"]["consensus"] == "single"

    assert report["panel_summary"]["average_score"] == 65.7  # (72+70+55)/3
    assert report["panel_summary"]["agreement_level"] == "majority"


def test_critical_any_wins():
    panel = [
        _review("approved", 90, []),
        _review(
            "critical",
            30,
            [
                {
                    "severity": "critical",
                    "category": "correctness",
                    "summary": "fatal flaw",
                    "detail": "",
                    "suggestion": "",
                }
            ],
        ),
        _review("minor-issues", 75, []),
    ]
    report = adjudicator.adjudicate({}, panel)
    assert report["verdict"] == "critical"
    assert report["convergence"] == "split"  # no verdict has a majority


def test_approved_threshold():
    panel = [
        _review("approved", 90, []),
        _review("approved", 85, []),
        _review("minor-issues", 70, []),
    ]
    report = adjudicator.adjudicate({}, panel)
    assert report["verdict"] == "approved"
    assert report["panel_summary"]["agreement_level"] == "majority"  # 2 approved


def test_low_average_score_forces_serious():
    panel = [
        _review("minor-issues", 52, []),
        _review("minor-issues", 50, []),
        _review("minor-issues", 54, []),
    ]
    report = adjudicator.adjudicate({}, panel)
    assert report["verdict"] == "serious-issues"  # avg 52 < 55
    assert report["panel_summary"]["agreement_level"] == "unanimous"  # 3 agreed


def test_default_minor_when_no_signal():
    panel = [_review("minor-issues", 72, [])]
    report = adjudicator.adjudicate({}, panel)
    assert report["verdict"] == "minor-issues"


def test_json_fence_tolerance():
    fenced = "```json\n" + _review("minor-issues", 70, []) + "\n```"
    panel = [
        fenced,
        _review("approved", 90, []),
        _review("approved", 88, []),
    ]
    report = adjudicator.adjudicate({}, panel)
    assert report["review_count"] == 3
    assert report["verdict"] == "approved"


def test_json_fragment_extraction():
    messy = "Here is my review.\n" + _review("minor-issues", 71, []) + "\nThat is all."
    assert adjudicator._parse_response(messy)["verdict"] == "minor-issues"


def test_empty_panel_safe():
    report = adjudicator.adjudicate({}, [])
    assert report["insufficient_data"] is True
    assert report["review_count"] == 0
    assert report["findings"] == []
    assert report["verdict"] == "inconclusive"
    assert report["confidence"] == 0.1


def test_invalid_responses_are_skipped():
    panel = ["not json at all", "", None, 123]
    report = adjudicator.adjudicate({}, panel)
    assert report["panel_summary"]["failed_reviews"] == 4
    assert report["review_count"] == 0
    assert report["insufficient_data"] is True


def test_parse_response_none_safe():
    assert adjudicator._parse_response(None) is None
    assert adjudicator._parse_response("") is None
    assert adjudicator._parse_response("no braces here") is None


def test_strengths_deduped():
    panel = [
        _review("approved", 90, [], strengths=["Clean structure", "Clear writing"]),
        _review("approved", 88, [], strengths=["Clean structure"]),
    ]
    report = adjudicator.adjudicate({}, panel)
    assert report["strengths"].count("Clean structure") == 1
    assert "Clear writing" in report["strengths"]


def test_fuzzy_clustering_jaccard():
    findings_a = [
        {
            "severity": "minor",
            "category": "logic",
            "summary": "core reasoning gap in chapter two",
            "detail": "",
            "suggestion": "",
        }
    ]
    findings_b = [
        {
            "severity": "minor",
            "category": "logic",
            "summary": "core reasoning gap in chapter two is not addressed",
            "detail": "",
            "suggestion": "",
        }
    ]
    panel = [
        _review("minor-issues", 71, findings_a),
        _review("minor-issues", 69, findings_b),
        _review("minor-issues", 70, []),
    ]
    report = adjudicator.adjudicate({}, panel)
    merged = {f["summary"]: f for f in report["findings"]}
    assert merged["core reasoning gap in chapter two"]["panel_agreement"] == 2
    assert merged["core reasoning gap in chapter two"]["consensus"] == "majority"


def test_no_cluster_across_category_severity():
    findings_a = [
        {
            "severity": "minor",
            "category": "logic",
            "summary": "core reasoning gap in chapter two",
            "detail": "",
            "suggestion": "",
        }
    ]
    findings_b = [
        {
            "severity": "major",
            "category": "correctness",
            "summary": "core reasoning gap in chapter two is not addressed",
            "detail": "",
            "suggestion": "",
        }
    ]
    panel = [
        _review("minor-issues", 70, findings_a),
        _review("minor-issues", 70, findings_b),
        _review("minor-issues", 72, []),
    ]
    report = adjudicator.adjudicate({}, panel)
    grouped = {f["summary"]: f["panel_agreement"] for f in report["findings"]}
    # high Jaccard but different category/severity -> NOT merged
    assert grouped["core reasoning gap in chapter two"] == 1
    assert grouped["core reasoning gap in chapter two is not addressed"] == 1


def test_exact_same_summary_merges_across_severity_taking_max():
    findings_a = [
        {
            "severity": "minor",
            "category": "clarity",
            "summary": "identical summary",
            "detail": "",
            "suggestion": "",
        }
    ]
    findings_b = [
        {
            "severity": "critical",
            "category": "correctness",
            "summary": "identical summary",
            "detail": "",
            "suggestion": "",
        }
    ]
    panel = [
        _review("minor-issues", 70, findings_a),
        _review("critical", 30, findings_b),
    ]
    report = adjudicator.adjudicate({}, panel)
    merged = {f["summary"]: f for f in report["findings"]}
    assert merged["identical summary"]["panel_agreement"] == 2
    assert merged["identical summary"]["severity"] == "critical"
    assert report["verdict"] == "critical"


def test_severity_helpers():
    assert (
        adjudicator._severity_rank("info")
        < adjudicator._severity_rank("minor")
        < adjudicator._severity_rank("major")
        < adjudicator._severity_rank("critical")
    )
    assert adjudicator._max_severity(["minor", "critical", "major"]) == "critical"
    assert adjudicator._max_severity([]) == "info"
    assert adjudicator._severity_rank("unknown-label") == adjudicator._severity_rank("info")


def test_accepts_model_response_objects():
    from double_gate import models

    client = models.get_client("stub")
    panel = [
        client.chat("s", ["u"]),
        _review("approved", 90, []),
        _review("approved", 88, []),
    ]
    report = adjudicator.adjudicate({}, panel)
    assert report["review_count"] == 3
    assert report["verdict"] == "approved"


def test_convergence_label():
    assert adjudicator._convergence_label(0, 3) == "split"
    assert adjudicator._convergence_label(1, 3) == "split"
    assert adjudicator._convergence_label(2, 3) == "majority"
    assert adjudicator._convergence_label(3, 3) == "unanimous"
    assert adjudicator._convergence_label(2, 4) == "split"
    assert adjudicator._convergence_label(1, 1) == "single"


def test_single_approved_review_is_not_enough_for_approval():
    report = adjudicator.adjudicate({}, [_review("approved", 95, [])])
    assert report["verdict"] == "minor-issues"
    assert report["convergence"] == "single"


def test_invalid_schema_is_counted_as_failed():
    bad = json.dumps({"verdict": "looks-good", "score": 100, "findings": []})
    report = adjudicator.adjudicate({}, [bad])
    assert report["review_count"] == 0
    assert report["panel_summary"]["invalid_schema_reviews"] == 1
    assert report["verdict"] == "inconclusive"
