"""Tests for the Double Gate quality gates, HTTP service and CLI.

Run from the project root with the source tree on the path:

    PYTHONPATH=src python3 -m pytest tests/test_gates.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from double_gate import gates  # noqa: E402

# --------------------------------------------------------------------------
# Shared fixtures
# --------------------------------------------------------------------------

LONG_CONTENT = "This is a sufficiently long output document. " * 10


def clean_data() -> dict:
    return {"alpha": 1, "beta": "text", "gamma": [1, 2], "delta": "filled", "epsilon": 3.14}


def clean_delivery() -> dict:
    return {
        "recipient": "review-board",
        "subject": "Review report",
        "signature": "Double Gate Panel",
    }


def clean_ctx(risk: str = "low") -> dict:
    return {
        "risk_level": risk,
        "data": clean_data(),
        "delivery": clean_delivery(),
    }


# --------------------------------------------------------------------------
# Gate 0 — task definition
# --------------------------------------------------------------------------

class TestGate0:
    def test_pass(self):
        result = gates.check_gate_0(LONG_CONTENT, {})
        assert result["passed"] is True
        assert result["gate"] == 0
        assert result["score"] >= 7

    def test_too_short(self):
        result = gates.check_gate_0("short", {})
        assert result["passed"] is False
        assert result["score"] < 7

    def test_too_many_unresolved_questions(self):
        ctx = {"unresolved_questions": [1, 2, 3, 4, 5]}
        result = gates.check_gate_0(LONG_CONTENT, ctx)
        assert result["passed"] is False

    def test_questions_alias_supported(self):
        ctx = {"questions": ["only one open question"]}
        result = gates.check_gate_0(LONG_CONTENT, ctx)
        assert result["passed"] is True


# --------------------------------------------------------------------------
# Gate 1 — data quality
# --------------------------------------------------------------------------

class TestGate1:
    def test_pass_low_empty_rate(self):
        result = gates.check_gate_1(LONG_CONTENT, {"data": clean_data()})
        assert result["passed"] is True

    def test_fail_high_empty_rate(self):
        data = {"a": None, "b": "x", "c": None, "d": "", "e": "z"}  # 3/5 = 60%
        result = gates.check_gate_1(LONG_CONTENT, {"data": data})
        assert result["passed"] is False
        assert result["score"] < 6

    def test_fail_missing_data(self):
        result = gates.check_gate_1(LONG_CONTENT, {})
        assert result["passed"] is False

    def test_fail_non_dict_data(self):
        result = gates.check_gate_1(LONG_CONTENT, {"data": ["not", "a", "dict"]})
        assert result["passed"] is False


# --------------------------------------------------------------------------
# Gate 2 — review
# --------------------------------------------------------------------------

class TestGate2:
    def test_engine_approved_passes(self):
        def engine(content, risk):
            return {"verdict": "approved", "overall": 90, "hallucination_guard": {"risk_level": "low"}}

        result = gates.check_gate_2(LONG_CONTENT, {"risk_level": "high", "review_engine": engine})
        assert result["passed"] is True

    def test_engine_serious_fails(self):
        def engine(content, risk):
            return {"verdict": "serious", "overall": 30, "hallucination_guard": {"risk_level": "high"}}

        result = gates.check_gate_2(LONG_CONTENT, {"risk_level": "high", "review_engine": engine})
        assert result["passed"] is False

    def test_engine_low_overall_fails(self):
        def engine(content, risk):
            return {"verdict": "approved", "overall": 40}

        result = gates.check_gate_2(LONG_CONTENT, {"risk_level": "high", "review_engine": engine})
        assert result["passed"] is False

    def test_engine_high_hallucination_risk_fails(self):
        def engine(content, risk):
            return {"verdict": "approved", "overall": 88, "hallucination_guard": {"risk_level": "high"}}

        result = gates.check_gate_2(LONG_CONTENT, {"risk_level": "medium", "review_engine": engine})
        assert result["passed"] is False

    def test_no_engine_high_is_flagged_not_hard_failed(self):
        result = gates.check_gate_2(LONG_CONTENT, {"risk_level": "high"})
        assert result["score"] == 5
        assert result["passed"] is False
        assert result["soft_fail"] is True
        assert any("human review" in issue for issue in result["issues"])

    def test_no_engine_medium_is_flagged(self):
        result = gates.check_gate_2(LONG_CONTENT, {"risk_level": "medium"})
        assert result["score"] == 5
        assert result["soft_fail"] is True

    def test_no_engine_low_passes_offline(self):
        result = gates.check_gate_2(LONG_CONTENT, {"risk_level": "low"})
        assert result["passed"] is True
        assert result["score"] >= 6

    def test_engine_failure_must_fail(self):
        def engine(content, risk):
            raise RuntimeError("engine exploded")

        result = gates.check_gate_2(LONG_CONTENT, {"risk_level": "high", "review_engine": engine})
        assert result["passed"] is False
        assert result["score"] == 0

    def test_engine_is_actually_invoked(self):
        calls = []

        def engine(content, risk):
            calls.append(risk)
            return {"verdict": "approved", "overall": 85}

        gates.check_gate_2(LONG_CONTENT, {"risk_level": "medium", "review_engine": engine})
        assert calls == ["medium"]


# --------------------------------------------------------------------------
# Gate 3 — bias scan
# --------------------------------------------------------------------------

class TestGate3:
    def test_pass_clean_content(self):
        result = gates.check_gate_3(LONG_CONTENT, {})
        assert result["passed"] is True

    def test_fail_confirmation_bias(self):
        result = gates.check_gate_3(
            "This report suffers from confirmation bias and only considers positives.", {}
        )
        assert result["passed"] is False

    def test_fail_survivorship_bias(self):
        result = gates.check_gate_3(
            "The analysis ignores dead companies; survivorship bias inflates returns.", {}
        )
        assert result["passed"] is False

    def test_fail_anchoring_bias(self):
        result = gates.check_gate_3("The first anchor price set the whole valuation.", {})
        assert result["passed"] is False

    def test_fail_chinese_bias_keywords(self):
        result = gates.check_gate_3("结论存在幸存者偏差和锚定效应。", {})
        assert result["passed"] is False


# --------------------------------------------------------------------------
# Gate 4 — output quality
# --------------------------------------------------------------------------

class TestGate4:
    def test_pass(self):
        result = gates.check_gate_4(LONG_CONTENT, {})
        assert result["passed"] is True

    def test_fail_too_short(self):
        result = gates.check_gate_4("short text here", {})
        assert result["passed"] is False

    def test_fail_empty(self):
        result = gates.check_gate_4("", {})
        assert result["passed"] is False


# --------------------------------------------------------------------------
# Gate 5 — delivery
# --------------------------------------------------------------------------

class TestGate5:
    def test_pass(self):
        result = gates.check_gate_5(LONG_CONTENT, {"delivery": clean_delivery()})
        assert result["passed"] is True

    def test_fail_missing_fields(self):
        delivery = {"recipient": "review-board", "subject": "Review report"}
        result = gates.check_gate_5(LONG_CONTENT, {"delivery": delivery})
        assert result["passed"] is False

    def test_fail_not_a_dict(self):
        result = gates.check_gate_5(LONG_CONTENT, {"delivery": "nope"})
        assert result["passed"] is False


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

class TestCheckAllGates:
    def test_all_pass(self):
        result = gates.check_all_gates(LONG_CONTENT, clean_ctx())
        assert result["passed"] is True
        assert result["hard_failed"] is False
        assert result["needs_human_review"] is False
        assert len(result["results"]) == 6
        assert [r["gate"] for r in result["results"]] == [0, 1, 2, 3, 4, 5]

    def test_single_failure_breaks_aggregate(self):
        ctx = clean_ctx()
        ctx["delivery"] = {"recipient": "review-board"}  # missing subject + signature
        result = gates.check_all_gates(LONG_CONTENT, ctx)
        assert result["passed"] is False
        assert result["hard_failed"] is True

    def test_high_risk_no_engine_needs_human_review(self):
        result = gates.check_all_gates(LONG_CONTENT, {"risk_level": "high"})
        assert result["needs_human_review"] is True
        assert result["passed"] is False

    def test_summary_counts(self):
        result = gates.check_all_gates(LONG_CONTENT, clean_ctx())
        assert result["summary"]["total_gates"] == 6
        assert result["summary"]["passed_gates"] == 6


class TestGateDefinitions:
    def test_six_definitions(self):
        definitions = gates.gate_definitions()
        assert len(definitions) == 6
        assert {d["gate"] for d in definitions} == {0, 1, 2, 3, 4, 5}
        assert all(d["threshold"] >= 6 for d in definitions)
        assert definitions[0]["threshold"] == 7


# CLI smoke tests (subprocess, offline paths only)
# --------------------------------------------------------------------------

PYTHON = sys.executable


def run_cli(args):
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [PYTHON, "-m", "double_gate.cli"] + args,
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
    )


class TestCli:
    def test_cli_gates_subprocess(self, tmp_path):
        content_file = tmp_path / "content.txt"
        content_file.write_text(LONG_CONTENT, encoding="utf-8")
        proc = run_cli(["gates", str(content_file), "--risk", "low"])
        assert proc.returncode == 0, proc.stderr
        body = json.loads(proc.stdout)
        assert "passed" in body
        assert "needs_human_review" in body
        assert len(body["results"]) == 6

    def test_cli_gates_with_data_and_delivery(self, tmp_path):
        content_file = tmp_path / "content.txt"
        data_file = tmp_path / "data.json"
        delivery_file = tmp_path / "delivery.json"
        content_file.write_text(LONG_CONTENT, encoding="utf-8")
        data_file.write_text(json.dumps(clean_data()), encoding="utf-8")
        delivery_file.write_text(json.dumps(clean_delivery()), encoding="utf-8")
        proc = run_cli(
            [
                "gates",
                str(content_file),
                "--risk",
                "low",
                "--data",
                str(data_file),
                "--delivery",
                str(delivery_file),
            ]
        )
        assert proc.returncode == 0, proc.stderr
        body = json.loads(proc.stdout)
        assert body["passed"] is True

    def test_cli_review_offline(self, tmp_path):
        content_file = tmp_path / "content.txt"
        content_file.write_text(LONG_CONTENT, encoding="utf-8")
        proc = run_cli(["review", str(content_file), "--risk", "low", "--offline"])
        assert proc.returncode == 0, proc.stderr
        body = json.loads(proc.stdout)
        assert body["status"] == "completed_with_limitations"
        assert body["panel_integrity"]["review_mode"] == "offline-simulation"
        assert "verdict" in body["adjudication"]

    def test_cli_catalog(self):
        proc = run_cli(["catalog"])
        assert proc.returncode == 0, proc.stderr
        body = json.loads(proc.stdout)
        assert "panels" in body

    def test_cli_guard(self, tmp_path):
        content_file = tmp_path / "content.txt"
        content_file.write_text("review api /api/jury/submit and file missing.def", encoding="utf-8")
        proc = run_cli(["guard", str(content_file), "--root", str(tmp_path)])
        assert proc.returncode == 0, proc.stderr
        body = json.loads(proc.stdout)
        assert isinstance(body, dict)
        # dedicated double_gate.guard module and the builtin fallback expose
        # slightly different shapes; both carry risk/verification information
        markers = ("overall_risk", "summary", "layer_1_extract", "layer_1_extraction")
        assert any(marker in body for marker in markers)
