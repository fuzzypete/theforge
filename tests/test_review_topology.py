"""Unit tests for the deterministic topology-walk detector (#2372).

The detector separates a review loop that is *converging* from one that is
inventorying a surface: resolving each cycle's finding correctly and then
discovering the same concern somewhere new. Its contract is asymmetric — a
missed walk costs one more development cycle, a false walk halts a change that
was about to finish — so most of these tests are about the cases where it must
stay silent.
"""

from __future__ import annotations

from dataclasses import dataclass

from theforge.review import ReviewFinding
from theforge.review_finding_classifier import classify_families
from theforge.review_topology import detect_topology_walk


@dataclass
class _Record:
    """Minimal FindingRecord stand-in (the detector reads by duck typing)."""

    severity: str
    disposition: str
    cycle_first_seen: int
    cycle_last_seen: int


def _finding(file: str, line: int | None, description: str, severity: str = "P1") -> dict:
    return {"file": file, "line": line, "description": description, "severity": severity}


def _review_finding(f: dict) -> ReviewFinding:
    return ReviewFinding(
        severity=f["severity"],
        file=f["file"],
        line=f["line"],
        observed=f["description"],
        suggestion=None,
    )


def _build(cycles: list[list[dict]]) -> tuple[list[tuple[int, list[dict]]], list[dict]]:
    """Run the real family classifier over per-cycle snapshots.

    Returns ``(review_cycle_findings, finding_trajectory)`` exactly as the
    coordinator would hold them, so the detector is exercised against the real
    upstream shapes rather than a hand-authored trajectory store.
    """
    snapshots: list[tuple[int, list[dict]]] = []
    store: list[dict] = []
    for i, findings in enumerate(cycles, start=1):
        snapshots.append((i, findings))
        if i >= 2:
            store, _surviving = classify_families(
                current_findings=[_review_finding(f) for f in findings],
                current_cycle=i,
                trajectory_store=store,
                prior_cycle_findings=[
                    (num, [_review_finding(f) for f in prior]) for num, prior in snapshots[:-1]
                ],
            )
    return snapshots, store


# The real run from the spec: each cycle resolved the prior finding and named a
# different path under one invariant.
_TOPOLOGY_WALK = [
    [
        _finding(
            "src/routing/dispatch.py",
            10,
            "unpriced_dispatch: seated primaries are dispatched without a price lookup",
        )
    ],
    [
        _finding(
            "src/routing/fallback.py",
            22,
            "unpriced_dispatch: fallback_models are dispatched without a price lookup",
        )
    ],
    [
        _finding(
            "src/routing/transport.py",
            44,
            "unpriced_dispatch: transport_fallback is dispatched without a price lookup",
        )
    ],
]


def _detect(cycles: list[list[dict]], registry=(), review_cycle: int | None = None):
    snapshots, store = _build(cycles)
    return detect_topology_walk(
        trajectory_cycle=len(cycles),
        review_cycle_findings=snapshots,
        finding_trajectory=store,
        finding_registry=registry,
        review_cycle=len(cycles) if review_cycle is None else review_cycle,
    )


class TestDetectsTheWalk:
    def test_real_example_shape_is_detected(self):
        signal = _detect(_TOPOLOGY_WALK)

        assert signal is not None
        assert signal["pattern"] == "topology_walk"
        assert signal["seed_anchor"] == "unpriced_dispatch"
        assert signal["cycles"] == [1, 2, 3]
        # The evidence names WHERE each cycle fired — the decision is about the
        # sequence of locations, not about the latest finding.
        assert [item["file"] for item in signal["sequence"]] == [
            "src/routing/dispatch.py",
            "src/routing/fallback.py",
            "src/routing/transport.py",
        ]
        assert "unpriced_dispatch" in signal["rationale"]

    def test_flat_count_of_one_per_cycle_still_fires(self):
        """A flat count that reflects discovery is caught: detection rests on
        what the findings say and where they are, never on the count."""
        signal = _detect(_TOPOLOGY_WALK)
        assert signal is not None
        assert all(len(c) == 1 for c in _TOPOLOGY_WALK)

    def test_detection_point_is_the_third_cycle_not_the_last(self):
        """Firing at cycle 3 of a 5-cycle ceiling is the whole value: a detector
        that can only fire on the last cycle reports what already happened."""
        assert _detect(_TOPOLOGY_WALK[:2]) is None
        assert _detect(_TOPOLOGY_WALK) is not None


class TestStaysSilentOnConvergence:
    def test_residual_finding_at_the_same_location_is_not_a_walk(self):
        """The spec's contrast case: cycle 2 is a residual of cycle 1's findings,
        at the same location. The count is falling, not flat."""
        cycles = [
            [
                _finding("src/a.py", 10, "unpriced_dispatch: primaries lack a price lookup"),
                _finding("src/a.py", 20, "unpriced_dispatch: retries lack a price lookup"),
                _finding("src/a.py", 30, "unpriced_dispatch: fallbacks lack a price lookup"),
            ],
            [_finding("src/a.py", 10, "unpriced_dispatch: primaries still lack a price lookup")],
            [_finding("src/a.py", 10, "unpriced_dispatch: primaries lack a price lookup here")],
        ]
        assert _detect(cycles) is None

    def test_unresolved_predecessor_on_the_registry_blocks_detection(self):
        """A P1 the classifier still calls ``unresolved`` means the change did
        NOT fix what it was shown — that is convergence residue, not a walk."""
        registry = [
            _Record(
                severity="P1",
                disposition="unresolved",
                cycle_first_seen=1,
                cycle_last_seen=3,
            )
        ]
        assert _detect(_TOPOLOGY_WALK, registry=registry) is None
        # …and the same trajectory with the predecessor recorded as resolved does fire.
        resolved = [
            _Record(
                severity="P1",
                disposition="net_new",
                cycle_first_seen=3,
                cycle_last_seen=3,
            )
        ]
        assert _detect(_TOPOLOGY_WALK, registry=resolved) is not None

    def test_sparse_history_does_not_fire(self):
        assert _detect(_TOPOLOGY_WALK[:1]) is None
        assert _detect(_TOPOLOGY_WALK[:2]) is None

    def test_cycle_with_no_p1_does_not_fire(self):
        cycles = list(_TOPOLOGY_WALK[:2]) + [
            [
                _finding(
                    "src/routing/transport.py",
                    44,
                    "unpriced_dispatch: transport_fallback naming nit",
                    severity="P2",
                )
            ]
        ]
        assert _detect(cycles) is None


class TestStaysSilentWhenUncertain:
    def test_file_path_only_overlap_is_not_a_shared_concern(self):
        """ "The same file keeps having problems" is not one invariant. Unrelated
        descriptions in one file form no family, so nothing spans the window."""
        cycles = [
            [_finding("src/a.py", 10, "the retry_budget is never reset")],
            [_finding("src/a.py", 20, "logging omits the run identifier")],
            [_finding("src/a.py", 30, "the merge lock is acquired twice")],
        ]
        assert _detect(cycles) is None

    def test_two_families_spanning_the_window_is_ambiguous(self):
        """Several concerns in flight at once is not the single-invariant
        signature the detector is allowed to claim."""
        cycles = [
            [
                _finding("src/a.py", 1, "unpriced_dispatch: primaries lack a price lookup"),
                _finding("src/b.py", 1, "stale_manifest: the seat manifest is not refreshed"),
            ],
            [
                _finding("src/c.py", 2, "unpriced_dispatch: fallback_models lack a price lookup"),
                _finding("src/d.py", 2, "stale_manifest: the pool manifest is not refreshed"),
            ],
            [
                _finding("src/e.py", 3, "unpriced_dispatch: transport lacks a price lookup"),
                _finding("src/f.py", 3, "stale_manifest: the alias manifest is not refreshed"),
            ],
        ]
        assert _detect(cycles) is None

    def test_no_family_anchor_at_all_does_not_fire(self):
        cycles = [
            [_finding("src/a.py", 1, "documentation is missing")],
            [_finding("src/b.py", 2, "an assertion is too weak")],
            [_finding("src/c.py", 3, "a name reads poorly")],
        ]
        assert _detect(cycles) is None

    def test_repeated_location_under_one_anchor_does_not_fire(self):
        """Same anchor, same place, three times running is a stuck finding —
        the loop is failing to fix it, not discovering siblings."""
        cycles = [
            [_finding("src/a.py", 10, "unpriced_dispatch: primaries lack a price lookup A")],
            [_finding("src/a.py", 10, "unpriced_dispatch: primaries lack a price lookup B")],
            [_finding("src/a.py", 10, "unpriced_dispatch: primaries lack a price lookup C")],
        ]
        assert _detect(cycles) is None


class TestCounterSeparation:
    def test_registry_check_uses_review_cycle_not_trajectory_cycle(self):
        """``trajectory_cycle`` and ``review_cycle`` are different counters —
        the registry's cycle fields are numbered in the latter, and a run whose
        review_cycle has been decremented must still be read correctly."""
        # An unresolved P1 active in review_cycle 2 (while trajectory_cycle is 3).
        registry = [
            _Record(severity="P1", disposition="unresolved", cycle_first_seen=1, cycle_last_seen=2)
        ]
        assert _detect(_TOPOLOGY_WALK, registry=registry, review_cycle=2) is None
        # The same record is inert once the review cycle has moved past it.
        signal = _detect(_TOPOLOGY_WALK, registry=registry, review_cycle=3)
        assert signal is not None
        assert signal["review_cycle"] == 3
        assert signal["trajectory_cycle"] == 3
