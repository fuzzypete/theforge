"""Assertion-based gate-contradiction classification for review findings.

Pure Python, stdlib-only.  Consumed by ``review_phase`` to decide whether a
passing gate mechanically contradicts a P1 finding.

The distinction this module enforces is between what a finding is *about* and
what it *asserts*.  A passing gate contradicts exactly one class of claim: that
tests / build / lint are currently *failing*.  It has no bearing on a claim that
coverage is inadequate, that acceptance evidence was never produced, or that a
criterion remains undemonstrated — all of which are entirely consistent with a
green gate.  Matching a finding's subject matter (any mention of "tests") rather
than its assertion is what allowed a correct "acceptance evidence is missing" P1
to be suppressed on the authority of a gate that had no bearing on it.

``asserts_gate_verifiable_failure`` therefore returns True only when a finding
positively asserts a gate-verifiable *failure* and carries no signal that it is
in fact an inadequacy / absence claim or an indictment of the gate itself.  It
fails closed: when the assertion cannot be positively established, the finding is
treated as non-contradicted and keeps blocking.
"""

from __future__ import annotations

# ── Positive failure assertions ──────────────────────────────────────────────
# Phrases that assert a gate-verifiable signal (tests, build, lint, compile) is
# currently FAILING — the only claim a PASS gate mechanically disproves.  Each is
# a subject bound to a failure predicate; a bare subject ("test suite") is not
# here, because a green gate says nothing about a finding that merely discusses
# tests.
_GATE_FAILURE_ASSERTIONS: tuple[str, ...] = (
    "test fail",
    "tests fail",
    "test failure",
    "tests are failing",
    "failing test",
    "test is failing",
    "tests failing",
    "build fail",
    "build error",
    "build is broken",
    "broken build",
    "does not compile",
    "doesn't compile",
    "will not compile",
    "won't compile",
    "compilation fail",
    "compilation error",
    "compile fail",
    "compile error",
    "lint fail",
    "lint error",
    "linter fail",
    "linter error",
    "import error",
    "syntax error",
    "0 passed",
    "0 tests pass",
    "zero tests pass",
)

# ── Suppression vetoes ────────────────────────────────────────────────────────
# Signals that a finding — even one that mentions tests or failures — is NOT a
# claim a green gate can contradict.  Two families:
#   1. Inadequacy / absence / insufficiency of coverage or acceptance evidence.
#      A passing gate is entirely consistent with all of these.
#   2. Indictment of the gate/validation mechanism itself.  A finding arguing the
#      oracle is wrong must never be dismissed on the oracle's authority.
# Presence of any veto term forces the fail-closed path (finding keeps blocking).
_SUPPRESSION_VETO: tuple[str, ...] = (
    # inadequacy / absence / insufficiency of coverage
    "inadequate",
    "insufficient",
    "not demonstrated",
    "undemonstrated",
    "never demonstrated",
    "not shown",
    "never shown",
    "not exercised",
    "does not exercise",
    "doesn't exercise",
    "only exercises",
    "only exercise",
    "only tests",
    "only test",
    "mocked",
    "mock",
    "stubbed",
    "stub",
    "patched out",
    "patches out",
    "coverage",
    "missing test",
    "no test",
    "without test",
    "lacks test",
    "does not cover",
    "doesn't cover",
    "not covered",
    "fails to demonstrate",
    "fails to show",
    "not validated",
    "not verified",
    "acceptance",
    "criterion",
    "criteria",
    "within budget",
    # indictment of the gate / validation mechanism itself
    "gate",
    "validation",
    "validate",
    "false pass",
    "reports pass",
    "report pass",
    "reported pass",
    "would pass",
    "still pass",
    "substring",
    "0 failed",
)


def asserts_gate_verifiable_failure(description: str) -> bool:
    """Return True only when *description* asserts a gate-verifiable failure.

    A gate-verifiable failure is a claim that tests, the build, or lint are
    currently failing — the one class of claim a PASS gate mechanically
    contradicts.

    Fails closed: any inadequacy / absence / gate-indictment signal, or the
    absence of a positive failure assertion, yields False, and the caller keeps
    the finding blocking.
    """
    d = description.lower()
    if any(veto in d for veto in _SUPPRESSION_VETO):
        return False
    return any(assertion in d for assertion in _GATE_FAILURE_ASSERTIONS)
