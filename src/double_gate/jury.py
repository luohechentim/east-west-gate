"""End-to-end orchestration for an auditable Double Gate review.

The orchestrator deliberately separates three kinds of evidence:

* reviewer responses, which are adjudicated deterministically;
* artifacts cited *by those reviewer responses*, which are checked by the
  hallucination guard; and
* citable artifacts in each merged finding, which drive conservative automatic
  triage.

The bundled ``stub`` panel is an offline demonstration fixture.  It exercises
the pipeline but is explicitly labelled as simulated and can never establish
independent-review assurance or a delivery-ready result.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from . import guard as artifact_guard
from .adjudicator import INCONCLUSIVE_VERDICT, adjudicate
from .models import ModelResponse, call_panel, registered_vendors
from .prompts import build_review_prompt

__all__ = [
    "Jury",
    "PanelConfigurationError",
    "PANELS",
    "get_jury",
    "submit",
    "get_result",
    "catalog",
]

logger = logging.getLogger("double_gate.jury")

_RISK_LEVELS = {"low", "medium", "high"}


class PanelConfigurationError(ValueError):
    """Raised when a configured review panel cannot be used safely."""


# The package is runnable without credentials, but these reviewers are a
# deterministic test fixture rather than an independent panel.
PANELS: dict[str, dict[str, Any]] = {
    "deep": {
        "risk": "high",
        "description": "offline demonstration panel: three role prompts backed by the deterministic stub",
        "models": [
            {"vendor": "stub", "provider": "offline-stub", "model": "stub-primary", "role": "primary"},
            {"vendor": "stub", "provider": "offline-stub", "model": "stub-guard", "role": "guard"},
            {"vendor": "stub", "provider": "offline-stub", "model": "stub-critic", "role": "critic"},
        ],
    },
    "standard": {
        "risk": "medium",
        "description": "offline demonstration panel: two role prompts backed by the deterministic stub",
        "models": [
            {"vendor": "stub", "provider": "offline-stub", "model": "stub-primary", "role": "primary"},
            {"vendor": "stub", "provider": "offline-stub", "model": "stub-critic", "role": "critic"},
        ],
    },
    "quick": {
        "risk": "low",
        "description": "offline demonstration panel: one deterministic stub response",
        "models": [
            {"vendor": "stub", "provider": "offline-stub", "model": "stub-primary", "role": "primary"}
        ],
    },
}


def _copy_panel(panel: Mapping[str, Any]) -> dict[str, Any]:
    copied = dict(panel)
    copied["models"] = [dict(member) for member in panel.get("models", [])]
    return copied


def _validate_panel(name: str, panel: Any) -> dict[str, Any]:
    """Validate and normalize a single panel definition from configuration."""
    if not isinstance(panel, Mapping):
        raise PanelConfigurationError(f"panel {name!r} must be a JSON object")
    raw_members = panel.get("models")
    if not isinstance(raw_members, list) or not raw_members:
        raise PanelConfigurationError(f"panel {name!r} must contain a non-empty models list")

    known_vendors = set(registered_vendors())
    members: list[dict[str, Any]] = []
    for index, raw_member in enumerate(raw_members, start=1):
        if not isinstance(raw_member, Mapping):
            raise PanelConfigurationError(f"panel {name!r} member {index} must be an object")
        member = dict(raw_member)
        vendor = str(member.get("vendor") or "").strip()
        if not vendor:
            raise PanelConfigurationError(f"panel {name!r} member {index} is missing vendor")
        if vendor not in known_vendors:
            available = ", ".join(sorted(known_vendors)) or "none"
            raise PanelConfigurationError(
                f"panel {name!r} member {index} uses unknown vendor {vendor!r}; available: {available}"
            )
        provider = str(
            member.get("provider") or member.get("independence_group") or vendor
        ).strip()
        if not provider:
            raise PanelConfigurationError(f"panel {name!r} member {index} has an empty provider")
        if "client_kwargs" in member and not isinstance(member["client_kwargs"], Mapping):
            raise PanelConfigurationError(
                f"panel {name!r} member {index} client_kwargs must be an object"
            )
        member["vendor"] = vendor
        member["provider"] = provider
        member["model"] = str(member.get("model") or "")
        member["role"] = str(member.get("role") or "")
        members.append(member)

    description = str(panel.get("description") or f"configured {name} review panel")
    risk = str(panel.get("risk") or "").strip().lower()
    if risk and risk not in _RISK_LEVELS:
        raise PanelConfigurationError(f"panel {name!r} has unsupported risk level {risk!r}")
    return {"risk": risk, "description": description, "models": members}


def _default_panels() -> dict[str, dict[str, Any]]:
    """Resolve built-in panels plus optional environment configuration.

    ``DOUBLE_GATE_PANELS_JSON`` is a JSON object keyed by panel name.  Secrets
    belong in environment variables referenced by ``api_key_env`` on a member,
    never in this JSON value or in a checked-in config file.
    """
    panels = {name: _copy_panel(panel) for name, panel in PANELS.items()}
    raw = os.getenv("DOUBLE_GATE_PANELS_JSON", "").strip()
    if not raw:
        return {name: _validate_panel(name, panel) for name, panel in panels.items()}
    try:
        configured = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PanelConfigurationError(f"DOUBLE_GATE_PANELS_JSON is invalid JSON: {exc.msg}") from exc
    if not isinstance(configured, Mapping):
        raise PanelConfigurationError("DOUBLE_GATE_PANELS_JSON must be a JSON object keyed by panel name")
    for name, panel in configured.items():
        normalized_name = str(name).strip()
        if not normalized_name:
            raise PanelConfigurationError("DOUBLE_GATE_PANELS_JSON contains an empty panel name")
        panels[normalized_name] = panel
    return {name: _validate_panel(name, panel) for name, panel in panels.items()}


def _resolve_panel(name: Optional[str], risk_level: str, offline: bool = False) -> dict[str, Any]:
    """Select a validated panel, forcing the bundled fixture in offline mode."""
    if risk_level not in _RISK_LEVELS:
        raise ValueError(f"risk_level must be one of {', '.join(sorted(_RISK_LEVELS))}")
    panels = {panel_name: _validate_panel(panel_name, panel) for panel_name, panel in PANELS.items()} if offline else _default_panels()
    if name:
        if name not in panels:
            available = ", ".join(sorted(panels))
            raise PanelConfigurationError(f"unknown panel {name!r}; available: {available}")
        return _copy_panel(panels[name])
    default_name = {"high": "deep", "medium": "standard", "low": "quick"}[risk_level]
    return _copy_panel(panels[default_name])


def _normalize_input(content: str, system_prompt: str) -> str:
    return f"{system_prompt}\n\n---\n\n{content}"


def _blind_dispatch(
    system: str, content: str, members: list[dict[str, Any]], submission_id: str
) -> tuple[list[dict[str, Any]], list[ModelResponse]]:
    """Call every requested reviewer and return serializable, ordered records."""
    responses = call_panel(system, content, members)
    entries: list[dict[str, Any]] = []
    for index, member in enumerate(members):
        response = responses[index] if index < len(responses) else ModelResponse(
            vendor=str(member.get("vendor") or ""),
            model=str(member.get("model") or ""),
            success=False,
            error="panel dispatch did not return a response for this member",
        )
        entries.append(
            {
                "auditor_id": f"reviewer-{index + 1}",
                "model": response.model or member.get("model") or None,
                "vendor": response.vendor or member.get("vendor") or "",
                "provider": member.get("provider") or member.get("vendor") or "",
                "role": member.get("role") or "",
                "content": response.content,
                "success": response.success,
                "error": response.error,
                "duration_ms": response.duration_ms,
                "usage": response.usage or {},
            }
        )
    return entries, responses


def _panel_integrity(panel: Mapping[str, Any], raw_entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe what the returned review can and cannot prove."""
    members = panel.get("models", [])
    providers = sorted({str(member.get("provider") or member.get("vendor") or "") for member in members})
    providers = [provider for provider in providers if provider]
    vendors = sorted({str(member.get("vendor") or "") for member in members if member.get("vendor")})
    contains_stub = any(member.get("vendor") == "stub" for member in members)
    successful = sum(1 for entry in raw_entries if entry.get("success"))
    requested = len(members)

    if contains_stub:
        review_mode = "offline-simulation"
    elif len(providers) >= 2:
        review_mode = "multi-provider"
    else:
        review_mode = "single-provider"

    limitations: list[str] = []
    if contains_stub:
        limitations.append(
            "The bundled stub is deterministic demonstration data, not an independent expert review."
        )
    if len(providers) < 2 and not contains_stub:
        limitations.append(
            "Only one provider group is configured; cross-provider independence is not established."
        )
    if successful < requested:
        limitations.append(f"Only {successful} of {requested} requested reviewer seats returned successfully.")

    return {
        "review_mode": review_mode,
        "requested_reviewers": requested,
        "successful_reviewers": successful,
        "provider_groups": providers,
        "client_vendors": vendors,
        "partial_failure": successful < requested,
        "limitations": limitations,
    }


def _review_texts_for_guard(raw_entries: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Build guard input from reviewer output, never from the submitted content."""
    texts: dict[str, dict[str, str]] = {}
    for entry in raw_entries:
        content = entry.get("content")
        if entry.get("success") and isinstance(content, str) and content.strip():
            texts[str(entry["auditor_id"])] = {
                "content": content,
                "vendor": str(entry.get("provider") or entry.get("vendor") or ""),
            }
    return texts


def _scan_result_payload(result: Any) -> dict[str, Any]:
    """Serialize a finding-level guard result without exposing local root paths."""
    artifact = result.artifact
    return {
        "artifact": {
            "name": artifact.name,
            "type": artifact.type.value,
            "context": artifact.context,
            "line": artifact.line,
        },
        "verdict": result.verdict.value,
        "confidence": result.confidence,
        "evidence": result.evidence,
    }


def _verify_findings(findings: list[dict[str, Any]], codebase_root: str) -> dict[str, list[dict[str, Any]]]:
    """Verify citable artifacts in each merged finding for auditable triage."""
    evidence: dict[str, list[dict[str, Any]]] = {}
    for finding in findings:
        finding_id = str(finding.get("id") or "")
        if not finding_id:
            continue
        finding_text = "\n".join(
            str(finding.get(key) or "") for key in ("summary", "detail", "suggestion")
        )
        artifacts = artifact_guard.extract_artifacts(
            finding_text, source_model=f"finding:{finding_id}"
        )
        evidence[finding_id] = [
            _scan_result_payload(artifact_guard.scan_artifact(artifact, codebase_root))
            for artifact in artifacts
        ]
    return evidence


def _finding_evidence_state(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no-citable-artifact"
    verdicts = {str(row.get("verdict") or "") for row in rows}
    if artifact_guard.Verdict.LIKELY_HALLUCINATION.value in verdicts:
        return "unverified-artifact"
    if verdicts <= {artifact_guard.Verdict.VERIFIED.value}:
        return "verified"
    return "ambiguous-artifact"


def _classify(
    adjudication: dict[str, Any],
    finding_evidence: dict[str, list[dict[str, Any]]],
    panel_integrity: Mapping[str, Any],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Conservatively split findings into machine-triaged and human decisions.

    An automatic acceptance is intentionally rare: it requires a non-simulated,
    fully returned multi-provider panel, a majority or unanimity, a major/critical
    finding, and verifiable artifact evidence.  All other findings remain visible
    as explicit human decisions instead of being silently accepted.
    """
    auto: list[str] = []
    escalated: list[dict[str, Any]] = []
    trusted_panel = (
        panel_integrity.get("review_mode") == "multi-provider"
        and not panel_integrity.get("partial_failure")
    )
    for finding in adjudication.get("findings", []):
        finding_id = str(finding.get("id") or "")
        consensus = str(finding.get("consensus") or "single")
        severity = str(finding.get("severity") or "minor")
        state = _finding_evidence_state(finding_evidence.get(finding_id, []))
        accepted = (
            trusted_panel
            and consensus in {"unanimous", "majority"}
            and severity in {"major", "critical"}
            and state == "verified"
        )
        if accepted:
            auto.append(finding_id)
            continue
        reasons = []
        if not trusted_panel:
            reasons.append(f"panel_mode={panel_integrity.get('review_mode')}")
        if consensus not in {"unanimous", "majority"}:
            reasons.append(f"consensus={consensus}")
        if severity not in {"major", "critical"}:
            reasons.append(f"severity={severity}")
        if state != "verified":
            reasons.append(f"artifact_evidence={state}")
        escalated.append(
            {
                "finding_id": finding_id,
                "reason": "; ".join(reasons) or "requires human decision",
                "finding_summary": finding.get("summary", ""),
                "recommendation": "human decision: accept / reject / modify",
                "options": ["accept", "reject", "modify"],
            }
        )
    return auto, escalated


def _delivery_status(
    adjudication: Mapping[str, Any],
    panel_integrity: Mapping[str, Any],
    guard_report: Optional[artifact_guard.GuardReport],
    escalations: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    """Return a conservative recommendation, never a formal delivery approval."""
    reasons = list(panel_integrity.get("limitations", []))
    verdict = str(adjudication.get("verdict") or INCONCLUSIVE_VERDICT)
    if verdict in {INCONCLUSIVE_VERDICT, "critical", "serious-issues"}:
        reasons.append(f"Adjudication verdict is {verdict!r}.")
    if guard_report is not None:
        risk = str(guard_report.summary.get("overall_hallucination_risk") or "low")
        if risk in {"high", "critical"}:
            reasons.append(f"The reviewer-artifact guard reported {risk} risk.")
    if escalations:
        reasons.append(f"{len(escalations)} finding(s) still require a human decision.")
    if reasons:
        return "human-review-required", reasons
    return "candidate-for-human-approval", [
        "No automated blocker was found; a responsible human must still approve delivery."
    ]


class Jury:
    """Run an auditable panel review without implicit disk persistence.

    ``store_path`` is opt-in because a report can contain sensitive submitted
    content and reviewer output.  When supplied, records are written atomically
    with owner-only file permissions where supported by the host OS.
    """

    def __init__(self, codebase_root: Optional[str] = None, store_path: Optional[str] = None):
        self.codebase_root = os.path.abspath(os.path.expanduser(codebase_root or os.getcwd()))
        self.store_path = (
            os.path.abspath(os.path.expanduser(store_path)) if store_path else None
        )

    def submit(
        self,
        content: str,
        submission_type: str = "review",
        risk_level: str = "medium",
        context: Optional[dict[str, Any]] = None,
        panel_name: Optional[str] = None,
        verify_artifacts: bool = True,
        offline: bool = False,
    ) -> dict[str, Any]:
        """Review ``content`` and return a full, JSON-serializable audit record."""
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be a non-empty string")
        normalized_risk = str(risk_level or "").strip().lower()
        if normalized_risk not in _RISK_LEVELS:
            raise ValueError(f"risk_level must be one of {', '.join(sorted(_RISK_LEVELS))}")

        started = time.monotonic()
        submitted_at = datetime.now(timezone.utc).isoformat()
        submission_id = f"aud_{uuid.uuid4().hex[:12]}"
        panel = _resolve_panel(panel_name, normalized_risk, offline=offline)
        system_prompt = build_review_prompt(str(submission_type or "review"))
        canonical_input = _normalize_input(content, system_prompt)
        invocation_hash = hashlib.sha256(canonical_input.encode("utf-8")).hexdigest()[:16]

        logger.info(
            "[%s] submit: type=%s risk=%s panel=%s",
            submission_id,
            submission_type,
            normalized_risk,
            panel["description"],
        )
        raw_entries, raw_responses = _blind_dispatch(
            system_prompt, content, panel["models"], submission_id
        )
        integrity = _panel_integrity(panel, raw_entries)

        submission = {
            "id": submission_id,
            "type": str(submission_type or "review"),
            "risk_level": normalized_risk,
            "context": context or {},
            "submitted_at": submitted_at,
        }
        adjudication = adjudicate(submission, raw_responses)

        guard_report: Optional[artifact_guard.GuardReport] = None
        if verify_artifacts:
            guard_report = artifact_guard.guard(
                {"model_texts": _review_texts_for_guard(raw_entries)},
                codebase_root=self.codebase_root,
            )
            adjudication = artifact_guard.inject_guard_into_report(adjudication, guard_report)

        finding_evidence = (
            _verify_findings(adjudication.get("findings", []), self.codebase_root)
            if verify_artifacts
            else {}
        )
        adjudication["finding_evidence"] = finding_evidence
        auto_accepted, escalations = _classify(adjudication, finding_evidence, integrity)
        delivery_status, delivery_reasons = _delivery_status(
            adjudication, integrity, guard_report, escalations
        )

        result = {
            "submission_id": submission_id,
            "status": "completed" if delivery_status == "candidate-for-human-approval" else "completed_with_limitations",
            "submission_type": str(submission_type or "review"),
            "risk_level": normalized_risk,
            "panel": [
                {
                    "vendor": member["vendor"],
                    "provider": member["provider"],
                    "model": member.get("model") or None,
                    "role": member.get("role") or None,
                }
                for member in panel["models"]
            ],
            "panel_integrity": integrity,
            "raw_responses": raw_entries,
            "adjudication": adjudication,
            "hallucination_guard": guard_report.to_dict() if guard_report else None,
            "auto_accepted": auto_accepted,
            "human_escalations": escalations,
            "delivery_status": delivery_status,
            "delivery_reasons": delivery_reasons,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "audit_trail": {
                "canonical_id": submission_id,
                "invocation_hash": invocation_hash,
                "submitted_at": submitted_at,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "scan_root_label": os.path.basename(self.codebase_root) or ".",
                "panel": panel["description"],
                "persistence": "enabled" if self.store_path else "disabled",
            },
        }
        self._save_result(submission_id, result)
        return result

    def get_result(self, submission_id: str) -> Optional[dict[str, Any]]:
        """Fetch a persisted result, or ``None`` when persistence is disabled/missing."""
        if self.store_path is None:
            return None
        return self._load_store().get(submission_id)

    @staticmethod
    def catalog() -> dict[str, Any]:
        """Return available panels and the evidence rules applied by this release."""
        panels = _default_panels()
        return {
            "panels": {
                name: {
                    "description": panel["description"],
                    "reviewers": len(panel["models"]),
                    "providers": sorted({member["provider"] for member in panel["models"]}),
                    "contains_stub": any(member["vendor"] == "stub" for member in panel["models"]),
                }
                for name, panel in panels.items()
            },
            "verdicts": ["approved", "minor-issues", "serious-issues", "critical", INCONCLUSIVE_VERDICT],
            "models": registered_vendors(),
            "guard": "L1 extract reviewer citations -> L2 confined scan -> L3 score -> L4 correlate -> L5 feedback",
            "gates": ["task", "data_quality", "review", "bias", "output_quality", "delivery"],
            "persistence": "disabled by default; pass store_path or CLI --store to opt in",
        }

    def _load_store(self) -> dict[str, Any]:
        if self.store_path is None:
            return {}
        try:
            with open(self.store_path, encoding="utf-8") as handle:
                store = json.load(handle)
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"refusing to overwrite malformed Double Gate store: {self.store_path}"
            ) from exc
        if not isinstance(store, dict):
            raise RuntimeError(f"refusing to overwrite non-object Double Gate store: {self.store_path}")
        return store

    def _save_result(self, submission_id: str, result: dict[str, Any]) -> None:
        if self.store_path is None:
            return
        store = self._load_store()
        store[submission_id] = result
        directory = os.path.dirname(self.store_path) or "."
        os.makedirs(directory, exist_ok=True)
        file_descriptor, temporary_path = tempfile.mkstemp(
            prefix=".double-gate-", suffix=".tmp", dir=directory, text=True
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                json.dump(store, handle, ensure_ascii=False, indent=2, default=str)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self.store_path)
            os.chmod(self.store_path, 0o600)
        except Exception:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
            raise


# ------------------------------------------------------------------ Facade

_JURIES: dict[tuple[str, Optional[str]], Jury] = {}


def get_jury(codebase_root: Optional[str] = None, store_path: Optional[str] = None) -> Jury:
    """Return a cached jury keyed by its scan root and opt-in store path."""
    root = os.path.abspath(os.path.expanduser(codebase_root or os.getcwd()))
    store = os.path.abspath(os.path.expanduser(store_path)) if store_path else None
    key = (root, store)
    if key not in _JURIES:
        _JURIES[key] = Jury(codebase_root=root, store_path=store)
    return _JURIES[key]


def submit(
    content: str,
    submission_type: str = "review",
    risk_level: str = "medium",
    context: Optional[dict[str, Any]] = None,
    panel_name: Optional[str] = None,
    verify_artifacts: bool = True,
    codebase_root: Optional[str] = None,
    store_path: Optional[str] = None,
    offline: bool = False,
) -> dict[str, Any]:
    """One-call full review pipeline (module-level convenience)."""
    return get_jury(codebase_root=codebase_root, store_path=store_path).submit(
        content,
        submission_type=submission_type,
        risk_level=risk_level,
        context=context,
        panel_name=panel_name,
        verify_artifacts=verify_artifacts,
        offline=offline,
    )


def get_result(submission_id: str, store_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Fetch a result from an explicitly selected store path."""
    return get_jury(store_path=store_path).get_result(submission_id)


def catalog() -> dict[str, Any]:
    """Return catalog metadata for the currently configured panels."""
    return Jury.catalog()
