"""Evidence-aware repair planning and re-review verification.

Absence from a weak or simulated re-review is not evidence that a defect was
fixed.  This module therefore distinguishes ``all_findings_absent`` from
``all_resolved``: the latter is only true after an adequately independent
re-review and an acceptable re-review verdict.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

__all__ = [
    "FixItem",
    "RepairChecklist",
    "build_checklist",
    "verify_fixed",
    "to_json",
]


@dataclass
class FixItem:
    """A finding tracked through repair and evidence-based verification."""

    finding_id: str
    severity: str
    summary: str
    detail: str
    suggestion: str
    status: str = "open"  # open | verified | rejected | not_actionable
    note: str = ""


@dataclass
class RepairChecklist:
    """Ordered list of repair decisions derived from an adjudication."""

    items: list[FixItem] = field(default_factory=list)

    def require_fix(self) -> list[FixItem]:
        """Return findings that still need an implementation or human decision."""
        return [item for item in self.items if item.status == "open"]

    def open_count(self) -> int:
        return len(self.require_fix())


def build_checklist(result: Mapping[str, Any]) -> RepairChecklist:
    """Build a checklist without silently marking a finding as fixed.

    Every non-information finding is open until someone either repairs and
    verifies it or explicitly rejects it.  Informational observations are kept
    in the audit trail as ``not_actionable`` rather than being mislabelled as a
    completed fix.
    """
    adjudication = result.get("adjudication", result)
    findings = adjudication.get("findings", []) if isinstance(adjudication, Mapping) else []
    checklist = RepairChecklist()
    for finding in findings if isinstance(findings, list) else []:
        if not isinstance(finding, Mapping):
            continue
        severity = str(finding.get("severity") or "minor").lower()
        item = FixItem(
            finding_id=str(finding.get("id") or "F000"),
            severity=severity,
            summary=str(finding.get("summary") or ""),
            detail=str(finding.get("detail") or ""),
            suggestion=str(finding.get("suggestion") or ""),
        )
        if severity == "info":
            item.status = "not_actionable"
            item.note = "informational observation; no repair is required"
        checklist.items.append(item)
    return checklist


def _tokens(text: str) -> set[str]:
    """Tokenize English and CJK finding summaries without an NLP dependency."""
    normalized = str(text or "").casefold()
    words = set(re.findall(r"[a-z0-9_]{2,}", normalized))
    if words:
        return words
    cjk = [char for char in normalized if "\u4e00" <= char <= "\u9fff"]
    return {"".join(cjk[index : index + 2]) for index in range(len(cjk) - 1)}


def _overlap(left: str, right: str) -> float:
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _match_finding(adjudication: Mapping[str, Any], item: FixItem) -> bool:
    """Return whether a semantically similar prior finding still appears.

    Generated finding IDs begin again at ``F1`` for every review, so comparing
    IDs directly creates false matches.  An explicit ``origin_finding_id`` is
    honored when a caller deliberately carries it forward; otherwise matching is
    based on stable summary overlap only.
    """
    findings = adjudication.get("findings", [])
    for finding in findings if isinstance(findings, list) else []:
        if not isinstance(finding, Mapping):
            continue
        if finding.get("origin_finding_id") == item.finding_id:
            return True
        if _overlap(str(finding.get("summary") or ""), item.summary) >= 0.5:
            return True
    return False


def _review_assurance(re_review: Mapping[str, Any], adjudication: Mapping[str, Any]) -> tuple[bool, str]:
    """Determine whether the re-review can close findings on its own evidence."""
    integrity = re_review.get("panel_integrity")
    if not isinstance(integrity, Mapping):
        return False, "missing panel integrity metadata"
    if integrity.get("review_mode") != "multi-provider":
        return False, f"review mode is {integrity.get('review_mode')!r}, not multi-provider"
    if integrity.get("partial_failure"):
        return False, "one or more requested reviewer seats failed"
    if int(adjudication.get("review_count") or 0) < 2:
        return False, "fewer than two schema-valid re-reviews were available"
    if adjudication.get("insufficient_data"):
        return False, "re-review adjudication reported insufficient data"
    return True, "independent multi-provider re-review completed"


def _has_explicit_evidence(evidence: Optional[Mapping[str, Any]]) -> bool:
    """Recognize a non-empty caller-supplied evidence record.

    The record is included verbatim in the returned report for auditability; the
    library does not pretend to independently execute or validate it.
    """
    return isinstance(evidence, Mapping) and bool(evidence)


def verify_fixed(
    re_review: Mapping[str, Any],
    checklist: RepairChecklist,
    match_fn: Callable[[Mapping[str, Any], FixItem], bool] = _match_finding,
    verification_evidence: Optional[Mapping[str, Any]] = None,
    allow_explicit_evidence: bool = False,
) -> dict[str, Any]:
    """Verify a repair checklist against an independent re-review.

    ``all_findings_absent`` says the old findings did not recur.  ``all_resolved``
    additionally requires multi-provider re-review assurance and an
    ``approved``/``minor-issues`` re-review verdict.  A caller may opt into
    ``allow_explicit_evidence`` for a non-empty, auditable local evidence record
    (for example, test output plus a code-review link), but that fact is surfaced
    rather than silently treated as independent review.
    """
    adjudication = re_review.get("adjudication", re_review)
    if not isinstance(adjudication, Mapping):
        raise ValueError("re_review must be an adjudication or a jury result mapping")

    independently_assured, assurance_note = _review_assurance(re_review, adjudication)
    explicit_evidence_used = allow_explicit_evidence and _has_explicit_evidence(verification_evidence)
    closure_assured = independently_assured or explicit_evidence_used
    still_present: list[str] = []

    for item in checklist.items:
        if item.status in {"rejected", "verified", "not_actionable"}:
            continue
        if match_fn(adjudication, item):
            item.status = "open"
            item.note = "finding still present after fix attempt"
            still_present.append(item.finding_id)
        elif closure_assured:
            item.status = "verified"
            item.note = (
                "finding absent in independently assured re-review"
                if independently_assured
                else "finding absent; caller-supplied verification evidence accepted"
            )
        else:
            item.status = "open"
            item.note = (
                "finding absent from re-review, but closure evidence is insufficient: "
                + assurance_note
            )

    all_findings_absent = not still_present
    verdict = str(adjudication.get("verdict") or "unknown")
    re_review_acceptable = verdict in {"approved", "minor-issues"}
    all_resolved = checklist.open_count() == 0 and closure_assured and re_review_acceptable
    if all_resolved:
        verification_status = "verified"
    elif not closure_assured:
        verification_status = "needs-independent-review"
    elif not re_review_acceptable:
        verification_status = "re-review-has-new-or-serious-issues"
    else:
        verification_status = "findings-still-open"

    return {
        "all_resolved": all_resolved,
        "all_findings_absent": all_findings_absent,
        "open": checklist.open_count(),
        "verified": sum(1 for item in checklist.items if item.status == "verified"),
        "verification_status": verification_status,
        "assurance": {
            "independent_re_review": independently_assured,
            "note": assurance_note,
            "explicit_evidence_accepted": explicit_evidence_used,
        },
        "verification_evidence": dict(verification_evidence or {}),
        "items": [vars(item) for item in checklist.items],
        "re_review_verdict": verdict,
    }


def to_json(checklist: RepairChecklist, indent: int = 2) -> str:
    """Serialize a checklist for persistence or CI logs."""
    return json.dumps([vars(item) for item in checklist.items], ensure_ascii=False, indent=indent)
