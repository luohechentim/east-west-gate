# Double Gate

[简体中文](README.zh-CN.md) · [Configuration](docs/CONFIGURATION.md) · [Security](SECURITY.md) · [Contributing](CONTRIBUTING.md)

**Evidence-aware review panels, artifact verification, quality gates, and repair verification for AI-assisted work.**

Double Gate helps an agent or team make review claims that can be inspected rather than merely asserted. It combines deterministic adjudication with explicit evidence boundaries:

- blind panel dispatch and structured JSON review parsing;
- convergence-aware adjudication that distinguishes unanimous, majority, split, and single-reviewer signals;
- a five-layer guard that checks **artifacts cited by reviewer output** against an approved codebase root;
- six deterministic task, data, review, bias, output, and delivery gates;
- repair checklists that only close findings after adequately assured re-review evidence.

It has no mandatory runtime dependency and includes a deterministic offline `stub` client for demos and tests.

> The offline stub is a **simulation**, not an independent expert review. Double Gate labels it `offline-simulation` and will not treat it as delivery-ready assurance. A real multi-provider panel still informs human judgment; it does not prove that a design, code change, or report is correct.

## Install

```bash
# Install from this source checkout.
python -m pip install .

# For contributors:
python -m pip install -e ".[dev]"

# After a future PyPI publication, this may be used instead:
# python -m pip install double-gate
```

The package supports Python 3.10+.

## Quick start: offline workflow

Create a short file to review, then run the deterministic demonstration panel:

```bash
double-gate review proposal.md --risk low --offline --root .
```

The JSON report includes the adjudication, `panel_integrity`, raw responses, artifact-guard report, finding evidence, and a conservative `delivery_status`. For an offline run, expect:

```json
{
  "status": "completed_with_limitations",
  "panel_integrity": {"review_mode": "offline-simulation"},
  "delivery_status": "human-review-required"
}
```

Run the deterministic quality gates with a complete context:

```bash
double-gate gates proposal.md \
  --risk low \
  --context examples/gates-context.json \
  --fail-on-failure
```

To check citations in review text directly:

```bash
double-gate guard reviewer-output.md --root ./your-repository
```

`--root` is a security boundary. A path reference that resolves outside it is reported as `UNVERIFIABLE`; the guard never probes arbitrary host paths.

## Use a real panel

Real panels are configured through `DOUBLE_GATE_PANELS_JSON`. Each reviewer can have its own OpenAI-compatible endpoint and API-key environment variable, while `provider` declares its independence group. See the complete [configuration guide](docs/CONFIGURATION.md).

```bash
export PROVIDER_A_API_KEY='...'
export PROVIDER_B_API_KEY='...'
export DOUBLE_GATE_PANELS_JSON='{
  "deep": {
    "description": "two-provider panel",
    "models": [
      {"vendor":"openai_compatible", "provider":"provider-a", "base_url":"https://provider-a.example/v1", "api_key_env":"PROVIDER_A_API_KEY", "model":"model-a", "role":"primary"},
      {"vendor":"openai_compatible", "provider":"provider-b", "base_url":"https://provider-b.example/v1", "api_key_env":"PROVIDER_B_API_KEY", "model":"model-b", "role":"critic"}
    ]
  }
}'

double-gate review proposal.md --risk high --panel deep --root ./your-repository
```

Use distinct `provider` groups only when they are genuinely independent. A panel with one provider is reported as `single-provider`, and any panel containing `stub` is reported as `offline-simulation`.

## CLI reference

| Command | Purpose |
| --- | --- |
| `double-gate review FILE` | Run a panel, adjudicate it, verify reviewer citations, and emit one JSON audit report. |
| `double-gate gates FILE` | Run the six deterministic gates. Use `--context` or `--data` and `--delivery`. |
| `double-gate guard FILE --root ROOT` | Scan cited files, APIs, imports, symbols, and fields in review text. |
| `double-gate repair FILE` | Run a review and emit an evidence-aware repair checklist. |
| `double-gate catalog` | Show current panel configuration and capability metadata. |

Useful controls:

```bash
# Read content from stdin.
printf 'Review this design.' | double-gate review - --offline --risk low

# Make a CI job fail for serious, critical, or inconclusive review outcomes.
double-gate review proposal.md --panel deep --fail-on serious

# Let gate 2 consult an already saved JSON review result.
double-gate review proposal.md --offline > /tmp/review.json
double-gate gates proposal.md --risk high --context examples/gates-context.json --review-report /tmp/review.json

# Persist sensitive report data only when you explicitly choose a local path.
double-gate review proposal.md --offline --store .local/double-gate-reports.json
```

Reports are not persisted by default. When `--store` / `store_path` is used, Double Gate writes atomically and applies owner-only permissions where the platform supports them. Keep stored reports out of source control.

## Programmatic API

```python
from double_gate import jury, repair

first = jury.submit(
    content="""# Change proposal
Add validation around the order workflow and document its failure modes.
""",
    submission_type="design",
    risk_level="high",
    codebase_root="./your-repository",
    # offline=True is useful for a pipeline smoke test, not a delivery decision.
    offline=True,
)

print(first["adjudication"]["verdict"])
print(first["panel_integrity"]["review_mode"])
print(first["delivery_status"])

checklist = repair.build_checklist(first)
# Apply fixes outside this library, then obtain a real multi-provider re-review.
second = jury.submit(
    content="...revised proposal...",
    submission_type="design",
    risk_level="high",
    codebase_root="./your-repository",
)
verification = repair.verify_fixed(second, checklist)
print(verification["all_findings_absent"])
print(verification["all_resolved"])
```

`all_findings_absent` and `all_resolved` are intentionally different. The latter also requires adequate re-review assurance and an acceptable re-review verdict. You may supply a documented local evidence record to `verify_fixed(..., verification_evidence=..., allow_explicit_evidence=True)`, but that is visibly marked as caller-supplied rather than independent review.

## How the evidence pipeline works

```text
submitted content
  -> blind reviewer calls
  -> schema validation + deterministic adjudication
  -> guard scans reviewer-cited artifacts within the approved root
  -> per-finding artifact evidence + conservative triage
  -> human delivery decision / repair checklist
  -> independent re-review + verification evidence
```

The artifact guard checks whether a cited file, API, import, symbol, or field can be located. It does **not** validate every factual claim or prove a behavior is correct. Treat `VERIFIED` as bounded existence evidence, `SUSPICIOUS` as incomplete evidence, `LIKELY_HALLUCINATION` as a strong warning, and `UNVERIFIABLE` as outside the scan boundary or otherwise not safely checkable.

## Agent skill

The repository includes [skills/double-gate/SKILL.md](skills/double-gate/SKILL.md) for agent environments that support skill folders. It is a workflow guide; the versioned Python code in `src/double_gate/` remains the executable source of truth.

## Development

```bash
python -m pip install -e ".[dev]"
python -m ruff check .
python -m pytest -q
python -m build
python -m twine check dist/*
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution expectations and [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md) for the first public GitHub push.

## License

[MIT](LICENSE) © 2026 Double Gate contributors.
