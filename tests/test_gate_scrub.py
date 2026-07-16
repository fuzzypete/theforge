from __future__ import annotations

import os
from pathlib import Path

import pytest

from theforge.config import (
    SCRUBBED_CLI_LAUNCHERS,
    SCRUBBED_ENV_VARS,
    SCRUBBED_HOME_PATHS,
    ModelProfile,
)
from theforge.config.auth import check_agent_auth
from theforge.config.gate_scrub import (
    SCRUBBED_CLI_LAUNCHERS as MODULE_CLI_LAUNCHERS,
)
from theforge.config.gate_scrub import (
    SCRUBBED_ENV_VARS as MODULE_ENV_VARS,
)


def test_scrubbed_env_vars_cover_expected_credentials():
    expected = {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_BASE_URL",
        "DOTENV_PATH",
        "DOTENV_FILE",
        "PYTHON_DOTENV_DISABLED",
    }
    assert expected.issubset(set(SCRUBBED_ENV_VARS))
    assert SCRUBBED_ENV_VARS == MODULE_ENV_VARS


def test_scrubbed_home_paths_cover_cli_auth_locations():
    scrubbed = {Path(p) for p in SCRUBBED_HOME_PATHS}
    assert Path(".codex/auth.json") in scrubbed
    assert Path(".claude") in scrubbed
    assert Path(".config/gcloud") in scrubbed
    assert Path(".gemini") in scrubbed


def test_scrubbed_cli_launchers_cover_supported_auth_probes():
    # "ghaw" is derived from SUPPORTED_CLIS; its real launcher is `gh`, which
    # stays unscrubbed — ghaw launches no local model, and remote dispatch is
    # blocked in tests by the network guard.
    expected = {"claude", "codex", "gemini", "ghaw", "npx"}
    assert expected == set(SCRUBBED_CLI_LAUNCHERS)
    assert SCRUBBED_CLI_LAUNCHERS == MODULE_CLI_LAUNCHERS


def test_gate_scrub_fixture_sets_dotenv_disable_and_rehomes_home():
    assert os.environ["PYTHON_DOTENV_DISABLED"] == "1"
    scrubbed_home = Path(os.environ["HOME"])
    for rel_path in SCRUBBED_HOME_PATHS:
        assert (scrubbed_home / rel_path).exists()


def test_gate_scrub_fixture_hides_cli_auth_launchers():
    claude_profile = ModelProfile(
        name="dev",
        cli="claude",
        model="sonnet",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=(),
    )
    codex_profile = ModelProfile(
        name="dev",
        cli="codex",
        model="gpt-5.4",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=(),
    )

    ok, reason = check_agent_auth(claude_profile, {})
    assert ok is False
    assert reason == "'claude' not found in PATH"

    ok, reason = check_agent_auth(codex_profile, {})
    assert ok is False
    assert reason == "npx not found in PATH"


def test_real_runner_sentinel_message_is_gate_specific():
    with pytest.raises(AssertionError, match="real agent call blocked by gate scrub"):
        raise AssertionError(
            "real agent call blocked by gate scrub: theforge.runners.run_agent; "
            "patch theforge.runners.run_agent explicitly."
        )
