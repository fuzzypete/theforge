"""Tests for the shared branch-landing resolver (#2795).

One resolver answers "has this branch's work landed on the base branch?" for
both consumers: resume triage in :mod:`theforge.sprint.dag` and the worktree
sweep in :mod:`theforge.coordinator.workspace`. This file owns the resolver's
own behaviour — every evidence source it consults, the order it consults them
in, and the tri-state it reports. ``_is_branch_merged`` is exercised here too,
as the boolean face the dependency-satisfaction and cached-preflight callers
still ask through.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from landing_evidence_test_helpers import publish_landed

from theforge.coordinator.branch_landing import (
    LANDED,
    UNDECIDABLE,
    UNLANDED,
    BranchLanding,
    _branch_adds_content_to_base,
    _has_base_commit_closing_issue,
    resolve_branch_landing,
)
from theforge.sprint.dag import _is_branch_merged

# ── _is_branch_merged: fast-forward merge regression ─────────────────


def _mock_git_ff(cmd: list[str], **kwargs: object) -> MagicMock:
    """Mock git: --is-ancestor returns 0, rev-list count returns 0 (same tip = FF)."""
    m = MagicMock()
    m.returncode = 0
    if "rev-list" in cmd and "--count" in cmd:
        m.stdout = b"0"  # branch and base at same commit after FF
    else:
        m.stdout = b""
    return m


def _mock_git_not_ancestor(cmd: list[str], **kwargs: object) -> MagicMock:
    """Mock git: --is-ancestor returns 1 (branch not ancestor of base)."""
    m = MagicMock()
    if "--is-ancestor" in cmd:
        m.returncode = 1
    else:
        m.returncode = 0
        m.stdout = b""
    return m


def test_is_branch_merged_ff_with_audit_approve(tmp_path: Path) -> None:
    """After FF merge (branch = base tip), audit trail APPROVE → True."""
    with (
        patch("theforge.coordinator.branch_landing.subprocess.run", side_effect=_mock_git_ff),
        patch("theforge.coordinator.branch_landing.has_review_approve", return_value=True),
    ):
        result = _is_branch_merged("forge/story-a", "main", tmp_path, slug="story-a")
    assert result is True


def test_is_branch_merged_squash_merge_with_audit_approve(tmp_path: Path) -> None:
    """Squash merges still resolve via audit trail when git topology is not ancestor."""
    with (
        patch(
            "theforge.coordinator.branch_landing.subprocess.run",
            side_effect=_mock_git_not_ancestor,
        ),
        patch("theforge.coordinator.branch_landing.has_review_approve", return_value=True),
    ):
        result = _is_branch_merged("forge/story-a", "main", tmp_path, slug="story-a")
    assert result is True


def test_is_branch_merged_ff_no_audit(tmp_path: Path) -> None:
    """After FF merge (branch = base tip), no audit trail entry → False (fresh branch)."""
    with (
        patch("theforge.coordinator.branch_landing.subprocess.run", side_effect=_mock_git_ff),
        patch("theforge.coordinator.branch_landing.has_review_approve", return_value=False),
    ):
        result = _is_branch_merged("forge/story-a", "main", tmp_path, slug="story-a")
    assert result is False


def test_is_branch_merged_ff_no_slug(tmp_path: Path) -> None:
    """After FF merge with no slug → False (no audit check possible, conservative)."""
    with patch("theforge.coordinator.branch_landing.subprocess.run", side_effect=_mock_git_ff):
        result = _is_branch_merged("forge/story-a", "main", tmp_path)
    assert result is False


def _no_external_evidence():
    """Silence every source but git topology, so a verdict can only come from it."""
    return (
        patch("theforge.coordinator.branch_landing.has_review_approve", return_value=False),
        patch("theforge.coordinator.branch_landing._merged_pr_probe", return_value=(None, True)),
    )


def _regular_merged_repo(path: Path, branch: str = "forge/story-a") -> None:
    """A real ``--no-ff`` merge: the branch entered base as a merge's second parent.

    Base also advances on its own first, so the merge cannot fast-forward — this
    is the ordinary merge-commit history, the one shape topology can prove.
    """
    _seed_repo(path)
    _git(path, "checkout", "-q", "-b", branch)
    for i in (1, 2):
        (path / f"w{i}.txt").write_text(f"work {i}\n")
        _git(path, "add", ".")
        _git(path, "commit", "-q", "-m", f"wip {i}")
    _git(path, "checkout", "-q", "main")
    (path / "base.txt").write_text("base moved on\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "base: unrelated work")
    _git(path, "merge", "-q", "--no-ff", "-m", f"Merge branch '{branch}'", branch)


def test_is_branch_merged_regular_merge(tmp_path: Path) -> None:
    """A real merge commit is topology evidence on its own — no audit, no PR.

    Regression for the cycle-1 P1: the old check demanded the branch still have
    commits base lacks, which an ancestor branch never does, so every genuine
    merge resolved as undecidable. This runs against real git rather than a mock
    that could assert a shape git cannot produce.
    """
    _regular_merged_repo(tmp_path)
    approve, probe = _no_external_evidence()

    with approve, probe:
        landing = resolve_branch_landing("forge/story-a", "main", tmp_path, slug="story-a")

    assert landing.status == LANDED
    assert landing.source == "topology"
    assert landing.describe_source("main") == "branch merged into main history"
    with approve, probe:
        assert _is_branch_merged("forge/story-a", "main", tmp_path, slug="story-a") is True


def test_is_branch_merged_regular_merge_needs_no_slug(tmp_path: Path) -> None:
    """Topology is local evidence: it decides without a story slug or an issue number."""
    _regular_merged_repo(tmp_path)
    _, probe = _no_external_evidence()

    with probe:
        landing = resolve_branch_landing("forge/story-a", "main", tmp_path)

    assert landing.status == LANDED
    assert landing.source == "topology"


def test_is_branch_merged_stale_empty_branch(tmp_path: Path) -> None:
    """Base moving past an empty branch must not count as merged.

    The branch is an ancestor of base and base is ahead of it — the same two
    facts a merge produces. It is separated by first-parent position: this tip
    is a plain old base commit, not a merged-in second parent.
    """
    _seed_repo(tmp_path)
    _git(tmp_path, "branch", "forge/story-a")
    for i in (1, 2):
        (tmp_path / f"b{i}.txt").write_text(f"base {i}\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-q", "-m", f"base {i}")

    approve, probe = _no_external_evidence()
    with approve, probe:
        landing = resolve_branch_landing("forge/story-a", "main", tmp_path, slug="story-a")

    assert landing.status == UNDECIDABLE
    assert landing.landed is False
    assert "branch is not merged into main by topology" in landing.describe_absent()


def test_is_branch_merged_fast_forward_merge_is_not_topology_evidence(tmp_path: Path) -> None:
    """A fast-forwarded branch sits at base's tip, which proves nothing on its own."""
    _seed_repo(tmp_path)
    _git(tmp_path, "checkout", "-q", "-b", "forge/story-a")
    (tmp_path / "w1.txt").write_text("work\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "wip")
    _git(tmp_path, "checkout", "-q", "main")
    _git(tmp_path, "merge", "-q", "--ff-only", "forge/story-a")

    approve, probe = _no_external_evidence()
    with approve, probe:
        landing = resolve_branch_landing("forge/story-a", "main", tmp_path, slug="story-a")

    assert landing.source != "topology"
    assert landing.landed is False


def test_is_branch_merged_not_ancestor_without_audit(tmp_path: Path) -> None:
    """Branch not an ancestor of base with no APPROVE audit stays unmerged."""
    with (
        patch(
            "theforge.coordinator.branch_landing.subprocess.run",
            side_effect=_mock_git_not_ancestor,
        ),
        patch("theforge.coordinator.branch_landing.has_review_approve", return_value=False),
    ):
        result = _is_branch_merged("forge/story-a", "main", tmp_path, slug="story-a")
    assert result is False


def _is_issue_grep(cmd: list[str], issue_number: int) -> bool:
    """True when ``cmd`` is the base-commit scan for ``issue_number``.

    Matches on the presence of a ``--grep=`` argument mentioning the issue
    rather than on its exact spelling: the prefilter pattern is an
    implementation detail, and pinning it here would make these mocks agree
    with a prefilter that no longer matches what git is actually asked.
    """
    return cmd[:2] == ["git", "log"] and any(
        c.startswith("--grep=") and str(issue_number) in c for c in cmd
    )


def _mock_base_commit(message: bytes, issue_number: int = 265):
    """Git mock: branch is not an ancestor, base carries ``message`` for the issue."""

    def _run(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        if "--is-ancestor" in cmd:
            m.returncode = 1
            m.stdout = b""
        elif _is_issue_grep(cmd, issue_number):
            m.returncode = 0
            m.stdout = message
        else:
            m.returncode = 0
            m.stdout = b""
        return m

    return _run


def test_is_branch_merged_external_squash_merge_by_closing_reference(tmp_path: Path) -> None:
    """A GitHub squash commit that *closes* the issue counts even without audit."""

    with (
        patch(
            "theforge.coordinator.branch_landing.subprocess.run",
            side_effect=_mock_base_commit(b"fix(sprint): tighten evidence\n\nCloses #265\n\x1e"),
        ),
        patch("theforge.sprint.dag._is_issue_closed", return_value=True),
    ):
        result = _is_branch_merged("feat/issue-265", "main", tmp_path)
    assert result is True


def test_is_branch_merged_bare_issue_mention_is_not_merge_evidence(tmp_path: Path) -> None:
    """A commit that merely mentions the issue is not evidence its branch merged (#2374).

    The commit below is the shape that caused the bug: a configuration change
    citing an unrelated open issue as context for a separate decision.
    """

    with (
        patch(
            "theforge.coordinator.branch_landing.subprocess.run",
            side_effect=_mock_base_commit(
                b"config: raise timeout and disable model\n\nDisabled pending #265\n\x1e"
            ),
        ),
        patch("theforge.coordinator.branch_landing.has_review_approve", return_value=False),
    ):
        evidence = resolve_branch_landing("feat/issue-265", "main", tmp_path, slug="issue-265")
    assert evidence.landed is False
    assert evidence.source is None


def test_is_branch_merged_closing_reference_for_other_issue_does_not_match(
    tmp_path: Path,
) -> None:
    """``Closes #2650`` must not satisfy issue 265 — the digits are a prefix."""

    with (
        patch(
            "theforge.coordinator.branch_landing.subprocess.run",
            side_effect=_mock_base_commit(b"fix: other thing\n\nCloses #2650\n\x1e"),
        ),
        patch("theforge.coordinator.branch_landing.has_review_approve", return_value=False),
    ):
        evidence = resolve_branch_landing("feat/issue-265", "main", tmp_path, slug="issue-265")
    assert evidence.landed is False


def _git(path: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


def _seed_repo(path: Path) -> None:
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("seed\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "seed")


def _squash_merged_repo(path: Path, base_message: str) -> None:
    """Real squash merge: branch survives with unique commits, work is on base.

    This is the shape ``git merge-base --is-ancestor`` fails on while
    ``git rev-list main..branch`` is non-zero — topologically indistinguishable
    from an unmerged branch, which is why this evidence source exists.
    """
    _seed_repo(path)
    _git(path, "checkout", "-q", "-b", "feat/issue-265")
    for i in (1, 2):
        (path / f"w{i}.txt").write_text(f"work {i}\n")
        _git(path, "add", ".")
        _git(path, "commit", "-q", "-m", f"wip {i}")
    _git(path, "checkout", "-q", "main")
    _git(path, "merge", "-q", "--squash", "feat/issue-265")
    _git(path, "commit", "-q", "-m", base_message)


def _unmerged_repo(path: Path, base_message: str) -> None:
    """The reported shape: five preserved commits, none of that work on base."""
    _seed_repo(path)
    _git(path, "checkout", "-q", "-b", "feat/issue-265")
    for i in range(1, 6):
        (path / f"w{i}.txt").write_text(f"work {i}\n")
        _git(path, "add", ".")
        _git(path, "commit", "-q", "-m", f"wip {i}")
    _git(path, "checkout", "-q", "main")
    (path / "config.yml").write_text("timeout: 30\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", base_message)


def _real_git_evidence(tmp_path: Path):
    """Resolve merge evidence against real git, with only external deps mocked."""
    with (
        patch(
            "theforge.coordinator.branch_landing._lookup_merged_pr_for_branch", return_value=None
        ),
        patch("theforge.coordinator.branch_landing.has_review_approve", return_value=False),
    ):
        return resolve_branch_landing("feat/issue-265", "main", tmp_path, slug="issue-265")


def test_real_squash_merge_with_closing_reference_is_merged(tmp_path: Path) -> None:
    """A real squash merge is detected even though the branch still has unique commits.

    Regression for the cycle-2 P1: vetoing on non-ancestor-plus-unique-commits
    suppressed every genuine squash merge whose source branch still existed,
    which is exactly what the issue-commit source is for. Topology cannot tell
    this repo from the unmerged one below; only content can.
    """
    _squash_merged_repo(tmp_path, "feat: the thing (#900)\n\nCloses #265")

    evidence = _real_git_evidence(tmp_path)

    assert evidence.landed is True
    assert evidence.source == "issue_commit"


def test_real_unmerged_branch_with_closing_reference_is_not_merged(tmp_path: Path) -> None:
    """A closing reference does not land work that is absent from base.

    Same topology as the squash repo above — non-ancestor, unique commits — but
    the branch's files are genuinely not on base, so the content veto fires.
    """
    _unmerged_repo(tmp_path, "chore: sweep\n\nCloses #265")

    evidence = _real_git_evidence(tmp_path)

    assert evidence.landed is False


def test_real_unmerged_branch_with_bare_mention_is_not_merged(tmp_path: Path) -> None:
    """The reported incident, end to end against real git (#2374)."""
    _unmerged_repo(
        tmp_path,
        "config: raise timeout, disable model\n\nDisabled pending #265",
    )

    evidence = _real_git_evidence(tmp_path)

    assert evidence.landed is False


def test_merge_tree_refusal_on_stdout_is_not_read_as_a_conflict(tmp_path: Path) -> None:
    """Exit 1 with a non-oid first line is a refusal, not a conflict.

    Today git writes "not something we can merge" to stderr and leaves stdout
    empty, so this shape cannot be produced from a real repo — it is mocked
    deliberately. It guards the branch of the exit-1 check that the real-git
    deleted-branch test cannot reach.
    """

    def _mock_refusal(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        if cmd[:2] == ["git", "merge-tree"]:
            m.returncode = 1
            m.stdout = b"merge-tree: nosuch - not something we can merge\n"
        else:
            m.returncode = 0
            m.stdout = b""
        return m

    with patch("theforge.coordinator.branch_landing.subprocess.run", side_effect=_mock_refusal):
        assert _branch_adds_content_to_base(tmp_path, "main", "feat/issue-265") is False


def _conflicting_repo(path: Path, base_message: str) -> None:
    """Unmerged branch that edits the same lines base edits, so replay conflicts."""
    _seed_repo(path)
    (path / "f.txt").write_text("one\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "add f")
    _git(path, "checkout", "-q", "-b", "feat/issue-265")
    (path / "f.txt").write_text("branch side\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "branch edit")
    _git(path, "checkout", "-q", "main")
    (path / "f.txt").write_text("base side\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", base_message)


def test_real_conflicting_unmerged_branch_is_not_merged(tmp_path: Path) -> None:
    """A conflict is content evidence of divergence, not an inconclusive result.

    Regression for the cycle-3 P1: treating every non-zero ``merge-tree`` exit
    as "cannot determine" let a conflicting, definitively unmerged branch be
    skipped on the strength of a closing reference in a base commit.
    """
    _conflicting_repo(tmp_path, "fix: base side wins\n\nCloses #265")

    evidence = _real_git_evidence(tmp_path)

    assert evidence.landed is False


def test_real_deleted_branch_with_closing_reference_is_merged(tmp_path: Path) -> None:
    """A missing branch must not be mistaken for a conflict.

    ``merge-tree`` reports an unknown ref with exit 1 — the same status as a
    conflict — so only the tree-oid check separates them. An externally merged
    branch that was then deleted has no evidence left but the closing
    reference, and vetoing it here would discard that entirely.
    """
    _seed_repo(tmp_path)
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "feat: landed\n\nCloses #265")

    evidence = _real_git_evidence(tmp_path)

    assert evidence.landed is True
    assert evidence.source == "issue_commit"


def test_real_squash_merge_vetoed_by_unsuccessful_audit(tmp_path: Path) -> None:
    """A recorded unsuccessful run with nothing landed still outranks the text.

    Content alone would accept this repo — the work *is* on base — so this pins
    the audit veto specifically, not the content check.
    """
    _squash_merged_repo(tmp_path, "feat: the thing (#900)\n\nCloses #265")
    from theforge.coordinator import audit_substrate

    audit_substrate.seed_records(
        tmp_path,
        [
            {
                "task": {"slug": "issue-265"},
                "run_id": "sr-265",
                "landing_status": "",
                "outcome": {"success": False},
                "reviews": [{"verdict": "REQUEST_CHANGES"}],
            }
        ],
    )

    evidence = _real_git_evidence(tmp_path)

    assert evidence.landed is False


def test_branch_merge_evidence_unsuccessful_audit_vetoes_closing_reference(
    tmp_path: Path,
) -> None:
    """An unsuccessful last run with nothing landed outranks a textual match (#2374)."""

    with (
        patch(
            "theforge.coordinator.branch_landing.subprocess.run",
            side_effect=_mock_base_commit(b"chore: sweep\n\nCloses #265\n\x1e"),
        ),
        patch("theforge.coordinator.branch_landing.has_review_approve", return_value=False),
        patch(
            "theforge.coordinator.branch_landing.latest_run_outcome",
            return_value={
                "outcome_success": 0,
                "verdict": "REQUEST_CHANGES",
                "landing_status": "",
            },
        ),
    ):
        evidence = resolve_branch_landing("feat/issue-265", "main", tmp_path, slug="issue-265")
    assert evidence.landed is False


def test_branch_merge_evidence_landed_audit_does_not_veto_closing_reference(
    tmp_path: Path,
) -> None:
    """A landed prior run is not a contradiction, so the closing reference stands."""

    with (
        patch(
            "theforge.coordinator.branch_landing.subprocess.run",
            side_effect=_mock_base_commit(b"chore: sweep\n\nCloses #265\n\x1e"),
        ),
        patch("theforge.coordinator.branch_landing.has_review_approve", return_value=False),
        patch(
            "theforge.coordinator.branch_landing.latest_run_outcome",
            return_value={
                "outcome_success": 1,
                "verdict": "APPROVE",
                "landing_status": "landed",
            },
        ),
    ):
        evidence = resolve_branch_landing("feat/issue-265", "main", tmp_path, slug="issue-265")
    assert evidence.landed is True
    assert evidence.source == "issue_commit"


def _init_repo_with_commit(path: Path, message: str) -> None:
    """Create a real single-branch git repo whose HEAD commit carries ``message``."""
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=path, check=True)


@pytest.mark.parametrize(
    ("message", "issue_number", "expected"),
    [
        # Every spelling the matcher advertises must survive the git prefilter.
        ("fix: land it\n\nCloses #265", 265, True),
        ("fix: land it\n\nCloses GH-265", 265, True),
        ("fix: land it\n\ncloses gh-265", 265, True),
        ("fix: land it\n\nResolves owner/repo#265", 265, True),
        ("fix: land it\n\nFixes: #265", 265, True),
        # Bare mentions are not evidence.
        ("config: disable model\n\nDisabled pending #265", 265, False),
        ("feat: thing (#265)", 265, False),
        # Adjacent issue numbers must not collide.
        ("fix: other\n\nCloses #2650", 265, False),
        ("fix: other\n\nCloses GH-2650", 265, False),
    ],
)
def test_has_base_commit_closing_issue_against_real_git(
    tmp_path: Path, message: str, issue_number: int, expected: bool
) -> None:
    """Exercise the real ``git log`` prefilter, not a mock of it (#2374).

    The prefilter and the Python matcher are two separate patterns over the
    same message. A mocked ``subprocess.run`` cannot catch them disagreeing —
    which is exactly how ``Closes GH-N`` came to be advertised by the matcher
    while the ``--fixed-strings --grep=#N`` prefilter dropped it before the
    matcher ever ran.
    """
    _init_repo_with_commit(tmp_path, message)
    assert _has_base_commit_closing_issue(tmp_path, "main", issue_number) is expected


def test_branch_merge_evidence_audit_veto_reads_real_substrate(tmp_path: Path) -> None:
    """The audit veto works end-to-end against a seeded substrate, not just a mock.

    Topology here is squash-merge-shaped (not an ancestor, no unique commits) so
    it cannot veto on its own — the recorded unsuccessful outcome must.
    """
    from theforge.coordinator import audit_substrate

    audit_substrate.seed_records(
        tmp_path,
        [
            {
                "task": {"slug": "issue-265"},
                "run_id": "sr-265",
                "landing_status": "",
                "outcome": {"success": False},
                "reviews": [{"verdict": "REQUEST_CHANGES"}],
            }
        ],
    )

    with (
        patch(
            "theforge.coordinator.branch_landing.subprocess.run",
            side_effect=_mock_base_commit(b"chore: sweep\n\nCloses #265\n\x1e"),
        ),
        patch("theforge.coordinator.branch_landing.has_review_approve", return_value=False),
    ):
        evidence = resolve_branch_landing("feat/issue-265", "main", tmp_path, slug="issue-265")
    assert evidence.landed is False


def test_branch_merge_evidence_audit_read_failure_does_not_veto(tmp_path: Path) -> None:
    """An unreadable audit has no opinion — it must not invert into a veto."""

    with (
        patch(
            "theforge.coordinator.branch_landing.subprocess.run",
            side_effect=_mock_base_commit(b"chore: sweep\n\nCloses #265\n\x1e"),
        ),
        patch("theforge.coordinator.branch_landing.has_review_approve", return_value=False),
        patch(
            "theforge.coordinator.branch_landing.latest_run_outcome",
            side_effect=OSError("audit unreadable"),
        ),
    ):
        evidence = resolve_branch_landing("feat/issue-265", "main", tmp_path, slug="issue-265")
    assert evidence.landed is True


def test_is_branch_merged_external_squash_merge_by_merged_pr_lookup(tmp_path: Path) -> None:
    """A merged PR for the issue branch counts even without audit or commit grep."""

    def _mock_external_pr(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        if "--is-ancestor" in cmd:
            m.returncode = 1
            m.stdout = b""
        elif _is_issue_grep(cmd, 1102):
            m.returncode = 0
            m.stdout = b""
        elif cmd[:3] == ["gh", "pr", "list"]:
            m.returncode = 0
            m.stdout = (
                '[{"number":1111,"url":"https://github.com/o/r/pull/1111",'
                '"mergedAt":"2026-05-01T12:34:56Z","baseRefName":"main"}]'
            )
        else:
            m.returncode = 0
            m.stdout = b""
        return m

    with (
        patch("theforge.coordinator.branch_landing.subprocess.run", side_effect=_mock_external_pr),
        patch("theforge.sprint.dag._is_issue_closed", return_value=True),
        patch("theforge.coordinator.branch_landing.has_review_approve", return_value=False),
    ):
        result = _is_branch_merged("feat/issue-1102", "main", tmp_path, slug="issue-1102")
    assert result is True


def test_is_branch_merged_issue_branch_without_base_commit_or_audit(tmp_path: Path) -> None:
    """Issue branch stays unmerged when base has no matching squash commit."""

    def _mock_no_external_squash(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        if "--is-ancestor" in cmd:
            m.returncode = 1
            m.stdout = b""
        elif _is_issue_grep(cmd, 265):
            m.returncode = 0
            m.stdout = b""
        else:
            m.returncode = 0
            m.stdout = b""
        return m

    with (
        patch(
            "theforge.coordinator.branch_landing.subprocess.run",
            side_effect=_mock_no_external_squash,
        ),
        patch("theforge.coordinator.branch_landing.has_review_approve", return_value=False),
    ):
        result = _is_branch_merged("feat/issue-265", "main", tmp_path, slug="issue-265")
    assert result is False


def test_is_branch_merged_open_issue_does_not_block_merge_evidence(tmp_path: Path) -> None:
    """An open GitHub issue no longer suppresses merge evidence (#2111).

    Symptom bugs are deliberately held open pending verification after their fix
    lands, so issue state is not a precondition for detecting the merge.
    """

    with (
        patch(
            "theforge.coordinator.branch_landing.subprocess.run",
            side_effect=_mock_base_commit(b"fix: land it\n\nCloses #265\n\x1e"),
        ),
        patch("theforge.sprint.dag._is_issue_closed", return_value=False),
        patch("theforge.coordinator.branch_landing.has_review_approve", return_value=False),
    ):
        result = _is_branch_merged("feat/issue-265", "main", tmp_path, slug="issue-265")
    assert result is True


def test_branch_merge_evidence_never_consults_issue_state(tmp_path: Path) -> None:
    """Issue state is not a gate on branch merge detection at all (#2111)."""

    def _mock_no_evidence(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        m.returncode = 1
        m.stdout = b""
        return m

    with (
        patch("theforge.coordinator.branch_landing.subprocess.run", side_effect=_mock_no_evidence),
        patch("theforge.coordinator.branch_landing.has_review_approve", return_value=False),
        patch("theforge.sprint.dag._is_issue_closed") as is_issue_closed,
    ):
        evidence = resolve_branch_landing("feat/issue-265", "main", tmp_path, slug="issue-265")

    assert evidence.landed is False
    is_issue_closed.assert_not_called()


def test_branch_merge_evidence_prefers_owned_audit_over_issue_commit(tmp_path: Path) -> None:
    """Owned audit evidence is consulted before the loose issue-commit grep."""

    grepped: list[list[str]] = []

    def _mock_run(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        if cmd[:2] == ["git", "log"] and any(c.startswith("--grep=") for c in cmd):
            grepped.append(cmd)
        m.returncode = 1
        m.stdout = b""
        return m

    with (
        patch("theforge.coordinator.branch_landing.subprocess.run", side_effect=_mock_run),
        patch("theforge.coordinator.branch_landing.has_review_approve", return_value=True),
    ):
        evidence = resolve_branch_landing("feat/issue-265", "main", tmp_path, slug="issue-265")

    assert evidence.landed is True
    assert evidence.source == "audit"
    assert grepped == []


def test_branch_merge_evidence_audit_error_falls_through_to_pr_lookup(tmp_path: Path) -> None:
    """A transient audit-read failure must not discard the merged-PR fallback."""

    def _mock_run(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        if cmd[:3] == ["gh", "pr", "list"]:
            m.returncode = 0
            m.stdout = (
                '[{"number":1111,"url":"https://github.com/o/r/pull/1111",'
                '"mergedAt":"2026-05-01T12:34:56Z","baseRefName":"main"}]'
            )
        else:
            m.returncode = 1
            m.stdout = b""
        return m

    with (
        patch("theforge.coordinator.branch_landing.subprocess.run", side_effect=_mock_run),
        patch(
            "theforge.coordinator.branch_landing.has_review_approve",
            side_effect=OSError("audit unreadable"),
        ),
    ):
        evidence = resolve_branch_landing("feat/issue-265", "main", tmp_path, slug="issue-265")

    assert evidence.landed is True
    assert evidence.source == "github_pr"
    assert evidence.pr_number == 1111


def test_is_branch_merged_squash_merge_reads_real_audit_history(tmp_path: Path) -> None:
    """Squash merges use persisted APPROVE history even though branch stays ahead."""
    from theforge.coordinator import audit_substrate

    audit_substrate.seed_records(
        tmp_path,
        [
            {
                "run_id": "rec-landed",
                "task": {"slug": "story-a"},
                "landing_status": "landed",
                "reviews": [{"verdict": "APPROVE"}],
            }
        ],
    )
    # Since #2849 the landed query reads the published assertion, not the
    # flattened column above.
    publish_landed(tmp_path, "rec-landed", slug="story-a")

    def _mock_squash(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        if "--is-ancestor" in cmd:
            m.returncode = 1
            m.stdout = b""
        elif cmd[:2] == ["git", "rev-list"] and cmd[2] == "main..forge/story-a":
            m.returncode = 0
            m.stdout = b"2"
        else:
            m.returncode = 0
            m.stdout = b""
        return m

    with patch("theforge.coordinator.branch_landing.subprocess.run", side_effect=_mock_squash):
        result = _is_branch_merged("forge/story-a", "main", tmp_path, slug="story-a")
    assert result is True


def test_is_branch_merged_squash_merge_ignores_failed_landing_audit(tmp_path: Path) -> None:
    """Failed landing history must not satisfy squash-merge detection."""
    from theforge.coordinator import audit_substrate

    audit_substrate.seed_records(
        tmp_path,
        [
            {
                "run_id": "rec-failed",
                "task": {"slug": "story-a"},
                "landing_status": "failed",
                "reviews": [{"verdict": "APPROVE"}],
            }
        ],
    )

    def _mock_squash(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        if "--is-ancestor" in cmd:
            m.returncode = 1
            m.stdout = b""
        elif cmd[:2] == ["git", "rev-list"] and cmd[2] == "main..forge/story-a":
            m.returncode = 0
            m.stdout = b"2"
        else:
            m.returncode = 0
            m.stdout = b""
        return m

    with patch("theforge.coordinator.branch_landing.subprocess.run", side_effect=_mock_squash):
        result = _is_branch_merged("forge/story-a", "main", tmp_path, slug="story-a")
    assert result is False


# ── Tri-state: landed / unlanded / undecidable (#2795) ──────────────────
#
# The sweep and resume triage both consume this, and they need different
# things from it: triage acts on "landed", the sweep has to *report* the other
# two apart. A boolean cannot carry that, and the sweep's own question — are
# this branch's commits on origin? — answers none of it.


def _mock_git_squash_shape(cmd: list[str], **kwargs: object) -> MagicMock:
    """Git as it looks at a squash-merged branch: not an ancestor, unique commits."""
    m = MagicMock()
    if "--is-ancestor" in cmd:
        m.returncode = 1
        m.stdout = b""
    elif cmd[:2] == ["git", "merge-tree"]:
        # Replaying the branch onto base is a no-op: the work is already there.
        m.returncode = 0
        m.stdout = b"a" * 40 + b"\n"
    elif cmd[:2] == ["git", "rev-parse"]:
        m.returncode = 0
        m.stdout = b"a" * 40 + b"\n"
    else:
        m.returncode = 0
        m.stdout = b""
    return m


def _mock_gh_merged_pr(number: int, cmd: list[str]) -> MagicMock | None:
    m = MagicMock()
    m.returncode = 0
    m.stdout = (
        f'[{{"number":{number},"url":"https://github.com/o/r/pull/{number}",'
        '"mergedAt":"2026-05-01T12:34:56Z","baseRefName":"main"}]'
    )
    return m


@pytest.mark.parametrize(
    ("branch", "slug", "pr_number"),
    [
        # The two branches named in #2795, as controlled evidence rather than
        # as live branches this checkout has to still be carrying.
        ("feat/issue-2553", "issue-2553", 2577),
        ("feat/issue-2365", "issue-2365", 2461),
    ],
)
def test_squash_landed_branch_resolves_as_landed(
    tmp_path: Path, branch: str, slug: str, pr_number: int
) -> None:
    """A squash landing is LANDED even though the branch's commits never reach origin."""

    def _run(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[:3] == ["gh", "pr", "list"]:
            return _mock_gh_merged_pr(pr_number, cmd)
        return _mock_git_squash_shape(cmd, **kwargs)

    with (
        patch("theforge.coordinator.branch_landing.subprocess.run", side_effect=_run),
        patch("theforge.coordinator.branch_landing.has_review_approve", return_value=False),
    ):
        landing = resolve_branch_landing(branch, "main", tmp_path, slug=slug)

    assert landing.status == LANDED
    assert landing.landed is True
    assert landing.source == "github_pr"
    assert landing.pr_number == pr_number
    assert landing.describe_source("main") == f"merged PR #{pr_number}"


def test_local_only_commits_without_merge_evidence_resolve_as_unlanded(
    tmp_path: Path,
) -> None:
    """Work provably absent from base is UNLANDED — the case preservation exists for."""
    _unmerged_repo(tmp_path, "chore: unrelated sweep")

    with (
        patch("theforge.coordinator.branch_landing._merged_pr_probe", return_value=(None, True)),
        patch("theforge.coordinator.branch_landing.has_review_approve", return_value=False),
    ):
        landing = resolve_branch_landing("feat/issue-265", "main", tmp_path, slug="issue-265")

    assert landing.status == UNLANDED
    assert landing.landed is False
    assert landing.source == "content"


def test_unreadable_evidence_resolves_as_undecidable_and_names_what_was_absent(
    tmp_path: Path,
) -> None:
    """A failed lookup is not a statement that no PR exists — it is an absence."""
    _squash_merged_repo(tmp_path, "feat: the thing (#900)")

    with (
        patch("theforge.coordinator.branch_landing._merged_pr_probe", return_value=(None, False)),
        patch("theforge.coordinator.branch_landing.has_review_approve", return_value=False),
    ):
        landing = resolve_branch_landing("feat/issue-265", "main", tmp_path, slug="issue-265")

    assert landing.status == UNDECIDABLE
    assert landing.landed is False
    absent = landing.describe_absent()
    assert "the merged-PR lookup could not run" in absent
    assert "no landed APPROVE in the audit trail" in absent
    assert "no main commit closes issue #265" in absent


def test_undecidable_when_the_branch_carries_no_issue_reference(tmp_path: Path) -> None:
    """Without an issue number the PR and commit sources cannot be consulted at all."""
    _seed_repo(tmp_path)
    _git(tmp_path, "checkout", "-q", "-b", "forge/story-a")

    with patch("theforge.coordinator.branch_landing.has_review_approve", return_value=False):
        landing = resolve_branch_landing("forge/story-a", "main", tmp_path, slug="story-a")

    assert landing.status == UNDECIDABLE
    assert "no issue reference in the branch name" in landing.describe_absent()


def test_topology_failure_is_undecidable_not_unlanded(tmp_path: Path) -> None:
    """An unreadable repository has no opinion about whether the work landed."""

    def _broken(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        m.returncode = 128
        m.stdout = b""
        return m

    with (
        patch("theforge.coordinator.branch_landing.subprocess.run", side_effect=_broken),
        patch("theforge.coordinator.branch_landing.has_review_approve", return_value=False),
    ):
        landing = resolve_branch_landing("feat/issue-265", "main", tmp_path, slug="issue-265")

    assert landing.status == UNDECIDABLE
    assert "git topology could not be read" in landing.describe_absent()


# ── A merged PR is a claim about the branch, not proof of its content ──
#
# #2795 cycle 3: PR evidence was accepted on sight. Two ways that deletes work —
# the PR merged into a different branch than the one this run is configured
# against, and the branch has moved on since the PR merged — and the sweep acts
# on the answer by removing the worktree.


def _gh_merged_pr_into(base_ref: str, number: int = 77):
    """Delegate every git call to real git, and answer ``gh`` with one merged PR."""
    real_run = subprocess.run

    def _run(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[:3] == ["gh", "pr", "list"]:
            m = MagicMock()
            m.returncode = 0
            m.stdout = (
                f'[{{"number":{number},"url":"https://github.com/o/r/pull/{number}",'
                f'"mergedAt":"2026-05-01T12:34:56Z","baseRefName":"{base_ref}"}}]'
            )
            return m
        return real_run(cmd, **kwargs)

    return _run


@pytest.mark.parametrize(
    ("base_ref", "expected_status", "expected_source"),
    [
        ("main", LANDED, "github_pr"),
        # Merged, but into a release line this run was not configured against:
        # the work is not in main, so nothing here may reclaim the worktree.
        ("release/v0.14", UNDECIDABLE, None),
        ("refs/heads/main", LANDED, "github_pr"),
    ],
)
def test_merged_pr_counts_only_when_it_merged_into_the_configured_base(
    tmp_path: Path, base_ref: str, expected_status: str, expected_source: str | None
) -> None:
    """A PR merged elsewhere is not a landing on this base branch."""
    _squash_merged_repo(tmp_path, "feat: the thing")  # content is on main, no closing ref

    with (
        patch(
            "theforge.coordinator.branch_landing.subprocess.run",
            side_effect=_gh_merged_pr_into(base_ref),
        ),
        patch("theforge.coordinator.branch_landing.has_review_approve", return_value=False),
    ):
        landing = resolve_branch_landing("feat/issue-265", "main", tmp_path, slug="issue-265")

    assert landing.status == expected_status
    assert landing.source == expected_source
    if expected_status is UNDECIDABLE:
        assert "no merged PR into main for the branch" in landing.describe_absent()


def test_merged_pr_does_not_land_a_branch_whose_content_is_absent_from_base(
    tmp_path: Path,
) -> None:
    """A PR record cannot speak for commits the branch acquired after it merged.

    The repository here is the reported shape: five commits of work, none of it
    in main. GitHub reports a merged PR for the branch all the same. Accepting
    that made the sweep delete the worktree holding the only copy (#2795).
    """
    _unmerged_repo(tmp_path, "chore: unrelated sweep")

    with (
        patch(
            "theforge.coordinator.branch_landing._merged_pr_probe",
            return_value=(
                BranchLanding(
                    status=LANDED,
                    source="github_pr",
                    pr_number=999,
                    pr_url="https://github.com/o/r/pull/999",
                ),
                True,
            ),
        ),
        patch("theforge.coordinator.branch_landing.has_review_approve", return_value=False),
    ):
        landing = resolve_branch_landing("feat/issue-265", "main", tmp_path, slug="issue-265")

    assert landing.status == UNLANDED
    assert landing.landed is False
    assert landing.source == "content"
    # The PR travels with the verdict so an operator can contest it, but it is
    # not reported as what decided the verdict.
    assert landing.pr_number == 999
    assert landing.describe_source("main") == (
        "branch content is absent from main despite merged PR #999"
    )


def test_merged_pr_still_lands_a_squash_merge_whose_content_reached_base(
    tmp_path: Path,
) -> None:
    """The content guard must not suppress the squash landings this fix exists for."""
    _squash_merged_repo(tmp_path, "feat: the thing")

    with (
        patch(
            "theforge.coordinator.branch_landing.subprocess.run",
            side_effect=_gh_merged_pr_into("main", number=2577),
        ),
        patch("theforge.coordinator.branch_landing.has_review_approve", return_value=False),
    ):
        landing = resolve_branch_landing("feat/issue-265", "main", tmp_path, slug="issue-265")

    assert landing.status == LANDED
    assert landing.pr_number == 2577
