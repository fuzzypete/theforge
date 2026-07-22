"""Gate-delegation exception for the unproven-completion cross-field rule (#1871).

A review-fix iteration delegates gate execution to the coordinator, which runs
the authoritative gate itself. Such a handoff legitimately marks criteria MET
without self-reporting ``gate_result: PASS``. When the handoff explicitly marks
``gate_delegated: true`` the completion claim is no longer treated as unproven —
but only a strictly boolean ``True`` marker counts, and callers that own
authoritative delegation knowledge (the coordinator dev-phase guard) can opt out
of honoring the self-reported marker entirely.
"""

from __future__ import annotations

from theforge.schemas import dev_handoff_claims_unproven_completion, validate_dev_handoff


def _completion(*, gate_result: str | None = None, gate_delegated=None) -> dict:
    data: dict = {
        "summary": "Fixed the review findings.",
        "commits": [{"sha": "abc1234", "message": "fix(x): address review"}],
        "acceptance_criteria": [
            {"criterion": "It works", "status": "MET", "notes": "fixed and covered"}
        ],
        "story_deviations": "none",
        "deferred_items": "none",
    }
    if gate_result is not None:
        data["gate_result"] = gate_result
    if gate_delegated is not None:
        data["gate_delegated"] = gate_delegated
    return data


class TestUnprovenCompletionDelegation:
    def test_ordinary_met_without_pass_is_unproven(self):
        # No delegation marker → ordinary handoff → completion is unproven.
        assert dev_handoff_claims_unproven_completion(_completion(gate_result=None)) is True

    def test_ordinary_met_with_blocked_is_unproven(self):
        assert dev_handoff_claims_unproven_completion(_completion(gate_result="BLOCKED")) is True

    def test_delegated_met_without_pass_is_proven(self):
        # Review-fix delegation marker present → not unproven; coordinator gates it.
        data = _completion(gate_result=None, gate_delegated=True)
        assert dev_handoff_claims_unproven_completion(data) is False

    def test_delegated_met_with_blocked_is_proven(self):
        # The delegated agent may report BLOCKED (it did not run the gate).
        data = _completion(gate_result="BLOCKED", gate_delegated=True)
        assert dev_handoff_claims_unproven_completion(data) is False

    def test_delegated_with_gate_pass_is_still_proven(self):
        # PASS alone already proves it; delegation marker is redundant but fine.
        data = _completion(gate_result="PASS", gate_delegated=True)
        assert dev_handoff_claims_unproven_completion(data) is False

    def test_malformed_string_marker_is_not_honored(self):
        # A truthy-but-not-boolean marker is malformed and must not be honored —
        # strict validation, not "any truthy value means delegated".
        data = _completion(gate_result=None, gate_delegated="true")
        assert dev_handoff_claims_unproven_completion(data) is True

    def test_malformed_integer_marker_is_not_honored(self):
        data = _completion(gate_result=None, gate_delegated=1)
        assert dev_handoff_claims_unproven_completion(data) is True

    def test_false_marker_is_not_delegation(self):
        data = _completion(gate_result=None, gate_delegated=False)
        assert dev_handoff_claims_unproven_completion(data) is True

    def test_honor_flag_off_ignores_self_reported_marker(self):
        # The coordinator guard passes honor_gate_delegation=False and relies on
        # its own authoritative delegation knowledge, so a self-reported marker
        # cannot bypass the check for an ordinary iteration.
        data = _completion(gate_result=None, gate_delegated=True)
        assert dev_handoff_claims_unproven_completion(data, honor_gate_delegation=False) is True

    def test_honor_flag_off_still_proven_when_gate_pass(self):
        # PASS still proves completion regardless of the honor flag.
        data = _completion(gate_result="PASS", gate_delegated=True)
        assert dev_handoff_claims_unproven_completion(data, honor_gate_delegation=False) is False

    def test_non_completion_handoff_never_trips(self):
        data = _completion(gate_result=None)
        data["acceptance_criteria"][0]["status"] = "PARTIAL"
        assert dev_handoff_claims_unproven_completion(data) is False


class TestValidateDevHandoffDelegationConsistency:
    """validate_dev_handoff shares the cross-field rule (schemas.py) so a
    delegated handoff must NOT be flagged as a structural completion-without-gate
    error there either (plan-review P2 #1)."""

    def test_ordinary_completion_without_pass_still_flagged(self):
        errors = validate_dev_handoff(_completion(gate_result=None))
        assert any("MET but gate_result is not PASS" in e for e in errors)

    def test_delegated_completion_without_pass_not_flagged(self):
        errors = validate_dev_handoff(_completion(gate_result=None, gate_delegated=True))
        assert not any("MET but gate_result is not PASS" in e for e in errors)
        # And the handoff is otherwise structurally valid.
        assert errors == []

    def test_delegated_completion_with_blocked_not_flagged(self):
        errors = validate_dev_handoff(_completion(gate_result="BLOCKED", gate_delegated=True))
        assert not any("MET but gate_result is not PASS" in e for e in errors)

    def test_malformed_delegation_marker_still_flagged(self):
        errors = validate_dev_handoff(_completion(gate_result=None, gate_delegated="yes"))
        assert any("MET but gate_result is not PASS" in e for e in errors)
