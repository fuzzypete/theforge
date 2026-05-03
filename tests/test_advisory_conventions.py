from __future__ import annotations

import dataclasses
import datetime as dt
import subprocess
from unittest.mock import patch

import yaml
from coord_test_helpers import _make_config

from theforge.advisory_conventions import load_advisory_summary, update_advisory_violations
from theforge.config import AdvisoryConventionsConfig, AdvisoryIssueFilingConfig


def _violation(
    *,
    file: str = "src/theforge/assignment.py",
    rule: str = "max_module_lines",
    line_count: int = 965,
    limit: int = 600,
) -> dict[str, object]:
    return {
        "rule": rule,
        "file": file,
        "detail": f"{file} has {line_count} lines (limit {limit})",
        "blocking": False,
    }


def test_update_advisory_violations_aggregates_and_prunes(tmp_path):
    config = _make_config(tmp_path)
    observed_at = dt.datetime(2026, 5, 2, 18, 45, tzinfo=dt.timezone.utc)

    result = update_advisory_violations(
        config,
        [_violation()],
        observed_at=observed_at,
        run_id="run-1",
        story_slug="story-1",
    )

    assert result["entry_count"] == 1
    artifact = yaml.safe_load(
        (tmp_path / ".forge" / "conventions" / "advisory.yaml").read_text(encoding="utf-8")
    )
    entry = next(iter(artifact["entries"].values()))
    assert entry["line_count"] == 965
    assert entry["limit"] == 600
    assert entry["gap"] == 365
    assert entry["first_seen"] == "2026-05-02T18:45:00Z"
    assert entry["last_seen"] == "2026-05-02T18:45:00Z"

    update_advisory_violations(
        config,
        [],
        observed_at=observed_at + dt.timedelta(hours=1),
        run_id="run-2",
        story_slug="story-2",
    )
    pruned = load_advisory_summary(config)
    assert pruned["entries"] == {}


def test_update_advisory_violations_writes_shared_mirror_when_enabled(tmp_path):
    config = dataclasses.replace(
        _make_config(tmp_path),
        conventions_advisory=AdvisoryConventionsConfig(
            commit_shared_artifact=True,
            shared_artifact_path="docs/advisory-conventions.yaml",
        ),
    )
    observed_at = dt.datetime(2026, 5, 2, 18, 45, tzinfo=dt.timezone.utc)

    update_advisory_violations(
        config,
        [_violation(file="src/theforge/cli/status.py", line_count=832)],
        observed_at=observed_at,
        run_id="run-3",
        story_slug="story-3",
    )

    shared_path = tmp_path / "docs" / "advisory-conventions.yaml"
    assert shared_path.exists()
    shared = yaml.safe_load(shared_path.read_text(encoding="utf-8"))
    assert len(shared["entries"]) == 1


def test_update_advisory_violations_files_issue_once_when_enabled(tmp_path):
    config = dataclasses.replace(
        _make_config(tmp_path),
        conventions_advisory=AdvisoryConventionsConfig(
            issue_filing=AdvisoryIssueFilingConfig(
                enabled=True,
                threshold_percent=25.0,
                label="refactor-debt",
                milestone="v0.12.0",
            )
        ),
    )
    observed_at = dt.datetime(2026, 5, 2, 18, 45, tzinfo=dt.timezone.utc)
    gh_result = subprocess.CompletedProcess(
        args=["gh"],
        returncode=0,
        stdout="https://github.com/example/repo/issues/123\n",
        stderr="",
    )

    with patch("theforge.advisory_conventions.subprocess.run", return_value=gh_result) as mock_run:
        update_advisory_violations(
            config,
            [_violation()],
            observed_at=observed_at,
            run_id="run-4",
            story_slug="story-4",
        )
        update_advisory_violations(
            config,
            [_violation(line_count=970)],
            observed_at=observed_at + dt.timedelta(hours=1),
            run_id="run-5",
            story_slug="story-5",
        )

    assert mock_run.call_count == 1
    artifact = load_advisory_summary(config)
    entry = next(iter(artifact["entries"].values()))
    assert entry["issue"]["url"] == "https://github.com/example/repo/issues/123"
    assert entry["issue"]["label"] == "refactor-debt"
    assert entry["issue"]["milestone"] == "v0.12.0"


def test_update_advisory_violations_disabled_filing_does_not_report_prior_issue_as_newly_filed(
    tmp_path,
):
    # First run: filing enabled, issue gets created.
    config_enabled = dataclasses.replace(
        _make_config(tmp_path),
        conventions_advisory=AdvisoryConventionsConfig(
            issue_filing=AdvisoryIssueFilingConfig(
                enabled=True,
                threshold_percent=25.0,
                label="refactor-debt",
            )
        ),
    )
    observed_at = dt.datetime(2026, 5, 2, 18, 45, tzinfo=dt.timezone.utc)
    gh_result = subprocess.CompletedProcess(
        args=["gh"],
        returncode=0,
        stdout="https://github.com/example/repo/issues/99\n",
        stderr="",
    )
    with patch("theforge.advisory_conventions.subprocess.run", return_value=gh_result):
        result_run1 = update_advisory_violations(
            config_enabled,
            [_violation()],
            observed_at=observed_at,
            run_id="run-a",
            story_slug="story-a",
        )
    assert len(result_run1["newly_filed_issues"]) == 1

    # Second and third runs: filing disabled. Prior issue block must be preserved
    # in the artifact but must NOT appear in newly_filed_issues.
    config_disabled = dataclasses.replace(
        _make_config(tmp_path),
        conventions_advisory=AdvisoryConventionsConfig(
            issue_filing=AdvisoryIssueFilingConfig(enabled=False)
        ),
    )
    for run_id in ("run-b", "run-c"):
        result = update_advisory_violations(
            config_disabled,
            [_violation(line_count=970)],
            observed_at=observed_at + dt.timedelta(hours=1),
            run_id=run_id,
            story_slug="story-b",
        )
        assert result["newly_filed_issues"] == [], f"{run_id}: expected no newly-filed issues"
        artifact = load_advisory_summary(config_disabled)
        entry = next(iter(artifact["entries"].values()))
        assert entry["issue"]["url"] == "https://github.com/example/repo/issues/99", (
            f"{run_id}: prior issue block must be preserved in artifact"
        )
