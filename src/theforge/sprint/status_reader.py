"""Sprint status reader — parse live state files and completed sprint summaries."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from .audit import (
    PREFLIGHT_DEGRADED_ROW_KEYS,
    _load_accumulated_stories,
    preflight_degraded_row_fields_from_audit,
    preflight_degraded_row_fields_from_row,
)
from .preserved_resume import preserved_escalated_detail_for_story


@dataclass
class StoryStatusEntry:
    """Per-story display data for forge sprint-status."""

    slug: str
    path: str
    status: str  # "done" | "running" | "waiting" | "blocked" | "failed" | "skipped"
    phase: str | None
    # None = the story's cost could not be measured (unknown, not zero) (#1992).
    cost_usd: float | None
    blocked_by: list[str] = field(default_factory=list)
    bundle_candidate: bool = False
    # Cost-aware batch group id (#727), or None. Rendered separately from
    # conflict bundles — the two primitives mean different things.
    batch_group: str | None = None
    elapsed_seconds: float | None = None
    stage: str = ""
    detail: str = ""
    complexity: str | None = None
    complexity_score: int | None = None
    model: str | None = None
    last_event_ts: float | None = None
    # Phases the story stopped still owing, derived from its persisted resume
    # record (e.g. ``["REVIEW"]`` when a review cycle concluded REQUEST_CHANGES
    # and the next one never ran). Read-only display data (#2239).
    outstanding_phases: list[str] = field(default_factory=list)
    # What each re-entry path would do, when they disagree — empty otherwise.
    # An operator choosing between `forge review` and `forge sprint --resume` is
    # choosing whether the work gets reviewed, so the choice is stated here
    # rather than left to be discovered by running one.
    reentry_note: str = ""


def _reentry_display(project_root: Path | None, slug: str) -> tuple[list[str], str]:
    """Return ``(outstanding_phases, reentry_note)`` for ``slug``.

    Reads the coordinator's persisted resume record — the same record a resume
    would recover — so status answers "what does this story still owe, and what
    will re-entering do" without starting a run.  Best-effort: any unreadable or
    absent record yields empty display fields rather than an error, because a
    status view that fails on a missing sidecar is worse than one that says
    nothing about it.
    """
    if project_root is None or not slug:
        return [], ""
    try:
        from theforge.coordinator.resume_persistence import (  # noqa: PLC0415
            describe_outstanding_phases,
            describe_reentry_paths,
            load_reentry_analysis,
        )

        analysis = load_reentry_analysis(Path(project_root), slug)
    except Exception:
        return [], ""
    if not analysis:
        return [], ""
    return describe_outstanding_phases(analysis), describe_reentry_paths(analysis)


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


def read_live_sprint_name(run_id: str, project_root: Path) -> str | None:
    """Return the ``sprint_name`` recorded in the live state for ``run_id``.

    Returns ``None`` if no live state file exists or it records no sprint name.
    Used to resolve the nested per-story log directory
    (``.forge/logs/<sprint_name>/<slug>/``) for ``forge logs --story``.
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
    name = data.get("sprint_name")
    return name if isinstance(name, str) and name else None


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

    # Run-id-keyed file is the canonical per-run record (issue #1480) — try
    # it first so an earlier run's summary is still queryable after later
    # same-name runs have overwritten the legacy name-keyed file.
    for candidate_id in candidate_ids:
        if not candidate_id:
            continue
        for per_run_path in logs_dir.glob(f"*/run-{candidate_id}-summary.yaml"):
            if per_run_path.is_file():
                return per_run_path

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


def _already_done_detail(outcome_source: object, reason: str | None = None) -> str:
    """Render the DETAIL string for an ALREADY_DONE outcome with a source tag.

    The bare string ``ALREADY_DONE`` collapses two structurally distinct paths
    (resume-skip-merged is mechanical and trustworthy; preflight-verdict
    short-circuit is the historically-suspect path) into one indistinguishable
    label. Operators must be able to tell at a glance which classification a
    given story landed under without running ``gh`` commands. This helper is
    the single rendering site that materialises that distinction.
    """
    if isinstance(outcome_source, str):
        if outcome_source == "resume_skip_merged":
            return "ALREADY_DONE (merged)"
        if outcome_source == "preflight_verdict":
            if reason:
                return f"Preflight verdict: {reason}"
            return "ALREADY_DONE (preflight)"
    if reason:
        return f"Preflight verdict: {reason}"
    return "ALREADY_DONE"


def _story_cost_usd(story: dict) -> float | None:
    """Read a story row's ``cost_usd``, preserving an unmeasured ``None``.

    ``None`` means at least one phase's spend was never measured. Rendering it
    as ``$0.00`` would present unpriced work as free (#1992).
    """
    raw = story.get("cost_usd", 0.0)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    return None


def _outcome_to_status(outcome: str) -> str:
    """Map a sprint-summary ``outcome`` value to a display status string."""
    if outcome in ("ALREADY_DONE", "DONE"):
        return "done"
    if outcome == "SKIPPED":
        return "skipped"
    if outcome == "OPERATOR_ACTION":
        return "operator-action"
    if outcome == "DECOMPOSED":
        return "decomposed"
    if outcome in ("ESCALATE", "MERGE_FAILED", "MERGE_ARMING_FAILED"):
        return "failed"
    return "failed"


def _normalize_complexity_score(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _normalize_complexity(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lower()


def _nonempty_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _is_preserved_escalated_story(story: dict) -> bool:
    """Whether a live/completed row represents a preserved escalated worktree."""
    for key in ("reason", "drop_reason", "error"):
        if _nonempty_str(story.get(key)) == "preserved-escalated":
            return True
    return False


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


def elapsed_seconds_since(started_at: object) -> float | None:
    """Seconds elapsed from a persisted UTC timestamp to now; None if unparsable."""
    started_at_dt = _parse_status_timestamp(started_at)
    if started_at_dt is None:
        return None
    elapsed = (datetime.datetime.now(datetime.timezone.utc) - started_at_dt).total_seconds()
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
        return elapsed_seconds_since(story.get("started_at"))

    return None


def _format_usage_stage(used: object, maximum: object, label: str) -> str:
    if isinstance(used, int) and isinstance(maximum, int) and maximum > 0:
        return f"{label}={used}/{maximum}"
    if isinstance(used, int) and used > 0:
        return f"{label}={used}"
    return ""


def _format_live_dev_stage(detail_data: dict) -> str:
    """Render DEV progress as outer review-cycle position plus dev iteration.

    ``review_cycle`` is the count of completed/active review cycles in the
    story's dev->review feedback loop. ``dev_iteration`` is the current dev
    attempt inside that loop. Showing only the latter makes a later DEV retry
    look indistinguishable from first-pass development once the per-cycle dev
    counter has reset.
    """
    parts: list[str] = []
    cycle = detail_data.get("review_cycle")
    if isinstance(cycle, int):
        cycle_stage = _format_usage_stage(
            cycle,
            detail_data.get("review_max_cycles"),
            "cycle",
        )
        parts.append(cycle_stage or f"cycle={cycle}")

    iteration = detail_data.get("dev_iteration")
    iter_stage = _format_usage_stage(
        iteration,
        detail_data.get("dev_max_iterations"),
        "iter",
    )
    if not iter_stage and isinstance(iteration, int):
        iter_stage = f"iter={iteration}"
    if iter_stage:
        parts.append(iter_stage)

    return " ".join(parts)


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


def _reviewer_progress_stage(progress: dict, pool_size: object) -> tuple[str, str]:
    """Render live per-reviewer progress into (stage, pool_detail).

    stage joins per-reviewer entries as ``name=done`` / ``name=iterN`` with
    ``↻rN/M`` appended when a retry is active, e.g.
    ``deepseek=done, gemini=iter3 ↻r1/2``.  A ``⚠Ns`` chip is appended for a
    reviewer that has received a time-nudge (imminent-timeout warning) and not
    yet finalized, e.g. ``gemini=iter3 ⚠116s``.  pool_detail is ``pool D/S done``
    where D counts done reviewers and S is the pool size.
    """
    parts: list[str] = []
    done_count = 0
    for name, info in progress.items():
        if not isinstance(info, dict):
            continue
        if info.get("done"):
            seg = f"{name}=done"
            done_count += 1
        else:
            iteration = info.get("iter")
            if isinstance(iteration, int) and iteration > 0:
                seg = f"{name}=iter{iteration}"
            else:
                seg = f"{name}=…"
        retry = info.get("retry")
        if isinstance(retry, (list, tuple)) and len(retry) == 2:
            seg += f" ↻r{retry[0]}/{retry[1]}"
        # Imminent-timeout signal (distinct from retry/iter progress): a
        # time-nudge was sent and the reviewer has not finalized. Persist until
        # it finalizes (nudge cleared) or times out (row replaced by phase move).
        nudge = info.get("nudge")
        if not info.get("done") and isinstance(nudge, int) and nudge > 0:
            seg += f" ⚠{nudge}s"
        parts.append(seg)
    total = pool_size if isinstance(pool_size, int) and pool_size > 0 else len(progress)
    pool_detail = f"pool {done_count}/{total} done"
    return ", ".join(parts), pool_detail


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
        refs = [f"#{item[len('issue-') :]}" for item in blocked_by]
        return f"depends on {', '.join(refs)}"
    return "; ".join(blocked_by)


_FAILURE_OUTCOMES = {
    "FAILED",
    "MERGE_FAILED",
    "ESCALATE",
    "ESCALATED",
    "DROPPED",
    "DROPPED_SHAPE",
    "DROPPED_AFTER_FIX",
}


def _terminal_phase(
    outcome: str,
    depends_on: list[str],
    last_phase: str | None = None,
) -> str | None:
    if outcome == "SKIPPED" and depends_on:
        return "waiting"
    if outcome in _FAILURE_OUTCOMES and last_phase:
        return last_phase
    return outcome or None


def _render_intake_drop_detail(final_outcome: str, detail_data: dict) -> str:
    """Operator-readable DETAIL for an intake-dropped story.

    Includes the primary rule code + finding problem (so the operator knows
    *what* failed) and an agent attempt summary (whether the LLM ran, cost,
    model) — the structured data already lives in ``detail.intake_findings``
    and ``detail.intake_audit``; this renders it instead of reducing to the
    rule-codes-only ``intake_summary`` string.
    """
    findings = detail_data.get("intake_findings") or []
    primary_code: str | None = None
    primary_problem: str | None = None
    if isinstance(findings, list) and findings:
        first = findings[0]
        if isinstance(first, dict):
            code = first.get("code")
            problem = first.get("problem")
            if isinstance(code, str) and code:
                primary_code = code
            if isinstance(problem, str) and problem.strip():
                primary_problem = problem.strip()
    if primary_code is None:
        codes = detail_data.get("intake_codes")
        if isinstance(codes, list) and codes and isinstance(codes[0], str):
            primary_code = codes[0]

    parts: list[str] = [final_outcome]
    if primary_code:
        head = f"[{primary_code}]"
        if primary_problem:
            head = f"{head} {primary_problem}"
        parts.append(head)
    elif primary_problem:
        parts.append(primary_problem)
    else:
        intake_summary = _nonempty_str(detail_data.get("intake_summary"))
        if intake_summary:
            parts.append(intake_summary)

    agent_summary = _nonempty_str(detail_data.get("intake_agent_summary"))
    if not agent_summary:
        intake_audit = detail_data.get("intake_audit")
        if isinstance(intake_audit, dict):
            agent = intake_audit.get("agent")
            if isinstance(agent, dict):
                bits: list[str] = []
                if agent.get("attempted"):
                    bits.append("agent_attempted=yes")
                else:
                    bits.append("agent_attempted=no")
                cost = agent.get("cost_usd")
                if isinstance(cost, (int, float)) and cost > 0:
                    bits.append(f"cost=${float(cost):.4f}")
                model = agent.get("model_used")
                if isinstance(model, str) and model:
                    bits.append(f"model={model}")
                agent_summary = ", ".join(bits) or None
    if agent_summary:
        parts.append(f"({agent_summary})")
    if len(parts) == 1:
        return ""
    return " ".join(parts)


#: Stage reported for a story the scheduler is deliberately holding at its plan
#: gate because its planned files overlap another story's. A held story emits no
#: events, so without this the wait reads as a stall (#2235).
COLLISION_GATE_STAGE = "collision gate"

#: How many overlapping paths to name before summarising the rest.
_GATE_FILE_PREVIEW = 3


def _collision_gate_detail(detail_data: dict) -> str | None:
    """Describe a held collision gate, or None when no hold is recorded."""
    blockers = detail_data.get("collision_gate_blockers")
    if not isinstance(blockers, list):
        return None
    blocker_names = [b for b in blockers if isinstance(b, str) and b]
    if not blocker_names:
        return None

    files = detail_data.get("collision_gate_files")
    paths = [f for f in files if isinstance(f, str) and f] if isinstance(files, list) else []

    text = f"held behind {', '.join(blocker_names)}"
    if paths:
        shown = [Path(p).name for p in paths[:_GATE_FILE_PREVIEW]]
        remaining = len(paths) - len(shown)
        files_str = ", ".join(shown)
        if remaining > 0:
            files_str += f" +{remaining} more"
        text += f" on {files_str}"
    return text


#: Stage reported for a story the coordinator is deliberately holding at a gate
#: that waits on a person (escalate, human review, plan review). The story emits
#: no events while the gate polls, so without this the wait reads as a stall —
#: and the one state that only an operator can clear is the one state the view
#: gives them no reason to act on (#2313).
OPERATOR_DECISION_STAGE = "operator decision"

#: How many decision options to name before summarising the rest.
_DECISION_OPTION_PREVIEW = 3


def _fmt_remaining(seconds: float) -> str:
    """Format a remaining-time span the same way the run log does."""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _pending_entry_is_live(entry: dict) -> bool:
    """True when ``entry`` is an undecided checkpoint owned by a live process.

    ``list_pending`` returns every file in ``.forge/pending`` verbatim, including
    already-decided records and records left behind by a dead run. Rendering
    either as "waiting on you" would send the operator to a gate that is not
    open, so both are filtered out here.
    """
    from theforge.pending import decision_of  # noqa: PLC0415

    # Through the shared predicate: a record the poller still considers
    # undecided is a gate that really is open, and hiding it here would leave an
    # operator with no prompt to answer something the run is blocked on.
    if decision_of(entry) is not None:
        return False
    # A triage record shares the directory but is not a story gate: no story is
    # held at it and no process owns it. It is pid-less by construction, so this
    # would already fall through — the kind check makes the exclusion explicit
    # rather than a consequence of a field being absent.
    if str(entry.get("kind") or "").strip() == "triage":
        return False
    pid = entry.get("pid")
    try:
        owner_pid = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        from theforge.pid import _is_pid_alive  # noqa: PLC0415

        return _is_pid_alive(owner_pid)
    except Exception:
        return False


def _pending_remaining_text(entry: dict) -> str:
    """Describe how long the pending wait has left, or that it has lapsed."""
    timeout_at_str = entry.get("timeout_at")
    if not isinstance(timeout_at_str, str) or not timeout_at_str:
        return ""
    try:
        timeout_at = datetime.datetime.fromisoformat(timeout_at_str)
    except Exception:
        return ""
    if timeout_at.tzinfo is None:
        timeout_at = timeout_at.replace(tzinfo=datetime.timezone.utc)
    remaining = (timeout_at - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    if remaining > 0:
        return f"{_fmt_remaining(remaining)} remaining"
    # The escalate gate preserves an expired checkpoint and keeps awaiting a
    # selection rather than auto-rejecting, so an elapsed window still needs the
    # operator — say that instead of implying the decision is gone.
    return "window elapsed — still awaiting decision"


def _run_identity(run_id: str, project_root: Path, state_path: Path) -> set[str]:
    """Every run id that names the run whose live state is being displayed.

    A sprint that re-execs keeps its work under a new run id while status may
    still be queried under the old one, so both ends of the redirect chain — and
    the stem of the state file actually resolved — identify the same run.
    """
    import json  # noqa: PLC0415

    ids = {run_id, state_path.stem}
    for candidate in (run_id, state_path.stem):
        if candidate:
            ids.add(_follow_redirect_chain(candidate, project_root))

    # Walk the redirect chain backwards too: a re-exec'd sprint opened its gates
    # under the run id it carried before the handoff, and that is still this run.
    predecessors: dict[str, list[str]] = {}
    try:
        redirect_files = sorted((project_root / ".forge" / "runs").glob("*.redirect"))
    except OSError:
        redirect_files = []
    for redirect_file in redirect_files:
        try:
            data = json.loads(redirect_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        new_id = data.get("new_run_id") if isinstance(data, dict) else None
        if isinstance(new_id, str) and new_id:
            predecessors.setdefault(new_id, []).append(redirect_file.stem)

    frontier = list(ids)
    while frontier:
        current = frontier.pop()
        for previous in predecessors.get(current, []):
            if previous and previous not in ids:
                ids.add(previous)
                frontier.append(previous)

    return {i for i in ids if i}


def _pending_belongs_to_run(entry: dict, identity: set[str], project_root: Path) -> bool:
    """True when ``entry`` is a checkpoint held by the run being displayed.

    A checkpoint is named after the *story* run id, which two concurrently live
    sprints working the same slug can share — so slug equality alone would show
    one sprint's gate on the other's row, sending the operator to a decision
    that is not theirs to make (#2313).
    """
    owner_run_id = _nonempty_str(entry.get("owner_run_id"))
    if owner_run_id:
        return owner_run_id in identity

    # A checkpoint written before ``owner_run_id`` existed still identifies its
    # process. If that PID is a different live run's, the gate is that run's.
    try:
        owner_pid = int(entry.get("pid"))
    except (TypeError, ValueError):
        return False
    try:
        from theforge.detach import find_run_id_for_pid  # noqa: PLC0415

        other_run_id = find_run_id_for_pid(project_root, owner_pid)
    except Exception:
        return True
    return not other_run_id or other_run_id in identity


def _select_pending_for_story(
    pending: list[dict],
    slug: str,
    identity: set[str],
    project_root: Path,
) -> dict | None:
    """Pick the live pending record a story row should display, if any."""
    if not slug:
        return None
    candidates = [
        entry
        for entry in pending
        if isinstance(entry, dict)
        and _nonempty_str(entry.get("story")) == slug
        and _pending_entry_is_live(entry)
        and _pending_belongs_to_run(entry, identity, project_root)
    ]
    if not candidates:
        return None

    # An ESCALATE checkpoint is the gate that stalls longest and costs most to
    # miss; prefer it, then the most recently created record.
    def _rank(entry: dict) -> tuple[int, str]:
        phase = _nonempty_str(entry.get("phase")) or ""
        created = _nonempty_str(entry.get("created_at")) or ""
        return (1 if phase.upper() == "ESCALATE" else 0, created)

    return max(candidates, key=_rank)


def _live_pending_records(project_root: Path) -> list[dict]:
    """Load `.forge/pending` checkpoints, best-effort (never raises)."""
    try:
        from theforge.pending import list_pending  # noqa: PLC0415

        return [entry for entry in list_pending(project_root) if isinstance(entry, dict)]
    except Exception:
        return []


def _pending_decision_display(entry: dict) -> str:
    """Render the DETAIL text for a story awaiting an operator decision."""
    phase = _nonempty_str(entry.get("phase")) or "gate"
    run_id = _nonempty_str(entry.get("run_id")) or "?"
    parts = [f"{phase} decision pending {run_id}"]
    remaining = _pending_remaining_text(entry)
    if remaining:
        parts.append(remaining)
    options = entry.get("options")
    opts = [o for o in options if isinstance(o, str) and o] if isinstance(options, list) else []
    if opts:
        shown = opts[:_DECISION_OPTION_PREVIEW]
        extra = len(opts) - len(shown)
        opts_str = "/".join(shown)
        if extra > 0:
            opts_str += f" +{extra} more"
        parts.append(f"options: {opts_str}")
    return "; ".join(parts)


def _stage_and_detail_from_live_story(story: dict) -> tuple[str, str, str | None]:
    """Live stage/detail, with the degraded-preflight condition appended.

    The condition is carried on every phase's row, not only PREFLIGHT's: a
    story that failed preflight and then ran on conservative values is exactly
    the case that has to stay visible after the phase it happened in (#2346).
    """
    stage, detail, complexity = _live_stage_and_detail(story)
    detail_data = story.get("detail")
    note = _preflight_degraded_detail(
        preflight_degraded_state(detail_data if isinstance(detail_data, dict) else {}, None)
    )
    if note:
        detail = f"{detail} — {note}" if detail and detail != "—" else note
    return stage, detail, complexity


def _live_stage_and_detail(story: dict) -> tuple[str, str, str | None]:
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

    if status_val == "operator-action":
        return "", "not sprintable; operator deliverable", complexity

    if status_val == "decomposed":
        return "", "returned for decomposition", complexity

    if status_val in {"done", "failed", "skipped", "preserved"}:
        final_outcome = detail_data.get("final_outcome")
        # Defensive backstop: a failed/skipped story must never display a
        # success outcome. If the detail dict still claims DONE/ALREADY_DONE
        # (e.g. from a leaked prior-run artifact), reconcile by trusting the
        # current run's status and use the canonical outcome from the entry.
        if status_val in {"failed", "skipped"} and final_outcome in {"DONE", "ALREADY_DONE"}:
            canonical_outcome = _nonempty_str(story.get("outcome"))
            final_outcome = canonical_outcome.upper() if canonical_outcome else None
        preserved_reason = (
            _nonempty_str(story.get("reason")) if status_val == "preserved" else None
        )
        skip_reason = _nonempty_str(story.get("reason")) if status_val == "skipped" else None
        # Intake-drop outcomes carry structured rule codes in the detail dict.
        # Surfacing them in the DETAIL column keeps operators from having to
        # consult audit YAML to learn which rule fired, what the agent tried,
        # or why the rerun gate failed.
        if isinstance(final_outcome, str) and final_outcome in {
            "DROPPED_AFTER_FIX",
            "DROPPED_SHAPE",
        }:
            rendered = _render_intake_drop_detail(final_outcome, detail_data)
            if rendered:
                return "", rendered, complexity
        if (
            skip_reason
            and isinstance(final_outcome, str)
            and final_outcome in {"SKIPPED", "DROPPED"}
        ):
            return "", skip_reason, complexity
        if (
            status_val == "preserved"
            and preserved_reason == "preserved-escalated"
            and (final_outcome in {None, "PRESERVED"} or not isinstance(final_outcome, str))
        ):
            return "", preserved_escalated_detail_for_story(story), complexity
        if isinstance(final_outcome, str) and final_outcome:
            if final_outcome == "ALREADY_DONE":
                return (
                    "",
                    _already_done_detail(detail_data.get("outcome_source")),
                    complexity,
                )
            return "", final_outcome, complexity
        if skip_reason:
            return "", skip_reason, complexity
        if status_val == "failed" and phase_val:
            return "", phase_val, complexity
        return "", "—", complexity

    if phase_val == "PLAN_DONE":
        # The scheduler chose this wait and recorded why. Say so, rather than
        # letting the story age out as generic inactivity (#2235).
        gate_detail = _collision_gate_detail(detail_data)
        if gate_detail:
            return COLLISION_GATE_STAGE, gate_detail, complexity

    if phase_val == "PLAN":
        current = detail_data.get("plan_attempt")
        maximum = detail_data.get("plan_max_attempts")
        stage = _format_usage_stage(current, maximum, "attempt")
        return stage, "planning", complexity

    if phase_val == "DEV":
        stage = _format_live_dev_stage(detail_data)
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
        cycle_stage = _format_usage_stage(cycle, detail_data.get("review_max_cycles"), "cycle")
        if not cycle_stage and isinstance(cycle, int):
            cycle_stage = f"cycle={cycle}"
        reviewer_progress = detail_data.get("reviewer_progress")
        if isinstance(reviewer_progress, dict) and reviewer_progress:
            stage, pool_detail = _reviewer_progress_stage(
                reviewer_progress, detail_data.get("reviewer_pool_size")
            )
            # Keep the cycle number visible during the in-flight window rather
            # than showing only per-reviewer progress (issue #1488) — the
            # reviewer_progress payload carries review_cycle for exactly this.
            if cycle_stage and stage:
                stage = f"{cycle_stage} {stage}"
            elif cycle_stage:
                stage = cycle_stage
            return stage, pool_detail, complexity
        stage = cycle_stage
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
        reviewer_progress = detail_data.get("reviewer_progress")
        if isinstance(reviewer_progress, dict) and reviewer_progress:
            stage, pool_detail = _reviewer_progress_stage(
                reviewer_progress, detail_data.get("reviewer_pool_size")
            )
            return stage, pool_detail, complexity
        current = detail_data.get("plan_attempt")
        maximum = detail_data.get("plan_max_attempts")
        stage = _format_usage_stage(current, maximum, "cycle")
        detail = _review_detail(
            detail_data.get("review_verdict"),
            detail_data.get("review_p1"),
            detail_data.get("review_p2"),
        )
        return stage, detail or "reviewing plan", complexity

    if phase_val == "REUSE_GATE":
        # Resume triage gating an existing worktree. Naming the branch and
        # commit under gate is the whole point of the phase: it tells the
        # operator which stranded worktree the long-running gate belongs to.
        target = _nonempty_str(detail_data.get("gate_branch")) or _nonempty_str(
            detail_data.get("gate_worktree")
        )
        commit = _nonempty_str(detail_data.get("gate_commit"))
        if target and commit:
            gate_detail = f"validating {target} @ {commit}"
        elif target:
            gate_detail = f"validating {target}"
        else:
            gate_detail = "validating existing worktree"
        return _nonempty_str(detail_data.get("gate_purpose")) or "", gate_detail, complexity

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
    knowledge_summary = (
        (audit_data or {}).get("knowledge_summary")
        if isinstance((audit_data or {}).get("knowledge_summary"), dict)
        else {}
    )

    detail = ""
    is_failure_outcome = outcome in {
        "FAILED",
        "MERGE_FAILED",
        "MERGE_ARMING_FAILED",
        "ESCALATE",
        "ESCALATED",
        "DROPPED",
        "DROPPED_SHAPE",
        "DROPPED_AFTER_FIX",
    }
    if isinstance(audit_data, dict):
        if outcome == "ALREADY_DONE":
            reason = _nonempty_str(preflight.get("reason"))
            detail = _already_done_detail(story.get("outcome_source"), reason)
        # For failure outcomes, the row's detail must describe the failure —
        # not surface a stale review APPROVE that conflates "review approved"
        # with "story is in good standing." Read outcome.message / error first;
        # then optionally append the review verdict label so it is still
        # available but clearly secondary.
        if not detail and is_failure_outcome:
            outcome_block = audit_data.get("outcome")
            if isinstance(outcome_block, dict):
                message = _nonempty_str(outcome_block.get("message"))
                if message:
                    detail = message
            if not detail:
                error = _nonempty_str(audit_data.get("error"))
                if error:
                    detail = error
            reviews = audit_data.get("reviews")
            if detail and isinstance(reviews, list) and reviews:
                last_review = reviews[-1]
                if isinstance(last_review, dict):
                    verdict = _nonempty_str(last_review.get("verdict"))
                    if verdict:
                        detail = f"{detail} (review verdict: {verdict})"
        # For success outcomes, the row's detail must not surface a stale
        # blocking review verdict (REQUEST_CHANGES) that conflates an
        # intermediate cycle with the story's final approved state — the
        # mirror image of the failure-outcome guard above. When the recorded
        # ``reviews`` list omits the terminal APPROVE cycle (leaving a
        # blocking verdict as the last entry), fall back to the canonical
        # outcome message instead of rendering that stale intermediate cycle.
        is_success_outcome = outcome in {"DONE", "ALREADY_DONE"}
        reviews = audit_data.get("reviews")
        if not detail and isinstance(reviews, list) and reviews:
            last_review = reviews[-1]
            if isinstance(last_review, dict):
                last_verdict = _nonempty_str(last_review.get("verdict"))
                if not (is_success_outcome and last_verdict and last_verdict != "APPROVE"):
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
        if outcome == "OPERATOR_ACTION":
            detail = "not sprintable; operator deliverable"
        elif outcome == "DECOMPOSED":
            detail = "returned for decomposition"
        elif verdict == "APPROVE":
            detail = "APPROVE"
        elif verdict:
            detail = verdict
        elif outcome == "PRESERVED" and _is_preserved_escalated_story(story):
            detail = preserved_escalated_detail_for_story(story)
        elif outcome in {"SKIPPED", "DROPPED"}:
            detail = (
                _nonempty_str(story.get("error"))
                or _nonempty_str(story.get("drop_reason"))
                or outcome
            )
        elif outcome in {"DROPPED_AFTER_FIX", "DROPPED_SHAPE"}:
            # Intake-drop entries carry the rule code + problem in ``error``
            # and the structured detail in ``intake``. Render finding problem
            # + agent attempt summary so the operator can act from this row
            # alone — not just see the outcome name.
            intake_block = story.get("intake")
            synthetic_detail: dict = {}
            if isinstance(intake_block, dict):
                synthetic_detail = {
                    "intake_findings": intake_block.get("findings") or [],
                    "intake_codes": intake_block.get("codes") or [],
                    "intake_agent_summary": intake_block.get("agent_summary"),
                    "intake_audit": intake_block.get("audit") or {},
                    "intake_summary": _nonempty_str(story.get("error")),
                }
            else:
                synthetic_detail = {
                    "intake_summary": _nonempty_str(story.get("error")),
                }
            rendered = _render_intake_drop_detail(outcome, synthetic_detail)
            detail = rendered or outcome
        elif outcome == "DONE":
            detail = "APPROVE"
        elif outcome == "ALREADY_DONE":
            detail = _already_done_detail(story.get("outcome_source"))
        else:
            detail = outcome

    allocation_note = _allocation_detail(story.get("story_allocation"))
    if allocation_note:
        detail = f"{detail} — {allocation_note}" if detail else allocation_note

    # A run that proceeded on a preflight that produced no evidence says so on
    # its own row, whatever the story's outcome was. Appended last precisely
    # because a DONE row is where it used to vanish (#2346).
    degraded_note = _preflight_degraded_detail(preflight_degraded_state(story, audit_data))
    if degraded_note:
        detail = f"{detail} — {degraded_note}" if detail else degraded_note

    summary_note = _knowledge_summary_detail(knowledge_summary)
    if summary_note:
        detail = f"{detail} — {summary_note}" if detail else summary_note

    return stage, detail, complexity


def _knowledge_summary_detail(block: dict) -> str:
    """Render a persisted knowledge-summary outcome as a row annotation."""
    if not block or block.get("attempted") is not True or block.get("written") is True:
        return ""
    status = _nonempty_str(block.get("status")) or "failed"
    reason = _nonempty_str(block.get("reason"))
    text = f"knowledge summary {status}"
    if reason:
        text = f"{text}: {reason}"
    return text


def preflight_degraded_state(story: dict, audit_data: dict | None) -> dict | None:
    """Normalize the degraded-preflight condition from either recorded shape.

    The summary row carries flat ``preflight_*`` keys (written since #2346); the
    per-story audit carries a nested ``preflight`` block with the same facts
    under shorter names. Summaries written before #2346 have only the latter, so
    both are read — through the one key map the writers use, so the two
    spellings cannot drift — and folded into one shape. Returns ``None`` when
    preflight was not degraded: the caller renders nothing for a healthy run.
    """
    row = preflight_degraded_row_fields_from_row(story if isinstance(story, dict) else {})
    if not row["preflight_degraded"]:
        block = (audit_data or {}).get("preflight")
        row = preflight_degraded_row_fields_from_audit(block)

    if not row["preflight_degraded"]:
        return None
    return {audit_key: row[row_key] for row_key, audit_key in PREFLIGHT_DEGRADED_ROW_KEYS.items()}


def _preflight_degraded_detail(state: dict | None) -> str:
    """Render the degraded-preflight condition as a status-row note."""
    if not state:
        return ""
    parts = [f"preflight degraded: {state.get('degraded_reason') or 'unknown'}"]
    action = state.get("failure_action")
    if action:
        parts.append(f"action={action}")
    signals = state.get("risk_signals") or []
    if signals:
        parts.append(f"risk signals: {', '.join(signals)}")
    return "; ".join(parts)


def _allocation_detail(allocation: object) -> str:
    """Render the per-story allocation condition for a status row (#2169).

    Only an abnormal condition is rendered: a story that stayed inside its band
    allocation needs no annotation, and adding one to every row would bury the
    ones that mean something. An exceeded allocation is stated with the band's
    expected range so the row says WHY $8 is anomalous, and an exhausted one is
    stated with the sprint headroom that remained.
    """
    if not isinstance(allocation, dict):
        return ""
    status = allocation.get("status")
    if status not in {"allocation_exceeded", "allocation_exhausted"}:
        return ""
    observed = allocation.get("observed_usd")
    cap = allocation.get("allocation_usd")
    parts: list[str] = []
    if isinstance(observed, (int, float)) and isinstance(cap, (int, float)):
        label = "exhausted" if status == "allocation_exhausted" else "over"
        parts.append(f"allocation {label}: ${float(observed):.2f} of ${float(cap):.2f}")
    else:
        parts.append(f"allocation {status}")
    median = allocation.get("median_usd")
    p90 = allocation.get("p90_usd")
    score = allocation.get("complexity_score")
    if isinstance(median, (int, float)) and isinstance(p90, (int, float)):
        parts.append(f"score {score} band median ${float(median):.2f} / p90 ${float(p90):.2f}")
    remaining = allocation.get("sprint_remaining_usd")
    if isinstance(remaining, (int, float)):
        parts.append(f"sprint remaining ${float(remaining):.2f}")
    return "; ".join(parts)


def read_completed_status(
    summary_path: Path,
    project_root: Path | None = None,
) -> list[StoryStatusEntry]:
    """Parse a sprint-summary.yaml and return per-story status entries.

    Enriches each entry with ``bundle_candidate`` and ``batch_group`` read from
    the per-story coordinator audit at ``<sprint-log-dir>/<slug>/audit.yaml``.
    The summary entry wins for ``batch_group`` when it carries one; the audit is
    the fallback for summaries written before the story's group was stamped.

    ``project_root`` is optional because the resume records that answer "what
    does this story still owe" live under ``<project_root>/.forge``, which a
    summary path alone cannot locate.  Omit it and the re-entry fields stay
    empty; every other field is unaffected.
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
        cost_usd = _story_cost_usd(story)

        status = _outcome_to_status(outcome)
        phase = _terminal_phase(
            outcome,
            list(story.get("depends_on") or []),
            _nonempty_str(story.get("last_phase")),
        )

        bundle_candidate = False
        _summary_batch_group = story.get("batch_group")
        batch_group = (
            _summary_batch_group
            if isinstance(_summary_batch_group, str) and _summary_batch_group
            else None
        )
        complexity = None
        complexity_score: int | None = None
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
                            if batch_group is None:
                                batch_group = _nonempty_str(preflight.get("batch_group"))
                            complexity = _normalize_complexity(preflight.get("complexity"))
                            complexity_score = _normalize_complexity_score(
                                preflight.get("complexity_score")
                            )
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
        outstanding_phases, reentry_note = _reentry_display(project_root, slug)

        entries.append(
            StoryStatusEntry(
                slug=slug,
                path=path,
                status=status,
                phase=phase,
                cost_usd=cost_usd,
                blocked_by=blocked_by,
                bundle_candidate=bundle_candidate,
                batch_group=batch_group,
                elapsed_seconds=_elapsed_seconds_from_bounds(
                    story.get("started_at"),
                    story.get("finished_at"),
                ),
                stage=stage,
                detail=detail,
                complexity=complexity,
                complexity_score=complexity_score,
                model=model_val,
                outstanding_phases=outstanding_phases,
                reentry_note=reentry_note,
            )
        )

    return entries


#: Statuses that describe work the sprint process was actively advancing. When
#: that process is gone, none of them can still be true.
_IN_FLIGHT_STATUSES = {"running"}


def mark_interrupted_entries(entries: list[StoryStatusEntry]) -> list[StoryStatusEntry]:
    """Rewrite in-flight entries for a run whose owning process is gone.

    Live state records the last phase a story reached; nothing rewrites those
    entries when the sprint process dies, so a killed story keeps reading as
    ``running``. Work that was interrupted is not work that is progressing —
    report it as ``interrupted``, preserving the last known phase as history
    rather than as current progress.
    """
    reconciled: list[StoryStatusEntry] = []
    for entry in entries:
        if entry.status not in _IN_FLIGHT_STATUSES:
            reconciled.append(entry)
            continue
        last_detail = (entry.detail or "").strip()
        detail = "interrupted — sprint process is no longer running"
        if last_detail:
            detail = f"{detail}; last: {last_detail}"
        reconciled.append(replace(entry, status="interrupted", detail=detail))
    return reconciled


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
    # Read the pending checkpoints once per snapshot: a gate waiting on a person
    # writes one, and it is the only live record of that wait (#2313).
    has_in_flight = any(
        isinstance(s, dict) and s.get("status") in _IN_FLIGHT_STATUSES for s in stories_data
    )
    pending_records = _live_pending_records(project_root) if has_in_flight else []
    run_identity = _run_identity(run_id, project_root, state_path) if pending_records else set()
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
        if status_val == "failed":
            last_phase_str = _nonempty_str(story.get("last_phase"))
            if last_phase_str:
                phase_display = last_phase_str
        stage, detail, complexity = _stage_and_detail_from_live_story(story)
        # A gate holding this story for a person outranks whatever the phase
        # would otherwise render: the phase says what the story was doing, the
        # checkpoint says what it is waiting for and who can clear it (#2313).
        if status_val in _IN_FLIGHT_STATUSES and pending_records:
            pending_entry = _select_pending_for_story(
                pending_records, slug, run_identity, project_root
            )
            if pending_entry is not None:
                stage = OPERATOR_DECISION_STAGE
                detail = _pending_decision_display(pending_entry)
        complexity_score = _normalize_complexity_score(story.get("complexity_score"))

        if status_val in {"skipped", "blocked", "operator-action", "decomposed"}:
            model_val: str | None = None
        else:
            model_raw = story.get("current_model")
            model_val = model_raw if isinstance(model_raw, str) and model_raw else None

        _detail_dict = story.get("detail")
        _last_event_ts: float | None = None
        if isinstance(_detail_dict, dict):
            _ts = _detail_dict.get("last_reviewer_event_ts")
            if isinstance(_ts, (int, float)):
                _last_event_ts = float(_ts)

        outstanding_phases, reentry_note = _reentry_display(project_root, slug)

        entries.append(
            StoryStatusEntry(
                slug=slug,
                path=story.get("path", slug),
                status=status_val,
                phase=phase_display,
                cost_usd=_story_cost_usd(story),
                blocked_by=blocked_by_val,
                bundle_candidate=bool(story.get("bundle_candidate", False)),
                batch_group=_nonempty_str(story.get("batch_group")),
                elapsed_seconds=_elapsed_seconds_from_live_story(story),
                stage=stage,
                detail=detail,
                complexity=complexity,
                complexity_score=complexity_score,
                model=model_val,
                last_event_ts=_last_event_ts,
                outstanding_phases=outstanding_phases,
                reentry_note=reentry_note,
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
            if outcome in {"SKIPPED", "DROPPED", "ALREADY_DONE"} or depends_on:
                _, detail, _ = _stage_and_detail_from_completed_story(story, None)
            outstanding_phases, reentry_note = _reentry_display(project_root, slug)
            entries.append(
                StoryStatusEntry(
                    slug=slug,
                    path=story.get("path", slug),
                    status=_outcome_to_status(outcome),
                    phase=_terminal_phase(
                        outcome,
                        depends_on,
                        _nonempty_str(story.get("last_phase")),
                    ),
                    cost_usd=_story_cost_usd(story),
                    blocked_by=depends_on,
                    bundle_candidate=False,
                    batch_group=_nonempty_str(story.get("batch_group")),
                    elapsed_seconds=_elapsed_seconds_from_bounds(
                        story.get("started_at"),
                        story.get("finished_at"),
                    ),
                    stage=_format_terminal_stage(story.get("iteration_usage")),
                    detail=detail,
                    complexity=None,
                    outstanding_phases=outstanding_phases,
                    reentry_note=reentry_note,
                )
            )
            seen_slugs.add(slug)
    return entries
