"""Tests for per-run audit file write in cli/shared.py (_write_per_run_record)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import _make_config, _make_task

from theforge.cli.shared import _write_audit
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase


def _make_result(tmp_path: Path, run_id: str | None = "run-test-001") -> CoordinatorResult:
    state = CoordinatorState()
    state.run_id = run_id
    return CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="done")


class TestPerRunFileWrite:
    def test_per_run_file_created(self, tmp_path: Path) -> None:
        """_write_audit creates .forge/audits/runs/{run_id}.json."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        result = _make_result(tmp_path, run_id="run-abc-001")

        _write_audit(result, config, task)

        run_file = tmp_path / ".forge" / "audits" / "runs" / "run-abc-001.json"
        assert run_file.exists(), "per-run JSON file should have been written"

    def test_per_run_file_is_valid_json(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        result = _make_result(tmp_path, run_id="run-valid-json")

        _write_audit(result, config, task)

        run_file = tmp_path / ".forge" / "audits" / "runs" / "run-valid-json.json"
        with open(run_file) as f:
            data = json.load(f)
        assert isinstance(data, dict)

    def test_per_run_file_has_required_envelope_fields(self, tmp_path: Path) -> None:
        """schema_version, run_id, and parent_run_id must be present."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        result = _make_result(tmp_path, run_id="run-envelope-001")

        _write_audit(result, config, task)

        run_file = tmp_path / ".forge" / "audits" / "runs" / "run-envelope-001.json"
        data = json.loads(run_file.read_text())

        assert "schema_version" in data
        # New per-run records are written at schema_version=4 (#1253 added
        # task.fix_ready/readiness_warnings); pre-slice records read as 1.
        assert data["schema_version"] == 4
        assert "run_id" in data
        assert data["run_id"] == "run-envelope-001"
        assert "parent_run_id" in data
        assert data["parent_run_id"] is None
        assert "forge_version" in data

    def test_per_run_file_not_written_when_run_id_is_none(self, tmp_path: Path) -> None:
        """When run_id is None, the per-run file should not be written."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        result = _make_result(tmp_path, run_id=None)

        _write_audit(result, config, task)

        runs_dir = tmp_path / ".forge" / "audits" / "runs"
        # Either the directory doesn't exist or contains no files
        if runs_dir.exists():
            assert list(runs_dir.iterdir()) == []

    def test_history_jsonl_no_longer_written(self, tmp_path: Path) -> None:
        """Substrate is the canonical write path; legacy history.jsonl is gone."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        result = _make_result(tmp_path, run_id="run-substrate-only")

        _write_audit(result, config, task)

        history_path = tmp_path / ".forge" / "audits" / "history.jsonl"
        assert not history_path.exists(), (
            "history.jsonl must NOT be written — substrate is the canonical write path"
        )

    def test_two_separate_run_ids_produce_distinct_files(self, tmp_path: Path) -> None:
        """Two runs with different run_ids must produce two distinct files."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)

        result1 = _make_result(tmp_path, run_id="run-first-001")
        result2 = _make_result(tmp_path, run_id="run-second-002")

        _write_audit(result1, config, task)
        _write_audit(result2, config, task)

        runs_dir = tmp_path / ".forge" / "audits" / "runs"
        files = sorted(f.name for f in runs_dir.iterdir())
        assert "run-first-001.json" in files
        assert "run-second-002.json" in files
        assert len(files) == 2

    def test_per_run_file_not_overwritten_on_second_call(self, tmp_path: Path) -> None:
        """Immutability: a second write with the same run_id must not overwrite."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        result = _make_result(tmp_path, run_id="run-immutable-001")

        _write_audit(result, config, task)
        run_file = tmp_path / ".forge" / "audits" / "runs" / "run-immutable-001.json"
        mtime1 = run_file.stat().st_mtime

        # Simulate a second call (e.g. accidental re-run with same id)
        import time

        time.sleep(0.01)
        _write_audit(result, config, task)
        mtime2 = run_file.stat().st_mtime

        assert mtime1 == mtime2, "per-run file must not be modified after initial write"

    def test_redaction_applied_to_secret_key(self, tmp_path: Path) -> None:
        """Values under secret-shaped keys must be redacted in the per-run file."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        result = _make_result(tmp_path, run_id="run-redact-001")

        fake_audit = {
            "forge_version": "0.0.0",
            "credentials": {"token": "super-secret-value"},
        }
        with patch("theforge.cli.shared.generate_audit_log", return_value=fake_audit):
            _write_audit(result, config, task)

        run_file = tmp_path / ".forge" / "audits" / "runs" / "run-redact-001.json"
        data = json.loads(run_file.read_text())
        assert data["credentials"]["token"] == "[REDACTED]"

    def test_redaction_applied_from_env_file(self, tmp_path: Path) -> None:
        """Values present in .forge/.env must be redacted from the per-run file."""
        env_dir = tmp_path / ".forge"
        env_dir.mkdir(parents=True, exist_ok=True)
        (env_dir / ".env").write_text("MY_PRIVATE_KEY=private_key_value_here\n")

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        result = _make_result(tmp_path, run_id="run-env-redact-001")

        fake_audit = {
            "forge_version": "0.0.0",
            "output": "using key private_key_value_here in request",
        }
        with patch("theforge.cli.shared.generate_audit_log", return_value=fake_audit):
            _write_audit(result, config, task)

        run_file = tmp_path / ".forge" / "audits" / "runs" / "run-env-redact-001.json"
        data = json.loads(run_file.read_text())
        assert data["output"] == "using key [REDACTED] in request"
