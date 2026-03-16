---
name: "Richer handoffs — dev→reviewer and reviewer→dev"
slug: richer-handoffs
file_scope:
  - src/theforge/task.py
  - src/theforge/review.py
  - src/theforge/coordinator.py
  - tests/test_task.py
  - tests/test_coordinator.py
pytest_target: tests/
---

# Richer Handoffs (Both Directions)

## Problem

Handoffs between dev and reviewer are thin in both directions, causing
review cycles to degrade into mechanical spec-checking rather than
meaningful code review.

### Dev → Reviewer (currently missing)

The reviewer sees only the diff stat and the spec. It has no visibility
into *why* the dev made specific decisions, what deviations from spec
were intentional, or what trade-offs were considered. This causes
reviewers to flag correct decisions as P1 spec violations — the
`coord_util.py` incident being the canonical example: three review cycles
blocked on a design decision the dev had good reasons for, with no
mechanism to explain those reasons.

In a real PR, the developer writes a description. The reviewer reads it
before looking at the diff. This context changes what gets flagged.

### Reviewer → Dev (currently thin)

The reviewer produces a `ReviewResult` with `summary`, `spec_mismatches`,
`test_gaps`, and `findings`. Only `findings` is passed back to the dev.
The summary that orients the dev on the overall problem is dropped. Spec
compliance failures and test gap analysis — both directly actionable —
are silently discarded.

## Solution

### Part 1: Dev Notes (dev → reviewer)

The dev prompt instructs the agent to write a `dev_notes` section in
`handoff.yaml` before finishing. This section captures:

- What was implemented
- Any intentional spec deviations with justification
- Key design decisions and trade-offs
- What was explicitly deferred and why

The coordinator reads `dev_notes` from `handoff.yaml` and passes it to
`build_review_prompt`. The reviewer sees it before examining the diff.

#### Dev prompt addition

Add to the gate step in `build_dev_prompt`:

```
After running the gate, add a `dev_notes` section to handoff.yaml:

  dev_notes: |
    What was implemented and any spec deviations with justification.
    If you deviated from the spec, explain why — the reviewer will
    read this before looking at the diff. Be specific: cite the spec
    section and your reason. Example: "Spec called for X in coordinator.py
    but I extracted to coord_util.py because 4 modules needed the same
    helper — duplication would be worse than an undocumented module."

This is your voice in the review. Use it.
```

#### Coordinator reads dev_notes

In `_get_handoff_content` (or inline where handoff is read), extract
`dev_notes` from the parsed handoff YAML. Pass it to
`build_review_prompt` as `dev_notes: str | None`.

#### Review prompt addition

Add a **Developer Notes** section before the diff:

```
## Developer Notes

{dev_notes}

Read this before examining the diff. The developer has flagged intentional
decisions and spec deviations here. If a deviation is justified, do NOT
flag it as a spec violation — flag only unjustified or incorrect deviations.
```

If `dev_notes` is absent or empty, omit the section entirely.

### Part 2: Richer Review Feedback (reviewer → dev)

Replace the `findings_to_markdown(parsed_review.findings)` call in
coordinator with a new `review_to_dev_handoff(result: ReviewResult) -> str`
function in `review.py`.

The output is **action-oriented**, not status-oriented. The dev is already
in a retry — it doesn't need the verdict restated, it needs to know what
to fix.

#### `review_to_dev_handoff` output format

```
## Review Summary
{result.summary}

## Spec Compliance Issues        ← only if spec_matches is False
{bullet list of spec_mismatches}

## Missing Test Coverage         ← only if test_adequate is False
{bullet list of test_gaps}

## Findings

### [{severity}] `{file}` (line {line})   ← omit line ref if null
**Issue:** {description}
**Fix:** {suggestion}                     ← omit if suggestion is None

...
```

Sections with no content are omitted entirely. If there are no findings,
emit "No findings." rather than an empty section.

The existing `findings_to_markdown` function is kept for backward
compatibility but no longer called by the coordinator.

## Acceptance Criteria

- [ ] Dev prompt includes `dev_notes` instruction in the gate step
- [ ] Coordinator reads `dev_notes` from parsed handoff YAML
- [ ] `build_review_prompt` accepts `dev_notes: str | None = None`
- [ ] Review prompt includes `## Developer Notes` section when dev_notes present
- [ ] `review_to_dev_handoff(result: ReviewResult) -> str` exists in `review.py`
- [ ] Output includes summary, spec mismatches (if any), test gaps (if any), findings
- [ ] Sections with no content are omitted
- [ ] Coordinator calls `review_to_dev_handoff` instead of `findings_to_markdown`
- [ ] `findings_to_markdown` still exists (backward compat, just unused by coordinator)
- [ ] `run_from_review` path also uses `review_to_dev_handoff`
- [ ] All existing tests pass
- [ ] New tests: `review_to_dev_handoff` with full result, empty findings, no mismatches
- [ ] New tests: `build_review_prompt` with and without `dev_notes`
- [ ] New tests: coordinator passes `dev_notes` from handoff to review prompt
