# Configuration

Double Gate has no required runtime dependency and uses an offline demonstration panel by default. The bundled `stub` reviewers are deterministic test fixtures: they exercise the workflow but do not establish independent review.

## A real panel

Set `DOUBLE_GATE_PANELS_JSON` to a JSON object. Each member names a registered client `vendor`, an independence `provider` group, a model, a role, and optionally per-seat OpenAI-compatible connection settings.

```bash
export PROVIDER_A_API_KEY='...'
export PROVIDER_B_API_KEY='...'
export DOUBLE_GATE_PANELS_JSON='{
  "deep": {
    "description": "two-provider production panel",
    "risk": "high",
    "models": [
      {
        "vendor": "openai_compatible",
        "provider": "provider-a",
        "base_url": "https://provider-a.example/v1",
        "api_key_env": "PROVIDER_A_API_KEY",
        "model": "model-a",
        "role": "primary"
      },
      {
        "vendor": "openai_compatible",
        "provider": "provider-b",
        "base_url": "https://provider-b.example/v1",
        "api_key_env": "PROVIDER_B_API_KEY",
        "model": "model-b",
        "role": "critic"
      }
    ]
  }
}'
```

Then run:

```bash
double-gate review proposal.md --risk high --panel deep --root ./your-repository
```

The `provider` field is an assurance label supplied by the operator. Use distinct, genuinely independent provider groups only when that claim is true. A panel with one provider is labelled `single-provider`; a panel containing `stub` is labelled `offline-simulation`.

## Persistence

Review reports can contain submitted content and raw model output. They are therefore **not persisted by default**. Opt in only to a protected local path:

```bash
double-gate review proposal.md --store .local/double-gate-reports.json
```

Writes are atomic and use owner-only permissions where the host supports them. The store path is ignored by the standard `.gitignore`; do not commit it.

## Artifact scanning boundary

`--root` and `codebase_root` are approved scan boundaries. File references that resolve outside those roots are returned as `UNVERIFIABLE`, not probed. The guard checks citations in reviewer output; it does not establish that every claim in a submitted design or codebase is factually correct.
