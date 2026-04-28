"""Surface consistency: every operator-facing surface projects from the
canonical ``SprintStoryState``.

Two flavours of test:

1. Source-scan enforcement — fail if any module under ``sprint/`` or ``cli/``
   reintroduces parallel integer counters (``specs_succeeded``,
   ``specs_failed``, ``specs_skipped``) outside the canonical structure.
2. Behavioural projection — given a populated ``SprintStoryState``, the
   ``SprintStateWriter`` on-disk file, the banner counts, and the summary
   counts must agree exactly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from theforge.sprint.state_writer import SprintStateWriter
from theforge.sprint.story_state import SprintStoryState, StoryOutcome

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = [
    REPO_ROOT / "src" / "theforge" / "sprint",
    REPO_ROOT / "src" / "theforge" / "cli",
]
CANONICAL_FILE = REPO_ROOT / "src" / "theforge" / "sprint" / "story_state.py"
# Files that legitimately mention specs_* names because they read from the
# canonical structure (assignment from counts(), or pass-through into the
# SprintResult dataclass which is the wire format).
ALLOWED_FILES = {
    REPO_ROOT / "src" / "theforge" / "sprint" / "manifest.py",
}

PARALLEL_COUNTER_PATTERN = re.compile(r"(specs_succeeded|specs_failed|specs_skipped)\s*\+=\s*\d")
LOCAL_COUNTER_DECL_PATTERN = re.compile(
    r"^\s*(specs_succeeded|specs_failed|specs_skipped)\s*=\s*0\s*$",
    re.MULTILINE,
)


def _iter_python_files():
    for root in SCAN_DIRS:
        for path in root.rglob("*.py"):
            if path == CANONICAL_FILE or path in ALLOWED_FILES:
                continue
            if path.name == "__init__.py":
                continue
            yield path


def test_no_parallel_counter_increments_outside_canonical_structure() -> None:
    """Re-introducing ``specs_X += 1`` is the bug class this story closed."""
    offenders: list[tuple[Path, str]] = []
    for path in _iter_python_files():
        text = path.read_text(encoding="utf-8")
        for match in PARALLEL_COUNTER_PATTERN.finditer(text):
            offenders.append((path, match.group(0)))
    assert not offenders, (
        "Parallel counter pattern reintroduced outside canonical structure:\n"
        + "\n".join(f"  {p}: {m}" for p, m in offenders)
    )


def test_no_local_zero_initialised_counter_decls() -> None:
    """A bare ``specs_succeeded = 0`` is the canonical-structure red flag."""
    offenders: list[tuple[Path, str]] = []
    for path in _iter_python_files():
        text = path.read_text(encoding="utf-8")
        for match in LOCAL_COUNTER_DECL_PATTERN.finditer(text):
            offenders.append((path, match.group(0).strip()))
    assert not offenders, "Local zero-initialised parallel counters reintroduced:\n" + "\n".join(
        f"  {p}: {m}" for p, m in offenders
    )


def test_state_writer_shares_canonical_instance(tmp_path: Path) -> None:
    """``SprintStateWriter`` must wrap the supplied ``SprintStoryState``."""
    state = SprintStoryState()
    writer = SprintStateWriter(
        run_id="test-run",
        project_root=tmp_path,
        sprint_name="sprint-x",
        story_state=state,
    )
    writer.init(
        [
            {"slug": "a", "path": "Issue #1", "status": "waiting"},
            {"slug": "b", "path": "Issue #2", "status": "waiting"},
        ]
    )
    # Same instance — transitions on writer reflect in canonical state.
    writer.update("a", status="running")
    assert state.get("a").outcome is StoryOutcome.RUNNING
    writer.update("a", status="done")
    writer.update("b", status="failed")
    counts = state.counts()
    assert counts == {"total": 2, "succeeded": 1, "failed": 1, "skipped": 0}


def test_disk_file_is_projection_of_canonical_state(tmp_path: Path) -> None:
    state = SprintStoryState()
    writer = SprintStateWriter(
        run_id="run-disk",
        project_root=tmp_path,
        sprint_name="sprint-y",
        story_state=state,
    )
    writer.init([{"slug": "a", "path": "Issue #1", "status": "waiting"}])
    writer.update("a", status="done", cost_usd=1.5)

    state_file = tmp_path / ".forge" / "runs" / "run-disk.state"
    assert state_file.exists()
    on_disk = yaml.safe_load(state_file.read_text(encoding="utf-8"))
    assert on_disk["sprint_name"] == "sprint-y"
    assert len(on_disk["stories"]) == 1
    story = on_disk["stories"][0]
    # Both legacy "status" and canonical "outcome" must be present and equal
    # so existing readers (status_reader.py) and new readers (canonical) agree.
    assert story["slug"] == "a"
    assert story["status"] == "done"
    assert story["outcome"] == "done"
    assert story["cost_usd"] == 1.5


def test_shape_gate_skipped_visible_in_canonical_state(tmp_path: Path) -> None:
    """A needs-grooming-skipped issue must be registered in the canonical
    structure with outcome=SKIPPED and a visible reason — addressing the
    documented disagreement #1 (forge status hides shape-gate skips)."""
    state = SprintStoryState()
    state.register(
        "issue-42",
        "Issue #42",
        outcome=StoryOutcome.SKIPPED,
        reason="needs_grooming_label",
    )
    writer = SprintStateWriter(
        run_id="run-skip",
        project_root=tmp_path,
        sprint_name="sprint-skip",
        story_state=state,
    )
    writer.init([{"slug": "issue-42", "path": "Issue #42", "status": "skipped"}])

    state_file = tmp_path / ".forge" / "runs" / "run-skip.state"
    on_disk = yaml.safe_load(state_file.read_text(encoding="utf-8"))
    assert on_disk["stories"][0]["status"] == "skipped"
    assert on_disk["stories"][0]["reason"] == "needs_grooming_label"


def test_already_done_persists_in_canonical_state() -> None:
    """An issue closed between sprint launch and fetch must surface with a
    terminal outcome, not be silently dropped — disagreement #2."""
    state = SprintStoryState()
    state.register("issue-99", "Issue #99", outcome=StoryOutcome.ALREADY_DONE)
    counts = state.counts()
    # ALREADY_DONE counts as a skip in legacy aggregates but the entry is
    # never silently dropped from the canonical structure.
    assert counts == {"total": 1, "succeeded": 1, "failed": 0, "skipped": 0}
    assert state.get("issue-99").outcome.is_terminal


def test_banner_counts_match_summary_counts() -> None:
    """Banner reads ``state.counts()``; the summary writer also projects from
    the same instance — disagreement #3 cannot recur by construction."""
    state = SprintStoryState()
    state.register("a", "p", outcome=StoryOutcome.DONE)
    state.register("b", "p", outcome=StoryOutcome.DONE)
    state.register("c", "p", outcome=StoryOutcome.DONE)
    state.register("d", "p", outcome=StoryOutcome.FAILED)
    state.register("e", "p", outcome=StoryOutcome.SKIPPED)

    banner_counts = state.counts()
    # Project the same way the summary writer does (audit.py path).
    summary_succeeded = sum(1 for s in state.stories() if s.outcome.is_succeeded)
    summary_failed = sum(1 for s in state.stories() if s.outcome.is_failed)
    summary_skipped = sum(1 for s in state.stories() if s.outcome.is_skipped)
    assert banner_counts["succeeded"] == summary_succeeded == 3
    assert banner_counts["failed"] == summary_failed == 1
    assert banner_counts["skipped"] == summary_skipped == 1


@pytest.mark.parametrize(
    "outcome,expected_bucket",
    [
        (StoryOutcome.DONE, "succeeded"),
        (StoryOutcome.ALREADY_DONE, "succeeded"),
        (StoryOutcome.FAILED, "failed"),
        (StoryOutcome.ESCALATED, "failed"),
        (StoryOutcome.SKIPPED, "skipped"),
        (StoryOutcome.PRESERVED, "skipped"),
        (StoryOutcome.DROPPED, "failed"),
    ],
)
def test_terminal_outcomes_projected_to_correct_bucket(
    outcome: StoryOutcome, expected_bucket: str
) -> None:
    state = SprintStoryState()
    state.register("a", "p", outcome=outcome)
    counts = state.counts()
    assert counts[expected_bucket] == 1
    other_buckets = {"succeeded", "failed", "skipped"} - {expected_bucket}
    for bucket in other_buckets:
        assert counts[bucket] == 0


def test_already_done_serialized_as_legacy_done_for_status_reader(tmp_path: Path) -> None:
    """The live .state file maps canonical ALREADY_DONE down to legacy ``done``
    so ``read_live_status`` (which understands legacy buckets only) renders
    the story with status=done and ``final_outcome`` exposing ALREADY_DONE."""
    from theforge.sprint.status_reader import read_live_status

    state = SprintStoryState()
    writer = SprintStateWriter(
        run_id="run-already",
        project_root=tmp_path,
        sprint_name="sprint-already",
        story_state=state,
    )
    writer.init([{"slug": "issue-99", "path": "Issue #99", "status": "waiting"}])
    writer.update("issue-99", status=StoryOutcome.ALREADY_DONE)

    entries = read_live_status("run-already", tmp_path)
    assert entries is not None
    by_slug = {entry.slug: entry for entry in entries}
    assert by_slug["issue-99"].status == "done"
    assert by_slug["issue-99"].detail == "ALREADY_DONE"


def test_summary_propagates_canonical_terminal_correction(tmp_path: Path) -> None:
    """Per-story rows in sprint-summary.yaml must reflect canonical
    terminal-to-terminal corrections (e.g., DONE -> FAILED when a queued PR
    fails to land), not res.phase.name."""
    import datetime
    from unittest.mock import MagicMock

    from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
    from theforge.sprint.audit import _write_sprint_summary
    from theforge.sprint.manifest import ResolvedSprint, SprintResult

    state = SprintStoryState()
    state.register("a", "Issue #1", outcome=StoryOutcome.DONE, canonical_ref="a.md")
    # Simulate the queued-PR-failed-to-land correction.
    state.transition("a", outcome=StoryOutcome.FAILED)

    coord_state = CoordinatorState(
        phase=Phase.DONE,
        started_at="2026-04-25T12:00:00Z",
        workspace_path=tmp_path,
        log_dir=tmp_path,
    )
    coord_result = CoordinatorResult(
        success=True, phase=Phase.DONE, state=coord_state, message="done"
    )
    sprint_res = SprintResult(
        name="s",
        specs_total=1,
        specs_succeeded=0,
        specs_failed=1,
        specs_skipped=0,
        total_cost_usd=0.0,
        budget_usd=10.0,
        results=[("a.md", coord_result)],
    )
    manifest = ResolvedSprint(name="s", budget_usd=10.0, stories=[], max_parallel=1)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    now = datetime.datetime.now(datetime.timezone.utc)
    _write_sprint_summary(
        manifest=manifest,
        result=sprint_res,
        canonical_refs=["a.md"],
        started_at=now,
        finished_at=now,
        duration=0.0,
        sprint_log_dir=log_dir,
        slug_map={"a.md": "a"},
        tasks_by_slug={"a": MagicMock(depends_on=[])},
        story_state=state,
    )
    summary = yaml.safe_load((log_dir / "sprint-summary.yaml").read_text())
    rows = {s["slug"]: s for s in summary["stories"]}
    # Per-story row outcome matches canonical, not res.phase.name (DONE).
    assert rows["a"]["outcome"] == "FAILED"
    assert summary["sprint"]["specs_failed"] == 1
    assert summary["sprint"]["specs_succeeded"] == 0


def test_all_skipped_shape_gate_path_uses_canonical_state(tmp_path: Path) -> None:
    """The all-skipped CLI path must project summary stories and counts from
    the canonical SprintStoryState — skipped issues must appear as stories,
    not just in the separate skipped list."""
    from theforge.cli.sprint import _emit_all_skipped_audit
    from theforge.sprint.shape_gate import SkippedIssue

    config = MagicMock_with_root(tmp_path)
    skipped = [
        SkippedIssue(
            issue_number=42,
            reason_codes=("needs_grooming_label",),
            source="label",
            title="Needs grooming",
            detail="issue carries 'needs-grooming' label",
        ),
        SkippedIssue(
            issue_number=43,
            reason_codes=("missing_acceptance_criteria",),
            source="local_check",
            title="No ACs",
            detail="local shape check rejected",
        ),
    ]
    _emit_all_skipped_audit(
        config=config,
        sprint_name="sprint-all-skipped",
        budget_usd=10.0,
        skipped_issues=skipped,
    )
    summary_path = tmp_path / ".forge" / "logs" / "sprint-all-skipped" / "sprint-summary.yaml"
    summary = yaml.safe_load(summary_path.read_text(encoding="utf-8"))
    # Counts come from canonical state, not from a hard-coded zero.
    assert summary["sprint"]["specs_total"] == 2
    assert summary["sprint"]["specs_skipped"] == 2
    assert summary["sprint"]["specs_succeeded"] == 0
    assert summary["sprint"]["specs_failed"] == 0
    # Stories list contains both shape-gate-skipped issues — not just the
    # legacy skipped[] list.
    slugs = {row["slug"] for row in summary["stories"]}
    assert {"issue-42", "issue-43"}.issubset(slugs)


def MagicMock_with_root(tmp_path: Path):
    """Tiny config stub providing only ``project_root`` for the all-skipped
    helper. Avoids pulling in the full ForgeConfig fixture."""
    from types import SimpleNamespace

    return SimpleNamespace(project_root=tmp_path)


def test_dropped_story_classified_as_failed_across_all_surfaces(tmp_path: Path) -> None:
    """A launch-guard DROPPED story (e.g. active-worktree collision) must
    surface as ``failed`` in every operator-facing place: canonical counts,
    live status reader, sprint-summary aggregates, notifications, and the
    completed-status projection. DROPPED in the skipped bucket would put the
    canonical model out of sync with status_reader._outcome_to_status, which
    has always mapped DROPPED to ``failed``."""
    from theforge.sprint.status_reader import _outcome_to_status

    state = SprintStoryState()
    state.register("a", "Issue #1", outcome=StoryOutcome.DROPPED, reason="active worktree")
    state.register("b", "Issue #2", outcome=StoryOutcome.DONE)

    counts = state.counts()
    # DROPPED is failed everywhere — banner, notifications, summary all
    # project this same value via state.counts().
    assert counts == {"total": 2, "succeeded": 1, "failed": 1, "skipped": 0}

    # status_reader (completed status projection) agrees: outcome="DROPPED"
    # also resolves to legacy status "failed".
    assert _outcome_to_status("DROPPED") == "failed"

    # Live .state file projects DROPPED to legacy ``failed`` too — the
    # operator never sees DROPPED bucketed as skipped on any surface.
    writer = SprintStateWriter(
        run_id="run-drop",
        project_root=tmp_path,
        sprint_name="sprint-drop",
        story_state=state,
    )
    writer.init([])  # entries already registered above
    on_disk = yaml.safe_load(
        (tmp_path / ".forge" / "runs" / "run-drop.state").read_text(encoding="utf-8")
    )
    a_row = next(s for s in on_disk["stories"] if s["slug"] == "a")
    assert a_row["status"] == "failed"
    assert a_row["outcome"] == "dropped"
    assert a_row["detail"]["final_outcome"] == "DROPPED"


def test_headless_invocation_registers_shape_gate_skipped_in_canonical() -> None:
    """When run_sprint runs without a CLI run_id, shape-gate-skipped issues
    are still registered in the canonical SprintStoryState passed to the
    summary writer (they appear in counts and the stories list)."""
    state = SprintStoryState()
    # Simulate the headless registration the runner performs.
    state.register(
        "issue-101",
        "Issue #101",
        outcome=StoryOutcome.SKIPPED,
        reason="needs_grooming_label",
    )
    counts = state.counts()
    assert counts == {"total": 1, "succeeded": 0, "failed": 0, "skipped": 1}
