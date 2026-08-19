"""Tests for per-story file attribution inside a batch group's shared worktree.

A batch group's branch carries several independent stories. Grounding a member's
review findings against the branch diff would let a sibling member's criteria
decide this member's outcome — the #2525 failure mode one scope down. These
cover the attribution that separates them, and the refusals that keep a guessed
split from being treated as a real one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from theforge.coordinator.batch_diff import (
    SOURCE_BATCH_COMMITS,
    batch_member_story_diff,
    latest_dev_handoff,
    member_commit_revs,
)
from theforge.coordinator.state import CoordinatorState


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=path, check=True)


def _commit(path: Path, relpath: str, message: str) -> str:
    target = path / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=path, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


class TestMemberCommitRevs:
    def test_selects_only_this_member_s_commits(self):
        handoff = {
            "commits": [
                {"sha": "aaa", "slug": "issue-324", "message": "feat: a"},
                {"sha": "bbb", "slug": "issue-326", "message": "feat: b"},
                {"sha": "ccc", "slug": "issue-324", "message": "test: a"},
            ]
        }
        assert member_commit_revs(handoff, "issue-324") == ["aaa", "ccc"]
        assert member_commit_revs(handoff, "issue-326") == ["bbb"]

    def test_slug_match_is_case_and_whitespace_insensitive(self):
        handoff = {"commits": [{"sha": "aaa", "slug": " Issue-324 "}]}
        assert member_commit_revs(handoff, "issue-324") == ["aaa"]

    def test_member_with_no_attributed_commits_gets_an_empty_list(self):
        """Distinct from unusable attribution: this member demonstrably has none."""
        handoff = {"commits": [{"sha": "bbb", "slug": "issue-326"}]}
        assert member_commit_revs(handoff, "issue-324") == []

    def test_unattributed_commits_refuse_the_whole_split(self):
        """Without slugs every member would see the group's whole change."""
        handoff = {"commits": [{"sha": "aaa", "message": "feat: a"}]}
        assert member_commit_revs(handoff, "issue-324") is None

    def test_attributed_commit_without_a_sha_refuses_the_split(self):
        """An incomplete set would ground findings from the commit it failed to name."""
        handoff = {
            "commits": [
                {"sha": "aaa", "slug": "issue-324"},
                {"sha": "", "slug": "issue-324"},
            ]
        }
        assert member_commit_revs(handoff, "issue-324") is None

    def test_missing_or_malformed_handoff_refuses_the_split(self):
        assert member_commit_revs(None, "issue-324") is None
        assert member_commit_revs({}, "issue-324") is None
        assert member_commit_revs({"commits": "not-a-list"}, "issue-324") is None
        assert member_commit_revs({"commits": []}, "issue-324") is None


class TestBatchMemberStoryDiff:
    def test_member_diff_excludes_a_sibling_member_s_files(self, tmp_path):
        _init_repo(tmp_path)
        sha_a = _commit(tmp_path, "src/a.py", "feat(issue-324): a")
        sha_b = _commit(tmp_path, "src/b.py", "feat(issue-326): b")
        handoff = {
            "commits": [
                {"sha": sha_a, "slug": "issue-324", "message": "feat: a"},
                {"sha": sha_b, "slug": "issue-326", "message": "feat: b"},
            ]
        }

        story_diff = batch_member_story_diff(tmp_path, handoff, "issue-324")

        assert story_diff.source == SOURCE_BATCH_COMMITS
        assert story_diff.files == frozenset(["src/a.py"])
        # The branch diff would have been both files; the sibling's is excluded.
        assert "src/b.py" not in (story_diff.files or set())

    def test_multiple_commits_for_one_member_are_unioned(self, tmp_path):
        _init_repo(tmp_path)
        sha_1 = _commit(tmp_path, "src/a.py", "feat(issue-324): a")
        _commit(tmp_path, "src/b.py", "feat(issue-326): b")
        sha_2 = _commit(tmp_path, "tests/test_a.py", "test(issue-324): a")
        handoff = {
            "commits": [
                {"sha": sha_1, "slug": "issue-324"},
                {"sha": sha_2, "slug": "issue-324"},
            ]
        }

        story_diff = batch_member_story_diff(tmp_path, handoff, "issue-324")

        assert story_diff.files == frozenset(["src/a.py", "tests/test_a.py"])

    def test_unusable_attribution_yields_an_unknown_set_not_the_branch_diff(self, tmp_path):
        _init_repo(tmp_path)
        _commit(tmp_path, "src/a.py", "feat: a")

        story_diff = batch_member_story_diff(tmp_path, {"commits": [{"sha": "x"}]}, "issue-324")

        # None, not the branch diff: falling back would reintroduce exactly the
        # cross-member grounding this module exists to prevent.
        assert story_diff.files is None
        assert "no per-story commit attribution" in (story_diff.detail or "")

    def test_member_with_no_commits_yields_a_known_empty_set(self, tmp_path):
        _init_repo(tmp_path)
        sha_b = _commit(tmp_path, "src/b.py", "feat(issue-326): b")
        handoff = {"commits": [{"sha": sha_b, "slug": "issue-326"}]}

        story_diff = batch_member_story_diff(tmp_path, handoff, "issue-324")

        # Known-empty, not unknown: this member demonstrably changed nothing,
        # which review must stay free to fail it for.
        assert story_diff.files == frozenset()
        assert "attributes no commits" in (story_diff.detail or "")

    def test_unresolvable_sha_yields_an_unknown_set(self, tmp_path):
        _init_repo(tmp_path)
        handoff = {"commits": [{"sha": "deadbeef" * 5, "slug": "issue-324"}]}

        story_diff = batch_member_story_diff(tmp_path, handoff, "issue-324")

        assert story_diff.files is None
        assert "could not be read" in (story_diff.detail or "")

    def test_audit_record_names_the_set_and_its_provenance(self, tmp_path):
        _init_repo(tmp_path)
        sha_a = _commit(tmp_path, "src/a.py", "feat(issue-324): a")
        handoff = {"commits": [{"sha": sha_a, "slug": "issue-324"}]}

        record = batch_member_story_diff(tmp_path, handoff, "issue-324").as_audit_record()

        assert record["source"] == SOURCE_BATCH_COMMITS
        assert record["available"] is True
        assert record["files"] == ["src/a.py"]


class TestLatestDevHandoff:
    def test_returns_the_most_recent_structured_handoff(self):
        state = CoordinatorState()
        state.dev_handoff_snapshots = [
            {"source": "structured_output", "path": None, "handoff": {"commits": [{"sha": "a"}]}},
            {"source": "structured_output", "path": None, "handoff": {"commits": [{"sha": "b"}]}},
        ]
        assert latest_dev_handoff(state) == {"commits": [{"sha": "b"}]}

    def test_skips_attempts_that_produced_no_structured_output(self):
        state = CoordinatorState()
        state.dev_handoff_snapshots = [
            {"source": "structured_output", "path": None, "handoff": {"commits": [{"sha": "a"}]}},
            {"source": "missing", "path": None, "handoff": None},
        ]
        assert latest_dev_handoff(state) == {"commits": [{"sha": "a"}]}

    def test_no_snapshots_is_none(self):
        assert latest_dev_handoff(CoordinatorState()) is None
