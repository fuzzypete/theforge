"""Tests for ``forge knowledge-report`` — window bounds over the audit substrate."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from tests.knowledge_effectiveness_test_helpers import record
from theforge.cli.knowledge_report import cmd_knowledge_report
from theforge.coordinator import audit_storage


def _record(run_id: str, *, started_at: str, included: bool = True) -> dict:
    """One seeded run, in or out of the with-prior cohort."""
    return record(run_id, cohort="with" if included else "without", started_at=started_at)


def _args(project_root: Path, **overrides: object) -> SimpleNamespace:
    base = {
        "config": str(project_root / "forge.yaml"),
        "since": None,
        "until": None,
        "recent_run_count": None,
        "format": "json",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _project(tmp_path: Path, records: list[dict]) -> Path:
    (tmp_path / "forge.yaml").write_text("project: test\n", encoding="utf-8")
    audit_storage.seed_records(tmp_path, records)
    return tmp_path


def _payload(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


class TestWindowBounds:
    def test_recent_run_count_bounds_the_window(self, tmp_path: Path, capsys) -> None:
        records = [
            _record(f"run-{i}", started_at=f"2026-08-{i + 1:02d}T10:00:00+00:00") for i in range(5)
        ]
        root = _project(tmp_path, records)

        assert cmd_knowledge_report(_args(root)) == 0
        assert _payload(capsys)["window"]["records_considered"] == 5

        assert cmd_knowledge_report(_args(root, recent_run_count=2)) == 0
        payload = _payload(capsys)
        assert payload["window"]["records_considered"] == 2
        assert payload["window"]["recent_run_count"] == 2

    def test_since_and_until_bound_the_window(self, tmp_path: Path, capsys) -> None:
        records = [
            _record(f"run-{i}", started_at=f"2026-08-{i + 1:02d}T10:00:00+00:00") for i in range(5)
        ]
        root = _project(tmp_path, records)

        assert cmd_knowledge_report(_args(root, since="2026-08-03")) == 0
        assert _payload(capsys)["window"]["records_considered"] == 3

        assert cmd_knowledge_report(_args(root, since="2026-08-02", until="2026-08-04")) == 0
        payload = _payload(capsys)
        assert payload["window"]["records_considered"] == 3
        assert payload["window"]["since"] == "2026-08-02"
        assert payload["window"]["until"] == "2026-08-04"

    def test_invalid_since_is_a_clean_failure(self, tmp_path: Path, capsys) -> None:
        root = _project(tmp_path, [_record("run-0", started_at="2026-08-01T10:00:00+00:00")])

        assert cmd_knowledge_report(_args(root, since="last-tuesday")) == 1
        assert "Invalid --since" in capsys.readouterr().err

    def test_zero_recent_run_count_is_rejected(self, tmp_path: Path, capsys) -> None:
        root = _project(tmp_path, [_record("run-0", started_at="2026-08-01T10:00:00+00:00")])

        assert cmd_knowledge_report(_args(root, recent_run_count=0)) == 1
        assert "--recent-run-count" in capsys.readouterr().err


class TestOutput:
    def test_cohorts_are_reported_from_the_manifests(self, tmp_path: Path, capsys) -> None:
        records = [
            _record(f"with-{i}", started_at=f"2026-08-0{i + 1}T10:00:00+00:00", included=True)
            for i in range(3)
        ] + [
            _record(f"without-{i}", started_at=f"2026-08-0{i + 4}T10:00:00+00:00", included=False)
            for i in range(3)
        ]
        root = _project(tmp_path, records)

        assert cmd_knowledge_report(_args(root)) == 0
        payload = _payload(capsys)

        assert payload["cohorts"]["with_prior_summary"] == 3
        assert payload["cohorts"]["without_prior_summary"] == 3
        assert payload["status"] == "insufficient_data"
        assert payload["matched_comparison"][0]["comparative_claim_supported"] is False
        assert "unreachable" in payload["status_reason"]
        assert payload["matched_buckets"][0]["with_prior_runs"] == 3

    def test_terminal_format_renders_the_verdict(self, tmp_path: Path, capsys) -> None:
        root = _project(tmp_path, [_record("run-0", started_at="2026-08-01T10:00:00+00:00")])

        assert cmd_knowledge_report(_args(root, format="terminal")) == 0
        out = capsys.readouterr().out
        assert "Knowledge-loop effectiveness" in out
        assert "insufficient_data" in out

    def test_yaml_format_is_accepted(self, tmp_path: Path, capsys) -> None:
        import yaml

        root = _project(tmp_path, [_record("run-0", started_at="2026-08-01T10:00:00+00:00")])

        assert cmd_knowledge_report(_args(root, format="yaml")) == 0
        assert yaml.safe_load(capsys.readouterr().out)["status"] == "insufficient_data"


class TestFailureModes:
    def test_missing_config_returns_error(self, tmp_path: Path, capsys) -> None:
        args = _args(tmp_path / "nowhere")
        args.config = str(tmp_path / "nowhere" / "forge.yaml")

        assert cmd_knowledge_report(args) == 1
        assert "forge.yaml not found" in capsys.readouterr().err

    def test_missing_substrate_returns_error(self, tmp_path: Path, capsys) -> None:
        (tmp_path / "forge.yaml").write_text("project: test\n", encoding="utf-8")

        assert cmd_knowledge_report(_args(tmp_path)) == 1
        assert "No audit history found" in capsys.readouterr().err
