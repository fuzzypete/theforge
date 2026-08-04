"""Tests for Codex CLI runner: sandbox flag injection."""

from __future__ import annotations

import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from theforge.config import ModelProfile
from theforge.runners.runner_codex import (
    _agent_text_from_events,
    _classify_codex_launch_failure,
    _CodexUsage,
    _error_text_from_events,
    _price_codex_usage,
    _run_codex,
    _usage_from_events,
    build_argv,
)

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
        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
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


class TestCodexJsonInvocationMode:
    """Codex must be invoked in machine-readable event mode (#2019).

    Without ``--json`` the only usage figure codex prints is a bare human total,
    which cannot be priced — so every run was recorded cost-unknown and any
    multi-story sprint on a codex pool deadlocked on the fail-closed budget check.
    """

    def test_fresh_run_requests_json_events(self, tmp_path: Path) -> None:
        profile = _make_profile(sandbox_mode="workspace-write")
        mock_proc = _make_subprocess_mock()
        with patch("theforge.runners.runner_codex._get_codex_session_id", return_value=None):
            with patch(_RUN_TARGET, return_value=mock_proc) as mock_run:
                _run_codex(prompt="do it", profile=profile, working_dir=tmp_path)
        cmd = _extract_codex_cmd(mock_run)
        assert "--json" in cmd
        # Flag must apply to `exec`, i.e. sit after the subcommand.
        assert cmd.index("--json") > cmd.index("exec")

    def test_resume_requests_json_events(self, tmp_path: Path) -> None:
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
        assert "--json" in cmd
        assert cmd.index("--json") > cmd.index("resume")

    def test_output_last_message_file_still_requested(self, tmp_path: Path) -> None:
        """Agent text still comes from -o; --json only changes what stdout carries."""
        profile = _make_profile(sandbox_mode="none")
        mock_proc = _make_subprocess_mock()
        with patch("theforge.runners.runner_codex._get_codex_session_id", return_value=None):
            with patch(_RUN_TARGET, return_value=mock_proc) as mock_run:
                _run_codex(prompt="do it", profile=profile, working_dir=tmp_path)
        assert "-o" in _extract_codex_cmd(mock_run)


class TestCodexEventUsageParsing:
    """Unit coverage for the `turn.completed` usage parser."""

    _EVENT = (
        '{"type":"thread.started","thread_id":"t1"}\n'
        '{"type":"turn.completed","usage":{"input_tokens":12892,'
        '"cached_input_tokens":9088,"cache_write_input_tokens":0,'
        '"output_tokens":21,"reasoning_output_tokens":14}}\n'
    )

    def test_parses_full_split(self) -> None:
        usage = _usage_from_events(self._EVENT)
        assert usage is not None
        assert usage.input_tokens == 12892
        assert usage.cached_input_tokens == 9088
        assert usage.output_tokens == 21
        assert usage.reasoning_output_tokens == 14

    def test_sums_multiple_turns(self) -> None:
        usage = _usage_from_events(self._EVENT + self._EVENT.splitlines()[1] + "\n")
        assert usage is not None
        assert usage.input_tokens == 12892 * 2
        assert usage.output_tokens == 42

    def test_ignores_non_turn_events_and_garbage_lines(self) -> None:
        stream = 'preamble text\n{"type":"item.completed"}\n{"broken\n'
        assert _usage_from_events(stream) is None

    def test_tolerates_truncated_final_line(self) -> None:
        """A killed run's last line is often half-written; earlier turns still count."""
        usage = _usage_from_events(self._EVENT + '{"type":"turn.compl')
        assert usage is not None
        assert usage.input_tokens == 12892

    def test_agent_text_reconstructed_from_events(self) -> None:
        stream = (
            '{"type":"item.completed","item":{"type":"agent_message","text":"Done here."}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n'
        )
        assert _agent_text_from_events(stream) == "Done here."

    def test_agent_text_none_when_no_message_event(self) -> None:
        assert _agent_text_from_events(self._EVENT) is None

    def test_error_events_surface_for_quota_fallback_classification(self) -> None:
        """Failures carried only on stdout must still reach AgentResult.output.

        The CLI→API fallback classifier pattern-matches that text, so an error
        stranded inside the event stream would silently disable quota fallback.
        """
        stream = '{"type":"error","message":"429 rate limit exceeded"}\n'
        assert _error_text_from_events(stream) == "429 rate limit exceeded"

    def test_error_text_reads_nested_message(self) -> None:
        stream = '{"type":"turn.failed","error":{"message":"usage limit reached"}}\n'
        assert _error_text_from_events(stream) == "usage limit reached"


class TestCodexGitRepoTrustGate:
    """Forge runs codex against directories that are not git repos (#2164).

    The preflight / escalation-advisor baseline checkout is built with
    ``git archive | tar -x`` — a plain file tree with no ``.git``. Codex's trust
    gate refuses such a directory unless ``--skip-git-repo-check`` is passed, and
    exits before contacting the model.
    """

    def test_fresh_run_skips_the_git_repo_check(self, tmp_path: Path) -> None:
        profile = _make_profile(sandbox_mode="workspace-write")
        mock_proc = _make_subprocess_mock()
        with patch("theforge.runners.runner_codex._get_codex_session_id", return_value=None):
            with patch(_RUN_TARGET, return_value=mock_proc) as mock_run:
                _run_codex(prompt="advise", profile=profile, working_dir=tmp_path)
        cmd = _extract_codex_cmd(mock_run)
        assert "--skip-git-repo-check" in cmd
        # Must apply to the `exec` subcommand, not sit before it.
        assert cmd.index("--skip-git-repo-check") > cmd.index("exec")

    def test_build_argv_carries_the_flag_directly(self, tmp_path: Path) -> None:
        argv = build_argv(
            profile=_make_profile(sandbox_mode="none"),
            working_dir=tmp_path,
            output_file=tmp_path / "out.txt",
            prompt="advise",
        )
        assert "--skip-git-repo-check" in argv


class TestCodexLaunchFailureClassification:
    """A pre-turn exit is a measured $0.00, not cost-unknown (#2164)."""

    _TRUST_GATE_STDERR = (
        "warning: `--full-auto` is deprecated; use `--sandbox workspace-write` instead.\n"
        "Reading additional input from stdin...\n"
        "Not inside a trusted directory and --skip-git-repo-check was not specified.\n"
    )

    def test_no_events_and_nonzero_exit_is_a_launch_failure(self) -> None:
        reason = _classify_codex_launch_failure(
            returncode=1, stdout="", stderr=self._TRUST_GATE_STDERR
        )
        assert (
            reason == "Not inside a trusted directory and --skip-git-repo-check was not specified."
        )

    def test_deprecation_noise_is_not_mistaken_for_the_reason(self) -> None:
        reason = _classify_codex_launch_failure(
            returncode=1, stdout="", stderr="warning: `--full-auto` is deprecated;\n"
        )
        # No substantive line → falls back to the raw last line rather than None.
        assert reason is not None
        assert "deprecated" in reason

    def test_zero_exit_is_never_a_launch_failure(self) -> None:
        assert _classify_codex_launch_failure(returncode=0, stdout="", stderr="noise") is None

    def test_turn_activity_rules_out_a_launch_failure(self) -> None:
        stdout = (
            '{"type":"thread.started","thread_id":"t1"}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}\n'
        )
        assert _classify_codex_launch_failure(returncode=1, stdout=stdout, stderr="boom") is None

    def test_turn_failed_counts_as_a_started_turn(self) -> None:
        """The model was engaged: unmeasurable, not free."""
        stdout = '{"type":"turn.failed","error":{"message":"upstream 500"}}\n'
        assert _classify_codex_launch_failure(returncode=1, stdout=stdout, stderr="") is None

    def test_thread_started_alone_still_counts_as_pre_turn(self) -> None:
        stdout = '{"type":"thread.started","thread_id":"t1"}\n'
        reason = _classify_codex_launch_failure(returncode=2, stdout=stdout, stderr="bad flag")
        assert reason == "bad flag"

    def test_unreadable_stdout_fails_closed_to_cost_unknown(self) -> None:
        """Prose on stdout means something ran that this parser cannot account for.

        Claiming $0.00 there would fabricate a zero — exactly what the
        cost-unknown contract exists to prevent. Only an all-pre-turn (or empty)
        event stream proves a free run.
        """
        assert (
            _classify_codex_launch_failure(returncode=1, stdout="partial work", stderr="") is None
        )


class TestCodexCachedTokenPricing:
    """Cached input must be discounted, and reasoning output must not be re-billed."""

    def test_cached_input_is_discounted_not_charged_at_full_rate(self) -> None:
        profile = _make_profile()
        usage = _CodexUsage(
            input_tokens=12892,
            cached_input_tokens=9088,
            output_tokens=21,
            reasoning_output_tokens=14,
        )
        cost, model_usage = _price_codex_usage(profile=profile, usage=usage)
        # o4-mini: (1.10, 4.40)/Mtok. uncached = 12892 - 9088 = 3804.
        expected = (3804 / 1e6) * 1.10 + (9088 / 1e6) * 1.10 * 0.1 + (21 / 1e6) * 4.40
        assert cost == pytest.approx(expected)
        # A flat-rate charge would be strictly larger — that overstatement is the
        # bug this guards.
        assert cost < (12892 / 1e6) * 1.10 + (21 / 1e6) * 4.40

    def test_reasoning_tokens_recorded_but_not_double_billed(self) -> None:
        profile = _make_profile()
        usage = _CodexUsage(input_tokens=0, output_tokens=1000, reasoning_output_tokens=800)
        cost, model_usage = _price_codex_usage(profile=profile, usage=usage)
        assert cost == pytest.approx((1000 / 1e6) * 4.40)
        assert model_usage[0].thinking_tokens == 800

    def test_cache_write_tokens_surface_in_model_usage(self) -> None:
        profile = _make_profile()
        usage = _CodexUsage(input_tokens=100, cache_write_input_tokens=40, output_tokens=10)
        _, model_usage = _price_codex_usage(profile=profile, usage=usage)
        assert model_usage[0].cache_creation_tokens == 40
        assert model_usage[0].cache_read_tokens == 0


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

    def test_json_event_usage_yields_measured_cost(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A `codex exec --json` turn.completed event is parsed into measured cost."""
        self._patch_env(monkeypatch, "json_usage")
        profile = _make_profile(sandbox_mode="none")  # o4-mini: (1.10, 4.40)/Mtok
        result = _run_codex(prompt="do the thing", profile=profile, working_dir=tmp_path)

        assert result.success is True
        assert result.cost_usd is not None
        assert len(result.model_usage) == 1
        usage = result.model_usage[0]
        assert usage.input_tokens == 12892
        assert usage.cache_read_tokens == 9088
        assert usage.cache_creation_tokens == 256
        assert usage.output_tokens == 2100
        assert usage.thinking_tokens == 1400
        expected = (3804 / 1e6) * 1.10 + (9088 / 1e6) * 1.10 * 0.1 + (2100 / 1e6) * 4.40
        assert result.cost_usd == pytest.approx(expected)

    def test_json_mode_does_not_leak_events_into_agent_output(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Empty -o file falls back to the agent message, not the raw JSONL stream."""
        self._patch_env(monkeypatch, "json_no_output_file")
        profile = _make_profile(sandbox_mode="none")
        result = _run_codex(prompt="do the thing", profile=profile, working_dir=tmp_path)

        assert result.output == "Task complete."
        assert "turn.completed" not in result.output
        assert result.cost_usd is not None

    def test_multi_turn_usage_accumulates(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Two turn.completed events sum, so a multi-turn run is fully accounted."""
        self._patch_env(monkeypatch, "json_two_turns")
        profile = _make_profile(sandbox_mode="none")
        result = _run_codex(prompt="do the thing", profile=profile, working_dir=tmp_path)

        assert result.model_usage[0].input_tokens == 12892 * 2
        assert result.model_usage[0].output_tokens == 2100 * 2

    def test_killed_run_recovers_partial_usage_before_cost_unknown(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A run killed on timeout keeps the usage it already emitted (#2019).

        The old path discarded TimeoutExpired output entirely and returned
        cost_usd=None even when the CLI had already reported a completed turn.
        """
        self._patch_env(monkeypatch, "json_partial_hang")
        profile = replace(_make_profile(sandbox_mode="none"), timeout_seconds=3)
        result = _run_codex(prompt="do the thing", profile=profile, working_dir=tmp_path)

        assert result.success is False
        assert result.failure_code == "timeout"
        assert result.cost_usd is not None
        assert result.model_usage[0].input_tokens == 12892

    def test_killed_run_without_usage_stays_cost_unknown(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No usage emitted before the kill → cost-unknown remains the honest answer."""
        self._patch_env(monkeypatch, "grandchild_hang")
        profile = replace(_make_profile(sandbox_mode="none"), timeout_seconds=3)
        result = _run_codex(prompt="do the thing", profile=profile, working_dir=tmp_path)

        assert result.success is False
        assert result.cost_usd is None
        assert result.model_usage == ()

    def test_runs_in_a_directory_with_no_git_repo(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Codex must start in the .git-less git-archive baseline checkout (#2164).

        The fake CLI reproduces codex's trust gate: it refuses unless
        ``--skip-git-repo-check`` is passed. ``tmp_path`` has no ``.git``, exactly
        like the preflight/escalation-advisor baseline dir.
        """
        assert not (tmp_path / ".git").exists()
        self._patch_env(monkeypatch, "git_trust_gate")
        profile = _make_profile(sandbox_mode="none")
        result = _run_codex(prompt="advise", profile=profile, working_dir=tmp_path)

        assert result.success is True, result.output
        assert "trusted directory" not in result.output
        assert result.cost_usd is not None

    def test_pre_turn_exit_is_measured_zero_cost_launch_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A process that dies before any turn spent nothing — $0.00, not unknown.

        Distinct from ``test_real_total_only_summary_is_cost_unknown_not_fabricated``:
        there the run executed and its usage could not be parsed (cost-unknown is
        the honest answer); here no turn ever began, so zero is measured fact.
        """
        self._patch_env(monkeypatch, "launch_refusal")
        profile = _make_profile(sandbox_mode="none")
        result = _run_codex(prompt="advise", profile=profile, working_dir=tmp_path)

        assert result.success is False
        assert result.cost_usd == 0.0
        assert result.model_usage == ()
        assert result.startup_failure is True
        assert result.failure_code == "cli_launch_failure"
        assert "trusted directory" in result.output

    def test_completed_run_with_unparseable_usage_stays_cost_unknown(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The launch-failure path must not swallow the cost-unknown contract."""
        self._patch_env(monkeypatch, "total_only")
        profile = _make_profile(sandbox_mode="none")
        result = _run_codex(prompt="do the thing", profile=profile, working_dir=tmp_path)

        assert result.cost_usd is None
        assert result.startup_failure is False
        assert result.failure_code is None

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
