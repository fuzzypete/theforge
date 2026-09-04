from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

from coord_test_helpers import _make_config, _make_task

from theforge.artifacts import ESCALATED_MARKER_PATH
from theforge.cli.shared import _write_audit
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.coordinator.workspace import _count_unpublished_commits, sweep_orphan_worktrees
from theforge.sprint.audit import _write_story_audit
from theforge.sprint.lock import _is_escalated_worktree


def _make_result(
    *, phase: Phase, workspace_path: Path | None = None, log_dir: Path | None = None
) -> CoordinatorResult:
    state = CoordinatorState()
    state.phase = phase
    state.workspace_path = workspace_path
    state.log_dir = log_dir
    return CoordinatorResult(
        success=phase == Phase.DONE, phase=phase, state=state, message=phase.name
    )


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_write_audit_skips_worktree_audit_for_non_escalate(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir(parents=True)
    log_dir = tmp_path / "logs" / task.slug
    result = _make_result(phase=Phase.DONE, workspace_path=workspace, log_dir=log_dir)

    _write_audit(result, config, task)

    assert not (workspace / ".forge" / "audit.yaml").exists()
    assert not (workspace / ESCALATED_MARKER_PATH).exists()
    assert (log_dir / "audit.yaml").exists()


def test_escalate_writes_marker_and_detection_uses_it(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir(parents=True)
    log_dir = tmp_path / "logs" / task.slug

    escalate_result = _make_result(phase=Phase.ESCALATE, workspace_path=workspace, log_dir=log_dir)
    _write_audit(escalate_result, config, task)

    assert (workspace / ESCALATED_MARKER_PATH).exists()
    assert _is_escalated_worktree(workspace) is True

    clean_workspace = tmp_path / "clean"
    clean_workspace.mkdir()
    assert _is_escalated_worktree(clean_workspace) is False


def test_sweep_orphan_worktrees_removes_forge_only_orphans_and_merged_but_preserves_escalated(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("root\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    _git(repo, "remote", "add", "origin", str(repo))
    _git(repo, "fetch", "origin", "main:refs/remotes/origin/main")

    base_config = _make_config(repo)
    config = dataclasses.replace(
        base_config,
        workspace=dataclasses.replace(
            base_config.workspace,
            path_pattern=".forge/worktrees/{slug}",
            branch_pattern="forge/{slug}",
            base_branch="main",
        ),
    )

    worktrees_root = repo / ".forge" / "worktrees"
    worktrees_root.mkdir(parents=True, exist_ok=True)

    orphan = worktrees_root / "orphan"
    (orphan / ".forge" / "traces").mkdir(parents=True)
    (orphan / ".forge" / "audit.yaml").write_text("outcome: {}\n", encoding="utf-8")
    (orphan / ".forge" / "traces" / "1-dev.txt").write_text("trace\n", encoding="utf-8")

    unexpected = worktrees_root / "unexpected"
    unexpected.mkdir()
    (unexpected / "README.txt").write_text("keep me\n", encoding="utf-8")

    merged = worktrees_root / "merged"
    _git(repo, "worktree", "add", "-b", "forge/merged", str(merged), "main")
    (merged / "merged.txt").write_text("done\n", encoding="utf-8")
    _git(merged, "add", "merged.txt")
    _git(merged, "commit", "-m", "merged work")
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--ff-only", "forge/merged")
    # Refresh origin/main so the merged work is actually published, matching a real
    # workflow where the PR landed on the remote before the sweep runs.
    _git(repo, "fetch", "origin", "main:refs/remotes/origin/main")

    escalated = worktrees_root / "escalated"
    _git(repo, "worktree", "add", "-b", "forge/escalated", str(escalated), "main")
    task = _make_task(repo)
    task = dataclasses.replace(task, slug="escalated")
    esc_result = _make_result(
        phase=Phase.ESCALATE, workspace_path=escalated, log_dir=repo / "logs"
    )
    _write_story_audit(config, task, esc_result, sprint_id="sprint-1")

    sweep_orphan_worktrees(repo, config)

    assert not orphan.exists()
    assert unexpected.exists()
    assert not merged.exists()
    assert not any(
        "forge/merged" in line
        for line in subprocess.run(
            ["git", "branch", "--list"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.splitlines()
    )
    assert escalated.exists()
    assert (escalated / ESCALATED_MARKER_PATH).exists()


def test_sweep_orphan_worktrees_removes_merged_branch_still_checked_out_elsewhere(
    tmp_path: Path,
) -> None:
    """git branch --merged prefixes branches checked out in another worktree with
    '+' (not '*'). The sweep must still recognize such a branch as merged, not just
    branches whose remote-tracking ref is gone."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("root\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    _git(repo, "remote", "add", "origin", str(repo))
    _git(repo, "fetch", "origin", "main:refs/remotes/origin/main")

    base_config = _make_config(repo)
    config = dataclasses.replace(
        base_config,
        workspace=dataclasses.replace(
            base_config.workspace,
            path_pattern=".forge/worktrees/{slug}",
            branch_pattern="forge/{slug}",
            base_branch="main",
        ),
    )

    worktrees_root = repo / ".forge" / "worktrees"
    worktrees_root.mkdir(parents=True, exist_ok=True)

    merged = worktrees_root / "merged-checked-out"
    _git(repo, "worktree", "add", "-b", "forge/merged-checked-out", str(merged), "main")
    (merged / "merged.txt").write_text("done\n", encoding="utf-8")
    _git(merged, "add", "merged.txt")
    _git(merged, "commit", "-m", "merged work")

    # Give the branch a live remote-tracking ref so branch_gone is False,
    # forcing the sweep to rely on the merged-branch check.
    _git(
        repo,
        "fetch",
        "origin",
        "forge/merged-checked-out:refs/remotes/origin/forge/merged-checked-out",
    )

    _git(repo, "merge", "--ff-only", "forge/merged-checked-out")

    # Refresh origin/main to include the merge commit, matching a real workflow
    # where the base branch has already been fast-forwarded on the remote.
    _git(repo, "fetch", "origin", "main:refs/remotes/origin/main")

    # The branch is still checked out in the "merged" worktree, so
    # `git branch --merged` run from `repo` reports it with a '+' prefix,
    # not '*' (which is reserved for the branch checked out in `repo` itself).
    merged_output = subprocess.run(
        ["git", "branch", "--merged", "origin/main"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert any(
        line.strip().startswith("+") and "forge/merged-checked-out" in line
        for line in merged_output.splitlines()
    )

    sweep_orphan_worktrees(repo, config)

    assert not merged.exists()
    assert not any(
        "forge/merged-checked-out" in line
        for line in subprocess.run(
            ["git", "branch", "--list"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.splitlines()
    )


def _init_sweep_repo(tmp_path: Path) -> tuple[Path, object]:
    """Create a repo with an origin remote and a forge worktrees root."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("root\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    _git(repo, "remote", "add", "origin", str(repo))
    _git(repo, "fetch", "origin", "main:refs/remotes/origin/main")

    base_config = _make_config(repo)
    config = dataclasses.replace(
        base_config,
        workspace=dataclasses.replace(
            base_config.workspace,
            path_pattern=".forge/worktrees/{slug}",
            branch_pattern="forge/{slug}",
            base_branch="main",
        ),
    )
    (repo / ".forge" / "worktrees").mkdir(parents=True, exist_ok=True)
    return repo, config


def _rev_parse(repo: Path, rev: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", rev], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_sweep_preserves_never_pushed_branch_with_committed_work(tmp_path: Path) -> None:
    """A story branch that has committed work but has never been pushed must survive
    the sweep: absence of refs/remotes/origin/<branch> is not evidence of integration."""
    repo, config = _init_sweep_repo(tmp_path)
    worktrees_root = repo / ".forge" / "worktrees"

    live = worktrees_root / "live"
    _git(repo, "worktree", "add", "-b", "forge/live", str(live), "main")
    (live / "work.py").write_text("value = 1\n", encoding="utf-8")
    _git(live, "add", "work.py")
    _git(live, "commit", "-m", "wip 1")
    (live / "work.py").write_text("value = 2\n", encoding="utf-8")
    _git(live, "add", "work.py")
    _git(live, "commit", "-m", "wip 2")
    head = _rev_parse(repo, "forge/live")

    # The branch has never been pushed and the tree is clean — exactly the state the
    # sweep previously treated as "branch gone".
    assert not (repo / ".git" / "refs" / "remotes" / "origin" / "forge" / "live").exists()

    sweep_orphan_worktrees(repo, config)

    assert live.exists()
    assert (live / "work.py").read_text(encoding="utf-8") == "value = 2\n"
    assert _rev_parse(repo, "forge/live") == head
    assert (
        len(
            subprocess.run(
                ["git", "log", "--oneline", "main..forge/live"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .splitlines()
        )
        == 2
    )


def test_sweep_removes_branch_whose_remote_ref_was_deleted_after_merging(
    tmp_path: Path,
) -> None:
    """A branch that was published, merged, and then had its remote ref deleted has
    no local-only commits, so the sweep reclaims it."""
    repo, config = _init_sweep_repo(tmp_path)
    worktrees_root = repo / ".forge" / "worktrees"

    landed = worktrees_root / "landed"
    _git(repo, "worktree", "add", "-b", "forge/landed", str(landed), "main")
    (landed / "landed.txt").write_text("done\n", encoding="utf-8")
    _git(landed, "add", "landed.txt")
    _git(landed, "commit", "-m", "landed work")

    # Publish, merge on the remote side, then delete the remote-tracking ref the way
    # a merged-and-deleted PR branch would.
    _git(repo, "fetch", "origin", "forge/landed:refs/remotes/origin/forge/landed")
    _git(repo, "merge", "--ff-only", "forge/landed")
    _git(repo, "fetch", "origin", "main:refs/remotes/origin/main")
    _git(repo, "update-ref", "-d", "refs/remotes/origin/forge/landed")

    sweep_orphan_worktrees(repo, config)

    assert not landed.exists()
    assert not any(
        "forge/landed" in line
        for line in subprocess.run(
            ["git", "branch", "--list"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.splitlines()
    )


def test_sweep_removes_never_pushed_branch_with_no_commits_of_its_own(
    tmp_path: Path,
) -> None:
    """An unpublished branch that added nothing holds no work to lose, so the guard
    does not block reclamation."""
    repo, config = _init_sweep_repo(tmp_path)
    worktrees_root = repo / ".forge" / "worktrees"

    empty = worktrees_root / "empty"
    _git(repo, "worktree", "add", "-b", "forge/empty", str(empty), "main")

    sweep_orphan_worktrees(repo, config)

    assert not empty.exists()
    assert not any(
        "forge/empty" in line
        for line in subprocess.run(
            ["git", "branch", "--list"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.splitlines()
    )


def test_sweep_preserves_worktree_when_unpublished_count_is_undeterminable(
    tmp_path: Path, monkeypatch
) -> None:
    """If git cannot answer whether the commits exist on origin, the sweep preserves."""
    from theforge.coordinator import workspace as ws

    repo, config = _init_sweep_repo(tmp_path)
    worktrees_root = repo / ".forge" / "worktrees"

    unknown = worktrees_root / "unknown"
    _git(repo, "worktree", "add", "-b", "forge/unknown", str(unknown), "main")

    monkeypatch.setattr(ws, "_count_unpublished_commits", lambda *_a, **_k: None)
    sweep_orphan_worktrees(repo, config)

    assert unknown.exists()


# ── Squash-landed branches are reclaimable (#2795) ──────────────────────
#
# The sweep's commit-presence probe cannot see a squash landing: the squash
# commit is a new SHA, so the branch's own commits never reach origin and the
# count stays non-zero on every run forever. These cover the shared resolver
# deciding instead, and the two branches named in the issue.


def _squash_landed_worktree(
    repo: Path,
    slug: str,
    branch: str,
    commit_message: str,
    *,
    keep_remote_ref: bool,
) -> Path:
    """Create a worktree whose work reached main through a real squash merge."""
    worktree = repo / ".forge" / "worktrees" / slug
    _git(repo, "worktree", "add", "-b", branch, str(worktree), "main")
    for index in (1, 2):
        (worktree / f"work{index}.py").write_text(f"value = {index}\n", encoding="utf-8")
        _git(worktree, "add", ".")
        _git(worktree, "commit", "-m", f"wip {index}")
    if keep_remote_ref:
        _git(repo, "fetch", "origin", f"{branch}:refs/remotes/origin/{branch}")
    _git(repo, "merge", "--squash", branch)
    _git(repo, "commit", "-m", commit_message)
    _git(repo, "fetch", "origin", "main:refs/remotes/origin/main")
    return worktree


def _merged_pr(number: int, url: str):
    """A controlled merged-PR probe result, standing in for the GitHub lookup."""
    from theforge.coordinator.branch_landing import LANDED, BranchLanding

    return lambda *_a, **_k: (
        BranchLanding(status=LANDED, source="github_pr", pr_number=number, pr_url=url),
        True,
    )


def _no_merged_pr(*_args: object, **_kwargs: object):
    """GitHub answered and reported no merged PR for the branch."""
    return None, True


def test_sweep_reclaims_squash_landed_branch_whose_remote_ref_was_deleted(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """feat/issue-2553, squashed in PR #2577 — the first branch named in #2795.

    Its commits are permanently absent from origin, which is why the old
    commit-presence gate preserved it on every sprint since it merged.
    """
    from theforge.coordinator import branch_landing

    repo, config = _init_sweep_repo(tmp_path)
    worktree = _squash_landed_worktree(
        repo,
        "issue-2553",
        "feat/issue-2553",
        "feat: shared landing resolver (#2577)\n\nCloses #2553",
        keep_remote_ref=False,
    )
    monkeypatch.setattr(
        branch_landing, "_merged_pr_probe", _merged_pr(2577, "https://github.com/o/r/pull/2577")
    )

    sweep_orphan_worktrees(repo, config)

    assert not worktree.exists()
    assert "merged PR #2577" in capsys.readouterr().err


def test_sweep_reclaims_squash_landed_branch_whose_remote_ref_survives(
    tmp_path: Path, monkeypatch
) -> None:
    """feat/issue-2365, squashed in PR #2461, with its origin ref still present.

    A squash-landed branch is not an ancestor of origin/main, so the sweep's
    merged/branch-gone gate says no as well. A landed resolution has to clear
    both gates or the fix only reaches branches whose remote ref was deleted.
    """
    from theforge.coordinator import branch_landing

    repo, config = _init_sweep_repo(tmp_path)
    worktree = _squash_landed_worktree(
        repo,
        "issue-2365",
        "feat/issue-2365",
        "feat: per-issue cost accounting (#2461)\n\nCloses #2365",
        keep_remote_ref=True,
    )
    monkeypatch.setattr(
        branch_landing, "_merged_pr_probe", _merged_pr(2461, "https://github.com/o/r/pull/2461")
    )

    sweep_orphan_worktrees(repo, config)

    assert not worktree.exists()
    assert not any(
        "feat/issue-2365" in line
        for line in subprocess.run(
            ["git", "branch", "--list"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.splitlines()
    )


def test_sweep_reclaims_squash_landed_branch_on_closing_reference_alone(
    tmp_path: Path, monkeypatch
) -> None:
    """No PR record, but the squash commit on main closes the issue and the work is there."""
    from theforge.coordinator import branch_landing

    repo, config = _init_sweep_repo(tmp_path)
    worktree = _squash_landed_worktree(
        repo,
        "issue-2553",
        "feat/issue-2553",
        "feat: shared landing resolver\n\nCloses #2553",
        keep_remote_ref=False,
    )
    monkeypatch.setattr(branch_landing, "_merged_pr_probe", _no_merged_pr)

    sweep_orphan_worktrees(repo, config)

    assert not worktree.exists()


def test_sweep_preserves_local_only_commits_without_merge_evidence(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The case the sweep exists for: real unlanded work, no evidence, preserved.

    Widening recognition of landed work must not widen deletion of unlanded
    work, so this branch — same shape as a squash-landed one to git topology —
    survives, and the message says why.
    """
    from theforge.coordinator import branch_landing

    repo, config = _init_sweep_repo(tmp_path)
    worktrees_root = repo / ".forge" / "worktrees"
    live = worktrees_root / "issue-9999"
    _git(repo, "worktree", "add", "-b", "feat/issue-9999", str(live), "main")
    for index in (1, 2, 3):
        (live / f"work{index}.py").write_text(f"value = {index}\n", encoding="utf-8")
        _git(live, "add", ".")
        _git(live, "commit", "-m", f"wip {index}")
    monkeypatch.setattr(branch_landing, "_merged_pr_probe", _no_merged_pr)

    sweep_orphan_worktrees(repo, config)

    assert live.exists()
    assert (live / "work3.py").exists()
    err = capsys.readouterr().err
    assert "preserving worktree feat/issue-9999" in err
    assert "branch content is absent from main" in err
    assert "3 local commits not present on origin" in err


def test_sweep_preserves_branch_published_only_under_an_unrelated_origin_ref(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Commits reachable from *some* origin ref are not commits that landed on base.

    The branch below was pushed under another name, so nothing on it is missing
    from origin, and its own remote-tracking ref does not exist. The sweep used
    to read that pair — zero unpublished commits, remote ref gone — as proof the
    work had merged and delete the worktree. It proves only that a copy exists
    somewhere; the content never reached the configured base branch (#2795).
    """
    from theforge.coordinator import branch_landing

    repo, config = _init_sweep_repo(tmp_path)
    stranded = repo / ".forge" / "worktrees" / "stranded"
    _git(repo, "worktree", "add", "-b", "forge/stranded", str(stranded), "main")
    (stranded / "work.py").write_text("value = 1\n", encoding="utf-8")
    _git(stranded, "add", ".")
    _git(stranded, "commit", "-m", "wip")

    # Published under an unrelated name: every commit is reachable from an
    # origin ref, but refs/remotes/origin/forge/stranded does not exist.
    _git(repo, "fetch", "origin", "forge/stranded:refs/remotes/origin/archive/stranded")
    assert _count_unpublished_commits("forge/stranded", repo) == 0
    monkeypatch.setattr(branch_landing, "_merged_pr_probe", _no_merged_pr)

    sweep_orphan_worktrees(repo, config)

    assert stranded.exists()
    assert (stranded / "work.py").exists()
    assert "preserving worktree forge/stranded" in capsys.readouterr().err


def test_sweep_preserves_undecidable_branch_and_names_the_absent_evidence(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """An undecidable landing is preserved, and the report says what was missing."""
    from theforge.coordinator import workspace as ws

    repo, config = _init_sweep_repo(tmp_path)
    worktrees_root = repo / ".forge" / "worktrees"
    unknown = worktrees_root / "unknown"
    _git(repo, "worktree", "add", "-b", "forge/unknown", str(unknown), "main")

    monkeypatch.setattr(ws, "_count_unpublished_commits", lambda *_a, **_k: None)
    sweep_orphan_worktrees(repo, config)

    assert unknown.exists()
    err = capsys.readouterr().err
    assert "preserving worktree forge/unknown" in err
    assert "landing undecidable" in err
    assert "no issue reference in the branch name" in err


def test_sweep_preserves_branch_with_merged_pr_but_content_absent_from_base(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A merged PR record does not make a worktree reclaimable on its own (#2795).

    The branch below has work that is nowhere in main — the shape of a branch
    that kept committing after its PR merged. GitHub still reports the merged
    PR, and the sweep acted on that alone, deleting the only copy of the work.
    """
    from theforge.coordinator import branch_landing

    repo, config = _init_sweep_repo(tmp_path)
    live = repo / ".forge" / "worktrees" / "issue-7777"
    _git(repo, "worktree", "add", "-b", "feat/issue-7777", str(live), "main")
    for index in (1, 2):
        (live / f"after_pr{index}.py").write_text(f"value = {index}\n", encoding="utf-8")
        _git(live, "add", ".")
        _git(live, "commit", "-m", f"work after the PR merged {index}")
    monkeypatch.setattr(
        branch_landing, "_merged_pr_probe", _merged_pr(4242, "https://github.com/o/r/pull/4242")
    )

    sweep_orphan_worktrees(repo, config)

    assert live.exists()
    assert (live / "after_pr2.py").exists()
    err = capsys.readouterr().err
    assert "preserving worktree feat/issue-7777" in err
    assert "branch content is absent from main despite merged PR #4242" in err
