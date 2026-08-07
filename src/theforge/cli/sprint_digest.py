"""Postmortem digest renderer for completed sprints.

``forge status`` has two renderings that read the *same* on-disk state:

* the wide telemetry table (``cli/sprint_status.py``) — the right view while a
  sprint is live and the operator is monitoring "what is stuck right now";
* this postmortem digest — the right view once a sprint is complete and the
  operator's question shifts from monitoring to recovery: what landed, what
  failed (and why), what produced partial value, what was skipped, and what to
  do next.

The digest is a pure *renderer*. Its interface to the rest of the system is the
artifacts it reads — ``sprint-summary.yaml`` (the LANDED accounting),
``sprint-rca.yaml`` (the recovery signals), and the per-story resume record
(what re-entering the story would do). It never invokes the RCA engine and never
touches live coordinator state; that keeps it implementable and testable
independently of both the live renderer and the classifier.

Layout for a completed sprint with one or more non-DONE stories::

    SPRINT <name>  ·  completed  ·  <duration>m  ·  $<cost>

    LANDED (n of m)
      ✓ #NNNN  $cost  elapsed  <title>

    ALREADY LANDED (skipped as merged) (n)
      ✓ #NNNN  —  —  <title>
           landed before this run — skipped as already merged

    OUTSTANDING (n)
      ✗ #NNNN  REVIEW cycle 2 not run
           re-entry: forge review runs REVIEW cycle 2; forge sprint --resume
                     recovers escalation decision <d> and skips REVIEW

    FAILED — <primary_failure_class> (n)
      ✗ #NNNN  $cost  elapsed  <title>
           primary:      <primary_failure_class>
           contributing: <factor>, <factor>
           evidence:     <excerpt> at <source>

    SKIPPED / INTAKE (n)
      ⊘ #NNNN  —  —  <title>
           <primary_failure_class> — <detail>

    PARTIAL VALUE (n)
      ◐ #NNNN  <partial value>; <partial value>

    NEXT
      - <recommended next action>

A completed sprint whose stories all landed renders the tighter LANDED-only
digest with none of the recovery sections.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from theforge.cli.reentry_display import load_reentry_display
from theforge.sprint.rca import DONE_OUTCOMES, RCA_FILENAME
from theforge.sprint.status_reader import (
    _elapsed_seconds_from_bounds,
    find_sprint_summary,
)

# Primary failure classes whose stories never entered DEV — intake shape drops,
# dependency-blocked skips, and launch-guard collisions. These route to the
# SKIPPED / INTAKE section rather than getting their own FAILED heading, because
# the operator's recovery action is reshaping/scheduling, not failure triage.
#
# ``sprint_state_stranded`` is deliberately NOT in this set: a worktree stranded
# by a prior generation is a recoverable failure whose recovery is
# resume/reconcile (not reshaping/scheduling), so it must render under its own
# FAILED heading via ``_print_failed_by_class`` — the generic recovery path for
# any primary class not routed to SKIPPED / INTAKE.
_SKIPPED_INTAKE_CLASSES = frozenset(
    {
        "intake_shape",
        "dependency_skip",
        "launch_collision",
    }
)

# Baseline evidence rule that every RCA entry carries; suppressed from the
# evidence lines because it only restates the outcome shown on the story row.
_BASELINE_EVIDENCE_RULE = "captured_outcome"
_MAX_EVIDENCE_LINES = 3


def display_sprint_digest(run_id: str, project_root: Path) -> int:
    """Render the postmortem digest for a completed sprint ``run_id``.

    Reads ``sprint-summary.yaml`` and ``sprint-rca.yaml`` from disk only. Returns
    0 on success, 1 when no sprint summary can be located for ``run_id``.
    """
    summary_path = find_sprint_summary(run_id, project_root)
    if summary_path is None:
        print(
            f"No sprint data found for run ID '{run_id}'.\n"
            "Is the run ID correct? Use 'forge status' to see active runs.",
            file=sys.stderr,
        )
        return 1

    summary = _load_yaml(summary_path)
    if not isinstance(summary, dict):
        print(f"Sprint summary for run ID '{run_id}' is unreadable.", file=sys.stderr)
        return 1

    sprint_block = summary.get("sprint") if isinstance(summary.get("sprint"), dict) else {}
    stories = [s for s in (summary.get("stories") or []) if isinstance(s, dict)]

    # ``LANDED`` is scoped to the reporting invocation: only work this run
    # actually produced and landed. Stories whose completion was established
    # *before* this invocation — a prior run's merge/close (issue #1906), or a
    # preflight verdict that the spec was already satisfied (issue #1937) — are
    # split into their own buckets. They still count as succeeded, so they are
    # excluded from ``non_done`` and never trigger the recovery sections.
    already_landed = [s for s in stories if _completion_bucket(s) == "already_landed"]
    already_satisfied = [s for s in stories if _completion_bucket(s) == "already_satisfied"]
    landed = [s for s in stories if _completion_bucket(s) == "landed"]
    non_done = [s for s in stories if _outcome(s) not in DONE_OUTCOMES]

    _print_header(sprint_block, run_id)
    _print_landed(landed, len(stories))
    _print_already_landed(already_landed)
    _print_already_satisfied(already_satisfied)

    # Shape-gate skip categories (issue #1453) are independent of story
    # outcomes: a sprint can land every runnable story yet still have skipped a
    # batch of issues at the gate. Render before the all-DONE early return so
    # those skips (and any stuck-issue patterns) are never hidden.
    _print_shape_gate_skips(summary)

    # All-DONE sprint: tighter LANDED-only digest, no recovery sections.
    if not non_done:
        return 0

    # Before the RCA branch, and before the missing-RCA early return below: a
    # phase the story still owes is a fact about the story, not a classification
    # of why it failed, so it must not depend on an RCA artifact existing. This
    # is the digest's answer to "what will re-entering do" (#2239).
    _print_outstanding(non_done, project_root)

    resolved_run_id = str(sprint_block.get("run_id") or run_id)
    rca = _read_digest_rca(summary_path.parent, resolved_run_id)
    if rca is None:
        # Completed sprint with non-DONE stories but no RCA artifact: point the
        # operator at the on-demand verb and render LANDED only.
        print()
        print(
            f"  No RCA artifact yet — run 'forge rca {run_id}' to classify the "
            f"{len(non_done)} non-DONE stor{'y' if len(non_done) == 1 else 'ies'}."
        )
        return 0

    rca_stories = rca.get("stories") if isinstance(rca.get("stories"), dict) else {}

    _print_failed_by_class(non_done, rca_stories)
    _print_skipped_intake(non_done, rca_stories)
    _print_partial_value(non_done, rca_stories)
    _print_next(stories, rca_stories)
    return 0


# ── Sections ──────────────────────────────────────────────────────────────────


def _print_header(sprint_block: dict, run_id: str) -> None:
    name = str(sprint_block.get("name") or run_id)
    parts = [f"SPRINT {name}", "completed"]
    duration = sprint_block.get("duration_seconds")
    if isinstance(duration, (int, float)) and duration > 0:
        parts.append(f"{int(duration // 60)}m")
    cost = sprint_block.get("total_cost_usd")
    if isinstance(cost, (int, float)):
        parts.append(f"${cost:.2f}")
    print("  ·  ".join(parts))


def _print_landed(landed: list[dict], total: int) -> None:
    print()
    print(f"LANDED ({len(landed)} of {total})")
    if not landed:
        print("  (none)")
        return
    for story in landed:
        print(f"  ✓ {_story_row(story)}")


def _print_already_landed(stories: list[dict]) -> None:
    """Render stories whose work merged/closed before the reporting invocation.

    A resume or re-dispatch that finds a story already merged (or its issue
    already closed) records ALREADY_DONE with ``outcome_source:
    resume_skip_merged`` and zero cost/time. That completion was established by
    an *earlier* run, so counting it under LANDED credits this run with work it
    did not do (issue #1906). It gets its own bucket that names what was
    actually established.
    """
    if not stories:
        return
    print()
    print(f"ALREADY LANDED (skipped as merged) ({len(stories)})")
    for story in stories:
        print(f"  ✓ {_story_row(story)}")
        print("       landed before this run — skipped as already merged")


def _print_already_satisfied(stories: list[dict]) -> None:
    """Render no-op ALREADY_DONE acceptances distinctly from landed changes.

    These stories succeeded without shipping a change — preflight determined the
    spec was already satisfied and no PR merged. Reporting them under LANDED
    would present a no-op acceptance as equivalent to a merged land (issue
    #1937), so they get their own bucket with the preflight reason as detail.
    """
    if not stories:
        return
    print()
    print(f"ALREADY SATISFIED ({len(stories)})")
    for story in stories:
        print(f"  ✓ {_story_row(story)}")
        reason = str(story.get("preflight_reason") or "").strip()
        if reason:
            print(f"       no change needed — {reason}")


def _completion_bucket(story: dict) -> str | None:
    """Classify a succeeded story by *when and how* its completion was established.

    Returns one of:

    * ``"already_landed"`` — completion established before this invocation, by a
      prior run's merge or an issue found already closed (``outcome_source:
      resume_skip_merged``). Zero cost, zero elapsed: this run did not do it.
    * ``"already_satisfied"`` — completion determined by other means, without a
      change shipping: a no-merge preflight verdict that the working tree
      already satisfies the spec (``outcome_source: preflight_verdict``).
    * ``"landed"`` — this invocation produced and landed the work.
    * ``None`` — the story did not succeed.

    ``reexec_reconcile`` deliberately stays in ``"landed"``: a re-exec'd
    generation is part of the *same* parent invocation, so its completion did
    happen in the run being reported.
    """
    if _outcome(story) not in DONE_OUTCOMES:
        return None
    source = str(story.get("outcome_source") or "")
    if source == "resume_skip_merged":
        return "already_landed"
    if source == "preflight_verdict" and not _shipped_a_change(story):
        return "already_satisfied"
    return "landed"


def _shipped_a_change(story: dict) -> bool:
    """True when the story carries merge evidence (merge boolean or landing record)."""
    if story.get("merge"):
        return True
    landing = story.get("landing")
    return bool(isinstance(landing, dict) and landing.get("merged"))


def _print_shape_gate_skips(summary: dict) -> None:
    """Render the shape-gate skip categories + stuck-issue patterns.

    Reads the ``shape_gate_skips`` block the sprint summary writer persisted
    (sourced from the audit substrate). Groups skipped issues by taxonomy
    category — cheapest-to-recover first — so an operator distinguishes a
    stale-label block they fix in seconds from a semantic gate that needs a
    producer from a genuine structural failure. Repeated-block ("stuck") issues
    are flagged prominently: the pattern that surfaced #1135 and #1405 only via
    manual log-walking.
    """
    block = summary.get("shape_gate_skips")
    if not isinstance(block, dict):
        return
    categories = block.get("categories")
    if not isinstance(categories, dict) or not categories:
        return

    total = block.get("total")
    total_str = f" ({total})" if isinstance(total, int) else ""
    print()
    print(f"SHAPE-GATE SKIPS{total_str}")
    for category, rows in categories.items():
        if not isinstance(rows, list) or not rows:
            continue
        label = str(category).replace("_", "-")
        print(f"  {label} ({len(rows)})")
        for row in rows:
            if not isinstance(row, dict):
                continue
            issue = row.get("issue_id")
            code = row.get("reason_code") or "<no code>"
            axis = row.get("four_question_axis")
            axis_str = f"  [{axis}]" if axis else ""
            print(f"    ⊘ #{issue}  {code}{axis_str}")

    stuck = block.get("stuck_issues")
    if isinstance(stuck, list) and stuck:
        threshold = block.get("threshold")
        window = ""
        for item in stuck:
            if not isinstance(item, dict):
                continue
            first = item.get("first_seen")
            last = item.get("last_seen")
            if first and last:
                window = f" ({str(first)[:10]} → {str(last)[:10]})"
            count = item.get("block_count")
            times = f"{count}×" if isinstance(count, int) else "repeatedly"
            print(
                f"  ⚠ STUCK: #{item.get('issue_id')} blocked by "
                f"{item.get('reason_code')} {times} across runs{window}"
            )
        if isinstance(threshold, int):
            print(f"       (stuck threshold: {threshold})")


def _print_outstanding(non_done: list[dict], project_root: Path) -> None:
    """Render phases the stopped stories still owe, and what re-entry would do.

    Reads the coordinator's persisted resume record — a third on-disk artifact
    alongside the summary and the RCA, still no live state and still no engine
    invocation, so the digest stays a pure renderer.

    Without this the postmortem is the *default* rendering for a completed
    sprint (``forge status`` with no run id lands here) and the one that says
    nothing about an unrun review cycle: the operator reads a failure
    classification, reaches for ``forge sprint --resume`` because it reads as
    "continue what you were doing", and never learns it would continue from a
    recorded decision instead of running the review (#2239).
    """
    rows: list[tuple[dict, list[str], str]] = []
    for story in non_done:
        slug = str(story.get("slug") or "")
        loaded = load_reentry_display(project_root, slug)
        if loaded is None:
            continue
        phases, note = loaded
        if not phases:
            continue
        rows.append((story, phases, note))
    if not rows:
        return

    print()
    print(f"OUTSTANDING ({len(rows)})")
    for story, phases, note in rows:
        print(f"  ✗ {_story_ref(story)}  {', '.join(phases)}")
        if note:
            print(f"       re-entry: {note}")


def _print_failed_by_class(non_done: list[dict], rca_stories: dict) -> None:
    """Group failed stories under their literal ``primary_failure_class``.

    Stories whose class routes to SKIPPED / INTAKE are excluded here. Class
    order follows first appearance in sprint order so the layout stays stable.
    """
    grouped: dict[str, list[dict]] = {}
    for story in non_done:
        entry = _rca_entry(story, rca_stories)
        primary = _primary_class(entry)
        if primary in _SKIPPED_INTAKE_CLASSES:
            continue
        grouped.setdefault(primary, []).append(story)

    for primary, group in grouped.items():
        print()
        print(f"FAILED — {primary} ({len(group)})")
        for story in group:
            entry = _rca_entry(story, rca_stories)
            print(f"  ✗ {_story_row(story)}")
            print(f"       primary:      {primary}")
            contributing = _string_list(entry.get("contributing_factors"))
            if contributing:
                print(f"       contributing: {', '.join(contributing)}")
            evidence_lines = _evidence_lines(entry)
            for index, line in enumerate(evidence_lines):
                label = "evidence:    " if index == 0 else "             "
                print(f"       {label} {line}")


def _print_skipped_intake(non_done: list[dict], rca_stories: dict) -> None:
    rows = [
        story
        for story in non_done
        if _primary_class(_rca_entry(story, rca_stories)) in _SKIPPED_INTAKE_CLASSES
    ]
    if not rows:
        return
    print()
    print(f"SKIPPED / INTAKE ({len(rows)})")
    for story in rows:
        entry = _rca_entry(story, rca_stories)
        print(f"  ⊘ {_story_row(story)}")
        primary = _primary_class(entry)
        detail = _skip_detail(entry)
        if detail:
            print(f"       {primary} — {detail}")
        else:
            print(f"       {primary}")


def _print_partial_value(non_done: list[dict], rca_stories: dict) -> None:
    """Stories with non-empty ``partial_value``, regardless of primary class.

    A story appears here *in addition to* its FAILED / SKIPPED placement so the
    operator gets both the failure-recovery and artifact-reuse views.
    """
    rows: list[tuple[dict, list[str]]] = []
    for story in non_done:
        entry = _rca_entry(story, rca_stories)
        values = _string_list(entry.get("partial_value"))
        if values:
            rows.append((story, values))
    if not rows:
        return
    print()
    print(f"PARTIAL VALUE ({len(rows)})")
    for story, values in rows:
        print(f"  ◐ {_story_ref(story)}  {'; '.join(values)}")


def _print_next(stories: list[dict], rca_stories: dict) -> None:
    """Flat, deduplicated ``recommended_next_actions`` across the RCA.

    Identical actions collapse to one line; when an action is story-specific and
    does not already name its originating story, the citation is appended.
    """
    order: list[str] = []
    origins: dict[str, list[str]] = {}
    for story in stories:
        entry = _rca_entry(story, rca_stories)
        num = _issue_number(story)
        for action in _string_list(entry.get("recommended_next_actions")):
            if action not in origins:
                origins[action] = []
                order.append(action)
        for action in _string_list(entry.get("recommended_next_actions")):
            if num and num not in origins[action]:
                origins[action].append(num)

    if not order:
        return
    print()
    print("NEXT")
    for action in order:
        uncited = [n for n in origins[action] if f"#{n}" not in action]
        if uncited:
            cite = ", ".join(f"#{n}" for n in uncited)
            print(f"  - {action} (from {cite})")
        else:
            print(f"  - {action}")


# ── Row / field helpers ───────────────────────────────────────────────────────


def _story_row(story: dict) -> str:
    """`#NNNN  $cost  elapsed  <title>` for a landed/failed row."""
    ref = _story_ref(story)
    cost = story.get("cost_usd")
    # A measured $0.00 is a number, not a missing value. Rendering it as the
    # absent marker collapsed three distinct states — "cost was zero", "cost is
    # unknown", and "no cost recorded" — into one dash, which is how a story
    # whose real spend was overwritten with 0.0 read as having no cost at all
    # (#2189). Only None / a missing key renders as absent.
    cost_str = (
        f"${cost:.2f}" if isinstance(cost, (int, float)) and not isinstance(cost, bool) else "—"
    )
    elapsed = _elapsed_seconds_from_bounds(story.get("started_at"), story.get("finished_at"))
    elapsed_str = f"{int(elapsed // 60)}m" if isinstance(elapsed, (int, float)) else "—"
    title = str(story.get("path") or story.get("slug") or "")
    return f"{ref}  {cost_str}  {elapsed_str}  {title}"


def _story_ref(story: dict) -> str:
    num = _issue_number(story)
    if num:
        return f"#{num}"
    return str(story.get("slug") or story.get("path") or "story")


def _issue_number(story: dict) -> str | None:
    slug = str(story.get("slug") or "")
    tail = slug.rsplit("-", 1)[-1] if "-" in slug else slug
    if tail.isdigit():
        return tail
    path = str(story.get("path") or "")
    if "#" in path:
        candidate = path.split("#", 1)[1].strip()
        if candidate.isdigit():
            return candidate
    return None


def _outcome(story: dict) -> str:
    return str(story.get("outcome") or "").upper()


def _rca_entry(story: dict, rca_stories: dict) -> dict:
    entry = rca_stories.get(str(story.get("slug") or ""))
    return entry if isinstance(entry, dict) else {}


def _primary_class(entry: dict) -> str:
    return str(entry.get("primary_failure_class") or "unknown_needs_rca")


def _skip_detail(entry: dict) -> str:
    """First non-baseline evidence excerpt for a SKIPPED / INTAKE row, if any."""
    for item in _evidence(entry):
        if item.get("rule_id") == _BASELINE_EVIDENCE_RULE:
            continue
        excerpt = str(item.get("excerpt") or "").strip()
        if excerpt:
            return excerpt
    return ""


def _evidence_lines(entry: dict) -> list[str]:
    """`<excerpt> at <source>` lines, baseline evidence suppressed."""
    lines: list[str] = []
    for item in _evidence(entry):
        if item.get("rule_id") == _BASELINE_EVIDENCE_RULE:
            continue
        excerpt = str(item.get("excerpt") or "").strip()
        source = str(item.get("source") or "").strip()
        if not excerpt:
            continue
        lines.append(f"{excerpt} at {source}" if source else excerpt)
        if len(lines) >= _MAX_EVIDENCE_LINES:
            break
    return lines


def _evidence(entry: dict) -> list[dict]:
    items = entry.get("evidence")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _read_digest_rca(sprint_log_dir: Path, run_id: str) -> dict | None:
    """Read the persisted RCA for ``run_id`` from disk (no classification).

    Prefers the durable run-keyed ``run-<id>-sprint-rca.yaml`` so a historical
    run resolves to its own analysis even after a later same-name run overwrote
    the ``sprint-rca.yaml`` pointer. The run-keyed file is trustworthy by name.

    The ``sprint-rca.yaml`` pointer, by contrast, tracks only the *latest* run
    for a sprint name (``forge rca`` writes it with ``write_pointer=False`` for
    historical runs). Accept it only when its ``sprint_run_id`` is absent or
    matches the queried run — otherwise it belongs to a different (later) run and
    would misattribute another run's failures into this postmortem brief. In
    that case fall through to the missing-RCA branch rather than lie.
    """
    if run_id:
        run_keyed = _load_yaml(sprint_log_dir / f"run-{run_id}-sprint-rca.yaml")
        if isinstance(run_keyed, dict):
            return run_keyed

    pointer = _load_yaml(sprint_log_dir / RCA_FILENAME)
    if isinstance(pointer, dict):
        pointer_run_id = pointer.get("sprint_run_id")
        if not pointer_run_id or not run_id or str(pointer_run_id) == str(run_id):
            return pointer
    return None


def _load_yaml(path: Path) -> object:
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception:
        return None
