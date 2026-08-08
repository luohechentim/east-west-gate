# Contributing to Double Gate

Thanks for helping make AI review workflows more evidence-based.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m ruff check .
python -m pytest -q
```

## Contribution rules

- Keep the core package dependency-light; justify every runtime dependency.
- Add a regression test for every behavior or safety fix.
- Do not add API keys, access tokens, private prompts, customer data, or local absolute paths.
- Preserve the difference between deterministic simulation, single-provider review, and genuinely multi-provider review. Do not turn a convenience signal into an unsupported assurance claim.
- Keep artifact scanning confined to explicit codebase roots.
- Update `README.md`, `README.zh-CN.md`, and `CHANGELOG.md` when behavior or public interfaces change.

## Pull requests

Keep PRs focused. Explain the behavior change, tests run, compatibility implications, and any review/guard limitation introduced. The pull-request template contains the required checklist.

## Reporting bugs and security issues

Use the issue forms for reproducible, non-sensitive defects. Follow [SECURITY.md](SECURITY.md) for vulnerabilities.
