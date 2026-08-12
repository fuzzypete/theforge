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


class TestOnlyBlockingFamiliesCount:
    """A family is only a reason to stop the story if it is why the story is
    still running. A recurring P2 never blocked anything."""

    def test_p2_only_family_does_not_fire_even_with_an_unrelated_p1_present(self):
        # The `naming_convention` nit spans all three cycles at new locations —
        # the topology-walk shape exactly — but it is P2. The P1s that are
        # actually holding the story are unrelated and are NOT walking.
        cycles = [
            [
                _finding("src/x.py", 1, "naming_convention: the helper name reads poorly", "P2"),
                _finding("src/p.py", 1, "the retry_budget is never reset"),
            ],
            [
                _finding("src/y.py", 2, "naming_convention: the fixture name reads poorly", "P2"),
                _finding("src/q.py", 2, "logging omits the run identifier"),
            ],
            [
                _finding("src/z.py", 3, "naming_convention: the constant name reads poorly", "P2"),
                _finding("src/r.py", 3, "the merge lock is acquired twice"),
            ],
        ]
        assert _detect(cycles) is None

    def test_p2_family_running_alongside_a_real_p1_walk_does_not_mask_it(self):
        """Filtering to blocking families happens BEFORE the one-family
        uniqueness check, so P2 churn cannot hide a genuine P1 walk."""
        cycles = [
            [
                _finding("src/x.py", 1, "naming_convention: the helper name reads poorly", "P2"),
                _TOPOLOGY_WALK[0][0],
            ],
            [
                _finding("src/y.py", 2, "naming_convention: the fixture name reads poorly", "P2"),
                _TOPOLOGY_WALK[1][0],
            ],
            [
                _finding("src/z.py", 3, "naming_convention: the constant name reads poorly", "P2"),
                _TOPOLOGY_WALK[2][0],
            ],
        ]
        signal = _detect(cycles)
        assert signal is not None
        assert signal["seed_anchor"] == "unpriced_dispatch"

    def test_a_family_that_turns_p2_mid_window_does_not_fire(self):
        """Every cycle's member must block. A concern downgraded to P2 is a
        concern the loop is no longer running for."""
        cycles = [
            list(_TOPOLOGY_WALK[0]),
            [dict(_TOPOLOGY_WALK[1][0], severity="P2")],
            list(_TOPOLOGY_WALK[2]),
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


class TestUnresolvableBlockingFamilySuppresses:
    """Dropping a family from the candidate set is a claim that it is not a
    reason the loop is running. That claim can only be made when non-blocking is
    *proven* — otherwise a second concern in flight would be silently discounted
    and a two-concern window would read as single-invariant."""

    # One description, raised at two places in the same cycle. The family's
    # stored description therefore resolves to two concrete findings, so which
    # location it means is not knowable.
    _DUP = "stale_manifest: the manifest is not refreshed before dispatch"

    def _window_with_unresolvable_p1_family(self):
        return [
            [_TOPOLOGY_WALK[0][0], _finding("src/b.py", 1, self._DUP)],
            [
                _TOPOLOGY_WALK[1][0],
                _finding("src/d.py", 2, self._DUP),
                _finding("src/e.py", 3, self._DUP),
            ],
            [_TOPOLOGY_WALK[2][0], _finding("src/f.py", 4, self._DUP)],
        ]

    def test_unresolvable_p1_family_suppresses_an_otherwise_valid_walk(self):
        cycles = self._window_with_unresolvable_p1_family()
        # Sanity: both families really do span the window, so this is the
        # two-blocking-concerns case and not an accident of family formation.
        _snapshots, store = _build(cycles)
        spanning = {fam["seed_anchor"] for fam in store if {1, 2, 3}.issubset(set(fam["cycles"]))}
        assert spanning == {"unpriced_dispatch", "stale_manifest"}

        assert _detect(cycles) is None

    def test_the_same_walk_fires_once_the_second_family_is_absent(self):
        """Isolates the suppression to the unresolvable family: remove it and
        the identical `unpriced_dispatch` walk is detected."""
        cycles = [[row[0]] for row in self._window_with_unresolvable_p1_family()]
        signal = _detect(cycles)
        assert signal is not None
        assert signal["seed_anchor"] == "unpriced_dispatch"

    def test_a_spanning_family_whose_description_matches_nothing_suppresses(self):
        """Zero matches is as unknowable as two: non-blocking was not established,
        so the family cannot be discounted."""
        cycles = self._window_with_unresolvable_p1_family()
        _snapshots, store = _build(cycles)
        # Rewrite the second family's cycle-2 description to something no
        # finding carries, reproducing a stored record that resolves to nothing.
        for fam in store:
            if fam["seed_anchor"] == "stale_manifest":
                fam["descriptions"][fam["cycles"].index(2)] = "a description nobody raised"
        assert (
            detect_topology_walk(
                trajectory_cycle=3,
                review_cycle_findings=[(i + 1, c) for i, c in enumerate(cycles)],
                finding_trajectory=store,
                review_cycle=3,
            )
            is None
        )


class TestIdenticalWordingAcrossSiblings:
    """Reviewers routinely describe one invariant in the same words at each place
    it is violated. Location, not prose, is what makes a sibling a sibling."""

    _SAME = "unpriced_dispatch: this path is dispatched without a price lookup"

    def test_identical_descriptions_at_distinct_locations_still_fire(self):
        cycles = [
            [_finding("src/routing/dispatch.py", 10, self._SAME)],
            [_finding("src/routing/fallback.py", 22, self._SAME)],
            [_finding("src/routing/transport.py", 44, self._SAME)],
        ]
        signal = _detect(cycles)
        assert signal is not None
        assert signal["seed_anchor"] == "unpriced_dispatch"
        assert [item["file"] for item in signal["sequence"]] == [
            "src/routing/dispatch.py",
            "src/routing/fallback.py",
            "src/routing/transport.py",
        ]

    def test_identical_descriptions_at_one_location_still_do_not_fire(self):
        """The relaxation is scoped to wording. Same words, same place is a
        finding the loop failed to fix — still a convergence story."""
        cycles = [
            [_finding("src/routing/dispatch.py", 10, self._SAME)],
            [_finding("src/routing/dispatch.py", 10, self._SAME)],
            [_finding("src/routing/dispatch.py", 10, self._SAME)],
        ]
        assert _detect(cycles) is None

    def test_identical_descriptions_within_one_cycle_are_still_unresolvable(self):
        """Two findings sharing wording in the SAME cycle leave the family's
        record pointing at two places, which stays ambiguous."""
        cycles = [
            [_finding("src/routing/dispatch.py", 10, self._SAME)],
            [
                _finding("src/routing/fallback.py", 22, self._SAME),
                _finding("src/routing/retry.py", 33, self._SAME),
            ],
            [_finding("src/routing/transport.py", 44, self._SAME)],
        ]
        assert _detect(cycles) is None
