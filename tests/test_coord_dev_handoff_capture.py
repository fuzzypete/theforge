"""Integration tests for the forge handoff capture → write → read flow.

Covers:
- dev_phase writes forge artifact when AgentResult.dev_handoff is populated
- dev_phase falls back to file read when dev_handoff is None
- validate_phase reads from forge artifact when present
- audit snapshot carries correct source field
- review_context helpers prefer forge artifact
"""

from __future__ import annotations

from pathlib import Path

import yaml

from theforge.agent_types import AgentResult
from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.dev_phase import _capture_dev_handoff
from theforge.coordinator.review_context import (
    _get_handoff_content,
    _get_raw_dev_notes,
    _latest_forge_handoff_path,
)
from theforge.coordinator.state import CoordinatorState
from theforge.task import TaskStory

# ── Fixtures ──────────────────────────────────────────────────────────


def _make_config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        log=LogConfig(enabled=False),
    )


def _make_task(tmp_path: Path, slug: str = "test-task") -> TaskStory:
    spec = tmp_path / "spec.md"
    spec.write_text("# Test\n\nDo the thing.", encoding="utf-8")
    return TaskStory(name="Test Task", story_path=spec, slug=slug)


def _sample_handoff_dict() -> dict:
    return {
        "summary": "Implemented the feature end-to-end.",
        "commits": [{"sha": "abc1234", "message": "feat: add feature"}],
        "acceptance_criteria": [
            {"criterion": "Feature works", "status": "MET", "notes": "tested"}
        ],
        "story_deviations": "none",
        "deferred_items": "none",
    }


def _make_agent_result_with_handoff(handoff: dict | None = None) -> AgentResult:
    return AgentResult(
        success=True,
        output="Work complete.",
        session_id="sess-1",
        cost_usd=0.50,
        exit_code=0,
        raw={},
        dev_handoff=handoff,
    )


# ── dev_phase: forge artifact writing ────────────────────────────────


class TestDevPhaseForgeArtifact:
    """_capture_dev_handoff writes/reads handoff based on AgentResult.dev_handoff."""

    def test_forge_artifact_written_when_dev_handoff_present(self, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()
        state = CoordinatorState()
        state.dev_iteration = 1
        handoff = _sample_handoff_dict()
        dev_result = _make_agent_result_with_handoff(handoff)

        artifact_path = _capture_dev_handoff(state, config, task, workspace, dev_result)

        assert artifact_path is not None
        assert artifact_path.exists()
        written = yaml.safe_load(artifact_path.read_text())
        assert written["summary"] == "Implemented the feature end-to-end."
        assert written["commits"][0]["sha"] == "abc1234"

    def test_forge_artifact_path_is_deterministic(self, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path, slug="my-feature")
        workspace = tmp_path / task.slug
        workspace.mkdir()
        state = CoordinatorState()
        state.dev_iteration = 2
        dev_result = _make_agent_result_with_handoff(_sample_handoff_dict())

        artifact_path = _capture_dev_handoff(state, config, task, workspace, dev_result)

        expected = tmp_path / ".forge" / "handoffs" / "my-feature" / "iter_2.yaml"
        assert artifact_path == expected

    def test_snapshot_source_is_structured_output_when_handoff_present(self, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()
        state = CoordinatorState()
        state.dev_iteration = 1
        dev_result = _make_agent_result_with_handoff(_sample_handoff_dict())

        _capture_dev_handoff(state, config, task, workspace, dev_result)

        snap = state.dev_handoff_snapshots[-1]
        assert snap["source"] == "structured_output"
        assert snap["path"] is not None
        assert snap["handoff"]["summary"] == "Implemented the feature end-to-end."

    def test_snapshot_source_is_missing_when_dev_handoff_absent(self, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()
        state = CoordinatorState()
        state.dev_iteration = 1
        dev_result = _make_agent_result_with_handoff(None)  # no structured output

        _capture_dev_handoff(state, config, task, workspace, dev_result)

        snap = state.dev_handoff_snapshots[-1]
        assert snap["source"] == "missing"
        assert snap["path"] is None

    def test_snapshot_source_is_missing_when_no_handoff_anywhere(self, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()
        # No handoff file written
        state = CoordinatorState()
        state.dev_iteration = 1
        dev_result = _make_agent_result_with_handoff(None)

        _capture_dev_handoff(state, config, task, workspace, dev_result)

        snap = state.dev_handoff_snapshots[-1]
        assert snap["source"] == "missing"

    def test_returns_none_when_no_structured_output(self, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()
        state = CoordinatorState()
        state.dev_iteration = 1
        dev_result = _make_agent_result_with_handoff(None)

        result = _capture_dev_handoff(state, config, task, workspace, dev_result)

        assert result is None


# ── _latest_forge_handoff_path ────────────────────────────────────────


class TestLatestForgeHandoffPath:
    def test_returns_none_when_no_snapshots(self, tmp_path):
        state = CoordinatorState()
        assert _latest_forge_handoff_path(state) is None

    def test_returns_none_when_source_is_file(self, tmp_path):
        state = CoordinatorState()
        state.dev_handoff_snapshots.append({"source": "file", "path": None, "handoff": {}})
        assert _latest_forge_handoff_path(state) is None

    def test_returns_path_when_source_is_structured_output_and_file_exists(self, tmp_path):
        p = tmp_path / "handoff.yaml"
        p.write_text("summary: test\n", encoding="utf-8")
        state = CoordinatorState()
        state.dev_handoff_snapshots.append(
            {"source": "structured_output", "path": str(p), "handoff": {}}
        )
        result = _latest_forge_handoff_path(state)
        assert result == p

    def test_returns_none_when_path_does_not_exist(self, tmp_path):
        state = CoordinatorState()
        state.dev_handoff_snapshots.append(
            {
                "source": "structured_output",
                "path": str(tmp_path / "nonexistent.yaml"),
                "handoff": {},
            }
        )
        assert _latest_forge_handoff_path(state) is None


# ── validate_phase: forge artifact preferred ──────────────────────────


class TestReviewContextForgePreference:
    """_get_handoff_content and _get_raw_dev_notes prefer forge artifact."""

    def _write_forge_artifact(self, tmp_path: Path, content: dict) -> Path:
        p = tmp_path / "forge_handoff.yaml"
        p.write_text(yaml.dump(content), encoding="utf-8")
        return p

    def test_get_handoff_content_returns_forge_artifact(self, tmp_path):
        forge_path = self._write_forge_artifact(tmp_path, _sample_handoff_dict())

        content = _get_handoff_content(forge_handoff_path=forge_path)

        assert "Captured from agent structured output" in content
        assert "Implemented the feature" in content

    def test_get_handoff_content_absent_when_no_forge(self, tmp_path):
        content = _get_handoff_content(forge_handoff_path=None)

        assert "no forge handoff artifact" in content

    def test_get_handoff_content_absent_when_forge_path_missing(self, tmp_path):
        nonexistent = tmp_path / "does_not_exist.yaml"

        content = _get_handoff_content(forge_handoff_path=nonexistent)

        assert "no forge handoff artifact" in content

    def test_get_raw_dev_notes_returns_yaml_from_forge_artifact(self, tmp_path):
        forge_path = self._write_forge_artifact(tmp_path, _sample_handoff_dict())

        raw = _get_raw_dev_notes(forge_handoff_path=forge_path)

        assert raw is not None
        parsed = yaml.safe_load(raw)
        assert parsed["summary"] == "Implemented the feature end-to-end."

    def test_get_raw_dev_notes_returns_none_when_no_forge(self, tmp_path):
        raw = _get_raw_dev_notes(forge_handoff_path=None)

        assert raw is None
