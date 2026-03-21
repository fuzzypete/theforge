"""Tests for cmd_telemetry — aggregation over history.jsonl records."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from theforge.cli import cmd_telemetry

# ── Helpers ───────────────────────────────────────────────────────────


def _make_record(
    slug: str = "test-slug",
    started_at: str = "2026-03-01T10:00:00+00:00",
    final_phase: str = "DONE",
    total_cost: float = 5.88,
    duration_s: float = 1371.0,
    dev_iterations: int = 1,
    review_cycles: int = 1,
    phases: dict | None = None,
) -> dict:
    """Build a minimal history.jsonl record."""
    default_phases = {
        "preflight": {"cost_usd": 0.11, "duration_s": 31.0, "outcome": "proceed"},
        "plan": {"cost_usd": 0.21, "duration_s": 78.0, "outcome": "success"},
        "plan_review": {
            "cost_usd": 0.53,
            "duration_s": 73.0,
            "iterations": 1,
            "outcome": "approve",
        },
        "dev": {"cost_usd": 3.26, "duration_s": 969.0, "iterations": 1, "outcome": "success"},
        "validate": {"cost_usd": 0.00, "duration_s": 12.0, "outcome": "pass"},
        "review": {
            "cost_usd": 1.77,
            "duration_s": 208.0,
            "cycles": 1,
            "outcome": "approve",
            "per_reviewer": {
                "claude-reviewer": {"cost": 0.50, "verdict": "APPROVE"},
                "codex-reviewer": {"cost": 0.45, "verdict": "APPROVE"},
            },
        },
    }
    return {
        "task": {"slug": slug, "name": slug},
        "outcome": {"success": True, "final_phase": final_phase},
        "timing": {
            "started_at": started_at,
            "finished_at": "2026-03-01T10:22:51+00:00",
            "duration_seconds": duration_s,
        },
        "cost": {"total_usd": total_cost},
        "iterations": {"dev_iterations": dev_iterations, "review_cycles": review_cycles},
        "phases": phases if phases is not None else default_phases,
        "totals": {
            "cost_usd": total_cost,
            "duration_s": duration_s,
            "dev_iterations": dev_iterations,
            "review_cycles": review_cycles,
        },
    }


def _write_history(history_path: Path, records: list[dict]) -> None:
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with open(history_path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _make_args(
    forge_yaml: Path, since: str | None = None, phase: str | None = None
) -> SimpleNamespace:
    return SimpleNamespace(config=str(forge_yaml), since=since, phase=phase)


def _setup_project(tmp_path: Path) -> tuple[Path, Path]:
    """Create a minimal forge project. Returns (project_root, history_path)."""
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text("project: test\n", encoding="utf-8")
    history_path = tmp_path / ".forge" / "audits" / "history.jsonl"
    return tmp_path, history_path


# ── Tests ─────────────────────────────────────────────────────────────


class TestTelemetryBasic:
    def test_returns_zero_with_valid_history(self, tmp_path: Path, capsys) -> None:
        """cmd_telemetry exits 0 with valid records."""
        _, history_path = _setup_project(tmp_path)
        _write_history(history_path, [_make_record()])
        args = _make_args(tmp_path / "forge.yaml")
        rc = cmd_telemetry(args)
        assert rc == 0

    def test_no_config_returns_error(self, tmp_path: Path) -> None:
        """Returns 1 when forge.yaml cannot be found."""
        args = _make_args(tmp_path / "nonexistent.yaml")
        rc = cmd_telemetry(args)
        assert rc == 1

    def test_missing_history_returns_error(self, tmp_path: Path) -> None:
        """Returns 1 when history.jsonl does not exist."""
        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project: test\n", encoding="utf-8")
        args = _make_args(forge_yaml)
        rc = cmd_telemetry(args)
        assert rc == 1

    def test_run_table_shows_slug(self, tmp_path: Path, capsys) -> None:
        """Run table includes the slug from the record."""
        _, history_path = _setup_project(tmp_path)
        _write_history(history_path, [_make_record(slug="my-feature")])
        args = _make_args(tmp_path / "forge.yaml")
        cmd_telemetry(args)
        out = capsys.readouterr().out
        assert "my-feature" in out

    def test_phase_breakdown_shown(self, tmp_path: Path, capsys) -> None:
        """Phase breakdown section is printed."""
        _, history_path = _setup_project(tmp_path)
        _write_history(history_path, [_make_record()])
        args = _make_args(tmp_path / "forge.yaml")
        cmd_telemetry(args)
        out = capsys.readouterr().out
        assert "Phase breakdown" in out

    def test_phase_labels_in_output(self, tmp_path: Path, capsys) -> None:
        """DEV and REVIEW labels appear in the phase breakdown."""
        _, history_path = _setup_project(tmp_path)
        _write_history(history_path, [_make_record()])
        args = _make_args(tmp_path / "forge.yaml")
        cmd_telemetry(args)
        out = capsys.readouterr().out
        assert "DEV" in out
        assert "REVIEW" in out


class TestTelemetrySinceFilter:
    def test_since_excludes_old_records(self, tmp_path: Path, capsys) -> None:
        """--since filter excludes records before the given date."""
        _, history_path = _setup_project(tmp_path)
        old = _make_record(slug="old-run", started_at="2026-01-01T10:00:00+00:00")
        new = _make_record(slug="new-run", started_at="2026-03-15T10:00:00+00:00")
        _write_history(history_path, [old, new])
        args = _make_args(tmp_path / "forge.yaml", since="2026-03-01")
        cmd_telemetry(args)
        out = capsys.readouterr().out
        assert "new-run" in out
        assert "old-run" not in out

    def test_since_includes_matching_records(self, tmp_path: Path, capsys) -> None:
        """--since includes records on or after the given date."""
        _, history_path = _setup_project(tmp_path)
        rec = _make_record(slug="on-date", started_at="2026-03-01T00:00:00+00:00")
        _write_history(history_path, [rec])
        args = _make_args(tmp_path / "forge.yaml", since="2026-03-01")
        cmd_telemetry(args)
        out = capsys.readouterr().out
        assert "on-date" in out

    def test_invalid_since_returns_error(self, tmp_path: Path) -> None:
        """Invalid --since value returns exit code 1."""
        _, history_path = _setup_project(tmp_path)
        _write_history(history_path, [_make_record()])
        args = _make_args(tmp_path / "forge.yaml", since="not-a-date")
        rc = cmd_telemetry(args)
        assert rc == 1

    def test_all_filtered_returns_zero_with_message(self, tmp_path: Path, capsys) -> None:
        """When --since filters all records, returns 0 with empty message."""
        _, history_path = _setup_project(tmp_path)
        old = _make_record(started_at="2026-01-01T10:00:00+00:00")
        _write_history(history_path, [old])
        args = _make_args(tmp_path / "forge.yaml", since="2026-12-01")
        rc = cmd_telemetry(args)
        assert rc == 0


class TestTelemetryPhaseFilter:
    def test_phase_filter_shows_single_phase(self, tmp_path: Path, capsys) -> None:
        """--phase filter shows only the specified phase label."""
        _, history_path = _setup_project(tmp_path)
        _write_history(history_path, [_make_record()])
        args = _make_args(tmp_path / "forge.yaml", phase="dev")
        cmd_telemetry(args)
        out = capsys.readouterr().out
        assert "DEV" in out
        assert "PREFLIGHT" not in out
        assert "REVIEW" not in out

    def test_phase_filter_suppresses_run_table(self, tmp_path: Path, capsys) -> None:
        """--phase filter suppresses the run table (no slug in output)."""
        _, history_path = _setup_project(tmp_path)
        _write_history(history_path, [_make_record(slug="my-slug")])
        args = _make_args(tmp_path / "forge.yaml", phase="dev")
        cmd_telemetry(args)
        out = capsys.readouterr().out
        assert "my-slug" not in out


class TestTelemetryGracefulHandling:
    def test_records_without_phases_key_are_skipped(self, tmp_path: Path, capsys) -> None:
        """Records without a phases key are skipped gracefully (old format)."""
        _, history_path = _setup_project(tmp_path)
        old_format = {
            "task": {"slug": "legacy-run"},
            "outcome": {"success": True, "final_phase": "DONE"},
            "timing": {"started_at": "2026-01-01T10:00:00+00:00", "duration_seconds": 100.0},
            "cost": {"total_usd": 1.0},
            "iterations": {"dev_iterations": 1, "review_cycles": 1},
            # no phases key
        }
        _write_history(history_path, [old_format])
        args = _make_args(tmp_path / "forge.yaml")
        rc = cmd_telemetry(args)
        assert rc == 0
        out = capsys.readouterr().out
        # Run table still shows the slug
        assert "legacy-run" in out
        # Phase breakdown reports no phase data
        assert "No phase telemetry data found" in out

    def test_mixed_records_aggregate_only_phase_records(self, tmp_path: Path, capsys) -> None:
        """Phase averages use only records that have phase data."""
        _, history_path = _setup_project(tmp_path)
        old_format = {
            "task": {"slug": "legacy"},
            "outcome": {"success": True, "final_phase": "DONE"},
            "timing": {"started_at": "2026-03-01T10:00:00+00:00", "duration_seconds": 100.0},
            "cost": {"total_usd": 1.0},
            "iterations": {"dev_iterations": 1, "review_cycles": 1},
        }
        new_record = _make_record(slug="new-run")
        _write_history(history_path, [old_format, new_record])
        args = _make_args(tmp_path / "forge.yaml")
        rc = cmd_telemetry(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "1 run(s) with phase data" in out

    def test_malformed_jsonl_lines_skipped(self, tmp_path: Path, capsys) -> None:
        """Malformed JSONL lines are skipped without error."""
        _, history_path = _setup_project(tmp_path)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with open(history_path, "w", encoding="utf-8") as f:
            f.write("{not valid json}\n")
            f.write(json.dumps(_make_record(slug="good-run")) + "\n")
        args = _make_args(tmp_path / "forge.yaml")
        rc = cmd_telemetry(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "good-run" in out


class TestTelemetryAggregation:
    def test_phase_averages_correct(self, tmp_path: Path, capsys) -> None:
        """Phase breakdown shows average cost across multiple records."""
        _, history_path = _setup_project(tmp_path)
        rec1 = _make_record(
            phases={
                "preflight": None,
                "plan": None,
                "plan_review": None,
                "dev": {
                    "cost_usd": 2.0,
                    "duration_s": 100.0,
                    "iterations": 1,
                    "outcome": "success",
                },
                "validate": None,
                "review": None,
            },
            total_cost=2.0,
        )
        rec2 = _make_record(
            phases={
                "preflight": None,
                "plan": None,
                "plan_review": None,
                "dev": {
                    "cost_usd": 4.0,
                    "duration_s": 200.0,
                    "iterations": 1,
                    "outcome": "success",
                },
                "validate": None,
                "review": None,
            },
            total_cost=4.0,
        )
        _write_history(history_path, [rec1, rec2])
        args = _make_args(tmp_path / "forge.yaml", phase="dev")
        cmd_telemetry(args)
        out = capsys.readouterr().out
        # Average cost should be $3.00
        assert "$3.00" in out
