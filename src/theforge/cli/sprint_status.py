"""forge sprint-status subcommand — per-story phase display for a sprint run."""

from __future__ import annotations

import re
import shutil
import sys
import textwrap
from pathlib import Path

from theforge.cli.reentry_display import issue_cost_line
from theforge.cli.shared import _find_config, load_config_checked
from theforge.coordinator.issue_cost import issue_number_from_slug
from theforge.coordinator.util import _fmt_cost_total

#: Width of the STATUS column — wide enough for the longest status label
#: ("interrupted") so a killed story does not push the row out of alignment.
_STATUS_WIDTH = 11

_DETAIL_REF_RE = re.compile(r"#(\d+)")

#: Places both sides of the rows-versus-recorded-spend comparison are persisted
#: to. Comparing at exactly that precision is what separates serialization noise
#: from a real decrease: a cent is not rounding, it is a cent, and a run that
#: reports one cent less than it already spent is the same defect at a smaller
#: scale (#2922).
_RECORDED_SPEND_PRECISION = 4

#: Slug -> issue-number normalization is shared with the issue-cost aggregate so
#: the number this view resolves a title for and the number that view sums runs
#: for can never diverge (#2365).
_issue_number_from_slug = issue_number_from_slug


def _ensure_titles(entries: list, project_root: Path, cache: dict) -> None:
    """Populate ``cache`` (issue number -> title | None) for uncached entries.

    Best-effort: collects issue numbers from each entry's path/slug and its
    ``blocked_by`` items and resolves the ones absent from ``cache`` via the
    sprint query primitive.

    Fetches are done one number at a time so a single unresolvable issue cannot
    discard the titles that did resolve — ``fetch_issues_by_numbers`` raises for
    the whole batch when any requested number is missing. A number confirmed
    absent from the repository is cached as ``None`` (a negative sentinel) so it
    is not re-fetched on subsequent ``--watch`` frames; a transient ``gh``
    failure is left uncached so a later frame can retry.
    """
    from theforge.sprint.query import fetch_issues_by_numbers

    numbers: set[int] = set()
    for entry in entries:
        path = getattr(entry, "path", getattr(entry, "slug", "")) or ""
        num = _issue_number_from_slug(path)
        if num is not None:
            numbers.add(num)
        for item in getattr(entry, "blocked_by", None) or []:
            dep = _issue_number_from_slug(str(item))
            if dep is not None:
                numbers.add(dep)

    missing = sorted(n for n in numbers if n not in cache)
    for number in missing:
        try:
            issues = fetch_issues_by_numbers([number], project_root)
        except RuntimeError as exc:
            # Genuinely-absent numbers get a negative sentinel so they are not
            # retried every frame; transient gh failures stay uncached to retry.
            if "not found in this repository" in str(exc):
                cache[number] = None
            continue
        except Exception:
            continue
        for issue in issues:
            if issue.get("number") == number and isinstance(issue.get("title"), str):
                cache[number] = issue["title"]


def _format_story_cell(path: str, cache: dict) -> str:
    """Return ``#N <title>`` when the slug resolves to a cached title, else path."""
    num = _issue_number_from_slug(path)
    if num is not None:
        title = cache.get(num)
        if title:
            return f"#{num} {title}"
    return path


def _enrich_detail(detail: str, cache: dict) -> str:
    """Append cached titles after each ``#N`` token in a detail string."""
    if not detail:
        return detail

    def _repl(match: "re.Match") -> str:
        token = match.group(0)
        number = int(match.group(1))
        title = cache.get(number)
        return f"{token} {title}" if title else token

    return _DETAIL_REF_RE.sub(_repl, detail)


def display_sprint_status(run_id: str, project_root: Path, title_cache: dict | None = None) -> int:
    """Display per-story status for a sprint run.

    Handles live (PID file present), completed (sprint-summary.yaml), and
    crashed (PID gone, .state still present) sprints.
    Returns 0 on success, 1 if no sprint data is found.

    ``title_cache`` maps issue number -> GitHub title; when None a fresh local
    dict is used (single-shot invocation). Watch mode passes a persistent dict
    so titles fetched on frame 1 are reused on every subsequent frame.
    """
    import yaml

    if title_cache is None:
        title_cache = {}

    from theforge.sprint.state_writer import is_terminal_sprint_phase
    from theforge.sprint.status_reader import (
        find_live_state_path,
        find_sprint_summary,
        mark_interrupted_entries,
        read_completed_status,
        read_live_status,
    )

    # Determine if the sprint is live (PID file present).
    pid_file = project_root / ".forge" / "runs" / f"{run_id}.pid"
    is_live = pid_file.exists()

    entries = None
    sprint_name = ""
    unexpected_end = False
    terminal_outcome: str | None = None
    terminal_cause: str | None = None
    total_cost_usd: float | None = None
    # False once any story's cost is known to be unmeasured — the header then
    # reports the measured lower bound as such instead of as the sprint cost.
    cost_complete: bool = True
    cost_measured_usd: float | None = None
    # What the run recorded about its own standing against the cap, when it
    # recorded one. Absent for runs that predate the field, which fall back to
    # comparing the cost this command is about to display (#2547).
    budget_status_recorded: str | None = None
    budget_overrun_recorded: float | None = None
    budget_spend_recorded: float | None = None
    duration_seconds: float | None = None
    sprint_phase: str | None = None
    sprint_phase_detail: str | None = None
    sprint_phase_started_at: str | None = None
    base_branch: str | None = None
    budget_usd: float | None = None
    max_parallel: int | None = None

    if is_live:
        entries = read_live_status(run_id, project_root)
        state_path = find_live_state_path(run_id, project_root)
        if state_path is not None:
            sprint_name = _read_sprint_name_from_state(state_path)
            meta = _read_sprint_meta_from_state(state_path)
            sprint_phase = meta.get("sprint_phase")
            sprint_phase_detail = meta.get("sprint_phase_detail")
            sprint_phase_started_at = meta.get("sprint_phase_started_at")
            base_branch = meta.get("base_branch")
            budget_usd = meta.get("budget_usd")
            max_parallel = meta.get("max_parallel")
            budget_status_recorded = meta.get("budget_status")
            budget_overrun_recorded = meta.get("budget_overrun_usd")
            budget_spend_recorded = meta.get("budget_spend_usd")
        # Approximate elapsed from the process start time via detach
        try:
            from theforge import detach as _detach

            parsed = _detach._read_pid_file(pid_file)
            if parsed:
                _, slug = parsed
                run_st = _detach.read_run_status(run_id, slug, project_root)
                elapsed_s = run_st.get("elapsed_seconds")
                if elapsed_s is not None:
                    duration_seconds = elapsed_s
        except Exception:
            pass
    else:
        # PID file gone. A leftover .state file means the run did not tear down
        # its live state — but that is a *crash* only when no terminal outcome
        # marker was written. A run that wrote <run_id>.ended terminated
        # deliberately (completed/stopped/orphaned); its lingering .state must
        # not be reported as an unexpected end.
        state_path = project_root / ".forge" / "runs" / f"{run_id}.state"
        if state_path.exists():
            from theforge import detach as _detach

            record = _detach.read_run_ended_record(run_id, project_root)
            if record is not None:
                terminal_outcome, terminal_cause = record
            unexpected_end = terminal_outcome is None
            entries = read_live_status(run_id, project_root)
            if entries is not None:
                # The owning process is gone: no story can still be advancing,
                # whatever the last phase written to .state says.
                entries = mark_interrupted_entries(entries)
                sprint_name = _read_sprint_name_from_state(state_path)
                # A stopped or crashed run left behind what it recorded about its
                # own spend. Read it: it is the only thing that can contradict
                # the per-story rows this command is about to sum (#2922).
                _stopped_meta = _read_sprint_meta_from_state(state_path)
                budget_usd = _stopped_meta.get("budget_usd")
                budget_status_recorded = _stopped_meta.get("budget_status")
                budget_overrun_recorded = _stopped_meta.get("budget_overrun_usd")
                budget_spend_recorded = _stopped_meta.get("budget_spend_usd")

    # For crashed sprints with an unreadable state file, show the banner with
    # an empty story list rather than falling through to sprint-summary (which
    # won't exist for crashed sprints).
    if entries is None and (unexpected_end or terminal_outcome):
        entries = []

    # Fall back to completed sprint-summary.yaml
    if entries is None:
        summary_path = find_sprint_summary(run_id, project_root)
        if summary_path is None:
            print(
                f"No sprint data found for run ID '{run_id}'.\n"
                "Is the run ID correct? Use 'forge status' to see active runs.",
                file=sys.stderr,
            )
            return 1
        entries = read_completed_status(summary_path, project_root)
        sprint_name = _read_sprint_name_from_summary(summary_path)
        # Read aggregate metrics from the completed summary.
        try:
            with open(summary_path, encoding="utf-8") as f:
                summary_data = yaml.safe_load(f) or {}
            sp = summary_data.get("sprint", {})
            total_cost_usd = sp.get("total_cost_usd")
            cost_complete = sp.get("cost_complete", True) is not False
            _measured = sp.get("total_cost_measured_usd")
            cost_measured_usd = _measured if isinstance(_measured, (int, float)) else None
            duration_seconds = sp.get("duration_seconds")
            # A completed sprint reports its own cap and how it finished against
            # it. Read both: without the budget there is nothing to compare the
            # cost to, which is how a 41%-over run once read as unremarkable
            # (#2547).
            _summary_budget = sp.get("budget_usd")
            if isinstance(_summary_budget, (int, float)):
                budget_usd = float(_summary_budget)
            _summary_status = sp.get("budget_status")
            if isinstance(_summary_status, str):
                budget_status_recorded = _summary_status
            _summary_overrun = sp.get("budget_overrun_usd")
            if isinstance(_summary_overrun, (int, float)):
                budget_overrun_recorded = float(_summary_overrun)
            _summary_verification = sp.get("budget_verification_spend_usd")
            if isinstance(_summary_verification, (int, float)):
                budget_spend_recorded = float(_summary_verification)
        except Exception:
            pass

    # For live/crashed sprints compute total cost from story entries. A story
    # whose cost is unknown makes the sprint total incomplete: sum only the
    # measured ones and report the result as a lower bound (#1992).
    if total_cost_usd is None and cost_complete and entries:
        _entry_costs = [getattr(e, "cost_usd", 0.0) for e in entries]
        _measured_sum = sum(c for c in _entry_costs if c is not None)
        if all(c is not None for c in _entry_costs):
            total_cost_usd = _measured_sum
        else:
            cost_complete = False
            cost_measured_usd = _measured_sum

    # A run's recorded spend is a high-water mark, not a running guess: money it
    # told an operator it had spent stays spent. When a finished run's per-story
    # rows sum to LESS than that figure, the two contradict each other and
    # neither is the sprint's cost — the run lost part of its own accounting
    # somewhere (#2922). Report the shortfall as unreconciled rather than let the
    # smaller, internally-consistent number stand as a settled total; an
    # under-reported total is what lets a cap bind later than the operator
    # believes it does. Only for runs that have finished: a live run's rows
    # legitimately trail its ledger while work is in flight.
    #
    # The comparison is made at the precision both figures are persisted to, not
    # against a tolerance: a tolerance would accept exactly the small decreases
    # it is hardest to notice, and a cent that vanished is still a cent that
    # vanished.
    cost_unreconciled_usd: float | None = None
    _recorded_is_number = isinstance(budget_spend_recorded, (int, float)) and not isinstance(
        budget_spend_recorded, bool
    )
    if not is_live and total_cost_usd is not None and _recorded_is_number:
        _recorded = round(float(budget_spend_recorded), _RECORDED_SPEND_PRECISION)
        if _recorded > round(total_cost_usd, _RECORDED_SPEND_PRECISION):
            cost_unreconciled_usd = _recorded
            cost_measured_usd = total_cost_usd
            total_cost_usd = None
            cost_complete = False

    # ── Header ───────────────────────────────────────────────────────────
    # A PID file is evidence a process exists, not evidence the sprint is still
    # advancing: the runner writes the terminal sprint_phase before its own
    # (possibly slow) wrap-up, and until it exits the header would otherwise
    # read "[live] phase: failed" alongside nothing but terminal stories
    # (#2013). The persisted terminal phase is the stronger claim — report it.
    if is_live and is_terminal_sprint_phase(sprint_phase):
        state_label = str(sprint_phase)
    elif is_live:
        state_label = "live"
    elif unexpected_end:
        state_label = "crashed"
    elif terminal_outcome:
        # A terminal marker survived alongside .state: report the recorded
        # outcome (completed/stopped/orphaned) rather than crash wording.
        state_label = terminal_outcome
    else:
        state_label = "completed"

    # Cost and budget stop being two independent numbers here: whichever spend
    # figure the header is about to show is compared against the cap, and an
    # overrun is stated next to it rather than left for the operator to notice
    # (#2547).
    overrun_marker = _budget_overrun_marker(
        budget_usd=budget_usd,
        displayed_cost_usd=total_cost_usd if total_cost_usd is not None else cost_measured_usd,
        recorded_status=budget_status_recorded,
        recorded_overrun_usd=budget_overrun_recorded,
        recorded_spend_usd=budget_spend_recorded,
    )

    header_parts: list[str] = [f"Sprint: {sprint_name}  run: {run_id}  [{state_label}]"]
    if sprint_phase:
        header_parts.append(
            _format_sprint_phase(sprint_phase, sprint_phase_detail, sprint_phase_started_at)
        )
    if total_cost_usd is not None:
        header_parts.append(f"cost: ${total_cost_usd:.2f}{overrun_marker}")
    elif cost_unreconciled_usd is not None:
        # Name both numbers, at the precision the gap was detected at. The
        # operator's question is which one to trust, and the honest answer is
        # neither — so neither is presented as the total, and a cent-sized gap is
        # not rounded into invisibility by the display.
        _rows_str = f"{cost_measured_usd or 0.0:.2f}"
        _recorded_str = f"{cost_unreconciled_usd:.2f}"
        if _rows_str == _recorded_str:
            _rows_str = f"{cost_measured_usd or 0.0:.4f}"
            _recorded_str = f"{cost_unreconciled_usd:.4f}"
        header_parts.append(
            f"cost: unreconciled (stories ${_rows_str} < "
            f"recorded ${_recorded_str}){overrun_marker}"
        )
    elif not cost_complete:
        header_parts.append(f"cost: {_fmt_cost_total(None, cost_measured_usd)}{overrun_marker}")
    if duration_seconds is not None:
        if is_live:
            header_parts.append(f"elapsed: {int(duration_seconds // 60)}m")
        else:
            header_parts.append(f"duration: {int(duration_seconds // 60)}m")
    print("  ".join(header_parts))

    # Second line: sprint configuration context (base branch, budget, parallel).
    # These let the operator confirm the sprint launched against the right
    # branch and ceiling without scrolling back to the launch banner.
    config_parts: list[str] = []
    if base_branch:
        config_parts.append(f"base: {base_branch}")
    if budget_usd is not None:
        config_parts.append(
            f"budget: ${float(budget_usd):.2f}" + ("  ⚠ exceeded" if overrun_marker else "")
        )
    if max_parallel is not None:
        config_parts.append(f"parallel: {max_parallel}")
    if config_parts:
        print("  ".join(config_parts))

    if unexpected_end:
        print("  ⚠  Sprint ended unexpectedly (PID file missing, state file present)")
    elif terminal_outcome == "failed":
        print("  ⚠  Sprint terminated abnormally — it did not complete")
    if terminal_cause:
        print(f"  cause: {terminal_cause}")
    print()

    if not entries:
        print("  No stories found.")
        return 0

    # Best-effort GitHub title fetch (cached for --watch sessions).
    _ensure_titles(entries, project_root, title_cache)

    # ── Story rows ───────────────────────────────────────────────────────
    status_icons = {
        "done": "✓",
        "running": "▸",
        "waiting": "○",
        "failed": "✗",
        "interrupted": "⚠",
        "skipped": "⊘",
        "blocked": "⊘",
        "operator-action": "⊘",
        # Neither ✓ nor ✗: the gate asked and the answer was to split it.
        "decomposed": "⤺",
    }

    # Column header
    stage_width = 20
    header = (
        f"  {'STORY':<28}  {'STATUS':<{_STATUS_WIDTH}}  {'PHASE':<12}  {'MODEL':<24}  "
        f"{'STAGE':<{stage_width}}  {'COMPLEXITY':<10}  {'COST':>7}  {'ELAPSED':>7}  DETAIL"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    # Three scheduling primitives, three renderings. Conflict bundles say
    # "these overlap and were implemented together to avoid merge pain"; batch
    # groups say "these are independent and were packed into one dev assignment
    # for cost". Collapsing them into one section would hide which decision the
    # scheduler actually made. A story in a bundle is never in a batch group
    # (compute_batch_groups excludes bundled slugs), so the bundle test wins.
    bundle_entries = [e for e in entries if e.bundle_candidate]
    batch_entries = [e for e in entries if not e.bundle_candidate and e.batch_group]
    regular_entries = [e for e in entries if not e.bundle_candidate and not e.batch_group]

    if bundle_entries:
        bundle_slugs = "  ".join(e.slug for e in bundle_entries)
        print(f"[bundle: {bundle_slugs}]")
        for entry in bundle_entries:
            _print_story_line(
                entry,
                status_icons,
                indent=2,
                title_cache=title_cache,
                project_root=project_root,
            )
        print()

    if batch_entries:
        groups: dict[str, list] = {}
        for entry in batch_entries:
            groups.setdefault(str(entry.batch_group), []).append(entry)
        for group_id in sorted(groups):
            group_slugs = "  ".join(e.slug for e in groups[group_id])
            print(f"[batch: {group_id}  {group_slugs}]")
            for entry in groups[group_id]:
                _print_story_line(
                    entry,
                    status_icons,
                    indent=2,
                    title_cache=title_cache,
                    project_root=project_root,
                )
            print()

    for entry in regular_entries:
        _print_story_line(
            entry, status_icons, indent=0, title_cache=title_cache, project_root=project_root
        )

    return 0


def cmd_sprint_status(args: object) -> int:
    """Show per-story status for a sprint run."""
    from theforge.config import load_config

    run_id: str = getattr(args, "run_id", "")
    if not run_id:
        print("run_id is required", file=sys.stderr)
        return 1

    config_path = _find_config()
    if config_path is None or not config_path.exists():
        print("forge.yaml not found.", file=sys.stderr)
        return 1
    config = load_config_checked(
        config_path,
        loader=load_config,
        emit_startup_auth_warnings=False,
    )
    project_root = config.project_root

    return display_sprint_status(run_id, project_root)


def _read_sprint_meta_from_state(state_path: object) -> dict:
    """Return sprint-level metadata (phase, base_branch, budget, parallel).

    Empty dict on any error or missing fields. Lets the live-status header
    surface watch-mode context during the pre-preflight window when the
    SprintStateWriter has not yet replaced the bootstrap state file.
    """
    import yaml  # noqa: PLC0415

    try:
        with open(Path(str(state_path)), encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict = {}
    if isinstance(data.get("sprint_phase"), str):
        out["sprint_phase"] = data["sprint_phase"]
    if isinstance(data.get("sprint_phase_detail"), str):
        out["sprint_phase_detail"] = data["sprint_phase_detail"]
    if isinstance(data.get("sprint_phase_started_at"), str):
        out["sprint_phase_started_at"] = data["sprint_phase_started_at"]
    if isinstance(data.get("base_branch"), str):
        out["base_branch"] = data["base_branch"]
    if isinstance(data.get("budget_usd"), (int, float)):
        out["budget_usd"] = float(data["budget_usd"])
    if isinstance(data.get("max_parallel"), int):
        out["max_parallel"] = data["max_parallel"]
    # The runner's own verdict on the cap. Preferred over a comparison derived
    # here because it accounts for spend the story rows never carry — carried
    # cost from an earlier generation, and passes that belong to no story
    # (#2547).
    if isinstance(data.get("budget_status"), str):
        out["budget_status"] = data["budget_status"]
    if isinstance(data.get("budget_overrun_usd"), (int, float)):
        out["budget_overrun_usd"] = float(data["budget_overrun_usd"])
    if isinstance(data.get("budget_spend_usd"), (int, float)):
        out["budget_spend_usd"] = float(data["budget_spend_usd"])
    return out


def _budget_overrun_marker(
    *,
    budget_usd: float | None,
    displayed_cost_usd: float | None,
    recorded_status: str | None = None,
    recorded_overrun_usd: float | None = None,
    recorded_spend_usd: float | None = None,
) -> str:
    """Return the over-budget marker to append to the cost and budget fields.

    Empty when there is no cap, or the run is inside it. The run's own recorded
    verdict wins where it exists — it saw spend this view cannot (cost carried
    from an earlier generation, passes that belong to no story) — and the
    displayed cost is the fallback, so a run that recorded nothing is still
    compared against its cap rather than printed beside it (#2547).
    """
    from theforge.sprint.budget import (  # noqa: PLC0415
        BUDGET_STATUS_OVER,
        budget_overrun_usd,
    )

    if budget_usd is None or float(budget_usd) <= 0.0:
        return ""
    spend_candidates = [
        c for c in (displayed_cost_usd, recorded_spend_usd) if isinstance(c, (int, float))
    ]
    derived = (
        budget_overrun_usd(budget_usd=float(budget_usd), spend_usd=max(spend_candidates))
        if spend_candidates
        else 0.0
    )
    if recorded_status == BUDGET_STATUS_OVER:
        overrun = float(recorded_overrun_usd or 0.0) or derived
    elif derived > 0.0:
        overrun = derived
    else:
        return ""
    return f"  ⚠ OVER BUDGET by ${overrun:.2f}"


def _read_sprint_name_from_state(state_path: object) -> str:
    """Read sprint_name from a .state YAML file.  Returns '' on any error."""
    import yaml  # noqa: PLC0415

    try:
        with open(Path(str(state_path)), encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data.get("sprint_name", "") if isinstance(data, dict) else ""
    except Exception:
        return ""


def _read_sprint_name_from_summary(summary_path: object) -> str:
    """Read sprint.name from a sprint-summary.yaml file.  Returns '' on any error."""
    import yaml  # noqa: PLC0415

    try:
        with open(Path(str(summary_path)), encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict):
            return data.get("sprint", {}).get("name", "")
        return ""
    except Exception:
        return ""


def _print_story_line(
    entry: object,
    status_icons: dict,
    indent: int,
    title_cache: dict | None = None,
    project_root: Path | None = None,
) -> None:
    """Print a single story line for forge sprint-status.

    The COST column is this run's spend and stays that. What the *issue* has
    cost across every recorded run is disclosed below the columns when there is
    more than one, because the column figure alone reads as the cost of the
    issue when it is one attempt's share of it (#2365).
    """
    cache = title_cache if title_cache is not None else {}
    icon = status_icons.get(getattr(entry, "status", "waiting"), "?")
    path = getattr(entry, "path", getattr(entry, "slug", ""))
    status = getattr(entry, "status", "waiting")
    phase = getattr(entry, "phase", None)
    stage = getattr(entry, "stage", "")
    cost_usd = getattr(entry, "cost_usd", 0.0)
    elapsed_s = getattr(entry, "elapsed_seconds", None)
    detail = getattr(entry, "detail", "")
    complexity = getattr(entry, "complexity", None)
    complexity_score = getattr(entry, "complexity_score", None)
    model = getattr(entry, "model", None)

    phase_str = phase if phase else "—"
    stage_str = stage if stage else "—"
    if isinstance(complexity_score, int):
        complexity_str = str(complexity_score)
    elif complexity:
        complexity_str = complexity
    else:
        complexity_str = "—"
    model_str = model if model else "—"
    if len(model_str) > 24:
        model_str = model_str[:24]
    if cost_usd is None:
        # Cost-unknown must not render like "no spend": the story ran on a
        # transport that reported no cost (#1992).
        cost_str = "unknown"
    else:
        cost_str = f"${cost_usd:.2f}" if cost_usd else "   —"
    elapsed_str = _format_story_elapsed(elapsed_s)

    prefix = " " * indent
    detail_width = _detail_column_width(indent)
    story_cell = _format_story_cell(path, cache)
    detail = _enrich_detail(detail, cache)
    path_lines = _wrap_cell(story_cell, 28)
    phase_lines = _wrap_cell(phase_str, 12)
    model_lines = _wrap_cell(model_str, 24)
    stage_width = 20
    stage_lines = _wrap_cell(stage_str, stage_width)
    detail_lines = _wrap_cell(detail if detail else "—", detail_width)

    line_count = max(
        len(path_lines),
        len(phase_lines),
        len(model_lines),
        len(stage_lines),
        len(detail_lines),
    )
    for index in range(line_count):
        icon_cell = icon if index == 0 else " "
        status_cell = status if index == 0 else ""
        complexity_cell = complexity_str if index == 0 else ""
        cost_cell = cost_str if index == 0 else ""
        elapsed_cell = elapsed_str if index == 0 else ""
        line = (
            f"{icon_cell} {path_lines[index] if index < len(path_lines) else '':<28}  "
            f"{status_cell:<{_STATUS_WIDTH}}  "
            f"{phase_lines[index] if index < len(phase_lines) else '':<12}  "
            f"{model_lines[index] if index < len(model_lines) else '':<24}  "
            f"{stage_lines[index] if index < len(stage_lines) else '':<{stage_width}}  "
            f"{complexity_cell:<10}  {cost_cell:>7}  {elapsed_cell:>7}  "
            f"{detail_lines[index] if index < len(detail_lines) else ''}"
        )
        print(f"{prefix}{line}")

    # Re-entry disclosure, below the columns rather than in DETAIL: it is about
    # what has *not* run and what running each re-entry path would do, which the
    # detail column (last-known progress) cannot say without lying about it.
    outstanding = list(getattr(entry, "outstanding_phases", None) or [])
    if outstanding:
        print(f"{prefix}    outstanding: {', '.join(outstanding)}")
    reentry_note = getattr(entry, "reentry_note", "")
    if reentry_note:
        print(f"{prefix}    re-entry: {reentry_note}")
    slug = str(getattr(entry, "slug", "") or "")
    for line in issue_cost_line(project_root, slug, indent=f"{prefix}    "):
        print(line)


def _format_sprint_phase(
    sprint_phase: str,
    detail: str | None,
    started_at: str | None,
) -> str:
    """Render the header's phase cell, naming what a long phase is working on.

    A phase name alone cannot distinguish a gate that has been running for
    fifteen minutes from a sprint that is wedged, so a phase that publishes a
    target and a start time gets both reported inline (#2014).
    """
    from theforge.sprint.status_reader import elapsed_seconds_since

    parts = [part for part in (detail,) if part]
    elapsed = elapsed_seconds_since(started_at)
    if elapsed is not None:
        # Sub-minute phases matter here in a way they do not for story rows: the
        # first seconds of a gate are exactly when an operator is watching.
        parts.append(f"{int(elapsed)}s" if elapsed < 60 else _format_story_elapsed(elapsed))
    if not parts:
        return f"phase: {sprint_phase}"
    return f"phase: {sprint_phase} ({', '.join(parts)})"


def _format_story_elapsed(elapsed_s: float | None) -> str:
    """Format story elapsed time for display."""
    if elapsed_s is None:
        return "—"
    total_seconds = max(0, int(elapsed_s))
    if total_seconds >= 3600:
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        return f"{hours}h {minutes}m"
    return f"{total_seconds // 60}m"


def _detail_column_width(indent: int) -> int:
    """Return an adaptive width for DETAIL so values wrap instead of truncating."""
    terminal_width = shutil.get_terminal_size((140, 20)).columns
    stage_width = 20
    fixed_width = (
        indent
        + 2
        + 28
        + 2
        + _STATUS_WIDTH
        + 2
        + 12
        + 2
        + 24
        + 2
        + stage_width
        + 2
        + 10
        + 2
        + 7
        + 2
        + 7
        + 2
    )
    return max(20, terminal_width - fixed_width)


def _wrap_cell(value: str, width: int) -> list[str]:
    """Wrap a cell value to the requested width without dropping content."""
    if width <= 0:
        return [value] if value else [""]
    wrapped = textwrap.wrap(
        value,
        width=width,
        break_long_words=False,
        break_on_hyphens=False,
    )
    return wrapped or [""]


def register_parser(subparsers: object) -> None:
    """Register the 'sprint-status' subcommand parser."""
    p = subparsers.add_parser(
        "sprint-status",
        help="Show per-story status for a sprint run",
    )
    p.add_argument("run_id", help="Sprint run ID (from 'forge status')")
