"""Postmortem digest renderer for completed sprints.

``forge status`` has two renderings that read the *same* on-disk state:

* the wide telemetry table (``cli/sprint_status.py``) — the right view while a
  sprint is live and the operator is monitoring "what is stuck right now";
* this postmortem digest — the right view once a sprint is complete and the
  operator's question shifts from monitoring to recovery: what landed, what
  failed (and why), what produced partial value, what was skipped, and what to
  do next.

The digest is a pure *renderer*. Its interface to the rest of the system is the
two artifacts it reads — ``sprint-summary.yaml`` (the LANDED accounting) and
``sprint-rca.yaml`` (the recovery signals). It never invokes the RCA engine and
never touches live coordinator state; that keeps it implementable and testable
independently of both the live renderer and the classifier.

Layout for a completed sprint with one or more non-DONE stories::

    SPRINT <name>  ·  completed  ·  <duration>m  ·  $<cost>

    LANDED (n of m)
      ✓ #NNNN  $cost  elapsed  <title>

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

from theforge.sprint.rca import DONE_OUTCOMES, RCA_FILENAME
from theforge.sprint.status_reader import (
    _elapsed_seconds_from_bounds,
    find_sprint_summary,
)

# Primary failure classes whose stories never entered DEV — intake shape drops,
# dependency-blocked skips, and launch-guard collisions. These route to the
# SKIPPED / INTAKE section rather than getting their own FAILED heading, because
# the operator's recovery action is reshaping/scheduling, not failure triage.
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

    landed = [s for s in stories if _outcome(s) in DONE_OUTCOMES]
    non_done = [s for s in stories if _outcome(s) not in DONE_OUTCOMES]

    _print_header(sprint_block, run_id)
    _print_landed(landed, len(stories))

    # All-DONE sprint: tighter LANDED-only digest, no recovery sections.
    if not non_done:
        return 0

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
    cost_str = f"${cost:.2f}" if isinstance(cost, (int, float)) and cost else "—"
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
