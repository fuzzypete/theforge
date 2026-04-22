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
            small|medium|large: {runs, success_rate}
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


def _ensure_model(data: dict, name: str) -> dict:
    models = data.setdefault("models", {})
    return models.setdefault(name, {})


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
    bc["runs"] = bc_runs
    bc["_successes"] = bc_successes
    bc["success_rate"] = round(bc_successes / bc_runs, 4)


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
    dev_entry = _ensure_model(data, outcome.dev_model)
    _update_dev(
        dev_entry,
        band,
        outcome.dev_success,
        outcome.dev_iterations,
        outcome.dev_cost_usd,
    )
    if outcome.preflight_model:
        pf_entry = _ensure_model(data, outcome.preflight_model)
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
) -> float | None:
    """Return dev success rate for (model, complexity) or None under min_runs."""
    models = (profiles or {}).get("models") or {}
    entry = models.get(model)
    if not isinstance(entry, dict):
        return None
    dev = entry.get("dev")
    if not isinstance(dev, dict):
        return None
    if complexity is None:
        runs = int(dev.get("runs", 0))
        return float(dev.get("success_rate", 0.0)) if runs >= min_runs else None
    band = _normalize_band(complexity)
    bc = (dev.get("by_complexity") or {}).get(band)
    if not isinstance(bc, dict):
        return None
    runs = int(bc.get("runs", 0))
    return float(bc.get("success_rate", 0.0)) if runs >= min_runs else None
