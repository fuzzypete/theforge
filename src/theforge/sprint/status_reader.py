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
    elapsed_seconds: float | None = None
    detail: str = ""


def _follow_redirect_chain(run_id: str, project_root: Path, max_hops: int = 20) -> str:
    """Follow .forge/runs/<run_id>.redirect files to find the terminal run_id.

    Each redirect file contains JSON with a ``new_run_id`` key written when the
    daemon hands off to a new worker process.  Returns the original run_id if no
    redirect chain exists.
    """
    import json  # noqa: PLC0415

    current = run_id
    runs_dir = project_root / ".forge" / "runs"
    for _ in range(max_hops):
        redirect_file = runs_dir / f"{current}.redirect"
        if not redirect_file.exists():
            break
        try:
            data = json.loads(redirect_file.read_text(encoding="utf-8"))
            new_id = data.get("new_run_id", "")
            if not new_id or new_id == current:
                break
            current = new_id
        except Exception:
            break
    return current


def find_sprint_summary(run_id: str, project_root: Path) -> Path | None:
    """Scan .forge/logs/*/sprint-summary.yaml for the file containing run_id.

    After a run_id rollover the summary is written under the terminal run_id.
    Follows the redirect chain so earlier run_ids resolve to the same summary.

    Returns the Path to the matching sprint-summary.yaml, or None if not found.
    """
    terminal_run_id = _follow_redirect_chain(run_id, project_root)
    candidate_ids = {run_id, terminal_run_id}

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
                if isinstance(sprint_info, dict) and sprint_info.get("run_id") in candidate_ids:
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

        # Derive a short detail line from the completed-sprint outcome.
        if outcome == "ESCALATE":
            detail = "ESCALATE"
        elif outcome == "ALREADY_DONE":
            detail = "already done"
        elif outcome == "DONE":
            detail = "APPROVE"
        elif status == "skipped" and raw_depends_on:
            detail = f"waiting on: {', '.join(raw_depends_on)}"
        else:
            detail = ""

        entries.append(
            StoryStatusEntry(
                slug=slug,
                path=path,
                status=status,
                phase=phase,
                cost_usd=cost_usd,
                blocked_by=blocked_by,
                bundle_candidate=bundle_candidate,
                elapsed_seconds=None,  # not tracked in sprint summary
                detail=detail,
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
        status_val = story.get("status", "waiting")
        phase_val = story.get("phase")
        blocked_by_val = list(story.get("blocked_by") or [])

        # Derive a short detail line from available live-state data.
        if blocked_by_val and status_val == "blocked":
            detail = f"waiting on: {', '.join(blocked_by_val)}"
        elif status_val == "failed" and phase_val:
            detail = f"failed in: {phase_val}"
        else:
            detail = ""

        entries.append(
            StoryStatusEntry(
                slug=story.get("slug", ""),
                path=story.get("path", story.get("slug", "")),
                status=status_val,
                phase=phase_val,
                cost_usd=float(story.get("cost_usd", 0.0)),
                blocked_by=blocked_by_val,
                bundle_candidate=bool(story.get("bundle_candidate", False)),
                elapsed_seconds=None,  # not tracked in live state file
                detail=detail,
            )
        )
    return entries
