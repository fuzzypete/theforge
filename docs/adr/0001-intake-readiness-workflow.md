# ADR-0001: Intake Readiness Workflow

- **Status:** Proposed
- **Date:** 2026-05-10
- **Deciders:** Peter Wickersham (project lead), with iterative review by Claude and Codex
- **Affected milestones:** v0.11.x (implementation), v0.12+ (dependency)
- **Related issues:** #1034, #367, #1450, #1469, #1033

---

## ADR convention preamble

This is the first ADR in TheForge. The convention established here:

- **Location:** `docs/adr/`
- **Filename:** `NNNN-kebab-slug.md`, zero-padded sequence number starting at `0001`
- **Status lifecycle:** `Proposed` → `Accepted` → (optionally) `Superseded by ADR-NNNN`
- **Scope:** Architectural rationale that outlives any single issue or PR. Per `CONVENTIONS.md`,
  ADRs are sanctioned for "structural rationale." Conventional rules that follow from an
  ADR's decision belong in `CONVENTIONS.md` once the decision ships, not in the ADR itself.

---

## Context

Today, "is this issue ready for development?" is answered only at sprint entry, by the
shape gate, in the form of a refusal. There is no first-class operator surface for the
companion question: "this is half-formed — make it ready." Every issue filed under
pressure (incident-time, mid-sprint discovery, fresh capture) takes the same loop:

1. File issue (best-effort body).
2. Run a sprint that includes it.
3. Shape gate refuses on format/type/diagnosis grounds.
4. Operator hand-edits the issue body.
5. Retry sprint.

The mechanical guard at step 3 is correct — refusal preserves trust. But it has no
companion that *ascends* a draft toward ready. The trust property gets paid for in
operator friction every single time an issue is filed.

A related gap appears mid-sprint: when running sprints surface new bugs, those new
bugs hit the same loop. The friction compounds during incident response, which is
exactly when the operator has least bandwidth for ceremony.

The architectural root cause is that "is this ready?" and "let's implement this" have
collapsed into the same operator decision. Sprint entry is the only place readiness
is enforced *and* the only place readiness can be (clumsily) created.

## Decision

### Headline principle

> **Sprint entry is the final readiness check, not the primary place readiness is created.**

Readiness becomes a deliberate pre-sprint operator workflow with its own commands.
Sprint entry retains the refusal-capable gate but stops being the place where work
gets shaped or repaired.

### Command taxonomy

Five distinct verbs, each answering a single question:

| Command | Question it answers | Status |
|---|---|---|
| `forge todo` (capture) | Preserve rough prose without ceremony. | Shipped (v0.9.0) |
| `forge shape` | Decide ontology and decomposition. What *kind* of work is this? | New, v0.11.x |
| `forge diagnose` | Investigate symptom bugs. What is the cause? | Shipped (MVP) |
| `forge groom` | Make a typed issue shape-gate-clean. | New, v0.11.x |
| `forge triage` | Classify findings / ambiguous backlog. | Existing epic #1033, v0.12+ |
| `forge sprint` | Run ready work. | Shipped; remains the *final* check, not a producer. |

Each command is operator-driven. None auto-invokes another. The shape gate emits
typed verdicts (see below) so an operator (or in later versions, an automation
layer) knows which command to run, but the gate itself does not spend or mutate.

### Per-type readiness paths

Different issue types traverse different subsets of the pipeline:

- **Bug:** `capture → shape → diagnose → groom → ready → sprint`
- **Feature / enhancement / story:** `capture → shape → groom → ready → sprint`
- **Operator-action (#1469):** `capture → shape → ready` (no `groom`; does not enter dev pipeline)
- **Documentation:** `capture → shape → groom → ready → sprint`
- **ADR-candidate:** `capture → shape → human writes ADR → optional follow-up implementation
  issue → that issue follows the bug or feature path`
- **Duplicate / stale:** `capture → shape → close with reason` (operator confirms close)

`forge groom` does **not** internally call `forge diagnose`. A bug missing diagnosis
is refused with reason "needs diagnosis — run `forge diagnose` first." Each command
stays operator-driven; producer chains are explicit.

### Adopted diagnosis vocabulary

Adopt the three-state bug-diagnosis taxonomy from #1450 verbatim:

1. **No diagnosis present** — symptom-only. Not sprintable. Route to `forge diagnose`.
2. **Diagnosis exists, cause unknown** — symptom documented, hypotheses ruled out, confirmed
   cause honestly "not yet identified." Investigation-ready, not implementation-ready.
   This is a first-class state, not a refusal.
3. **Diagnosis with confirmed cause** — implementation-ready. Sprint may proceed.
   Review must verify the *symptom* no longer reproduces (per #1446).

Current shape gate collapses (1) and (2), forcing operators to fake (3). The new
vocabulary gives operators a way to be honest about "I don't know yet" — which is the
refusal-capability property the rest of the system depends on.

### Minimum-useful typed verdicts (v0.11.x scope)

The shape gate's verdict surface in v0.11.x is bounded to this list. Anything beyond
is deferred to later work on the #1450 router epic.

```
needs_type
needs_diagnosis
diagnosis_cause_unknown
needs_grooming_missing_ac
needs_grooming_missing_example
needs_grooming_scope_split
needs_operator_action
adr_candidate
duplicate_or_stale
```

Each verdict maps to a single recommended operator command and (in time) a single
producer agent if/when auto-routing ships.

### `forge shape` MVP behavior

- **Input:** rough prose, `todo:draft` body, or any untyped issue.
- **Output (confident):** classify to one type from the v0.11.x list. Propose the
  type as an issue label and body restructure. Operator applies with `--apply` or
  reviews diff and applies via `gh`.
- **Output (uncertain):** keep as `todo:draft`, emit structured ambiguity questions
  for the operator to answer.
- **Epic decomposition:** **single-level proposal only**. May suggest child stories
  in prose form. Does NOT auto-create child issues in v0.11.x.
- **ADR-candidate handling:** proposes file path and title; does NOT auto-write the
  ADR. Human writes durable architecture artifacts.

Invariant: `forge shape` may propose, classify, split, or refuse. It must not
pretend ambiguous prose is runnable work.

### `forge groom` MVP behavior

- **Input:** a typed issue (bug, feature, story, docs).
- **Output:** issue body restructured to be shape-gate-clean for its type. For bugs,
  requires diagnosis exists (any state). For features/stories, fills missing AC sections
  and concrete examples.
- **Refuses with reason** when prerequisites aren't met (e.g., bug without diagnosis).
- Does NOT invoke `forge diagnose` or `forge shape` automatically.

## Mid-sprint workflow (v0.11.x)

When a sprint surfaces a new bug, the operator's workflow is:

```
gh issue create ...                  # capture
forge shape <issue>                  # classify (often produces type immediately)
forge diagnose <issue>               # if bug and needs diagnosis
forge groom <issue>                  # ready repair
gh issue edit <issue> --add-label ready
```

The running sprint is **not** modified. The newly-ready issue becomes eligible for
the next sprint via normal selection. "Queue for next sprint" means the `ready`
label is applied — no new `forge queue` command is introduced in v0.11.x.

If an explicit queue command is later useful as ergonomics, it can be added in
v0.12+ as a wrapper around the same convention.

## Inline intake remediation posture

The existing `intake.grooming: true` flag in `forge.yaml` is **kept as an opt-in
fallback**, not deprecated. It serves operators who skipped pre-sprint grooming
and is the safety net for incident-time pressure.

When inline remediation fires, the log must explicitly state:

```
[forge] Intake remediation ran at sprint entry for #N.
[forge] Intended workflow: run `forge groom N` before sprint selection.
```

This turns inline remediation into training wheels rather than magic. The default
value of `intake.grooming` flips to `false` once `forge groom` ships (solo-operator
memo: no migration ramp).

## Out of scope for v0.11.x

Explicitly deferred to later milestones, not lost:

- **Live sprint injection** — running sprints accepting new groomed issues. Belongs
  to autonomy work; v0.12+ at earliest.
- **Shape gate auto-invoking producers** — second half of #1450 router epic.
- **Multi-issue creation by `forge shape`** — epic → children, story-set splits.
  Single-level proposal only in v0.11.x.
- **Full triage lifecycle (#1033)** — keep at v0.12.0.
- **Full typed-verdict taxonomy** — only the nine-item v0.11.x list lands now.
- **`forge queue` command** — handled via the `ready` label convention; revisit
  later if ergonomics warrant.

## Roadmap mapping

- **v0.9.0 (shipped):** `forge todo` capture surface.
- **v0.10.0 (in progress):** no intake architecture changes. Trust theme stays
  focused on sprint determinism.
- **v0.11.x — intake-readiness slice:**
  1. Minimum-useful typed verdicts in shape gate (vocabulary)
  2. `forge shape` MVP (classification, internal clarification Q&A)
  3. `forge groom` MVP (readiness repair for typed issues)
  4. Mid-sprint workflow documented and exercised
  5. Inline remediation posture documented; default flipped to off
- **v0.12.0+:** full #1450 router (auto-invoke producers, complete verdict taxonomy),
  `forge triage` (#1033), `forge shape` multi-issue decomposition. Autonomy work
  in this milestone *depends on* v0.11.x intake-readiness slice being usable.
- **v0.13+:** adaptive payoff (per existing release sequencing).

## Consequences

### Positive

- Operator pain pattern (file → refused → hand-edit → retry) eliminated.
- Trust gradient through readiness becomes visible and adjustable.
- Autonomous filing (v0.12+) gets a working autonomous grooming substrate to build on.
- "Honest 'I don't know yet'" state for bugs unblocks the false-confidence loop
  documented in #1450.
- Sprint entry simplifies — no longer the place where mutations / spend get triggered
  by half-formed work.

### Negative

- v0.11.x grows by three new top-level commands (`shape`, `groom` net-new; existing
  `diagnose` continues). CLI surface expansion is real cost.
- Operators must learn the per-type pipeline. Initial discovery cost is a
  documentation problem (`docs/guides/authoring.md` updates).
- The slice is wide enough that scope creep is the dominant risk. Treat the v0.11.x
  list above as a hard fence.

### Risks

- **Scope creep into router automation.** Mitigated by explicit out-of-scope list above.
- **`forge shape` produces low-confidence classifications and operator overrides every
  one.** Mitigated by uncertain-output behavior (keep as `todo:draft`, surface ambiguity
  rather than force classification).
- **Inline remediation continues to be the de-facto path.** Mitigated by explicit
  log message and default flip. Monitor adoption after v0.11.x ships.

## References

- **#1034** — Epic: forge groom (will be promoted from v0.12.0 to v0.11.x and reframed)
- **#367** — forge story → reframed as `forge shape` per this ADR
- **#1450** — Epic: Shape gate becomes a router. Three-state diagnosis taxonomy adopted
  verbatim. Router auto-invocation split out to v0.12+.
- **#1469** — Epic: Operator-action issue type
- **#1033** — Epic: forge triage (kept at v0.12.0)
- **#1446** — Symptom-side mechanical verification (the "review verifies symptom not
  hypothesized cause" discipline that the three-state taxonomy enables)
- **CONVENTIONS.md** — ADR sanctioned as durable artifact for structural rationale
- **Mid-sprint intake issue (TBD)** — to be filed against v0.11.x with this ADR as
  the design anchor
