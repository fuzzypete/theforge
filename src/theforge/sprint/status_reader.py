"""Sprint status reader — parse live state files and completed sprint summaries."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .audit import _load_accumulated_stories


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
    stage: str = ""
    detail: str = ""
    complexity: str | None = None
    model: str | None = None


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


def find_live_state_path(run_id: str, project_root: Path) -> Path | None:
    """Return the best available live sprint state file for ``run_id``.

    During sprint re-exec startup, the new worker writes ``<old>.redirect`` and
    ``<new>.pid`` before ``<new>.state`` exists. In that window, surface the
    predecessor ``.state`` so status commands keep rendering the sprint view.
    """
    import json  # noqa: PLC0415

    runs_dir = project_root / ".forge" / "runs"
    state_path = runs_dir / f"{run_id}.state"
    if state_path.exists():
        return state_path

    for redirect_file in runs_dir.glob("*.redirect"):
        try:
            data = json.loads(redirect_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("new_run_id") != run_id:
            continue
        predecessor_state = runs_dir / f"{redirect_file.stem}.state"
        if predecessor_state.exists():
            return predecessor_state

    return None


def find_sprint_summary(run_id: str, project_root: Path) -> Path | None:
    """Scan .forge/logs/*/sprint-summary.yaml for the file containing run_id.

    After a run_id rollover the summary is written under the terminal run_id.
    Follows the redirect chain so earlier run_ids resolve to the same summary.

    Also matches summaries whose story metadata records the queried run_id in
    any story's ``story_run_id`` or legacy ``preflight_source_run_id`` field.
    This covers mid-run sprint re-execs where the final summary is written
    under the last worker run_id but earlier worker run_ids still need to
    resolve to the same logical sprint summary.

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
                    if story.get("story_run_id") in candidate_ids:
                        return summary_path
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


def _nonempty_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _parse_status_timestamp(value: object) -> datetime.datetime | None:
    """Parse persisted UTC timestamps from sprint summary or live state data."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _elapsed_seconds_from_bounds(started_at: object, finished_at: object) -> float | None:
    started_at_dt = _parse_status_timestamp(started_at)
    finished_at_dt = _parse_status_timestamp(finished_at)
    if started_at_dt is None or finished_at_dt is None:
        return None
    elapsed = (finished_at_dt - started_at_dt).total_seconds()
    return elapsed if elapsed >= 0 else None


def _elapsed_seconds_from_live_story(story: dict) -> float | None:
    """Compute elapsed time for a live state entry when timestamps are available."""
    started_at_dt = _parse_status_timestamp(story.get("started_at"))
    if started_at_dt is None:
        return None

    finished_at_dt = _parse_status_timestamp(story.get("finished_at"))
    if finished_at_dt is not None:
        elapsed = (finished_at_dt - started_at_dt).total_seconds()
        return elapsed if elapsed >= 0 else None

    if story.get("status") == "running":
        elapsed = (datetime.datetime.now(datetime.timezone.utc) - started_at_dt).total_seconds()
        return elapsed if elapsed >= 0 else None

    return None


def _format_usage_stage(used: object, maximum: object, label: str) -> str:
    if isinstance(used, int) and isinstance(maximum, int) and maximum > 0:
        return f"{label}={used}/{maximum}"
    if isinstance(used, int) and used > 0:
        return f"{label}={used}"
    return ""


def _format_terminal_stage(iteration_usage: dict | None) -> str:
    if not isinstance(iteration_usage, dict):
        return ""
    review_raw = iteration_usage.get("review")
    dev_raw = iteration_usage.get("dev")
    review = review_raw if isinstance(review_raw, dict) else {}
    dev = dev_raw if isinstance(dev_raw, dict) else {}
    review_used = review.get("used")
    dev_used = dev.get("used")

    parts: list[str] = []
    if isinstance(review_used, int) and review_used > 0:
        parts.append(f"{review_used} cyc")
    if isinstance(dev_used, int) and dev_used > 0:
        parts.append(f"{dev_used} iter")
    return " / ".join(parts)


def _review_detail(verdict: object, p1: object, p2: object) -> str:
    verdict_text = _nonempty_str(verdict)
    if isinstance(p1, int) and isinstance(p2, int):
        count_text = f"{p1}P1 {p2}P2"
        return f"{verdict_text} {count_text}".strip() if verdict_text else count_text
    return verdict_text or ""


def _classify_wait_reason(blocked_by: list[str]) -> str:
    if not blocked_by:
        return ""
    joined = " ".join(blocked_by).lower()
    if "budget" in joined:
        return "budget"
    if "parallel" in joined or "saturated" in joined:
        return "parallel cap"
    return "dependency"


def _waiting_detail(blocked_by: list[str]) -> str:
    if not blocked_by:
        return "waiting"
    if all(item.startswith("issue-") for item in blocked_by):
        return f"depends on {', '.join(blocked_by)}"
    return "; ".join(blocked_by)


def _terminal_phase(outcome: str, depends_on: list[str]) -> str | None:
    if outcome == "SKIPPED" and depends_on:
        return "waiting"
    return outcome or None


def _stage_and_detail_from_live_story(story: dict) -> tuple[str, str, str | None]:
    phase_val = story.get("phase")
    status_val = story.get("status", "waiting")
    blocked_by_val = list(story.get("blocked_by") or [])
    detail_data = story.get("detail") or {}
    complexity = _normalize_complexity(story.get("complexity"))

    if not isinstance(detail_data, dict):
        detail_data = {}

    if status_val in {"waiting", "blocked"} and blocked_by_val:
        return _classify_wait_reason(blocked_by_val), _waiting_detail(blocked_by_val), complexity

    if phase_val == "PREFLIGHT":
        verdict = detail_data.get("preflight_verdict")
        sufficiency = detail_data.get("preflight_sufficiency")
        parts = [part for part in (verdict, sufficiency) if isinstance(part, str) and part]
        if parts:
            return "", " / ".join(parts), complexity
        if status_val == "waiting":
            return "", "waiting", complexity
        return "", "—", complexity

    if status_val == "waiting":
        return "", "waiting", complexity

    if status_val in {"done", "failed", "skipped", "preserved"}:
        final_outcome = detail_data.get("final_outcome")
        # Defensive backstop: a failed/skipped story must never display a
        # success outcome. If the detail dict still claims DONE/ALREADY_DONE
        # (e.g. from a leaked prior-run artifact), reconcile by trusting the
        # current run's status and use the canonical outcome from the entry.
        if status_val in {"failed", "skipped"} and final_outcome in {"DONE", "ALREADY_DONE"}:
            canonical_outcome = _nonempty_str(story.get("outcome"))
            final_outcome = canonical_outcome.upper() if canonical_outcome else None
        skip_reason = _nonempty_str(story.get("reason")) if status_val == "skipped" else None
        if (
            skip_reason
            and isinstance(final_outcome, str)
            and final_outcome in {"SKIPPED", "DROPPED"}
        ):
            return "", skip_reason, complexity
        if isinstance(final_outcome, str) and final_outcome:
            return "", final_outcome, complexity
        if skip_reason:
            return "", skip_reason, complexity
        if status_val == "failed" and phase_val:
            return "", phase_val, complexity
        return "", "—", complexity

    if phase_val == "PLAN":
        current = detail_data.get("plan_attempt")
        maximum = detail_data.get("plan_max_attempts")
        stage = _format_usage_stage(current, maximum, "attempt")
        return stage, "planning", complexity

    if phase_val == "DEV":
        iteration = detail_data.get("dev_iteration")
        stage = _format_usage_stage(iteration, detail_data.get("dev_max_iterations"), "iter")
        if not stage and isinstance(iteration, int):
            stage = f"iter={iteration}"
        current_finding = _nonempty_str(detail_data.get("current_finding"))
        files_touched = detail_data.get("files_touched")
        last_gate = _nonempty_str(detail_data.get("last_gate_result"))
        detail_parts: list[str] = []
        if current_finding:
            detail_parts.append(current_finding)
        if isinstance(files_touched, int):
            detail_parts.append(f"{files_touched} files touched")
        if last_gate:
            detail_parts.append(f"last gate {last_gate}")
        return stage, " | ".join(detail_parts) or "running", complexity

    if phase_val == "REVIEW":
        cycle = detail_data.get("review_cycle")
        stage = _format_usage_stage(cycle, detail_data.get("review_max_cycles"), "cycle")
        if not stage and isinstance(cycle, int):
            stage = f"cycle={cycle}"
        p1 = detail_data.get("review_p1")
        p2 = detail_data.get("review_p2")
        detail = _review_detail(detail_data.get("review_verdict"), p1, p2)
        return stage, detail or "running", complexity

    if phase_val in {"GATE", "VALIDATE"}:
        gate = detail_data.get("gate_status")
        if isinstance(gate, str) and gate:
            return "", gate, complexity
        return "", "running", complexity

    if phase_val == "PLAN_REVIEW":
        current = detail_data.get("plan_attempt")
        maximum = detail_data.get("plan_max_attempts")
        stage = _format_usage_stage(current, maximum, "cycle")
        detail = _review_detail(
            detail_data.get("review_verdict"),
            detail_data.get("review_p1"),
            detail_data.get("review_p2"),
        )
        return stage, detail or "reviewing plan", complexity

    if phase_val == "WORKSPACE":
        return (
            _nonempty_str(detail_data.get("workspace_stage")) or "",
            _nonempty_str(detail_data.get("command")) or "creating workspace",
            complexity,
        )

    return "", "", complexity


def _stage_and_detail_from_completed_story(
    story: dict,
    audit_data: dict | None,
) -> tuple[str, str, str | None]:
    outcome = str(story.get("outcome", ""))
    depends_on = list(story.get("depends_on") or [])
    preflight_data = (audit_data or {}).get("preflight")
    preflight = preflight_data if isinstance(preflight_data, dict) else {}
    if outcome == "SKIPPED" and depends_on:
        return (
            _classify_wait_reason(depends_on),
            _waiting_detail(depends_on),
            _normalize_complexity(preflight.get("complexity")),
        )

    complexity = _normalize_complexity(preflight.get("complexity"))
    stage = _format_terminal_stage(story.get("iteration_usage"))

    detail = ""
    if isinstance(audit_data, dict):
        reviews = audit_data.get("reviews")
        if isinstance(reviews, list) and reviews:
            last_review = reviews[-1]
            if isinstance(last_review, dict):
                finding_counts = last_review.get("findings_by_severity")
                p1 = p2 = None
                if isinstance(finding_counts, dict):
                    p1 = finding_counts.get("P1", 0)
                    p2 = finding_counts.get("P2", 0)
                detail = _review_detail(last_review.get("verdict"), p1, p2)
                summary = _nonempty_str(last_review.get("summary"))
                if summary and not detail:
                    detail = summary
                elif summary and detail:
                    detail = f"{detail} — {summary}"
        if not detail:
            outcome_block = audit_data.get("outcome")
            if isinstance(outcome_block, dict):
                message = _nonempty_str(outcome_block.get("message"))
                if message:
                    detail = message
        if not detail:
            error = _nonempty_str(audit_data.get("error"))
            if error:
                detail = error

    if not detail:
        # Only trust a top-level verdict for success outcomes. A stale APPROVE
        # on a FAILED/ESCALATED/SKIPPED row from a prior run must not leak.
        _success_outcome = outcome in {"DONE", "ALREADY_DONE"}
        verdict = _nonempty_str(story.get("verdict")) if _success_outcome else None
        if verdict == "APPROVE":
            detail = "APPROVE"
        elif verdict:
            detail = verdict
        elif outcome in {"SKIPPED", "DROPPED"}:
            detail = (
                _nonempty_str(story.get("error"))
                or _nonempty_str(story.get("drop_reason"))
                or outcome
            )
        elif outcome == "DONE":
            detail = "APPROVE"
        elif outcome == "ALREADY_DONE":
            detail = "ALREADY_DONE"
        else:
            detail = outcome

    return stage, detail, complexity


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
        phase = _terminal_phase(outcome, list(story.get("depends_on") or []))

        bundle_candidate = False
        complexity = None
        audit_data: dict | None = None
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
        stage, detail, derived_complexity = _stage_and_detail_from_completed_story(
            story, audit_data
        )
        if complexity is None:
            complexity = derived_complexity

        dev_model_raw = story.get("dev_model")
        model_val = dev_model_raw if isinstance(dev_model_raw, str) and dev_model_raw else None

        entries.append(
            StoryStatusEntry(
                slug=slug,
                path=path,
                status=status,
                phase=phase,
                cost_usd=cost_usd,
                blocked_by=blocked_by,
                bundle_candidate=bundle_candidate,
                elapsed_seconds=_elapsed_seconds_from_bounds(
                    story.get("started_at"),
                    story.get("finished_at"),
                ),
                stage=stage,
                detail=detail,
                complexity=complexity,
                model=model_val,
            )
        )

    return entries


def read_live_status(run_id: str, project_root: Path) -> list[StoryStatusEntry] | None:
    """Read .forge/runs/<run-id>.state for live sprint status.

    Returns a list of ``StoryStatusEntry`` objects, or ``None`` if the state
    file does not exist.

    When the live state file records a ``sprint_id``, merge in accumulated story
    entries from prior process generations so live status reflects the full
    logical sprint across re-exec boundaries.
    """
    state_path = find_live_state_path(run_id, project_root)
    if state_path is None:
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
    seen_slugs: set[str] = set()
    for story in stories_data:
        if not isinstance(story, dict):
            continue
        slug = story.get("slug", "")
        status_val = story.get("status", "waiting")
        phase_val = story.get("phase")
        blocked_by_val = list(story.get("blocked_by") or [])
        phase_display = phase_val
        if status_val in {"waiting", "blocked"} and blocked_by_val:
            phase_display = "waiting"
        stage, detail, complexity = _stage_and_detail_from_live_story(story)

        if status_val in {"skipped", "blocked"}:
            model_val: str | None = None
        else:
            model_raw = story.get("current_model")
            model_val = model_raw if isinstance(model_raw, str) and model_raw else None

        entries.append(
            StoryStatusEntry(
                slug=slug,
                path=story.get("path", slug),
                status=status_val,
                phase=phase_display,
                cost_usd=float(story.get("cost_usd", 0.0)),
                blocked_by=blocked_by_val,
                bundle_candidate=bool(story.get("bundle_candidate", False)),
                elapsed_seconds=_elapsed_seconds_from_live_story(story),
                stage=stage,
                detail=detail,
                complexity=complexity,
                model=model_val,
            )
        )
        if slug:
            seen_slugs.add(slug)

    sprint_id = data.get("sprint_id")
    if isinstance(sprint_id, str) and sprint_id:
        for story in _load_accumulated_stories(sprint_id, project_root):
            if not isinstance(story, dict):
                continue
            slug = story.get("slug", "")
            if not slug or slug in seen_slugs:
                continue
            outcome = str(story.get("outcome", "SKIPPED"))
            depends_on = list(story.get("depends_on") or [])
            detail = _nonempty_str(story.get("verdict")) or outcome
            if outcome in {"SKIPPED", "DROPPED"} or depends_on:
                _, detail, _ = _stage_and_detail_from_completed_story(story, None)
            entries.append(
                StoryStatusEntry(
                    slug=slug,
                    path=story.get("path", slug),
                    status=_outcome_to_status(outcome),
                    phase=_terminal_phase(outcome, depends_on),
                    cost_usd=float(story.get("cost_usd", 0.0)),
                    blocked_by=depends_on,
                    bundle_candidate=False,
                    elapsed_seconds=_elapsed_seconds_from_bounds(
                        story.get("started_at"),
                        story.get("finished_at"),
                    ),
                    stage=_format_terminal_stage(story.get("iteration_usage")),
                    detail=detail,
                    complexity=None,
                )
            )
            seen_slugs.add(slug)
    return entries
