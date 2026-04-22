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
    complexity: str | None = None


def _follow_redirect_chain(run_id: str, project_root: Path, max_hops: int = 20) -> str:
    """Follow .forge/runs/<run_id>.redirect files to find the terminal run_id.

    Each redirect file contains JSON with a ``new_run_id`` key written when the
    daemon hands off to a new worker process. Returns the original run_id if no
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

    Also matches summaries whose sprint-level accumulated state records the
    queried run_id in any story's ``preflight_source_run_id``. This covers
    mid-run sprint re-execs where the final summary is written under the last
    worker run_id but earlier worker run_ids still need to resolve to the same
    logical sprint summary.

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
            if not isinstance(data, dict):
                continue
            sprint_info = data.get("sprint", {})
            if isinstance(sprint_info, dict) and sprint_info.get("run_id") in candidate_ids:
                return summary_path
            stories = data.get("stories", [])
            if isinstance(stories, list):
                for story in stories:
                    if not isinstance(story, dict):
                        continue
                    if story.get("preflight_source_run_id") in candidate_ids:
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
    return "failed"


def _normalize_complexity(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lower()


def _detail_from_live_story(story: dict) -> tuple[str, str | None]:
    phase_val = story.get("phase")
    status_val = story.get("status", "waiting")
    blocked_by_val = list(story.get("blocked_by") or [])
    detail_data = story.get("detail") or {}
    complexity = _normalize_complexity(story.get("complexity"))

    if not isinstance(detail_data, dict):
        detail_data = {}

    if status_val == "blocked" and blocked_by_val:
        return f"blocked by: {', '.join(blocked_by_val)}", complexity

    if phase_val == "PREFLIGHT":
        verdict = detail_data.get("preflight_verdict")
        sufficiency = detail_data.get("preflight_sufficiency")
        parts = [part for part in (verdict, sufficiency) if isinstance(part, str) and part]
        if parts:
            return " / ".join(parts), complexity
        if status_val == "waiting":
            return "waiting", complexity
        return "—", complexity

    if status_val == "waiting":
        return "waiting", complexity

    if status_val in {"done", "failed", "skipped", "preserved"}:
        final_outcome = detail_data.get("final_outcome")
        if isinstance(final_outcome, str) and final_outcome:
            return final_outcome, complexity
        if status_val == "failed" and phase_val:
            return phase_val, complexity
        return "—", complexity

    if phase_val == "PLAN":
        current = detail_data.get("plan_attempt")
        maximum = detail_data.get("plan_max_attempts")
        if isinstance(current, int) and isinstance(maximum, int) and maximum > 0:
            return f"plan {current}/{maximum}", complexity
        return "plan", complexity

    if phase_val == "DEV":
        cycle = detail_data.get("review_cycle")
        iteration = detail_data.get("dev_iteration")
        if isinstance(cycle, int) and isinstance(iteration, int):
            return f"c{cycle + 1} i{iteration}", complexity
        return "dev", complexity

    if phase_val == "REVIEW":
        p1 = detail_data.get("review_p1")
        p2 = detail_data.get("review_p2")
        if isinstance(p1, int) and isinstance(p2, int):
            return f"{p1} P1, {p2} P2", complexity
        return "review", complexity

    if phase_val == "VALIDATE":
        gate = detail_data.get("gate_status")
        if isinstance(gate, str) and gate:
            return gate, complexity
        return "running", complexity

    if phase_val == "PLAN_REVIEW":
        current = detail_data.get("plan_attempt")
        maximum = detail_data.get("plan_max_attempts")
        if isinstance(current, int) and isinstance(maximum, int) and maximum > 0:
            return f"plan review {current}/{maximum}", complexity
        return "plan review", complexity

    return "", complexity


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
        phase = outcome if status in ("running", "failed") else None

        bundle_candidate = False
        complexity = None
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
                            complexity = _normalize_complexity(preflight.get("complexity"))
                except Exception:
                    pass

        raw_depends_on = story.get("depends_on") or []
        blocked_by = list(raw_depends_on) if status == "skipped" and raw_depends_on else []

        if outcome == "ESCALATE":
            detail = "ESCALATE"
        elif outcome == "ALREADY_DONE":
            detail = "ALREADY_DONE"
        elif outcome == "DONE":
            detail = "APPROVE"
        elif status == "skipped" and raw_depends_on:
            detail = f"blocked by: {', '.join(raw_depends_on)}"
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
                elapsed_seconds=None,
                detail=detail,
                complexity=complexity,
            )
        )

    return entries


def read_live_status(run_id: str, project_root: Path) -> list[StoryStatusEntry] | None:
    """Read .forge/runs/<run-id>.state for live sprint status.

    Returns a list of ``StoryStatusEntry`` objects, or ``None`` if the state
    file does not exist.
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
        detail, complexity = _detail_from_live_story(story)

        entries.append(
            StoryStatusEntry(
                slug=story.get("slug", ""),
                path=story.get("path", story.get("slug", "")),
                status=status_val,
                phase=phase_val,
                cost_usd=float(story.get("cost_usd", 0.0)),
                blocked_by=blocked_by_val,
                bundle_candidate=bool(story.get("bundle_candidate", False)),
                elapsed_seconds=None,
                detail=detail,
                complexity=complexity,
            )
        )
    return entries
