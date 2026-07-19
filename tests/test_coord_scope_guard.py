"""Tests for the committed-diff scope guard (theforge #1615).

Covers the matcher (repo-root env/tooling config and forge artifacts are
flagged; in-scope source and nested legitimate paths are not), the real-git
seam (``check_committed_scope`` over a worktree fixture, per convention 8), and
the DEV→REVIEW boundary wiring that escalates on an out-of-scope committed file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import _make_config

from theforge.coordinator.review_phase import _ReviewOutcome, _run_review_phase
from theforge.coordinator.scope_guard import (
    check_committed_scope,
    committed_diff_paths,
    find_out_of_scope_config,
)
from theforge.coordinator.state import CoordinatorState, Phase
from theforge.task import TaskStory


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / ".gitignore").write_text(".forge/\n", encoding="utf-8")
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=path, check=True)


# ── matcher: find_out_of_scope_config ─────────────────────────────────


def test_matcher_flags_repo_root_env_config() -> None:
    paths = ["poetry.toml", ".npmrc", ".python-version", "foo.local", ".vscode/settings.json"]
    assert find_out_of_scope_config(paths) == sorted(paths)


def test_matcher_flags_forge_artifacts() -> None:
    paths = [".forge/handoff.yaml", ".forge/trajectory.yaml", ".forge/last_setup_command"]
    assert find_out_of_scope_config(paths) == sorted(paths)


def test_matcher_does_not_flag_in_scope_source() -> None:
    paths = ["src/app/x.py", "tests/test_x.py", "docs/guide.md", "pyproject.toml"]
    assert find_out_of_scope_config(paths) == []


def test_matcher_does_not_flag_nested_env_config() -> None:
    """Env-config names are repo-root only — a nested occurrence may be a
    legitimate deliverable (e.g. an npm-tooling story shipping config/.npmrc)."""
    paths = ["config/.npmrc", "packages/web/poetry.toml", "src/.python-version"]
    assert find_out_of_scope_config(paths) == []


def test_matcher_flags_editor_dir_prefix() -> None:
    assert find_out_of_scope_config([".idea/workspace.xml"]) == [".idea/workspace.xml"]


def test_matcher_mixed_scope_returns_only_offending() -> None:
    paths = ["src/app/x.py", "poetry.toml", "tests/test_x.py", ".forge/handoff.yaml"]
    assert find_out_of_scope_config(paths) == [".forge/handoff.yaml", "poetry.toml"]


# ── seam: check_committed_scope over a real git worktree ──────────────


def _commit_on_feature(path: Path, files: dict[str, str], message: str) -> None:
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=path, check=True)
    for rel, content in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=path, check=True)


def test_check_committed_scope_flags_poetry_toml(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit_on_feature(
        tmp_path,
        {"src/app/x.py": "print('hi')\n", "poetry.toml": "[virtualenvs]\nin-project = true\n"},
        "feat: implement plus env workaround",
    )

    ok, diagnostic, audit = check_committed_scope(tmp_path, "main")

    assert ok is False
    assert diagnostic is not None
    assert "poetry.toml" in diagnostic
    assert audit["offending"] == ["poetry.toml"]
    assert "src/app/x.py" in audit["diff_paths"]


def test_check_committed_scope_passes_in_scope_only(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit_on_feature(
        tmp_path,
        {"src/app/x.py": "print('hi')\n", "tests/test_x.py": "def test_x(): pass\n"},
        "feat: implement",
    )

    ok, diagnostic, audit = check_committed_scope(tmp_path, "main")

    assert ok is True
    assert diagnostic is None
    assert audit["offending"] == []
    assert set(audit["diff_paths"]) == {"src/app/x.py", "tests/test_x.py"}


def test_committed_diff_paths_returns_none_when_diff_unavailable(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    # Non-existent base ref → neither origin/ nor local ref can be diffed →
    # None (inspection failure), NOT [] (genuinely empty diff).
    assert committed_diff_paths(tmp_path, "does-not-exist") is None


def test_check_committed_scope_fails_closed_when_diff_unavailable(tmp_path: Path) -> None:
    """When neither base ref can be diffed the guard must fail closed — it
    cannot certify the branch clean of out-of-scope config (#1615 review P1)."""
    _init_repo(tmp_path)
    _commit_on_feature(
        tmp_path,
        {"poetry.toml": "[virtualenvs]\nin-project = true\n"},
        "feat: sneaks in poetry.toml",
    )

    ok, diagnostic, audit = check_committed_scope(tmp_path, "does-not-exist")

    assert ok is False
    assert diagnostic is not None
    assert "does-not-exist" in diagnostic
    assert audit["diff_error"] is True


def test_check_committed_scope_fails_open_when_no_committed_head(tmp_path: Path) -> None:
    """A workspace with no resolvable committed HEAD (not a git repo / no
    commits) has no committed diff for an agent to leak into, so the guard
    fails open rather than manufacturing a spurious escalation — distinct from
    the unverifiable-but-real state above (#1615 review P1)."""
    # tmp_path is a bare directory, not a git repo.
    ok, diagnostic, audit = check_committed_scope(tmp_path, "main")

    assert ok is True
    assert diagnostic is None
    assert audit["diff_error"] is False
    assert audit["offending"] == []


def test_check_committed_scope_flags_committed_forge_artifact(tmp_path: Path) -> None:
    """Force-committing a forge runtime artifact (the way the original
    handoff.yaml leak actually happened despite .gitignore) is flagged
    end-to-end through a real committed-diff seam (#1615 review P2)."""
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp_path, check=True)
    (tmp_path / "src.py").write_text("ok\n", encoding="utf-8")
    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir(exist_ok=True)
    (forge_dir / "handoff.yaml").write_text("gate_decision: PASS\n", encoding="utf-8")
    subprocess.run(["git", "add", "src.py"], cwd=tmp_path, check=True)
    # -f because .forge/ is gitignored — exactly how the artifact leaked in the field.
    subprocess.run(["git", "add", "-f", ".forge/handoff.yaml"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat: leak handoff"], cwd=tmp_path, check=True)

    ok, diagnostic, audit = check_committed_scope(tmp_path, "main")

    assert ok is False
    assert ".forge/handoff.yaml" in diagnostic
    assert audit["offending"] == [".forge/handoff.yaml"]


# ── seam: DEV → REVIEW boundary escalates on out-of-scope committed file ──


def test_run_review_phase_escalates_on_out_of_scope_committed_config(tmp_path: Path) -> None:
    """A committed repo-root env-config file must escalate at DEV→REVIEW as a
    hygiene escalation, before reviewers run — mirrors the pre-review-hygiene
    escalation contract."""
    _init_repo(tmp_path)
    _commit_on_feature(
        tmp_path,
        {"src.py": "ok\n", "poetry.toml": "[virtualenvs]\nin-project = true\n"},
        "feat: implement plus env workaround",
    )

    config = _make_config(tmp_path)
    spec_path = tmp_path.parent / f"{tmp_path.name}-spec.md"
    spec_path.write_text("# spec\n", encoding="utf-8")
    task = TaskStory(name="t", slug="t", story_path=spec_path)
    state = CoordinatorState()
    state.run_id = "abcd1234"

    pool_called: list[bool] = []

    with patch(
        "theforge.coordinator.review_phase._run_review_pool",
        side_effect=lambda *a, **kw: pool_called.append(True) or ([], [], None, [], []),
    ):
        outcome, result, _config = _run_review_phase(
            state,
            config,
            task,
            "# spec\n",
            tmp_path,
            "feat/x",
            task_start=0.0,
            interactive=False,
            auto_merge=False,
            notify=False,
            logger=None,
        )

    assert outcome == _ReviewOutcome.ESCALATE
    assert result is not None and result.success is False
    assert state.phase == Phase.ESCALATE
    assert state.escalate_kind == "hygiene"
    assert "poetry.toml" in (state.error or "")
    # Reviewers never ran — the guard fires before the pool.
    assert pool_called == []
    # Audit recorded under SCOPE_GUARD.
    scope_entry = next(e for e in state.workspace_hygiene_audit if e["phase"] == "SCOPE_GUARD")
    assert scope_entry["offending"] == ["poetry.toml"]


def test_run_review_phase_scope_guard_passes_in_scope_diff(tmp_path: Path) -> None:
    """An in-scope-only committed diff passes the scope guard; the guard does
    not manufacture a hygiene escalation for a clean diff."""
    _init_repo(tmp_path)
    _commit_on_feature(
        tmp_path,
        {"src.py": "ok\n"},
        "feat: implement",
    )

    config = _make_config(tmp_path)
    spec_path = tmp_path.parent / f"{tmp_path.name}-spec.md"
    spec_path.write_text("# spec\n", encoding="utf-8")
    task = TaskStory(name="t", slug="t", story_path=spec_path)
    state = CoordinatorState()
    state.run_id = "abcd1234"

    with (
        patch(
            "theforge.coordinator.review_phase._run_review_pool",
            side_effect=lambda *a, **kw: ([], [], None, [], []),
        ),
        patch("theforge.coordinator.review_phase._run_escalate_gate", return_value=None),
    ):
        outcome, _result, _config = _run_review_phase(
            state,
            config,
            task,
            "# spec\n",
            tmp_path,
            "feat/x",
            task_start=0.0,
            interactive=False,
            auto_merge=False,
            notify=False,
            logger=None,
        )

    # The scope guard passed (audit records no offenders); any escalation here
    # comes from the empty pool, not the scope guard.
    scope_entry = next(e for e in state.workspace_hygiene_audit if e["phase"] == "SCOPE_GUARD")
    assert scope_entry["offending"] == []
    assert outcome in (_ReviewOutcome.ESCALATE, _ReviewOutcome.RETRY_DEV)
    assert state.escalate_kind != "hygiene"
