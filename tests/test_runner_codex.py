"""Tests for Codex CLI runner: sandbox flag injection."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from theforge.config import ModelProfile
from theforge.runners.runner_codex import _run_codex
from theforge.workspace_env import resolve_workspace_python

# Spawn seam patched by the argv-construction tests below.
_RUN_TARGET = "theforge.runners.runner_codex.process_group.run_in_process_group"


def _make_profile(
    sandbox_mode: str = "workspace-write",
    reasoning_effort: str | None = None,
) -> ModelProfile:
    return ModelProfile(
        name="dev",
        cli="codex",
        model="o4-mini",
        budget_usd=2.0,
        timeout_seconds=900,
        allowed_tools=("Read", "Edit", "Write", "Bash"),
        sandbox_mode=sandbox_mode,
        reasoning_effort=reasoning_effort,
    )


def _make_subprocess_mock(returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = ""
    proc.stderr = ""
    return proc


def _extract_codex_cmd(mock_run: MagicMock) -> list[str]:
    """Return the cmd list from the process_group.run_in_process_group call."""
    return mock_run.call_args[0][0]


def _write_matching_workspace_venv(tmp_path: Path) -> None:
    (tmp_path / ".python-version").write_text("3.12.12\n", encoding="utf-8")
    resolved = resolve_workspace_python(tmp_path)
    cfg = tmp_path / ".venv" / "pyvenv.cfg"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        "\n".join(
            [
                f"home = {resolved.executable.parent}",
                "include-system-site-packages = false",
                f"version = {resolved.version}",
                f"executable = {resolved.executable}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


class TestCodexSandboxFlag:
    """Assert that sandbox handling matches the Codex CLI contract."""

    def test_workspace_write_adds_sandbox_flag(self, tmp_path: Path) -> None:
        """sandbox_mode=workspace-write → --sandbox workspace-write in cmd."""
        profile = _make_profile(sandbox_mode="workspace-write")
        mock_proc = _make_subprocess_mock()
        with patch(_RUN_TARGET, return_value=mock_proc):
            with patch("theforge.runners.runner_codex._get_codex_session_id", return_value=None):
                with patch(_RUN_TARGET, return_value=mock_proc) as mock_run:
                    _run_codex(
                        prompt="implement the thing",
                        profile=profile,
                        working_dir=tmp_path,
                    )
        cmd = _extract_codex_cmd(mock_run)
        assert "--sandbox" in cmd
        sandbox_idx = cmd.index("--sandbox")
        assert cmd[sandbox_idx + 1] == "workspace-write"

    def test_read_only_adds_sandbox_flag(self, tmp_path: Path) -> None:
        """sandbox_mode=read-only → --sandbox read-only in cmd."""
        profile = _make_profile(sandbox_mode="read-only")
        mock_proc = _make_subprocess_mock()
        with patch("theforge.runners.runner_codex._get_codex_session_id", return_value=None):
            with patch(_RUN_TARGET, return_value=mock_proc) as mock_run:
                _run_codex(
                    prompt="review only",
                    profile=profile,
                    working_dir=tmp_path,
                )
        cmd = _extract_codex_cmd(mock_run)
        assert "--sandbox" in cmd
        sandbox_idx = cmd.index("--sandbox")
        assert cmd[sandbox_idx + 1] == "read-only"

    def test_none_omits_sandbox_flag(self, tmp_path: Path) -> None:
        """sandbox_mode=none → no --sandbox flag in cmd."""
        profile = _make_profile(sandbox_mode="none")
        mock_proc = _make_subprocess_mock()
        with patch("theforge.runners.runner_codex._get_codex_session_id", return_value=None):
            with patch(_RUN_TARGET, return_value=mock_proc) as mock_run:
                _run_codex(
                    prompt="debug run",
                    profile=profile,
                    working_dir=tmp_path,
                )
        cmd = _extract_codex_cmd(mock_run)
        assert "--sandbox" not in cmd

    def test_resume_omits_sandbox_flag(self, tmp_path: Path) -> None:
        """Resume path omits --sandbox because current Codex CLI rejects it there."""
        profile = _make_profile(sandbox_mode="workspace-write")
        mock_proc = _make_subprocess_mock()
        with patch(_RUN_TARGET, return_value=mock_proc) as mock_run:
            _run_codex(
                prompt="continue",
                profile=profile,
                working_dir=tmp_path,
                session_id="sess-abc123",
            )
        cmd = _extract_codex_cmd(mock_run)
        assert "resume" in cmd
        assert "--sandbox" not in cmd

    def test_sandbox_flag_none_on_resume(self, tmp_path: Path) -> None:
        """sandbox_mode=none omits --sandbox on resume too."""
        profile = _make_profile(sandbox_mode="none")
        mock_proc = _make_subprocess_mock()
        with patch(_RUN_TARGET, return_value=mock_proc) as mock_run:
            _run_codex(
                prompt="continue",
                profile=profile,
                working_dir=tmp_path,
                session_id="sess-abc123",
            )
        cmd = _extract_codex_cmd(mock_run)
        assert "--sandbox" not in cmd
        assert "resume" in cmd

    def test_resume_omits_working_dir_flag(self, tmp_path: Path) -> None:
        """`codex exec resume` does not accept -C; working dir is passed via cwd= instead."""
        profile = _make_profile(sandbox_mode="workspace-write")
        mock_proc = _make_subprocess_mock()
        with patch(_RUN_TARGET, return_value=mock_proc) as mock_run:
            _run_codex(
                prompt="continue",
                profile=profile,
                working_dir=tmp_path,
                session_id="sess-abc123",
            )
        cmd = _extract_codex_cmd(mock_run)
        assert "resume" in cmd
        assert "-C" not in cmd

    def test_resume_orders_session_id_after_flags(self, tmp_path: Path) -> None:
        """Resume command keeps flags before the positional session id."""
        profile = _make_profile(sandbox_mode="workspace-write")
        mock_proc = _make_subprocess_mock()
        with patch(_RUN_TARGET, return_value=mock_proc) as mock_run:
            _run_codex(
                prompt="continue",
                profile=profile,
                working_dir=tmp_path,
                session_id="sess-abc123",
            )
        cmd = _extract_codex_cmd(mock_run)
        assert cmd[:4] == ["npx", "@openai/codex", "exec", "resume"]
        assert cmd[-2:] == ["sess-abc123", "-"]

    def test_uses_workspace_venv_env(self, tmp_path: Path) -> None:
        """Codex subprocess env prefers the worktree virtualenv."""
        profile = _make_profile(sandbox_mode="none")
        _write_matching_workspace_venv(tmp_path)
        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True, exist_ok=True)
        mock_proc = _make_subprocess_mock()

        with patch("theforge.runners.runner_codex._get_codex_session_id", return_value=None):
            with patch(_RUN_TARGET, return_value=mock_proc) as mock_run:
                _run_codex(
                    prompt="debug run",
                    profile=profile,
                    working_dir=tmp_path,
                    secrets={"OPENAI_API_KEY": "secret"},
                )

        env_passed = mock_run.call_args[1]["env"]
        assert env_passed["PATH"].split(os.pathsep)[0] == str(venv_bin)
        assert env_passed["VIRTUAL_ENV"] == str(tmp_path / ".venv")
        assert env_passed["OPENAI_API_KEY"] == "secret"


class TestCodexResumeSandboxReassertion:
    """Resume reasserts the session's sandbox policy explicitly (issue #1012).

    The fresh run selects containment with ``--sandbox <mode>``; the resume
    subcommand rejects that flag, so forge restates the same native policy via
    ``-c sandbox_mode=<mode>`` guarded by ``--strict-config`` (fail closed on CLI
    drift) rather than trusting the CLI to carry the policy forward.
    """

    def _resume_cmd(self, tmp_path: Path, sandbox_mode: str) -> list[str]:
        profile = _make_profile(sandbox_mode=sandbox_mode)
        mock_proc = _make_subprocess_mock()
        with patch(_RUN_TARGET, return_value=mock_proc) as mock_run:
            _run_codex(
                prompt="continue",
                profile=profile,
                working_dir=tmp_path,
                session_id="sess-abc123",
            )
        return _extract_codex_cmd(mock_run)

    def test_workspace_write_reasserts_via_config_override(self, tmp_path: Path) -> None:
        """workspace-write resume restates the policy with -c sandbox_mode, not --sandbox."""
        cmd = self._resume_cmd(tmp_path, "workspace-write")
        assert "--sandbox" not in cmd  # resume path rejects the flag
        assert "-c" in cmd
        c_idx = cmd.index("-c")
        assert cmd[c_idx + 1] == "sandbox_mode=workspace-write"

    def test_read_only_reasserts_matching_policy(self, tmp_path: Path) -> None:
        """read-only resume restates read-only (continuity, not full-auto's workspace-write)."""
        cmd = self._resume_cmd(tmp_path, "read-only")
        assert "sandbox_mode=read-only" in cmd
        assert "sandbox_mode=workspace-write" not in cmd

    def test_strict_config_guards_the_reassertion(self, tmp_path: Path) -> None:
        """--strict-config precedes the sandbox override so an unknown field fails closed."""
        cmd = self._resume_cmd(tmp_path, "workspace-write")
        assert "--strict-config" in cmd
        # The guard must come before the override it protects.
        assert cmd.index("--strict-config") < cmd.index("sandbox_mode=workspace-write")

    def test_none_opts_out_of_reassertion(self, tmp_path: Path) -> None:
        """sandbox_mode=none reasserts nothing and does not force the strict-config guard."""
        cmd = self._resume_cmd(tmp_path, "none")
        assert not any(a.startswith("sandbox_mode=") for a in cmd)
        assert "--strict-config" not in cmd

    def test_deprecated_full_auto_flag_dropped_on_resume(self, tmp_path: Path) -> None:
        """--full-auto (deprecated; contradicts an explicit read-only override) is gone."""
        assert "--full-auto" not in self._resume_cmd(tmp_path, "workspace-write")
        assert "--full-auto" not in self._resume_cmd(tmp_path, "read-only")
        assert "--full-auto" not in self._resume_cmd(tmp_path, "none")

    def test_reassertion_coexists_with_reasoning_override(self, tmp_path: Path) -> None:
        """Both -c overrides are present and each is validated under --strict-config."""
        profile = _make_profile(sandbox_mode="read-only", reasoning_effort="high")
        mock_proc = _make_subprocess_mock()
        with patch(_RUN_TARGET, return_value=mock_proc) as mock_run:
            _run_codex(
                prompt="continue",
                profile=profile,
                working_dir=tmp_path,
                session_id="sess-abc123",
            )
        cmd = _extract_codex_cmd(mock_run)
        assert "sandbox_mode=read-only" in cmd
        assert "model_reasoning_effort=high" in cmd
        assert "--strict-config" in cmd


# ---------------------------------------------------------------------------
# Lifecycle tests — real subprocess, fake-CLI fixture
# ---------------------------------------------------------------------------


class TestCodexLifecycle:
    """Real-subprocess lifecycle tests for the Codex CLI runner.

    Uses tests/fake_bin/npx (routing @openai/codex) rather than mocking
    subprocess.run, so the tests exercise real process invocation and output-
    file reading that mocks cannot detect.
    """

    _FAKE_BIN: ClassVar[Path] = Path(__file__).parent / "fake_bin"

    @pytest.fixture(autouse=True)
    def _ensure_executable(self) -> None:
        script = self._FAKE_BIN / "npx"
        script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def _patch_env(self, monkeypatch: pytest.MonkeyPatch, mode: str = "happy") -> None:
        """Patch runner_codex.build_workspace_env to put fake_bin first on PATH."""
        fake_bin = self._FAKE_BIN
        from theforge.workspace_env import build_workspace_env as _orig

        def _build(
            workspace_path: Path,
            base_env: object = None,
            *,
            extra: object = None,
        ) -> dict:
            env = _orig(workspace_path, base_env, extra=extra)  # type: ignore[arg-type]
            env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
            env["FAKE_CODEX_MODE"] = mode
            return env

        monkeypatch.setattr("theforge.runners.runner_codex.build_workspace_env", _build)

    def test_happy_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Fake npx(codex) writes output file; runner reads it and returns success."""
        self._patch_env(monkeypatch, "happy")
        profile = _make_profile(sandbox_mode="none")
        result = _run_codex(
            prompt="do the thing",
            profile=profile,
            working_dir=tmp_path,
        )
        assert result.success is True
        assert "Task complete." in result.output
        assert result.exit_code == 0

    def test_no_usage_records_cost_unknown_not_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When codex emits no token usage, cost is None (unmeasured), never $0.00."""
        self._patch_env(monkeypatch, "happy")
        profile = _make_profile(sandbox_mode="none")
        result = _run_codex(
            prompt="do the thing",
            profile=profile,
            working_dir=tmp_path,
        )
        assert result.cost_usd is None
        assert result.model_usage == ()

    def test_json_usage_yields_real_estimated_cost(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A codex JSON blob with a usage block is priced via the pricing table."""
        self._patch_env(monkeypatch, "usage")
        profile = _make_profile(sandbox_mode="none")  # model o4-mini: (1.10, 4.40)/Mtok
        result = _run_codex(
            prompt="do the thing",
            profile=profile,
            working_dir=tmp_path,
        )
        # 1000 in * 1.10/M + 500 out * 4.40/M = 0.0011 + 0.0022 = 0.0033
        assert result.cost_usd is not None
        assert result.cost_usd == pytest.approx(0.0033)
        assert len(result.model_usage) == 1
        usage = result.model_usage[0]
        assert usage.input_tokens == 1000
        assert usage.output_tokens == 500

    def test_stdout_usage_line_yields_real_estimated_cost(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Cost is recovered from codex's stdout token-usage summary line too."""
        self._patch_env(monkeypatch, "usage_stdout")
        profile = _make_profile(sandbox_mode="none")
        result = _run_codex(
            prompt="do the thing",
            profile=profile,
            working_dir=tmp_path,
        )
        assert result.cost_usd == pytest.approx(0.0033)
        assert len(result.model_usage) == 1

    def test_real_total_only_summary_is_cost_unknown_not_fabricated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The real Codex CLI human summary is a bare total (no input/output split).

        A total alone can't be priced with the (input, output) table, so the run
        must be recorded cost-unknown (None) — never a fabricated cost derived by
        guessing a split from the total.
        """
        self._patch_env(monkeypatch, "total_only")
        profile = _make_profile(sandbox_mode="none")
        result = _run_codex(
            prompt="do the thing",
            profile=profile,
            working_dir=tmp_path,
        )
        assert result.cost_usd is None
        assert result.model_usage == ()
