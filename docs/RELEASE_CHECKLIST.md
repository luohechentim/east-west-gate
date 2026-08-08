# GitHub Open-Source Release Checklist

Run this checklist before the first public push and each tagged release.

## Repository setup

- [ ] Create the GitHub repository and set its visibility to public.
- [ ] Replace any repository URL placeholders only after the final owner/repository name is known.
- [ ] Enable branch protection for `main`: pull request required, CI required, and force pushes disabled.
- [ ] Enable private vulnerability reporting in the GitHub **Security** tab.
- [ ] Add repository topics such as `llm`, `ai-safety`, `code-review`, and `quality-gates`.

## Content and security review

- [ ] Confirm `git status --ignored` contains no real `.env`, API key, store JSON, customer data, or generated reports.
- [ ] Search the staged content for credential-shaped strings and local absolute paths.
- [ ] Verify `LICENSE`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, and `CONTRIBUTING.md` reflect the intended maintainer policy.
- [ ] Check that examples only use fictitious data and endpoints.

## Verification

```bash
python -m pip install -e ".[dev]"
python -m ruff check .
python -m pytest -q
python -m build
python -m twine check dist/*
```

## First push

```bash
git init -b main
git add .
git commit -m "feat: prepare Double Gate 0.3.0 for open source"
git remote add origin git@github.com:YOUR_ACCOUNT/double-gate.git
git push -u origin main
```

Use the HTTPS remote form instead if that is how your GitHub account is configured. Do not create a GitHub release or publish a package until the public CI run is green.
