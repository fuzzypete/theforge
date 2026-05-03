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
``_findings_sum``) are the running accumulators used to update the derived
fields in place. They are part of the persisted schema.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
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
    dev_cost_usd: float
    preflight_model: str | None = None
    dev_actual_model: str | None = None
    dev_provider: str | None = None
    dev_cli: str | None = None
    preflight_actual_model: str | None = None
    preflight_provider: str | None = None
    preflight_cli: str | None = None
    preflight_cost_usd: float = 0.0
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


def _ensure_model(
    data: dict,
    name: str,
    *,
    actual_model: str | None = None,
    provider: str | None = None,
    cli: str | None = None,
) -> dict:
    models = data.setdefault("models", {})
    entry = models.setdefault(name, {})
    metadata = _identity_metadata(actual_model=actual_model, provider=provider, cli=cli)
    if metadata and not isinstance(entry.get("_identity"), dict):
        entry["_identity"] = metadata
    return entry


def _update_dev(
    entry: dict,
    complexity: str,
    success: bool,
    iterations: int,
    cost_usd: float,
) -> None:
    dev = entry.setdefault("dev", {})
    runs = int(dev.get("runs", 0)) + 1
    successes = int(dev.get("_successes", 0)) + (1 if success else 0)
    iter_sum = float(dev.get("_iterations_sum", 0.0)) + float(iterations)
    cost_sum = float(dev.get("_cost_sum", 0.0)) + float(cost_usd)
    dev["runs"] = runs
    dev["_successes"] = successes
    dev["_iterations_sum"] = iter_sum
    dev["_cost_sum"] = cost_sum
    dev["success_rate"] = round(successes / runs, 4)
    dev["avg_iterations"] = round(iter_sum / runs, 4)
    dev["avg_cost_usd"] = round(cost_sum / runs, 6)

    by = dev.setdefault("by_complexity", {})
    bc = by.setdefault(complexity, {})
    bc_runs = int(bc.get("runs", 0)) + 1
    bc_successes = int(bc.get("_successes", 0)) + (1 if success else 0)
    bc_iter_sum = float(bc.get("_iterations_sum", 0.0)) + float(iterations)
    bc_cost_sum = float(bc.get("_cost_sum", 0.0)) + float(cost_usd)
    bc["runs"] = bc_runs
    bc["_successes"] = bc_successes
    bc["_iterations_sum"] = bc_iter_sum
    bc["_cost_sum"] = bc_cost_sum
    bc["success_rate"] = round(bc_successes / bc_runs, 4)
    bc["avg_iterations"] = round(bc_iter_sum / bc_runs, 4)
    bc["avg_cost_usd"] = round(bc_cost_sum / bc_runs, 6)


def _update_review(entry: dict, cycles: int, findings: int, cost_usd: float) -> None:
    if cycles <= 0:
        return
    rev = entry.setdefault("review", {})
    runs = int(rev.get("runs", 0)) + cycles
    find_sum = float(rev.get("_findings_sum", 0.0)) + float(findings)
    cost_sum = float(rev.get("_cost_sum", 0.0)) + float(cost_usd)
    rev["runs"] = runs
    rev["_findings_sum"] = find_sum
    rev["_cost_sum"] = cost_sum
    rev["avg_findings"] = round(find_sum / runs, 4)
    rev["avg_cost_usd"] = round(cost_sum / runs, 6)


def _update_preflight(entry: dict, cost_usd: float) -> None:
    pf = entry.setdefault("preflight", {})
    runs = int(pf.get("runs", 0)) + 1
    cost_sum = float(pf.get("_cost_sum", 0.0)) + float(cost_usd)
    pf["runs"] = runs
    pf["_cost_sum"] = cost_sum
    pf["avg_cost_usd"] = round(cost_sum / runs, 6)


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
    iterations_sum = 0.0
    cost_sum = 0.0
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
        iterations_sum += entry_iterations
        cost_sum += entry_cost
    if runs < min_runs or runs <= 0:
        return None
    return {
        "runs": float(runs),
        "avg_iterations": round(iterations_sum / runs, 4),
        "avg_cost_usd": round(cost_sum / runs, 6),
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
