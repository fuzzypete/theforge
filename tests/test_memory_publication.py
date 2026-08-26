"""Project memory reaches a repository that refuses direct base commits (#2598).

These are transport tests over real git: a real checkout, a real bare origin, and
a real ``pre-receive`` hook implementing the policy that produced the reported
failure — the base branch advances by merge only. What is asserted is the two
properties the transport exists for:

* the base branch never receives a direct commit, and
* the project-root checkout is left clean whether the publish succeeds or not.

The second is not a nicety. A dirty project root refuses a landing, so a
transport that cannot commit is also a transport that strands the next story.
Staging is what separates those two failures, and it is asserted separately
from publication for exactly that reason.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from theforge.sprint.memory_publication import (
    MEMORY_BRANCH,
    MEMORY_PUBLISH_PUSHED_NO_PR,
    MEMORY_PUBLISH_STAGED_ONLY,
    pending_memory_paths,
    publish_staged_project_memory,
    stage_and_publish_project_memory,
    stage_pending_project_memory,
    staged_memory_paths,
    staging_dir,
)

BASE = "main"

_FORGE_GITIGNORE = """\
.forge/**
!.forge/audits/
!.forge/audits/runs/
!.forge/audits/runs/**
!.forge/audits/landing/
!.forge/audits/landing/**
!.forge/knowledge/
!.forge/knowledge/summaries/
!.forge/knowledge/summaries/**
"""

# The reported policy, as a hook: the base branch advances by merge commits
# only. Forge's own story landings satisfy it; a direct audit commit does not.
_PRE_RECEIVE = """\
#!/bin/sh
while read old new ref; do
  case "$ref" in
    refs/heads/%s) ;;
    *) continue ;;
  esac
  case "$old" in
    0000000000000000000000000000000000000000) continue ;;
  esac
  for sha in $(git rev-list --first-parent "$old..$new"); do
    parents=$(git rev-list --parents -n 1 "$sha" | wc -w)
    if [ "$parents" -lt 3 ]; then
      echo "COMMIT BLOCKED: Non-merge commit $sha on %s." >&2
      exit 1
    fi
  done
done
exit 0
""" % (BASE, BASE)


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check
    )
    return proc.stdout.strip()


@pytest.fixture()
def protected_repo(tmp_path: Path) -> Path:
    """A checkout whose origin refuses direct non-merge commits to the base."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch", BASE)

    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "--initial-branch", BASE)
    _git(root, "config", "user.email", "forge@example.com")
    _git(root, "config", "user.name", "Forge Test")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    (root / ".gitignore").write_text(_FORGE_GITIGNORE, encoding="utf-8")
    _git(root, "add", "README.md", ".gitignore")
    _git(root, "commit", "-m", "seed")
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "push", "-u", "origin", BASE)

    hook = origin / "hooks" / "pre-receive"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(_PRE_RECEIVE, encoding="utf-8")
    hook.chmod(0o755)
    return root


def _write_memory(root: Path, run_id: str, *, landing: bool = False) -> None:
    runs = root / ".forge" / "audits" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{run_id}.json").write_text(json.dumps({"run_id": run_id}) + "\n", encoding="utf-8")
    summaries = root / ".forge" / "knowledge" / "summaries"
    summaries.mkdir(parents=True, exist_ok=True)
    (summaries / f"{run_id}.yaml").write_text(f"run_id: {run_id}\n", encoding="utf-8")
    if landing:
        evidence = root / ".forge" / "audits" / "landing"
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / f"{run_id}.landed.json").write_text(
            json.dumps({"run_id": run_id}) + "\n", encoding="utf-8"
        )


# ── Nothing transient is ever visible in the tracked trees ───────────────
#
# The memory trees are re-included by forge's generated .gitignore precisely so
# they are tracked. A write-in-progress file inside one of them is therefore
# indistinguishable from project memory: it dirties the shared checkout for as
# long as the write takes — enough to refuse a sibling story — and the transport
# would carry it into the corpus. Both writers put their temporary file outside
# the tree for that reason (#2598).


def test_the_run_record_writer_leaves_no_temporary_file_in_the_tracked_tree(
    protected_repo: Path,
) -> None:
    from theforge.sprint.audit import _replace_canonical_run_file

    runs = protected_repo / ".forge" / "audits" / "runs"
    runs.mkdir(parents=True, exist_ok=True)

    _replace_canonical_run_file(runs / "run-a.json", {"run_id": "run-a"})

    assert [p.name for p in runs.iterdir()] == ["run-a.json"]
    assert _git(protected_repo, "status", "--porcelain", "-uall") == (
        "?? .forge/audits/runs/run-a.json"
    )


def test_the_evidence_writer_leaves_no_temporary_file_in_the_tracked_tree(
    protected_repo: Path,
) -> None:
    from theforge.coordinator.landing_evidence import (
        build_landing_assertion,
        landing_evidence_dir,
        write_landing_assertion,
    )

    write_landing_assertion(
        protected_repo,
        build_landing_assertion(
            run_id="run-a",
            slug="issue-1",
            landing_mode="merge-pr",
            target_branch=BASE,
            reviewed_commit="aaaa111",
            gated_commit="aaaa111",
            carrier_kind="pull_request",
            carrier_ref="#1",
            landed_commit="bbbb222",
            observer="test",
        ),
    )

    evidence = landing_evidence_dir(protected_repo)
    assert [p.name for p in evidence.iterdir()] == ["run-a.landed.json"]
    assert _git(protected_repo, "status", "--porcelain", "-uall") == (
        "?? .forge/audits/landing/run-a.landed.json"
    )


def test_a_stray_temporary_file_is_never_treated_as_publishable_memory(
    protected_repo: Path,
) -> None:
    """Belt and braces for repositories whose writers pre-date the fix."""
    runs = protected_repo / ".forge" / "audits" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "run-a.tmp").write_text("half-written", encoding="utf-8")
    _write_memory(protected_repo, "run-b")

    assert pending_memory_paths(protected_repo) == [
        ".forge/audits/runs/run-b.json",
        ".forge/knowledge/summaries/run-b.yaml",
    ]


# ── The policy this exists for ───────────────────────────────────────────


def test_the_hook_really_refuses_a_direct_base_commit(protected_repo: Path) -> None:
    """Guard on the fixture: without this, every test below proves nothing."""
    _write_memory(protected_repo, "run-a")
    _git(protected_repo, "add", "--force", ".forge/audits/runs")
    _git(protected_repo, "commit", "-m", "chore(audit): record sprint run audits")
    proc = subprocess.run(
        ["git", "push", "origin", BASE],
        cwd=str(protected_repo),
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "COMMIT BLOCKED" in proc.stderr


# ── Staging ──────────────────────────────────────────────────────────────


def test_staging_leaves_the_checkout_clean(protected_repo: Path) -> None:
    _write_memory(protected_repo, "run-a", landing=True)
    assert pending_memory_paths(protected_repo)

    staged = stage_pending_project_memory(protected_repo)

    assert not staged.errors
    assert sorted(staged.paths) == [
        ".forge/audits/landing/run-a.landed.json",
        ".forge/audits/runs/run-a.json",
        ".forge/knowledge/summaries/run-a.yaml",
    ]
    assert _git(protected_repo, "status", "--porcelain", "-uall") == ""
    assert sorted(staged_memory_paths(protected_repo)) == sorted(staged.paths)


def test_staging_drains_artifacts_a_refused_commit_left_in_the_index(
    protected_repo: Path,
) -> None:
    """The direct path ``git add``\\ s before it commits.

    A commit refused by a hook leaves the artifacts staged in the index, so a
    restore keyed to the index would put back exactly the content being
    drained and the checkout would stay dirty.
    """
    _write_memory(protected_repo, "run-a")
    _git(protected_repo, "add", "--force", ".forge/audits/runs")

    stage_pending_project_memory(protected_repo)

    assert _git(protected_repo, "status", "--porcelain", "-uall") == ""
    assert ".forge/audits/runs/run-a.json" in staged_memory_paths(protected_repo)


def test_staging_a_rewritten_tracked_record_restores_the_committed_content(
    protected_repo: Path,
) -> None:
    """A record already on the branch and modified since is drained, not moved.

    Moving it would leave a deletion behind, and the checkout would still be
    dirty — the condition staging exists to remove.
    """
    _write_memory(protected_repo, "run-a")
    _git(protected_repo, "add", "--force", ".forge/audits/runs")
    _git(protected_repo, "commit", "-m", "seed memory")
    record = protected_repo / ".forge" / "audits" / "runs" / "run-a.json"
    record.write_text(json.dumps({"run_id": "run-a", "revised": True}) + "\n", encoding="utf-8")

    stage_pending_project_memory(protected_repo)

    assert _git(protected_repo, "status", "--porcelain", "-uall") == ""
    assert json.loads(record.read_text(encoding="utf-8")) == {"run_id": "run-a"}
    staged = staging_dir(protected_repo) / ".forge/audits/runs/run-a.json"
    assert json.loads(staged.read_text(encoding="utf-8"))["revised"] is True


def test_a_failed_publish_retains_staged_memory_rather_than_losing_it(
    protected_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is dropped on the floor when the transport itself fails."""
    _write_memory(protected_repo, "run-a")
    stage_pending_project_memory(protected_repo)
    _git(protected_repo, "remote", "set-url", "origin", str(protected_repo / "nowhere.git"))

    state, _pr = publish_staged_project_memory(protected_repo, BASE)

    assert state == MEMORY_PUBLISH_STAGED_ONLY
    assert ".forge/audits/runs/run-a.json" in staged_memory_paths(protected_repo)
    assert _git(protected_repo, "status", "--porcelain", "-uall") == ""


# ── Publication ──────────────────────────────────────────────────────────


def test_memory_reaches_the_remote_without_a_direct_base_commit(
    protected_repo: Path,
) -> None:
    """The whole point: the corpus travels, the protected branch is untouched."""
    _write_memory(protected_repo, "run-a", landing=True)
    base_before = _git(protected_repo, "rev-parse", f"origin/{BASE}")

    state, _pr = stage_and_publish_project_memory(protected_repo, BASE)

    # gh is not installed in the test environment, so there is no PR carrier;
    # the branch is published either way and the state says exactly that.
    assert state == MEMORY_PUBLISH_PUSHED_NO_PR
    assert _git(protected_repo, "rev-parse", f"origin/{BASE}") == base_before
    assert _git(protected_repo, "status", "--porcelain", "-uall") == ""
    # Staging is retained, not cleared: until the memory pull request merges,
    # this is the only place forge's own readers can find what it published —
    # the canonical trees were drained to publish them. It costs nothing in the
    # checkout, because ``.forge/**`` denies it.
    assert ".forge/audits/runs/run-a.json" in staged_memory_paths(protected_repo)

    published = _git(
        protected_repo, "ls-tree", "-r", "--name-only", f"origin/{MEMORY_BRANCH}"
    ).splitlines()
    assert ".forge/audits/runs/run-a.json" in published
    assert ".forge/knowledge/summaries/run-a.yaml" in published
    assert ".forge/audits/landing/run-a.landed.json" in published


def test_a_fresh_clone_of_the_memory_branch_carries_the_corpus(
    protected_repo: Path, tmp_path: Path
) -> None:
    """ "Travels to another machine" asserted as a clone, not as a push result."""
    _write_memory(protected_repo, "run-a")
    stage_and_publish_project_memory(protected_repo, BASE)

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "--branch", MEMORY_BRANCH, str(tmp_path / "origin.git"), str(clone))
    record = clone / ".forge" / "audits" / "runs" / "run-a.json"
    assert json.loads(record.read_text(encoding="utf-8")) == {"run_id": "run-a"}


def test_a_second_sprint_accumulates_onto_the_same_memory_branch(
    protected_repo: Path,
) -> None:
    """One long-lived branch, updated in place — not one branch per sprint."""
    _write_memory(protected_repo, "run-a")
    stage_and_publish_project_memory(protected_repo, BASE)
    _write_memory(protected_repo, "run-b")
    stage_and_publish_project_memory(protected_repo, BASE)

    published = _git(
        protected_repo, "ls-tree", "-r", "--name-only", f"origin/{MEMORY_BRANCH}"
    ).splitlines()
    assert ".forge/audits/runs/run-a.json" in published
    assert ".forge/audits/runs/run-b.json" in published
    heads = _git(protected_repo, "ls-remote", "--heads", "origin")
    assert heads.count("refs/heads/forge/") == 1


def test_republishing_after_the_memory_pr_merged_restarts_from_the_base(
    protected_repo: Path,
) -> None:
    """The branch does not accumulate forever once its content is in the base.

    Merging the memory branch is what an operator does with the pull request.
    Afterwards the branch's commits are all ancestors of the base, and
    continuing from it would grow a permanently-behind branch for no reason.
    """
    _write_memory(protected_repo, "run-a")
    stage_and_publish_project_memory(protected_repo, BASE)

    # The operator merges the memory PR — a merge commit, which the policy allows.
    _git(protected_repo, "fetch", "origin", MEMORY_BRANCH)
    _git(protected_repo, "merge", "--no-ff", "-m", "merge memory", f"origin/{MEMORY_BRANCH}")
    _git(protected_repo, "push", "origin", BASE)

    _write_memory(protected_repo, "run-b")
    state, _pr = stage_and_publish_project_memory(protected_repo, BASE)

    assert state == MEMORY_PUBLISH_PUSHED_NO_PR
    ahead = _git(
        protected_repo,
        "rev-list",
        "--count",
        f"origin/{BASE}..origin/{MEMORY_BRANCH}",
    )
    assert ahead == "1", "the republished branch carries only the new sprint's memory"
    # run-a's content is in the base branch now, so staging drops it and keeps
    # only what the base branch has not absorbed.
    assert staged_memory_paths(protected_repo) == [
        ".forge/audits/runs/run-b.json",
        ".forge/knowledge/summaries/run-b.yaml",
    ]
