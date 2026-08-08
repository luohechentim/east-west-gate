"""Repair-loop demo that preserves the distinction between absence and proof.

Run from the project root:
    PYTHONPATH=src python3 examples/demo_repair_loop.py
"""

from double_gate import jury, repair

CONTENT = """# Refactor proposal

Refactor the order service, validate amounts, and document the persistence and
retry behavior. The change needs a reviewable test plan before implementation.
"""


def main() -> None:
    first = jury.submit(CONTENT, submission_type="design", risk_level="high", offline=True)
    checklist = repair.build_checklist(first)
    print("first review mode:", first["panel_integrity"]["review_mode"])
    print("open fixes:", [item.finding_id for item in checklist.require_fix()])

    fixed_content = CONTENT + "\nThe proposal now includes explicit validation and test cases.\n"
    again = jury.submit(fixed_content, submission_type="design", risk_level="high", offline=True)
    report = repair.verify_fixed(again, checklist)

    print("re-review verdict:", report["re_review_verdict"])
    print("findings absent:", report["all_findings_absent"])
    print("all resolved:", report["all_resolved"])
    print("verification status:", report["verification_status"])
    print("note:", report["assurance"]["note"])
    print("\nUse a real multi-provider re-review before treating a repair as verified.")


if __name__ == "__main__":
    main()
