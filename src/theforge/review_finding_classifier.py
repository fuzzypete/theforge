"""Review-finding family classification: anchor extraction and cross-cycle matching.

Adapts the plan_finding_classifier anchor machinery (extract_anchors, Anchor,
MatchResult) for ReviewFinding objects.  The extraction and matching rules are
identical to plan-finding-identity; only the consumer differs (dev fix-prompt
framing instead of plan regen disposition).

Family identity:
- A family is identified by a seed anchor — the lexicographically first
  non-file anchor from the first valid cross-cycle match.
- File-path-only anchor overlap does not create or join a family.
- A single finding in isolation does not start a family.
- When a finding matches multiple families, it joins the one with the longest
  history (most cycles); ties broken alphabetically by seed anchor.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from theforge.plan_finding_classifier import (
    Anchor,
    MatchResult,
    _jaccard_str,
    extract_anchors,
    strip_reviewer_prefix,
)

if TYPE_CHECKING:
    from theforge.review import ReviewFinding

logger = logging.getLogger(__name__)

# File-like path placeholder values that should not be treated as anchors.
_NON_PATH_PLACEHOLDERS: frozenset[str] = frozenset({"unknown", "", "null", "none"})


def extract_review_finding_anchors(finding: "ReviewFinding") -> frozenset[Anchor]:
    """Extract structural anchors from a ReviewFinding.

    Anchors come from:
    - finding.description (via extract_anchors, which internally calls
      strip_reviewer_prefix — harmless for non-prefixed descriptions)
    - finding.file, if it is a non-empty, path-like value (not a placeholder)

    finding.suggestion is intentionally excluded (per spec: anchors come from
    description only).
    """
    anchors: set[Anchor] = set(extract_anchors(finding.description))

    # Add an explicit file-path anchor from the direct file field if usable.
    file_val = (finding.file or "").strip()
    if (
        file_val
        and file_val.lower() not in _NON_PATH_PLACEHOLDERS
        and ("/" in file_val or "." in file_val)
    ):
        anchors.add(Anchor(value=file_val, kind="file_path"))

    return frozenset(anchors)


def match_review_findings(
    current: list["ReviewFinding"],
    prior: list["ReviewFinding"],
) -> list[MatchResult]:
    """Match each current finding against prior findings using anchor-first logic.

    Rules (identical to plan_finding_classifier.match_plan_findings):
    - A match requires ≥1 shared anchor.
    - File-path-only overlap is insufficient: at least one non-file anchor must
      also be shared.
    - Non-file anchors (snake_ident, camel_ident, dotted_path) are sufficient
      on their own.
    - When multiple prior candidates qualify, Jaccard token overlap on descriptions
      is used as a tie-breaker.
    - When no prior candidate qualifies, the finding is unmatched.

    Returns one MatchResult per current finding (in order).
    """
    if not prior:
        return [
            MatchResult(
                current_index=i,
                prior_index=None,
                shared_anchors=(),
                prose_similarity_used=False,
                abstain_reason="no prior findings to match against",
            )
            for i in range(len(current))
        ]

    # Pre-extract anchors for all findings.
    current_anchors = [extract_review_finding_anchors(f) for f in current]
    prior_anchors = [extract_review_finding_anchors(f) for f in prior]
    # Strip any reviewer prefix for prose similarity (harmless no-op for non-prefixed text).
    current_clean = [strip_reviewer_prefix(f.description) for f in current]
    prior_clean = [strip_reviewer_prefix(f.description) for f in prior]

    results: list[MatchResult] = []

    for ci in range(len(current)):
        ca = current_anchors[ci]

        candidates: list[tuple[int, frozenset[Anchor]]] = []
        file_only_priors: list[int] = []

        for pi in range(len(prior)):
            pa = prior_anchors[pi]
            shared: frozenset[Anchor] = ca & pa
            if not shared:
                continue

            non_file_shared = frozenset(a for a in shared if a.kind != "file_path")
            file_shared = frozenset(a for a in shared if a.kind == "file_path")

            if not non_file_shared and file_shared:
                file_only_priors.append(pi)
                continue

            candidates.append((pi, shared))

        if not candidates:
            if not ca:
                abstain_reason = "no extractable structural anchors in current finding"
            elif file_only_priors:
                abstain_reason = "file-path-only overlap — no shared non-file anchor"
            else:
                abstain_reason = "no shared structural anchors"

            results.append(
                MatchResult(
                    current_index=ci,
                    prior_index=None,
                    shared_anchors=(),
                    prose_similarity_used=False,
                    abstain_reason=abstain_reason,
                )
            )
            continue

        if len(candidates) == 1:
            pi, shared = candidates[0]
            results.append(
                MatchResult(
                    current_index=ci,
                    prior_index=pi,
                    shared_anchors=tuple(sorted(shared, key=lambda a: (a.kind, a.value))),
                    prose_similarity_used=False,
                    abstain_reason=None,
                )
            )
        else:
            best_pi, best_shared = max(
                candidates,
                key=lambda c: _jaccard_str(current_clean[ci], prior_clean[c[0]]),
            )
            results.append(
                MatchResult(
                    current_index=ci,
                    prior_index=best_pi,
                    shared_anchors=tuple(sorted(best_shared, key=lambda a: (a.kind, a.value))),
                    prose_similarity_used=True,
                    abstain_reason=None,
                )
            )

    return results


def classify_families(
    current_findings: list["ReviewFinding"],
    current_cycle: int,
    trajectory_store: list[dict],
    prior_cycle_findings: list[tuple[int, list["ReviewFinding"]]],
) -> tuple[list[dict], list[dict]]:
    """Classify current findings into new vs surviving families.

    For each current finding, attempts to match it against findings from each
    prior cycle.  A match (per full anchor-first rules) creates or joins a family.

    Family creation seeds both the prior cycle and current cycle appearance in
    the family's cycles list (spec: "Family created from a cycle-1/cycle-2 match
    must show cycles [1, 2], not just [2]").

    When a finding matches multiple existing families, it joins the one with the
    longest history (most cycles); ties broken alphabetically by seed anchor.

    Args:
        current_findings: findings from the current review cycle.
        current_cycle: trajectory_cycle number for this cycle.
        trajectory_store: the existing per-family store (mutated and returned).
        prior_cycle_findings: list of (trajectory_cycle, [ReviewFinding]) from
            all prior cycles — matched in order, oldest first.

    Returns:
        (updated_trajectory_store, surviving_families)
        surviving_families is the subset of trajectory_store where the family
        has appeared in 2+ distinct cycles AND appears in current_cycle.
    """
    # Build a lookup: seed_anchor -> index into trajectory_store
    seed_to_idx: dict[str, int] = {
        entry["seed_anchor"]: i for i, entry in enumerate(trajectory_store)
    }

    # Build collision-resistant membership map: (cycle, file, line, full_desc) -> frozen_seed.
    # This enforces the frozen-seed identity constraint: when a current finding matches
    # a prior finding that is already in family F, the match can only produce an action
    # for family F — and only if the current shared anchors include F's frozen seed.
    # Without this check, a current finding that shares only a non-seed anchor with a
    # familied prior finding would incorrectly create a new family seeded by that
    # alternate anchor.
    #
    # Keys use (cycle, file, line, full_description) rather than (cycle, description[:200])
    # to avoid collisions when two familied findings from the same cycle share the same
    # 200-character description prefix.
    _cycle_findings_lookup: dict[int, list["ReviewFinding"]] = {
        cycle_num: findings for cycle_num, findings in prior_cycle_findings
    }
    _prior_membership: dict[tuple, str] = {}
    for family in trajectory_store:
        fam_seed = family["seed_anchor"]
        for cyc, stored_desc in zip(family["cycles"], family["descriptions"]):
            cycle_rfs = _cycle_findings_lookup.get(cyc, [])
            matched_rf = next(
                (rf for rf in cycle_rfs if rf.description[:200] == stored_desc), None
            )
            if matched_rf is not None:
                key: tuple = (cyc, matched_rf.file, matched_rf.line, matched_rf.description)
            else:
                key = (cyc, None, None, stored_desc)
            _prior_membership[key] = fam_seed

    # For each current finding, find its best match across all prior cycles.
    # We track the best (seed_anchor, prior_cycle, shared_anchors, prior_finding) per
    # current finding index.
    best_match_for: dict[int, tuple[str, int, tuple[Anchor, ...], "ReviewFinding"]] = {}

    for prior_cycle_num, prior_findings in prior_cycle_findings:
        results = match_review_findings(current_findings, prior_findings)
        for result in results:
            if result.prior_index is None:
                continue

            # Collect all shared non-file anchor values from this match.
            shared_non_file_values = {
                a.value for a in result.shared_anchors if a.kind != "file_path"
            }
            if not shared_non_file_values:
                # File-path-only match — no valid seed, skip family creation
                continue

            ci = result.current_index
            prior_finding = prior_findings[result.prior_index]

            # ── Frozen-seed constraint ──────────────────────────────────────────
            # If the matched prior finding is already in family F, this match is only
            # valid for F — and only when F's frozen seed is among the current shared
            # anchors.  If the frozen seed is absent, skip this match entirely: do NOT
            # create a new family seeded by an alternate anchor present in the match.
            prior_key = (
                prior_cycle_num,
                prior_finding.file,
                prior_finding.line,
                prior_finding.description,
            )
            frozen_seed = _prior_membership.get(prior_key)
            if frozen_seed is not None:
                if frozen_seed not in shared_non_file_values:
                    # Current finding overlaps with a familied prior finding, but not
                    # on the defining identity anchor — leave current finding unfamilied.
                    continue
                effective_seed = frozen_seed
                existing_len = len(set(trajectory_store[seed_to_idx[frozen_seed]]["cycles"]))
            else:
                # Prior finding is not in any family.
                # Find which shared anchors correspond to existing family seeds.
                # A match whose shared anchors include "load_config" must join the
                # "load_config" family (if it exists), not create a new one seeded by
                # a lex-first anchor that happens not to be an existing seed.
                matching_existing_seeds = [v for v in shared_non_file_values if v in seed_to_idx]

                if matching_existing_seeds:
                    # Pick the best existing family: longest history, alphabetical tie-break
                    effective_seed = min(
                        matching_existing_seeds,
                        key=lambda s: (-len(set(trajectory_store[seed_to_idx[s]]["cycles"])), s),
                    )
                    existing_len = len(
                        set(trajectory_store[seed_to_idx[effective_seed]]["cycles"])
                    )
                else:
                    # No existing family matched — lex-first anchor seeds a potential new family
                    effective_seed = min(shared_non_file_values)
                    existing_len = 0

            if ci in best_match_for:
                # Compare to existing best: prefer longer family history, then alpha seed
                _existing_seed, _existing_cycle, _existing_shared, _existing_pf = best_match_for[
                    ci
                ]
                if _existing_seed in seed_to_idx:
                    _existing_fam_len = len(
                        set(trajectory_store[seed_to_idx[_existing_seed]]["cycles"])
                    )
                else:
                    _existing_fam_len = 0

                # Choose longer history; ties broken alphabetically
                if existing_len > _existing_fam_len or (
                    existing_len == _existing_fam_len and effective_seed < _existing_seed
                ):
                    best_match_for[ci] = (
                        effective_seed,
                        prior_cycle_num,
                        result.shared_anchors,
                        prior_finding,
                    )
            else:
                best_match_for[ci] = (
                    effective_seed,
                    prior_cycle_num,
                    result.shared_anchors,
                    prior_finding,
                )

    # Apply the best matches: update or create families
    for ci, (seed, prior_cycle_num, shared_anchors, prior_finding) in best_match_for.items():
        current_finding = current_findings[ci]

        if seed in seed_to_idx:
            # Join existing family
            family = trajectory_store[seed_to_idx[seed]]
            if current_cycle not in family["cycles"]:
                family["cycles"].append(current_cycle)
                family["descriptions"].append(current_finding.description[:200])
        else:
            # Create new family — record BOTH the prior cycle and current cycle appearances
            new_family: dict = {
                "seed_anchor": seed,
                "cycles": [prior_cycle_num, current_cycle],
                "descriptions": [
                    prior_finding.description[:200],
                    current_finding.description[:200],
                ],
            }
            trajectory_store.append(new_family)
            seed_to_idx[seed] = len(trajectory_store) - 1

    # Surviving families: present in 2+ distinct cycles AND in current_cycle
    surviving = [
        entry
        for entry in trajectory_store
        if len(set(entry["cycles"])) >= 2 and current_cycle in entry["cycles"]
    ]

    return trajectory_store, surviving
