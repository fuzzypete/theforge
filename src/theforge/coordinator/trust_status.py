"""Trust-status vocabulary and mechanical derivation for per-run records.

ADR-0006 clause 4 ("tainted runs don't teach") requires v0.13 routing to
exclude runs that failed their own trust checks from aggregates. This module
owns the machine-readable marker that makes that possible: an enumerable
``trust_status`` plus structured ``trust_checks`` entries on the native per-run
audit record.

Kept deliberately dependency-light (stdlib only) so foundational callers — the
audit writer, the substrate reader-side migration, and any future trust-check
producer — can import the vocabulary and the derivation without pulling in
runtime dependencies. Producers of individual check results (e.g. the reviewer
tree-currency verifier) live where they have the evidence to compute them; this
module only defines the vocabulary and the aggregate derivation over their
results.
"""

from __future__ import annotations

from typing import Callable, Iterable, TypeVar

_T = TypeVar("_T")

# ── Trust-status vocabulary ──────────────────────────────────────────────
# Aggregate status of a run, derived mechanically from its trust_checks.
TRUST_TRUSTED = "trusted"
TRUST_TAINTED = "tainted"
TRUST_UNCHECKED = "unchecked"

ALLOWED_TRUST_STATUSES: frozenset[str] = frozenset({TRUST_TRUSTED, TRUST_TAINTED, TRUST_UNCHECKED})

# ── Trust-check result vocabulary ────────────────────────────────────────
# Result of a single applicable trust check. Only PASS/FAIL are applicable
# results — a run type with no implemented check emits no entry at all rather
# than a third "skipped" result (absence of a proof is not taint, ADR-0006
# clause 4).
CHECK_PASS = "pass"
CHECK_FAIL = "fail"

ALLOWED_CHECK_RESULTS: frozenset[str] = frozenset({CHECK_PASS, CHECK_FAIL})


def make_trust_check(
    *,
    check: str,
    result: str,
    producer: str,
    evidence: dict | None = None,
) -> dict:
    """Build one structured trust-check entry.

    ``check`` names the verification (e.g. ``reviewer_tree_currency``);
    ``result`` is :data:`CHECK_PASS` or :data:`CHECK_FAIL`; ``producer``
    identifies the coordinator-owned code that computed it; ``evidence`` is the
    observed values that drove the result (expected/current commits, etc.). The
    result is validated so a producer cannot smuggle an out-of-vocabulary value
    into the record.
    """
    if result not in ALLOWED_CHECK_RESULTS:
        raise ValueError(f"trust-check result {result!r} not in {sorted(ALLOWED_CHECK_RESULTS)}")
    return {
        "check": check,
        "result": result,
        "producer": producer,
        "evidence": evidence or {},
    }


def derive_trust_status(trust_checks: Iterable[dict] | None) -> str:
    """Derive the aggregate ``trust_status`` from a run's trust-check entries.

    Mechanical rule (ADR-0006 clause 4):

    - any affirmative failed check → :data:`TRUST_TAINTED`;
    - at least one applicable check (a ``pass``) with no failures →
      :data:`TRUST_TRUSTED`;
    - no applicable check at all → :data:`TRUST_UNCHECKED`.

    ``unchecked`` is the default for run types that have no trust check yet and
    is admissible for routing — taint requires an affirmative failed check, not
    the absence of a proof. Entries whose result is neither ``pass`` nor
    ``fail`` are treated as not-applicable and never yield ``trusted`` on their
    own.
    """
    if not trust_checks:
        return TRUST_UNCHECKED
    has_applicable = False
    for entry in trust_checks:
        if not isinstance(entry, dict):
            continue
        result = entry.get("result")
        if result == CHECK_FAIL:
            return TRUST_TAINTED
        if result == CHECK_PASS:
            has_applicable = True
    return TRUST_TRUSTED if has_applicable else TRUST_UNCHECKED


# ── Centralized routing taint gate (ADR-0006 clause 4, #1852) ─────────────
# One admissibility rule, applied by every router-consumed aggregate and
# profile lookup so "tainted runs don't teach" is enforced in exactly one
# place rather than re-derived per call site. The rule is intentionally
# asymmetric: only an affirmative ``tainted`` status excludes a run. A missing
# status (legacy records that predate the marker), ``trusted``, and
# ``unchecked`` are all admissible — taint requires a failed check, not the
# absence of a proof.


def is_tainted(trust_status: str | None) -> bool:
    """Return True only when ``trust_status`` is the affirmative taint marker.

    Missing/``None``, :data:`TRUST_TRUSTED`, and :data:`TRUST_UNCHECKED` are all
    non-tainted. This is the single predicate every routing consumer routes its
    admissibility decision through.
    """
    return trust_status == TRUST_TAINTED


def is_routing_admissible(trust_status: str | None) -> bool:
    """Return True when a run may carry routing weight (i.e. is not tainted).

    The inverse of :func:`is_tainted`, named for the call sites that read it as
    an inclusion test. Trusted, unchecked, and missing-status runs are all
    admissible (ADR-0006 clause 4).
    """
    return not is_tainted(trust_status)


def filter_tainted_records(
    records: Iterable[_T],
    *,
    status_of: Callable[[_T], str | None] | None = None,
    status_key: str = "trust_status",
) -> tuple[list[_T], int]:
    """Split ``records`` into (admissible, excluded-for-taint count).

    Every router-consumed history/aggregate loader routes its records through
    this one helper so the exclusion rule and the reported count can never
    drift from each other. ``status_of`` extracts the trust status from a
    record; when omitted, records are treated as mappings and the status is read
    from ``status_key`` (defaulting to ``"trust_status"``). Records whose status
    :func:`is_tainted` are dropped from the returned list and counted; all others
    are preserved in order. The source records are never mutated — filtering is a
    read-time gate (ADR-0002 refusal-to-forget), never a deletion.
    """
    admissible: list[_T] = []
    excluded = 0
    for record in records:
        if status_of is not None:
            status = status_of(record)
        elif isinstance(record, dict):
            status = record.get(status_key)
        else:
            status = None
        if is_tainted(status):
            excluded += 1
        else:
            admissible.append(record)
    return admissible, excluded
