# Changelog

All notable changes to this project are documented in this file.

## 0.3.0 — 2026-08-08

- Corrected the hallucination guard to inspect reviewer output rather than the submitted source text.
- Confined artifact file checks to approved scan roots and blocked absolute-path and traversal probes outside them.
- Added honest panel-assurance metadata: offline stubs are now explicitly simulated and cannot produce an automatic delivery-ready recommendation.
- Added per-reviewer provider configuration, preserving failed panel seats instead of silently dropping bad vendors.
- Made finding triage evidence-aware and fixed misleading finding-ID matching across re-reviews.
- Made report persistence opt-in, atomic, and owner-readable only where supported.
- Added CLI stdin support, explicit CI exit policies, review-report input for gate 2, and a single JSON context option for gates.
- Added packaging, linting, GitHub community health, security, release-checklist, and CI improvements for public release.

## 0.2.0

- Initial public package archive supplied by the project author.
