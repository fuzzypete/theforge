"""Workflow guards for the shape-check sweep wiring."""

from __future__ import annotations

from pathlib import Path

import yaml

from theforge.shape_check.policy_digest import POLICY_SOURCE_FILES

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github/workflows/shape-check.yml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text())


def _workflow_on() -> dict:
    workflow = _workflow()
    return workflow.get("on") or workflow[True]


def _policy_paths() -> list[str]:
    return [f"src/theforge/shape_check/{name}" for name in POLICY_SOURCE_FILES]


def test_workflow_triggers_preview_and_apply_for_policy_manifest_changes() -> None:
    workflow_on = _workflow_on()

    assert workflow_on["pull_request"]["branches"] == ["main"]
    assert workflow_on["pull_request"]["paths"] == _policy_paths()
    assert workflow_on["push"]["branches"] == ["main"]
    assert workflow_on["push"]["paths"] == _policy_paths()


def test_workflow_dispatch_offers_preview_and_apply_modes() -> None:
    mode = _workflow_on()["workflow_dispatch"]["inputs"]["mode"]

    assert mode["default"] == "preview"
    assert mode["options"] == ["preview", "apply"]


def test_workflow_jobs_split_permissions_by_mode() -> None:
    jobs = _workflow()["jobs"]

    assert jobs["shape-check-issue-event"]["permissions"] == {
        "issues": "write",
        "contents": "read",
    }
    assert jobs["shape-check-preview"]["permissions"] == {
        "issues": "read",
        "contents": "read",
    }
    assert jobs["shape-check-apply"]["permissions"] == {
        "issues": "write",
        "contents": "read",
    }


def test_every_job_runs_the_module_entrypoint() -> None:
    jobs = _workflow()["jobs"]

    for job_name in ("shape-check-issue-event", "shape-check-preview", "shape-check-apply"):
        run_steps = [step["run"] for step in jobs[job_name]["steps"] if "run" in step]
        assert "pip install -e ." in run_steps
        assert "python -m theforge.shape_check" in run_steps
