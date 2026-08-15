# Double Gate v0.3.1

Double Gate v0.3.1 is a maintenance release that refreshes the GitHub Actions
toolchain without changing the Python API, CLI, evidence model, or review semantics.

## Changes

- upgrade `actions/checkout` from v4 to v7;
- upgrade `actions/setup-python` from v5 to v7;
- upgrade `actions/upload-artifact` from v4 to v7;
- synchronize package, runtime, citation, and issue-template version metadata.

## Compatibility

The package continues to support Python 3.10 through 3.13. This release contains
no intended breaking changes.

## Verify locally

```bash
python -m pip install -e ".[dev]"
python -m ruff check .
python -m pytest -q
python -m build
python -m twine check dist/*
```

Repository: <https://github.com/luohechentim/east-west-gate>
