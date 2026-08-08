---
name: double-gate
description: >-
  Cross-model review panel with anti-hallucination guard, six quality gates, and a
  bug-fix loop for AI agents. Use when any AI agent output (code, design, decision,
  report, fix) needs an independent quality check before delivery, or when a bug needs
  to be found, fixed, and verified. Triggers: 质检, 评审, 复核, review, audit, 修 bug,
  fix bug, 防幻觉, hallucination guard, quality gate, 收敛, 裁决, verdict,
  multi-model review, 独立评审.
---

# Double Gate

A self-contained review discipline that helps AI agents catch their own blind spots and
close bugs with evidence. It bundles three engines — a **review panel**, an
**anti-hallucination guard**, and **six quality gates** — plus a **bug-fix loop** that
turns findings into verified fixes.

Philosophy: *one model cannot see its own errors; a finding is not closed by an
assertion, it is closed by evidence.*

## When to use

- Before delivering any agent output (code, design, decision, report) that matters.
- When a bug report needs triage, a fix, and proof the fix works.
- When you want to catch hallucinated references (files/APIs/symbols that do not exist).
- When multiple reviewers disagree and you need an honest adjudication.

## Install (one-time)

```bash
pip install .          # core, stdlib only
pip install ".[test]"  # + pytest
```

## Quickstart — offline (no API key)

```bash
double-gate review docs/design.md --risk high --offline   # review + adjudicate
double-gate gates  docs/design.md --risk high             # six quality gates
double-gate guard  docs/design.md --root .                # anti-hallucination scan
```

The bundled offline panel is deterministic demonstration data. It is useful for testing
the workflow, but it is not independent review and must not be presented as a delivery
approval.

## Programmatic workflow

```python
from double_gate import jury, repair

# 1. Review: blind reviewers, adjudicated. offline=True is only a pipeline smoke test.
result = jury.submit(content, submission_type="design", risk_level="high",
                     verify_artifacts=True, offline=True)

verdict = result["adjudication"]["verdict"]            # approved/minor-issues/serious-issues/critical/inconclusive
hg = result["hallucination_guard"]["summary"]          # artifact verification report
escalated = result["human_escalations"]                # findings needing a fix decision
mode = result["panel_integrity"]["review_mode"]         # offline-simulation/single-provider/multi-provider

# 2. Fix: turn findings into an actionable checklist
checklist = repair.build_checklist(result)

# 3. Apply your fix, then re-review with an adequately independent panel
fixed_content = apply_your_fix(content, checklist)     # your code/design edit
again = jury.submit(fixed_content, risk_level="high")

# 4. Verify: a finding is closed only if it no longer appears in the re-review
report = repair.verify_fixed(again, checklist)
assert report["all_resolved"]
```

## Six quality gates

| gate | what it checks | pass line |
|------|----------------|-----------|
| 0 task definition | content long enough, scope bounded | ≥ 7 |
| 1 data quality | null-rate in supplied data ≤ 5% | ≥ 6 |
| 2 review | a real `review_engine` was consulted | ≥ 6 |
| 3 bias | confirmation/anchoring/survivorship keywords | ≥ 6 |
| 4 output quality | non-empty, substantial | ≥ 6 |
| 5 delivery | recipient/subject/signature present | ≥ 6 |

## Verdicts

`approved` · `minor-issues` · `serious-issues` · `critical` · `inconclusive` — derived from the
independent reviewers' convergence and scores. Convergence quality is labeled honestly
(unanimous/majority/split/single), never faked.

## Plug in real reviewers (optional)

```bash
export OPENAI_BASE_URL=https://api.openai.com/v1
export OPENAI_API_KEY=sk-...
export OPENAI_MODEL=gpt-4o
export DOUBLE_GATE_PANELS_JSON='{
  "deep": {"risk": "high", "models": [
    {"vendor": "openai_compatible", "model": "gpt-4o", "role": "primary"},
    {"vendor": "openai_compatible", "model": "gpt-4o", "role": "guard"},
    {"vendor": "openai_compatible", "model": "gpt-4o", "role": "critic"}]}}'
double-gate review docs/design.md --risk high
```

`stub` reviewer works offline (deterministic, `STUB_VERDICT` env to change verdict), but it
is explicitly marked as `offline-simulation` rather than independent review. For a real
panel, configure distinct provider groups and per-member `api_key_env` values as described
in `docs/CONFIGURATION.md`.

## Running inside an agent

- Before delivering work, run `double-gate review <file> --offline` as a workflow smoke
  test, or use a real multi-provider panel for substantive review. Attach the complete
  report and do not call an offline result a delivery approval.
- On a bug report: build the checklist, apply fixes, re-review, and only report a bug
  "fixed" when `verify_fixed()` says `all_resolved: true`; an absent finding from a
  simulated or weak re-review is not enough.
- If the hallucination guard flags a cited file/API/symbol as missing, re-check the
  reference before trusting the reviewer.

## Project layout

```
src/double_gate/
├── models.py       pluggable reviewer registry + call_panel + role personas
├── prompts.py      review / adjudication / blind-review prompt templates
├── adjudicator.py  deterministic verdict engine (convergence + severity merge)
├── guard.py        L1–L5 anti-hallucination guard (artifact existence scan)
├── jury.py         end-to-end orchestrator
├── repair.py       bug-fix loop (checklist → fix → verify)
├── gates.py        six quality gates
└── cli.py          CLI (argparse, stdlib only)
```

## License

MIT.
