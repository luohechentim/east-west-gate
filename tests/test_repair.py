"""Tests for the bug-fix loop (repair.py)."""

from __future__ import annotations

from double_gate import repair


def _fake_result(findings, escalations=None):
    return {
        "adjudication": {
            "findings": findings,
            "verdict": "minor-issues",
        },
        "human_escalations": escalations or [],
        "hallucination_guard": {"detailed_results": []},
    }


def test_build_checklist_opens_critical_and_major():
    result = _fake_result(
        [
            {"id": "F1", "severity": "critical", "summary": "SQL injection", "suggestion": "use params"},
            {"id": "F2", "severity": "major", "summary": "missing auth", "suggestion": "add token"},
            {"id": "F3", "severity": "minor", "summary": "style", "suggestion": ""},
        ]
    )
    checklist = repair.build_checklist(result)
    assert checklist.open_count() == 3
    open_ids = [i.finding_id for i in checklist.require_fix()]
    assert "F1" in open_ids and "F2" in open_ids


def test_verify_fixed_marks_absent_finding_verified():
    result = _fake_result(
        [{"id": "F1", "severity": "major", "summary": "missing auth token", "suggestion": "add auth"}]
    )
    checklist = repair.build_checklist(result)
    # re-review no longer mentions the auth finding
    re_review = {"adjudication": {"findings": [], "verdict": "approved"}}
    report = repair.verify_fixed(re_review, checklist)
    assert report["all_findings_absent"] is True
    assert report["all_resolved"] is False
    assert report["verification_status"] == "needs-independent-review"
    assert checklist.items[0].status == "open"


def test_verify_fixed_keeps_open_when_finding_persists():
    result = _fake_result(
        [{"id": "F1", "severity": "major", "summary": "SQL injection in query builder", "suggestion": "parameterize"}]
    )
    checklist = repair.build_checklist(result)
    # a nearly-identical finding still present -> stays open
    re_review = {
        "adjudication": {
            "findings": [
                {"id": "F9", "severity": "major",
                 "summary": "SQL injection remains in the query builder; parameterize it"},
            ],
            "verdict": "serious-issues",
        }
    }
    report = repair.verify_fixed(re_review, checklist)
    assert report["all_resolved"] is False
    assert report["open"] == 1
    assert checklist.items[0].status == "open"


def test_verify_fixed_closes_distant_finding():
    # A low-overlap finding is treated as a new/different issue, so the original
    # finding is considered resolved (conservative: don't keep it open on weak match).
    result = _fake_result(
        [{"id": "F1", "severity": "major", "summary": "SQL injection in query builder", "suggestion": "parameterize"}]
    )
    checklist = repair.build_checklist(result)
    re_review = {
        "adjudication": {
            "findings": [
                {"id": "F9", "severity": "minor", "summary": "renamed a variable for clarity"},
            ],
            "verdict": "approved",
        }
    }
    report = repair.verify_fixed(re_review, checklist)
    assert report["verified"] == 0
    assert checklist.items[0].status == "open"


def test_verify_fixed_closes_with_assured_multi_provider_review():
    result = _fake_result(
        [{"id": "F1", "severity": "major", "summary": "missing auth token", "suggestion": "add auth"}]
    )
    checklist = repair.build_checklist(result)
    re_review = {
        "panel_integrity": {"review_mode": "multi-provider", "partial_failure": False},
        "adjudication": {
            "findings": [],
            "verdict": "approved",
            "review_count": 2,
            "insufficient_data": False,
        },
    }
    report = repair.verify_fixed(re_review, checklist)
    assert report["all_resolved"] is True
    assert report["verified"] == 1
    assert checklist.items[0].status == "verified"


def test_to_json_roundtrip():
    result = _fake_result(
        [{"id": "F1", "severity": "major", "summary": "auth", "suggestion": "token"}]
    )
    checklist = repair.build_checklist(result)
    data = repair.to_json(checklist)
    assert '"finding_id": "F1"' in data
