"""Deterministic adjudication engine (pure logic, no LLM).

Consumes the JSON-shaped review responses produced by panel reviewers (or the
offline stub client) and merges them into a single auditable decision: final
verdict, confidence, findings clustered across reviewers with per-finding
consensus levels, strengths, and a panel summary.

The output structure mirrors the schema described in
:data:`double_gate.prompts.ADJUDICATION_SYSTEM_PROMPT`, so an LLM-based
adjudicator and this deterministic engine can be swapped freely.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "adjudicate",
    "REVIEW_VERDICTS",
    "INCONCLUSIVE_VERDICT",
    "_parse_response",
    "_merge_findings",
    "_determine_verdict",
    "_convergence_label",
    "_severity_rank",
    "_max_severity",
]

REVIEW_VERDICTS = ("approved", "minor-issues", "serious-issues", "critical")
INCONCLUSIVE_VERDICT = "inconclusive"

_SEVERITY_ORDER = {"info": 0, "minor": 1, "major": 2, "critical": 3}

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


# --------------------------------------------------------------------------- #
# response parsing
# --------------------------------------------------------------------------- #
def _extract_text(response: Any) -> Tuple[Optional[str], bool]:
    """Pull the raw text out of a response, tolerating ModelResponse objects.

    Returns ``(text, ok)``. A duck-typed object with ``content`` /
    ``success`` attributes is handled without importing ``models``, keeping
    this module pure and dependency-free.
    """
    if isinstance(response, str):
        return response, True
    content = getattr(response, "content", None)
    success = getattr(response, "success", True)
    if not success or content is None:
        return None, False
    return content, True


def _parse_response(raw: Any) -> Optional[Dict[str, Any]]:
    """Parse a review response into a dict, tolerating messy output.

    Handles: markdown code fences (`````json ... `````), pure JSON, and JSON
    embedded in surrounding prose (first ``{`` .. last ``}``). Returns ``None``
    when nothing parseable is found.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()

    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except (ValueError, TypeError):
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, dict):
                return data
        except (ValueError, TypeError):
            return None
    return None


# --------------------------------------------------------------------------- #
# severity helpers
# --------------------------------------------------------------------------- #
def _severity_rank(severity: Any) -> int:
    """Map a severity label to its ordering rank (unknown -> info)."""
    if not severity:
        return _SEVERITY_ORDER["info"]
    return _SEVERITY_ORDER.get(str(severity).strip().lower(), _SEVERITY_ORDER["info"])


def _max_severity(severities: Sequence[Any]) -> str:
    """Return the highest-severity label from a collection (info if empty)."""
    best = "info"
    best_rank = -1
    for value in severities:
        if not value:
            continue
        rank = _severity_rank(value)
        if rank > best_rank:
            best_rank = rank
            best = str(value).strip().lower()
    return best


def _convergence_label(agreement: int, total: int) -> str:
    """Classify the strength of agreement across independent sources.

    A label is only as strong as the panel denominator.  In particular, three
    agreeing reviewers are *not* unanimous in a four-reviewer panel, and a
    two-versus-two result is a split rather than a majority.
    """
    agreement = int(agreement or 0)
    total = int(total or 0)
    if total <= 1:
        return "single"
    if agreement >= total:
        return "unanimous"
    if agreement >= 2 and agreement > total / 2:
        return "majority"
    return "split"


def _similarity_tokens(text: str) -> set[str]:
    """Return stable tokens for finding clustering.

    Word tokens are much less likely than raw character sets to merge unrelated
    English findings.  For CJK-only text, use overlapping character bigrams so
    the same deterministic fallback remains useful without a tokenizer extra.
    """
    normalized = str(text or "").casefold()
    words = set(re.findall(r"[a-z0-9_]{2,}", normalized))
    if words:
        return words
    cjk = [char for char in normalized if "\u4e00" <= char <= "\u9fff"]
    return {"".join(cjk[index : index + 2]) for index in range(len(cjk) - 1)}


def _jaccard(left: str, right: str) -> float:
    """Jaccard similarity over normalized word/CJK-bigram tokens (0.0..1.0)."""
    set_left = _similarity_tokens(left)
    set_right = _similarity_tokens(right)
    if not set_left and not set_right:
        return 1.0
    union = set_left | set_right
    if not union:
        return 0.0
    return len(set_left & set_right) / len(union)


# --------------------------------------------------------------------------- #
# finding clustering
# --------------------------------------------------------------------------- #
def _findings_match(
    summary: str,
    category: Any,
    severity: Any,
    representative: Dict[str, Any],
) -> bool:
    """Decide whether a finding belongs to an existing cluster.

    Matches on exact ``summary``, or on Jaccard >= 0.5 *and* identical
    ``category`` / ``severity``.
    """
    rep_summary = str(representative.get("summary", "")).strip()
    if not summary or not rep_summary:
        return False
    if summary.casefold() == rep_summary.casefold():
        return True
    same_label = (
        str(category or "").strip().lower() == str(representative.get("category") or "").strip().lower()
        and str(severity or "").strip().lower()
        == str(representative.get("severity") or "").strip().lower()
    )
    if not same_label:
        return False
    return _jaccard(summary, rep_summary) >= 0.6


def _merge_findings(
    source_findings: Sequence[Tuple[int, Dict[str, Any]]], total_sources: int
) -> List[Dict[str, Any]]:
    """Cluster findings across reviewers and merge each cluster.

    ``source_findings`` is a sequence of ``(source_index, finding_dict)``.
    Returns merged findings with ``id``, ``severity`` (max across the cluster),
    ``consensus`` and ``panel_agreement`` (number of sources).
    """
    clusters: List[Dict[str, Any]] = []
    for source, finding in source_findings:
        finding = finding or {}
        summary = str(finding.get("summary", "")).strip()
        if not summary:
            continue
        category = finding.get("category")
        severity = finding.get("severity")

        cluster = None
        for candidate in clusters:
            if _findings_match(summary, category, severity, candidate["representative"]):
                cluster = candidate
                break
        if cluster is None:
            cluster = {"representative": finding, "items": [], "sources": []}
            clusters.append(cluster)
        cluster["items"].append(finding)
        cluster["sources"].append(source)
        if _severity_rank(severity) > _severity_rank(cluster["representative"].get("severity")):
            cluster["representative"] = finding

    merged: List[Dict[str, Any]] = []
    for index, cluster in enumerate(clusters, start=1):
        rep = cluster["representative"]
        items = cluster["items"]
        details = [item.get("detail") for item in items if item.get("detail")]
        suggestions = [item.get("suggestion") for item in items if item.get("suggestion")]
        source_agreement = len(set(cluster["sources"]))
        merged.append(
            {
                "id": f"F{index}",
                "severity": _max_severity([item.get("severity") for item in items]),
                "category": rep.get("category"),
                "summary": rep.get("summary", ""),
                "consensus": (
                    "single"
                    if source_agreement == 1
                    else _convergence_label(source_agreement, total_sources)
                ),
                "panel_agreement": source_agreement,
                "detail": _join_parts(details) or rep.get("detail") or "",
                "suggestion": _join_parts(suggestions) or rep.get("suggestion") or "",
                "sources": sorted(set(cluster["sources"])),
            }
        )
    return merged


def _join_parts(parts: Sequence[Any]) -> str:
    """Dedupe-and-join detail/suggestion strings, preserving order."""
    seen = set()
    kept = []
    for part in parts:
        text = str(part).strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        kept.append(text)
    return " | ".join(kept)


def _dedupe_strengths(strengths: Sequence[str]) -> List[str]:
    seen = set()
    result = []
    for strength in strengths:
        key = strength.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(strength)
    return result


def _submission_id(submission: Any) -> Optional[str]:
    if isinstance(submission, dict):
        return submission.get("id") or submission.get("submission_id")
    if isinstance(submission, str):
        return submission
    return None


# --------------------------------------------------------------------------- #
# verdict decision
# --------------------------------------------------------------------------- #
def _determine_verdict(
    verdicts: Sequence[str],
    scores: Sequence[float],
    merged_findings: Optional[Sequence[Dict[str, Any]]] = None,
) -> str:
    """Decide the final verdict from reviewer verdicts, scores and findings.

    Order of precedence:
    1. any critical verdict/finding        -> ``critical``
    2. any serious verdict, major finding, or avg score < 55 -> ``serious-issues``
    3. >= 60% approved and avg score >= 70 -> ``approved``
    4. otherwise                           -> ``minor-issues``
    """
    if not verdicts:
        return INCONCLUSIVE_VERDICT
    findings = list(merged_findings or [])
    max_severity = _max_severity([f.get("severity") for f in findings])
    if any(v == "critical" for v in verdicts) or max_severity == "critical":
        return "critical"
    average = sum(scores) / len(scores) if scores else None
    if (
        any(v == "serious-issues" for v in verdicts)
        or max_severity == "major"
        or (average is not None and average < 55)
    ):
        return "serious-issues"
    if (
        len(verdicts) >= 2
        and verdicts
        and sum(1 for v in verdicts if v == "approved") / len(verdicts) >= 0.6
        and average is not None
        and average >= 70
    ):
        return "approved"
    return "minor-issues"


def _panel_confidence(label: str, successful: int, confidences: Sequence[float]) -> float:
    """Blend consensus strength with reviewers' self-reported confidence."""
    base = {"unanimous": 0.9, "majority": 0.72, "split": 0.35, "single": 0.45}.get(
        label, 0.35
    )
    if successful == 0:
        return 0.1
    average = sum(confidences) / len(confidences) if confidences else None
    if average is not None:
        blended = 0.5 * base + 0.5 * average
        return round(max(0.0, min(1.0, blended)), 2)
    return round(base, 2)


# --------------------------------------------------------------------------- #
# response normalisation
# --------------------------------------------------------------------------- #
def _normalise_review(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Validate enough of a review schema to safely use it in adjudication.

    Reviewers can add extra keys, but a response without a canonical verdict is
    not a usable review.  Numeric values are clamped to their documented ranges
    so one malformed provider response cannot inflate panel confidence.
    """
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in REVIEW_VERDICTS:
        return None
    normalized = dict(data)
    normalized["verdict"] = verdict

    score = data.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score):
        normalized["score"] = max(0.0, min(100.0, float(score)))
    else:
        normalized["score"] = None

    confidence = data.get("confidence")
    if (
        isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and math.isfinite(confidence)
    ):
        normalized["confidence"] = max(0.0, min(1.0, float(confidence)))
    else:
        normalized["confidence"] = None

    normalized["findings"] = (
        [finding for finding in data.get("findings", []) if isinstance(finding, dict)]
        if isinstance(data.get("findings", []), list)
        else []
    )
    normalized["strengths"] = (
        [strength.strip() for strength in data.get("strengths", []) if isinstance(strength, str) and strength.strip()]
        if isinstance(data.get("strengths", []), list)
        else []
    )
    return normalized


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def adjudicate(submission: Any, panel_responses: Sequence[Any]) -> Dict[str, Any]:
    """Merge independent review responses into a single adjudication report.

    ``panel_responses`` accepts raw strings, or any object exposing
    ``content`` / ``success`` attributes (e.g. :class:`models.ModelResponse`).
    Unparseable responses are counted as failed but never raise.

    Returns a dict shaped like:
      submission_id, verdict, confidence, convergence, panel_summary,
      findings (clustered, with consensus), strengths, review_count,
      insufficient_data
    """
    parsed: List[Tuple[int, Dict[str, Any]]] = []
    failed = 0
    invalid_schema = 0
    for source_index, response in enumerate(panel_responses):
        text, ok = _extract_text(response)
        if not ok:
            failed += 1
            continue
        data = _parse_response(text)
        if data is None:
            failed += 1
            continue
        normalized = _normalise_review(data)
        if normalized is None:
            failed += 1
            invalid_schema += 1
            continue
        parsed.append((source_index, normalized))

    verdicts: List[str] = []
    scores: List[float] = []
    confidences: List[float] = []
    source_findings: List[Tuple[int, Dict[str, Any]]] = []
    strengths: List[str] = []

    for source_index, data in parsed:
        verdict = data.get("verdict")
        if verdict in REVIEW_VERDICTS:
            verdicts.append(verdict)
        score = data.get("score")
        if score is not None:
            scores.append(float(score))
        confidence = data.get("confidence")
        if confidence is not None:
            confidences.append(float(confidence))
        for finding in data.get("findings") or []:
            if isinstance(finding, dict):
                source_findings.append((source_index, finding))
        for strength in data.get("strengths") or []:
            if isinstance(strength, str) and strength.strip():
                strengths.append(strength.strip())

    merged_findings = _merge_findings(source_findings, total_sources=len(parsed))
    merged_strengths = _dedupe_strengths(strengths)

    verdict = _determine_verdict(verdicts, scores, merged_findings)
    average_score = round(sum(scores) / len(scores), 1) if scores else None

    verdict_counts = {v: verdicts.count(v) for v in REVIEW_VERDICTS}
    agreement = max(verdict_counts.values()) if verdicts else 0
    agreement_level = _convergence_label(agreement, len(verdicts))

    panel_summary = {
        "total_reviews": len(panel_responses),
        "successful_reviews": len(parsed),
        "failed_reviews": failed,
        "invalid_schema_reviews": invalid_schema,
        "verdicts": verdict_counts,
        "average_score": average_score,
        "agreement_level": agreement_level,
    }

    return {
        "submission_id": _submission_id(submission),
        "verdict": verdict,
        "confidence": _panel_confidence(agreement_level, len(parsed), confidences),
        "convergence": agreement_level,
        "panel_summary": panel_summary,
        "findings": merged_findings,
        "strengths": merged_strengths,
        "review_count": len(parsed),
        "insufficient_data": len(parsed) == 0,
    }
