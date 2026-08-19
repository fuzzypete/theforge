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
from theforge.coordinator.changed_files import collect_commit_files, is_commit_id
from theforge.coordinator.state import CoordinatorState

#: Realistic commit ids. The attribution path validates every "sha" it is given
#: as a bare hex commit id before any git call sees it, so a placeholder like
#: "aaa" is refused as data — which is the point (#2525).
_SHA_A = "a" * 40
_SHA_B = "b" * 40
_SHA_C = "c" * 40


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
                {"sha": _SHA_A, "slug": "issue-324", "message": "feat: a"},
                {"sha": _SHA_B, "slug": "issue-326", "message": "feat: b"},
                {"sha": _SHA_C, "slug": "issue-324", "message": "test: a"},
            ]
        }
        assert member_commit_revs(handoff, "issue-324") == [_SHA_A, _SHA_C]
        assert member_commit_revs(handoff, "issue-326") == [_SHA_B]

    def test_slug_match_is_case_and_whitespace_insensitive(self):
        handoff = {"commits": [{"sha": _SHA_A, "slug": " Issue-324 "}]}
        assert member_commit_revs(handoff, "issue-324") == [_SHA_A]

    def test_member_with_no_attributed_commits_gets_an_empty_list(self):
        """Distinct from unusable attribution: this member demonstrably has none."""
        handoff = {"commits": [{"sha": _SHA_B, "slug": "issue-326"}]}
        assert member_commit_revs(handoff, "issue-324") == []

    def test_unattributed_commits_refuse_the_whole_split(self):
        """Without slugs every member would see the group's whole change."""
        handoff = {"commits": [{"sha": _SHA_A, "message": "feat: a"}]}
        assert member_commit_revs(handoff, "issue-324") is None

    def test_attributed_commit_without_a_sha_refuses_the_split(self):
        """An incomplete set would ground findings from the commit it failed to name."""
        handoff = {
            "commits": [
                {"sha": _SHA_A, "slug": "issue-324"},
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

        story_diff = batch_member_story_diff(tmp_path, {"commits": [{"sha": _SHA_A}]}, "issue-324")

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
            {
                "source": "structured_output",
                "path": None,
                "handoff": {"commits": [{"sha": _SHA_A}]},
            },
            {
                "source": "structured_output",
                "path": None,
                "handoff": {"commits": [{"sha": _SHA_B}]},
            },
        ]
        assert latest_dev_handoff(state) == {"commits": [{"sha": _SHA_B}]}

    def test_skips_attempts_that_produced_no_structured_output(self):
        state = CoordinatorState()
        state.dev_handoff_snapshots = [
            {
                "source": "structured_output",
                "path": None,
                "handoff": {"commits": [{"sha": _SHA_A}]},
            },
            {"source": "missing", "path": None, "handoff": None},
        ]
        assert latest_dev_handoff(state) == {"commits": [{"sha": _SHA_A}]}

    def test_no_snapshots_is_none(self):
        assert latest_dev_handoff(CoordinatorState()) is None


class TestCommitIdValidation:
    """The handoff is agent output, so its "sha" values are untrusted input.

    ``_run_shell`` runs commands under ``shell=True``. A rev interpolated into
    one is not a value but a command, so attribution refuses anything that is
    not a bare hex commit id rather than sanitising it.
    """

    def test_accepts_abbreviated_and_full_hex_ids(self):
        assert is_commit_id("abc1234")
        assert is_commit_id(_SHA_A)
        assert is_commit_id("ABC1234DEF")
        assert is_commit_id("  abc1234  ")

    def test_rejects_shell_metacharacters(self):
        for hostile in (
            "abc1234; touch /tmp/pwned",
            "abc1234 && id",
            "$(id)",
            "`id`",
            "abc1234|id",
            "abc1234\nid",
            "a b",
        ):
            assert not is_commit_id(hostile), hostile

    def test_rejects_revision_expressions_and_ref_names(self):
        # Valid revisions to git, none of them a thing an agent should name.
        for rev in ("HEAD", "HEAD~2", "main", "abc1234^", "abc1234..def5678", "--all"):
            assert not is_commit_id(rev), rev

    def test_rejects_non_strings_and_wrong_lengths(self):
        assert not is_commit_id(None)
        assert not is_commit_id(1234567)
        assert not is_commit_id("")
        assert not is_commit_id("abc12")  # too short to be a git abbreviation
        assert not is_commit_id("a" * 41)
        assert not is_commit_id("g" * 40)  # not hex

    def test_hostile_sha_refuses_the_attribution_before_any_git_call(self, tmp_path):
        handoff = {"commits": [{"sha": "abc1234; touch pwned", "slug": "issue-324"}]}

        assert member_commit_revs(handoff, "issue-324") is None

        story_diff = batch_member_story_diff(tmp_path, handoff, "issue-324")
        assert story_diff.files is None
        assert not (tmp_path / "pwned").exists()

    def test_collect_commit_files_refuses_an_unvalidated_hostile_rev(self, tmp_path):
        """Defence in depth: the git-level helper validates too, not just its caller."""
        _init_repo(tmp_path)
        marker = tmp_path / "pwned"

        assert collect_commit_files(tmp_path, [f"HEAD; touch {marker}"]) is None
        assert collect_commit_files(tmp_path, ["HEAD"]) is None  # not a commit id
        assert not marker.exists()

    def test_collect_commit_files_still_reads_a_real_commit(self, tmp_path):
        _init_repo(tmp_path)
        sha = _commit(tmp_path, "src/a.py", "feat: a")

        snapshot = collect_commit_files(tmp_path, [sha])

        assert snapshot is not None
        assert [entry["path"] for entry in snapshot["files"]] == ["src/a.py"]
        assert snapshot["commits"] == [sha]
