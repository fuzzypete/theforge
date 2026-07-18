"""Model capability profiles — aggregated per-model, per-role run outcomes.

Maintains ``.forge/model_profiles.yaml`` with success rate, iteration count and
cost averages for each model, broken down by role (dev, review, preflight) and
— for the dev role — by complexity band. Updated after every run from
``CoordinatorState``; seeded from ``assignment_history.yaml`` on first run.

This module has a thin I/O surface (``load_profiles``/``save_profiles``); all
aggregation is pure. No LLM calls.

Schema on disk::

    models:
      <name>:
        dev:
          runs, success_rate, avg_iterations, avg_cost_usd
          by_complexity:
            small|medium|large: {runs, success_rate, avg_iterations, avg_cost_usd}
        review:
          runs, avg_findings, avg_cost_usd
        preflight:
          runs, avg_cost_usd

Underscore-prefixed siblings (``_successes``, ``_iterations_sum``, ``_cost_sum``,
``_findings_sum``, ``_cost_unknown_runs``) are the running accumulators used to
update the derived fields in place. They are part of the persisted schema.

``_cost_unknown_runs`` counts runs whose transport could not measure cost (a
``None`` cost signal, e.g. a CLI runner that emits no token usage). Those runs
are *not* folded into ``_cost_sum`` and ``avg_cost_usd`` is averaged over
measured runs only (``runs - _cost_unknown_runs``). This keeps a genuinely free
($0.00 measured) run distinct from an unmeasured one so spend never silently
disappears from the ledger.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

ROLES = ("dev", "review", "preflight")
COMPLEXITY_BANDS = ("small", "medium", "large")


# ── Data carrier ──────────────────────────────────────────────────────────


@dataclass
class RunOutcome:
    """Everything ``update_from_run`` needs about one coordinator run.

    ``reviewers`` maps each reviewer profile name to a
    ``(cycles_participated, findings_observed, cost_usd)`` triple attributed to
    that reviewer. Attribution at the per-reviewer level is approximate —
    reviewers see the same code so findings_observed is the full per-cycle
    aggregate and cost is divided evenly across successful reviewers in each
    cycle.
    """

    complexity: str
    dev_model: str
    dev_success: bool
    dev_iterations: int
    dev_cost_usd: float | None  # None = the transport could not measure cost
    complexity_score: int | None = None
    # Observed wall-clock of the completed dev phase (seconds); None when unknown
    # or when the run was harness-killed (a kill's true duration is unknown).
    dev_duration_s: float | None = None
    # True when the harness terminated the dev phase at its timeout limit. Such a
    # run is a censored observation: its duration only bounds the timeout from
    # below and must never lower learned duration.
    dev_timeout_killed: bool = False
    # The granted per-story timeout (seconds) at which a killed run was terminated.
    dev_timeout_limit_s: int | None = None
    preflight_model: str | None = None
    dev_actual_model: str | None = None
    dev_provider: str | None = None
    dev_cli: str | None = None
    preflight_actual_model: str | None = None
    preflight_provider: str | None = None
    preflight_cli: str | None = None
    preflight_cost_usd: float | None = None  # None = cost unmeasured
    reviewers: dict[str, tuple[int, int, float]] = field(default_factory=dict)


# ── I/O ───────────────────────────────────────────────────────────────────


def load_profiles(path: Path) -> dict[str, Any]:
    """Read ``model_profiles.yaml``; return ``{"models": {}}`` if absent/bad."""
    if not path.exists():
        return {"models": {}}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as exc:  # noqa: BLE001
        log.warning("[model_profiles] Failed to load %s: %s", path, exc)
        return {"models": {}}
    if not isinstance(data, dict):
        return {"models": {}}
    if not isinstance(data.get("models"), dict):
        data["models"] = {}
    return data


def save_profiles(path: Path, data: dict[str, Any]) -> None:
    """Write profiles to disk; best-effort (warns but doesn't raise)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("[model_profiles] Failed to write %s: %s", path, exc)


def load_reset_history(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Read the profile reset audit log; return an empty skeleton if absent/bad."""
    if not path.exists():
        return {"resets": []}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as exc:  # noqa: BLE001
        log.warning("[model_profiles] Failed to load reset history %s: %s", path, exc)
        return {"resets": []}
    if not isinstance(data, dict):
        return {"resets": []}
    resets = data.get("resets")
    if not isinstance(resets, list):
        data["resets"] = []
    return data


def save_reset_history(path: Path, data: dict[str, list[dict[str, Any]]]) -> None:
    """Write the reset audit log to disk; best-effort (warns but doesn't raise)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("[model_profiles] Failed to write reset history %s: %s", path, exc)


# ── Pure aggregation ──────────────────────────────────────────────────────


def _normalize_band(complexity: str | None) -> str:
    """Map legacy/alt complexity strings to small|medium|large."""
    if not complexity:
        return "medium"
    cl = complexity.lower()
    if cl in COMPLEXITY_BANDS:
        return cl
    return {"low": "small", "high": "large"}.get(cl, "medium")


def _provider_from_cli(cli: str | None) -> str | None:
    return {
        "claude": "anthropic",
        "codex": "openai",
        "gemini": "google",
    }.get((cli or "").strip().lower() or "")


def _transport_from_identity(provider: str | None, cli: str | None) -> str | None:
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


def _ensure_model(
    data: dict,
    name: str,
    *,
    actual_model: str | None = None,
    provider: str | None = None,
    cli: str | None = None,
) -> dict:
    models = data.setdefault("models", {})
    canonical = canonical_id_from_identity(actual_model=actual_model, provider=provider, cli=cli)
    storage_key = canonical or name
    entry = models.setdefault(storage_key, {})
    metadata = _identity_metadata(actual_model=actual_model, provider=provider, cli=cli)
    if metadata and not isinstance(entry.get("_identity"), dict):
        entry["_identity"] = metadata
    return entry


def _entry_model_label(entry: dict) -> str:
    ident = entry.get("_identity")
    if isinstance(ident, dict):
        return str(ident.get("model") or "?")
    return "?"


def _fold_cost(bucket: dict, cost_usd: float | None, *, unknown_count: int = 1) -> None:
    """Fold one run's cost into ``bucket``; needs ``bucket['runs']`` already set.

    ``cost_usd is None`` means the transport could not measure cost: the run is
    tallied in ``_cost_unknown_runs`` (never added to ``_cost_sum``) and
    ``avg_cost_usd`` is averaged over MEASURED runs only. ``unknown_count`` lets
    the review path attribute more than one unmeasured run in a single call.
    """
    cost_sum = float(bucket.get("_cost_sum", 0.0))
    unknown = int(bucket.get("_cost_unknown_runs", 0))
    if cost_usd is None:
        unknown += unknown_count
    else:
        cost_sum += float(cost_usd)
    bucket["_cost_sum"] = cost_sum
    bucket["_cost_unknown_runs"] = unknown
    measured = int(bucket.get("runs", 0)) - unknown
    bucket["avg_cost_usd"] = round(cost_sum / measured, 6) if measured > 0 else 0.0


def _fold_duration(
    bucket: dict,
    duration_s: float | None,
    killed: bool,
    limit_s: int | None,
) -> None:
    """Fold one dev run's wall-clock signal into ``bucket``.

    Two disjoint cases, kept segregated so harness-killed runs never contaminate
    learned completed-run duration (see #1763):

    - A COMPLETED run (``not killed``) with a known ``duration_s`` accumulates
      into ``_duration_sum``/``_duration_runs`` (→ ``avg_duration_s``) and raises
      ``max_duration_s``.
    - A harness-KILLED run only raises ``max_killed_timeout_s`` (the limit that
      terminated it). Its true duration is unknown and only bounds the timeout
      from below — it never touches the duration accumulators, so a kill can
      never lower learned duration.
    """
    if not killed and duration_s is not None:
        dur_sum = float(bucket.get("_duration_sum", 0.0)) + float(duration_s)
        dur_runs = int(bucket.get("_duration_runs", 0)) + 1
        bucket["_duration_sum"] = dur_sum
        bucket["_duration_runs"] = dur_runs
        bucket["avg_duration_s"] = round(dur_sum / dur_runs, 4) if dur_runs > 0 else 0.0
        bucket["max_duration_s"] = max(float(bucket.get("max_duration_s", 0.0)), float(duration_s))
    if killed and limit_s is not None:
        bucket["max_killed_timeout_s"] = max(
            float(bucket.get("max_killed_timeout_s", 0.0)), float(limit_s)
        )


def _update_dev(
    entry: dict,
    complexity: str,
    success: bool,
    iterations: int,
    cost_usd: float | None,
    complexity_score: int | None = None,
    duration_s: float | None = None,
    timeout_killed: bool = False,
    timeout_limit_s: int | None = None,
) -> None:
    if cost_usd is None:
        log.warning(
            "[model_profiles] Dev run recorded cost-unmeasured (NOT $0.00): "
            "model=%s complexity=%s — transport reported no cost.",
            _entry_model_label(entry),
            complexity,
        )
    dev = entry.setdefault("dev", {})
    runs = int(dev.get("runs", 0)) + 1
    successes = int(dev.get("_successes", 0)) + (1 if success else 0)
    iter_sum = float(dev.get("_iterations_sum", 0.0)) + float(iterations)
    dev["runs"] = runs
    dev["_successes"] = successes
    dev["_iterations_sum"] = iter_sum
    dev["success_rate"] = round(successes / runs, 4)
    dev["avg_iterations"] = round(iter_sum / runs, 4)
    _fold_cost(dev, cost_usd)
    _fold_duration(dev, duration_s, timeout_killed, timeout_limit_s)

    by = dev.setdefault("by_complexity", {})
    bc = by.setdefault(complexity, {})
    bc_runs = int(bc.get("runs", 0)) + 1
    bc_successes = int(bc.get("_successes", 0)) + (1 if success else 0)
    bc_iter_sum = float(bc.get("_iterations_sum", 0.0)) + float(iterations)
    bc["runs"] = bc_runs
    bc["_successes"] = bc_successes
    bc["_iterations_sum"] = bc_iter_sum
    bc["success_rate"] = round(bc_successes / bc_runs, 4)
    bc["avg_iterations"] = round(bc_iter_sum / bc_runs, 4)
    _fold_cost(bc, cost_usd)
    _fold_duration(bc, duration_s, timeout_killed, timeout_limit_s)

    if complexity_score is not None:
        score_key = str(int(complexity_score))
        by_score = dev.setdefault("by_complexity_score", {})
        sc = by_score.setdefault(score_key, {})
        sc_runs = int(sc.get("runs", 0)) + 1
        sc_successes = int(sc.get("_successes", 0)) + (1 if success else 0)
        sc_iter_sum = float(sc.get("_iterations_sum", 0.0)) + float(iterations)
        sc["runs"] = sc_runs
        sc["_successes"] = sc_successes
        sc["_iterations_sum"] = sc_iter_sum
        sc["success_rate"] = round(sc_successes / sc_runs, 4)
        sc["avg_iterations"] = round(sc_iter_sum / sc_runs, 4)
        _fold_cost(sc, cost_usd)
        _fold_duration(sc, duration_s, timeout_killed, timeout_limit_s)


def _update_review(entry: dict, cycles: int, findings: int, cost_usd: float | None) -> None:
    if cycles <= 0:
        return
    if cost_usd is None:
        log.warning(
            "[model_profiles] Review run recorded cost-unmeasured (NOT $0.00): "
            "model=%s cycles=%d — transport reported no cost.",
            _entry_model_label(entry),
            cycles,
        )
    rev = entry.setdefault("review", {})
    runs = int(rev.get("runs", 0)) + cycles
    find_sum = float(rev.get("_findings_sum", 0.0)) + float(findings)
    rev["runs"] = runs
    rev["_findings_sum"] = find_sum
    rev["avg_findings"] = round(find_sum / runs, 4)
    _fold_cost(rev, cost_usd, unknown_count=cycles)


def _update_preflight(entry: dict, cost_usd: float | None) -> None:
    if cost_usd is None:
        log.warning(
            "[model_profiles] Preflight run recorded cost-unmeasured (NOT $0.00): "
            "model=%s — transport reported no cost.",
            _entry_model_label(entry),
        )
    pf = entry.setdefault("preflight", {})
    runs = int(pf.get("runs", 0)) + 1
    pf["runs"] = runs
    _fold_cost(pf, cost_usd)


def _zero_dev_bucket(bucket: dict) -> None:
    bucket["runs"] = 0
    bucket["_successes"] = 0
    bucket["_iterations_sum"] = 0.0
    bucket["_cost_sum"] = 0.0
    bucket["_cost_unknown_runs"] = 0
    bucket["success_rate"] = 0.0
    bucket["avg_iterations"] = 0.0
    bucket["avg_cost_usd"] = 0.0
    bucket["_duration_sum"] = 0.0
    bucket["_duration_runs"] = 0
    bucket["avg_duration_s"] = 0.0
    bucket["max_duration_s"] = 0.0
    bucket["max_killed_timeout_s"] = 0.0


def _zero_review_section(section: dict) -> None:
    section["runs"] = 0
    section["_findings_sum"] = 0.0
    section["_cost_sum"] = 0.0
    section["_cost_unknown_runs"] = 0
    section["avg_findings"] = 0.0
    section["avg_cost_usd"] = 0.0


def _zero_preflight_section(section: dict) -> None:
    section["runs"] = 0
    section["_cost_sum"] = 0.0
    section["_cost_unknown_runs"] = 0
    section["avg_cost_usd"] = 0.0


def _recompute_dev_section(section: dict) -> None:
    by = section.setdefault("by_complexity", {})
    runs = 0
    successes = 0
    iterations = 0.0
    cost = 0.0
    cost_unknown = 0
    duration_sum = 0.0
    duration_runs = 0
    max_duration = 0.0
    max_killed_timeout = 0.0
    for band in COMPLEXITY_BANDS:
        bucket = by.setdefault(band, {})
        runs += int(bucket.get("runs", 0))
        successes += int(bucket.get("_successes", 0))
        iterations += float(bucket.get("_iterations_sum", 0.0))
        cost += float(bucket.get("_cost_sum", 0.0))
        cost_unknown += int(bucket.get("_cost_unknown_runs", 0))
        duration_sum += float(bucket.get("_duration_sum", 0.0))
        duration_runs += int(bucket.get("_duration_runs", 0))
        max_duration = max(max_duration, float(bucket.get("max_duration_s", 0.0)))
        max_killed_timeout = max(
            max_killed_timeout, float(bucket.get("max_killed_timeout_s", 0.0))
        )
    section["runs"] = runs
    section["_successes"] = successes
    section["_iterations_sum"] = iterations
    section["_cost_sum"] = cost
    section["_cost_unknown_runs"] = cost_unknown
    section["success_rate"] = round(successes / runs, 4) if runs > 0 else 0.0
    section["avg_iterations"] = round(iterations / runs, 4) if runs > 0 else 0.0
    measured = runs - cost_unknown
    section["avg_cost_usd"] = round(cost / measured, 6) if measured > 0 else 0.0
    section["_duration_sum"] = duration_sum
    section["_duration_runs"] = duration_runs
    section["avg_duration_s"] = (
        round(duration_sum / duration_runs, 4) if duration_runs > 0 else 0.0
    )
    section["max_duration_s"] = max_duration
    section["max_killed_timeout_s"] = max_killed_timeout


def apply_run(data: dict, outcome: RunOutcome) -> dict:
    """Pure: fold one run outcome into the profiles dict, returning it."""
    band = _normalize_band(outcome.complexity)
    dev_entry = _ensure_model(
        data,
        outcome.dev_model,
        actual_model=outcome.dev_actual_model,
        provider=outcome.dev_provider,
        cli=outcome.dev_cli,
    )
    _update_dev(
        dev_entry,
        band,
        outcome.dev_success,
        outcome.dev_iterations,
        outcome.dev_cost_usd,
        complexity_score=outcome.complexity_score,
        duration_s=outcome.dev_duration_s,
        timeout_killed=outcome.dev_timeout_killed,
        timeout_limit_s=outcome.dev_timeout_limit_s,
    )
    if outcome.preflight_model:
        pf_entry = _ensure_model(
            data,
            outcome.preflight_model,
            actual_model=outcome.preflight_actual_model,
            provider=outcome.preflight_provider,
            cli=outcome.preflight_cli,
        )
        _update_preflight(pf_entry, outcome.preflight_cost_usd)
    for name, (cycles, findings, cost) in outcome.reviewers.items():
        rev_entry = _ensure_model(data, name)
        _update_review(rev_entry, cycles, findings, cost)
    return data


# ── Backfill ──────────────────────────────────────────────────────────────


def backfill_from_history(history_path: Path) -> dict:
    """Aggregate ``assignment_history.yaml`` into a profiles dict.

    Only dev-role per-complexity success rates are derivable — the escalation
    history doesn't record iteration counts or cost, so those remain zero until
    fresh runs populate them.
    """
    data: dict = {"models": {}}
    if not history_path.exists():
        return data
    try:
        with open(history_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except Exception as exc:  # noqa: BLE001
        log.warning("[model_profiles] Failed to read history %s: %s", history_path, exc)
        return data
    if not isinstance(raw, dict):
        return data
    records = raw.get("escalations") or []
    if not isinstance(records, list):
        return data
    for r in records:
        if not isinstance(r, dict):
            continue
        model = str(r.get("dev_model") or "").strip()
        if not model:
            continue
        band = _normalize_band(str(r.get("complexity") or ""))
        outcome = str(r.get("outcome") or "").strip().upper()
        success = outcome == "DONE"
        entry = _ensure_model(data, model)
        _update_dev(entry, band, success, iterations=0, cost_usd=0.0)
    return data


# ── Convenience: load + update + save ─────────────────────────────────────


def update_from_run(
    profiles_path: Path,
    history_path: Path | None,
    outcome: RunOutcome,
) -> dict:
    """Load profiles (seeding from history on first run), apply, save."""
    if not profiles_path.exists() and history_path is not None:
        data = backfill_from_history(history_path)
    else:
        data = load_profiles(profiles_path)
    data = apply_run(data, outcome)
    save_profiles(profiles_path, data)
    return data


# ── Reader API for assignment ─────────────────────────────────────────────


def get_dev_success_rate(
    profiles: dict,
    model: str,
    complexity: str | None = None,
    min_runs: int = 3,
    *,
    actual_model: str | None = None,
    provider: str | None = None,
    cli: str | None = None,
) -> float | None:
    """Return dev success rate for (model, complexity) or None under min_runs."""
    matching = _matching_profile_entries(
        profiles,
        model,
        actual_model=actual_model,
        provider=provider,
        cli=cli,
    )
    if not matching:
        return None
    if complexity is None:
        runs = 0
        successes = 0.0
        for _, entry in matching:
            dev = entry.get("dev")
            if not isinstance(dev, dict):
                continue
            entry_runs = int(dev.get("runs", 0))
            if entry_runs <= 0:
                continue
            runs += entry_runs
            successes += _success_count(dev, entry_runs)
        return round(successes / runs, 4) if runs >= min_runs and runs > 0 else None
    band = _normalize_band(complexity)
    runs = 0
    successes = 0.0
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
        runs += entry_runs
        successes += _success_count(bc, entry_runs)
    return round(successes / runs, 4) if runs >= min_runs and runs > 0 else None


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
        "max_duration_s": round(max_duration_s, 4),
        "duration_runs": float(duration_runs),
        "max_killed_timeout_s": round(max_killed_timeout_s, 4),
    }


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


def summarize_profile_scope(
    data: dict | None,
    canonical_id: str,
    *,
    role: str | None = None,
    complexity: str | None = None,
) -> list[dict[str, Any]]:
    """Return the exact slices a reset would affect, with pre-reset counts."""
    models = (data or {}).get("models") or {}
    if not isinstance(models, dict):
        return []
    entry = models.get(canonical_id)
    if not isinstance(entry, dict):
        return []

    normalized_complexity = _normalize_band(complexity) if complexity is not None else None
    summaries: list[dict[str, Any]] = []
    if normalized_complexity is not None:
        roles = ("dev",)
    else:
        roles = (role,) if role else ROLES

    for current_role in roles:
        section = entry.get(current_role)
        if not isinstance(section, dict):
            if current_role == "dev" and normalized_complexity is not None:
                summaries.append(
                    {
                        "role": "dev",
                        "complexity": normalized_complexity,
                        "runs": 0,
                        "successes": 0,
                        "avg_iterations": 0.0,
                        "avg_cost_usd": 0.0,
                        "cost_unknown_runs": 0,
                    }
                )
            continue

        if current_role == "dev":
            if normalized_complexity is not None:
                bucket = (section.get("by_complexity") or {}).get(normalized_complexity) or {}
                if not isinstance(bucket, dict):
                    bucket = {}
                summaries.append(
                    {
                        "role": "dev",
                        "complexity": normalized_complexity,
                        "runs": int(bucket.get("runs", 0)),
                        "successes": int(bucket.get("_successes", 0)),
                        "avg_iterations": float(bucket.get("avg_iterations", 0.0)),
                        "avg_cost_usd": float(bucket.get("avg_cost_usd", 0.0)),
                        "cost_unknown_runs": int(bucket.get("_cost_unknown_runs", 0)),
                    }
                )
                continue
            summaries.append(
                {
                    "role": "dev",
                    "complexity": None,
                    "runs": int(section.get("runs", 0)),
                    "successes": int(section.get("_successes", 0)),
                    "avg_iterations": float(section.get("avg_iterations", 0.0)),
                    "avg_cost_usd": float(section.get("avg_cost_usd", 0.0)),
                    "cost_unknown_runs": int(section.get("_cost_unknown_runs", 0)),
                    "by_complexity": deepcopy(section.get("by_complexity") or {}),
                }
            )
            continue

        summary = {
            "role": current_role,
            "complexity": None,
            "runs": int(section.get("runs", 0)),
            "avg_cost_usd": float(section.get("avg_cost_usd", 0.0)),
            "cost_unknown_runs": int(section.get("_cost_unknown_runs", 0)),
        }
        if current_role == "review":
            summary["avg_findings"] = float(section.get("avg_findings", 0.0))
        summaries.append(summary)

    return summaries


def reset_profile_data(
    data: dict | None,
    canonical_id: str,
    *,
    role: str | None = None,
    complexity: str | None = None,
) -> tuple[dict, list[dict[str, Any]]]:
    """Pure: reset one canonical model's profile data, optionally scoped."""
    updated = deepcopy(data if isinstance(data, dict) else {"models": {}})
    models = updated.setdefault("models", {})
    if not isinstance(models, dict):
        updated["models"] = {}
        models = updated["models"]

    pre_reset = summarize_profile_scope(
        updated,
        canonical_id,
        role=role,
        complexity=complexity,
    )
    if canonical_id not in models or not isinstance(models.get(canonical_id), dict):
        return updated, pre_reset

    entry = models[canonical_id]
    normalized_complexity = _normalize_band(complexity) if complexity is not None else None

    if role == "dev" and normalized_complexity is not None:
        dev = entry.setdefault("dev", {})
        by = dev.setdefault("by_complexity", {})
        bucket = by.setdefault(normalized_complexity, {})
        _zero_dev_bucket(bucket)
        _recompute_dev_section(dev)
        return updated, pre_reset

    if normalized_complexity is not None:
        dev = entry.setdefault("dev", {})
        by = dev.setdefault("by_complexity", {})
        bucket = by.setdefault(normalized_complexity, {})
        _zero_dev_bucket(bucket)
        _recompute_dev_section(dev)
        return updated, pre_reset

    roles = (role,) if role else ROLES
    for current_role in roles:
        if current_role == "dev":
            dev = entry.setdefault("dev", {})
            by = dev.setdefault("by_complexity", {})
            for band in COMPLEXITY_BANDS:
                _zero_dev_bucket(by.setdefault(band, {}))
            _recompute_dev_section(dev)
        elif current_role == "review":
            _zero_review_section(entry.setdefault("review", {}))
        elif current_role == "preflight":
            _zero_preflight_section(entry.setdefault("preflight", {}))

    return updated, pre_reset


def record_profile_reset(
    *,
    profiles_path: Path,
    reset_history_path: Path,
    canonical_id: str,
    operator: str,
    role: str | None = None,
    complexity: str | None = None,
    reason: str | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Reset one canonical model profile slice and append an audit log entry."""
    data = load_profiles(profiles_path)
    updated, pre_reset = reset_profile_data(
        data,
        canonical_id,
        role=role,
        complexity=complexity,
    )
    save_profiles(profiles_path, updated)

    changed = any(int(summary.get("runs", 0)) > 0 for summary in pre_reset)
    entry = {
        "timestamp": timestamp
        or datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "operator": operator,
        "scope": {
            "canonical_id": canonical_id,
            "role": role,
            "complexity": _normalize_band(complexity) if complexity is not None else None,
        },
        "changed": changed,
        "pre_reset": pre_reset,
    }
    if reason:
        entry["reason"] = reason

    history = load_reset_history(reset_history_path)
    history.setdefault("resets", []).append(entry)
    save_reset_history(reset_history_path, history)
    return entry


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
        from theforge.config.models import AGENT_REGISTRY, resolve_agent_spec
    except Exception:  # noqa: BLE001
        return None
    candidates = [model_key]
    prefixes = {key.split("/", 1)[0] for key in AGENT_REGISTRY}
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


# ── Canonical-ID migration ────────────────────────────────────────────────


def canonical_id_for_legacy_key(model_key: str, entry: dict | None = None) -> str | None:
    """Return canonical ID (`provider/model/transport`) for a legacy storage key.

    Resolution order:
      1. ``_identity`` metadata stamped on the entry (richest, most reliable).
      2. Already-canonical key (idempotency: ``anthropic/sonnet/cli`` → itself).
      3. Direct AGENT_REGISTRY match (e.g. ``claude/sonnet`` registry slot).
      4. ``-cli``/``-api`` suffix-aware unique-spec lookup
         (e.g. ``sonnet-cli`` → unique anthropic/sonnet CLI spec).
      5. Bare-name unique-spec lookup with constructable transport
         (e.g. ``deepseek-deepseek-reasoner`` → unique deepseek/deepseek-reasoner
         API spec).

    Returns ``None`` when the key cannot be resolved unambiguously — those keys
    are reported as ambiguous by the migration tool and left under their legacy
    storage names rather than guessed at.
    """
    key = (model_key or "").strip()
    if not key:
        return None

    if isinstance(entry, dict):
        metadata = entry.get("_identity")
        if isinstance(metadata, dict):
            provider = str(metadata.get("provider") or "").strip()
            model = str(metadata.get("model") or "").strip()
            transport = str(metadata.get("transport") or "").strip()
            if not transport and metadata.get("cli"):
                transport = "cli"
            if provider and model and transport in ("cli", "api"):
                return f"{provider}/{model}/{transport}"

    # Already canonical?
    parts = key.split("/")
    if len(parts) == 3 and parts[2] in ("cli", "api"):
        return key

    spec = _resolve_agent_spec_for_profile_key(key)
    if spec is not None:
        return f"{spec.provider}/{spec.model}/{spec.transport.kind}"

    transport_hint: str | None = None
    base = key
    if key.endswith("-cli"):
        transport_hint = "cli"
        base = key[: -len("-cli")]
    elif key.endswith("-api"):
        transport_hint = "api"
        base = key[: -len("-api")]

    spec = _unique_registry_spec(base, transport_hint)
    if spec is not None:
        return f"{spec.provider}/{spec.model}/{spec.transport.kind}"

    if transport_hint is None:
        # Try `<provider>-<model>` style: split at the first dash where the
        # prefix matches a known provider, e.g. ``deepseek-deepseek-reasoner``
        # → provider=deepseek, model=deepseek-reasoner.
        try:
            from theforge.config.models import AGENT_REGISTRY  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            AGENT_REGISTRY = {}  # type: ignore[assignment]
        for spec_obj in AGENT_REGISTRY.values():
            prefix = f"{spec_obj.provider}-"
            if key.startswith(prefix):
                model_part = key[len(prefix) :]
                if model_part == spec_obj.model:
                    same_model = [
                        s
                        for s in AGENT_REGISTRY.values()
                        if s.provider == spec_obj.provider and s.model == spec_obj.model
                    ]
                    transports = {s.transport.kind for s in same_model}
                    if len(transports) == 1:
                        return f"{spec_obj.provider}/{spec_obj.model}/{spec_obj.transport.kind}"

    return None


def _unique_registry_spec(model_name: str, transport: str | None) -> Any | None:
    try:
        from theforge.config.models import AGENT_REGISTRY  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    matches = [
        spec
        for spec in AGENT_REGISTRY.values()
        if spec.model == model_name and (transport is None or spec.transport.kind == transport)
    ]
    # Unique by (provider, model, transport) — keep all distinct triples to
    # detect ambiguity (e.g. CLI + API for same model with no transport hint).
    triples = {(s.provider, s.model, s.transport.kind) for s in matches}
    if len(triples) == 1:
        return matches[0]
    return None


def _bucket_summary(entry: dict) -> dict[str, float | int]:
    """Headline numbers for an entry, used in the migration report."""
    runs = 0
    successes = 0.0
    cost = 0.0
    iterations = 0.0
    for role in ("dev", "review", "preflight"):
        sec = entry.get(role)
        if not isinstance(sec, dict):
            continue
        sec_runs = int(sec.get("runs", 0))
        runs += sec_runs
        if role == "dev":
            successes += _success_count(sec, sec_runs)
            cost += float(sec.get("_cost_sum", float(sec.get("avg_cost_usd", 0.0)) * sec_runs))
            iterations += float(
                sec.get("_iterations_sum", float(sec.get("avg_iterations", 0.0)) * sec_runs)
            )
        elif role == "review":
            cost += float(sec.get("_cost_sum", float(sec.get("avg_cost_usd", 0.0)) * sec_runs))
        elif role == "preflight":
            cost += float(sec.get("_cost_sum", float(sec.get("avg_cost_usd", 0.0)) * sec_runs))
    return {
        "runs": runs,
        "successes": successes,
        "cost_usd": round(cost, 6),
        "iterations": round(iterations, 4),
    }


def _merge_duration(target: dict, src: dict) -> None:
    """Combine the duration/kill accumulators of two dev buckets.

    Sums the completed-run accumulators, maxes the maxima, and recomputes
    ``avg_duration_s``. Legacy entries predating these fields lack the keys, so
    every read tolerates absence via ``.get(..., 0)``.
    """
    dur_sum = float(target.get("_duration_sum", 0.0)) + float(src.get("_duration_sum", 0.0))
    dur_runs = int(target.get("_duration_runs", 0)) + int(src.get("_duration_runs", 0))
    target["_duration_sum"] = dur_sum
    target["_duration_runs"] = dur_runs
    target["avg_duration_s"] = round(dur_sum / dur_runs, 4) if dur_runs > 0 else 0.0
    target["max_duration_s"] = max(
        float(target.get("max_duration_s", 0.0)), float(src.get("max_duration_s", 0.0))
    )
    target["max_killed_timeout_s"] = max(
        float(target.get("max_killed_timeout_s", 0.0)),
        float(src.get("max_killed_timeout_s", 0.0)),
    )


def _merge_dev(target: dict, src: dict) -> None:
    runs = int(target.get("runs", 0)) + int(src.get("runs", 0))
    successes = int(target.get("_successes", 0)) + int(src.get("_successes", 0))
    iter_sum = float(target.get("_iterations_sum", 0.0)) + float(src.get("_iterations_sum", 0.0))
    cost_sum = float(target.get("_cost_sum", 0.0)) + float(src.get("_cost_sum", 0.0))
    # If accumulators absent on src, derive from runs * avg fields.
    if "_successes" not in src and "runs" in src:
        successes = int(target.get("_successes", 0)) + int(
            round(float(src.get("success_rate", 0.0)) * int(src.get("runs", 0)))
        )
    if "_iterations_sum" not in src and "runs" in src:
        iter_sum = float(target.get("_iterations_sum", 0.0)) + float(
            src.get("avg_iterations", 0.0)
        ) * int(src.get("runs", 0))
    if "_cost_sum" not in src and "runs" in src:
        # Legacy reconstruction: this ``avg_cost_usd * runs`` fallback assumes
        # ``avg_cost_usd`` was computed over TOTAL runs. Since #1616 the average
        # is over MEASURED runs only (``runs - _cost_unknown_runs``), so this
        # reconstruction is only exact when ``_cost_unknown_runs == 0`` — i.e.
        # for pre-#1616 profiles, which never recorded unmeasured runs. Do not
        # rely on it when an old entry both omits ``_cost_sum`` and carries
        # unmeasured runs (a combination that cannot occur in practice, since
        # ``_cost_sum`` has always been persisted alongside ``avg_cost_usd``).
        cost_sum = float(target.get("_cost_sum", 0.0)) + float(src.get("avg_cost_usd", 0.0)) * int(
            src.get("runs", 0)
        )

    # Same measured-vs-total caveat as above applies to the by_complexity /
    # by_complexity_score and review/preflight ``avg_cost_usd * runs`` fallbacks.
    cost_unknown = int(target.get("_cost_unknown_runs", 0)) + int(src.get("_cost_unknown_runs", 0))
    target["runs"] = runs
    target["_successes"] = successes
    target["_iterations_sum"] = iter_sum
    target["_cost_sum"] = cost_sum
    target["_cost_unknown_runs"] = cost_unknown
    if runs > 0:
        target["success_rate"] = round(successes / runs, 4)
        target["avg_iterations"] = round(iter_sum / runs, 4)
        measured = runs - cost_unknown
        target["avg_cost_usd"] = round(cost_sum / measured, 6) if measured > 0 else 0.0
    _merge_duration(target, src)

    src_by = src.get("by_complexity") or {}
    if src_by:
        target_by = target.setdefault("by_complexity", {})
        for band, bc_src in src_by.items():
            if not isinstance(bc_src, dict):
                continue
            bc_target = target_by.setdefault(band, {})
            bc_runs = int(bc_target.get("runs", 0)) + int(bc_src.get("runs", 0))
            bc_succ = int(bc_target.get("_successes", 0)) + int(
                bc_src.get(
                    "_successes",
                    round(float(bc_src.get("success_rate", 0.0)) * int(bc_src.get("runs", 0))),
                )
            )
            bc_iter = float(bc_target.get("_iterations_sum", 0.0)) + float(
                bc_src.get(
                    "_iterations_sum",
                    float(bc_src.get("avg_iterations", 0.0)) * int(bc_src.get("runs", 0)),
                )
            )
            bc_cost = float(bc_target.get("_cost_sum", 0.0)) + float(
                bc_src.get(
                    "_cost_sum",
                    float(bc_src.get("avg_cost_usd", 0.0)) * int(bc_src.get("runs", 0)),
                )
            )
            bc_unknown = int(bc_target.get("_cost_unknown_runs", 0)) + int(
                bc_src.get("_cost_unknown_runs", 0)
            )
            bc_target["runs"] = bc_runs
            bc_target["_successes"] = bc_succ
            bc_target["_iterations_sum"] = bc_iter
            bc_target["_cost_sum"] = bc_cost
            bc_target["_cost_unknown_runs"] = bc_unknown
            if bc_runs > 0:
                bc_target["success_rate"] = round(bc_succ / bc_runs, 4)
                bc_target["avg_iterations"] = round(bc_iter / bc_runs, 4)
                bc_measured = bc_runs - bc_unknown
                bc_target["avg_cost_usd"] = (
                    round(bc_cost / bc_measured, 6) if bc_measured > 0 else 0.0
                )
            _merge_duration(bc_target, bc_src)

    src_by_score = src.get("by_complexity_score") or {}
    if src_by_score:
        target_by_score = target.setdefault("by_complexity_score", {})
        for score_key, sc_src in src_by_score.items():
            if not isinstance(sc_src, dict):
                continue
            sc_target = target_by_score.setdefault(score_key, {})
            sc_runs = int(sc_target.get("runs", 0)) + int(sc_src.get("runs", 0))
            sc_succ = int(sc_target.get("_successes", 0)) + int(
                sc_src.get(
                    "_successes",
                    round(float(sc_src.get("success_rate", 0.0)) * int(sc_src.get("runs", 0))),
                )
            )
            sc_iter = float(sc_target.get("_iterations_sum", 0.0)) + float(
                sc_src.get(
                    "_iterations_sum",
                    float(sc_src.get("avg_iterations", 0.0)) * int(sc_src.get("runs", 0)),
                )
            )
            sc_cost = float(sc_target.get("_cost_sum", 0.0)) + float(
                sc_src.get(
                    "_cost_sum",
                    float(sc_src.get("avg_cost_usd", 0.0)) * int(sc_src.get("runs", 0)),
                )
            )
            sc_unknown = int(sc_target.get("_cost_unknown_runs", 0)) + int(
                sc_src.get("_cost_unknown_runs", 0)
            )
            sc_target["runs"] = sc_runs
            sc_target["_successes"] = sc_succ
            sc_target["_iterations_sum"] = sc_iter
            sc_target["_cost_sum"] = sc_cost
            sc_target["_cost_unknown_runs"] = sc_unknown
            if sc_runs > 0:
                sc_target["success_rate"] = round(sc_succ / sc_runs, 4)
                sc_target["avg_iterations"] = round(sc_iter / sc_runs, 4)
                sc_measured = sc_runs - sc_unknown
                sc_target["avg_cost_usd"] = (
                    round(sc_cost / sc_measured, 6) if sc_measured > 0 else 0.0
                )
            _merge_duration(sc_target, sc_src)


def _merge_review(target: dict, src: dict) -> None:
    runs = int(target.get("runs", 0)) + int(src.get("runs", 0))
    find_sum = float(target.get("_findings_sum", 0.0)) + float(
        src.get("_findings_sum", float(src.get("avg_findings", 0.0)) * int(src.get("runs", 0)))
    )
    cost_sum = float(target.get("_cost_sum", 0.0)) + float(
        src.get("_cost_sum", float(src.get("avg_cost_usd", 0.0)) * int(src.get("runs", 0)))
    )
    cost_unknown = int(target.get("_cost_unknown_runs", 0)) + int(src.get("_cost_unknown_runs", 0))
    target["runs"] = runs
    target["_findings_sum"] = find_sum
    target["_cost_sum"] = cost_sum
    target["_cost_unknown_runs"] = cost_unknown
    if runs > 0:
        target["avg_findings"] = round(find_sum / runs, 4)
        measured = runs - cost_unknown
        target["avg_cost_usd"] = round(cost_sum / measured, 6) if measured > 0 else 0.0


def _merge_preflight(target: dict, src: dict) -> None:
    runs = int(target.get("runs", 0)) + int(src.get("runs", 0))
    cost_sum = float(target.get("_cost_sum", 0.0)) + float(
        src.get("_cost_sum", float(src.get("avg_cost_usd", 0.0)) * int(src.get("runs", 0)))
    )
    cost_unknown = int(target.get("_cost_unknown_runs", 0)) + int(src.get("_cost_unknown_runs", 0))
    target["runs"] = runs
    target["_cost_sum"] = cost_sum
    target["_cost_unknown_runs"] = cost_unknown
    if runs > 0:
        measured = runs - cost_unknown
        target["avg_cost_usd"] = round(cost_sum / measured, 6) if measured > 0 else 0.0


def _merge_entry(target: dict, src: dict) -> None:
    if not isinstance(src, dict):
        return
    for role, merger in (
        ("dev", _merge_dev),
        ("review", _merge_review),
        ("preflight", _merge_preflight),
    ):
        sec = src.get(role)
        if not isinstance(sec, dict):
            continue
        target_sec = target.setdefault(role, {})
        merger(target_sec, sec)
    src_id = src.get("_identity")
    if isinstance(src_id, dict) and not isinstance(target.get("_identity"), dict):
        target["_identity"] = dict(src_id)


def migrate_profiles_data(
    data: dict | None,
) -> tuple[dict, list[dict]]:
    """Pure: rewrite ``data`` so legacy alias entries merge under canonical IDs.

    Idempotent. Re-running on already-migrated data returns the same dict.
    Ambiguous keys (those with no unambiguous resolution) are left in place and
    flagged in the report. The report is a list of dicts — each entry is either
    a merge record (``canonical_id``, ``merged_from``, ``combined``) or an
    ambiguous-skip record (``ambiguous_key``, ``reason``).
    """
    models = (data or {}).get("models") or {}
    if not isinstance(models, dict):
        models = {}

    groups: dict[str, list[tuple[str, dict]]] = {}
    ambiguous: list[tuple[str, dict]] = []

    for key, entry in models.items():
        if not isinstance(entry, dict):
            continue
        canonical = canonical_id_for_legacy_key(key, entry)
        if canonical is None:
            ambiguous.append((key, entry))
        else:
            groups.setdefault(canonical, []).append((key, entry))

    new_models: dict[str, dict] = {}
    report: list[dict] = []

    for canonical, members in groups.items():
        if len(members) == 1 and members[0][0] == canonical:
            # Already canonical with no aliases to merge.
            new_models[canonical] = members[0][1]
            continue
        merged: dict = {}
        # Stamp identity early so later metadata wins are skipped.
        provider, model, transport = canonical.split("/", 2)
        merged["_identity"] = {
            "provider": provider,
            "model": model,
            "transport": transport,
        }
        if transport == "cli":
            cli_runner = _provider_to_cli_runner(provider)
            if cli_runner:
                merged["_identity"]["cli"] = cli_runner
        for _, entry in members:
            _merge_entry(merged, entry)
        new_models[canonical] = merged
        report.append(
            {
                "canonical_id": canonical,
                "merged_from": [{"key": k, **_bucket_summary(e)} for k, e in members],
                "combined": _bucket_summary(merged),
            }
        )

    for key, entry in ambiguous:
        new_models[key] = entry
        report.append(
            {
                "ambiguous_key": key,
                "reason": "could not resolve to a unique canonical identity",
            }
        )

    out = dict(data or {})
    out["models"] = new_models
    return out, report


def _provider_to_cli_runner(provider: str) -> str | None:
    return {"anthropic": "claude", "openai": "codex", "google": "gemini"}.get(provider)


def migrate_history_data(
    data: dict | None,
) -> tuple[dict, list[dict]]:
    """Pure: canonicalize ``dev_model`` field on each escalation record.

    For records whose ``dev_model`` resolves to a canonical ID, the field is
    overwritten in place. Records that cannot be resolved are left unchanged
    and reported. Idempotent.
    """
    if not isinstance(data, dict):
        return {"escalations": []}, []
    records = data.get("escalations") or []
    if not isinstance(records, list):
        return {"escalations": []}, []

    new_records: list[dict] = []
    report: list[dict] = []
    for r in records:
        if not isinstance(r, dict):
            continue
        new_r = dict(r)
        legacy = str(r.get("dev_model") or "").strip()
        canonical = canonical_id_for_legacy_key(legacy) if legacy else None
        if canonical and canonical != legacy:
            new_r["dev_model"] = canonical
            report.append(
                {
                    "from": legacy,
                    "to": canonical,
                    "story": r.get("story"),
                }
            )
        elif legacy and canonical is None:
            report.append({"ambiguous_key": legacy, "story": r.get("story")})
        new_records.append(new_r)

    out = dict(data)
    out["escalations"] = new_records
    return out, report


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
