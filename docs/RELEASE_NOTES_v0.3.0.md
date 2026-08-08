# Double Gate v0.3.0

Double Gate is now packaged as a public, source-first Python project for deterministic review evidence and repair verification.

## What is included

- blind panel dispatch with structured JSON parsing and convergence-aware adjudication;
- a bounded artifact guard for files, APIs, imports, symbols, and fields cited by review output;
- six deterministic task, data, review, bias, output, and delivery gates;
- evidence-aware repair checklists and re-review verification;
- an offline simulation mode for repeatable development and CI smoke tests;
- a CLI, Python API, examples, tests, configuration guide, security policy, and agent skill.

## Important boundary

Offline mode is explicitly labelled `offline-simulation` and must not be presented as independent expert review. Real multi-provider panels provide additional evidence for a human release decision; they do not prove correctness.

## Verify locally

```bash
python -m pip install -e ".[dev]"
python -m ruff check .
python -m pytest -q
python -m build
python -m twine check dist/*
```

Repository: <https://github.com/luohechentim/east-west-gate>
