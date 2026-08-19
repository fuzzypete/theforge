"""Model capability profiles — the read model routing consults.

Owns the derivations assignment, exploration and adaptive sizing read from
stored profile state (#2467): the per-role reliability signals
(:func:`get_dev_signal`, :func:`get_review_signal`,
:func:`get_role_reliability_signal`), the per-domain fit signals, the
observed-cost tie-break, and the complexity/score stat readers. Every one of
them takes the stored profiles dict as an argument and returns a value — none
of them writes.

This module does NOT import :mod:`theforge.model_profiles_storage`, and must
not: that one-way boundary is what makes "how an outcome is folded into stored
history" and "what a signal returns" separate changes. A signal is therefore
exercisable against a profiles dict constructed directly in a test, with no run
outcome applied to produce it. Shared constants and identity resolution come
from :mod:`theforge.model_profiles_identity`.

Adding a routing signal is a change to this file alone.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from theforge.model_profiles_identity import (
    COMPLEXITY_BANDS,
    DEFAULT_RECENCY_HALF_LIFE_RUNS,
    DEFAULT_RECENCY_MODE,
    DEFAULT_RECENCY_WINDOW,
    DOMAIN_RECENCY_WINDOW,
    OBSERVED_COST_TIEBREAK_MIN_SAMPLES,
    OBSERVED_COST_TIEBREAK_RECENCY_DAYS,
    RESOLVED_MODEL_ATTEMPT_BREAKDOWN_KEY,
    RESOLVED_MODEL_BREAKDOWN_KEY,
    _infer_identity_from_key,
    _metric_sum,
    _normalize_band,
    _provider_from_cli,
    _success_count,
    canonical_id_from_identity,
)

# ── Reader API for assignment ─────────────────────────────────────────────


def _recency_params(recency: Any | None) -> tuple[str, float, int]:
    """Resolve (mode, half_life_runs, window) from a config object or defaults.

    ``recency`` is any object exposing ``mode`` / ``half_life_runs`` / ``window``
    (the config's ``assignment.recency`` block); ``None`` selects the module
    defaults. Kept duck-typed so this pure module never imports the config layer.
    """
    if recency is None:
        return DEFAULT_RECENCY_MODE, DEFAULT_RECENCY_HALF_LIFE_RUNS, DEFAULT_RECENCY_WINDOW
    mode = str(getattr(recency, "mode", DEFAULT_RECENCY_MODE) or DEFAULT_RECENCY_MODE)
    half_life = float(getattr(recency, "half_life_runs", DEFAULT_RECENCY_HALF_LIFE_RUNS))
    window = int(getattr(recency, "window", DEFAULT_RECENCY_WINDOW))
    return mode, half_life, window


def _weighted_rate(
    recent: list[float],
    *,
    fallback: float | None,
    mode: str = DEFAULT_RECENCY_MODE,
    half_life_runs: float = DEFAULT_RECENCY_HALF_LIFE_RUNS,
    window: int = DEFAULT_RECENCY_WINDOW,
    min_samples: int = 0,
) -> float | None:
    """Recency-weight a bounded ring of numeric outcomes (ADR-0006 clause 2.4).

    This is the single weighting path every profile-derived rate flows through
    (#1392): the per-complexity dev signal, the per-domain dev signal (via
    :func:`_windowed_rate`), the reviewer completion-rate signal, and the
    plan-reviewer value signals (:mod:`theforge.reviewer_value`). ``recent`` is an
    ordered outcome ring (oldest first, newest last), already bounded per bucket.
    Entries are ``0/1`` for the boolean signals (success / completion) and
    arbitrary floats for continuous ones (uniqueness rate, latency-per-P1); the
    weighted mean below is identical for the ``0/1`` case, so this generalization
    is behavior-preserving for every existing caller. Modes:

    - ``"exponential"`` (default): weight run at age ``a`` (0 = newest) by
      ``0.5 ** (a / half_life_runs)``, so a run decays to half its weight every
      ``half_life_runs`` runs. Recomputable from the stored ring after a
      parameter change; deterministic (no wall-clock).
    - ``"window"``: unweighted mean over the last ``window`` outcomes (the legacy
      per-domain behavior, preserved so both signals share this function).
    - ``"off"``: no recency weighting — returns ``fallback`` (the lifetime raw
      rate), an operator kill-switch.

    Falls back to ``fallback`` when no windowed data exists (e.g. a legacy bucket
    predating the ring), so the value is never silently zeroed. ``min_samples``
    extends that guard to a sample floor on the *ring itself*: until the ring has
    accumulated at least ``min_samples`` outcomes, the weighted value falls back
    to ``fallback`` (the lifetime raw rate). This is what keeps recency weighting
    composing cleanly with the sample floor for a legacy/migrated bucket whose
    lifetime ``runs`` already passes ``min_runs`` but whose ring holds only a
    handful of freshly-folded outcomes — one new failure must not be allowed to
    become the entire weighted sample and swing a model with strong long-term
    history to 0.0.
    """
    if mode == "off":
        return fallback
    if not recent:
        return fallback
    capped = recent[-window:] if window and window > 0 else list(recent)
    if not capped or len(capped) < min_samples:
        return fallback
    if mode == "window" or not half_life_runs or half_life_runs <= 0:
        return round(sum(capped) / len(capped), 4)
    decay = 0.5 ** (1.0 / half_life_runs)
    n = len(capped)
    num = 0.0
    den = 0.0
    for i, outcome in enumerate(capped):
        weight = decay ** (n - 1 - i)  # newest outcome → age 0 → weight 1.0
        num += weight * float(outcome)
        den += weight
    return round(num / den, 4) if den > 0 else fallback


def _resolved_population(
    buckets: list[dict],
    *,
    total_key: str = "runs",
    key: str = RESOLVED_MODEL_BREAKDOWN_KEY,
) -> dict:
    """Summarize which concrete versions produced a signal's population (#2226).

    Every routing signal below reports a rate over a population keyed by the
    identity that was *configured*. When that identity is a family alias, the
    population can span several concrete versions, and the rate is then an
    average over observations of different models. This says which versions, in
    what proportion, and — the one bit a consumer most needs — whether the
    population is ``mixed`` at all.

    Deliberately additive: it does not change the rate, the sample floor, or the
    weighting. A population that spans two versions is not automatically wrong;
    it is something the operator has to be able to *see* rather than infer from
    a behavioural surprise. ``attributed``/``unattributed`` make the coverage
    explicit, so a partly-pre-#2226 population cannot read as single-model just
    because the older half recorded no served version.

    ``key`` and ``total_key`` must name a matched pair — the breakdown and the
    counter it explains. A section can carry two populations (``runs`` and
    ``_attempted_count``), and reading a breakdown against the wrong denominator
    reports more attributed observations than the signal's population holds.

    Returns ``{}`` when nothing in the population carries a served version at
    all — an empty mapping rather than a fabricated single-model claim.
    """
    by_model: dict[str, int] = {}
    attributed = 0
    total_runs = 0
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        total_runs += int(bucket.get(total_key, 0))
        breakdown = bucket.get(key)
        if not isinstance(breakdown, dict):
            continue
        for model, counts in breakdown.items():
            if not isinstance(counts, dict):
                continue
            runs = int(counts.get("runs", 0))
            if runs <= 0:
                continue
            by_model[model] = by_model.get(model, 0) + runs
            attributed += runs
    if not by_model:
        return {}
    return {
        "by_resolved_model": dict(sorted(by_model.items(), key=lambda kv: (-kv[1], kv[0]))),
        "distinct": len(by_model),
        "mixed": len(by_model) > 1,
        "attributed": attributed,
        "unattributed": max(0, total_runs - attributed),
    }


def get_dev_signal(
    profiles: dict,
    model: str,
    complexity: str | None = None,
    min_runs: int = 3,
    *,
    actual_model: str | None = None,
    provider: str | None = None,
    cli: str | None = None,
    recency: Any | None = None,
) -> dict:
    """Return a structured dev routing signal for explainability + ranking.

    Reads only the in-memory ``profiles`` dict (no disk access, no LLM call).
    One lookup surfaces everything the router weighs for a dev candidate, so a
    caller can both rank and explain from a single read:

    - ``rate``: the ``min_runs``-gated **recency-weighted** success rate; ``None``
      below the sample floor. This is the value the router ranks on (#1392 —
      routing consults the decayed view, not the lifetime cumulative average).
    - ``raw``: the ungated lifetime cumulative success ratio (``None`` when no
      admissible runs exist), kept so the audit can show raw-vs-weighted drift.
    - ``weighted``: the recency-weighted value over the bucket's ``_recent`` ring
      (:func:`_weighted_rate`); falls back to ``raw`` until the ring itself holds
      at least ``min_runs`` outcomes, so a legacy/migrated bucket that passes the
      floor on cumulative history but has a nearly-empty ring is not driven by a
      single freshly-folded run. ``rate`` is this value gated by the sample floor.
    - ``runs``: admissible sample count consulted (tainted / harness-terminated
      runs already excluded upstream).
    - ``tainted_runs``: how many runs were excluded from this bucket for taint
      (ADR-0006 clause 4), surfaced so the exclusion stays visible in the audit.
    - ``floor``: ``"pass"`` when ``runs >= min_runs`` (and ``runs > 0``), else
      ``"fail"``.
    - ``weighting``: the recency parameters actually applied (mode / half-life /
      window) so an operator can reproduce ``weighted`` from ``raw`` history.
    - ``resolved_population``: which concrete versions produced this population
      (#2226) — ``by_resolved_model`` counts, ``distinct``, and ``mixed``. A
      candidate that names a family alias can accumulate observations of several
      served versions under one key; this is what lets a consumer tell a
      population describing one model from one describing several. Empty when no
      consulted observation recorded a served version.
    """
    matching = _matching_profile_entries(
        profiles,
        model,
        actual_model=actual_model,
        provider=provider,
        cli=cli,
    )
    return _aggregate_dev_signal(
        [entry for _, entry in matching],
        complexity=complexity,
        min_runs=min_runs,
        recency=recency,
    )


def _aggregate_dev_signal(
    entries: list[dict],
    *,
    complexity: str | None,
    min_runs: int,
    recency: Any | None,
) -> dict:
    """Aggregate dev history over already-selected profile entries.

    The single arithmetic path behind every dev signal — which entries are
    *selected* is the caller's question, and the two callers answer it
    differently on purpose: :func:`get_dev_signal` selects by the router's
    identity matching, :func:`get_dev_signal_for_keys` by an explicit key set.
    Keeping the sums, the recency weighting and the sample floor here is what
    stops those two selections from also becoming two ways to compute a rate.
    """
    mode, half_life, window = _recency_params(recency)
    runs = 0
    successes = 0.0
    tainted = 0
    recent: list[int] = []
    consulted: list[dict] = []
    band = _normalize_band(complexity) if complexity is not None else None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        dev = entry.get("dev")
        if not isinstance(dev, dict):
            continue
        section = dev if band is None else (dev.get("by_complexity") or {}).get(band)
        if not isinstance(section, dict):
            continue
        consulted.append(section)
        tainted += int(section.get("tainted_runs", 0))
        ring = section.get("_recent")
        if isinstance(ring, list):
            recent.extend(int(v) for v in ring)
        entry_runs = int(section.get("runs", 0))
        if entry_runs <= 0:
            continue
        runs += entry_runs
        successes += _success_count(section, entry_runs)
    raw = round(successes / runs, 4) if runs > 0 else None
    # The weighted value is gated on the *ring* reaching the same sample floor,
    # not just lifetime ``runs``: a legacy/migrated bucket can pass ``min_runs``
    # on cumulative history while its ring holds only a few freshly-folded
    # outcomes. Below that ring floor the weighted value falls back to raw so a
    # single new run cannot become the entire weighted sample (#1392 review).
    weighted = _weighted_rate(
        recent,
        fallback=raw,
        mode=mode,
        half_life_runs=half_life,
        window=window,
        min_samples=min_runs,
    )
    floor_ok = runs >= min_runs and runs > 0
    return {
        "raw": raw,
        "weighted": weighted,
        "runs": runs,
        "tainted_runs": tainted,
        "floor": "pass" if floor_ok else "fail",
        "weighting": {"mode": mode, "half_life_runs": half_life, "window": window},
        "rate": weighted if floor_ok else None,
        "resolved_population": _resolved_population(consulted),
    }


def get_dev_success_rate(
    profiles: dict,
    model: str,
    complexity: str | None = None,
    min_runs: int = 3,
    *,
    actual_model: str | None = None,
    provider: str | None = None,
    cli: str | None = None,
    recency: Any | None = None,
) -> float | None:
    """Return dev success rate for (model, complexity) or None under min_runs.

    Thin wrapper over :func:`get_dev_signal` so ranking and explainability
    share one aggregation path and can never diverge on the ranked value. The
    returned rate is the recency-weighted value (#1392), gated by ``min_runs``.
    """
    return get_dev_signal(
        profiles,
        model,
        complexity,
        min_runs,
        actual_model=actual_model,
        provider=provider,
        cli=cli,
        recency=recency,
    )["rate"]


def _trailing_clean_streak(recent: list[int]) -> int:
    """Length of the newest all-clean run in an ordered ``0/1`` outcome ring.

    ``recent`` is oldest-first / newest-last (the shape every ``_completion_recent``
    ring is folded in). Counts backwards from the newest outcome and stops at the
    first failure, so the result is the number of *consecutive* clean attempts a
    model has most recently strung together — the evidence the reviewer
    re-inclusion recovery rule is defined over (#1880).
    """
    streak = 0
    for outcome in reversed(recent):
        if int(outcome) != 1:
            break
        streak += 1
    return streak


def get_review_signal(
    profiles: dict,
    model: str,
    min_runs: int = 5,
    *,
    actual_model: str | None = None,
    provider: str | None = None,
    cli: str | None = None,
    recency: Any | None = None,
) -> dict:
    """Return a structured reviewer *completion* routing signal (#1388).

    The reviewer analog of :func:`get_dev_signal`, over the ``review`` section's
    attempt-completion counters rather than dev success. Reads only the in-memory
    ``profiles`` dict (no disk, no LLM); the authoritative evidence is the native
    reviewer-attempt telemetry these counters are folded from (ADR-0002).

    - ``rate``: the ``min_runs``-gated **recency-weighted** completion rate;
      ``None`` below the sample floor so a cold-start reviewer falls through to the
      existing tier/budget/cross-provider ordering instead of being penalized.
    - ``raw``: the ungated lifetime ``_completed_count / _attempted_count`` ratio
      (``None`` when no attempts exist), kept for raw-vs-weighted audit drift.
    - ``weighted``: the recency-weighted value over the ``_completion_recent``
      ring (:func:`_weighted_rate`); falls back to ``raw`` until the ring holds at
      least ``min_runs`` outcomes. ``rate`` is this value gated by the floor.
    - ``attempted`` / ``completed``: the running totals consulted (tainted runs
      already excluded upstream), so the rate is recomputable from the audit.
    - ``floor``: ``"pass"`` when ``attempted >= min_runs`` (and ``> 0``), else
      ``"fail"``.
    - ``weighting``: the recency parameters actually applied.
    - ``recovery``: the re-inclusion evidence (#1880) — ``clean_streak`` (how many
      of the newest ring outcomes are consecutively clean),
      ``clean_attempts_required`` (``K``, pinned to ``min_runs`` so the recovery
      rule reuses the existing sample floor rather than adding a config surface),
      and ``recovered`` (``True`` once the newest ``K`` attempts are all clean).
      This is reported unconditionally; the *threshold* comparison that decides
      whether recovery is relevant lives in the router, which owns the threshold.
    - ``resolved_population``: which concrete versions produced this population
      (#2226) — ``by_resolved_model`` counts, ``distinct``, and ``mixed``. A
      candidate that names a family alias can accumulate observations of several
      served versions under one key; this is what lets a consumer tell a
      population describing one model from one describing several. Empty when no
      consulted observation recorded a served version.
    """
    mode, half_life, window = _recency_params(recency)
    matching = _matching_profile_entries(
        profiles,
        model,
        actual_model=actual_model,
        provider=provider,
        cli=cli,
    )
    attempted = 0
    completed = 0
    tainted = 0
    recent: list[int] = []
    consulted: list[dict] = []
    for _, entry in matching:
        rev = entry.get("review")
        if not isinstance(rev, dict):
            continue
        consulted.append(rev)
        tainted += int(rev.get("tainted_runs", 0))
        attempted += int(rev.get("_attempted_count", 0))
        completed += int(rev.get("_completed_count", 0))
        ring = rev.get("_completion_recent")
        if isinstance(ring, list):
            recent.extend(int(v) for v in ring)
    raw = round(completed / attempted, 4) if attempted > 0 else None
    weighted = _weighted_rate(
        recent,
        fallback=raw,
        mode=mode,
        half_life_runs=half_life,
        window=window,
        min_samples=min_runs,
    )
    floor_ok = attempted >= min_runs and attempted > 0
    clean_streak = _trailing_clean_streak(recent)
    required_clean = max(int(min_runs), 1)
    return {
        "raw": raw,
        "weighted": weighted,
        "attempted": attempted,
        "completed": completed,
        "tainted_runs": tainted,
        "floor": "pass" if floor_ok else "fail",
        "weighting": {"mode": mode, "half_life_runs": half_life, "window": window},
        "rate": weighted if floor_ok else None,
        "recovery": {
            "clean_streak": clean_streak,
            "clean_attempts_required": required_clean,
            "recovered": clean_streak >= required_clean,
        },
        "resolved_population": _resolved_population(
            consulted,
            total_key="_attempted_count",
            key=RESOLVED_MODEL_ATTEMPT_BREAKDOWN_KEY,
        ),
    }


def get_role_reliability_signal(
    profiles: dict,
    model: str,
    role: str,
    min_runs: int = 5,
    *,
    actual_model: str | None = None,
    provider: str | None = None,
    cli: str | None = None,
    recency: Any | None = None,
) -> dict:
    """Return a structured role reliability signal for a non-dev role (#1489).

    Generalizes the reviewer *completion* signal (:func:`get_review_signal`, #1388)
    to any role whose section carries the shared attempt-completion counters —
    today ``preflight`` and ``planner``. Reads only the in-memory ``profiles`` dict
    (no disk, no LLM); the authoritative evidence is the native per-phase telemetry
    the counters are deterministically folded from (ADR-0002 / ADR-0006 clause 2).

    ``role`` is the profile section key (``"preflight"`` / ``"planner"``) so the
    signal is strictly per-role-scoped: a planner's history never leaks into a
    preflight decision. The returned shape matches :func:`get_review_signal` with an
    added ``role`` field so an audit can name the consulted signal:

    - ``rate``: the ``min_runs``-gated **recency-weighted** completion rate;
      ``None`` below the sample floor (cold start), so a role with too little
      admissible history falls through to the existing tier/budget ordering rather
      than being penalized (AC: cold-start ⇒ static policy).
    - ``raw``: the ungated lifetime ``_completed_count / _attempted_count`` ratio.
    - ``weighted``: the recency-weighted value over the ``_completion_recent`` ring.
    - ``attempted`` / ``completed``: running totals consulted (tainted runs already
      excluded upstream and surfaced under ``tainted_runs``).
    - ``floor``: ``"pass"`` when ``attempted >= min_runs`` (and ``> 0``), else
      ``"fail"``.
    - ``schema_ok``: whether the section carried recognizable completion counters —
      ``False`` for a legacy/foreign section shape, which forces a cold-start
      result (schema stability, ADR-0006 clause 2).
    - ``resolved_population``: which concrete versions produced this population
      (#2226) — ``by_resolved_model`` counts, ``distinct``, and ``mixed``. A
      candidate that names a family alias can accumulate observations of several
      served versions under one key; this is what lets a consumer tell a
      population describing one model from one describing several. Empty when no
      consulted observation recorded a served version.
    """
    mode, half_life, window = _recency_params(recency)
    matching = _matching_profile_entries(
        profiles,
        model,
        actual_model=actual_model,
        provider=provider,
        cli=cli,
    )
    attempted = 0
    completed = 0
    tainted = 0
    schema_ok = True
    recent: list[int] = []
    consulted: list[dict] = []
    for _, entry in matching:
        section = entry.get(role)
        if not isinstance(section, dict):
            continue
        consulted.append(section)
        tainted += int(section.get("tainted_runs", 0))
        # A section that carries runs/cost but no completion counters is an older
        # schema that predates #1489: it cannot answer the reliability question, so
        # it must not silently read as 0% completion. Mark it schema-incompatible
        # and let the floor force a cold-start result.
        if "_attempted_count" not in section and "completion_rate" not in section:
            if int(section.get("runs", 0)) > 0:
                schema_ok = False
            continue
        attempted += int(section.get("_attempted_count", 0))
        completed += int(section.get("_completed_count", 0))
        ring = section.get("_completion_recent")
        if isinstance(ring, list):
            recent.extend(int(v) for v in ring)
    raw = round(completed / attempted, 4) if attempted > 0 else None
    weighted = _weighted_rate(
        recent,
        fallback=raw,
        mode=mode,
        half_life_runs=half_life,
        window=window,
        min_samples=min_runs,
    )
    floor_ok = schema_ok and attempted >= min_runs and attempted > 0
    return {
        "role": role,
        "raw": raw,
        "weighted": weighted,
        "attempted": attempted,
        "completed": completed,
        "tainted_runs": tainted,
        "schema_ok": schema_ok,
        "floor": "pass" if floor_ok else "fail",
        "weighting": {"mode": mode, "half_life_runs": half_life, "window": window},
        "rate": weighted if floor_ok else None,
        "resolved_population": _resolved_population(
            consulted,
            total_key="_attempted_count",
            key=RESOLVED_MODEL_ATTEMPT_BREAKDOWN_KEY,
        ),
    }


def get_dev_domain_signal(
    profiles: dict,
    model: str,
    domains: list[str] | None,
    min_runs: int = 3,
    *,
    actual_model: str | None = None,
    provider: str | None = None,
    cli: str | None = None,
) -> dict:
    """Return a per-domain dev routing signal for the story's domain tags (#155).

    Aggregates the ``dev.by_domain`` slices for each requested tag across every
    matching profile entry, then clears the full ADR-0006 clause-2 admissibility
    set so a domain rate can only ever be a *preference within the eligible
    pool*, never eligibility itself. The value the router ranks on (``rate``) is
    the **recency-weighted** rate, not the lifetime cumulative ``raw``:

    - **Mechanical provenance / completeness (2.1–2.2):** the slice is folded by
      :func:`_fold_domain_slice`, which segregates harness-terminated runs out of
      the capability counts — the same complete recording path the complexity
      slice uses. No summary prose feeds this.
    - **Sample floor (2.3):** ``rate`` is ``None`` until ``runs >= min_runs``;
      below the floor the caller falls through to static tier/budget routing
      (cold start is a static-routing condition, not a low-confidence one).
    - **Recency weighting (2.4):** ``weighted`` is computed from a bounded recent
      window (:data:`DOMAIN_RECENCY_WINDOW`), a windowed view of history rather
      than the lifetime cumulative aggregate clause 2.4 forbids. ``rate`` returns
      the **weighted** value, so stale buckets decay out of routing weight; both
      ``raw`` and ``weighted`` are recorded (clause 7). When the shared decay
      mechanism (#1392) lands it supersedes this window without changing the read
      contract.
    - **Taint exclusion (clause 4):** tainted runs are never folded into the
      capability counts or the window; they are tallied under ``tainted_runs``
      (returned per-domain and in total) so the exclusion is visible and a
      tainted bucket cannot carry routing weight.
    - **Role specificity (per-role):** this reads only ``dev`` slices; it says
      nothing about review reliability.
    - **Schema stability (2.5):** legacy profiles simply lack ``by_domain`` and
      read as no-data (cold start), never as a penalty.

    A run tagged with several of the requested domains contributes to each of
    those domain buckets, so the aggregate double-counts across overlap — this is
    intentional: it rewards a model with broad demonstrated strength across the
    story's domains. The per-domain breakdown is returned in ``by_domain`` so the
    routing_decision block can show the matching profile slice per tag.

    ``resolved_population`` (#2226) reports which concrete versions produced the
    population, both in aggregate and per domain, so an alias candidate's
    per-domain rate is not read as describing one model when it describes
    several.

    Returns ``rate=None`` with ``floor="fail"`` when ``domains`` is empty or no
    admissible domain data exists — an explicit no-signal status, never a
    negative score.
    """
    requested = [d for d in (domains or []) if isinstance(d, str) and d]
    matching = _matching_profile_entries(
        profiles,
        model,
        actual_model=actual_model,
        provider=provider,
        cli=cli,
    )
    per_domain: dict[str, dict] = {}
    total_runs = 0
    total_successes = 0.0
    total_tainted = 0
    recent_all: list[int] = []
    consulted: list[dict] = []
    for domain in requested:
        d_runs = 0
        d_successes = 0.0
        d_tainted = 0
        d_recent: list[int] = []
        d_consulted: list[dict] = []
        for _, entry in matching:
            dev = entry.get("dev")
            if not isinstance(dev, dict):
                continue
            dd = (dev.get("by_domain") or {}).get(domain)
            if not isinstance(dd, dict):
                continue
            d_consulted.append(dd)
            d_tainted += int(dd.get("tainted_runs", 0))
            entry_runs = int(dd.get("runs", 0))
            if entry_runs <= 0:
                continue
            d_runs += entry_runs
            d_successes += _success_count(dd, entry_runs)
            recent = dd.get("_recent")
            if isinstance(recent, list):
                d_recent.extend(int(v) for v in recent)
        d_raw = round(d_successes / d_runs, 4) if d_runs > 0 else None
        d_weighted = _windowed_rate(d_recent, fallback=d_raw)
        per_domain[domain] = {
            "runs": d_runs,
            "raw": d_raw,
            "weighted": d_weighted,
            "tainted_runs": d_tainted,
            "resolved_population": _resolved_population(d_consulted),
        }
        consulted.extend(d_consulted)
        total_runs += d_runs
        total_successes += d_successes
        total_tainted += d_tainted
        recent_all.extend(d_recent)
    raw = round(total_successes / total_runs, 4) if total_runs > 0 else None
    weighted = _windowed_rate(recent_all, fallback=raw)
    floor_ok = total_runs >= min_runs and total_runs > 0
    return {
        "domains": requested,
        "raw": raw,
        # Admissible ranked value is the recency-weighted window, not lifetime raw.
        "weighted": weighted,
        "runs": total_runs,
        "tainted_runs": total_tainted,
        "floor": "pass" if floor_ok else "fail",
        "recency": "windowed",
        "rate": weighted if floor_ok else None,
        "by_domain": per_domain,
        "resolved_population": _resolved_population(consulted),
    }


def get_dev_domain_complexity_signal(
    profiles: dict,
    model: str,
    domains: list[str] | None,
    complexity: str | None,
    min_runs: int = 3,
    *,
    actual_model: str | None = None,
    provider: str | None = None,
    cli: str | None = None,
    recency: Any | None = None,
) -> dict:
    """Return the per-(domain, band) dev signal — the true cross aggregate (#325).

    Reads ``dev.by_domain[domain].by_complexity[band]`` — the nested slice folded
    alongside the flat marginals — for each requested domain tag, summed across
    matching profile entries. This is the aggregate a challenger-sampling routing
    key ``(phase, domain, band)`` is scoped to: unlike the flat ``by_domain``
    slice it does NOT pool across bands, and unlike ``by_complexity`` it does NOT
    pool across domains, so two keys differing on EITHER axis compute distinct
    runs / rate / cadence.

    The ranked ``rate`` is the **recency-weighted** value under the SAME shared,
    configurable mechanism as :func:`get_dev_signal` (#1392): the ``recency``
    object's ``mode`` / ``half_life_runs`` / ``window`` are honored, so an
    operator setting ``assignment.recency.mode`` to ``off`` (lifetime raw) or
    ``exponential`` gets that policy for domain-bearing routing slots too — not a
    hardcoded window. Taint-excluded.

    Returns ``rate=None`` (``floor="fail"``) when ``domains`` is empty or the
    (domain, band) slice has fewer than ``min_runs`` admissible runs — an explicit
    cold-start status for that specific routing key.
    """
    requested = [d for d in (domains or []) if isinstance(d, str) and d]
    band = _normalize_band(complexity)
    mode, half_life, window = _recency_params(recency)
    matching = _matching_profile_entries(
        profiles,
        model,
        actual_model=actual_model,
        provider=provider,
        cli=cli,
    )
    total_runs = 0
    total_successes = 0.0
    total_tainted = 0
    recent_all: list[int] = []
    for domain in requested:
        for _, entry in matching:
            dev = entry.get("dev")
            if not isinstance(dev, dict):
                continue
            dd = (dev.get("by_domain") or {}).get(domain)
            if not isinstance(dd, dict):
                continue
            bc = (dd.get("by_complexity") or {}).get(band)
            if not isinstance(bc, dict):
                continue
            total_tainted += int(bc.get("tainted_runs", 0))
            ring = bc.get("_recent")
            if isinstance(ring, list):
                recent_all.extend(int(v) for v in ring)
            entry_runs = int(bc.get("runs", 0))
            if entry_runs <= 0:
                continue
            total_runs += entry_runs
            total_successes += _success_count(bc, entry_runs)
    raw = round(total_successes / total_runs, 4) if total_runs > 0 else None
    # Shared configurable recency mechanism (#1392) — same as get_dev_signal, so
    # assignment.recency.mode/half_life/window applies to these routing slots too.
    weighted = _weighted_rate(
        recent_all,
        fallback=raw,
        mode=mode,
        half_life_runs=half_life,
        window=window,
        min_samples=min_runs,
    )
    floor_ok = total_runs >= min_runs and total_runs > 0
    return {
        "domains": requested,
        "band": band,
        "raw": raw,
        "weighted": weighted,
        "runs": total_runs,
        "tainted_runs": total_tainted,
        "floor": "pass" if floor_ok else "fail",
        "weighting": {"mode": mode, "half_life_runs": half_life, "window": window},
        "rate": weighted if floor_ok else None,
    }


def get_observed_cost_tiebreak_signal(
    observed_costs: dict | None,
    model_key: str,
    *,
    role: str,
    complexity: str | None,
    reasoning_effort: str | None,
    seed: float,
    actual_model: str | None = None,
    provider: str | None = None,
    cli: str | None = None,
    min_samples: int = OBSERVED_COST_TIEBREAK_MIN_SAMPLES,
    max_age_days: int = OBSERVED_COST_TIEBREAK_RECENCY_DAYS,
    now: datetime | None = None,
) -> dict[str, object]:
    """Return the cohort-aware cost tiebreak signal for one candidate.

    Observed spend is admissible only when the model has a matched invocation
    cohort on role, complexity, and requested reasoning effort, the cohort meets
    its sample floor, and its newest observation is recent enough. Otherwise the
    router falls back to the deterministic declared-price seed it already uses.
    """
    canonical = canonical_id_from_identity(actual_model=actual_model, provider=provider, cli=cli)
    storage_key = canonical or model_key
    cohort = {
        "role": str(role or "").upper(),
        "complexity": str(complexity or "").upper() if complexity else None,
        "reasoning_effort": str(reasoning_effort or "").lower() if reasoning_effort else None,
    }
    signal: dict[str, object] = {
        "value": seed,
        "source": "seed",
        "seed": seed,
        "reason": "no_matching_cohort",
        "observations": 0,
        "cohort": cohort,
    }
    if not observed_costs:
        return signal
    if not (cohort["role"] and cohort["complexity"] and cohort["reasoning_effort"]):
        signal["reason"] = "cohort_incomplete"
        return signal
    raw_model = observed_costs.get(storage_key)
    if not isinstance(raw_model, dict):
        return signal
    cohort_key = f"{cohort['role']}|{cohort['complexity']}|{cohort['reasoning_effort']}"
    raw_cohort = raw_model.get(cohort_key)
    if not isinstance(raw_cohort, dict):
        return signal
    observations = raw_cohort.get("observations")
    if not isinstance(observations, list):
        signal["reason"] = "cohort_malformed"
        return signal
    signal["observations"] = len(observations)
    floor = max(1, int(min_samples))
    if len(observations) < floor:
        signal["reason"] = "below_sample_floor"
        return signal
    latest_seen: datetime | None = None
    costs: list[float] = []
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        cost = obs.get("cost_usd")
        if not isinstance(cost, (int, float)):
            continue
        costs.append(float(cost))
        stamp = obs.get("started_at")
        if not isinstance(stamp, str) or not stamp.strip():
            continue
        try:
            parsed = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        latest_seen = parsed if latest_seen is None or parsed > latest_seen else latest_seen
    signal["observations"] = len(costs)
    if len(costs) < floor:
        signal["reason"] = "below_sample_floor"
        return signal
    if latest_seen is None:
        signal["reason"] = "stale_or_undated"
        return signal
    as_of = now.astimezone(UTC) if isinstance(now, datetime) else datetime.now(UTC)
    age_days = (as_of - latest_seen).total_seconds() / 86400.0
    signal["latest_started_at"] = latest_seen.isoformat()
    signal["max_age_days"] = int(max_age_days)
    if age_days > float(max_age_days):
        signal["reason"] = "stale"
        return signal
    signal["value"] = round(float(statistics.median(costs)), 6)
    signal["source"] = "observed"
    signal["reason"] = "observed_median"
    return signal


def _windowed_rate(recent: list[int], *, fallback: float | None) -> float | None:
    """Return the recency-weighted rate over a bounded per-domain outcome window.

    ``recent`` is the concatenation of the ``_recent`` rings clause 2.4 maintains
    per domain slice (each already capped to :data:`DOMAIN_RECENCY_WINDOW`). Kept
    as a thin wrapper over the shared :func:`_weighted_rate` (#1392) in ``window``
    mode so the per-domain signal uses the same weighting function as the
    per-complexity signal — an unweighted mean over the last
    :data:`DOMAIN_RECENCY_WINDOW` outcomes — rather than a bespoke per-call-site
    computation. Falls back to ``fallback`` (the lifetime raw rate) when no
    windowed data exists so the value is never silently zeroed.
    """
    return _weighted_rate(recent, fallback=fallback, mode="window", window=DOMAIN_RECENCY_WINDOW)


def get_dev_complexity_stats(
    profiles: dict,
    model: str,
    complexity: str | None,
    *,
    min_runs: int = 3,
    actual_model: str | None = None,
    provider: str | None = None,
    cli: str | None = None,
) -> dict[str, float] | None:
    """Return per-band dev averages when the complexity band has enough runs."""
    matching = _matching_profile_entries(
        profiles,
        model,
        actual_model=actual_model,
        provider=provider,
        cli=cli,
    )
    if not matching:
        return None
    band = _normalize_band(complexity)
    runs = 0
    measured_runs = 0
    iterations_sum = 0.0
    cost_sum = 0.0
    duration_runs = 0
    max_duration_s = 0.0
    max_killed_timeout_s = 0.0
    for _, entry in matching:
        dev = entry.get("dev")
        if not isinstance(dev, dict):
            continue
        bc = (dev.get("by_complexity") or {}).get(band)
        if not isinstance(bc, dict):
            continue
        entry_runs = int(bc.get("runs", 0))
        if entry_runs <= 0:
            continue
        entry_iterations = _metric_sum(bc, entry_runs, "_iterations_sum", "avg_iterations")
        entry_cost = _metric_sum(bc, entry_runs, "_cost_sum", "avg_cost_usd")
        if entry_iterations is None or entry_cost is None:
            return None
        runs += entry_runs
        # Cost is a MEASURED-run average: divide by (runs - unmeasured), never by
        # total runs, so unmeasured runs don't dilute the average toward zero.
        measured_runs += entry_runs - int(bc.get("_cost_unknown_runs", 0))
        iterations_sum += entry_iterations
        cost_sum += entry_cost
        # Duration/kill floors: legacy profiles predate these fields, so a missing
        # key defaults to 0 rather than voiding the whole (iteration) result.
        duration_runs += int(bc.get("_duration_runs", 0))
        max_duration_s = max(max_duration_s, float(bc.get("max_duration_s", 0.0)))
        max_killed_timeout_s = max(
            max_killed_timeout_s, float(bc.get("max_killed_timeout_s", 0.0))
        )
    if runs < min_runs or runs <= 0:
        return None
    return {
        "runs": float(runs),
        "avg_iterations": round(iterations_sum / runs, 4),
        "avg_cost_usd": round(cost_sum / measured_runs, 6) if measured_runs > 0 else 0.0,
        # How many of those runs actually had a measurable cost. Without this the
        # caller cannot tell a genuinely $0.00 band from an entirely UNMEASURED
        # one — both report ``avg_cost_usd: 0.0`` — and cost-ranked routing
        # (#2392) would treat an unmeasured model as free.
        "cost_measured_runs": float(measured_runs),
        "max_duration_s": round(max_duration_s, 4),
        "duration_runs": float(duration_runs),
        "max_killed_timeout_s": round(max_killed_timeout_s, 4),
    }


def get_dev_score_cost_stats(
    profiles: dict,
    complexity_score: int | None,
    *,
    min_runs: int = 3,
) -> dict[str, float] | None:
    """Return dev cost stats for ONE complexity score, across every model.

    Deliberately unlike :func:`get_dev_complexity_stats`, which narrows to a
    complexity *band* and to the profile entries that share the selected
    model's identity. The per-story allocation is derived from the whole-story
    costs recorded at the story's complexity *score*, across all models
    (``audit_substrate.derive_cost_samples_by_score``). A dev projection that
    is going to be subtracted from that allocation has to describe the same
    population, so this aggregation keeps the score and drops both the band
    widening and the model narrowing.

    Cost only: iteration and wall-clock sizing still read the per-band,
    per-model stats, because those are properties of the model doing the work
    rather than of the story's complexity.

    Returns ``None`` when fewer than ``min_runs`` runs exist at the score, or
    when no run at the score had a measurable cost — an unmeasured population
    cannot be averaged into a dollar figure.
    """
    try:
        score_key = str(int(complexity_score))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    models = (profiles or {}).get("models") or {}
    if not isinstance(models, dict):
        return None
    runs = 0
    measured_runs = 0
    cost_sum = 0.0
    for entry in models.values():
        if not isinstance(entry, dict):
            continue
        dev = entry.get("dev")
        if not isinstance(dev, dict):
            continue
        by_score = dev.get("by_complexity_score")
        if not isinstance(by_score, dict):
            continue
        sc = by_score.get(score_key)
        if not isinstance(sc, dict):
            continue
        entry_runs = int(sc.get("runs", 0))
        if entry_runs <= 0:
            continue
        entry_cost = _metric_sum(sc, entry_runs, "_cost_sum", "avg_cost_usd")
        if entry_cost is None:
            return None
        runs += entry_runs
        # Same measured-cost arithmetic as the per-band stats: unmeasured runs
        # must not dilute the average toward zero.
        measured_runs += entry_runs - int(sc.get("_cost_unknown_runs", 0))
        cost_sum += entry_cost
    if runs < min_runs or runs <= 0 or measured_runs <= 0:
        return None
    return {
        "runs": float(runs),
        "measured_runs": float(measured_runs),
        "avg_cost_usd": round(cost_sum / measured_runs, 6),
    }


def list_dev_evidence_keys(
    profiles: dict,
    *,
    resolve: Callable[[str, dict], str | None] | None = None,
) -> list[dict]:
    """Enumerate every stored key carrying dev evidence, with attribution (#2308).

    The routing signals above answer "what does the evidence say about *this*
    model"; this answers the inverse — "whose evidence is in the store at all,
    and can each key still be named". Both questions read the same stored dict,
    but only the second one can tell a comparison drawn against a live model
    from one drawn against a key that is no longer in use.

    ``resolve`` maps ``(key, entry)`` to a canonical ``provider/model/transport``
    ID, or ``None`` when the key cannot be named unambiguously. It is injected
    rather than imported because the resolver lives in
    :mod:`theforge.model_profiles_storage`, which this module must not import.
    With no resolver every key reports ``resolution="unresolved"``.

    Each record carries:

    - ``key`` / ``entry_runs``: the stored key and its lifetime dev run count.
    - ``canonical_id``: the resolved identity, or ``None``.
    - ``resolution``: ``"canonical"`` (the key already *is* the canonical ID),
      ``"legacy"`` (a shorthand or role name that resolves onto one), or
      ``"unresolved"``.
    - ``runs_by_band``: admissible dev runs per complexity band.
    - ``last_updated``: always ``None`` — profiles record no per-key timestamp,
      so recency of a key's evidence is *unknown*, which is a different claim
      from "recent" and must not be silently filtered on.
    """
    models = (profiles or {}).get("models") or {}
    if not isinstance(models, dict):
        return []
    records: list[dict] = []
    for key, entry in sorted(models.items()):
        if not isinstance(entry, dict):
            continue
        dev = entry.get("dev")
        dev = dev if isinstance(dev, dict) else {}
        by_complexity = dev.get("by_complexity")
        by_complexity = by_complexity if isinstance(by_complexity, dict) else {}
        runs_by_band = {}
        for band in COMPLEXITY_BANDS:
            bucket = by_complexity.get(band)
            runs_by_band[band] = int(bucket.get("runs", 0)) if isinstance(bucket, dict) else 0
        canonical_id = resolve(key, entry) if resolve is not None else None
        if canonical_id is None:
            resolution = "unresolved"
        elif canonical_id == key:
            resolution = "canonical"
        else:
            resolution = "legacy"
        records.append(
            {
                "key": key,
                "canonical_id": canonical_id,
                "resolution": resolution,
                "entry_runs": int(dev.get("runs", 0)),
                "runs_by_band": runs_by_band,
                "last_updated": None,
            }
        )
    return records


def get_dev_signal_for_keys(
    profiles: dict,
    keys: list[str] | tuple[str, ...],
    complexity: str | None = None,
    min_runs: int = 3,
    *,
    recency: Any | None = None,
) -> dict:
    """Return the dev signal over an **explicitly named** set of stored keys.

    Same shape and arithmetic as :func:`get_dev_signal`; the difference is how
    the population is chosen. The router's signal selects entries by identity
    matching on ``(provider, model)``, which is deliberately loose: it is how a
    candidate finds its own history under whatever key it was recorded with.

    A caller that has *already* decided which keys belong to which identity —
    the declared-vs-observed report, which partitions the store by canonical ID
    so evidence it excludes as unattributable cannot also be counted (#2308) —
    needs the population to be exactly that decision and nothing else. Passing
    the key set explicitly is what makes the two answers one answer: a key
    absent from ``keys`` contributes nothing, however its identity metadata
    reads. Unknown keys are ignored rather than raising.
    """
    models = (profiles or {}).get("models") or {}
    if not isinstance(models, dict):
        models = {}
    return _aggregate_dev_signal(
        [models[key] for key in keys if isinstance(models.get(key), dict)],
        complexity=complexity,
        min_runs=min_runs,
        recency=recency,
    )


def _matching_profile_entries(
    profiles: dict,
    model_key: str,
    *,
    actual_model: str | None,
    provider: str | None,
    cli: str | None,
) -> list[tuple[str, dict]]:
    models = (profiles or {}).get("models") or {}
    if not isinstance(models, dict):
        return []
    target = _resolve_identity(
        model_key,
        None,
        actual_model=actual_model,
        provider=provider,
        cli=cli,
    )
    matching: list[tuple[str, dict]] = []
    for key, entry in models.items():
        if not isinstance(entry, dict):
            continue
        if key == model_key:
            matching.append((key, entry))
            continue
        if target is None:
            continue
        if _resolve_identity(key, entry) == target:
            matching.append((key, entry))
    return matching


def _resolve_identity(
    model_key: str,
    entry: dict | None,
    *,
    actual_model: str | None = None,
    provider: str | None = None,
    cli: str | None = None,
) -> tuple[str, str] | None:
    metadata = entry.get("_identity") if isinstance(entry, dict) else None
    if isinstance(metadata, dict):
        meta_provider = str(metadata.get("provider") or "").strip()
        meta_model = str(metadata.get("model") or "").strip()
        if meta_provider and meta_model:
            return (meta_provider, meta_model)
    explicit_provider = (provider or _provider_from_cli(cli) or "").strip()
    explicit_model = (actual_model or "").strip()
    if explicit_provider and explicit_model:
        return (explicit_provider, explicit_model)
    inferred = _infer_identity_from_key(model_key)
    if inferred is not None:
        return inferred
    if explicit_provider and model_key:
        return (explicit_provider, model_key)
    return None
