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


# ── Engine: workspace base-branch divergence is a known mechanical class ──────


def test_workspace_base_divergence_classifies_not_unknown(tmp_path: Path) -> None:
    """A WORKSPACE abort divergence error must classify as workspace_divergence,
    not fall through to the unknown_needs_rca residual — and the recommended
    action must point at resolving the branch state, not LLM diagnosis.

    Reproduces #1899: the sprint RCA recorded primary_failure_class:
    unknown_needs_rca for a story whose captured error was the exact
    WORKSPACE abort divergence message raised by workspace.py.
    """
    divergence_err = (
        "WORKSPACE abort: base branch 'release/v0.13' has diverged from origin"
        " (local is 1 ahead, 1 behind). Run: git rebase origin/release/v0.13"
    )
    d = _sprint_dir(tmp_path, name="issues-1899,158,1019,1108,1443,1489")
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-1899", "outcome": "FAILED", "error": divergence_err}]),
    )
    entry = _build(d)["stories"]["issue-1899"]
    assert entry["primary_failure_class"] == "workspace_divergence"
    assert entry["primary_failure_class"] != UNKNOWN_CLASS
    rule_ids = {ev["rule_id"] for ev in entry["evidence"]}
    assert "workspace_base_divergence" in rule_ids
    actions = " ".join(entry["recommended_next_actions"]).lower()
    assert "rebase" in actions or "diverg" in actions
    assert "forge diagnose" not in actions


def test_workspace_abort_pull_failure_without_divergence_not_misclassified(
    tmp_path: Path,
) -> None:
    """A WORKSPACE abort that is a plain pull failure (no divergence) must not be
    classified as workspace_divergence — it did not diverge, so the rebase
    remediation would be wrong.
    """
    pull_failed_err = (
        "WORKSPACE abort: pull failed for base branch 'release/v0.13' — "
        "fatal: unable to access 'https://github.com/...': Could not resolve host"
    )
    d = _sprint_dir(tmp_path, name="issues-1900")
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-1900", "outcome": "FAILED", "error": pull_failed_err}]),
    )
    entry = _build(d)["stories"]["issue-1900"]
    assert entry["primary_failure_class"] != "workspace_divergence"
    rule_ids = {ev["rule_id"] for ev in entry["evidence"]}
    assert "workspace_base_divergence" not in rule_ids


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


def test_corroborated_429_classifies_despite_captured_error(tmp_path: Path) -> None:
    """A bare 429 corroborated by a strong phrase in the same source still classifies.

    When a scanned provider log carries both an ambiguous status code and an
    unambiguous provider-limit phrase ("HTTP 429 ... overloaded"), the hit is
    corroborated and must classify provider_quota even though the story summary
    holds an unrelated captured error — the ambiguity guard applies only to
    uncorroborated bare status codes.
    """
    empty_worktree_err = (
        "Gate exited PASS but branch has no commits ahead of base — "
        "treating empty worktree as missing-work failure"
    )
    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-1324", "outcome": "FAILED", "error": empty_worktree_err}]),
    )
    story_dir = d / "issue-1324"
    story_dir.mkdir(parents=True, exist_ok=True)
    (story_dir / "dev.log").write_text(
        "Provider returned HTTP 429\nModel is overloaded, retry later\n", encoding="utf-8"
    )
    entry = _build(d)["stories"]["issue-1324"]
    assert entry["primary_failure_class"] == "provider_quota"
    assert {ev["rule_id"] for ev in entry["evidence"]} >= {"provider_usage_limit"}
    assert any("quota" in a.lower() for a in entry["recommended_next_actions"])


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


# ── Engine: cause codes come from forge's own run, not target-repo prose (#2031) ──


def test_agent_prose_about_project_fallbacks_is_not_a_runtime_cause(tmp_path: Path) -> None:
    """Agent prose analysing the *target repository* must not assign a cause code.

    A preflight/dev agent describing the project under development ("no fallback
    path", "the fallback is unavailable") is describing the work being attempted,
    not the run attempting it. Matching that vocabulary into
    ``provider_fallback_not_applied`` told the operator to go wire a forge
    subsystem that was never involved.
    """
    d = _sprint_dir(tmp_path, name="prose-fallback")
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-248", "outcome": "FAILED", "error": "story did not complete"}]),
    )
    (d / "issue-248").mkdir(parents=True, exist_ok=True)
    (d / "issue-248" / "preflight-raw.log").write_text(
        "The rest-timer scheduling function invalidates the session unconditionally, "
        "with no fallback or guard path preserving the existing alarm.\n",
        encoding="utf-8",
    )
    (d / "issue-248" / "dev-iteration-1.log").write_text(
        "Dev notes: the fallback is unavailable in the target module, so a fallback "
        "not applied branch has to be added.\n",
        encoding="utf-8",
    )

    entry = _build(d)["stories"]["issue-248"]
    assert entry["primary_failure_class"] == UNKNOWN_CLASS
    assert "fallback_not_applied" not in entry["contributing_factors"]
    assert "provider_fallback_not_applied" not in {ev["rule_id"] for ev in entry["evidence"]}
    assert not any("wire the provider fallback" in a for a in entry["recommended_next_actions"])
    assert any("diagnose" in a for a in entry["recommended_next_actions"])


def test_genuine_unapplied_provider_fallback_still_recorded(tmp_path: Path) -> None:
    """A fallback forge really declined to apply is still classified.

    Detection is field-derived from the run's own transport telemetry: a
    fallback-eligible transport failure (``transport_fallback_reason``) where no
    fallback ran (``transport_fallback_fired: false``).
    """
    d = _sprint_dir(tmp_path, name="real-fallback")
    _write(
        d / "sprint-summary.yaml",
        _summary(
            [
                {
                    "slug": "issue-300",
                    "outcome": "ESCALATE",
                    "error": "escalated",
                    "verdict": "REQUEST_CHANGES",
                }
            ]
        ),
    )
    _write(
        d / "issue-300" / "audit.yaml",
        {
            "iterations": {
                "dev_loop": [
                    {
                        "iteration": 1,
                        "transport_fallback_fired": False,
                        "transport_fallback_reason": None,
                        "transport_used": "cli",
                    },
                    {
                        "iteration": 2,
                        "transport_fallback_fired": False,
                        "transport_fallback_reason": "cli_quota_exhausted",
                        "transport_used": "cli",
                    },
                ]
            }
        },
    )

    entry = _build(d)["stories"]["issue-300"]
    assert entry["primary_failure_class"] == "review_rejected"
    assert "fallback_not_applied" in entry["contributing_factors"]
    hit = next(ev for ev in entry["evidence"] if ev["rule_id"] == "provider_fallback_not_applied")
    assert hit["source"].endswith("audit.yaml")
    assert "cli_quota_exhausted" in hit["excerpt"]
    assert any("wire the provider fallback" in a for a in entry["recommended_next_actions"])


def test_applied_provider_fallback_is_not_recorded_as_unapplied(tmp_path: Path) -> None:
    """A fallback that fired (even into a later failure) is not "not applied"."""
    d = _sprint_dir(tmp_path, name="fired-fallback")
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-301", "outcome": "FAILED", "error": "boom"}]),
    )
    _write(
        d / "issue-301" / "audit.yaml",
        {
            "iterations": {
                "dev_loop": [
                    {
                        "iteration": 1,
                        "transport_fallback_fired": True,
                        "transport_fallback_reason": "cli_quota_exhausted",
                        "transport_used": "api",
                    }
                ]
            }
        },
    )

    entry = _build(d)["stories"]["issue-301"]
    assert "fallback_not_applied" not in entry["contributing_factors"]
    assert not any("wire the provider fallback" in a for a in entry["recommended_next_actions"])


def test_unclassified_story_drops_text_derived_contributing_factors(tmp_path: Path) -> None:
    """An unexplained failure does not also assert a confident text-scan amplifier.

    ``unknown_needs_rca`` means forge could not classify the run; a contributing
    factor resting only on a text scan — and the remediation derived from it —
    must not be emitted alongside that admission.
    """
    d = _sprint_dir(tmp_path, name="unknown-contributing")
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-302", "outcome": "FAILED", "error": "unexplained"}]),
    )
    (d / "issue-302").mkdir(parents=True, exist_ok=True)
    (d / "run-6c83b3061455.log").write_text(
        "Pending decision timed out after 10m 0s — auto-escalating for #302\n",
        encoding="utf-8",
    )

    entry = _build(d)["stories"]["issue-302"]
    assert entry["primary_failure_class"] == UNKNOWN_CLASS
    assert entry["contributing_factors"] == []
    # The evidence trail still records what fired, so the trace is not lost.
    assert "pending_decision_auto_rejected" in {ev["rule_id"] for ev in entry["evidence"]}
    assert not any("operator decision gate" in a for a in entry["recommended_next_actions"])


def test_structured_contributing_factor_survives_unknown_primary(tmp_path: Path) -> None:
    """Field-derived amplifiers are the run's own facts, not an inference."""
    d = _sprint_dir(tmp_path, name="unknown-structured-contributing")
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-303", "outcome": "FAILED", "error": "unexplained"}]),
    )
    _write(
        d / "issue-303" / "audit.yaml",
        {
            "iterations": {
                "dev_loop": [
                    {
                        "iteration": 1,
                        "transport_fallback_fired": False,
                        "transport_fallback_reason": "cli_quota_exhausted",
                    }
                ]
            }
        },
    )

    entry = _build(d)["stories"]["issue-303"]
    assert entry["primary_failure_class"] == UNKNOWN_CLASS
    assert "fallback_not_applied" in entry["contributing_factors"]
    assert any("wire the provider fallback" in a for a in entry["recommended_next_actions"])


def test_fallback_seam_coordinator_audit_to_rca(tmp_path: Path) -> None:
    """Seam: real dev telemetry → generate_audit_log → RCA fallback classification.

    Verifies the cause code is anchored to the field the coordinator actually
    writes, not to a field name this test invented.
    """
    from coord_test_helpers import _make_config, _make_task

    from theforge.coordinator.audit import generate_audit_log
    from theforge.coordinator.state import (
        CoordinatorResult,
        CoordinatorState,
        DevIterationTelemetry,
        Phase,
    )

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=1, review_cycle=0)
    state.phase = Phase.ESCALATE
    state.error = "dev agent failed"
    state.dev_iteration_telemetry = [
        DevIterationTelemetry(
            iteration=1,
            max_iterations=3,
            cost_usd=1.0,
            duration_s=5.0,
            cycle=0,
            gate_result="FAIL",
            transport_fallback_fired=False,
            transport_fallback_reason="cli_quota_exhausted",
            transport_used="cli",
        )
    ]

    audit = generate_audit_log(
        config,
        task,
        CoordinatorResult(success=False, phase=Phase.ESCALATE, state=state, message=state.error),
    )

    d = _sprint_dir(tmp_path, name="seam-fallback")
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-304", "outcome": "MERGE_FAILED", "error": "conflict"}]),
    )
    _write(d / "issue-304" / "audit.yaml", audit)

    entry = _build(d)["stories"]["issue-304"]
    assert entry["primary_failure_class"] == "merge_failed"
    assert "fallback_not_applied" in entry["contributing_factors"]


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
    assert payload["ruleset_version"] == 5


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
    assert before["ruleset_version"] == 5

    # Improved rule set (v6): a new rule now recognises the previously-unknown
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
    monkeypatch.setattr(rca_mod, "RULESET_VERSION", 6)

    write_sprint_rca(d, overwrite=True)
    after = read_sprint_rca(d)
    assert after["stories"]["issue-5"]["primary_failure_class"] == "flooble_fault"
    assert after["ruleset_version"] == 6
    # Same inputs, different rule set → distinguishable analyses.
    assert before["ruleset_version"] != after["ruleset_version"]


def test_cached_already_done_structured_outcome_vetoes_historical_intake_prose(
    tmp_path: Path,
) -> None:
    """Historical intake prose echoed into preflight artifacts must not outrank
    the current run's structured terminal outcome."""
    empty_worktree_err = (
        "Gate exited PASS but branch has no commits ahead of base — "
        "treating empty worktree as missing-work failure"
    )
    d = _sprint_dir(tmp_path, name="already-done-veto")
    _write(
        d / "sprint-summary.yaml",
        _summary(
            [
                {
                    "slug": "issue-1904",
                    "outcome": "FAILED",
                    "error": empty_worktree_err,
                    "preflight": "ALREADY_DONE",
                    "preflight_original_verdict": "ALREADY_DONE",
                }
            ],
            run_id="47acc396fb8f",
        ),
    )
    _write(
        d / "issue-1904" / "audit.yaml",
        {
            "error": empty_worktree_err,
            "outcome": {"message": empty_worktree_err},
            "preflight": {
                "verdict": "ALREADY_DONE",
                "original_verdict": "ALREADY_DONE",
                "failure_action": "terminal cached preflight verdict accepted",
            },
        },
    )
    (d / "issue-1904" / "preflight-raw.log").write_text(
        "Historical note from issue body: dropped after fix on a different host weeks ago.\n",
        encoding="utf-8",
    )
    (d / "issue-1904" / "preflight.yaml").write_text(
        "issue_body: |\n  This story previously dropped_after_fix elsewhere.\n",
        encoding="utf-8",
    )

    entry = _build(d)["stories"]["issue-1904"]
    assert entry["primary_failure_class"] == UNKNOWN_CLASS
    assert not any(action.startswith("reshape") for action in entry["recommended_next_actions"])
    assert any("diagnose" in action for action in entry["recommended_next_actions"])
    assert {ev["rule_id"] for ev in entry["evidence"]} >= {
        "intake_dropped_after_fix",
        "captured_outcome",
    }


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


def test_stranded_prior_generation_worktree_is_distinct_from_launch_collision(
    tmp_path: Path,
) -> None:
    """Issue #1838: a worktree stranded by a prior generation classifies as
    ``sprint_state_stranded`` (priority over ``launch_collision``) with a
    reconcile-oriented action — not flattened into a fresh collision."""
    from theforge.sprint.launch_guard import REASON_STRANDED_WORKTREE

    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary(
            [
                {
                    "slug": "issue-1823",
                    "outcome": "DROPPED",
                    "drop_reason": REASON_STRANDED_WORKTREE,
                }
            ]
        ),
    )
    entry = _build(d)["stories"]["issue-1823"]
    assert entry["primary_failure_class"] == "sprint_state_stranded"
    actions = " ".join(entry["recommended_next_actions"]).lower()
    assert "reconcile" in actions or "resume" in actions
    # The reconcile action must warn against clearing the worktree and
    # re-sprinting fresh (which would discard partial work).
    assert "do not" in actions or "not clear" in actions or "discard" in actions


# ── Engine: stale review vs terminal dev-handoff gate-evidence failure ────────


def test_stale_review_after_dev_gate_evidence_failure(tmp_path: Path) -> None:
    """A later dev iteration that ends at HANDOFF_NO_GATE_EVIDENCE is the terminal
    cause — a prior REQUEST_CHANGES against a now-superseded commit must not win.

    Mirrors HDP #94/#220: gate PASS, cycle-1 REQUEST_CHANGES, then a review-fix
    dev iteration that terminates without gate evidence before re-review.
    """
    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary(
            [
                {
                    "slug": "issue-94",
                    "outcome": "ESCALATE",
                    "verdict": "REQUEST_CHANGES",
                    "error": (
                        "Dev handoff claims completion (acceptance criteria MET) without "
                        "gate PASS evidence"
                    ),
                }
            ]
        ),
    )
    _write(
        d / "issue-94" / "audit.yaml",
        {
            "reviews": [{"cycle": 1, "verdict": "REQUEST_CHANGES"}],
            "iterations": {
                "dev_loop": [
                    {"cycle": 0, "gate_result": "PASS"},
                    {"cycle": 1, "gate_result": "HANDOFF_NO_GATE_EVIDENCE"},
                ]
            },
        },
    )
    entry = _build(d)["stories"]["issue-94"]
    assert entry["primary_failure_class"] == "dev_gate_evidence_missing"
    rule_ids = {ev["rule_id"] for ev in entry["evidence"]}
    assert "dev_handoff_no_gate_evidence" in rule_ids
    assert "review_changes_requested" not in rule_ids
    # Output must make clear the latest commit was not reviewed.
    excerpt = next(
        ev["excerpt"]
        for ev in entry["evidence"]
        if ev["rule_id"] == "dev_handoff_no_gate_evidence"
    )
    assert "not reviewed" in excerpt
    assert "stale review" in excerpt
    assert any("latest commit" in a and "reviewed" in a for a in entry["recommended_next_actions"])


def test_first_iteration_gate_evidence_failure_without_review(tmp_path: Path) -> None:
    """A dev-handoff gate-evidence failure with no prior review still classifies as
    dev_gate_evidence_missing (not UNKNOWN) and omits the stale-review note."""
    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-95", "outcome": "ESCALATE"}]),
    )
    _write(
        d / "issue-95" / "audit.yaml",
        {"iterations": {"dev_loop": [{"cycle": 0, "gate_result": "HANDOFF_NO_GATE_EVIDENCE"}]}},
    )
    entry = _build(d)["stories"]["issue-95"]
    assert entry["primary_failure_class"] == "dev_gate_evidence_missing"
    excerpt = next(
        ev["excerpt"]
        for ev in entry["evidence"]
        if ev["rule_id"] == "dev_handoff_no_gate_evidence"
    )
    assert "stale review" not in excerpt


def test_genuine_terminal_review_rejection_still_classifies(tmp_path: Path) -> None:
    """A terminal REQUEST_CHANGES with no later un-reviewed dev iteration (the dev
    passed its gate; review rejected on content) stays classified review_rejected."""
    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary([{"slug": "issue-96", "outcome": "ESCALATE", "verdict": "REQUEST_CHANGES"}]),
    )
    _write(
        d / "issue-96" / "audit.yaml",
        {
            "reviews": [
                {"cycle": 1, "verdict": "REQUEST_CHANGES"},
                {"cycle": 2, "verdict": "REQUEST_CHANGES"},
            ],
            "iterations": {
                "dev_loop": [
                    {"cycle": 0, "gate_result": "PASS"},
                    {"cycle": 1, "gate_result": "PASS"},
                ]
            },
        },
    )
    entry = _build(d)["stories"]["issue-96"]
    assert entry["primary_failure_class"] == "review_rejected"
    rule_ids = {ev["rule_id"] for ev in entry["evidence"]}
    assert "review_changes_requested" in rule_ids
    assert "dev_handoff_no_gate_evidence" not in rule_ids


def test_dev_gate_evidence_seam_coordinator_audit_to_rca(tmp_path: Path) -> None:
    """Seam: drive the real coordinator audit writer (dev telemetry → audit.yaml),
    then the RCA engine off that audit.

    The gate_result of the terminal dev iteration flows from
    DevIterationTelemetry through generate_audit_log's ``iterations.dev_loop`` into
    RCA classification — the exact cross-phase boundary this fix depends on.
    """
    from coord_test_helpers import _make_config, _make_task

    from theforge.coordinator.audit import generate_audit_log
    from theforge.coordinator.state import (
        CoordinatorResult,
        CoordinatorState,
        DevIterationTelemetry,
        Phase,
        ReviewCycleMetadata,
    )
    from theforge.review import ReviewFinding, ReviewResult

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=2, review_cycle=1)
    state.phase = Phase.ESCALATE
    state.error = (
        "Dev handoff claims completion (acceptance criteria MET) without gate PASS evidence"
    )
    state.review_cycle_metadata = [
        ReviewCycleMetadata(pool_models=[], successful=[], failed=[], synthesized=False)
    ]
    state.review_results = [
        ReviewResult(
            verdict="REQUEST_CHANGES",
            summary="found a real P1",
            findings=[
                ReviewFinding(severity="P1", file="f.py", line=1, observed="bug", suggestion=None)
            ],
            story_matches=False,
            story_mismatches=[],
            test_adequate=True,
            test_gaps=[],
            parse_errors=[],
            raw_yaml={},
        )
    ]
    state.dev_iteration_telemetry = [
        DevIterationTelemetry(
            iteration=1,
            max_iterations=3,
            cost_usd=1.0,
            duration_s=5.0,
            cycle=0,
            gate_result="PASS",
        ),
        DevIterationTelemetry(
            iteration=2,
            max_iterations=3,
            cost_usd=0.5,
            duration_s=3.0,
            cycle=1,
            gate_result="HANDOFF_NO_GATE_EVIDENCE",
        ),
    ]

    audit = generate_audit_log(
        config,
        task,
        CoordinatorResult(success=False, phase=Phase.ESCALATE, state=state, message=state.error),
    )

    d = _sprint_dir(tmp_path, name="seam-gate-evidence")
    _write(d / "sprint-summary.yaml", _summary([{"slug": "issue-220", "outcome": "ESCALATE"}]))
    _write(d / "issue-220" / "audit.yaml", audit)

    entry = _build(d)["stories"]["issue-220"]
    assert entry["primary_failure_class"] == "dev_gate_evidence_missing"


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


def test_merge_failed_check_failure_guidance_names_the_checks(tmp_path: Path) -> None:
    """Issue #1946: a PR abandoned decided-red must not be told to resolve a
    merge conflict or raise an iteration budget — it has evidence against both."""
    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary(
            [
                {
                    "slug": "issue-1934",
                    "outcome": "MERGE_FAILED",
                    "error": (
                        "Queued PR required checks failed (gate): https://github.com/x/y/pull/1943"
                    ),
                }
            ]
        ),
    )
    entry = _build(d)["stories"]["issue-1934"]
    actions = " ".join(entry["recommended_next_actions"]).lower()

    assert entry["primary_failure_class"] == "merge_failed"
    assert "required checks" in actions
    assert "merge conflict" not in actions
    assert "iteration budget" not in actions


def test_merge_failed_timeout_guidance_points_at_the_wait_budget(tmp_path: Path) -> None:
    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary(
            [
                {
                    "slug": "issue-9",
                    "outcome": "MERGE_FAILED",
                    "error": "Queued PR timed out after 3600s: https://github.com/x/y/pull/9",
                }
            ]
        ),
    )
    actions = " ".join(_build(d)["stories"]["issue-9"]["recommended_next_actions"]).lower()

    assert "merge_wait_timeout_seconds" in actions
    assert "merge conflict" not in actions


def test_merge_failed_conflict_guidance_preserved(tmp_path: Path) -> None:
    d = _sprint_dir(tmp_path)
    _write(
        d / "sprint-summary.yaml",
        _summary(
            [
                {
                    "slug": "issue-10",
                    "outcome": "MERGE_FAILED",
                    "error": "merge conflict in src/theforge/sprint/runner.py",
                }
            ]
        ),
    )
    actions = " ".join(_build(d)["stories"]["issue-10"]["recommended_next_actions"]).lower()

    assert "resolve the merge conflict" in actions
