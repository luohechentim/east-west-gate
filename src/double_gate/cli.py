"""Command-line interface for Double Gate.

All commands emit JSON to stdout so they can be used in a local workflow or a
CI job.  Commands report findings by default; use the explicit failure flags
when a non-zero exit status should gate automation.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Mapping, Optional


def _read_text(path: str) -> str:
    """Read UTF-8 text from a path, or from stdin when ``path`` is ``-``."""
    if path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _read_json(path: str) -> Any:
    """Read JSON from a path, or from stdin when ``path`` is ``-``."""
    if path == "-":
        return json.load(sys.stdin)
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _review_exit_code(payload: Mapping[str, Any], policy: str) -> int:
    if policy == "none":
        return 0
    adjudication = payload.get("adjudication", payload)
    verdict = str(adjudication.get("verdict") or "inconclusive") if isinstance(adjudication, Mapping) else "inconclusive"
    if policy == "serious":
        return 2 if verdict in {"critical", "serious-issues", "inconclusive"} else 0
    return 2 if verdict != "approved" else 0


def _cmd_review(args: argparse.Namespace) -> int:
    from double_gate import jury

    payload = jury.submit(
        content=_read_text(args.file),
        submission_type=args.type,
        risk_level=args.risk,
        context={},
        panel_name=args.panel,
        verify_artifacts=not args.no_verify_artifacts,
        codebase_root=args.root,
        store_path=args.store,
        offline=args.offline,
    )
    _print_json(payload)
    return _review_exit_code(payload, args.fail_on)


def _review_engine_from_report(path: str):
    """Return a callable bridge so gate 2 genuinely consults a saved review."""
    report = _read_json(path)
    if not isinstance(report, Mapping):
        raise ValueError("--review-report must contain a JSON object")

    def engine(_content: str, _risk: str) -> dict[str, Any]:
        adjudication = report.get("adjudication", report)
        if not isinstance(adjudication, Mapping):
            return {"verdict": "inconclusive", "overall": 0}
        panel_summary = adjudication.get("panel_summary", {})
        if not isinstance(panel_summary, Mapping):
            panel_summary = {}
        hallucination_guard = adjudication.get("hallucination_guard")
        if not isinstance(hallucination_guard, Mapping):
            hallucination_guard = report.get("hallucination_guard", {})
        return {
            "verdict": adjudication.get("verdict"),
            "overall": panel_summary.get("average_score"),
            "hallucination_guard": hallucination_guard,
        }

    return engine


def _cmd_gates(args: argparse.Namespace) -> int:
    from double_gate import gates

    context: dict[str, Any] = {"risk_level": args.risk}
    if args.context:
        loaded = _read_json(args.context)
        if not isinstance(loaded, Mapping):
            raise ValueError("--context must contain a JSON object")
        context.update(loaded)
        context["risk_level"] = args.risk
    if args.data:
        context["data"] = _read_json(args.data)
    if args.delivery:
        context["delivery"] = _read_json(args.delivery)
    if args.review_report:
        context["review_engine"] = _review_engine_from_report(args.review_report)

    payload = gates.check_all_gates(_read_text(args.file), context)
    _print_json(payload)
    return 2 if args.fail_on_failure and not payload["passed"] else 0


def _cmd_guard(args: argparse.Namespace) -> int:
    from double_gate import guard

    _print_json(guard.scan(_read_text(args.file), root=args.root))
    return 0


def _cmd_repair(args: argparse.Namespace) -> int:
    from double_gate import jury, repair

    result = jury.submit(
        content=_read_text(args.file),
        submission_type=args.type,
        risk_level=args.risk,
        context={},
        panel_name=args.panel,
        verify_artifacts=not args.no_verify_artifacts,
        codebase_root=args.root,
        store_path=args.store,
        offline=args.offline,
    )
    checklist = repair.build_checklist(result)
    payload = {
        "submission_id": result["submission_id"],
        "verdict": result["adjudication"].get("verdict"),
        "delivery_status": result["delivery_status"],
        "open_fixes": [
            {
                "id": item.finding_id,
                "severity": item.severity,
                "summary": item.summary,
                "suggestion": item.suggestion,
            }
            for item in checklist.require_fix()
        ],
        "hint": (
            "Apply the fixes, re-run a multi-provider review, then call "
            "repair.verify_fixed() with the new result and this checklist."
        ),
    }
    _print_json(payload)
    return _review_exit_code(result, args.fail_on)


def _cmd_catalog(_args: argparse.Namespace) -> int:
    from double_gate import jury

    _print_json(jury.catalog())
    return 0


def _add_review_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--risk", choices=["low", "medium", "high"], default="medium")
    parser.add_argument("--type", default="general", help="submission type used to tailor the prompt")
    parser.add_argument("--panel", help="configured panel name (defaults from --risk)")
    parser.add_argument("--root", default=".", help="approved repository root for artifact verification")
    parser.add_argument("--store", help="optional JSON store path; reports are not persisted by default")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="force the bundled deterministic demonstration panel; never calls a provider",
    )
    parser.add_argument(
        "--no-verify-artifacts",
        action="store_true",
        help="skip the reviewer-artifact hallucination guard",
    )
    parser.add_argument(
        "--fail-on",
        choices=["none", "serious", "any"],
        default="none",
        help="optional process exit policy: no failure, serious/critical/inconclusive, or any non-approved verdict",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="double-gate",
        description="Double Gate — evidence-aware panel review and quality gates",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    review = subparsers.add_parser("review", help="run a panel review and print a JSON audit report")
    review.add_argument("file", help="UTF-8 content file to review, or - for stdin")
    _add_review_options(review)
    review.set_defaults(func=_cmd_review)

    gates = subparsers.add_parser("gates", help="run the six deterministic quality gates")
    gates.add_argument("file", help="UTF-8 content file to check, or - for stdin")
    gates.add_argument("--risk", choices=["low", "medium", "high"], default="medium")
    gates.add_argument("--context", help="JSON object containing the complete gate context")
    gates.add_argument("--data", help="JSON value to use as ctx['data'] (overrides --context)")
    gates.add_argument("--delivery", help="JSON value to use as ctx['delivery'] (overrides --context)")
    gates.add_argument("--review-report", help="a JSON review result for gate 2 to consult")
    gates.add_argument("--fail-on-failure", action="store_true", help="exit 2 when any gate does not pass")
    gates.set_defaults(func=_cmd_gates)

    guard = subparsers.add_parser("guard", help="scan reviewer-cited artifacts against an approved root")
    guard.add_argument("file", help="UTF-8 review text to scan, or - for stdin")
    guard.add_argument("--root", default=".", help="approved repository root to scan")
    guard.set_defaults(func=_cmd_guard)

    repair = subparsers.add_parser("repair", help="review content and create an evidence-aware repair checklist")
    repair.add_argument("file", help="UTF-8 content file to review, or - for stdin")
    _add_review_options(repair)
    repair.set_defaults(func=_cmd_repair)

    catalog = subparsers.add_parser("catalog", help="list configured panels and package capabilities")
    catalog.set_defaults(func=_cmd_catalog)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """Run the CLI and convert expected local/configuration errors to exit code 2."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
