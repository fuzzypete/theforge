"""Tests for the observed-run evidence collector behind ``forge report``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from theforge.reporting import evidence as ev


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _record(
    run_id: str,
    *,
    slug: str,
    sprint_name: str | None = "issues-320,324",
    forge_version: str = "0.14.2",
    configuration: dict | None = None,
    story_text: str = "## Problem\n\nresume false-skips.\n",
    github_issue: int | None = 320,
) -> dict:
    record: dict = {
        "run_id": run_id,
        "forge_version": forge_version,
        "sprint_name": sprint_name,
        "sprint_id": "5ff0",
        "task": {"slug": slug, "story_text": story_text, "github_issue": github_issue},
        "cost": {
            "agents": [
                {
                    "role": "dev",
                    "profile": "dev_profile",
                    "ledger": {
                        "version": 1,
                        "profile": "dev_profile",
                        "configured_identity": {
                            "raw": "opus",
                            "identity": "anthropic-opus-cli",
                            "source": "config",
                            "resolution": "alias",
                        },
                        "resolved_primary_identity": {
                            "raw": "anthropic-opus-cli",
                            "identity": "anthropic-opus-cli",
                            "source": "catalog",
                            "resolution": "exact",
                        },
                    },
                }
            ]
        },
    }
    if configuration is not None:
        record["configuration"] = configuration
    return record


def _observing_project(
    tmp_path: Path,
    *,
    sprint_name: str = "issues-320,324",
    sprint_run_id: str = "f5aa21cf2d8d",
    stories: tuple[tuple[str, str], ...] = (("issue-320", "aaa111"), ("issue-324", "bbb222")),
    configuration: dict | None = None,
) -> Path:
    root = tmp_path / "hdp"
    forge = root / ".forge"
    _write(
        root / ".git" / "config",
        '[remote "origin"]\n\turl = git@github.com:fuzzypete/hdp.git\n',
    )
    logs = forge / "logs" / sprint_name
    _write(logs / f"run-{sprint_run_id}.log", "sprint run log body\n")
    _write(
        logs / f"run-{sprint_run_id}-summary.yaml",
        yaml.safe_dump(
            {
                "sprint": {"name": sprint_name, "sprint_id": "5ff0", "run_id": sprint_run_id},
                "stories": [
                    {"slug": slug, "story_run_id": run_id, "outcome": "DONE"}
                    for slug, run_id in stories
                ],
            }
        ),
    )
    _write(forge / "audits" / f"run-{sprint_run_id}-sprint-audit.yaml", "sprint: {}\n")
    for slug, run_id in stories:
        _write(
            forge / "audits" / "runs" / f"{run_id}.json",
            json.dumps(
                _record(
                    run_id,
                    slug=slug,
                    sprint_name=sprint_name,
                    configuration=configuration,
                    github_issue=int(slug.split("-")[1]),
                )
            ),
        )
        _write(logs / slug / "audit.yaml", f"story: {slug}\n")
        _write(logs / slug / f"run-{run_id}.log", f"{slug} run log\n")
        _write(logs / slug / "review-cycle-1" / "synthesized.yaml", "verdict: APPROVE\n")
    return root


def test_story_run_id_collects_every_evidence_kind(tmp_path):
    root = _observing_project(tmp_path, configuration={"resolved_sha256": "abc123def456"})

    result = ev.collect_run_evidence(root, "aaa111")

    assert result.run_kind == "story"
    assert result.forge_version == "0.14.2"
    assert result.observed_project == "fuzzypete/hdp"
    assert result.story_run_ids == ("aaa111",)
    kinds = {a.kind for a in result.artifacts}
    assert ev.KIND_RUN_LOG in kinds
    assert ev.KIND_STORY_AUDIT in kinds
    assert ev.KIND_SPRINT_STATE in kinds
    assert ev.KIND_REVIEWER_OUTPUTS in kinds
    assert ev.KIND_STORY_BODY in kinds
    assert ev.KIND_RUNTIME_IDENTITY in kinds
    assert ev.KIND_RESOLVED_CONFIG in kinds


def test_sprint_run_id_reaches_every_story_record(tmp_path):
    root = _observing_project(tmp_path, configuration={"resolved_sha256": "abc123def456"})

    result = ev.collect_run_evidence(root, "f5aa21cf2d8d")

    assert result.run_kind == "sprint"
    assert result.story_run_ids == ("aaa111", "bbb222")
    assert result.story_slugs == ("issue-320", "issue-324")
    audits = {a.name for a in result.artifacts if a.kind == ev.KIND_STORY_AUDIT}
    assert len(audits) == 2
    bodies = {a.kind for a in result.artifacts}
    assert ev.KIND_RUN_RECORD in bodies


def test_runtime_identity_and_config_come_from_the_record_not_the_reader(tmp_path):
    root = _observing_project(
        tmp_path,
        configuration={
            "resolved_sha256": "recorded-digest",
            "source_path": "/observed/forge.yaml",
        },
    )
    # A reader's own forge.yaml, deliberately different from the recorded one.
    _write(root / "forge.yaml", "project:\n  root: .\n")

    result = ev.collect_run_evidence(root, "aaa111")

    config = next(a for a in result.artifacts if a.kind == ev.KIND_RESOLVED_CONFIG)
    assert "recorded-digest" in config.content
    assert "/observed/forge.yaml" in config.content
    identity = next(a for a in result.artifacts if a.kind == ev.KIND_RUNTIME_IDENTITY)
    assert "anthropic-opus-cli" in identity.content
    assert "recorded-digest"[:12] in result.config_summary


def test_record_without_configuration_reports_it_missing(tmp_path):
    root = _observing_project(tmp_path, configuration=None)

    result = ev.collect_run_evidence(root, "aaa111")

    missing = [m for m in result.missing if m.kind == ev.KIND_RESOLVED_CONFIG]
    assert missing, "a record predating config capture must be named missing"
    assert "predates resolved-configuration capture" in missing[0].reason
    assert not [a for a in result.artifacts if a.kind == ev.KIND_RESOLVED_CONFIG]
    assert result.config_summary.startswith("missing —")


def test_missing_artifact_is_named_and_never_emitted_empty(tmp_path):
    root = _observing_project(tmp_path, configuration={"resolved_sha256": "abc"})

    result = ev.collect_run_evidence(root, "aaa111")

    intake = [m for m in result.missing if m.kind == ev.KIND_INTAKE_CANDIDATES]
    assert intake and "intake" in intake[0].reason
    assert not [a for a in result.artifacts if a.kind == ev.KIND_INTAKE_CANDIDATES]
    assert not result.complete


def test_intake_candidate_artifacts_are_captured_when_present(tmp_path):
    root = _observing_project(tmp_path, configuration={"resolved_sha256": "abc"})
    _write(
        root / ".forge" / "intake" / "candidates" / "issue-320-20260101T000000Z.md",
        "candidate body\n",
    )

    result = ev.collect_run_evidence(root, "aaa111")

    captured = [a for a in result.artifacts if a.kind == ev.KIND_INTAKE_CANDIDATES]
    assert len(captured) == 1
    assert captured[0].content == "candidate body\n"


def test_unknown_run_id_refuses_rather_than_reporting_an_empty_record(tmp_path):
    root = _observing_project(tmp_path)

    with pytest.raises(ev.EvidenceError) as excinfo:
        ev.collect_run_evidence(root, "deadbeef")

    assert "deadbeef" in str(excinfo.value)


def test_oversized_artifact_is_truncated_and_says_so(tmp_path):
    root = _observing_project(tmp_path, configuration={"resolved_sha256": "abc"})
    _write(root / ".forge" / "logs" / "issues-320,324" / "issue-320" / "run-aaa111.log", "x" * 500)

    result = ev.collect_run_evidence(root, "aaa111", max_artifact_chars=100)

    logs = [a for a in result.artifacts if a.name.endswith("run-aaa111.log")]
    assert logs and logs[0].truncated
    assert logs[0].truncated_from == 500
    assert "truncated" in logs[0].content


def test_observed_project_reads_the_checkouts_own_remote(tmp_path):
    root = tmp_path / "proj"
    _write(root / ".git" / "config", '[remote "origin"]\n\turl = https://github.com/acme/thing\n')

    assert ev.read_observed_project(root) == "acme/thing"


def test_observed_project_is_none_without_a_remote(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()

    assert ev.read_observed_project(root) is None
