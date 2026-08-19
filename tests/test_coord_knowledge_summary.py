"""Seam tests for issue #1859: summary generation as a post-DONE side effect.

These exercise the boundary the unit tests in ``test_knowledge_summary.py`` do
not: the coordinator control flow that decides whether to spend money at all,
and the two terminal audit writers it hangs off.

What is pinned here:

- the config gate: disabled means **no agent invocation**, not a discarded result
- non-load-bearing failure: an agent that raises, fails, or emits garbage leaves
  the run's outcome and its audit trail exactly as they were
- exactly-once: a sprint calls ``_write_story_audit`` repeatedly for the same
  finished story, and the summary agent must be dispatched once
- containment: dispatch only happens on a tool-free API transport, because an
  empty allowlist on a CLI profile grants the CLI's *unrestricted* default tools
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from coord_test_helpers import _make_task

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    AgentDef,
    ForgeConfig,
    KnowledgeConfig,
    LogConfig,
    ModelRef,
    PlanConfig,
    RetryPolicy,
    TransportFallbackConfig,
    WorkspaceConfig,
    transport_for,
)
from theforge.config.model_identity import DEFAULT_PHASE_ELIGIBILITY, PHASE_KNOWLEDGE_SUMMARY
from theforge.config.models import AGENT_REGISTRY
from theforge.coordinator import knowledge_summary_flow
from theforge.coordinator.audit_substrate import (
    CURRENT_RECORD_SCHEMA_VERSION,
    MIGRATION_HELPERS,
)
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.knowledge_summary import summary_path
from theforge.sprint.status_reader import read_completed_status

RUN_ID = "run-abc123"

VALID_OUTPUT = """\
run_summary:
  run_id: run-abc123
  what_changed:
    description: Added retry logic with exponential backoff.
    approach: Wrapped call sites in a retry decorator.
  what_was_learned:
    - claim: Timeout handling needs both a connect and a read timeout.
      evidence:
        - type: file
          path: src/client.py
          description: the retry path this run rewrote
  learned_patterns:
    - retry-decorator
  review_insights:
    - reviewers kept returning to the retry path
  complexity_signal:
    dominant_difficulty: edge case coverage
"""

UNEVIDENCED_OUTPUT = """\
run_summary:
  run_id: run-abc123
  what_changed:
    description: Added retry logic.
  what_was_learned:
    - claim: This codebase always prefers decorators for cross-cutting concerns.
      evidence: []
"""


class _FakeAgentResult:
    def __init__(self, output: str = VALID_OUTPUT, success: bool = True) -> None:
        self.success = success
        self.output = output
        self.cost_usd = 0.12
        self.model_used = "claude-sonnet-4-5"
        self.transport_used = "api"


def _make_config(
    project_root: Path,
    *,
    run_summaries: bool = True,
    provider: str = "anthropic",
    model: str | None = None,
    transport_fallbacks: dict[str, TransportFallbackConfig] | None = None,
) -> ForgeConfig:
    """A config whose plan model dispatches over an API transport.

    The API transport is the containment property under test, not incidental
    fixture detail: ``runners.api`` serves an empty tool allowlist as a single
    stateless call, which is the only dispatch path that is mechanically
    tool-free.
    """
    return ForgeConfig(
        project="test",
        project_root=project_root,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern=".forge/worktrees/{slug}",
            branch_pattern="forge/{slug}",
            base_branch="main",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        preflight_fallback_profile=None,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        log=LogConfig(enabled=False),
        plan=PlanConfig(
            enabled=True,
            ref=ModelRef(
                model=model or ("claude-sonnet-4-5" if provider == "anthropic" else "gpt-5.4"),
                budget_usd=0.5,
                timeout_seconds=300,
                provider=provider,
            ),
        ),
        knowledge=KnowledgeConfig(run_summaries=run_summaries),
        transport_fallbacks=transport_fallbacks or {},
    )


def _audit() -> dict:
    return {
        "run_id": RUN_ID,
        "task": {"name": "Retry the client", "slug": "retry-client", "github_issue": 42},
        "timing": {"finished_at": "2026-08-15T12:00:00+00:00"},
        "preflight": {"work_type": "feature", "complexity": "medium", "domains": ["backend"]},
        "iterations": {"dev_iterations_productive": 2, "review_cycles_total": 1},
        "cost": {"total_usd": 4.25},
        "changed_files": {
            "base_ref": "aaa111",
            "head_ref": "bbb222",
            "files": [{"path": "src/client.py", "insertions": 40, "deletions": 3}],
        },
        "reviews": [{"cycle": 1, "verdict": "APPROVE", "summary": "good"}],
        "finding_registry": [
            {
                "finding_id": "f-003",
                "cycle_first_seen": 1,
                "cycle_last_seen": 1,
                "file": "src/client.py",
                "severity": "P1",
                "description": "missing read timeout on retry path",
                "disposition": "resolved",
            }
        ],
        "phases": {"plan": {"plan_structured": {"steps": [{"id": 1, "description": "retry"}]}}},
    }


def _done_result() -> CoordinatorResult:
    state = CoordinatorState()
    state.run_id = RUN_ID
    state.started_at = "2026-08-15T11:00:00+00:00"
    # The seam tests build their audit through the real ``generate_audit_log``,
    # so the run needs something citable on the state itself — a run with no
    # anchors is correctly skipped and would test nothing.
    state.changed_files = {
        "base_ref": "aaa111",
        "head_ref": "bbb222",
        "files": [{"path": "src/client.py", "insertions": 40, "deletions": 3}],
    }
    return CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="done")


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Record every summary-agent dispatch instead of making one."""
    recorded: list[dict] = []

    def _fake_run_agent(**kwargs: object) -> _FakeAgentResult:
        recorded.append(kwargs)
        return _FakeAgentResult()

    monkeypatch.setattr(knowledge_summary_flow, "run_agent", _fake_run_agent)
    return recorded


class TestGeneration:
    def test_a_completed_run_persists_an_evidence_backed_summary(
        self, tmp_path: Path, calls: list[dict]
    ) -> None:
        config = _make_config(tmp_path)
        audit = _audit()

        outcome = knowledge_summary_flow.maybe_generate_run_summary(config, _done_result(), audit)

        path = outcome.path
        assert path == summary_path(tmp_path, RUN_ID)
        artifact = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert artifact["run_id"] == RUN_ID
        assert artifact["authoritative_run_record"] == f".forge/audits/runs/{RUN_ID}.json"
        assert artifact["what_was_learned"][0]["evidence"][0]["path"] == "src/client.py"
        assert artifact["changed_files"] == ["src/client.py"]
        assert artifact["generation"]["cost_usd"] == 0.12
        assert audit["knowledge_summary"]["status"] == "written"
        assert audit["knowledge_summary"]["written"] is True
        assert len(calls) == 1

    def test_dispatch_is_tool_free_over_an_api_transport(
        self, tmp_path: Path, calls: list[dict]
    ) -> None:
        """An empty allowlist is only *narrow* on the API path — pin the path."""
        outcome = knowledge_summary_flow.maybe_generate_run_summary(
            _make_config(tmp_path), _done_result(), _audit()
        )

        profile = calls[0]["profile"]
        assert profile.mode == "api"
        assert profile.allowed_tools == ()
        assert calls[0]["plain_text"] is True
        assert outcome.status == "written"

    def test_phase_eligibility_can_move_summary_to_another_api_candidate(
        self, tmp_path: Path, calls: list[dict]
    ) -> None:
        excluded = replace(
            AGENT_REGISTRY["openai/gpt-5.4/api"],
            routing=replace(
                AGENT_REGISTRY["openai/gpt-5.4/api"].routing,
                phase_eligibility=frozenset(DEFAULT_PHASE_ELIGIBILITY - {PHASE_KNOWLEDGE_SUMMARY}),
            ),
        )
        eligible = AGENT_REGISTRY["google/gemini-2.5-pro/api"]
        config = replace(
            _make_config(tmp_path, provider="openai", model="gpt-5.4"),
            agents=[
                AgentDef(
                    name="openai-gpt-5-4",
                    provider="openai",
                    model="gpt-5.4",
                    budget_usd=1.0,
                    timeout_seconds=120,
                    tier="mid",
                    transport=transport_for("openai", "api"),
                ),
                AgentDef(
                    name="google-gemini-2-5-pro",
                    provider="google",
                    model="gemini-2.5-pro",
                    budget_usd=1.0,
                    timeout_seconds=120,
                    tier="mid",
                    transport=transport_for("google", "api"),
                ),
            ],
            model_registry={
                "openai/gpt-5.4/api": excluded,
                "google/gemini-2.5-pro/api": eligible,
            },
        )

        outcome = knowledge_summary_flow.maybe_generate_run_summary(
            config, _done_result(), _audit()
        )

        assert outcome.status == "written"
        assert calls[0]["profile"].model == "gemini-2.5-pro"
        assert calls[0]["profile"].name == "knowledge_summary"
        assert calls[0]["profile"].budget_usd == 0.5
        assert calls[0]["profile"].timeout_seconds == 300
        assert calls[0]["profile"].phase == PHASE_KNOWLEDGE_SUMMARY

    def test_provider_level_transport_fallback_keeps_cli_pool_summary_dispatchable(
        self, tmp_path: Path, calls: list[dict]
    ) -> None:
        fallback = TransportFallbackConfig(provider="openai", model="gpt-5.4-mini")
        config = replace(
            _make_config(
                tmp_path,
                provider="openai",
                model="gpt-5.4",
                transport_fallbacks={"openai": fallback},
            ),
            agents=[
                AgentDef(
                    name="openai-gpt-5-4",
                    provider=None,
                    cli="codex",
                    model="gpt-5.4",
                    budget_usd=3.0,
                    timeout_seconds=900,
                    tier="mid",
                    transport=transport_for("openai", "cli"),
                ),
            ],
        )

        outcome = knowledge_summary_flow.maybe_generate_run_summary(
            config, _done_result(), _audit()
        )

        assert outcome.status == "written"
        profile = calls[0]["profile"]
        assert profile.mode == "api"
        assert profile.model == "gpt-5.4-mini"
        assert profile.name == "knowledge_summary"
        assert profile.budget_usd == 0.5
        assert profile.timeout_seconds == 300

    def test_matching_pool_model_keeps_plan_derived_summary_limits(
        self, tmp_path: Path, calls: list[dict]
    ) -> None:
        config = replace(
            _make_config(tmp_path, provider="openai", model="gpt-5.4"),
            agents=[
                AgentDef(
                    name="openai-gpt-5-4",
                    provider="openai",
                    model="gpt-5.4",
                    budget_usd=3.0,
                    timeout_seconds=900,
                    tier="mid",
                    transport=transport_for("openai", "api"),
                ),
            ],
        )

        outcome = knowledge_summary_flow.maybe_generate_run_summary(
            config, _done_result(), _audit()
        )

        assert outcome.status == "written"
        profile = calls[0]["profile"]
        assert profile.model == "gpt-5.4"
        assert profile.name == "knowledge_summary"
        assert profile.budget_usd == 0.5
        assert profile.timeout_seconds == 300

    def test_summary_dispatch_uses_plain_text_through_the_real_runner_api_seam(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exercise coordinator -> run_agent -> run_api_agent without a fake seam."""
        from theforge import runners as runner_exports
        from theforge.runners.cli import run_agent as real_run_agent

        config = _make_config(tmp_path, provider="openai", model="gpt-5.4")
        loop_runner = MagicMock()
        single_shot_runner = MagicMock(return_value=_FakeAgentResult())

        monkeypatch.setattr(runner_exports, "run_agent", real_run_agent)
        monkeypatch.setattr(knowledge_summary_flow, "run_agent", None)
        with (
            patch.dict("theforge.runners.api._LOOP_RUNNERS", {"openai": loop_runner}),
            patch.dict("theforge.runners.api.PROVIDER_RUNNERS", {"openai": single_shot_runner}),
        ):
            outcome = knowledge_summary_flow.maybe_generate_run_summary(
                config, _done_result(), _audit()
            )

        path = outcome.path
        assert path == summary_path(tmp_path, RUN_ID)
        loop_runner.assert_not_called()
        prompt, profile, secrets = single_shot_runner.call_args.args
        assert profile.allowed_tools == ()
        assert secrets == config.secrets
        assert "run_summary:" in prompt
        assert single_shot_runner.call_args.kwargs == {"plain_text": True}

    def test_a_cli_plan_model_without_an_api_fallback_is_not_dispatched(
        self, tmp_path: Path, calls: list[dict]
    ) -> None:
        from dataclasses import replace

        config = _make_config(tmp_path)
        config = replace(
            config,
            plan=replace(config.plan, ref=replace(config.plan.ref, cli="claude", provider=None)),
        )

        outcome = knowledge_summary_flow.maybe_generate_run_summary(
            config, _done_result(), _audit()
        )
        assert outcome.status == "skipped"
        assert outcome.attempted is True
        assert calls == []
        assert not summary_path(tmp_path, RUN_ID).exists()

    def test_phase_eligible_matching_cli_pool_model_can_use_plan_ref_api_fallback(
        self, tmp_path: Path, calls: list[dict]
    ) -> None:
        fallback = TransportFallbackConfig(provider="openai", model="gpt-5.4-mini")
        config = _make_config(tmp_path, provider="openai", model="gpt-5.4")
        config = replace(
            config,
            plan=replace(
                config.plan,
                ref=replace(
                    config.plan.ref,
                    cli="codex",
                    provider=None,
                    transport=transport_for("openai", "cli"),
                    api_fallback=fallback,
                ),
            ),
            agents=[
                AgentDef(
                    name="openai-gpt-5-4",
                    provider=None,
                    cli="codex",
                    model="gpt-5.4",
                    budget_usd=3.0,
                    timeout_seconds=900,
                    tier="mid",
                    transport=transport_for("openai", "cli"),
                ),
            ],
        )

        outcome = knowledge_summary_flow.maybe_generate_run_summary(
            config, _done_result(), _audit()
        )

        assert outcome.status == "written"
        profile = calls[0]["profile"]
        assert profile.mode == "api"
        assert profile.model == "gpt-5.4-mini"
        assert profile.name == "knowledge_summary"
        assert profile.budget_usd == 0.5
        assert profile.timeout_seconds == 300


class TestGate:
    def test_disabled_config_invokes_no_agent_and_writes_nothing(
        self, tmp_path: Path, calls: list[dict]
    ) -> None:
        config = _make_config(tmp_path, run_summaries=False)

        outcome = knowledge_summary_flow.maybe_generate_run_summary(
            config, _done_result(), _audit()
        )
        assert outcome.status == "not_attempted"
        assert outcome.reason == "disabled"
        assert calls == []
        assert not summary_path(tmp_path, RUN_ID).exists()

    def test_a_run_that_did_not_reach_done_is_not_summarised(
        self, tmp_path: Path, calls: list[dict]
    ) -> None:
        state = CoordinatorState()
        state.run_id = RUN_ID
        escalated = CoordinatorResult(
            success=False, phase=Phase.ESCALATE, state=state, message="escalated"
        )

        outcome = knowledge_summary_flow.maybe_generate_run_summary(
            _make_config(tmp_path), escalated, _audit()
        )
        assert outcome.status == "not_attempted"
        assert outcome.reason == "run_not_done"
        assert calls == []

    def test_an_already_summarised_run_is_not_billed_again(
        self, tmp_path: Path, calls: list[dict]
    ) -> None:
        config = _make_config(tmp_path)

        first = knowledge_summary_flow.maybe_generate_run_summary(config, _done_result(), _audit())
        second = knowledge_summary_flow.maybe_generate_run_summary(
            config, _done_result(), _audit()
        )

        assert first.status == "written"
        assert second.status == "not_attempted"
        assert second.reason == "already_exists"
        assert len(calls) == 1


class TestNonLoadBearing:
    def test_an_unevidenced_claim_is_rejected_and_nothing_is_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            knowledge_summary_flow,
            "run_agent",
            lambda **_: _FakeAgentResult(output=UNEVIDENCED_OUTPUT),
        )

        outcome = knowledge_summary_flow.maybe_generate_run_summary(
            _make_config(tmp_path), _done_result(), _audit()
        )
        assert outcome.status == "rejected"
        assert outcome.attempted is True
        assert not summary_path(tmp_path, RUN_ID).exists()

    @pytest.mark.parametrize(
        "agent",
        [
            pytest.param(lambda **_: _FakeAgentResult(output="I give up"), id="unparseable"),
            pytest.param(
                lambda **_: _FakeAgentResult(success=False, output=""), id="agent-failure"
            ),
            pytest.param(
                lambda **_: (_ for _ in ()).throw(RuntimeError("transport exploded")),
                id="raises",
            ),
        ],
    )
    def test_a_failing_summary_agent_never_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: object
    ) -> None:
        monkeypatch.setattr(knowledge_summary_flow, "run_agent", agent)

        outcome = knowledge_summary_flow.maybe_generate_run_summary(
            _make_config(tmp_path), _done_result(), _audit()
        )
        assert outcome.attempted is True
        assert outcome.written is False
        assert not summary_path(tmp_path, RUN_ID).exists()

    def test_an_unwritable_summary_dir_does_not_break_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, calls: list[dict]
    ) -> None:
        def _boom(*_args: object, **_kwargs: object) -> None:
            raise OSError("read-only file system")

        monkeypatch.setattr(knowledge_summary_flow, "write_summary", _boom)

        outcome = knowledge_summary_flow.maybe_generate_run_summary(
            _make_config(tmp_path), _done_result(), _audit()
        )
        assert outcome.status == "failed"
        assert outcome.reason == "read-only file system"


class TestTerminalWriterSeams:
    """The audit writers stay the load-bearing path; summaries hang off them."""

    def test_cli_audit_writer_produces_a_summary_and_the_audit(
        self, tmp_path: Path, calls: list[dict]
    ) -> None:
        from theforge.cli import shared

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        result = _done_result()

        audit_path = shared._write_audit(result, config, task)

        assert audit_path.exists()
        assert summary_path(tmp_path, RUN_ID).exists()
        run_record = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
        assert run_record["knowledge_summary"]["status"] == "written"
        record = (tmp_path / ".forge" / "audits" / "runs" / f"{RUN_ID}.json").read_text()
        assert '"knowledge_summary"' in record
        assert len(calls) == 1

    def test_repeated_story_audit_writes_bill_one_summary(
        self, tmp_path: Path, calls: list[dict]
    ) -> None:
        """A sprint writes the same finished story's audit at several seams."""
        from theforge.sprint.audit import _write_story_audit

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        result = _done_result()
        result.state.log_dir = tmp_path / ".forge" / "logs" / "sprint" / "retry-client"

        for _ in range(3):
            _write_story_audit(config, task, result)

        assert summary_path(tmp_path, RUN_ID).exists()
        assert len(calls) == 1
        audit = yaml.safe_load((result.state.log_dir / "audit.yaml").read_text(encoding="utf-8"))
        assert audit["knowledge_summary"]["status"] == "written"
        assert audit["knowledge_summary"]["written"] is True

        run_record = json.loads(
            (tmp_path / ".forge" / "audits" / "runs" / f"{RUN_ID}.json").read_text(
                encoding="utf-8"
            )
        )
        assert run_record["knowledge_summary"]["status"] == "written"
        assert run_record["knowledge_summary"]["written"] is True

    def test_sprint_writes_a_canonical_run_record_before_summary_generation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from theforge.sprint.audit import _write_story_audit

        seen: dict[str, object] = {}

        def _fake_run_agent(**_kwargs: object) -> _FakeAgentResult:
            run_file = tmp_path / ".forge" / "audits" / "runs" / f"{RUN_ID}.json"
            seen["exists"] = run_file.exists()
            if run_file.exists():
                seen["record"] = json.loads(run_file.read_text(encoding="utf-8"))
            return _FakeAgentResult()

        monkeypatch.setattr(knowledge_summary_flow, "run_agent", _fake_run_agent)
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        result = _done_result()
        result.state.log_dir = tmp_path / ".forge" / "logs" / "sprint" / "retry-client"

        _write_story_audit(config, task, result)

        assert seen["exists"] is True
        persisted = seen["record"]
        assert isinstance(persisted, dict)
        assert "knowledge_summary" not in persisted

        run_record = json.loads(
            (tmp_path / ".forge" / "audits" / "runs" / f"{RUN_ID}.json").read_text(
                encoding="utf-8"
            )
        )
        assert run_record["knowledge_summary"]["status"] == "written"

    def test_a_summary_failure_leaves_the_audit_trail_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from theforge.cli import shared

        monkeypatch.setattr(
            knowledge_summary_flow,
            "run_agent",
            lambda **_: (_ for _ in ()).throw(RuntimeError("transport exploded")),
        )
        config = _make_config(tmp_path)

        audit_path = shared._write_audit(_done_result(), config, _make_task(tmp_path))

        assert audit_path.exists()
        audit = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
        assert audit["knowledge_summary"]["attempted"] is True
        assert audit["knowledge_summary"]["written"] is False
        assert audit["knowledge_summary"]["status"] == "failed"
        assert (tmp_path / ".forge" / "audits" / "runs" / f"{RUN_ID}.json").exists()
        assert not summary_path(tmp_path, RUN_ID).exists()

    def test_failed_summary_generation_surfaces_in_completed_sprint_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from theforge.sprint.audit import _write_story_audit

        monkeypatch.setattr(
            knowledge_summary_flow,
            "run_agent",
            lambda **_: _FakeAgentResult(output="prose without rooted block"),
        )
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        result = _done_result()
        result.state.log_dir = tmp_path / ".forge" / "logs" / "sprint" / "retry-client"

        _write_story_audit(config, task, result)
        summary_pathname = tmp_path / ".forge" / "logs" / "sprint" / "sprint-summary.yaml"
        summary_pathname.write_text(
            yaml.safe_dump(
                {
                    "sprint": {"run_id": "run-sprint", "name": "sprint"},
                    "stories": [
                        {
                            "slug": "retry-client",
                            "path": "Issue #42",
                            "outcome": "DONE",
                            "cost_usd": 1.0,
                            "story_run_id": RUN_ID,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        entries = read_completed_status(summary_pathname)

        assert len(entries) == 1
        assert "knowledge summary rejected" in entries[0].detail
        assert "no rooted 'run_summary:' block" in entries[0].detail

    def test_rejected_sprint_summary_is_persisted_to_canonical_run_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from theforge.sprint.audit import _write_story_audit

        monkeypatch.setattr(
            knowledge_summary_flow,
            "run_agent",
            lambda **_: _FakeAgentResult(output="prose without rooted block"),
        )
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        result = _done_result()
        result.state.log_dir = tmp_path / ".forge" / "logs" / "sprint" / "retry-client"

        _write_story_audit(config, task, result)

        run_record = json.loads(
            (tmp_path / ".forge" / "audits" / "runs" / f"{RUN_ID}.json").read_text(
                encoding="utf-8"
            )
        )
        assert run_record["knowledge_summary"]["status"] == "rejected"
        assert run_record["knowledge_summary"]["attempted"] is True
        assert run_record["knowledge_summary"]["written"] is False

    def test_rejected_sprint_summary_is_not_redispatched_on_later_story_audit_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from theforge.sprint.audit import _write_story_audit

        calls = 0

        def _rejecting_agent(**_kwargs: object) -> _FakeAgentResult:
            nonlocal calls
            calls += 1
            return _FakeAgentResult(output="prose without rooted block")

        monkeypatch.setattr(knowledge_summary_flow, "run_agent", _rejecting_agent)
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        result = _done_result()
        result.state.log_dir = tmp_path / ".forge" / "logs" / "sprint" / "retry-client"

        for _ in range(3):
            _write_story_audit(config, task, result)

        assert calls == 1
        audit = yaml.safe_load((result.state.log_dir / "audit.yaml").read_text(encoding="utf-8"))
        assert audit["knowledge_summary"]["status"] == "rejected"
        assert audit["knowledge_summary"]["written"] is False

        run_record = json.loads(
            (tmp_path / ".forge" / "audits" / "runs" / f"{RUN_ID}.json").read_text(
                encoding="utf-8"
            )
        )
        assert run_record["knowledge_summary"]["status"] == "rejected"
        assert run_record["knowledge_summary"]["written"] is False


def test_audit_schema_version_exposes_knowledge_summary_status() -> None:
    # v30 is where knowledge_summary entered the record. Pinning equality made
    # this test fail on every later bump for reasons that have nothing to do
    # with knowledge summaries; what it means to assert is that the field's
    # version has been reached and its migration is registered, both of which
    # survive later fields being added (#2525 bumped to v31).
    assert CURRENT_RECORD_SCHEMA_VERSION >= 30
    assert 29 in MIGRATION_HELPERS
