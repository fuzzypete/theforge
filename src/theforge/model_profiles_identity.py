"""Model profile vocabulary shared by the accumulation and read-model halves.

``model_profiles`` is two owners with one shared vocabulary (#2467):
:mod:`theforge.model_profiles_storage` accumulates run outcomes into stored
profiles, and :mod:`theforge.model_profiles_read_model` derives the signals
routing consults from that stored state. Neither imports the other. What both
genuinely need — the role/band names, the recency-window defaults, the stored
section keys, the identity resolution chain that maps a profile key to a
``(provider, model)`` pair, and the two stat primitives that read a stored
bucket's counters — lives here so each name has exactly one binding.

Nothing here accumulates anything or answers a routing question; adding either
belongs in the owning module, not in this one.
"""

from __future__ import annotations

from typing import Any

ROLES = ("dev", "review", "preflight", "planner")
COMPLEXITY_BANDS = ("small", "medium", "large")

# Per-domain recency window (issue #155 / ADR-0006 clause 2.4). Per-domain dev
# outcomes maintain a bounded ring of their most recent genuine (non-tainted,
# non-harness-terminated) results so the routing-admissible rate consults a
# *windowed* view of history rather than a lifetime cumulative average — the
# exact shape clause 2.4 forbids from carrying routing weight. This is a local,
# deterministic windowing mechanism; the shared decay mechanism (#1392) reuses
# the same ring shape (:func:`_weighted_rate`) without changing the read contract
# (raw + weighted both recorded).
DOMAIN_RECENCY_WINDOW = 20

# Shared recency-weighting defaults (#1392, ADR-0006 clause 2.4). Every dev
# capability bucket (top-level, per-complexity, per-score) keeps a bounded ring
# of its most recent genuine outcomes — ``_recent`` — capped at
# :data:`CAPABILITY_RECENCY_WINDOW` on disk. The routing-admissible ``weighted``
# rate is computed from that ring by :func:`_weighted_rate` using the parameters
# below; operators override them via the ``assignment.recency`` config section.
#
# The mechanism is a run-position exponential decay rather than a wall-clock one:
# profiles store an ordered outcome ring, not per-run timestamps, so "age" is
# counted in runs (a story completed N runs ago), keeping the aggregation pure
# (no clock dependency) and deterministically recomputable from stored admissible
# data after any parameter change. ``half_life_runs`` is the number of runs after
# which a run's weight halves; the default (~50 runs) means the most recent
# handful of stories dominate while a large stale slice decays out of relevance
# instead of permanently anchoring a lifetime average (the #1392 Sonnet case).
DEFAULT_RECENCY_MODE = "exponential"
DEFAULT_RECENCY_HALF_LIFE_RUNS = 50.0
DEFAULT_RECENCY_WINDOW = 200
# Maximum outcomes retained per capability ring on disk. Bounds profile growth
# while leaving headroom to tune ``window`` upward without losing history; a
# configured ``window`` beyond this simply consults everything stored.
CAPABILITY_RECENCY_WINDOW = 200
OBSERVED_COST_TIEBREAK_MIN_SAMPLES = 3
OBSERVED_COST_TIEBREAK_RECENCY_DAYS = 30


RESOLVED_MODEL_BREAKDOWN_KEY = "by_resolved_model"
"""Per-served-version breakdown of the population counted by a section's ``runs``."""

RESOLVED_MODEL_ATTEMPT_BREAKDOWN_KEY = "by_resolved_model_attempts"
"""Per-served-version breakdown of the population counted by ``_attempted_count``.

Deliberately a *second* key rather than a shared one. Several sections carry two
independent populations: ``review`` counts cycles-with-findings under ``runs``
and invocations under ``_attempted_count``; ``preflight``/``planner`` count
phases under ``runs`` and attempts under ``_attempted_count``. Both are folded
from the same run, at different call sites, with different denominators — so one
shared breakdown would be incremented twice per invocation and report more
attributed version observations than either population contains. Each breakdown
is scoped to the counter it explains, and each signal reads the one matching its
own denominator.
"""

ALIAS_DERIVED_KEY = "alias_derived"
"""Section key holding evidence projected from an alias onto a concrete version."""


def _normalize_band(complexity: str | None) -> str:
    """Map legacy/alt complexity strings to small|medium|large."""
    if not complexity:
        return "medium"
    cl = complexity.lower()
    if cl in COMPLEXITY_BANDS:
        return cl
    return {"low": "small", "high": "large"}.get(cl, "medium")


def _provider_from_cli(cli: str | None) -> str | None:
    """Map a recorded CLI runner name to its provider family.

    Delegates to the config registry so there is one such mapping in the
    codebase rather than a copy that can drift from it.
    """
    from theforge.config.models import provider_for_cli_runner

    return provider_for_cli_runner((cli or "").strip().lower() or None)


def _transport_from_identity(provider: str | None, cli: str | None) -> str | None:
    """Classify a *recorded run's* transport from what the runner reported.

    This reads attempt telemetry, not configuration: by the time an attempt is
    recorded the dispatch decision has already been made from the profile's
    TransportSpec, and ``cli`` is that transport's runner name. Nothing here
    feeds dispatch.
    """
    if cli:
        return "cli"
    if provider:
        return "api"
    return None


def _identity_metadata(
    *,
    actual_model: str | None,
    provider: str | None,
    cli: str | None,
) -> dict[str, str] | None:
    model = (actual_model or "").strip()
    inferred_provider = (provider or _provider_from_cli(cli) or "").strip()
    transport = _transport_from_identity(inferred_provider or None, cli)
    if not model or not inferred_provider:
        return None
    out = {
        "provider": inferred_provider,
        "model": model,
    }
    if transport:
        out["transport"] = transport
    if cli:
        out["cli"] = cli
    return out


def canonical_id_from_identity(
    *,
    actual_model: str | None,
    provider: str | None,
    cli: str | None,
) -> str | None:
    """Derive a canonical model ID from runtime identity fields.

    Returns None when the input is too thin to identify a canonical
    (provider+model+transport) — e.g. role-shaped names like ``"dev"`` that
    carry no provider hint.
    """
    inferred_provider = (provider or _provider_from_cli(cli) or "").strip()
    transport = _transport_from_identity(inferred_provider or None, cli)
    model = (actual_model or "").strip()
    if not (model and inferred_provider and transport):
        return None
    return f"{inferred_provider}/{model}/{transport}"


def _infer_identity_from_key(model_key: str) -> tuple[str, str] | None:
    stripped = (model_key or "").strip()
    if not stripped:
        return None
    spec = _resolve_agent_spec_for_profile_key(stripped)
    if spec is not None:
        return (spec.provider, spec.model)
    if stripped.endswith("-cli"):
        return _identity_from_unique_spec(stripped[: -len("-cli")], transport="cli")
    if stripped.endswith("-api"):
        return _identity_from_unique_spec(stripped[: -len("-api")], transport="api")
    return _identity_from_unique_spec(stripped)


def _resolve_agent_spec_for_profile_key(model_key: str) -> Any | None:
    try:
        from theforge.config.models import known_raw_model_key_prefixes, resolve_agent_spec
    except Exception:  # noqa: BLE001
        return None
    candidates = [model_key]
    # Legacy storage keys are dash-joined raw keys, so the legacy provider-prefix
    # spellings have to be tried too — resolve_agent_spec normalizes them.
    prefixes = known_raw_model_key_prefixes()
    for prefix in sorted(prefixes, key=len, reverse=True):
        if model_key.startswith(f"{prefix}-"):
            candidates.append(f"{prefix}/{model_key[len(prefix) + 1 :]}")
    for candidate in candidates:
        try:
            return resolve_agent_spec(candidate)
        except ValueError:
            continue
    return None


def _identity_from_unique_spec(
    model_name: str,
    transport: str | None = None,
) -> tuple[str, str] | None:
    try:
        from theforge.config.models import AGENT_REGISTRY
    except Exception:  # noqa: BLE001
        return None
    matches = []
    for spec in AGENT_REGISTRY.values():
        if spec.model != model_name:
            continue
        if transport is not None and spec.transport.kind != transport:
            continue
        matches.append((spec.provider, spec.model))
    unique = set(matches)
    if len(unique) == 1:
        return next(iter(unique))
    return None


def _success_count(stats: dict[str, Any], runs: int) -> float:
    if "_successes" in stats:
        return float(stats.get("_successes", 0.0))
    return float(stats.get("success_rate", 0.0)) * float(runs)


def _metric_sum(
    stats: dict[str, Any],
    runs: int,
    sum_key: str,
    avg_key: str,
) -> float | None:
    if sum_key in stats:
        return float(stats.get(sum_key, 0.0))
    if avg_key in stats:
        return float(stats.get(avg_key, 0.0)) * float(runs)
    return None
