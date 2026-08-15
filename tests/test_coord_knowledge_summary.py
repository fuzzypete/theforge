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

from pathlib import Path

import pytest
import yaml
from coord_test_helpers import _make_task

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    KnowledgeConfig,
    LogConfig,
    ModelRef,
    PlanConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator import knowledge_summary_flow
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.knowledge_summary import summary_path

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


def _make_config(project_root: Path, *, run_summaries: bool = True) -> ForgeConfig:
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
                model="claude-sonnet-4-5",
                budget_usd=0.5,
                timeout_seconds=300,
                provider="anthropic",
            ),
        ),
        knowledge=KnowledgeConfig(run_summaries=run_summaries),
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

        path = knowledge_summary_flow.maybe_generate_run_summary(config, _done_result(), _audit())

        assert path == summary_path(tmp_path, RUN_ID)
        artifact = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert artifact["run_id"] == RUN_ID
        assert artifact["authoritative_run_record"] == f".forge/audits/runs/{RUN_ID}.json"
        assert artifact["what_was_learned"][0]["evidence"][0]["path"] == "src/client.py"
        assert artifact["changed_files"] == ["src/client.py"]
        assert artifact["generation"]["cost_usd"] == 0.12
        assert len(calls) == 1

    def test_dispatch_is_tool_free_over_an_api_transport(
        self, tmp_path: Path, calls: list[dict]
    ) -> None:
        """An empty allowlist is only *narrow* on the API path — pin the path."""
        knowledge_summary_flow.maybe_generate_run_summary(
            _make_config(tmp_path), _done_result(), _audit()
        )

        profile = calls[0]["profile"]
        assert profile.mode == "api"
        assert profile.allowed_tools == ()

    def test_a_cli_plan_model_without_an_api_fallback_is_not_dispatched(
        self, tmp_path: Path, calls: list[dict]
    ) -> None:
        from dataclasses import replace

        config = _make_config(tmp_path)
        config = replace(
            config,
            plan=replace(config.plan, ref=replace(config.plan.ref, cli="claude", provider=None)),
        )

        assert (
            knowledge_summary_flow.maybe_generate_run_summary(config, _done_result(), _audit())
            is None
        )
        assert calls == []
        assert not summary_path(tmp_path, RUN_ID).exists()


class TestGate:
    def test_disabled_config_invokes_no_agent_and_writes_nothing(
        self, tmp_path: Path, calls: list[dict]
    ) -> None:
        config = _make_config(tmp_path, run_summaries=False)

        assert (
            knowledge_summary_flow.maybe_generate_run_summary(config, _done_result(), _audit())
            is None
        )
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

        assert (
            knowledge_summary_flow.maybe_generate_run_summary(
                _make_config(tmp_path), escalated, _audit()
            )
            is None
        )
        assert calls == []

    def test_an_already_summarised_run_is_not_billed_again(
        self, tmp_path: Path, calls: list[dict]
    ) -> None:
        config = _make_config(tmp_path)

        first = knowledge_summary_flow.maybe_generate_run_summary(config, _done_result(), _audit())
        second = knowledge_summary_flow.maybe_generate_run_summary(
            config, _done_result(), _audit()
        )

        assert first is not None
        assert second is None
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

        assert (
            knowledge_summary_flow.maybe_generate_run_summary(
                _make_config(tmp_path), _done_result(), _audit()
            )
            is None
        )
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

        assert (
            knowledge_summary_flow.maybe_generate_run_summary(
                _make_config(tmp_path), _done_result(), _audit()
            )
            is None
        )
        assert not summary_path(tmp_path, RUN_ID).exists()

    def test_an_unwritable_summary_dir_does_not_break_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, calls: list[dict]
    ) -> None:
        def _boom(*_args: object, **_kwargs: object) -> None:
            raise OSError("read-only file system")

        monkeypatch.setattr(knowledge_summary_flow, "write_summary", _boom)

        assert (
            knowledge_summary_flow.maybe_generate_run_summary(
                _make_config(tmp_path), _done_result(), _audit()
            )
            is None
        )


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
        assert (tmp_path / ".forge" / "audits" / "runs" / f"{RUN_ID}.json").exists()
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
        assert (tmp_path / ".forge" / "audits" / "runs" / f"{RUN_ID}.json").exists()
        assert not summary_path(tmp_path, RUN_ID).exists()
