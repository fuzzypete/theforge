"""Landing evidence is created by a landing, not written about one (#2598).

The model these tests pin has one asymmetry at its centre: an *attempt* may say
anything short of "it landed", and an *assertion* may only be built by naming
what landed where. Everything else here follows from that — write-once
assertions, attempts that never satisfy a landed query, and an absence that
reads as unresolved rather than as failure.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from theforge.coordinator.landing_evidence import (
    ATTEMPT_OUTCOMES,
    LandingEvidenceError,
    build_landing_assertion,
    build_landing_attempt,
    is_landing_assertion,
    landed_run_ids,
    landing_evidence_dir,
    landing_state,
    read_landing_assertion,
    read_landing_attempts,
    write_landing_assertion,
    write_landing_attempt,
)


def _assertion(**overrides: object) -> dict:
    fields = {
        "run_id": "run-1",
        "slug": "issue-1",
        "landing_mode": "merge-pr",
        "target_branch": "main",
        "reviewed_commit": "aaaa111",
        "gated_commit": "aaaa111",
        "carrier_kind": "pull_request",
        "carrier_ref": "#42",
        "landed_commit": "bbbb222",
        "observer": "test",
    }
    fields.update(overrides)  # type: ignore[arg-type]
    return build_landing_assertion(**fields)  # type: ignore[arg-type]


def _attempt(**overrides: object) -> dict:
    fields = {
        "run_id": "run-1",
        "slug": "issue-1",
        "landing_mode": "merge-pr",
        "target_branch": "main",
        "outcome": "queued",
        "observer": "test",
    }
    fields.update(overrides)  # type: ignore[arg-type]
    return build_landing_attempt(**fields)  # type: ignore[arg-type]


# ── The asymmetry ────────────────────────────────────────────────────────


def test_an_attempt_has_no_spelling_of_landed() -> None:
    """The closed outcome set is the enforcement, not a convention."""
    assert "landed" not in ATTEMPT_OUTCOMES
    with pytest.raises(LandingEvidenceError):
        _attempt(outcome="landed")


@pytest.mark.parametrize("outcome", sorted(ATTEMPT_OUTCOMES))
def test_no_attempt_outcome_satisfies_a_landed_query(tmp_path: Path, outcome: str) -> None:
    write_landing_attempt(tmp_path, _attempt(outcome=outcome))
    assert landed_run_ids(tmp_path) == set()
    assert read_landing_assertion(tmp_path, "run-1") is None
    assert landing_state(tmp_path, "run-1") != "landed"


@pytest.mark.parametrize(
    "missing",
    ["reviewed_commit", "gated_commit", "landed_commit", "carrier_ref", "target_branch"],
)
def test_an_assertion_cannot_omit_what_it_claims(missing: str) -> None:
    """A caller who cannot name the landing cannot express one here."""
    with pytest.raises(LandingEvidenceError) as exc:
        _assertion(**{missing: ""})
    assert missing in str(exc.value)


def test_an_assertion_names_reviewed_and_landed_commits_separately() -> None:
    """``merge_strategy: squash`` makes them different SHAs — routinely.

    A model that stored one commit would have to pick, and either choice is
    wrong for half the landings forge performs.
    """
    assertion = _assertion(reviewed_commit="aaaa111", landed_commit="cccc333")
    assert assertion["reviewed_commit"] == "aaaa111"
    assert assertion["landed_commit"] == "cccc333"
    assert assertion["reviewed_commit"] != assertion["landed_commit"]


def test_an_unknown_carrier_kind_is_refused() -> None:
    with pytest.raises(LandingEvidenceError):
        _assertion(carrier_kind="vibes")


# ── Immutability ─────────────────────────────────────────────────────────


def test_an_assertion_is_write_once(tmp_path: Path) -> None:
    """A landing happens once; a second observation cannot rewrite the first."""
    first = write_landing_assertion(tmp_path, _assertion(landed_commit="bbbb222"))
    second = write_landing_assertion(
        tmp_path, _assertion(landed_commit="dddd444", observer="later")
    )
    assert first == second
    stored = json.loads(first.read_text(encoding="utf-8"))
    assert stored["landed_commit"] == "bbbb222"
    assert stored["observer"] == "test"


def test_attempts_accumulate_rather_than_collapse(tmp_path: Path) -> None:
    """A retried landing made two attempts and the record must show both."""
    write_landing_attempt(tmp_path, _attempt(outcome="failed", detail="sibling dirt"))
    write_landing_attempt(tmp_path, _attempt(outcome="queued", detail="retried"))
    attempts = read_landing_attempts(tmp_path, "run-1")
    assert [a["outcome"] for a in attempts] == ["failed", "queued"]


def test_a_malformed_artifact_reads_as_unresolved_not_as_landed(tmp_path: Path) -> None:
    """A corrupt file must not answer a landed query in either direction."""
    directory = landing_evidence_dir(tmp_path)
    directory.mkdir(parents=True)
    (directory / "run-1.landed.json").write_text("{not json", encoding="utf-8")
    assert read_landing_assertion(tmp_path, "run-1") is None
    assert landed_run_ids(tmp_path) == set()
    assert landing_state(tmp_path, "run-1") == "unresolved"


def test_an_assertion_missing_a_field_is_not_evidence(tmp_path: Path) -> None:
    directory = landing_evidence_dir(tmp_path)
    directory.mkdir(parents=True)
    payload = _assertion()
    payload.pop("landed_commit")
    (directory / "run-1.landed.json").write_text(json.dumps(payload), encoding="utf-8")
    assert not is_landing_assertion(payload)
    assert landed_run_ids(tmp_path) == set()


# ── The read model ───────────────────────────────────────────────────────


def test_no_evidence_at_all_is_unresolved(tmp_path: Path) -> None:
    """Not landed, not failed. The old three-state field could not say this."""
    assert landing_state(tmp_path, "run-never-seen") == "unresolved"


def test_a_queued_attempt_is_unresolved_and_a_failed_one_is_not_landed(tmp_path: Path) -> None:
    write_landing_attempt(tmp_path, _attempt(outcome="queued"))
    assert landing_state(tmp_path, "run-1") == "unresolved"
    write_landing_attempt(tmp_path, _attempt(outcome="closed"))
    assert landing_state(tmp_path, "run-1") == "not_landed"


def test_an_assertion_outranks_a_later_negative_attempt(tmp_path: Path) -> None:
    """Once observed, a landing is a fact about the past."""
    write_landing_assertion(tmp_path, _assertion())
    write_landing_attempt(tmp_path, _attempt(outcome="failed"))
    assert landing_state(tmp_path, "run-1") == "landed"
    assert landed_run_ids(tmp_path) == {"run-1"}
