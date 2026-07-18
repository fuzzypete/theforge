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


def _build(d: Path):
    """Build RCA from a sprint dir's legacy summary (engine takes a summary path)."""
    return build_sprint_rca(d / "sprint-summary.yaml")


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
    assert _build(d) is None
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
    rca = _build(d)
    assert rca is not None
    assert set(rca["stories"].keys()) == {"issue-793"}


# ── Engine: entry shape (all required keys present) ───────────────────────────


def test_every_entry_has_required_keys(tmp_path: Path) -> None:
    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-42", "outcome": "FAILED", "error": "boom"}]),
    )
    rca = _build(d)
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

    rca = _build(d)
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


# ── Engine: ambiguous "429" must not match inside cost/duration floats ────────


def test_bare_429_in_cost_float_not_provider_quota(tmp_path: Path) -> None:
    """The reported bug: a cost_usd float whose digits contain "429" must not be
    classified provider_quota, and no "wait for quota reset" remediation fires.

    Reproduces sprint 493cd6ae81e4 / #1747: the real failure is an empty-worktree
    missing-work escalate, but ``cost_usd: 0.6659942999999999`` contains the
    substring ``429`` and previously drove a spurious provider_usage_limit hit.
    """
    empty_worktree_err = (
        "Gate exited PASS but branch has no commits ahead of base — "
        "treating empty worktree as missing-work failure"
    )
    d = _sprint_dir(tmp_path, name="issues-927,1773,1747")
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-1747", "outcome": "FAILED", "error": empty_worktree_err}]),
    )
    _write(
        d / "issue-1747" / "audit.yaml",
        {
            "error": empty_worktree_err,
            "error_type": None,
            "cost": {"cost_usd": 0.6659942999999999, "dev_invocations": 1},
        },
    )
    entry = _build(d)["stories"]["issue-1747"]
    assert entry["primary_failure_class"] != "provider_quota"
    assert entry["primary_failure_class"] == UNKNOWN_CLASS
    rule_ids = {ev["rule_id"] for ev in entry["evidence"]}
    assert "provider_usage_limit" not in rule_ids
    assert not any("quota" in a.lower() for a in entry["recommended_next_actions"])
    # The real captured error is still surfaced as baseline evidence.
    assert any("empty worktree" in ev["excerpt"].lower() for ev in entry["evidence"])


def test_standalone_429_status_still_detected(tmp_path: Path) -> None:
    """A genuine standalone HTTP 429 in captured provider output still classifies.

    Anchoring must not blind the rule to real rate-limit responses — only to
    digit runs inside unrelated numbers.
    """
    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-1324", "outcome": "ESCALATE"}]),
    )
    cycle_dir = d / "issue-1324" / "review-cycle-1"
    cycle_dir.mkdir(parents=True, exist_ok=True)
    (cycle_dir / "openai-gpt.yaml").write_text(
        "output: |\n  Provider returned HTTP 429 Too Many Requests\n",
        encoding="utf-8",
    )
    entry = _build(d)["stories"]["issue-1324"]
    assert entry["primary_failure_class"] == "provider_quota"
    quota_ev = next(ev for ev in entry["evidence"] if ev["rule_id"] == "provider_usage_limit")
    assert "429" in quota_ev["excerpt"]


def test_ambiguous_429_yields_to_captured_non_provider_error(tmp_path: Path) -> None:
    """A bare 429 hit must not outrank an explicit captured non-provider error.

    Even when a standalone 429 appears in a scanned file, a concrete captured
    terminal error describing a different cause takes precedence — the story is
    not attributed to provider_quota and no quota remediation is emitted.
    """
    empty_worktree_err = (
        "Gate exited PASS but branch has no commits ahead of base — "
        "treating empty worktree as missing-work failure"
    )
    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-1747", "outcome": "FAILED", "error": empty_worktree_err}]),
    )
    story_dir = d / "issue-1747"
    story_dir.mkdir(parents=True, exist_ok=True)
    (story_dir / "dev.log").write_text("transient blip: code 429 seen once\n", encoding="utf-8")
    entry = _build(d)["stories"]["issue-1747"]
    assert entry["primary_failure_class"] != "provider_quota"
    assert not any("quota" in a.lower() for a in entry["recommended_next_actions"])


def test_error_prose_mentioning_cost_is_preserved(tmp_path: Path) -> None:
    """Telemetry redaction keys on field names, not the word 'cost' in prose.

    An unambiguous provider phrase in an error message that also mentions cost
    must still classify provider_quota.
    """
    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-1324", "outcome": "ESCALATE"}]),
    )
    story_dir = d / "issue-1324"
    story_dir.mkdir(parents=True, exist_ok=True)
    (story_dir / "dev.log").write_text(
        "The provider hit its usage limit before any cost accrued\n", encoding="utf-8"
    )
    entry = _build(d)["stories"]["issue-1324"]
    assert entry["primary_failure_class"] == "provider_quota"


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
    rca = _build(d)
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
    rca = _build(d)
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
    entry = _build(d)["stories"]["issue-9"]
    assert entry["primary_failure_class"] == "dependency_skip"
    assert any("issue-8" in a for a in entry["recommended_next_actions"])


# ── Engine: unknown residual never drops ──────────────────────────────────────


def test_unknown_needs_rca_residual(tmp_path: Path) -> None:
    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-77", "outcome": "SKIPPED"}]),
    )
    entry = _build(d)["stories"]["issue-77"]
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
    first = _build(d)
    second = _build(d)
    assert first == second
    # generated_at derived from summary finished_at → stable
    assert first["generated_at"] == "2026-05-08T03:00:00Z"
    assert first["sprint_run_id"] == "6c83b3061455"


def test_ruleset_version_stamped(tmp_path: Path) -> None:
    """Every artifact records the rule-set version that produced it."""
    from theforge.sprint import rca as rca_mod

    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-5", "outcome": "FAILED", "error": "boom"}]),
    )
    payload = _build(d)
    assert payload["schema_version"] == rca_mod.SCHEMA_VERSION
    assert payload["ruleset_version"] == rca_mod.RULESET_VERSION


def test_improved_ruleset_regenerates_versioned(tmp_path: Path, monkeypatch) -> None:
    """A changed rule set regenerates a different, version-stamped artifact.

    The on-disk inputs are held fixed; only the classifier rule set changes.
    Regeneration produces different conclusions AND a bumped ruleset_version, so
    the re-analysis is a visible, versioned event rather than a silent rewrite of
    historical judgement.
    """
    from theforge.sprint import rca as rca_mod

    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-5", "outcome": "FAILED", "error": "mysterious flooble"}]),
    )
    write_sprint_rca(d)
    before = read_sprint_rca(d)
    assert before["stories"]["issue-5"]["primary_failure_class"] == UNKNOWN_CLASS
    assert before["ruleset_version"] == 1

    # Improved rule set (v2): a new rule now recognises the previously-unknown
    # signature. Inputs on disk are unchanged.
    improved_rule = rca_mod.RcaRule(
        rule_id="flooble_detected",
        failure_class="flooble_fault",
        role="primary",
        description="Recognises the flooble signature.",
        patterns=("flooble",),
    )
    monkeypatch.setattr(rca_mod, "RULES", (*rca_mod.RULES, improved_rule))
    monkeypatch.setattr(rca_mod, "RULES_BY_ID", {r.rule_id: r for r in rca_mod.RULES})
    monkeypatch.setattr(
        rca_mod, "_PRIMARY_PRIORITY", ("flooble_fault", *rca_mod._PRIMARY_PRIORITY)
    )
    monkeypatch.setattr(rca_mod, "RULESET_VERSION", 2)

    write_sprint_rca(d, overwrite=True)
    after = read_sprint_rca(d)
    assert after["stories"]["issue-5"]["primary_failure_class"] == "flooble_fault"
    assert after["ruleset_version"] == 2
    # Same inputs, different rule set → distinguishable analyses.
    assert before["ruleset_version"] != after["ruleset_version"]


def test_evidence_source_cites_resolved_summary(tmp_path: Path) -> None:
    """Summary-derived + captured_outcome evidence cites the analysed summary file.

    When the engine is pointed at run-<id>-summary.yaml, evidence sources must
    reference that file — not the legacy sprint-summary.yaml — because the
    analysis is evidenced against the artifacts actually used for that run.
    """
    d = _sprint_dir(tmp_path, name="evidence-src")
    # Legacy pointer belongs to a different, all-DONE run.
    _write(
        d / "sprint-summary.yaml", _summary([{"slug": "issue-x", "outcome": "DONE"}], run_id="new")
    )
    # Historical failed run in its own run-keyed summary.
    per_run = d / "run-hist-summary.yaml"
    _write(
        per_run,
        _summary(
            [{"slug": "issue-1", "outcome": "MERGE_FAILED", "error": "conflict"}], run_id="hist"
        ),
    )

    rca = build_sprint_rca(per_run)
    entry = rca["stories"]["issue-1"]
    for ev in entry["evidence"]:
        if ev["source"].endswith("summary.yaml"):
            assert ev["source"].endswith("run-hist-summary.yaml"), ev
            assert not ev["source"].endswith("/sprint-summary.yaml")
    # captured_outcome baseline is summary-derived and must be correctly sourced.
    baseline = entry["evidence"][-1]
    assert baseline["rule_id"] == "captured_outcome"
    assert baseline["source"].endswith("run-hist-summary.yaml")


def test_historical_run_ignores_sibling_run_log(tmp_path: Path) -> None:
    """A historical run whose own run log is gone must not borrow a sibling's log.

    run-<hist>.log is absent, but a later same-name run's run-<other>.log remains
    in the sprint dir and contains a line mentioning this story. The engine must
    NOT attribute that sibling run's line to the historical run — evidence is
    confined to the requested run's local artifacts.
    """
    d = _sprint_dir(tmp_path, name="contam")
    per_run = d / "run-hist-summary.yaml"
    _write(
        per_run,
        _summary(
            [{"slug": "issue-1324", "outcome": "ESCALATE", "error": "usage limit hit"}],
            run_id="hist",
        ),
    )
    # A DIFFERENT run's log survives and references issue-1324 with a gate timeout.
    (d / "run-other.log").write_text(
        "Pending decision timed out after 10m 0s — auto-escalating for #1324\n",
        encoding="utf-8",
    )
    # This run's own log does NOT exist (run-hist.log absent).

    entry = build_sprint_rca(per_run)["stories"]["issue-1324"]
    # Provider quota still classified from the summary error...
    assert entry["primary_failure_class"] == "provider_quota"
    # ...but the sibling run's gate-timeout line must not leak in.
    assert "operator_gate_timeout" not in entry["contributing_factors"]
    for ev in entry["evidence"]:
        assert not ev["source"].endswith("run-other.log"), ev


# ── Signal rules (field-derived) ──────────────────────────────────────────────


def test_merge_failed_signal(tmp_path: Path) -> None:
    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-3", "outcome": "MERGE_FAILED", "error": "conflict"}]),
    )
    entry = _build(d)["stories"]["issue-3"]
    assert entry["primary_failure_class"] == "merge_failed"
    assert {ev["rule_id"] for ev in entry["evidence"]} >= {"merge_failed"}


def test_operator_action_signal(tmp_path: Path) -> None:
    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-4", "outcome": "OPERATOR_ACTION", "error": "human only"}]),
    )
    entry = _build(d)["stories"]["issue-4"]
    assert entry["primary_failure_class"] == "operator_action"


def test_launch_collision_signal(tmp_path: Path) -> None:
    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary(
            [
                {
                    "slug": "issue-6",
                    "outcome": "DROPPED",
                    "drop_reason": "active-worktree collision",
                }
            ]
        ),
    )
    entry = _build(d)["stories"]["issue-6"]
    assert entry["primary_failure_class"] == "launch_collision"
    assert any("worktree" in a or "lock" in a for a in entry["recommended_next_actions"])


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


def test_cli_rca_historical_run_via_per_run_summary(tmp_path: Path, monkeypatch) -> None:
    """Regenerating an older run must analyse that run, not the overwritten pointer.

    The legacy sprint-summary.yaml has been overwritten by a later, all-DONE run;
    the failed run survives only in its run-<id>-summary.yaml. `forge rca <old>`
    must classify the old run's failure and write the durable run-keyed RCA
    without clobbering the current run's sprint-rca.yaml pointer.
    """
    from theforge.cli import rca as rca_cli

    d = _sprint_dir(tmp_path, name="hist-sprint")
    # Older run: a real failure, preserved only in the run-keyed summary.
    _write(
        d / "run-old123-summary.yaml",
        _summary(
            [{"slug": "issue-1324", "outcome": "ESCALATE", "error": "usage limit hit"}],
            run_id="old123",
        ),
    )
    # Later same-name run overwrote the legacy pointer with an all-DONE summary.
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-2000", "outcome": "DONE"}], run_id="new456"),
    )
    _init_forge_project(tmp_path)
    monkeypatch.setattr(rca_cli, "load_config", lambda _p: SimpleNamespace(project_root=tmp_path))
    monkeypatch.setattr(rca_cli, "_find_config", lambda *_a, **_k: tmp_path / "forge.yaml")

    args = SimpleNamespace(run_id="old123", config=None, refresh=False)
    assert rca_cli.cmd_rca(args) == 0

    # The old run's failure was analysed into a run-keyed artifact...
    run_keyed = d / "run-old123-sprint-rca.yaml"
    assert run_keyed.exists()
    hist = yaml.safe_load(run_keyed.read_text())
    assert hist["sprint_run_id"] == "old123"
    story = hist["stories"]["issue-1324"]
    assert story["primary_failure_class"] == "provider_quota"
    # Summary-derived evidence must cite the RESOLVED run-keyed summary, never
    # the legacy pointer (which now belongs to a later run and lacks the cited
    # excerpt). No evidence source may point at sprint-summary.yaml.
    summary_sources = [
        ev["source"] for ev in story["evidence"] if ev["source"].endswith("summary.yaml")
    ]
    assert summary_sources, "expected at least one summary-derived evidence entry"
    for src in summary_sources:
        assert src.endswith("run-old123-summary.yaml"), src
        assert not src.endswith("/sprint-summary.yaml")
    # ...without creating/clobbering the latest-run pointer.
    assert not (d / "sprint-rca.yaml").exists()


def test_cli_rca_refresh_historical_run(tmp_path: Path, monkeypatch) -> None:
    """--refresh regenerates a historical (non-latest) run's run-keyed artifact."""
    from theforge.cli import rca as rca_cli

    d = _sprint_dir(tmp_path, name="hist-refresh")
    _write(
        d / "run-old999-summary.yaml",
        _summary(
            [{"slug": "issue-1324", "outcome": "ESCALATE", "error": "usage limit hit"}],
            run_id="old999",
        ),
    )
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-2000", "outcome": "DONE"}], run_id="newAAA"),
    )
    _init_forge_project(tmp_path)
    monkeypatch.setattr(rca_cli, "load_config", lambda _p: SimpleNamespace(project_root=tmp_path))
    monkeypatch.setattr(rca_cli, "_find_config", lambda *_a, **_k: tmp_path / "forge.yaml")

    run_keyed = d / "run-old999-sprint-rca.yaml"
    assert (
        rca_cli.cmd_rca(SimpleNamespace(run_id="old999", config=None, refresh=False, check=False))
        == 0
    )
    assert run_keyed.exists()

    # Without --refresh the existing run-keyed artifact is left untouched.
    run_keyed.write_text("sentinel: true\n", encoding="utf-8")
    assert (
        rca_cli.cmd_rca(SimpleNamespace(run_id="old999", config=None, refresh=False, check=False))
        == 0
    )
    assert "sentinel" in run_keyed.read_text()

    # --refresh regenerates it; the latest pointer is never created.
    assert (
        rca_cli.cmd_rca(SimpleNamespace(run_id="old999", config=None, refresh=True, check=False))
        == 0
    )
    assert "sentinel" not in run_keyed.read_text()
    assert not (d / "sprint-rca.yaml").exists()


def test_cli_rca_check_reproducible_and_drift(tmp_path: Path, monkeypatch) -> None:
    """--check reports reproducibility (exit 0) and drift (exit 2) without writing."""
    from theforge.cli import rca as rca_cli
    from theforge.sprint import rca as rca_mod

    d = _sprint_dir(tmp_path, name="check-sprint")
    _write(
        d / "sprint-summary.yaml",
        _summary(
            [{"slug": "issue-1324", "outcome": "ESCALATE", "error": "usage limit hit"}],
            run_id="chk-run",
        ),
    )
    _init_forge_project(tmp_path)
    monkeypatch.setattr(rca_cli, "load_config", lambda _p: SimpleNamespace(project_root=tmp_path))
    monkeypatch.setattr(rca_cli, "_find_config", lambda *_a, **_k: tmp_path / "forge.yaml")

    # --check with no artifact yet → rc 1 (nothing to check).
    assert (
        rca_cli.cmd_rca(SimpleNamespace(run_id="chk-run", config=None, refresh=False, check=True))
        == 1
    )

    # Generate, then --check must report reproducible (rc 0), no write.
    assert (
        rca_cli.cmd_rca(SimpleNamespace(run_id="chk-run", config=None, refresh=False, check=False))
        == 0
    )
    pointer = d / "sprint-rca.yaml"
    before = pointer.read_text()
    assert (
        rca_cli.cmd_rca(SimpleNamespace(run_id="chk-run", config=None, refresh=False, check=True))
        == 0
    )
    assert pointer.read_text() == before  # --check never writes

    # Change the rule set so a fresh generation would diverge → drift, rc 2.
    monkeypatch.setattr(rca_mod, "RULESET_VERSION", 99)
    assert (
        rca_cli.cmd_rca(SimpleNamespace(run_id="chk-run", config=None, refresh=False, check=True))
        == 2
    )
    assert pointer.read_text() == before  # still no write on drift


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
