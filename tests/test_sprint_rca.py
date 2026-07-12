"""Tests for the sprint RCA engine and the ``forge rca`` verb.

The engine is a pure, deterministic mapping from local artifacts
(sprint-summary.yaml + per-story audit/logs) to sprint-rca.yaml. Tests exercise
classification (primary + contributing factors), evidence sourcing, the
``unknown_needs_rca`` residual, regenerability, and the on-demand verb.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from types import SimpleNamespace

import yaml

from theforge.sprint.rca import (
    RULES,
    RULES_BY_ID,
    UNKNOWN_CLASS,
    build_sprint_rca,
    has_non_done_stories,
    read_sprint_rca,
    write_sprint_rca,
)


def _write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _sprint_dir(tmp_path: Path, name: str = "issues-1324,1326,793") -> Path:
    d = tmp_path / ".forge" / "logs" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _summary(stories: list[dict], run_id: str = "6c83b3061455") -> dict:
    return {
        "sprint": {
            "name": "test-sprint",
            "run_id": run_id,
            "finished_at": "2026-05-08T03:00:00Z",
        },
        "stories": stories,
    }


# ── Engine: eager-generation trigger ──────────────────────────────────────────


def test_has_non_done_stories_true_when_any_failed() -> None:
    summary = _summary(
        [
            {"slug": "issue-1", "outcome": "DONE"},
            {"slug": "issue-2", "outcome": "FAILED"},
        ]
    )
    assert has_non_done_stories(summary) is True


def test_all_done_stories_produce_no_rca(tmp_path: Path) -> None:
    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary(
            [
                {"slug": "issue-1", "outcome": "DONE"},
                {"slug": "issue-2", "outcome": "ALREADY_DONE"},
            ]
        ),
    )
    assert has_non_done_stories(read_sprint_rca_summary(d)) is False
    assert build_sprint_rca(d) is None
    assert write_sprint_rca(d) is None
    assert not (d / "sprint-rca.yaml").exists()


def read_sprint_rca_summary(d: Path) -> dict:
    return yaml.safe_load((d / "sprint-summary.yaml").read_text())


# ── Engine: only non-DONE stories appear; LANDED excluded ─────────────────────


def test_landed_stories_not_duplicated(tmp_path: Path) -> None:
    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary(
            [
                {"slug": "issue-1", "outcome": "DONE"},
                {"slug": "issue-2", "outcome": "ALREADY_DONE"},
                {"slug": "issue-793", "outcome": "DROPPED_AFTER_FIX", "error": "intake"},
            ]
        ),
    )
    rca = build_sprint_rca(d)
    assert rca is not None
    assert set(rca["stories"].keys()) == {"issue-793"}


# ── Engine: entry shape (all required keys present) ───────────────────────────


def test_every_entry_has_required_keys(tmp_path: Path) -> None:
    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-42", "outcome": "FAILED", "error": "boom"}]),
    )
    rca = build_sprint_rca(d)
    entry = rca["stories"]["issue-42"]
    for key in (
        "primary_failure_class",
        "contributing_factors",
        "evidence",
        "partial_value",
        "recommended_next_actions",
    ):
        assert key in entry, key
    assert isinstance(entry["contributing_factors"], list)
    assert isinstance(entry["partial_value"], list)
    assert isinstance(entry["recommended_next_actions"], list)
    # evidence triples
    for ev in entry["evidence"]:
        assert set(ev.keys()) == {"source", "rule_id", "excerpt"}


# ── Engine: provider-quota primary + operator-gate-timeout contributing ───────


def test_provider_quota_with_gate_timeout(tmp_path: Path) -> None:
    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-1324", "outcome": "ESCALATE", "error": "escalated"}]),
    )
    # Captured agent output with a usage-limit error.
    cycle_dir = d / "issue-1324" / "review-cycle-2"
    cycle_dir.mkdir(parents=True, exist_ok=True)
    (cycle_dir / "openai-gpt.yaml").write_text(
        "output: |\n  ERROR: You've hit your usage limit. Try again at 6:57 PM.\n",
        encoding="utf-8",
    )
    # Sprint run log with a pending-decision timeout line referencing #1324.
    (d / "run-6c83b3061455.log").write_text(
        "issue #1324 escalating\n"
        "Pending decision timed out after 10m 0s — auto-escalating for #1324\n",
        encoding="utf-8",
    )

    rca = build_sprint_rca(d)
    entry = rca["stories"]["issue-1324"]
    assert entry["primary_failure_class"] == "provider_quota"
    assert "operator_gate_timeout" in entry["contributing_factors"]
    rule_ids = {ev["rule_id"] for ev in entry["evidence"]}
    assert "provider_usage_limit" in rule_ids
    assert "pending_decision_auto_rejected" in rule_ids
    # evidence sources are relative to the logs root
    quota_ev = next(ev for ev in entry["evidence"] if ev["rule_id"] == "provider_usage_limit")
    assert quota_ev["source"].endswith("openai-gpt.yaml")
    assert "usage limit" in quota_ev["excerpt"].lower()


# ── Engine: worker timeout + partial value ────────────────────────────────────


def test_worker_timeout_with_partial_value(tmp_path: Path) -> None:
    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-1326", "outcome": "FAILED", "error": "timeout"}]),
    )
    _write(
        d / "issue-1326" / "audit.yaml",
        {
            "outcome": {
                "final_phase": "FAILED",
                "message": "Worker thread timed out after 3600s",
            },
            "error": "Worker timeout (>3600s) during phase VALIDATE",
            "cost": {"dev_invocations": 1},
            "workspace": {"path": "/wt/issue-1326", "branch": "feat/issue-1326"},
        },
    )
    rca = build_sprint_rca(d)
    entry = rca["stories"]["issue-1326"]
    assert entry["primary_failure_class"] == "worker_timeout"
    assert any("dev produced 1 iteration" in v for v in entry["partial_value"])


# ── Engine: intake shape drop ─────────────────────────────────────────────────


def test_intake_shape_drop(tmp_path: Path) -> None:
    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary(
            [
                {
                    "slug": "issue-793",
                    "outcome": "DROPPED_AFTER_FIX",
                    "error": "issue #793 -> dropped_after_fix (rerun gate still failing)",
                }
            ]
        ),
    )
    rca = build_sprint_rca(d)
    entry = rca["stories"]["issue-793"]
    assert entry["primary_failure_class"] == "intake_shape"
    assert entry["contributing_factors"] == []
    assert any("reshape" in a for a in entry["recommended_next_actions"])


# ── Engine: dependency skip ───────────────────────────────────────────────────


def test_dependency_skip(tmp_path: Path) -> None:
    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-9", "outcome": "SKIPPED", "depends_on": ["issue-8"]}]),
    )
    entry = build_sprint_rca(d)["stories"]["issue-9"]
    assert entry["primary_failure_class"] == "dependency_skip"
    assert any("issue-8" in a for a in entry["recommended_next_actions"])


# ── Engine: unknown residual never drops ──────────────────────────────────────


def test_unknown_needs_rca_residual(tmp_path: Path) -> None:
    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-77", "outcome": "SKIPPED"}]),
    )
    entry = build_sprint_rca(d)["stories"]["issue-77"]
    assert entry["primary_failure_class"] == UNKNOWN_CLASS
    # Never evidence-empty: baseline captured_outcome is always present.
    assert entry["evidence"]
    assert entry["evidence"][-1]["rule_id"] == "captured_outcome"
    assert any("diagnose" in a for a in entry["recommended_next_actions"])


# ── Engine: determinism / regenerability ──────────────────────────────────────


def test_deterministic_from_disk(tmp_path: Path) -> None:
    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-1324", "outcome": "ESCALATE", "error": "usage limit hit"}]),
    )
    first = build_sprint_rca(d)
    second = build_sprint_rca(d)
    assert first == second
    # generated_at derived from summary finished_at → stable
    assert first["generated_at"] == "2026-05-08T03:00:00Z"
    assert first["sprint_run_id"] == "6c83b3061455"


def test_improved_ruleset_regenerates_without_rerun(tmp_path: Path) -> None:
    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-5", "outcome": "FAILED", "error": "mysterious"}]),
    )
    p1 = write_sprint_rca(d)
    assert p1 is not None
    before = read_sprint_rca(d)
    assert before["stories"]["issue-5"]["primary_failure_class"] == UNKNOWN_CLASS

    # Simulate an improved artifact: add captured output the rules now match.
    (d / "issue-5").mkdir(exist_ok=True)
    (d / "issue-5" / "dev.log").write_text("Worker thread timed out after 60s\n", encoding="utf-8")
    write_sprint_rca(d, overwrite=True)
    after = read_sprint_rca(d)
    assert after["stories"]["issue-5"]["primary_failure_class"] == "worker_timeout"


# ── Rule set discoverability ──────────────────────────────────────────────────


def test_all_rule_ids_unique_and_indexed() -> None:
    ids = [r.rule_id for r in RULES]
    assert len(ids) == len(set(ids))
    assert set(RULES_BY_ID.keys()) == set(ids)
    for rule in RULES:
        assert rule.role in {"primary", "contributing", "informational"}


# ── write / overwrite semantics ───────────────────────────────────────────────


# ── Seam: completion boundary (real summary writer → RCA engine) ──────────────


def test_completion_seam_summary_to_rca(tmp_path: Path) -> None:
    """Drive the real sprint-summary writer, then the RCA engine off its output.

    Mirrors the runner's completion path: ``_write_sprint_summary`` writes the
    artifact, then the RCA engine classifies non-DONE stories from that same
    on-disk summary — no runtime state passed between them.
    """
    from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
    from theforge.sprint.audit import _write_sprint_summary

    sprint_log_dir = tmp_path / ".forge" / "logs" / "seam-sprint"
    sprint_log_dir.mkdir(parents=True, exist_ok=True)

    started_at = datetime.datetime(2026, 5, 8, 2, 0, 0, tzinfo=datetime.timezone.utc)
    finished_at = datetime.datetime(2026, 5, 8, 3, 0, 0, tzinfo=datetime.timezone.utc)

    timeout_state = CoordinatorState(
        phase=Phase.ESCALATE,
        started_at=started_at.isoformat(),
        workspace_path=tmp_path / "workspace",
        log_dir=sprint_log_dir / "issue-1326",
        error="Worker timeout (>3600s) during phase VALIDATE",
        error_type="TimeoutError",
    )
    timeout_result = CoordinatorResult(
        success=False,
        phase=Phase.ESCALATE,
        state=timeout_state,
        message="Worker thread timed out after 3600s",
    )
    sprint_result = SimpleNamespace(results=[("issue:1326", timeout_result)], stopped_reason=None)
    manifest = SimpleNamespace(name="seam-sprint", budget_usd=50.0, max_parallel=1)

    _write_sprint_summary(
        manifest=manifest,
        result=sprint_result,
        canonical_refs=["issue:1326"],
        started_at=started_at,
        finished_at=finished_at,
        duration=3600.0,
        sprint_log_dir=sprint_log_dir,
        story_times={"issue-1326": (started_at, finished_at)},
        slug_map={"issue:1326": "issue-1326"},
        run_id="run-seam",
        tasks_by_slug={"issue-1326": SimpleNamespace(depends_on=[])},
        project_root=tmp_path,
    )

    # Per-story log carrying the worker-timeout signal (as the runner writes).
    (sprint_log_dir / "issue-1326").mkdir(parents=True, exist_ok=True)
    (sprint_log_dir / "issue-1326" / "dev.log").write_text(
        "Worker thread timed out after 3600s\n", encoding="utf-8"
    )

    path = write_sprint_rca(sprint_log_dir)
    assert path is not None and path.name == "sprint-rca.yaml"
    rca = read_sprint_rca(sprint_log_dir)
    assert rca["sprint_run_id"] == "run-seam"
    entry = rca["stories"]["issue-1326"]
    assert entry["primary_failure_class"] == "worker_timeout"


# ── CLI: forge rca verb ───────────────────────────────────────────────────────


def _init_forge_project(tmp_path: Path) -> None:
    (tmp_path / "forge.yaml").write_text("project: rca-test\nprovider: mock\n", encoding="utf-8")


def test_cli_rca_generates_and_refresh(tmp_path: Path, monkeypatch, capsys) -> None:
    from theforge.cli import rca as rca_cli

    d = _sprint_dir(tmp_path, name="cli-sprint")
    _write(
        d / "sprint-summary.yaml",
        _summary(
            [{"slug": "issue-1324", "outcome": "ESCALATE", "error": "usage limit hit"}],
            run_id="cli-run-1",
        ),
    )
    _init_forge_project(tmp_path)

    monkeypatch.setattr(rca_cli, "load_config", lambda _p: SimpleNamespace(project_root=tmp_path))
    monkeypatch.setattr(rca_cli, "_find_config", lambda *_a, **_k: tmp_path / "forge.yaml")

    args = SimpleNamespace(run_id="cli-run-1", config=None, refresh=False)
    assert rca_cli.cmd_rca(args) == 0
    rca_path = d / "sprint-rca.yaml"
    assert rca_path.exists()
    generated = read_sprint_rca(d)
    assert generated["stories"]["issue-1324"]["primary_failure_class"] == "provider_quota"

    # Without --refresh, an existing file is left untouched.
    rca_path.write_text("sentinel: true\n", encoding="utf-8")
    assert rca_cli.cmd_rca(SimpleNamespace(run_id="cli-run-1", config=None, refresh=False)) == 0
    assert "sentinel" in rca_path.read_text()

    # --refresh overwrites.
    assert rca_cli.cmd_rca(SimpleNamespace(run_id="cli-run-1", config=None, refresh=True)) == 0
    assert "sentinel" not in rca_path.read_text()


def test_cli_rca_unknown_run_id(tmp_path: Path, monkeypatch) -> None:
    from theforge.cli import rca as rca_cli

    _init_forge_project(tmp_path)
    monkeypatch.setattr(rca_cli, "load_config", lambda _p: SimpleNamespace(project_root=tmp_path))
    monkeypatch.setattr(rca_cli, "_find_config", lambda *_a, **_k: tmp_path / "forge.yaml")
    args = SimpleNamespace(run_id="does-not-exist", config=None, refresh=False)
    assert rca_cli.cmd_rca(args) == 1


def test_write_no_overwrite_leaves_existing(tmp_path: Path) -> None:
    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-1", "outcome": "FAILED", "error": "x"}]),
    )
    path = write_sprint_rca(d)
    path.write_text("sentinel: true\n", encoding="utf-8")
    # overwrite=False must not regenerate
    write_sprint_rca(d, overwrite=False)
    assert "sentinel" in path.read_text()
    # overwrite=True regenerates
    write_sprint_rca(d, overwrite=True)
    assert "sentinel" not in path.read_text()
