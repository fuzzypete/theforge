"""Sprint carry-accounting helpers shared by query, CLI, and runner paths."""

from __future__ import annotations

import datetime
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from . import unmeasured as unmeasured_spend_policy
from .budget import budget_verification_spend

if TYPE_CHECKING:
    from .unmeasured import AcceptedUnmeasuredSpend


@dataclass(frozen=True)
class SprintCarryBudgetSnapshot:
    """Budget-relevant spend already attached to a logical sprint before dispatch."""

    sprint_id: str | None
    carried_cost_usd: float
    unresolved_unmeasured_sources: tuple[str, ...]
    accepted_unmeasured_spend: tuple["AcceptedUnmeasuredSpend", ...]
    accepted_unmeasured_ceiling_usd: float
    verification_spend_usd: float

    @property
    def headroom_is_lower_bound(self) -> bool:
        return bool(self.unresolved_unmeasured_sources)

    def remaining_headroom_usd(self, budget_usd: float) -> float:
        return round(float(budget_usd) - self.verification_spend_usd, 4)

    def floored_at(self, carried_cost_usd: float) -> "SprintCarryBudgetSnapshot":
        """This snapshot with its carried spend raised to at least *carried_cost_usd*.

        The records this snapshot is assembled from can under-report: a
        generation that lost its accumulated rows across a re-exec leaves them
        summing to less than the run actually spent. Where a stronger account of
        the same money exists — the spend the run persisted to its own live
        state — the disclosure and the startup refusal must be made against that
        one, or a run is admitted under a ceiling it has already passed (#2922).

        Never lowers: a smaller floor is not evidence of less spend.
        """
        if carried_cost_usd <= self.carried_cost_usd:
            return self
        return replace(
            self,
            carried_cost_usd=float(carried_cost_usd),
            verification_spend_usd=budget_verification_spend(
                accumulated_cost=0.0,
                prior_cost=float(carried_cost_usd),
                accepted_unmeasured_ceiling_usd=self.accepted_unmeasured_ceiling_usd,
            ),
        )


def _existing_sprint_id(sprint_name: str, project_root: Path) -> str | None:
    """Return the stable sprint id when one already exists, else ``None``."""
    sprint_id_path = project_root / ".forge" / "logs" / sprint_name / ".sprint_id"
    try:
        if sprint_id_path.exists():
            sprint_id = sprint_id_path.read_text(encoding="utf-8").strip()
            return sprint_id or None
    except OSError:
        return None
    return None


def sprint_invocation_carries_prior_spend(*, resume: bool, reexec: bool) -> bool:
    """Whether this invocation should inherit prior same-sprint spend."""
    return bool(resume or reexec)


def previous_run_marker_present(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the process is continuing from an earlier detached sprint run."""
    env = os.environ if environ is None else environ
    return bool(env.get("FORGE_PREV_RUN_ID"))


def _prior_sprint_block(project_root: Path, sprint_id: str | None) -> dict:
    """Return this sprint's block from sprint-audit.yaml, or ``{}``."""
    if not sprint_id:
        return {}
    audit_path = project_root / ".forge" / "audits" / "sprint-audit.yaml"
    if not audit_path.exists():
        return {}
    try:
        import yaml  # noqa: PLC0415

        with open(audit_path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        sprint_block = data.get("sprint", {})
        if not isinstance(sprint_block, dict):
            return {}
        if sprint_block.get("sprint_id") != sprint_id:
            return {}
        return sprint_block
    except (OSError, yaml.YAMLError, AttributeError):
        return {}


def prior_unmeasured_spend_sources(project_root: Path, sprint_id: str | None) -> list[str]:
    """The sources the prior generation recorded as unmeasured, if any."""
    recorded = _prior_sprint_block(project_root, sprint_id).get("unmeasured_spend_sources") or []
    return [str(source) for source in recorded if source] if isinstance(recorded, list) else []


def prior_sprint_cost_incomplete(
    project_root: Path,
    sprint_id: str | None,
    accepted: Mapping[str, "AcceptedUnmeasuredSpend"] | None = None,
) -> bool:
    """Return True when the prior generation recorded an incomplete sprint cost."""
    sprint_block = _prior_sprint_block(project_root, sprint_id)
    if sprint_block.get("cost_complete") is not False:
        return False
    if accepted:
        recorded = sprint_block.get("unmeasured_spend_sources") or []
        if isinstance(recorded, list) and unmeasured_spend_policy.all_sources_accepted(
            [str(source) for source in recorded if source], accepted
        ):
            return False
    return True


def _parse_accumulated_story_timestamp(value: object) -> datetime.datetime | None:
    """Parse timestamps persisted in accumulated sprint story state."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_prior_sprint_accounting(
    project_root: Path,
    sprint_id: str | None,
) -> tuple[float, datetime.datetime | None, dict[str, dict]]:
    """Recover prior same-sprint cost/timing from progressive story state."""
    if not sprint_id:
        return 0.0, None, {}

    from .audit import _load_accumulated_stories  # noqa: PLC0415

    recovered_entries: dict[str, dict] = {}
    recovered_cost = 0.0
    earliest_started_at: datetime.datetime | None = None
    for raw_entry in _load_accumulated_stories(sprint_id, project_root):
        if not isinstance(raw_entry, dict):
            continue
        canonical_ref = raw_entry.get("canonical_ref")
        if not isinstance(canonical_ref, str) or not canonical_ref:
            continue
        entry = dict(raw_entry)
        recovered_entries[canonical_ref] = entry
        try:
            recovered_cost += float(entry.get("cost_usd", 0.0) or 0.0)
        except (TypeError, ValueError):
            pass
        started_at = _parse_accumulated_story_timestamp(entry.get("started_at"))
        if started_at is not None and (
            earliest_started_at is None or started_at < earliest_started_at
        ):
            earliest_started_at = started_at

    return round(recovered_cost, 4), earliest_started_at, recovered_entries


def read_prior_sprint_audit_cost(project_root: Path, sprint_id: str | None) -> float:
    """Read carry-forward cost for a same-sprint resume/re-exec from sprint-audit.yaml."""
    sprint_block = _prior_sprint_block(project_root, sprint_id)
    total = sprint_block.get("total_cost_usd")
    if total is None:
        total = sprint_block.get("total_cost_measured_usd", 0.0)
    try:
        return float(total or 0.0)
    except (TypeError, ValueError):
        return 0.0


def load_sprint_carry_budget_snapshot(
    *,
    project_root: Path,
    sprint_name: str,
    sprint_id: str | None = None,
    resume: bool = False,
    reexec: bool = False,
    accepted_unmeasured: Mapping[str, "AcceptedUnmeasuredSpend"] | None = None,
    has_previous_run_marker: bool | None = None,
) -> SprintCarryBudgetSnapshot:
    """Return the carried budget state the next dispatch will inherit."""
    resolved_sprint_id = sprint_id or _existing_sprint_id(sprint_name, project_root)
    carries_prior_spend = sprint_invocation_carries_prior_spend(resume=resume, reexec=reexec)
    if has_previous_run_marker is None:
        has_previous_run_marker = previous_run_marker_present()
    if not resolved_sprint_id:
        return SprintCarryBudgetSnapshot(
            sprint_id=None,
            carried_cost_usd=0.0,
            unresolved_unmeasured_sources=(),
            accepted_unmeasured_spend=(),
            accepted_unmeasured_ceiling_usd=0.0,
            verification_spend_usd=0.0,
        )

    recovered_cost_usd, _started_at, entries_by_ref = read_prior_sprint_accounting(
        project_root, resolved_sprint_id
    )
    carried_cost_usd = 0.0
    if carries_prior_spend:
        if entries_by_ref:
            carried_cost_usd = recovered_cost_usd
        elif has_previous_run_marker:
            carried_cost_usd = read_prior_sprint_audit_cost(project_root, resolved_sprint_id)

    occurrence_ids: dict[str, str | None] = {}
    raw_unmeasured_sources: list[str] = []
    for entry in entries_by_ref.values():
        slug = entry.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        if "cost_usd" in entry and entry.get("cost_usd") is None:
            raw_source = f"carried:{slug}"
            raw_unmeasured_sources.append(raw_source)
            origin = unmeasured_spend_policy.build_source(
                raw_source,
                unmeasured_spend_policy.read_story_audit(project_root, sprint_name, slug),
            ).origin
            occurrence_ids[raw_source] = origin.get("run_id")

    if accepted_unmeasured is not None:
        accepted_index = dict(accepted_unmeasured)
    else:
        from .audit import _load_accepted_unmeasured_spend  # noqa: PLC0415

        accepted_index = unmeasured_spend_policy.accepted_by_source(
            _load_accepted_unmeasured_spend(resolved_sprint_id, project_root)
        )
    if carries_prior_spend and prior_sprint_cost_incomplete(
        project_root, resolved_sprint_id, accepted_index
    ):
        raw_unmeasured_sources.append("carried:prior-generation")
    unresolved, applied = unmeasured_spend_policy.partition(
        raw_unmeasured_sources,
        accepted_index,
        current_generation=set(),
        occurrence_ids=occurrence_ids,
    )
    accepted_ceiling_usd = unmeasured_spend_policy.accepted_ceiling_total(applied)
    verification_spend_usd = budget_verification_spend(
        accumulated_cost=0.0,
        prior_cost=carried_cost_usd,
        accepted_unmeasured_ceiling_usd=accepted_ceiling_usd,
    )
    return SprintCarryBudgetSnapshot(
        sprint_id=resolved_sprint_id,
        carried_cost_usd=carried_cost_usd,
        unresolved_unmeasured_sources=tuple(unresolved),
        accepted_unmeasured_spend=tuple(applied),
        accepted_unmeasured_ceiling_usd=accepted_ceiling_usd,
        verification_spend_usd=verification_spend_usd,
    )
