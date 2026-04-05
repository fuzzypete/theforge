from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from theforge.coordinator.workspace_scrub import _scrub_forge_history


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _init_repo(tmp_path: Path) -> tuple[Path, str]:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", remote], check=True, capture_output=True, text=True)

    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", str(remote), str(repo)], check=True, capture_output=True, text=True
    )
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")

    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    _git(repo, "branch", "-M", "main")
    _git(repo, "push", "-u", "origin", "main")
    _git(repo, "checkout", "-b", "feat/test")
    return repo, "main"


def _commit_count(repo: Path, base_branch: str) -> int:
    out = _git(repo, "rev-list", "--count", f"origin/{base_branch}..HEAD")
    return int(out)


def test_scrub_no_contamination_leaves_history_unchanged(tmp_path):
    repo, base_branch = _init_repo(tmp_path)
    (repo / "src.py").write_text("print('ok')\n", encoding="utf-8")
    _git(repo, "add", "src.py")
    _git(repo, "commit", "-m", "feat: add src")

    before = _commit_count(repo, base_branch)
    before_head = _git(repo, "rev-parse", "HEAD")

    _scrub_forge_history(repo, "feat/test", base_branch)

    assert _commit_count(repo, base_branch) == before
    assert _git(repo, "rev-parse", "HEAD") == before_head


def test_scrub_drops_forge_only_commits(tmp_path):
    repo, base_branch = _init_repo(tmp_path)
    (repo / "src.py").write_text("print('real')\n", encoding="utf-8")
    _git(repo, "add", "src.py")
    _git(repo, "commit", "-m", "feat: real change")

    forge_dir = repo / ".forge"
    forge_dir.mkdir(exist_ok=True)
    (forge_dir / "handoff.yaml").write_text("temp: true\n", encoding="utf-8")
    _git(repo, "add", ".forge/handoff.yaml")
    _git(repo, "commit", "-m", "chore: forge artifact")

    _scrub_forge_history(repo, "feat/test", base_branch)

    log = _git(repo, "log", "--oneline", "--reverse", f"origin/{base_branch}..HEAD")
    assert "forge artifact" not in log
    assert "real change" in log
    assert _commit_count(repo, base_branch) == 1


def test_scrub_strips_forge_paths_from_mixed_commit(tmp_path):
    repo, base_branch = _init_repo(tmp_path)
    forge_dir = repo / ".forge"
    forge_dir.mkdir(exist_ok=True)
    (repo / "src.py").write_text("print('mixed')\n", encoding="utf-8")
    (forge_dir / "handoff.yaml").write_text("temp: true\n", encoding="utf-8")
    _git(repo, "add", "src.py", ".forge/handoff.yaml")
    _git(repo, "commit", "-m", "feat: mixed change")

    _scrub_forge_history(repo, "feat/test", base_branch)

    log = _git(repo, "log", "--oneline", "--reverse", f"origin/{base_branch}..HEAD")
    assert "mixed change" in log
    show = _git(repo, "show", "--name-only", "--format=", "--first-parent", "HEAD")
    assert "src.py" in show.splitlines()
    assert ".forge/handoff.yaml" not in show.splitlines()


def test_scrub_rebase_failure_is_silent(tmp_path):
    repo, base_branch = _init_repo(tmp_path)

    def side_effect(cmd, cwd, **kwargs):
        if cmd == f"git log --format=%H origin/{base_branch}..HEAD":
            return True, "abc123"
        if cmd == "git diff-tree --no-commit-id -r --name-only abc123":
            return True, ".forge/handoff.yaml"
        if cmd == f"git rebase -i --keep-empty origin/{base_branch}":
            return False, "err"
        return True, ""

    with (
        patch("theforge.coordinator.workspace_scrub._cu._run_shell", side_effect=side_effect),
        patch("theforge.coordinator.workspace_scrub._cu._log") as mock_log,
    ):
        _scrub_forge_history(repo, "feat/test", base_branch)

    assert any("forge-history scrub failed: err" in str(call) for call in mock_log.call_args_list)


def test_scrub_returns_early_when_branch_has_no_unique_commits(tmp_path):
    repo, base_branch = _init_repo(tmp_path)

    calls: list[str] = []

    def side_effect(cmd, cwd, **kwargs):
        calls.append(cmd)
        if cmd == f"git log --format=%H origin/{base_branch}..HEAD":
            return True, ""
        return True, "unexpected"

    with patch("theforge.coordinator.workspace_scrub._cu._run_shell", side_effect=side_effect):
        _scrub_forge_history(repo, "feat/test", base_branch)

    assert calls == [f"git log --format=%H origin/{base_branch}..HEAD"]
