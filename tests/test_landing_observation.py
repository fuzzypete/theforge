"""When an observation may become a positive landing assertion (#2598).

Two properties, both of them about refusing to assert:

*Branch names are reused.* ``forge/issue-42`` may carry a pull request that
merged months ago, and a lookup keyed on the branch alone returns it happily.
Pairing that PR's merge commit with *this* run's reviewed commit fabricates a
landing out of two real facts, which is precisely the class of claim the
evidence model exists to remove. So a merged PR is evidence about this run only
if it demonstrably carried this run's work.

*A landing that does not happen changes nothing about the run.* The run record
is an attestation of what the run did; a failed or timed-out landing is a fact
about the world's response to it. It produces an attempt artifact and leaves the
record alone.

The GitHub boundary is mocked at ``_sh``, so these run with no ``gh`` on PATH.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from coord_test_helpers import _make_config

from theforge.coordinator.landing_evidence import (
    build_landing_attempt,
    landing_state,
    read_landing_assertion,
    read_landing_attempts,
    write_landing_attempt,
)
from theforge.sprint.landing_observation import (
    _merged_pr_commit,
    _pr_carries_commit,
    _same_commit,
    reconcile_landing_evidence,
)

REVIEWED = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
GATED = REVIEWED
STALE_HEAD = "9999999999999999999999999999999999999999"
STALE_MERGE = "0000000000000000000000000000000000000000"
FRESH_MERGE = "5555555555555555555555555555555555555555"


def _pr(**overrides: object) -> dict:
    entry = {
        "number": 7,
        "url": "https://example.test/pr/7",
        "mergeCommit": {"oid": FRESH_MERGE},
        "mergedAt": "2026-08-25T00:00:00Z",
        "headRefOid": REVIEWED,
        "commits": [{"oid": REVIEWED}],
    }
    entry.update(overrides)  # type: ignore[arg-type]
    return entry


# ── Containment ──────────────────────────────────────────────────────────


def test_a_pull_request_that_did_not_carry_the_work_is_not_evidence_for_it() -> None:
    stale = _pr(headRefOid=STALE_HEAD, commits=[{"oid": STALE_HEAD}])
    assert not _pr_carries_commit(stale, {REVIEWED})


def test_a_pull_request_whose_head_is_the_attested_commit_carries_it() -> None:
    assert _pr_carries_commit(_pr(), {REVIEWED})


def test_a_squashed_pull_request_is_still_recognised_by_its_commit_list() -> None:
    """A squash rewrites the head; head-matching alone would reject every one."""
    squashed = _pr(headRefOid=STALE_HEAD, commits=[{"oid": REVIEWED}])
    assert _pr_carries_commit(squashed, {REVIEWED})


def test_an_abbreviated_sha_still_identifies_the_commit() -> None:
    assert _same_commit(REVIEWED[:12], REVIEWED)
    assert _pr_carries_commit(_pr(), {REVIEWED[:12]})


def test_a_sha_too_short_to_identify_anything_does_not_match() -> None:
    """Loose matching here would re-introduce the fabrication by another route."""
    assert not _same_commit(REVIEWED[:4], REVIEWED)


def _gh_dispatcher(entries: list[dict], calls: list[str] | None = None):
    def _sh(cmd: str, cwd: Path, timeout: int = 60) -> tuple[bool, str]:
        if calls is not None:
            calls.append(cmd)
        if cmd.startswith("gh pr list") and "--state merged" in cmd:
            return True, json.dumps(entries)
        if cmd.startswith("gh pr list") and "--state open" in cmd:
            return True, "[]"
        if cmd.startswith("git rev-parse --verify"):
            return False, ""  # no local ref: topology cannot rescue the lookup
        return True, ""

    return _sh


def test_a_stale_merged_pull_request_is_refused_as_the_landed_carrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = _pr(
        headRefOid=STALE_HEAD, commits=[{"oid": STALE_HEAD}], mergeCommit={"oid": STALE_MERGE}
    )
    monkeypatch.setattr("theforge.sprint.landing_observation._sh", _gh_dispatcher([stale]))
    assert _merged_pr_commit(Path("/nowhere"), "forge/issue-42", {REVIEWED}) is None


def test_the_pull_request_that_carried_the_work_is_chosen_over_a_stale_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stale PR merged *later*, so recency alone would pick the wrong one."""
    stale = _pr(
        number=0,
        url="https://example.test/pr/0",
        headRefOid=STALE_HEAD,
        commits=[{"oid": STALE_HEAD}],
        mergeCommit={"oid": STALE_MERGE},
        mergedAt="2027-01-01T00:00:00Z",
    )
    monkeypatch.setattr("theforge.sprint.landing_observation._sh", _gh_dispatcher([stale, _pr()]))
    assert _merged_pr_commit(Path("/nowhere"), "forge/issue-42", {REVIEWED}) == (
        FRESH_MERGE,
        "#7",
        "https://example.test/pr/7",
    )


def test_an_unverifiable_lookup_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """With nothing attested, no PR can be tied to this run — so none is used."""
    monkeypatch.setattr("theforge.sprint.landing_observation._sh", _gh_dispatcher([_pr()]))
    assert _merged_pr_commit(Path("/nowhere"), "forge/issue-42", set()) is None


# ── The seam ─────────────────────────────────────────────────────────────


def _queued_attempt(project_root: Path, run_id: str, slug: str) -> None:
    write_landing_attempt(
        project_root,
        build_landing_attempt(
            run_id=run_id,
            slug=slug,
            landing_mode="merge-pr",
            target_branch="main",
            outcome="timeout",
            source_commit=REVIEWED,
            gated_commit=GATED,
            observer="test",
        ),
    )


def test_reconciliation_leaves_a_run_unresolved_when_only_a_stale_pr_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point, at the seam: unresolved beats a plausible wrong answer."""
    config = _make_config(tmp_path)
    _queued_attempt(tmp_path, "run-1", "issue-42")
    stale = _pr(
        headRefOid=STALE_HEAD, commits=[{"oid": STALE_HEAD}], mergeCommit={"oid": STALE_MERGE}
    )
    monkeypatch.setattr("theforge.sprint.landing_observation._sh", _gh_dispatcher([stale]))

    assert reconcile_landing_evidence(config) == []
    assert read_landing_assertion(tmp_path, "run-1") is None
    assert landing_state(tmp_path, "run-1") == "unresolved"
    assert [a["outcome"] for a in read_landing_attempts(tmp_path, "run-1")] == [
        "timeout",
        "unknown",
    ]


def test_reconciliation_asserts_when_the_pull_request_carried_the_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _make_config(tmp_path)
    _queued_attempt(tmp_path, "run-1", "issue-42")
    monkeypatch.setattr("theforge.sprint.landing_observation._sh", _gh_dispatcher([_pr()]))

    published = reconcile_landing_evidence(config)

    assert [a["run_id"] for a in published] == ["run-1"]
    assertion = read_landing_assertion(tmp_path, "run-1")
    assert assertion is not None
    assert assertion["landed_commit"] == FRESH_MERGE
    assert assertion["reviewed_commit"] == REVIEWED
    assert assertion["carrier_ref"] == "#7"
    assert landing_state(tmp_path, "run-1") == "landed"


# ── The run record is not rewritten into a negative claim ────────────────


def _audit(landing_status: str | None, *, error: str | None = None) -> dict:
    """The audit payload a story's record is generated from."""
    return {
        "run_id": "run-1",
        "task": {"slug": "issue-42"},
        "landing_status": landing_status,
        "landing": {"outcome": error} if error else None,
        "merge": {"merged": landing_status == "landed", "error": error},
        "landing_event": {"landing_status": landing_status} if landing_status else None,
    }


def _record(project_root: Path) -> dict:
    path = project_root / ".forge" / "audits" / "runs" / "run-1.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_failed_landing_does_not_rewrite_the_run_record(tmp_path: Path) -> None:
    """The record says what the run did. The landing failing is not about the run."""
    from theforge.sprint.audit import _write_native_story_record

    _write_native_story_record(tmp_path, _audit("pending_integration"), force_replace=True)
    _write_native_story_record(
        tmp_path, _audit("failed", error="merge conflict"), force_replace=True
    )

    record = _record(tmp_path)
    assert record["landing_status"] == "pending_integration"
    assert record["landing_event"] == {"landing_status": "pending_integration"}
    assert record["merge"] == {"merged": False, "error": None}


def test_a_timed_out_queued_pr_does_not_rewrite_the_run_record(tmp_path: Path) -> None:
    """The wrap-up marks a timed-out queued PR failed; the record must not follow."""
    from theforge.sprint.audit import _write_native_story_record

    _write_native_story_record(tmp_path, _audit("pending_integration"), force_replace=True)
    _write_native_story_record(
        tmp_path, _audit("failed", error="merge wait expired"), force_replace=True
    )

    assert _record(tmp_path)["landing_status"] == "pending_integration"


def test_a_successful_landing_may_still_advance_the_record(tmp_path: Path) -> None:
    """The asymmetry: advanced to a fact, never rewritten into a denial."""
    from theforge.sprint.audit import _write_native_story_record

    _write_native_story_record(tmp_path, _audit("pending_integration"), force_replace=True)
    _write_native_story_record(tmp_path, _audit("landed"), force_replace=True)

    record = _record(tmp_path)
    assert record["landing_status"] == "landed"
    assert record["merge"]["merged"] is True


def test_a_record_that_landed_is_not_later_demoted(tmp_path: Path) -> None:
    """A stray late write must not undo an observed landing either."""
    from theforge.sprint.audit import _write_native_story_record

    _write_native_story_record(tmp_path, _audit("landed"), force_replace=True)
    _write_native_story_record(tmp_path, _audit("failed", error="late noise"), force_replace=True)

    assert _record(tmp_path)["landing_status"] == "landed"


def test_a_first_write_cannot_create_the_negative_claim_either(tmp_path: Path) -> None:
    """A story whose landing fails on its first attempt has no earlier record.

    Guarding only rewrites would let exactly that story's record be *created*
    carrying the negative — the same claim, arrived at by a different route. It
    is demoted to what the record actually knew: a landing was owed.
    """
    from theforge.sprint.audit import _write_native_story_record

    _write_native_story_record(tmp_path, _audit("failed", error="refused"), force_replace=True)

    record = _record(tmp_path)
    assert record["landing_status"] == "pending_integration"
    assert record["merge"] is None
    assert record["landing_event"] is None
