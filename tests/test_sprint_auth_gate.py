"""Auth readiness gate and post-launch auth circuit breaker (#1952).

Two boundaries are covered here:

- **Pre-dispatch.** A revoked Claude credential must abort the sprint before
  any story is dispatched, in seconds, with an operator-facing message naming
  the credential — and with no story acquiring a failure verdict.
- **Post-launch.** A fatal auth failure discovered after dispatch must stop the
  sprint scheduling further stories instead of re-presenting the same rejected
  credential once per story and per phase.

Every fixture is a fake: no real provider CLI is invoked and no real credential
is read. The credential store is a temp file the test writes itself.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.config.auth import (
    CLAUDE_CLI_ENV_TOKENS,
    check_agent_auth,
    check_claude_credentials,
)
from theforge.config.types import (
    ModelProfile,
    PlanAgentReviewConfig,
    PlanConfig,
)
from theforge.coordinator.agent_failure import (
    CATEGORY_AUTH,
    CATEGORY_TRANSPORT,
    ERROR_TYPE_INFRASTRUCTURE_ABORT,
    AgentInvocationFailure,
    is_infrastructure_abort,
    mark_infrastructure_abort,
)
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.sprint.auth_gate import (
    SprintAuthUnavailable,
    check_sprint_auth_readiness,
    enforce_sprint_auth_readiness,
)
from theforge.sprint.manifest import ResolvedSprint
from theforge.sprint.runner import run_sprint
from theforge.sprint.sources import FileSource

# The exact shape the CLI leaves behind after repeated 401s: both tokens
# blanked, expiresAt zeroed, refreshTokenExpiresAt still a week out. This is
# what makes the file read as valid until token *length* is checked.
_REVOKED_CREDENTIAL = {
    "claudeAiOauth": {
        "accessToken": "",
        "refreshToken": "",
        "expiresAt": 0,
        "refreshTokenExpiresAt": 1785854291411,
        "scopes": ["user:inference"],
        "subscriptionType": "max",
    }
}


@pytest.fixture(autouse=True)
def _no_ambient_claude_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every env token that would satisfy the probe without the store.

    Without this, a developer shell exporting CLAUDE_CODE_OAUTH_TOKEN or
    ANTHROPIC_API_KEY makes the probe short-circuit to ready and every
    revoked-credential assertion below fails for a reason that has nothing to
    do with the code. These tests assert on a credential file they wrote
    themselves; the host must not be able to answer for it.
    """
    for var in CLAUDE_CLI_ENV_TOKENS:
        monkeypatch.delenv(var, raising=False)


def _now_ms() -> float:
    return time.time() * 1000.0


def _write_credentials(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / ".credentials.json"
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload),
        encoding="utf-8",
    )
    return path


# ── check_claude_credentials ─────────────────────────────────────────────


def test_revoked_credential_is_not_ready(tmp_path: Path) -> None:
    """Blank tokens with a future refresh expiry are the revocation signature."""
    path = _write_credentials(tmp_path, _REVOKED_CREDENTIAL)

    ready, reason = check_claude_credentials({}, credentials_path=path)

    assert ready is False
    assert str(path) in reason
    assert "revoked" in reason.lower()


def test_reason_never_contains_token_material(tmp_path: Path) -> None:
    """A failure reason names the path and the condition, never a secret."""
    secret = "sk-ant-oat01-DO-NOT-LEAK"
    path = _write_credentials(
        tmp_path,
        {
            "claudeAiOauth": {
                "accessToken": secret,
                "refreshToken": "",
                "expiresAt": 0,
            }
        },
    )

    ready, reason = check_claude_credentials({}, credentials_path=path)

    assert ready is False
    assert secret not in reason
    assert "DO-NOT-LEAK" not in reason


def test_missing_credential_file_makes_no_claim(tmp_path: Path) -> None:
    """Absence of evidence is not evidence: the CLI may use the Keychain."""
    ready, reason = check_claude_credentials({}, credentials_path=tmp_path / "does-not-exist.json")

    assert ready is True
    assert reason == ""


def test_malformed_json_is_not_ready(tmp_path: Path) -> None:
    path = _write_credentials(tmp_path, "{not json at all")

    ready, reason = check_claude_credentials({}, credentials_path=path)

    assert ready is False
    assert "not valid JSON" in reason


def test_unknown_store_layout_makes_no_claim(tmp_path: Path) -> None:
    """A store written by another auth backend is not classifiable as dead."""
    path = _write_credentials(tmp_path, {"someOtherBackend": {"token": "x"}})

    ready, _reason = check_claude_credentials({}, credentials_path=path)

    assert ready is True


def test_live_access_token_is_ready(tmp_path: Path) -> None:
    path = _write_credentials(
        tmp_path,
        {
            "claudeAiOauth": {
                "accessToken": "live-token",
                "refreshToken": "live-refresh",
                "expiresAt": int(_now_ms() + 3_600_000),
                "refreshTokenExpiresAt": int(_now_ms() + 604_800_000),
            }
        },
    )

    ready, reason = check_claude_credentials({}, credentials_path=path)

    assert ready is True
    assert reason == ""


def test_expired_access_with_live_refresh_is_ready(tmp_path: Path) -> None:
    """A refreshable credential is healthy — the CLI renews it transparently."""
    path = _write_credentials(
        tmp_path,
        {
            "claudeAiOauth": {
                "accessToken": "stale-token",
                "refreshToken": "live-refresh",
                "expiresAt": int(_now_ms() - 1000),
                "refreshTokenExpiresAt": int(_now_ms() + 604_800_000),
            }
        },
    )

    ready, _reason = check_claude_credentials({}, credentials_path=path)

    assert ready is True


def test_expired_access_and_expired_refresh_is_not_ready(tmp_path: Path) -> None:
    path = _write_credentials(
        tmp_path,
        {
            "claudeAiOauth": {
                "accessToken": "stale-token",
                "refreshToken": "stale-refresh",
                "expiresAt": int(_now_ms() - 10_000),
                "refreshTokenExpiresAt": int(_now_ms() - 1000),
            }
        },
    )

    ready, reason = check_claude_credentials({}, credentials_path=path)

    assert ready is False
    assert "expired refresh token" in reason


def test_env_token_bypasses_the_store(tmp_path: Path) -> None:
    """With an env credential the store is irrelevant; probing it would be a lie."""
    path = _write_credentials(tmp_path, _REVOKED_CREDENTIAL)

    ready, _reason = check_claude_credentials(
        {"ANTHROPIC_API_KEY": "sk-present"}, credentials_path=path
    )

    assert ready is True


# ── check_agent_auth integration ─────────────────────────────────────────


def _claude_cli_profile():
    return replace(DEFAULT_DEV_PROFILE, name="dev")


def test_check_agent_auth_probe_is_opt_in(tmp_path: Path) -> None:
    """Without the flag a stale credential must not fail config-time checks."""
    path = _write_credentials(tmp_path, _REVOKED_CREDENTIAL)

    with (
        patch("theforge.config.auth.shutil.which", return_value="/usr/bin/claude"),
        patch("theforge.config.auth.claude_credentials_path", return_value=path),
    ):
        ready_default, _ = check_agent_auth(
            _claude_cli_profile(), {}, include_sandbox_readiness=False
        )
        ready_probed, reason = check_agent_auth(
            _claude_cli_profile(),
            {},
            include_sandbox_readiness=False,
            include_credential_probe=True,
        )

    assert ready_default is True
    assert ready_probed is False
    assert "revoked" in reason.lower()


# ── sprint launch gate ───────────────────────────────────────────────────


def _make_config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
            base_branch="main",
        ),
        validation=replace(DEFAULT_VALIDATION, gate_command="true"),
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
    )


def _make_resolved(tmp_path: Path, slugs: tuple[str, ...]) -> ResolvedSprint:
    source = FileSource()
    stories = []
    for slug in slugs:
        story_file = tmp_path / f"{slug}.md"
        story_file.write_text(
            f"---\nname: Story {slug}\nslug: {slug}\n---\n# Content\n",
            encoding="utf-8",
        )
        task = source.fetch(f"{slug}.md", tmp_path)
        stories.append((task, source, f"{slug}.md"))
    return ResolvedSprint(
        name="Test Sprint",
        budget_usd=10.0,
        stories=stories,
        max_parallel=1,
    )


def test_gate_reports_revoked_credential_for_configured_profiles(tmp_path: Path) -> None:
    path = _write_credentials(tmp_path, _REVOKED_CREDENTIAL)
    config = _make_config(tmp_path)

    with patch("theforge.config.auth.claude_credentials_path", return_value=path):
        failures = check_sprint_auth_readiness(config)

    assert failures
    assert {f.label for f in failures} <= {"dev", "preflight", "review", "synthesis"}
    assert all("revoked" in f.reason.lower() for f in failures)


def _api_profile(name: str) -> ModelProfile:
    """A non-Claude profile, so the only Claude surface can be isolated."""
    return ModelProfile(
        name=name,
        cli=None,
        provider="openai",
        model="gpt-5.4",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=(),
    )


def _all_api_config(tmp_path: Path, **overrides) -> ForgeConfig:
    """A config whose dev/preflight/review profiles are all non-Claude."""
    return replace(
        _make_config(tmp_path),
        dev_profile=_api_profile("dev"),
        preflight_profile=_api_profile("preflight"),
        review_pool=[_api_profile("review")],
        **overrides,
    )


def test_gate_covers_a_claude_planner_when_nothing_else_is_claude(tmp_path: Path) -> None:
    """PLAN dispatches and spends; a revoked planner credential must abort too."""
    path = _write_credentials(tmp_path, _REVOKED_CREDENTIAL)
    config = _all_api_config(tmp_path, plan=PlanConfig(enabled=True, cli="claude"))

    with patch("theforge.config.auth.claude_credentials_path", return_value=path):
        failures = check_sprint_auth_readiness(config)

    assert [f.label for f in failures] == ["plan"]
    assert "revoked" in failures[0].reason.lower()


def test_gate_covers_a_claude_plan_reviewer_when_nothing_else_is_claude(
    tmp_path: Path,
) -> None:
    path = _write_credentials(tmp_path, _REVOKED_CREDENTIAL)
    config = _all_api_config(
        tmp_path,
        plan_agent_review=PlanAgentReviewConfig(enabled=True, cli="claude"),
    )

    with patch("theforge.config.auth.claude_credentials_path", return_value=path):
        failures = check_sprint_auth_readiness(config)

    assert [f.label for f in failures] == ["plan-review"]


def test_gate_covers_a_claude_plan_review_pool(tmp_path: Path) -> None:
    """The pool format must be enumerated, not just the legacy scalar fields."""
    path = _write_credentials(tmp_path, _REVOKED_CREDENTIAL)
    config = _all_api_config(
        tmp_path,
        plan_agent_review=PlanAgentReviewConfig(
            enabled=True,
            pool=[replace(DEFAULT_REVIEW_PROFILE, name="plan-reviewer-1")],
        ),
    )

    with patch("theforge.config.auth.claude_credentials_path", return_value=path):
        failures = check_sprint_auth_readiness(config)

    assert [f.profile_name for f in failures] == ["plan-reviewer-1"]


def test_gate_ignores_a_disabled_claude_planner(tmp_path: Path) -> None:
    """A phase that never dispatches makes no claim on the credential."""
    path = _write_credentials(tmp_path, _REVOKED_CREDENTIAL)
    config = _all_api_config(
        tmp_path,
        plan=PlanConfig(enabled=False, cli="claude"),
        plan_agent_review=PlanAgentReviewConfig(enabled=False, cli="claude"),
    )

    with patch("theforge.config.auth.claude_credentials_path", return_value=path):
        assert check_sprint_auth_readiness(config) == []


def test_claude_planner_aborts_the_sprint_before_dispatch(tmp_path: Path) -> None:
    """Seam test: the planner-only case reaches the same launch abort."""
    path = _write_credentials(tmp_path, _REVOKED_CREDENTIAL)
    config = _all_api_config(tmp_path, plan=PlanConfig(enabled=True, cli="claude"))
    resolved = _make_resolved(tmp_path, ("story-a",))

    with (
        patch("theforge.config.auth.claude_credentials_path", return_value=path),
        patch("theforge.sprint.runner._run_baseline_gate") as mock_baseline,
        patch("theforge.sprint.runner.run_task") as mock_run_task,
    ):
        with pytest.raises(SprintAuthUnavailable) as exc_info:
            run_sprint(config, resolved)

    assert "plan" in str(exc_info.value)
    assert not mock_run_task.called
    assert not mock_baseline.called


def test_gate_is_silent_when_no_credential_evidence(tmp_path: Path) -> None:
    config = _make_config(tmp_path)

    with patch(
        "theforge.config.auth.claude_credentials_path",
        return_value=tmp_path / "absent.json",
    ):
        assert check_sprint_auth_readiness(config) == []
        enforce_sprint_auth_readiness(config)


def test_revoked_credential_aborts_sprint_before_story_dispatch(tmp_path: Path) -> None:
    """The whole point: seconds, no dispatch, no verdict, credential named."""
    path = _write_credentials(tmp_path, _REVOKED_CREDENTIAL)
    config = _make_config(tmp_path)
    resolved = _make_resolved(tmp_path, ("story-a", "story-b"))

    started = time.monotonic()
    with (
        patch("theforge.config.auth.claude_credentials_path", return_value=path),
        patch("theforge.sprint.runner._run_baseline_gate") as mock_baseline,
        patch("theforge.sprint.runner.run_batch_preflight") as mock_preflight,
        patch("theforge.sprint.runner.run_task") as mock_run_task,
        patch("theforge.sprint.runner._write_sprint_audit") as mock_audit,
        patch("theforge.sprint.runner._write_story_audit") as mock_story_audit,
    ):
        with pytest.raises(SprintAuthUnavailable) as exc_info:
            run_sprint(config, resolved)
    elapsed = time.monotonic() - started

    message = str(exc_info.value)
    # Operator-facing message names the credential as the cause...
    assert str(path) in message
    assert "credential" in message.lower()
    # ...and says plainly that no story was judged.
    assert "none was marked failed" in message

    # No story work of any kind was dispatched, and nothing expensive ran.
    assert not mock_run_task.called
    assert not mock_preflight.called
    assert not mock_baseline.called
    # No story acquired a verdict: no story audit, no sprint audit.
    assert not mock_story_audit.called
    assert not mock_audit.called
    # Seconds, not minutes.
    assert elapsed < 10.0


def test_healthy_credential_lets_the_sprint_run(tmp_path: Path) -> None:
    """The gate must not become a new way for healthy sprints to fail."""
    path = _write_credentials(
        tmp_path,
        {
            "claudeAiOauth": {
                "accessToken": "live-token",
                "refreshToken": "live-refresh",
                "expiresAt": int(_now_ms() + 3_600_000),
                "refreshTokenExpiresAt": int(_now_ms() + 604_800_000),
            }
        },
    )
    config = _make_config(tmp_path)
    resolved = _make_resolved(tmp_path, ("story-a",))

    with (
        patch("theforge.config.auth.claude_credentials_path", return_value=path),
        patch(
            "theforge.sprint.runner._run_baseline_gate",
            return_value={"passed": True, "message": "ok"},
        ),
        patch("theforge.sprint.runner.run_task", return_value=_ok_result()) as mock_run_task,
        patch("theforge.sprint.runner._write_sprint_audit"),
        patch("theforge.sprint.runner._write_sprint_summary"),
    ):
        result = run_sprint(config, resolved)

    assert mock_run_task.called
    assert result.specs_succeeded == 1


# ── post-launch circuit breaker ──────────────────────────────────────────


def _ok_result() -> CoordinatorResult:
    state = CoordinatorState()
    state.preflight_result = MagicMock(cost_usd=0.0)
    return CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="ok")


def _infra_abort_result(category: str) -> CoordinatorResult:
    """A run that ended with no model output, in *category*."""
    state = CoordinatorState()
    state.preflight_result = MagicMock(cost_usd=0.0)
    state.phase = Phase.ESCALATE
    state.error = "401 OAuth access token has been revoked"
    failure = AgentInvocationFailure(
        phase="PREFLIGHT",
        category=category,
        detail="401 OAuth access token has been revoked",
        profile_name="dev",
    )
    mark_infrastructure_abort(state, failure, message=state.error)
    return CoordinatorResult(
        success=False,
        phase=Phase.ESCALATE,
        state=state,
        message=state.error,
        infrastructure_failure=True,
    )


def _run_two_story_sprint(
    tmp_path: Path,
    first_result: CoordinatorResult,
) -> tuple[object, list[str]]:
    """Run a 2-story sprint whose first story returns *first_result*."""
    config = _make_config(tmp_path)
    resolved = _make_resolved(tmp_path, ("story-a", "story-b"))
    dispatched: list[str] = []

    def _fake_run_task(_config, task, **_kwargs):
        dispatched.append(task.slug)
        return first_result if len(dispatched) == 1 else _ok_result()

    with (
        patch(
            "theforge.config.auth.claude_credentials_path",
            return_value=tmp_path / "absent.json",
        ),
        patch(
            "theforge.sprint.runner._run_baseline_gate",
            return_value={"passed": True, "message": "ok"},
        ),
        patch("theforge.sprint.runner.run_task", side_effect=_fake_run_task),
        patch("theforge.sprint.runner._write_sprint_audit"),
        patch("theforge.sprint.runner._write_sprint_summary"),
        patch("theforge.sprint.runner._write_story_audit"),
    ):
        result = run_sprint(config, resolved)
    return result, dispatched


def test_fatal_auth_failure_stops_the_rest_of_the_sprint(tmp_path: Path) -> None:
    """One revoked-credential story is the whole answer for every later story."""
    result, dispatched = _run_two_story_sprint(tmp_path, _infra_abort_result(CATEGORY_AUTH))

    # Only the story that discovered the failure ran.
    assert dispatched == ["story-a"]
    assert result.specs_skipped == 1
    assert result.stopped_reason is not None
    assert "authentication" in result.stopped_reason.lower()


def test_transport_failure_does_not_trip_the_breaker(tmp_path: Path) -> None:
    """A transport drop can succeed next time; retrying there is real resilience."""
    _result, dispatched = _run_two_story_sprint(tmp_path, _infra_abort_result(CATEGORY_TRANSPORT))

    assert dispatched == ["story-a", "story-b"]


def test_ordinary_story_failure_does_not_trip_the_breaker(tmp_path: Path) -> None:
    """A real ESCALATE is a statement about the story, not about the substrate."""
    state = CoordinatorState()
    state.preflight_result = MagicMock(cost_usd=0.0)
    state.phase = Phase.ESCALATE
    escalated = CoordinatorResult(
        success=False,
        phase=Phase.ESCALATE,
        state=state,
        message="story needs human judgment",
    )

    _result, dispatched = _run_two_story_sprint(tmp_path, escalated)

    assert dispatched == ["story-a", "story-b"]


# ── parallel: a sibling already in flight when the breaker trips ─────────


def _cancelled_result() -> CoordinatorResult:
    """What coordinator/engine.py returns for a story killed by stop_event.

    Deliberately built to that module's shape — a plain ESCALATE tagged
    ``StoryCancelled``, with no infrastructure markers — because the point of
    the test is that the sprint scheduler must re-attribute it rather than
    trust it.
    """
    state = CoordinatorState()
    state.preflight_result = MagicMock(cost_usd=0.0)
    state.phase = Phase.ESCALATE
    state.error = "Story cancelled by sprint timeout"
    state.error_type = "StoryCancelled"
    return CoordinatorResult(
        success=False,
        phase=Phase.ESCALATE,
        state=state,
        message="Story cancelled by sprint timeout",
    )


def test_inflight_sibling_cancelled_by_breaker_is_not_a_story_failure(
    tmp_path: Path,
) -> None:
    """The #1951 bug class, at parallelism > 1.

    story-b is genuinely in flight — dispatched, running, and blocked — when
    story-a's revoked credential trips the breaker. It comes back through the
    generic stop_event cancellation path, whose result is shaped for a worker
    *timeout*. Left alone that reads as a story failure. It must not: no model
    judged story-b, so a substrate outage would be presented as a property of
    the work.
    """
    config = _make_config(tmp_path)
    resolved = replace(_make_resolved(tmp_path, ("story-a", "story-b")), max_parallel=2)

    b_dispatched = threading.Event()
    a_may_finish = threading.Event()
    outcomes: dict[str, CoordinatorResult] = {}

    def _fake_run_task(_config, task, **kwargs):
        stop_event = kwargs.get("stop_event")
        if task.slug == "story-b":
            # Genuinely in flight: block until the breaker cancels us, exactly
            # as a worker parked in a provider call would.
            b_dispatched.set()
            assert stop_event is not None
            assert stop_event.wait(timeout=30), "breaker never cancelled the in-flight sibling"
            outcomes["story-b"] = _cancelled_result()
            return outcomes["story-b"]
        # story-a discovers the revoked credential, but only after story-b is
        # confirmed running — otherwise the breaker would trip before dispatch
        # and this would silently degrade into the already-covered SKIP path.
        assert b_dispatched.wait(timeout=30), "story-b never dispatched"
        a_may_finish.set()
        outcomes["story-a"] = _infra_abort_result(CATEGORY_AUTH)
        return outcomes["story-a"]

    with (
        patch(
            "theforge.config.auth.claude_credentials_path",
            return_value=tmp_path / "absent.json",
        ),
        patch(
            "theforge.sprint.runner._run_baseline_gate",
            return_value={"passed": True, "message": "ok"},
        ),
        patch("theforge.sprint.runner.run_task", side_effect=_fake_run_task),
        patch("theforge.sprint.runner._write_sprint_audit"),
        patch("theforge.sprint.runner._write_sprint_summary"),
        patch("theforge.sprint.runner._write_story_audit"),
    ):
        result = run_sprint(config, resolved)

    # The sibling really was in flight and really was cancelled by us.
    assert a_may_finish.is_set()
    assert "story-b" in outcomes

    # It is NOT a story failure. story-a (which did meet the substrate) keeps
    # its own #1951 handling; story-b was never judged at all.
    story_b = next(r for slug, r in result.results if slug.startswith("story-b"))
    assert story_b.infrastructure_failure is True
    assert story_b.state.error_type == ERROR_TYPE_INFRASTRUCTURE_ABORT
    # The taint marker #1951 relies on to keep this out of adaptive memory.
    assert story_b.state.infrastructure_failure["category"] == CATEGORY_AUTH
    assert is_infrastructure_abort(story_b.state)
    # And the credential is named as the cause, not the story.
    assert "credential" in story_b.state.error.lower()

    assert result.stopped_reason is not None
    assert "authentication" in result.stopped_reason.lower()
