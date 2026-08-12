"""Topology-walk detection over the review-cycle finding trajectory.

A review loop cannot, by counting findings, tell a change that is *converging*
from one that is *walking a topology*: each cycle resolves exactly what it was
shown and the reviewer then finds the same concern in a location nobody had
enumerated. Both shapes produce a small, flat finding count per cycle, so the
loop runs to its ceiling and escalates only once the budget is gone.

What separates them is not the count but the *identity* of the findings:

* converging — cycle N's findings are the residue of cycle N-1's, at the same
  places, and the count falls toward zero;
* walking a topology — cycle N-1's findings are gone (resolved), and cycle N
  raises new findings that share cycle N-1's non-file anchor at a *different*
  location.

This module is the deterministic detector for the second shape. It is pure
stdlib and pure data: it reads snapshots the coordinator already keeps (the
per-cycle finding snapshots, the family trajectory store produced by
``review_finding_classifier.classify_families``, and the finding-registry
dispositions) and returns a plain evidence dict, or ``None``.

It is deliberately conservative. The cost of a false negative is one more
development cycle; the cost of a false positive is halting a change that was
about to finish. Every ambiguity — sparse history, more than one blocking
family, a finding whose family membership cannot be resolved back to a concrete
location, a repeated location, a file-path-only anchor, an unresolved
predecessor still on the registry — returns ``None``.

Only families whose member is a **blocking (P1) finding in every cycle of the
window** can produce a signal. A recurring P2 nit is not why the loop is still
running, so stopping the story over one would halt work for churn that never
blocked it.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

__all__ = ["MIN_FAMILY_SPAN", "detect_topology_walk"]

#: How many consecutive cycles one family must span before the pattern is a
#: pattern. Two cycles is a coincidence: a single fix-and-find-next is exactly
#: what a converging change also looks like. Three is the first cycle at which
#: "resolved the predecessor, found a new sibling" has happened *twice*, which
#: is the earliest point the sequence says anything the second cycle did not.
MIN_FAMILY_SPAN = 3

#: Descriptions stored in the family trajectory store are truncated to this
#: length by ``classify_families``; matching a family member back to its
#: concrete finding uses the same prefix.
_DESC_PREFIX = 200

_WS_RE = re.compile(r"\s+")


def _norm(text: object) -> str:
    return _WS_RE.sub(" ", str(text or "")).strip().lower()


def _is_p1(finding: dict) -> bool:
    return str(finding.get("severity") or "P1").strip().upper() == "P1"


def _location(finding: dict) -> tuple[str, object]:
    """Location key for a finding snapshot: (normalised file, line)."""
    return (_norm(finding.get("file")), finding.get("line"))


def _looks_like_path(anchor: str) -> bool:
    """True when a seed anchor is really a file path rather than a code anchor.

    ``classify_families`` already refuses to seed a family on a ``file_path``
    anchor, so this is a belt-and-braces check: a topology walk is defined by a
    shared *non-file* invariant, and "the same file keeps having problems" is
    not that.
    """
    return "/" in anchor or anchor.endswith((".py", ".md", ".yaml", ".yml", ".json", ".toml"))


def _family_member(
    family: dict,
    cycle: int,
    cycle_findings: Sequence[dict],
) -> dict | None:
    """Resolve the family's recorded description for ``cycle`` to a finding.

    Returns ``None`` when the family has no entry for that cycle, or when the
    recorded description matches zero or more than one finding in the cycle's
    snapshot — either way the concrete location is not knowable, and a detector
    that guesses one would be reporting a location nobody raised.
    """
    cycles = family.get("cycles") or []
    descriptions = family.get("descriptions") or []
    if len(cycles) != len(descriptions):
        return None
    recorded = [descriptions[i] for i, cyc in enumerate(cycles) if cyc == cycle]
    if len(recorded) != 1:
        return None
    wanted = _norm(recorded[0])
    matches = [
        f
        for f in cycle_findings
        if _norm(str(f.get("description") or "")[:_DESC_PREFIX]) == wanted
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def _has_unresolved_predecessor(
    finding_registry: Iterable[Any],
    review_cycle: int,
) -> bool:
    """True when a P1 first seen in an earlier cycle is still ``unresolved`` now.

    ``unresolved`` is the classifier's word for "the same finding is still
    there" — the signature of a change that has *not* fixed its predecessor.
    Its presence is exactly the convergence-residual case this detector must
    not fire on.
    """
    for record in finding_registry:
        severity = str(getattr(record, "severity", "") or "").strip().upper()
        if severity != "P1":
            continue
        if str(getattr(record, "disposition", "") or "") != "unresolved":
            continue
        if getattr(record, "cycle_last_seen", None) != review_cycle:
            continue
        first_seen = getattr(record, "cycle_first_seen", None)
        if isinstance(first_seen, int) and first_seen < review_cycle:
            return True
    return False


def detect_topology_walk(
    *,
    trajectory_cycle: int,
    review_cycle_findings: Sequence[tuple[int, Sequence[dict]]],
    finding_trajectory: Sequence[dict],
    finding_registry: Iterable[Any] = (),
    review_cycle: int = 0,
) -> dict | None:
    """Return topology-walk evidence for the current cycle, or ``None``.

    Args:
        trajectory_cycle: the monotonic trajectory counter for the cycle that
            just merged (``state.trajectory_cycle``).
        review_cycle_findings: ``(trajectory_cycle, [finding_dict])`` snapshots,
            one per merged review cycle (``state.review_cycle_findings``).
        finding_trajectory: the family store from ``classify_families``
            (``state.finding_trajectory``).
        finding_registry: ``FindingRecord``-shaped objects carrying
            ``severity`` / ``disposition`` / ``cycle_first_seen`` /
            ``cycle_last_seen`` (``state.finding_registry``). Read by duck
            typing so this module stays stdlib-only.
        review_cycle: the coordinator's review-cycle counter, which is what the
            registry's cycle fields are numbered in. ``trajectory_cycle`` and
            ``review_cycle`` are different counters and must not be compared to
            each other.

    Returns a plain dict (JSON/YAML-serialisable) describing the family, the
    cycle sequence, the per-cycle locations and descriptions, and the rationale
    — or ``None`` whenever the evidence does not unambiguously say "topology
    walk".
    """
    if trajectory_cycle < MIN_FAMILY_SPAN:
        return None

    by_cycle: dict[int, list[dict]] = {}
    for cycle_num, findings in review_cycle_findings:
        by_cycle[int(cycle_num)] = [f for f in findings if isinstance(f, dict)]

    span = list(range(trajectory_cycle - MIN_FAMILY_SPAN + 1, trajectory_cycle + 1))
    if any(cyc not in by_cycle for cyc in span):
        return None

    # A predecessor finding the classifier still calls `unresolved` is a change
    # that did NOT fix what it was shown — the convergence-residual shape.
    if _has_unresolved_predecessor(finding_registry, review_cycle):
        return None

    # Candidate families: one concern present in every cycle of the window,
    # whose member in every one of those cycles is a BLOCKING (P1) finding.
    #
    # The P1 requirement is what ties the pattern to the reason the loop is
    # still running. A recurring P2 nit can span three cycles perfectly well
    # while the P1s that are actually holding the story are unrelated and
    # converging; escalating on it would stop the story over churn that never
    # blocked anything. Filtering here rather than after the uniqueness check
    # also means such a nit cannot mask a genuine P1 walk running alongside it.
    span_set = set(span)
    candidates: list[tuple[dict, list[tuple[int, dict]]]] = []
    for fam in finding_trajectory:
        if not isinstance(fam, dict) or not span_set.issubset(set(fam.get("cycles") or [])):
            continue
        seed = str(fam.get("seed_anchor") or "").strip()
        if not seed or _looks_like_path(seed):
            continue
        # Resolve each cycle's family member back to a concrete finding. An
        # unresolvable member means the location is not knowable, which
        # disqualifies the family rather than the whole detection.
        members: list[tuple[int, dict]] = []
        for cyc in span:
            member = _family_member(fam, cyc, by_cycle[cyc])
            if member is None or not _is_p1(member):
                members = []
                break
            members.append((cyc, member))
        if members:
            candidates.append((fam, members))

    if len(candidates) != 1:
        # Zero: no single blocking concern spans the window — nothing to name.
        # More than one: several concerns are in flight at once, which is not
        # the single-invariant signature this detector is allowed to claim.
        return None
    family, members = candidates[0]
    seed_anchor = str(family["seed_anchor"]).strip()

    # Every cycle in the window must sit at a DIFFERENT location: that is what
    # "a new sibling path each cycle" means, and it is the whole distinction
    # from a residual restatement of the same finding.
    locations = [_location(m) for _, m in members]
    if len(set(locations)) != len(locations):
        return None
    descriptions = [_norm(m.get("description")) for _, m in members]
    if len(set(descriptions)) != len(descriptions):
        return None

    # Successive cycles must have RESOLVED their predecessor: no P1 location
    # from cycle N-1 may still be raised in cycle N. One carried-over location
    # is enough to make this a convergence story instead.
    for earlier, later in zip(span, span[1:]):
        earlier_locs = {_location(f) for f in by_cycle[earlier] if _is_p1(f)}
        later_locs = {_location(f) for f in by_cycle[later] if _is_p1(f)}
        if earlier_locs & later_locs:
            return None

    sequence = [
        {
            "cycle": cyc,
            "file": member.get("file") or "",
            "line": member.get("line"),
            "description": str(member.get("description") or "")[:_DESC_PREFIX],
        }
        for cyc, member in members
    ]
    cycle_labels = ", ".join(str(c) for c in span)
    return {
        "pattern": "topology_walk",
        "seed_anchor": seed_anchor,
        "cycles": list(span),
        "trajectory_cycle": trajectory_cycle,
        "review_cycle": review_cycle,
        "sequence": sequence,
        "rationale": (
            f"Cycles {cycle_labels} each resolved the previous cycle's findings and then "
            f"raised a new instance of the same concern ({seed_anchor!r}) at a location "
            f"not previously flagged. The finding count is not falling because the change "
            f"is converging; it is flat because a surface nobody enumerated is being "
            f"inventoried one development pass at a time."
        ),
    }
