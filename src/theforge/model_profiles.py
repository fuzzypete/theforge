"""Model capability profiles — compatibility facade over the two owners.

``model_profiles.py`` used to hold two change-reasons in one file: accumulating
a finished run's outcome into stored history, and deriving the signals routing
consults from that history. Adding a routing signal and changing how an outcome
is folded are unrelated changes that shared a module, a review, and a file claim
in the scheduler. They are now two owners with shared vocabulary between them
(#2467, following the #2350 split of ``coordinator/audit_substrate.py``):

``theforge.model_profiles_storage``
    Owns profiles as *stored state*: the ``RunOutcome``/``ReviewerAttempt``/
    ``RoleAttempt`` carriers, ``load_profiles``/``save_profiles``, the pure fold
    of one run (``apply_run`` and its ``_fold_*``/``_update_*`` helpers),
    ``backfill_from_history``/``update_from_run``, the operator reset path with
    its audit log, and the canonical-ID migration with its ``_merge_*``
    catalogue. New accumulation belongs here.

``theforge.model_profiles_read_model``
    Owns profiles as a *queryable model*: ``get_dev_signal``,
    ``get_review_signal``, ``get_role_reliability_signal``, the per-domain fit
    signals, the observed-cost tie-break, and the complexity/score stat readers.
    Every one takes the stored profiles dict and returns a value. New routing
    signals belong here.

``theforge.model_profiles_identity``
    The vocabulary both halves genuinely share: ``ROLES``/``COMPLEXITY_BANDS``,
    the recency-window defaults, the stored section keys, the identity
    resolution chain, and the two stat primitives over a stored bucket. Each
    such name has exactly one binding, so this facade re-exports one of each.

The dependency runs one way in *both* directions being absent: the read model
never imports storage, and storage never imports the read model. That is what
makes a new signal and a new accumulator independent changes — neither touches
the other's file. A signal is therefore exercisable against a profiles dict a
test constructs directly, with no run outcome applied to produce it.

This module re-exports all three, including the private names other modules and
tests import, so every existing ``from theforge.model_profiles import X`` keeps
working. New code should import from the owning module instead. Note that
``monkeypatch.setattr`` against a name here rebinds only this facade's
reference — patch the owning module when a seam needs replacing.
"""

from __future__ import annotations

# ── Shared vocabulary: constants, identity, stat primitives ──────────────
from .model_profiles_identity import (  # noqa: F401
    ALIAS_DERIVED_KEY,
    CAPABILITY_RECENCY_WINDOW,
    COMPLEXITY_BANDS,
    DEFAULT_RECENCY_HALF_LIFE_RUNS,
    DEFAULT_RECENCY_MODE,
    DEFAULT_RECENCY_WINDOW,
    DOMAIN_RECENCY_WINDOW,
    OBSERVED_COST_TIEBREAK_MIN_SAMPLES,
    OBSERVED_COST_TIEBREAK_RECENCY_DAYS,
    RESOLVED_MODEL_ATTEMPT_BREAKDOWN_KEY,
    RESOLVED_MODEL_BREAKDOWN_KEY,
    ROLES,
    _identity_from_unique_spec,
    _identity_metadata,
    _infer_identity_from_key,
    _metric_sum,
    _normalize_band,
    _provider_from_cli,
    _resolve_agent_spec_for_profile_key,
    _success_count,
    _transport_from_identity,
    canonical_id_from_identity,
)

# ── Read model: the signals routing consults ─────────────────────────────
from .model_profiles_read_model import (  # noqa: F401
    _matching_profile_entries,
    _recency_params,
    _resolve_identity,
    _resolved_population,
    _trailing_clean_streak,
    _weighted_rate,
    _windowed_rate,
    get_dev_complexity_stats,
    get_dev_domain_complexity_signal,
    get_dev_domain_signal,
    get_dev_score_cost_stats,
    get_dev_signal,
    get_dev_success_rate,
    get_observed_cost_tiebreak_signal,
    get_review_signal,
    get_role_reliability_signal,
)

# ── Storage: run-outcome accumulation, reset, migration ──────────────────
from .model_profiles_storage import (  # noqa: F401
    _RELEASE_DATE_SUFFIX,
    ReviewerAttempt,
    RoleAttempt,
    RunOutcome,
    _bucket_summary,
    _ensure_model,
    _entry_model_label,
    _fold_completion_counters,
    _fold_cost,
    _fold_dev_bucket,
    _fold_dev_capability,
    _fold_domain_slice,
    _fold_duration,
    _fold_harness_terminated,
    _fold_resolved_model,
    _merge_completion,
    _merge_cost_section,
    _merge_dev,
    _merge_duration,
    _merge_entry,
    _merge_harness_terminated,
    _merge_planner,
    _merge_preflight,
    _merge_recent,
    _merge_review,
    _merge_tainted_runs,
    _project_alias_derived,
    _provider_to_cli_runner,
    _recompute_dev_section,
    _resolve_canonical_key,
    _resolve_storage_key,
    _strip_release_date,
    _unique_registry_spec,
    _update_dev,
    _update_planner,
    _update_preflight,
    _update_review,
    _update_review_completion,
    _update_role_completion,
    _zero_dev_bucket,
    _zero_planner_section,
    _zero_preflight_section,
    _zero_review_section,
    apply_run,
    backfill_from_history,
    canonical_id_for_legacy_key,
    load_profiles,
    load_reset_history,
    migrate_history_data,
    migrate_profiles_data,
    record_profile_reset,
    reset_profile_data,
    save_profiles,
    save_reset_history,
    summarize_profile_scope,
    update_from_run,
)

__all__ = [
    "ALIAS_DERIVED_KEY",
    "apply_run",
    "backfill_from_history",
    "_bucket_summary",
    "canonical_id_for_legacy_key",
    "canonical_id_from_identity",
    "CAPABILITY_RECENCY_WINDOW",
    "COMPLEXITY_BANDS",
    "DEFAULT_RECENCY_HALF_LIFE_RUNS",
    "DEFAULT_RECENCY_MODE",
    "DEFAULT_RECENCY_WINDOW",
    "DOMAIN_RECENCY_WINDOW",
    "_ensure_model",
    "_entry_model_label",
    "_fold_completion_counters",
    "_fold_cost",
    "_fold_dev_bucket",
    "_fold_dev_capability",
    "_fold_domain_slice",
    "_fold_duration",
    "_fold_harness_terminated",
    "_fold_resolved_model",
    "get_dev_complexity_stats",
    "get_dev_domain_complexity_signal",
    "get_dev_domain_signal",
    "get_dev_score_cost_stats",
    "get_dev_signal",
    "get_dev_success_rate",
    "get_observed_cost_tiebreak_signal",
    "get_review_signal",
    "get_role_reliability_signal",
    "_identity_from_unique_spec",
    "_identity_metadata",
    "_infer_identity_from_key",
    "load_profiles",
    "load_reset_history",
    "_matching_profile_entries",
    "_merge_completion",
    "_merge_cost_section",
    "_merge_dev",
    "_merge_duration",
    "_merge_entry",
    "_merge_harness_terminated",
    "_merge_planner",
    "_merge_preflight",
    "_merge_recent",
    "_merge_review",
    "_merge_tainted_runs",
    "_metric_sum",
    "migrate_history_data",
    "migrate_profiles_data",
    "_normalize_band",
    "OBSERVED_COST_TIEBREAK_MIN_SAMPLES",
    "OBSERVED_COST_TIEBREAK_RECENCY_DAYS",
    "_project_alias_derived",
    "_provider_from_cli",
    "_provider_to_cli_runner",
    "_recency_params",
    "_recompute_dev_section",
    "record_profile_reset",
    "_RELEASE_DATE_SUFFIX",
    "reset_profile_data",
    "_resolve_agent_spec_for_profile_key",
    "_resolve_canonical_key",
    "_resolve_identity",
    "_resolve_storage_key",
    "RESOLVED_MODEL_ATTEMPT_BREAKDOWN_KEY",
    "RESOLVED_MODEL_BREAKDOWN_KEY",
    "_resolved_population",
    "ReviewerAttempt",
    "RoleAttempt",
    "ROLES",
    "RunOutcome",
    "save_profiles",
    "save_reset_history",
    "_strip_release_date",
    "_success_count",
    "summarize_profile_scope",
    "_trailing_clean_streak",
    "_transport_from_identity",
    "_unique_registry_spec",
    "_update_dev",
    "update_from_run",
    "_update_planner",
    "_update_preflight",
    "_update_review",
    "_update_review_completion",
    "_update_role_completion",
    "_weighted_rate",
    "_windowed_rate",
    "_zero_dev_bucket",
    "_zero_planner_section",
    "_zero_preflight_section",
    "_zero_review_section",
]
