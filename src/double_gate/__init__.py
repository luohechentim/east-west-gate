"""Double Gate — a cross-model review panel with anti-hallucination guard, quality
gates, and a bug-fix loop for AI agents.

Multiple independent LLM reviewers (or offline stub reviewers) blind-audit the same
content; an adjudicator merges their findings into a verdict; a five-layer guard
verifies every cited artifact actually exists; six quality gates apply deterministic
checks; and a repair loop turns findings into verified fixes. Zero vendor lock-in and
zero API-key requirement for the offline path.
"""

from __future__ import annotations

__version__ = "0.3.0"
__all__ = [
    "__version__",
    "jury",
    "models",
    "prompts",
    "adjudicator",
    "guard",
    "gates",
    "repair",
    "cli",
]
