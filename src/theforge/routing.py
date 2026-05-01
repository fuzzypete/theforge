"""Shared deterministic routing policy tables.

The numeric complexity score path is the canonical dev-tier policy for adaptive
assignment. It intentionally splits the legacy MEDIUM band so localized work can
stay cheap while broader cross-module work escalates sooner:

- scores 1-3 route to ``cheap``
- scores 4-6 route to ``mid``
- scores 7-10 route to ``strong``

Keep score-threshold edits here so assignment, coordinator preflight, and
static role derivation stay aligned. Raising the cheap-tier ceiling is a
single-line change to ``DEV_SCORE_TIER_BUCKETS``.
"""

from __future__ import annotations

# Ordered upper-bound buckets for the 1-10 dev complexity score.
DEV_SCORE_TIER_BUCKETS: tuple[tuple[int, str], ...] = (
    (3, "cheap"),
    (6, "mid"),
    (10, "strong"),
)

# Legacy enum fallback used when a numeric score is unavailable.
DEV_COMPLEXITY_TIER: dict[str, str] = {
    "LOW": "cheap",
    "MEDIUM": "mid",
    "HIGH": "strong",
}


def score_to_dev_tier(score: int) -> str:
    """Return the dev-phase tier for a 1-10 complexity score."""
    for max_score, tier in DEV_SCORE_TIER_BUCKETS:
        if score <= max_score:
            return tier
    return DEV_SCORE_TIER_BUCKETS[-1][1]
