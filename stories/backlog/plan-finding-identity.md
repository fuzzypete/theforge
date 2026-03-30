---
name: "Plan-Finding Identity"
slug: "plan-finding-identity"
pytest_target: tests/
---

# Plan-Finding Identity

## Problem

When the forge coordinator tracks findings across review cycles (and eventually across plan regenerations), it needs a stable way to determine whether two findings refer to the same underlying issue. Today there is no identity mechanism — findings are compared only by position or raw text, which breaks under paraphrase, model variation, reordering, and plan regeneration. Without stable identity, the coordinator cannot detect recurring issues, track resolution, or suppress duplicates, leading to review churn and lost context across cycles.

## Requirements

### Structural Anchor Extraction

The system must extract structural anchors from existing review finding fields (`file`, `description`). Structural anchors are specificity-bearing tokens — references concrete enough to identify a single artifact or decision. No new schema fields are added for MVP; identity is derived entirely from fields that already exist. The `suggestion` field is excluded from anchor extraction — it contains fix recommendations that vary across reviewers and add noise to identity matching.

Anchor classes (MVP):
- **File paths / filenames with extension** — e.g. `coordinator.py`, `src/theforge/config.py`
- **Multi-segment snake_case identifiers** (≥2 segments) — e.g. `load_config`, `strict_auth`
- **Multi-segment camelCase identifiers** (≥2 segments) — e.g. `loadConfig`, `checkAgentAuth`
- **Dotted paths / attribute chains** — e.g. `config.profiles.dev`, `state.plan_attempt_metadata`

Non-anchors (excluded):
- Single-segment words, even technical ones (`config`, `auth`, `schema`, `validation`)
- Common verbs and nouns (`add`, `check`, `parameter`, `function`)
- Bare numbers or line numbers (unstable across plan versions)
- Plan-step labels (`Step 3`, `Phase 2`) — handled separately

### Anchor-First Matching

Two findings may only be considered a match if they share at least one structural anchor. File-path-only overlap is not sufficient — a file path must be paired with at least one other anchor (identifier or dotted path) to constitute a match, since the same file often contains multiple unrelated issues. Non-file anchors (multi-segment identifiers, dotted paths) are sufficient on their own.

Prose similarity (keyword overlap, Jaccard, or equivalent) may be used to rank or break ties among candidates that already share a structural anchor, but it must never be the sole basis for a match. When no structural anchor overlap exists between two findings, the system must abstain rather than guess.

### Abstain Path

When the matching process cannot find a safe match (no shared structural anchor), it must emit an explicit "unmatched" result rather than silently dropping or force-matching the finding. Unmatched findings are preserved as new, independent items. The coordinator must handle unmatched findings without error.

### Plan-Section References as Secondary Context

References to plan sections (e.g., section titles, step numbers) may contribute to match confidence as a secondary signal but must never serve as the sole or primary identity key. If plan-section references are the only overlap between two findings, the system must treat them as unmatched.

### Match Provenance

Every match decision must be logged with the anchors that contributed to the match and whether prose similarity was used as a tie-breaker. Every abstain decision must be logged with the reason (no shared anchors). This log must be available for post-hoc inspection without requiring an LLM to interpret.

### Determinism

Given identical inputs (two sets of findings), the matching algorithm must produce identical output every time. No randomness, no model calls, no non-deterministic ordering dependencies.

### Emergent Taxonomy

The system must not impose a fixed issue-class taxonomy. If categorization is needed in the future, it should be derived from observed anchor patterns after sufficient data exists. MVP does not classify findings into predefined categories.

## Acceptance Criteria

- [ ] Given two findings that reference the same file path and at least one shared identifier or dotted path, the system reports them as a match
- [ ] Given two findings that reference the same file path but share no other anchor, the system reports them as unmatched
- [ ] Given two findings with identical semantic meaning but completely different wording and no shared structural anchor, the system reports them as unmatched (abstain)
- [ ] Given two findings that share only a plan-section reference and no structural anchor, the system reports them as unmatched
- [ ] Given two findings that share a structural anchor and have different prose, the system reports them as a match with the shared anchor logged in provenance
- [ ] Given a finding with no extractable structural anchors, the system marks it as unmatched without error
- [ ] Running the same matching inputs twice produces identical output
- [ ] Match provenance output includes: the anchor(s) that caused the match, whether prose similarity contributed, and for abstentions the reason no match was made
- [ ] No new fields are added to the review finding schema for this change
- [ ] The coordinator handles unmatched findings by preserving them as independent items without halting or erroring

## Notes

This story was informed by a multi-model deliberation (codex, gemini, deepseek). The following design questions were resolved during review:

- **No new schema fields for MVP.** Prove extraction from existing fields first.
- **No prose-only fallback.** At least one structural anchor is always required. Abstaining is safer than a false-positive match that triggers premature backtrack.
- **No fixed taxonomy.** Passive observation only. Clustering can come later once there's enough data to know what clusters look like.
- **Anchor specificity defined by structure, not frequency.** Multi-segment identifiers (≥2 segments for snake_case/camelCase) are anchors. Single-segment words are not. File paths are anchors but not sufficient alone. See Structural Anchor Extraction for the full definition.
- **Abstain path emits "unmatched."** Unmatched findings are preserved as new independent items. The coordinator handles them without error.
- **`suggestion` excluded from anchor extraction.** `FindingRecord` does not persist `suggestion`, so anchors extracted from it would be asymmetric (available for current findings but not prior-cycle findings). The `description` field carries the substantive content.
- **No migration needed for existing `finding_id` hashes.** This story targets plan review finding matching, which is new. The existing dev review `finding_registry` uses sha256-hash IDs and is unaffected. If anchor-based identity later replaces dev review hashing, migration would be a separate story.
