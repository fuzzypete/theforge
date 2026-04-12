"""Sprint status reader — parse live state files and completed sprint summaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class StoryStatusEntry:
    """Per-story display data for forge sprint-status."""

    slug: str
    path: str
    status: str  # "done" | "running" | "waiting" | "blocked" | "failed" | "skipped"
    phase: str | None
    cost_usd: float
    blocked_by: list[str] = field(default_factory=list)
    bundle_candidate: bool = False


def find_sprint_summary(run_id: str, project_root: Path) -> Path | None:
    """Scan .forge/logs/*/sprint-summary.yaml for the file containing run_id.

    Returns the Path to the matching sprint-summary.yaml, or None if not found.
    """
    logs_dir = project_root / ".forge" / "logs"
    if not logs_dir.exists():
        return None
    try:
        sprint_dirs = sorted(d for d in logs_dir.iterdir() if d.is_dir())
    except OSError:
        return None
    for sprint_dir in sprint_dirs:
        summary_path = sprint_dir / "sprint-summary.yaml"
        if not summary_path.exists():
            continue
        try:
            with open(summary_path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict):
                sprint_info = data.get("sprint", {})
                if isinstance(sprint_info, dict) and sprint_info.get("run_id") == run_id:
                    return summary_path
        except Exception:
            continue
    return None


def _outcome_to_status(outcome: str) -> str:
    """Map a sprint-summary ``outcome`` value to a display status string."""
    if outcome in ("ALREADY_DONE", "DONE"):
        return "done"
    if outcome == "SKIPPED":
        return "skipped"
    if outcome == "ESCALATE":
        return "failed"
    # Any other phase name (INIT, WORKSPACE, DEV, …) means the run stopped mid-phase
    return "failed"


def read_completed_status(summary_path: Path) -> list[StoryStatusEntry]:
    """Parse a sprint-summary.yaml and return per-story status entries.

    Enriches each entry with ``bundle_candidate`` read from the per-story
    coordinator audit at ``<sprint-log-dir>/<slug>/audit.yaml``.
    """
    try:
        with open(summary_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception:
        return []

    if not isinstance(data, dict):
        return []

    sprint_log_dir = summary_path.parent
    stories_data = data.get("stories", [])

    entries = []
    for story in stories_data:
        if not isinstance(story, dict):
            continue
        slug = story.get("slug", "")
        path = story.get("path", slug)
        outcome = story.get("outcome", "SKIPPED")
        cost_usd = float(story.get("cost_usd", 0.0))

        status = _outcome_to_status(outcome)
        # For done/skipped outcomes the phase label isn't useful in the display
        phase = outcome if status in ("running", "failed") else None

        # Read bundle_candidate from the per-story coordinator audit
        bundle_candidate = False
        if slug:
            audit_path = sprint_log_dir / slug / "audit.yaml"
            if audit_path.exists():
                try:
                    with open(audit_path, encoding="utf-8") as f:
                        audit_data = yaml.safe_load(f)
                    if isinstance(audit_data, dict):
                        preflight = audit_data.get("preflight")
                        if isinstance(preflight, dict):
                            bundle_candidate = bool(preflight.get("bundle_candidate", False))
                except Exception:
                    pass

        # depends_on is persisted in the summary since the state writer records it;
        # show it as blocked_by for skipped stories so the operator can see why.
        raw_depends_on = story.get("depends_on") or []
        blocked_by = list(raw_depends_on) if status == "skipped" and raw_depends_on else []

        entries.append(
            StoryStatusEntry(
                slug=slug,
                path=path,
                status=status,
                phase=phase,
                cost_usd=cost_usd,
                blocked_by=blocked_by,
                bundle_candidate=bundle_candidate,
            )
        )

    return entries


def read_live_status(run_id: str, project_root: Path) -> list[StoryStatusEntry] | None:
    """Read .forge/runs/<run-id>.state for live sprint status.

    Returns a list of ``StoryStatusEntry`` objects, or ``None`` if the state
    file does not exist (sprint not yet started or state file missing).
    """
    state_path = project_root / ".forge" / "runs" / f"{run_id}.state"
    if not state_path.exists():
        return None
    try:
        with open(state_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    stories_data = data.get("stories", [])
    entries = []
    for story in stories_data:
        if not isinstance(story, dict):
            continue
        entries.append(
            StoryStatusEntry(
                slug=story.get("slug", ""),
                path=story.get("path", story.get("slug", "")),
                status=story.get("status", "waiting"),
                phase=story.get("phase"),
                cost_usd=float(story.get("cost_usd", 0.0)),
                blocked_by=list(story.get("blocked_by") or []),
                bundle_candidate=bool(story.get("bundle_candidate", False)),
            )
        )
    return entries
