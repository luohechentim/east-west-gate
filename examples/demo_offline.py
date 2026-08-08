"""Offline workflow demo: panel plumbing, bounded guard, and quality gates.

Run from the project root:
    PYTHONPATH=src python3 examples/demo_offline.py

The stub panel is intentionally labelled as a simulation. This example shows
the workflow shape without claiming that the output is an independent review.
"""

from double_gate import gates, guard, jury

PROPOSAL = """# Proposal: order checkout endpoint

Add validation around a checkout workflow, document failure modes, and provide
tests for expected retry behavior. This example contains no real customer data.
"""


def main() -> None:
    print("=" * 70)
    print("Step 1 — offline demonstration panel")
    print("=" * 70)
    result = jury.submit(
        PROPOSAL,
        submission_type="design",
        risk_level="high",
        codebase_root=".",
        offline=True,
    )
    print("verdict           :", result["adjudication"]["verdict"])
    print("review mode       :", result["panel_integrity"]["review_mode"])
    print("delivery status   :", result["delivery_status"])
    print("delivery reasons  :", len(result["delivery_reasons"]))

    print()
    print("=" * 70)
    print("Step 2 — inspect a reviewer citation inside the approved root")
    print("=" * 70)
    review_text = (
        "The review claims that `double_gate/not_a_real_module.py` and "
        "`double_gate/gates.py` are both required."
    )
    report = guard.scan(review_text, root="src")
    for row in report["detailed_results"]:
        artifact = row["artifact"]["name"]
        print(f"{artifact:45s} -> {row['verdict']}")
    print("guard risk        :", report["summary"]["overall_hallucination_risk"])

    print()
    print("=" * 70)
    print("Step 3 — six deterministic gates")
    print("=" * 70)
    gate_result = gates.check_all_gates(
        PROPOSAL,
        {
            "risk_level": "high",
            "data": {"records": 120, "missing_fields": 0},
            "delivery": {
                "recipient": "review-board",
                "subject": "offline workflow demo",
                "signature": "Double Gate example",
            },
            # A simulated review is correctly flagged for human review by gate 2.
            "review_engine": lambda _content, _risk: result,
        },
    )
    for item in gate_result["results"]:
        state = "PASS" if item["passed"] else "FLAG"
        print(f"[{state}] gate {item['gate']} {item['name']:16s} score={item['score']}")
    print("needs human review:", gate_result["needs_human_review"])


if __name__ == "__main__":
    main()
