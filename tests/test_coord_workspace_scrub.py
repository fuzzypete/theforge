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

    # Mirror the real project's .gitignore: .forge/ entirely ignored.
    # Tracked .forge/ files (hooks, .env.example) are force-added because the
    # parent-dir rule prevents negation overrides in practice.
    (repo / ".gitignore").write_text(".forge/\n", encoding="utf-8")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".gitignore", "README.md")
    _git(repo, "commit", "-m", "base")
    _git(repo, "branch", "-M", "main")
    _git(repo, "push", "-u", "origin", "main")
    _git(repo, "checkout", "-b", "feat/test")
    return repo, "main"


def _init_repo_with_tracked_forge_files(
    tmp_path: Path,
    *,
    with_hook: bool = False,
    with_env_example: bool = False,
) -> tuple[Path, str]:
    """Like _init_repo but optionally tracks .forge/ project files in the base branch."""
    repo, base_branch = _init_repo(tmp_path)
    _git(repo, "checkout", base_branch)

    forge_dir = repo / ".forge"
    to_add: list[str] = []

    if with_hook:
        hooks_dir = forge_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "post_run.sh").write_text("#!/bin/bash\necho ok\n", encoding="utf-8")
        to_add.append(".forge/hooks/post_run.sh")

    if with_env_example:
        forge_dir.mkdir(parents=True, exist_ok=True)
        (forge_dir / ".env.example").write_text("API_KEY=\n", encoding="utf-8")
        to_add.append(".forge/.env.example")

    if to_add:
        _git(repo, "add", "-f", *to_add)
        _git(repo, "commit", "-m", "base: track forge project files")
        _git(repo, "push", "origin", base_branch)

    # feat/test pointed at old main HEAD; reset it to the new HEAD
    _git(repo, "branch", "-D", "feat/test")
    _git(repo, "checkout", "-b", "feat/test")
    return repo, base_branch


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
    _git(repo, "add", "-f", ".forge/handoff.yaml")
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
    _git(repo, "add", "src.py")
    _git(repo, "add", "-f", ".forge/handoff.yaml")
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
        if cmd == f"git ls-tree -r --name-only origin/{base_branch} -- .forge/":
            return True, ""
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


def test_scrub_does_not_drop_commit_touching_only_tracked_forge_hooks(tmp_path):
    """A commit that modifies only .forge/hooks/ files is not dropped or scrubbed."""
    repo, base_branch = _init_repo_with_tracked_forge_files(tmp_path, with_hook=True)

    hook = repo / ".forge" / "hooks" / "post_run.sh"
    hook.write_text("#!/bin/bash\necho updated\n", encoding="utf-8")
    _git(repo, "add", "-f", ".forge/hooks/post_run.sh")
    _git(repo, "commit", "-m", "feat: update hook")

    _scrub_forge_history(repo, "feat/test", base_branch)

    log = _git(repo, "log", "--oneline", f"origin/{base_branch}..HEAD")
    assert "update hook" in log
    assert _commit_count(repo, base_branch) == 1


def test_scrub_preserves_hooks_in_mixed_commit(tmp_path):
    """Mixed commit: artifact paths removed, .forge/hooks/ paths preserved."""
    repo, base_branch = _init_repo_with_tracked_forge_files(tmp_path, with_hook=True)

    forge_dir = repo / ".forge"
    (repo / "src.py").write_text("print('hello')\n", encoding="utf-8")
    (forge_dir / "handoff.yaml").write_text("run: 1\n", encoding="utf-8")
    hook = forge_dir / "hooks" / "post_run.sh"
    hook.write_text("#!/bin/bash\necho updated\n", encoding="utf-8")
    _git(repo, "add", "src.py")
    _git(repo, "add", "-f", ".forge/handoff.yaml", ".forge/hooks/post_run.sh")
    _git(repo, "commit", "-m", "feat: real + artifact + hook")

    _scrub_forge_history(repo, "feat/test", base_branch)

    show = _git(repo, "show", "--name-only", "--format=", "--first-parent", "HEAD")
    changed = show.splitlines()
    assert "src.py" in changed
    assert ".forge/handoff.yaml" not in changed
    assert ".forge/hooks/post_run.sh" in changed


def test_scrub_does_not_drop_commit_touching_only_env_example(tmp_path):
    """A commit that modifies only .forge/.env.example is not dropped or scrubbed."""
    repo, base_branch = _init_repo_with_tracked_forge_files(tmp_path, with_env_example=True)

    env_example = repo / ".forge" / ".env.example"
    env_example.write_text("API_KEY=\nNEW_VAR=\n", encoding="utf-8")
    _git(repo, "add", "-f", ".forge/.env.example")
    _git(repo, "commit", "-m", "feat: update env example")

    _scrub_forge_history(repo, "feat/test", base_branch)

    log = _git(repo, "log", "--oneline", f"origin/{base_branch}..HEAD")
    assert "update env example" in log
    assert _commit_count(repo, base_branch) == 1


def test_scrub_preserves_env_example_in_mixed_commit(tmp_path):
    """Mixed commit with .forge/.env.example + artifact: only artifact is stripped."""
    repo, base_branch = _init_repo_with_tracked_forge_files(tmp_path, with_env_example=True)

    forge_dir = repo / ".forge"
    (repo / "src.py").write_text("print('hi')\n", encoding="utf-8")
    (forge_dir / "handoff.yaml").write_text("run: 1\n", encoding="utf-8")
    (forge_dir / ".env.example").write_text("API_KEY=\nNEW_VAR=\n", encoding="utf-8")
    _git(repo, "add", "src.py")
    _git(repo, "add", "-f", ".forge/handoff.yaml", ".forge/.env.example")
    _git(repo, "commit", "-m", "feat: real + artifact + env.example")

    _scrub_forge_history(repo, "feat/test", base_branch)

    show = _git(repo, "show", "--name-only", "--format=", "--first-parent", "HEAD")
    changed = show.splitlines()
    assert "src.py" in changed
    assert ".forge/handoff.yaml" not in changed
    assert ".forge/.env.example" in changed
