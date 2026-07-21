"""Trust-status marker: derivation, tree-currency producer, migration, seam.

Covers issue #1851 — the structured trust-status field on the native per-run
record that promotes reviewer tree-currency (#1826) from prose into
machine-readable telemetry (ADR-0006 clause 4).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from theforge.coordinator import audit_substrate
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.review_context import (
    REVIEWER_TREE_CURRENCY_CHECK,
    REVIEWER_TREE_CURRENCY_PRODUCER,
    evaluate_reviewer_tree_currency,
)
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.coordinator.trust_status import (
    CHECK_FAIL,
    CHECK_PASS,
    TRUST_TAINTED,
    TRUST_TRUSTED,
    TRUST_UNCHECKED,
    derive_trust_status,
    make_trust_check,
)
from theforge.devhandoff import DevHandoff
from theforge.task import TaskStory

# ── Derivation vocabulary ─────────────────────────────────────────────────


def test_derive_trust_status_no_checks_is_unchecked() -> None:
    assert derive_trust_status([]) == TRUST_UNCHECKED
    assert derive_trust_status(None) == TRUST_UNCHECKED


def test_derive_trust_status_pass_only_is_trusted() -> None:
    checks = [make_trust_check(check="c", result=CHECK_PASS, producer="p")]
    assert derive_trust_status(checks) == TRUST_TRUSTED


def test_derive_trust_status_any_fail_is_tainted() -> None:
    checks = [
        make_trust_check(check="a", result=CHECK_PASS, producer="p"),
        make_trust_check(check="b", result=CHECK_FAIL, producer="p"),
    ]
    assert derive_trust_status(checks) == TRUST_TAINTED


def test_make_trust_check_rejects_out_of_vocabulary_result() -> None:
    try:
        make_trust_check(check="c", result="maybe", producer="p")
    except ValueError as exc:
        assert "maybe" in str(exc)
    else:  # pragma: no cover - failure path
        raise AssertionError("expected ValueError for invalid result")


def test_second_check_adds_entry_not_parallel_taint_field() -> None:
    """A second trust check is just another entry; taint derives from all of them."""
    checks = [
        make_trust_check(check=REVIEWER_TREE_CURRENCY_CHECK, result=CHECK_PASS, producer="p"),
        make_trust_check(check="gate_evidence_validity", result=CHECK_FAIL, producer="q"),
    ]
    assert derive_trust_status(checks) == TRUST_TAINTED
    assert {c["check"] for c in checks} == {
        REVIEWER_TREE_CURRENCY_CHECK,
        "gate_evidence_validity",
    }


# ── Reviewer tree-currency producer ────────────────────────────────────────


def _handoff(commits: list[dict]) -> DevHandoff:
    return DevHandoff(
        summary="done",
        commits=commits,
        acceptance_criteria=[],
        story_deviations=[],
        deferred_items=[],
        gate_result="PASS",
        parse_errors=[],
        raw={},
    )


def test_tree_currency_pass_when_commits_reconcile(tmp_path: Path) -> None:
    handoff = _handoff([{"sha": "abc123", "message": "feat: thing"}])
    with (
        patch("theforge.coordinator.review_context._parse_dev_handoff", return_value=handoff),
        patch(
            "theforge.coordinator.review_context._cu._run_shell",
            return_value=(True, "abc123 feat: thing"),
        ),
    ):
        check = evaluate_reviewer_tree_currency(tmp_path, "main")

    assert check is not None
    assert check["check"] == REVIEWER_TREE_CURRENCY_CHECK
    assert check["result"] == CHECK_PASS
    assert check["producer"] == REVIEWER_TREE_CURRENCY_PRODUCER


def test_tree_currency_pass_when_handoff_uses_full_sha(tmp_path: Path) -> None:
    """A full-SHA handoff for the same commit git --oneline abbreviates is clean (#1851)."""
    full_sha = "abc1234567890abcdef1234567890abcdef123456"
    handoff = _handoff([{"sha": full_sha, "message": "feat: thing"}])
    with (
        patch("theforge.coordinator.review_context._parse_dev_handoff", return_value=handoff),
        patch(
            "theforge.coordinator.review_context._cu._run_shell",
            return_value=(True, "abc1234 feat: thing"),
        ),
    ):
        check = evaluate_reviewer_tree_currency(tmp_path, "main")

    assert check is not None
    assert check["result"] == CHECK_PASS
    assert check["evidence"]["missing_from_branch"] == []
    assert check["evidence"]["omitted_from_handoff"] == []
    assert derive_trust_status([check]) == TRUST_TRUSTED


def test_tree_currency_fail_on_stale_checkout(tmp_path: Path) -> None:
    """A handoff whose commits diverge from git history taints the run (#1534)."""
    handoff = _handoff([{"sha": "deadbee", "message": "feat: imaginary commit"}])
    with (
        patch("theforge.coordinator.review_context._parse_dev_handoff", return_value=handoff),
        patch(
            "theforge.coordinator.review_context._cu._run_shell",
            return_value=(True, "abc123 feat: real commit"),
        ),
    ):
        check = evaluate_reviewer_tree_currency(tmp_path, "main")

    assert check is not None
    assert check["result"] == CHECK_FAIL
    assert check["evidence"]["missing_from_branch"] == ["deadbee feat: imaginary commit"]
    assert check["evidence"]["omitted_from_handoff"] == ["abc123 feat: real commit"]
    assert derive_trust_status([check]) == TRUST_TAINTED


def test_tree_currency_not_applicable_without_parseable_handoff(tmp_path: Path) -> None:
    with patch("theforge.coordinator.review_context._parse_dev_handoff", return_value=None):
        assert evaluate_reviewer_tree_currency(tmp_path, "main") is None


# ── Migration v7 → v8 ──────────────────────────────────────────────────────


def test_migrate_v7_to_v8_backfills_unchecked() -> None:
    v7_record = {"task": {"slug": "test"}, "routing_decision": None}

    migrated = audit_substrate._migrate_v7_to_v8(v7_record)

    assert migrated["trust_status"] == TRUST_UNCHECKED
    assert migrated["trust_checks"] == []
    assert migrated["task"] == {"slug": "test"}


def test_migrate_v7_to_v8_is_idempotent_when_fields_present() -> None:
    existing = [make_trust_check(check="c", result=CHECK_PASS, producer="p")]
    v7_record = {"trust_status": TRUST_TRUSTED, "trust_checks": existing}

    migrated = audit_substrate._migrate_v7_to_v8(v7_record)

    assert migrated["trust_status"] == TRUST_TRUSTED
    assert migrated["trust_checks"] == existing


# ── Seam: review/audit boundary ────────────────────────────────────────────


def _make_config(tmp_path: Path):
    from theforge.config import (
        DEFAULT_DEV_PROFILE,
        DEFAULT_PREFLIGHT_PROFILE,
        DEFAULT_REVIEW_PROFILE,
        ForgeConfig,
        RetryPolicy,
        ValidationConfig,
        WorkspaceConfig,
    )

    spec_path = tmp_path / "spec.md"
    spec_path.write_text("# spec", encoding="utf-8")
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=ValidationConfig(gate_command="make gate"),
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(),
    )


def _audit_record(tmp_path: Path, state: CoordinatorState) -> dict:
    config = _make_config(tmp_path)
    task = TaskStory(name="Test", slug="test", story_path=tmp_path / "spec.md")
    state.started_at = "2026-01-01T00:00:00+00:00"
    state.run_id = "deadbeefcafe"
    result = CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="done")
    return generate_audit_log(config, task, result)


def test_stale_review_run_records_tainted_with_entry(tmp_path: Path) -> None:
    """A stale-checkout trust-check failure surfaces as tainted + a named entry."""
    state = CoordinatorState()
    state.trust_checks[REVIEWER_TREE_CURRENCY_CHECK] = make_trust_check(
        check=REVIEWER_TREE_CURRENCY_CHECK,
        result=CHECK_FAIL,
        producer=REVIEWER_TREE_CURRENCY_PRODUCER,
        evidence={"missing_from_branch": ["deadbee x"]},
    )

    record = _audit_record(tmp_path, state)

    assert record["trust_status"] == TRUST_TAINTED
    assert len(record["trust_checks"]) == 1
    entry = record["trust_checks"][0]
    assert entry["check"] == REVIEWER_TREE_CURRENCY_CHECK
    assert entry["producer"] == REVIEWER_TREE_CURRENCY_PRODUCER
    assert entry["result"] == CHECK_FAIL


def test_clean_reviewed_run_records_trusted(tmp_path: Path) -> None:
    state = CoordinatorState()
    state.trust_checks[REVIEWER_TREE_CURRENCY_CHECK] = make_trust_check(
        check=REVIEWER_TREE_CURRENCY_CHECK,
        result=CHECK_PASS,
        producer=REVIEWER_TREE_CURRENCY_PRODUCER,
    )

    record = _audit_record(tmp_path, state)

    assert record["trust_status"] == TRUST_TRUSTED
    assert record["trust_checks"][0]["result"] == CHECK_PASS


def test_run_without_trust_check_is_unchecked_and_admissible(tmp_path: Path) -> None:
    """A run type with no implemented check stays unchecked (routing-admissible)."""
    state = CoordinatorState()

    record = _audit_record(tmp_path, state)

    assert record["trust_status"] == TRUST_UNCHECKED
    assert record["trust_checks"] == []


def test_trust_fields_round_trip_through_substrate(tmp_path: Path) -> None:
    """The marker survives write → sqlite → migrated read at the current version."""
    state = CoordinatorState()
    state.trust_checks[REVIEWER_TREE_CURRENCY_CHECK] = make_trust_check(
        check=REVIEWER_TREE_CURRENCY_CHECK,
        result=CHECK_FAIL,
        producer=REVIEWER_TREE_CURRENCY_PRODUCER,
    )
    record = _audit_record(tmp_path, state)
    audit_substrate.seed_records(tmp_path, [record])

    conn = audit_substrate.require_substrate(tmp_path)
    try:
        loaded = audit_substrate.latest_record_for(conn, run_id="deadbeefcafe")
    finally:
        conn.close()

    assert loaded is not None
    assert loaded["trust_status"] == TRUST_TAINTED
    assert loaded["trust_checks"][0]["check"] == REVIEWER_TREE_CURRENCY_CHECK
