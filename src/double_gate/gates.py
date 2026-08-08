"""Six quality gates for the Double Gate review pipeline.

Every gate inspects a piece of ``content`` together with a context dict
(``ctx``) and returns a uniform result:

    {
        "gate": <int>,          # 0..5
        "name": <str>,
        "passed": <bool>,
        "score": <int>,         # 0..9
        "threshold": <int>,     # minimum score required for this gate
        "issues": [<str>, ...],
        "soft_fail": <bool>,    # True when the gate is flagged but not hard-rejected
    }

Passing thresholds (``GATE_THRESHOLDS``): gate 0 requires >= 7, all the
remaining gates require >= 6.

The module depends only on the Python standard library.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

# Passing threshold per gate index. Gate 0 (task definition) is stricter.
GATE_THRESHOLDS: Dict[int, int] = {0: 7, 1: 6, 2: 6, 3: 6, 4: 6, 5: 6}

GATE_NAMES: Dict[int, str] = {
    0: "task_definition",
    1: "data_quality",
    2: "review",
    3: "bias_scan",
    4: "output_quality",
    5: "delivery",
}

_GATE_DESCRIPTIONS: Dict[int, str] = {
    0: "Task definition: content length is >= 20 chars and the number of "
       "unresolved questions stays within the configured boundary.",
    1: "Data quality: ctx['data'] is a dict whose empty-value rate is <= 5%.",
    2: "Review: the injected review engine (ctx['review_engine']) is actually "
       "invoked and its verdict is accepted; when no engine is configured the "
       "gate is flagged for human review instead of hard-failing.",
    3: "Bias scan: no confirmation, anchoring or survivorship bias signals "
       "are detected in the content.",
    4: "Output quality: content is non-empty and at least 50 characters long.",
    5: "Delivery: ctx['delivery'] provides recipient, subject and signature.",
}

# Maximum number of unresolved questions tolerated by gate 0.
_DEFAULT_MAX_UNRESOLVED = 3

# English + Chinese keyword lists used by the bias scan (gate 3).
_BIAS_KEYWORDS: Dict[str, List[str]] = {
    "confirmation": [
        "confirmation bias",
        "selection bias",
        "only positives",
        "ignoring contrary evidence",
        "selective evidence",
        "cherry-pick",
        "cherry pick",
        "选择性",
        "只看到",
        "只看好消息",
        "选择性证据",
        "报喜不报忧",
    ],
    "anchoring": [
        "anchoring",
        "anchor bias",
        "anchored",
        "anchor",
        "reference point",
        "first impression",
        "initial value",
        "锚定",
        "锚定效应",
        "参照锚点",
        "先入为主",
        "初始值",
    ],
    "survivorship": [
        "survivorship",
        "survivor bias",
        "survivorship bias",
        "only survivors",
        "dead companies excluded",
        "幸存者",
        "幸存者偏差",
        "存活偏差",
        "只看活下来的",
    ],
}


def _result(
    gate: int,
    score: int,
    passed: bool,
    issues: List[str],
    name: str,
    soft_fail: bool = False,
) -> Dict[str, Any]:
    """Build a uniform gate result dict."""
    return {
        "gate": gate,
        "name": name,
        "passed": passed,
        "score": score,
        "threshold": GATE_THRESHOLDS[gate],
        "issues": issues,
        "soft_fail": soft_fail,
    }


def _is_empty(value: Any) -> bool:
    """An empty value is None, the empty string, or an empty container."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def check_gate_0(content: str, ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Task definition gate.

    Requires non-trivial content length and a bounded number of unresolved
    questions (read from ``ctx['unresolved_questions']`` or ``ctx['questions']``).
    """
    ctx = ctx or {}
    issues: List[str] = []
    text = content or ""
    length = len(text)
    if length < 20:
        issues.append(f"content too short ({length} chars, minimum 20)")
        return _result(0, 4, False, issues, GATE_NAMES[0])

    unresolved = ctx.get("unresolved_questions", ctx.get("questions"))
    if unresolved is None:
        unresolved = []
    if not isinstance(unresolved, (list, tuple)):
        unresolved = [unresolved]
    max_unresolved = int(ctx.get("max_unresolved_questions", _DEFAULT_MAX_UNRESOLVED))
    n_unresolved = len(unresolved)
    if n_unresolved > max_unresolved:
        issues.append(
            f"{n_unresolved} unresolved questions exceed the boundary of {max_unresolved}"
        )
        return _result(0, 5, False, issues, GATE_NAMES[0])

    issues.append(
        f"task definition ok (length {length}, {n_unresolved} unresolved questions)"
    )
    return _result(0, 8, True, issues, GATE_NAMES[0])


def check_gate_1(content: str, ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Data quality gate.

    Requires ``ctx['data']`` to be a non-empty dict whose empty-value rate is
    at most 5%.
    """
    ctx = ctx or {}
    issues: List[str] = []
    data = ctx.get("data")
    if not isinstance(data, dict):
        issues.append("ctx['data'] must be a dict")
        return _result(1, 0, False, issues, GATE_NAMES[1])
    if not data:
        issues.append("ctx['data'] contains no fields")
        return _result(1, 4, False, issues, GATE_NAMES[1])

    total = len(data)
    empty = sum(1 for value in data.values() if _is_empty(value))
    rate = empty / total
    if rate > 0.05:
        issues.append(f"empty value rate {rate:.1%} exceeds 5% ({empty}/{total} fields)")
        return _result(1, 3, False, issues, GATE_NAMES[1])

    issues.append(f"empty value rate {rate:.1%} within the 5% limit ({empty}/{total} fields)")
    return _result(1, 8, True, issues, GATE_NAMES[1])


def check_gate_2(content: str, ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Review gate.

    The gate MUST actually invoke the review engine injected in
    ``ctx['review_engine']`` -- a callable with signature
    ``review_engine(content, risk) -> dict``.  It accepts either a compact
    result containing ``verdict`` / ``overall`` or a full ``jury.submit``
    result with nested ``adjudication`` metadata. Passing is never hard-coded.

    * engine configured, invocation fails  -> gate fails (score 0)
    * engine configured, verdict is
      ``critical``/``serious``/``inconclusive``/``rejected``/``fail`` or ``overall < 60``
      or ``hallucination_guard.risk_level`` is high -> gate fails
    * a simulated or single-provider jury result is flagged for human review
      instead of being treated as evidence of independent review
    * no engine configured + high/medium risk -> score 5, marked for human
      review (``soft_fail=True``). This does NOT hard-fail so the package can
      run without an LLM backend, but it is still flagged.
    * no engine configured + low risk -> gate passes (offline operation).
    """
    ctx = ctx or {}
    issues: List[str] = []
    risk = str(ctx.get("risk_level", "medium")).lower()
    engine: Optional[Callable[[str, str], dict]] = ctx.get("review_engine")

    if engine is None:
        if risk in ("high", "medium"):
            issues.append(
                "review engine not configured; high/medium risk requires human review"
            )
            return _result(2, 5, False, issues, GATE_NAMES[2], soft_fail=True)
        issues.append("no review engine configured; low risk accepted for offline operation")
        return _result(2, 8, True, issues, GATE_NAMES[2])

    try:
        outcome = engine(content, risk)
    except Exception as exc:  # engine present but broken -> must fail
        issues.append(f"review engine invocation failed: {exc}")
        return _result(2, 0, False, issues, GATE_NAMES[2])

    if not isinstance(outcome, dict):
        issues.append("review engine returned a non-dict result")
        return _result(2, 0, False, issues, GATE_NAMES[2])

    # A full Jury result stores the machine decision under ``adjudication``.
    # Normalize it before interpreting verdict, score, guard and assurance.
    normalized = dict(outcome)
    nested = outcome.get("adjudication")
    if isinstance(nested, dict):
        normalized = dict(nested)
        for key in ("hallucination_guard", "panel_integrity", "delivery_status"):
            if key in outcome and key not in normalized:
                normalized[key] = outcome[key]
        panel_summary = normalized.get("panel_summary")
        if isinstance(panel_summary, dict) and "overall" not in normalized:
            normalized["overall"] = panel_summary.get("average_score")

    verdict = str(normalized.get("verdict", "")).lower()
    overall = normalized.get("overall")
    hallucination_risk = None
    hg = normalized.get("hallucination_guard")
    if isinstance(hg, dict):
        hallucination_risk = hg.get("risk_level")

    rejected_verdicts = (
        "critical",
        "serious",
        "serious-issues",
        "inconclusive",
        "rejected",
        "fail",
        "failed",
        "denied",
    )
    if verdict in rejected_verdicts:
        issues.append(f"review engine returned rejecting verdict '{verdict}'")
        return _result(2, 3, False, issues, GATE_NAMES[2])

    if isinstance(overall, (int, float)) and overall < 60:
        issues.append(f"review engine overall score {overall} is below the 60 threshold")
        return _result(2, 3, False, issues, GATE_NAMES[2])

    if hallucination_risk and str(hallucination_risk).lower() in ("high", "critical"):
        issues.append(f"hallucination guard reports risk level '{hallucination_risk}'")
        return _result(2, 3, False, issues, GATE_NAMES[2])

    integrity = normalized.get("panel_integrity")
    if isinstance(integrity, dict):
        review_mode = integrity.get("review_mode")
        if review_mode in ("offline-simulation", "single-provider"):
            issues.append(
                f"review mode '{review_mode}' does not establish independent review; human review required"
            )
            return _result(2, 5, False, issues, GATE_NAMES[2], soft_fail=True)
        if integrity.get("partial_failure"):
            issues.append("review panel had failed seats; human review required")
            return _result(2, 5, False, issues, GATE_NAMES[2], soft_fail=True)

    if normalized.get("insufficient_data"):
        issues.append("review engine reported insufficient review data")
        return _result(2, 3, False, issues, GATE_NAMES[2])

    issues.append(f"review engine approved (verdict='{verdict}', overall={overall})")
    return _result(2, 8, True, issues, GATE_NAMES[2])


def check_gate_3(content: str, ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Bias scan gate.

    Detects confirmation, anchoring and survivorship bias signals using
    English and Chinese keyword lists.
    """
    ctx = ctx or {}
    issues: List[str] = []
    text = (content or "").lower()
    hits: List[str] = []
    for bias_name, keywords in _BIAS_KEYWORDS.items():
        for keyword in keywords:
            if keyword in text:
                hits.append(f"{bias_name}: '{keyword}'")
                break
    if hits:
        issues.append("possible bias signals detected: " + "; ".join(hits))
        return _result(3, 3, False, issues, GATE_NAMES[3])

    issues.append("no obvious bias keywords detected")
    return _result(3, 8, True, issues, GATE_NAMES[3])


def check_gate_4(content: str, ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Output quality gate: non-empty and at least 50 characters."""
    ctx = ctx or {}
    issues: List[str] = []
    text = content or ""
    if not text:
        issues.append("output content is empty")
        return _result(4, 0, False, issues, GATE_NAMES[4])
    if len(text) < 50:
        issues.append(f"output too short ({len(text)} chars, minimum 50)")
        return _result(4, 4, False, issues, GATE_NAMES[4])

    issues.append(f"output quality ok ({len(text)} chars)")
    return _result(4, 8, True, issues, GATE_NAMES[4])


def check_gate_5(content: str, ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Delivery gate: ctx['delivery'] must carry recipient, subject, signature."""
    ctx = ctx or {}
    issues: List[str] = []
    delivery = ctx.get("delivery")
    if not isinstance(delivery, dict):
        issues.append("ctx['delivery'] must be a dict with recipient/subject/signature")
        return _result(5, 0, False, issues, GATE_NAMES[5])

    missing = [key for key in ("recipient", "subject", "signature") if not delivery.get(key)]
    if missing:
        issues.append("delivery missing required fields: " + ", ".join(missing))
        return _result(5, 3, False, issues, GATE_NAMES[5])

    issues.append("delivery metadata complete")
    return _result(5, 8, True, issues, GATE_NAMES[5])


def check_all_gates(content: str, ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run all six gates and aggregate the outcome.

    The overall ``passed`` flag is True only when every gate passed its
    threshold. Gates flagged with ``soft_fail`` (e.g. the review gate without
    a configured engine) do not count toward ``hard_failed`` but still make
    ``passed`` False and set ``needs_human_review``.
    """
    ctx = dict(ctx or {})
    results = [
        check_gate_0(content, ctx),
        check_gate_1(content, ctx),
        check_gate_2(content, ctx),
        check_gate_3(content, ctx),
        check_gate_4(content, ctx),
        check_gate_5(content, ctx),
    ]
    passed = all(result["passed"] for result in results)
    hard_failed = any(
        result["passed"] is False and not result.get("soft_fail") for result in results
    )
    needs_human_review = any(result.get("soft_fail") for result in results)
    return {
        "passed": passed,
        "hard_failed": hard_failed,
        "needs_human_review": needs_human_review,
        "results": results,
        "summary": {
            "passed_gates": sum(1 for r in results if r["passed"]),
            "total_gates": len(results),
        },
    }


def gate_definitions() -> List[Dict[str, Any]]:
    """Return metadata for all six gates (used by the HTTP service and CLI)."""
    return [
        {
            "gate": index,
            "name": name,
            "description": _GATE_DESCRIPTIONS[index],
            "threshold": GATE_THRESHOLDS[index],
        }
        for index, name in GATE_NAMES.items()
    ]
