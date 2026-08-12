"""Tests for issue #2347: a run must record what it changed, not only what it cost.

The audit record carried `cost.total_usd` and nothing about the files the spend
bought, so cost could not be joined to code. Reconstructing the join afterwards
from git history recovers a small minority of it, because a third of commits
name more than one issue.

These tests pin the mechanism at each surface it crosses:

- ``collect_changed_files``: the structured comparison itself, against real git
  repositories — resolved SHAs, per-file counts, binary files, empty comparisons
- ``generate_audit_log``: the captured snapshot reaches the record, including on
  a run that terminated before review
- ``land_story`` → ``_write_story_audit``: the snapshot survives a real local
  merge that deletes the worktree and the branch before the audit is written
- ``audit_changed_files``: the indexed rows join back to ``audit_records`` by
  path and agree with what git says the run's commits touched
- ``_migrate_v27_to_v28``: an older record reads as ``null``, which is a
  different claim from a captured comparison that found nothing
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from coord_test_helpers import _make_task

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator import audit_substrate
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.changed_files import (
    capture_changed_files,
    collect_changed_files,
    resolve_changed_files,
)
from theforge.coordinator.completion import land_story
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.sprint.audit import _write_story_audit
from theforge.task import TaskStory

WORKTREE_PATTERN = ".forge/worktrees/{slug}"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return result.stdout


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern=WORKTREE_PATTERN,
            branch_pattern="forge/{slug}",
            base_branch="main",
            on_approve="merge",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        preflight_fallback_profile=None,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        log=LogConfig(enabled=False),
    )


def _story_repo(tmp_path: Path, slug: str = "story") -> tuple[Path, Path, str]:
    """Project root with a base commit plus a story worktree that changed files.

    Returns ``(project_root, workspace_path, branch)``. The story branch adds one
    file and rewrites another, so the expected numstat is non-trivial in both
    directions.
    """
    project_root = tmp_path / "repo"
    project_root.mkdir()
    _git(project_root, "init", "--initial-branch=main")
    _git(project_root, "config", "user.email", "test@example.com")
    _git(project_root, "config", "user.name", "Test")

    # Forge-owned state (worktrees included) is ignored in a real project; the
    # landing path refuses to merge into a dirty project root, so the fixture
    # has to look like the repository it is standing in for.
    _write(project_root / ".gitignore", ".forge/\n")
    _write(project_root / "src" / "keep.py", "# untouched\n")
    _write(project_root / "src" / "rewritten.py", "a\nb\nc\n")
    _git(project_root, "add", ".gitignore", "src")
    _git(project_root, "commit", "-m", "initial")

    branch = f"forge/{slug}"
    workspace_path = project_root / WORKTREE_PATTERN.format(slug=slug)
    workspace_path.parent.mkdir(parents=True, exist_ok=True)
    _git(project_root, "worktree", "add", "-b", branch, str(workspace_path), "main")
    _git(workspace_path, "config", "user.email", "test@example.com")
    _git(workspace_path, "config", "user.name", "Test")

    _write(workspace_path / "src" / "added.py", "one\ntwo\n")
    _write(workspace_path / "src" / "rewritten.py", "a\nB\nc\nd\n")
    _git(workspace_path, "add", "src")
    _git(workspace_path, "commit", "-m", "story: change two files")

    return project_root, workspace_path, branch


def _done_result(state: CoordinatorState) -> CoordinatorResult:
    return CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="done")


# ── The collector ────────────────────────────────────────────────────────


class TestCollectChangedFiles:
    def test_records_resolved_refs_and_per_file_counts(self, tmp_path: Path) -> None:
        """The comparison names SHAs, not the moving base-branch ref."""
        project_root, workspace_path, _branch = _story_repo(tmp_path)

        snapshot = collect_changed_files(workspace_path, "main")

        assert snapshot is not None
        assert snapshot["base_ref"] == _git(project_root, "rev-parse", "main").strip()
        assert snapshot["head_ref"] == _git(workspace_path, "rev-parse", "HEAD").strip()
        assert snapshot["files"] == [
            {"path": "src/added.py", "insertions": 2, "deletions": 0, "binary": False},
            {"path": "src/rewritten.py", "insertions": 2, "deletions": 1, "binary": False},
        ]

    def test_unchanged_comparison_returns_empty_file_list(self, tmp_path: Path) -> None:
        """A branch that changed nothing is a *made* comparison, not an absent one."""
        project_root = tmp_path / "repo"
        project_root.mkdir()
        _git(project_root, "init", "--initial-branch=main")
        _git(project_root, "config", "user.email", "test@example.com")
        _git(project_root, "config", "user.name", "Test")
        _write(project_root / "a.txt", "a\n")
        _git(project_root, "add", "a.txt")
        _git(project_root, "commit", "-m", "initial")

        snapshot = collect_changed_files(project_root, "main")

        assert snapshot is not None
        assert snapshot["files"] == []
        assert snapshot["base_ref"] == snapshot["head_ref"]

    def test_binary_change_is_flagged_rather_than_recorded_as_zero_lines(
        self, tmp_path: Path
    ) -> None:
        """git reports ``-`` for a binary file; 0/0 alone would read as "no change"."""
        _project_root, workspace_path, _branch = _story_repo(tmp_path)
        (workspace_path / "asset.bin").write_bytes(b"\x00\x01\x02\xff")
        _git(workspace_path, "add", "asset.bin")
        _git(workspace_path, "commit", "-m", "story: add a binary asset")

        snapshot = collect_changed_files(workspace_path, "main")

        assert snapshot is not None
        entry = next(f for f in snapshot["files"] if f["path"] == "asset.bin")
        assert entry["binary"] is True
        assert entry["insertions"] == 0
        assert entry["deletions"] == 0

    def test_unresolvable_refs_return_none_not_an_empty_set(self, tmp_path: Path) -> None:
        """An impossible comparison must not be recorded as one that found nothing."""
        _project_root, workspace_path, _branch = _story_repo(tmp_path)

        assert collect_changed_files(workspace_path, "no-such-branch") is None
        assert collect_changed_files(tmp_path / "missing", "main") is None

    def test_missing_workspace_returns_none(self, tmp_path: Path) -> None:
        assert collect_changed_files(tmp_path / "gone", "main") is None


# ── The capture seam ─────────────────────────────────────────────────────


class TestCaptureSeam:
    def test_capture_stores_snapshot_on_state(self, tmp_path: Path) -> None:
        _project_root, workspace_path, _branch = _story_repo(tmp_path)
        config = _make_config(tmp_path / "repo")
        state = CoordinatorState()
        state.workspace_path = workspace_path

        captured = capture_changed_files(state, config)

        assert captured is state.changed_files
        assert [f["path"] for f in state.changed_files["files"]] == [
            "src/added.py",
            "src/rewritten.py",
        ]

    def test_capture_never_clobbers_a_good_snapshot_with_a_failure(self, tmp_path: Path) -> None:
        """Once the worktree is gone, the snapshot taken while it existed stands."""
        _project_root, workspace_path, _branch = _story_repo(tmp_path)
        config = _make_config(tmp_path / "repo")
        state = CoordinatorState()
        state.workspace_path = workspace_path
        capture_changed_files(state, config)
        good = state.changed_files

        state.workspace_path = tmp_path / "deleted"
        assert capture_changed_files(state, config) == good
        assert state.changed_files == good

    def test_resolve_prefers_the_stored_snapshot_over_the_live_worktree(
        self, tmp_path: Path
    ) -> None:
        """The audit reads the frozen capture, not whatever the tree holds later."""
        _project_root, workspace_path, _branch = _story_repo(tmp_path)
        config = _make_config(tmp_path / "repo")
        state = CoordinatorState()
        state.workspace_path = workspace_path
        state.changed_files = {"base_ref": "a" * 40, "head_ref": "b" * 40, "files": []}

        assert resolve_changed_files(state, config)["files"] == []

    def test_resolve_does_not_freeze_its_fallback_onto_state(self, tmp_path: Path) -> None:
        """The in-flight flush calls this mid-run; a stored answer would go stale."""
        _project_root, workspace_path, _branch = _story_repo(tmp_path)
        config = _make_config(tmp_path / "repo")
        state = CoordinatorState()
        state.workspace_path = workspace_path

        assert resolve_changed_files(state, config) is not None
        assert state.changed_files is None


# ── The audit record ─────────────────────────────────────────────────────


class TestAuditRecord:
    def test_done_run_records_refs_and_per_file_counts(self, tmp_path: Path) -> None:
        """AC1/AC3: a completed run's record carries the file set and its refs."""
        project_root, workspace_path, _branch = _story_repo(tmp_path)
        config = _make_config(project_root)
        task = _make_task(project_root)
        state = CoordinatorState()
        state.run_id = "deadbeefcafe"
        state.started_at = "2026-01-01T00:00:00+00:00"
        state.workspace_path = workspace_path
        capture_changed_files(state, config)

        record = generate_audit_log(config, task, _done_result(state))

        block = record["changed_files"]
        assert block["base_ref"] == _git(project_root, "rev-parse", "main").strip()
        assert block["head_ref"] == _git(workspace_path, "rev-parse", "HEAD").strip()
        assert block["files"] == [
            {"path": "src/added.py", "insertions": 2, "deletions": 0, "binary": False},
            {"path": "src/rewritten.py", "insertions": 2, "deletions": 1, "binary": False},
        ]

    def test_escalated_run_with_no_reviews_still_records_its_file_set(
        self, tmp_path: Path
    ) -> None:
        """AC2: escalated runs never reach review and are the ones worth attributing."""
        project_root, workspace_path, _branch = _story_repo(tmp_path)
        config = _make_config(project_root)
        task = _make_task(project_root)
        state = CoordinatorState()
        state.run_id = "deadbeefcafe"
        state.started_at = "2026-01-01T00:00:00+00:00"
        state.workspace_path = workspace_path
        state.escalate_reason = "dev could not satisfy the spec"
        result = CoordinatorResult(
            success=False, phase=Phase.ESCALATE, state=state, message="escalated"
        )

        record = generate_audit_log(config, task, result)

        assert record["reviews"] == []
        assert [f["path"] for f in record["changed_files"]["files"]] == [
            "src/added.py",
            "src/rewritten.py",
        ]

    def test_failed_dev_run_with_no_snapshot_falls_back_to_the_worktree(
        self, tmp_path: Path
    ) -> None:
        """AC2: a DEV failure lands here with nothing captured and a live worktree."""
        project_root, workspace_path, _branch = _story_repo(tmp_path)
        config = _make_config(project_root)
        task = _make_task(project_root)
        state = CoordinatorState()
        state.run_id = "deadbeefcafe"
        state.started_at = "2026-01-01T00:00:00+00:00"
        state.workspace_path = workspace_path
        state.error = "dev agent failed"
        state.error_type = "dev_failure"
        result = CoordinatorResult(
            success=False, phase=Phase.DEV, state=state, message="dev failed"
        )

        record = generate_audit_log(config, task, result)

        assert record["changed_files"] is not None
        assert len(record["changed_files"]["files"]) == 2

    def test_run_without_any_comparison_records_null(self, tmp_path: Path) -> None:
        """AC6: "no comparison" must be sayable, and distinct from "changed nothing"."""
        project_root, _workspace_path, _branch = _story_repo(tmp_path)
        config = _make_config(project_root)
        task = _make_task(project_root)
        state = CoordinatorState()
        state.run_id = "deadbeefcafe"
        state.started_at = "2026-01-01T00:00:00+00:00"

        record = generate_audit_log(config, task, _done_result(state))

        assert record["changed_files"] is None


# ── The landing seam ─────────────────────────────────────────────────────


class TestLandingSeam:
    def test_snapshot_survives_a_merge_that_deletes_the_worktree(self, tmp_path: Path) -> None:
        """AC1: the record is written after landing has destroyed the evidence.

        This is the whole reason capture is a pre-cleanup seam rather than
        something the audit writer recomputes: by the time ``_write_story_audit``
        runs, there is no worktree left to ask.
        """
        project_root, workspace_path, branch = _story_repo(tmp_path, slug="story")
        config = _make_config(project_root)
        task = TaskStory(name="Story", slug="story", story_path=None)
        state = CoordinatorState()
        state.run_id = "deadbeefcafe"
        state.started_at = "2026-01-01T00:00:00+00:00"
        state.workspace_path = workspace_path
        state.branch_name = branch
        expected_head = _git(workspace_path, "rev-parse", "HEAD").strip()

        merge_info, landing_status = land_story(
            config, task, branch, workspace_path, None, state, "merge"
        )

        assert merge_info["merged"] is True
        assert landing_status == "landed"
        assert not workspace_path.exists()

        result = _done_result(state)
        result.merge = merge_info
        result.landing_status = landing_status
        _write_story_audit(config, task, result)

        run_path = project_root / ".forge" / "audits" / "runs" / "deadbeefcafe.json"
        record = json.loads(run_path.read_text(encoding="utf-8"))
        assert record["changed_files"]["head_ref"] == expected_head
        assert [f["path"] for f in record["changed_files"]["files"]] == [
            "src/added.py",
            "src/rewritten.py",
        ]

    def test_capture_includes_files_the_pre_merge_cleanup_commit_lands(
        self, tmp_path: Path
    ) -> None:
        """Landing auto-commits leftover tracked changes; those are the run's too."""
        project_root, workspace_path, branch = _story_repo(tmp_path, slug="story")
        config = _make_config(project_root)
        task = TaskStory(name="Story", slug="story", story_path=None)
        _write(workspace_path / "src" / "keep.py", "# edited but never committed\n")
        state = CoordinatorState()
        state.run_id = "deadbeefcafe"
        state.workspace_path = workspace_path
        state.branch_name = branch

        merge_info, _status = land_story(
            config, task, branch, workspace_path, None, state, "merge"
        )

        assert merge_info["merged"] is True
        assert "src/keep.py" in {f["path"] for f in state.changed_files["files"]}


# ── The audit index ──────────────────────────────────────────────────────


class TestSubstrateProjection:
    def _indexed_run(self, tmp_path: Path) -> tuple[Path, dict]:
        project_root, workspace_path, _branch = _story_repo(tmp_path)
        config = _make_config(project_root)
        task = _make_task(project_root)
        state = CoordinatorState()
        state.run_id = "deadbeefcafe"
        state.started_at = "2026-01-01T00:00:00+00:00"
        state.workspace_path = workspace_path
        capture_changed_files(state, config)
        record = generate_audit_log(config, task, _done_result(state))
        audit_substrate.seed_records(project_root, [record])
        return project_root, record

    def test_rows_join_to_audit_records_and_match_git(self, tmp_path: Path) -> None:
        """AC4/AC5: per-file query by index, agreeing with what git says changed."""
        project_root, record = self._indexed_run(tmp_path)
        workspace_path = project_root / WORKTREE_PATTERN.format(slug="story")
        head = record["changed_files"]["head_ref"]
        base = record["changed_files"]["base_ref"]
        git_paths = sorted(
            p
            for p in _git(workspace_path, "diff", "--name-only", base, head).splitlines()
            if p.strip()
        )

        conn = audit_substrate.create_or_open(project_root)
        try:
            rows = conn.execute(
                "SELECT c.path, c.insertions, c.deletions, r.total_cost_usd, r.run_id "
                "FROM audit_changed_files c JOIN audit_records r ON r.run_id = c.run_id "
                "ORDER BY c.path"
            ).fetchall()
            single = audit_substrate.runs_touching_path(conn, "src/rewritten.py")
        finally:
            conn.close()

        assert [row[0] for row in rows] == git_paths
        assert {row[4] for row in rows} == {record["run_id"]}
        numstat = _git(workspace_path, "diff", "--numstat", base, head)
        expected = {
            line.split("\t")[2]: (int(line.split("\t")[0]), int(line.split("\t")[1]))
            for line in numstat.splitlines()
            if line.strip()
        }
        assert {row[0]: (row[1], row[2]) for row in rows} == expected

        assert len(single) == 1
        assert single[0]["run_id"] == record["run_id"]
        assert single[0]["insertions"] == expected["src/rewritten.py"][0]

    def test_reupsert_replaces_rather_than_accumulates_rows(self, tmp_path: Path) -> None:
        project_root, record = self._indexed_run(tmp_path)
        record["changed_files"] = {
            "base_ref": record["changed_files"]["base_ref"],
            "head_ref": record["changed_files"]["head_ref"],
            "files": [{"path": "src/only.py", "insertions": 1, "deletions": 0, "binary": False}],
        }
        audit_substrate.seed_records(project_root, [record])

        conn = audit_substrate.create_or_open(project_root)
        try:
            paths = [
                row[0] for row in conn.execute("SELECT path FROM audit_changed_files").fetchall()
            ]
        finally:
            conn.close()

        assert paths == ["src/only.py"]

    def test_absent_and_empty_file_sets_both_index_no_rows(self, tmp_path: Path) -> None:
        """The absent/empty distinction lives in the record, not in a sentinel row."""
        project_root = tmp_path / "root"
        project_root.mkdir()
        absent = {"run_id": "aaaaaaaaaaaa", "schema_version": 28, "changed_files": None}
        empty = {
            "run_id": "bbbbbbbbbbbb",
            "schema_version": 28,
            "changed_files": {"base_ref": "a" * 40, "head_ref": "b" * 40, "files": []},
        }
        audit_substrate.seed_records(project_root, [absent, empty])

        conn = audit_substrate.create_or_open(project_root)
        try:
            count = conn.execute("SELECT COUNT(*) FROM audit_changed_files").fetchone()[0]
            stored = {
                row[0]: json.loads(row[1])["changed_files"]
                for row in conn.execute("SELECT run_id, raw_json FROM audit_records")
            }
        finally:
            conn.close()

        assert count == 0
        assert stored["aaaaaaaaaaaa"] is None
        assert stored["bbbbbbbbbbbb"]["files"] == []

    def test_older_substrate_backfills_rows_from_raw_json_on_open(self, tmp_path: Path) -> None:
        """AC4: already-indexed records that carry the block reach the new table."""
        project_root, record = self._indexed_run(tmp_path)

        conn = audit_substrate.create_or_open(project_root)
        try:
            conn.execute("DROP TABLE audit_changed_files")
            conn.execute("UPDATE meta SET value = '8' WHERE key = 'schema_version'")
            conn.commit()
        finally:
            conn.close()

        conn = audit_substrate.create_or_open(project_root)
        try:
            paths = sorted(
                row[0] for row in conn.execute("SELECT path FROM audit_changed_files").fetchall()
            )
            version = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        finally:
            conn.close()

        assert paths == ["src/added.py", "src/rewritten.py"]
        assert version == str(audit_substrate.SUBSTRATE_SCHEMA_VERSION)


# ── Reading older records ────────────────────────────────────────────────


class TestBackwardCompatibility:
    def test_v27_record_reads_as_null_not_empty(self, tmp_path: Path) -> None:
        """AC6: an old run's absent file set stays absent, never "changed nothing"."""
        project_root = tmp_path / "root"
        project_root.mkdir()
        legacy = {"run_id": "cccccccccccc", "schema_version": 27, "task": {"slug": "old"}}
        audit_substrate.seed_records(project_root, [legacy])

        conn = audit_substrate.create_or_open(project_root)
        try:
            loaded = list(audit_substrate.iter_records(conn))
        finally:
            conn.close()

        assert len(loaded) == 1
        assert "changed_files" in loaded[0]
        assert loaded[0]["changed_files"] is None

    def test_migration_does_not_clobber_a_present_block(self) -> None:
        block = {"base_ref": "a" * 40, "head_ref": "b" * 40, "files": []}

        migrated = audit_substrate._migrate_v27_to_v28({"changed_files": block})

        assert migrated["changed_files"] == block
