"""A story record carries the issue's real title, not a restated reference (#2664).

The digest's trailing column is documented as ``<title>`` but renders the story
record's ``path`` field. Every site that built an issue-backed record wrote
``Issue #<number>`` there, so the column repeated the ``#NNNN`` reference the
row already opened with and a sprint report named no work at all.

These tests pin the title from where it is resolved (intake) through the state
the operator-facing surfaces read, and the rendering guard that refuses to
print the duplicate when no title was resolvable.
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from sprint_test_helpers import run_sprint_ctx

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.sprint.manifest import ResolvedSprint
from theforge.sprint.state_writer import write_bootstrap_state
from theforge.sprint.story_state import issue_reference_label, story_title
from theforge.task import TaskStory

# ── story_title: which label a record carries ────────────────────────────────


class TestStoryTitle:
    def test_issue_backed_story_keeps_the_resolved_title(self) -> None:
        assert (
            story_title("Fix flaky retry timeout", canonical_ref="issue:2524", slug="issue-2524")
            == "Fix flaky retry timeout"
        )

    def test_missing_title_falls_back_to_the_issue_reference(self) -> None:
        assert story_title(None, canonical_ref="issue:2524", slug="issue-2524") == "Issue #2524"
        assert story_title("   ", canonical_ref="issue:2524") == "Issue #2524"

    def test_reference_shaped_title_is_treated_as_absent(self) -> None:
        """A record whose 'title' is already the placeholder gains nothing."""
        assert story_title("Issue #2524", canonical_ref="issue:2524") == "Issue #2524"

    def test_title_is_collapsed_to_a_single_line(self) -> None:
        assert story_title("Fix\n  flaky   retry", canonical_ref="issue:7") == "Fix flaky retry"

    def test_issue_reference_is_derivable_from_the_slug_alone(self) -> None:
        assert story_title("Real title", slug="issue-99") == "Real title"
        assert story_title(None, slug="issue-99") == "Issue #99"

    def test_file_backed_story_path_is_never_replaced_by_a_title(self) -> None:
        """``path`` for a file story is a repository path the audit keys on."""
        assert (
            story_title("Story A", canonical_ref="stories/story-a.md", slug="story-a")
            == "stories/story-a.md"
        )
        assert issue_reference_label("stories/story-a.md", "story-a") is None


# ── bootstrap state: the title survives intake ───────────────────────────────


class TestBootstrapState:
    def test_issue_rows_carry_the_fetched_title(self, tmp_path: Path) -> None:
        write_bootstrap_state(
            "run-title",
            tmp_path,
            sprint_name="issues-1461,1462",
            sprint_phase="starting",
            issues=[
                {"number": 1461, "title": "Cache the model catalog"},
                {"number": 1462, "title": "Reap orphaned worktrees"},
            ],
        )

        data = yaml.safe_load(
            (tmp_path / ".forge" / "runs" / "run-title.state").read_text(encoding="utf-8")
        )
        paths = {story["slug"]: story["path"] for story in data["stories"]}
        assert paths == {
            "issue-1461": "Cache the model catalog",
            "issue-1462": "Reap orphaned worktrees",
        }

    def test_titleless_issue_row_falls_back_to_the_reference(self, tmp_path: Path) -> None:
        write_bootstrap_state(
            "run-untitled",
            tmp_path,
            sprint_name="issues-1461",
            sprint_phase="starting",
            issues=[{"number": 1461, "title": ""}],
        )

        data = yaml.safe_load(
            (tmp_path / ".forge" / "runs" / "run-untitled.state").read_text(encoding="utf-8")
        )
        assert data["stories"][0]["path"] == "Issue #1461"

    def test_shape_gate_skip_rows_carry_their_title(self, tmp_path: Path) -> None:
        from theforge.sprint.shape_gate import SkippedIssue

        write_bootstrap_state(
            "run-skip",
            tmp_path,
            sprint_name="issues-1461",
            sprint_phase="starting",
            issues=[],
            skipped_issues=[
                SkippedIssue(
                    issue_number=1461,
                    reason_codes=("missing_acceptance_criteria",),
                    source="local_check",
                    title="Cache the model catalog",
                )
            ],
        )

        data = yaml.safe_load(
            (tmp_path / ".forge" / "runs" / "run-skip.state").read_text(encoding="utf-8")
        )
        assert data["stories"][0]["path"] == "Cache the model catalog"
        assert data["stories"][0]["status"] == "skipped"


# ── digest rendering ─────────────────────────────────────────────────────────


def _render_digest(tmp_path: Path, sprint_name: str, run_id: str, stories: list[dict]) -> str:
    from theforge.cli.sprint_digest import display_sprint_digest

    log_dir = tmp_path / ".forge" / "logs" / sprint_name
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "sprint-summary.yaml").write_text(
        yaml.safe_dump(
            {
                "sprint": {
                    "name": sprint_name,
                    "run_id": run_id,
                    "total_cost_usd": 1.0,
                    "duration_seconds": 600.0,
                    "finished_at": "2026-05-08T03:00:00Z",
                },
                "stories": stories,
            }
        ),
        encoding="utf-8",
    )
    buf = io.StringIO()
    with patch("sys.stdout", buf):
        rc = display_sprint_digest(run_id, tmp_path)
    assert rc == 0, buf.getvalue()
    return buf.getvalue()


class TestDigestTitleColumn:
    def _landed(self, num: int, path: str) -> dict:
        return {
            "slug": f"issue-{num}",
            "path": path,
            "outcome": "DONE",
            "cost_usd": 7.58,
            "started_at": "2026-05-08T00:00:00Z",
            "finished_at": "2026-05-08T01:01:00Z",
        }

    def test_landed_row_names_the_work(self, tmp_path: Path) -> None:
        output = _render_digest(
            tmp_path, "issues-2524", "run-1", [self._landed(2524, "Fix flaky retry timeout")]
        )
        assert "#2524  $7.58  61m  Fix flaky retry timeout" in output

    def test_row_never_restates_its_own_reference(self, tmp_path: Path) -> None:
        """A titleless record renders no title column rather than a duplicate."""
        output = _render_digest(
            tmp_path, "issues-2524", "run-2", [self._landed(2524, "Issue #2524")]
        )
        assert "Issue #2524" not in output
        assert "#2524  $7.58  61m" in output


# ── seam: runner → story state → digest ──────────────────────────────────────


def _make_config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(
            max_dev_iterations=2,
            max_review_cycles=2,
            preflight_complexity_gate_threshold=11,
        ),
    )


def _coordinator_result(success: bool) -> CoordinatorResult:
    state = CoordinatorState()
    state.preflight_verdict = "PROCEED"
    preflight = MagicMock()
    preflight.cost_usd = 1.0
    state.preflight_result = preflight
    return CoordinatorResult(
        success=success,
        phase=Phase.DONE if success else Phase.DEV,
        state=state,
        message="Done." if success else "Failed.",
    )


def test_run_sprint_records_the_issue_title_for_landed_and_failed_stories(
    tmp_path: Path,
) -> None:
    """Seam: the title on the TaskStory reaches the record every report reads.

    The runner registers per-story records after preflight; the digest's title
    column is whatever that registration wrote. Both the succeeded and the
    failed row must name the work (#2664).
    """
    config = _make_config(tmp_path)
    source = MagicMock()
    resolved = ResolvedSprint(
        name="issues-2524,2541",
        budget_usd=50.0,
        stories=[
            (
                TaskStory(
                    name="Fix flaky retry timeout",
                    slug="issue-2524",
                    story_text="Do the thing.",
                    github_issue=2524,
                ),
                source,
                "issue:2524",
            ),
            (
                TaskStory(
                    name="Reap orphaned worktrees",
                    slug="issue-2541",
                    story_text="Do the other thing.",
                    github_issue=2541,
                ),
                source,
                "issue:2541",
            ),
        ],
        max_parallel=1,
    )

    def _run_task(task, *args, **kwargs):
        return _coordinator_result(task.slug == "issue-2524")

    with patch("theforge.sprint.runner.run_task", side_effect=_run_task):
        run_sprint_ctx(config, resolved, run_id="run-seam")

    summary = yaml.safe_load(
        (tmp_path / ".forge" / "logs" / resolved.name / "sprint-summary.yaml").read_text(
            encoding="utf-8"
        )
    )
    paths = {story["slug"]: story["path"] for story in summary["stories"]}
    assert paths == {
        "issue-2524": "Fix flaky retry timeout",
        "issue-2541": "Reap orphaned worktrees",
    }

    # ...and the digest the operator reads renders them, never the reference twice.
    from theforge.cli.sprint_digest import display_sprint_digest

    buf = io.StringIO()
    with patch("sys.stdout", buf):
        display_sprint_digest("run-seam", tmp_path)
    output = buf.getvalue()
    assert "Fix flaky retry timeout" in output
    assert "Reap orphaned worktrees" in output
    assert "Issue #2524" not in output
    assert "Issue #2541" not in output
