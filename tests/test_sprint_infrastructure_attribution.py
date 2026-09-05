"""A shared-infrastructure failure is not the executing story's failure (#2107).

When the rolling advisory artifact — a path every story of a sprint writes —
fails to persist, the story that happened to be running when it surfaced was
recorded ``ESCALATE`` with ``Worker exception: ...`` and reported to the
operator as ``FAILED — unknown_needs_rca``. Its own work had completed.

The same boundary now covers audit-substrate lock contention (#2906): a store
held by a *sibling worker of the same sprint* is contention the sprint's own
parallelism created, so it is attributed to the shared infrastructure rather
than charged to whichever story lost the race.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import yaml
from sprint_test_helpers import run_sprint_ctx
from test_sprint_parallel import (
    _make_config,
    _make_coordinator_result,
    _make_manifest_parallel,
    _make_spec_file,
)

from theforge.advisory_conventions import AdvisoryArtifactError
from theforge.coordinator.agent_failure import ERROR_TYPE_INFRASTRUCTURE_ABORT
from theforge.coordinator.audit_storage import SubstrateLockTimeoutError
from theforge.sprint.rca import UNKNOWN_CLASS, build_sprint_rca


def _advisory_error(tmp_path: Path) -> AdvisoryArtifactError:
    artifact = tmp_path / ".forge" / "conventions" / "advisory.yaml"
    cause = FileNotFoundError(2, "No such file or directory")
    return AdvisoryArtifactError(artifact, cause)


def test_advisory_persistence_failure_is_not_a_story_escalate(tmp_path: Path) -> None:
    """The story is recorded as an infrastructure abort, not as its own failure."""
    _make_spec_file(tmp_path, "Story A", "story-a")
    _make_spec_file(tmp_path, "Story B", "story-b")
    manifest_path = _make_manifest_parallel(
        tmp_path,
        ["story-a.md", "story-b.md"],
        budget=10.0,
        max_parallel=1,
    )
    config = _make_config(tmp_path)

    result_b = _make_coordinator_result(success=True, cost=1.0)
    with patch(
        "theforge.sprint.runner.run_task",
        side_effect=[_advisory_error(tmp_path), result_b],
    ):
        sprint = run_sprint_ctx(config, manifest_path)

    # The sprint still finishes and the other story is unaffected.
    assert sprint.specs_succeeded == 1

    sprint_audit = yaml.safe_load(
        (tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text(encoding="utf-8")
    )
    entry = next(e for e in sprint_audit["specs"] if e["path"] == "story-a.md")

    # Not the story's verdict: no "Worker exception" attribution, and the
    # machine-readable code names the substrate.
    assert entry["error_type"] == ERROR_TYPE_INFRASTRUCTURE_ABORT
    assert entry["outcome_code"] == ERROR_TYPE_INFRASTRUCTURE_ABORT
    assert "Worker exception" not in (entry["error"] or "")
    # The operator keeps the real errno and the artifact path.
    assert ".forge/conventions/advisory.yaml" in entry["error"]
    assert "No such file or directory" in entry["error"]

    durable_audit = yaml.safe_load(
        (tmp_path / ".forge" / "logs" / "Parallel Sprint" / "story-a" / "audit.yaml").read_text(
            encoding="utf-8"
        )
    )
    shared = durable_audit["shared_infrastructure_failures"]
    assert shared and shared[0]["component"] == "advisory_conventions_artifact"
    assert shared[0]["error_type"] == "FileNotFoundError"
    assert durable_audit["agent_invocation"]["infrastructure_failure"]["component"] == (
        "advisory_conventions_artifact"
    )


def test_advisory_persistence_failure_classifies_as_shared_infrastructure(
    tmp_path: Path,
) -> None:
    """RCA names the infrastructure, not ``unknown_needs_rca`` on the story."""
    _make_spec_file(tmp_path, "Story A", "story-a")
    manifest_path = _make_manifest_parallel(tmp_path, ["story-a.md"], budget=10.0, max_parallel=1)
    config = _make_config(tmp_path)

    with patch(
        "theforge.sprint.runner.run_task",
        side_effect=[_advisory_error(tmp_path)],
    ):
        run_sprint_ctx(config, manifest_path)

    summary_path = tmp_path / ".forge" / "logs" / "Parallel Sprint" / "sprint-summary.yaml"
    assert summary_path.exists(), "sprint summary was not written"
    payload = build_sprint_rca(summary_path)
    assert payload is not None
    story = payload["stories"]["story-a"]

    assert story["primary_failure_class"] != UNKNOWN_CLASS
    assert story["primary_failure_class"] == "shared_infrastructure"
    assert any(e["rule_id"] == "shared_infrastructure_abort" for e in story["evidence"])
    # The operator is sent to the infrastructure, not to 'forge diagnose' on a
    # story that may already have produced complete work.
    actions = " ".join(story["recommended_next_actions"])
    assert "forge diagnose" not in actions
    assert "shared run infrastructure" in actions


def _lock_timeout_error(tmp_path: Path) -> SubstrateLockTimeoutError:
    substrate = tmp_path / ".forge" / "audits" / "index.sqlite"
    return SubstrateLockTimeoutError(
        f"audit substrate at {substrate} is held by another forge process and did "
        "not become available within 81.0s: database is locked. This is transient "
        "lock contention between concurrent readers/writers, not damage to the "
        "substrate — the store is intact and the operation can be retried."
    )


def test_substrate_lock_contention_is_not_a_story_escalate(tmp_path: Path) -> None:
    """A sibling worker's lock is the sprint's contention, not this story's failure (#2906)."""
    _make_spec_file(tmp_path, "Story A", "story-a")
    _make_spec_file(tmp_path, "Story B", "story-b")
    manifest_path = _make_manifest_parallel(
        tmp_path,
        ["story-a.md", "story-b.md"],
        budget=10.0,
        max_parallel=1,
    )
    config = _make_config(tmp_path)

    result_b = _make_coordinator_result(success=True, cost=1.0)
    with patch(
        "theforge.sprint.runner.run_task",
        side_effect=[_lock_timeout_error(tmp_path), result_b],
    ):
        sprint = run_sprint_ctx(config, manifest_path)

    assert sprint.specs_succeeded == 1

    sprint_audit = yaml.safe_load(
        (tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text(encoding="utf-8")
    )
    entry = next(e for e in sprint_audit["specs"] if e["path"] == "story-a.md")

    assert entry["error_type"] == ERROR_TYPE_INFRASTRUCTURE_ABORT
    assert entry["outcome_code"] == ERROR_TYPE_INFRASTRUCTURE_ABORT
    # Not "worker thread raised SubstrateLockTimeoutError" — the raw exception
    # name was exactly what reached the operator as an undiagnosed failure.
    assert "Worker exception" not in (entry["error"] or "")
    assert "SubstrateLockTimeoutError" not in (entry["error"] or "")
    # The condition the raising code named precisely is preserved, and the
    # destructive remedy it used to recommend is gone.
    assert "database is locked" in entry["error"]
    assert "forge audits rebuild" not in entry["error"]

    durable_audit = yaml.safe_load(
        (tmp_path / ".forge" / "logs" / "Parallel Sprint" / "story-a" / "audit.yaml").read_text(
            encoding="utf-8"
        )
    )
    shared = durable_audit["shared_infrastructure_failures"]
    assert shared and shared[0]["component"] == "audit_substrate_lock"
    assert shared[0]["error_type"] == "SubstrateLockTimeoutError"
    assert durable_audit["agent_invocation"]["infrastructure_failure"]["component"] == (
        "audit_substrate_lock"
    )


def test_substrate_lock_contention_classifies_as_shared_infrastructure(tmp_path: Path) -> None:
    """RCA names the contention rather than asserting no cause was found (#2906)."""
    _make_spec_file(tmp_path, "Story A", "story-a")
    manifest_path = _make_manifest_parallel(tmp_path, ["story-a.md"], budget=10.0, max_parallel=1)
    config = _make_config(tmp_path)

    with patch(
        "theforge.sprint.runner.run_task",
        side_effect=[_lock_timeout_error(tmp_path)],
    ):
        run_sprint_ctx(config, manifest_path)

    summary_path = tmp_path / ".forge" / "logs" / "Parallel Sprint" / "sprint-summary.yaml"
    payload = build_sprint_rca(summary_path)
    assert payload is not None
    story = payload["stories"]["story-a"]

    assert story["primary_failure_class"] != UNKNOWN_CLASS
    assert story["primary_failure_class"] == "shared_infrastructure"
    assert any(e["rule_id"] == "shared_infrastructure_abort" for e in story["evidence"])
    assert any("audit_substrate_lock" in e["excerpt"] for e in story["evidence"])

    actions = " ".join(story["recommended_next_actions"])
    # Neither destructive repair of a healthy store nor diagnosis of a story
    # that never failed.
    assert "forge audits rebuild" not in actions
    assert "forge diagnose" not in actions
    assert "shared run infrastructure" in actions


def test_infrastructure_abort_suppresses_iteration_budget_advice(tmp_path: Path) -> None:
    """A run that only *looks* iteration-exhausted still follows its infra cause."""
    sprint_dir = tmp_path / ".forge" / "logs" / "issues-2443"
    story_dir = sprint_dir / "issue-2443"
    story_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "sprint": {"name": "s", "run_id": "r1", "finished_at": "2026-08-13T03:00:00Z"},
        "stories": [
            {
                "slug": "issue-2443",
                "outcome": "FAILED",
                "error": "dev exhausted iterations",
                "error_type": ERROR_TYPE_INFRASTRUCTURE_ABORT,
                "iteration_usage": {
                    "dev": {"used": 3, "max": 3, "hit_limit": True, "early_finish": False},
                    "review": {
                        "used": 0,
                        "max": 2,
                        "hit_limit": False,
                        "early_finish": False,
                        "early_terminated": False,
                    },
                },
                "contributing_factors": ["dev_iteration_limit"],
            }
        ],
    }
    audit = {
        "outcome": {"error_type": ERROR_TYPE_INFRASTRUCTURE_ABORT},
        "agent_invocation": {
            "infrastructure_failure": {
                "category": "process",
                "detail": "runner exited before agent output was available",
            }
        },
        "iterations": {"usage_summary": summary["stories"][0]["iteration_usage"]},
    }
    (sprint_dir / "sprint-summary.yaml").write_text(yaml.safe_dump(summary), encoding="utf-8")
    (story_dir / "audit.yaml").write_text(yaml.safe_dump(audit), encoding="utf-8")

    payload = build_sprint_rca(sprint_dir / "sprint-summary.yaml")
    story = payload["stories"]["issue-2443"]
    actions = " ".join(story["recommended_next_actions"]).lower()

    assert story["primary_failure_class"] == "shared_infrastructure"
    assert any(e["rule_id"] == "shared_infrastructure_abort" for e in story["evidence"])
    assert any("process" in e["excerpt"] for e in story["evidence"])
    assert "iteration budget" not in actions
    assert "narrow" not in actions
