# Issue shape reference

> Generated from `theforge.shape_check.issue_spec`. Do not edit by hand —
> change the specification and regenerate (the drift test in
> `tests/test_issue_spec.py` fails until the two agree).

This is the whole structural contract: what a well-formed issue of each type
is, which sections it must carry, which it may not, and the states it can
occupy. The checker validates against this same data, so a rule stated here
is the rule the gate enforces.

Two widths of heading recognition are deliberate. A section is *recognized*
generously — a body written `## Observed behavior` is a bug body — but only
the exact spellings listed below are *canonicalized* on output. Recognizing a
spelling is never a licence to rewrite a heading whose extra words carry the
author's meaning.

Content the specification does not model — background, notes, worked examples,
your own headings — is preserved as written. It is never dropped, re-levelled,
or demoted into quoted prose.

## Types

- [`bug`](#bug) — a defect report: what happened, what should have happened, and why
- [`enhancement`](#enhancement) — new or changed behavior, stated as outcomes a reviewer can check
- [`task`](#task) — operator-scoped or documentation-shaped work with a checkable outcome
- [`spike`](#spike) — a chartered question: design work and a validating POC, which closes only on a recorded outcome
- [`epic`](#epic) — a tracking entry grouping runnable children; never dispatched itself
- [`operator-action`](#operator-action) — a deliverable only a human operator can produce; deliberately non-dispatched

---

## `bug`

A defect report: what happened, what should have happened, and why.

### Sections, in canonical order

| Section | Heading | Rule |
| --- | --- | --- |
| Observed | `## Observed` (also recognized: `What happened`) | required — the gate refuses a body without it |
| Expected | `## Expected` (also recognized: `What was expected`) | required — the gate refuses a body without it |
| Steps to reproduce | `## Steps to reproduce` (also recognized: `Reproduction`) | optional — modeled so it renders canonically |
| Diagnosis | `## Diagnosis` (also recognized: `Root cause`) | required — the gate refuses a body without it |
| Acceptance criteria | `## Acceptance criteria` (also recognized: `Done criteria`, `Checklist`) | forbidden — its presence contradicts this type |

### Fields of `## Diagnosis`

Each field is written as a bolded bullet lead-in. The gate matches the
**label** literally (case-insensitive).

- **Observed symptom** — bolded bullet lead-in or heading inside "## Diagnosis"
  Example: - **Observed symptom:** sprint resume false-skips zero-delta APPROVE stories, reporting them merged when no commit landed.
- **Evidence** — bolded bullet lead-in or heading inside "## Diagnosis"
  Example: - **Evidence:** run id `1ff6b0bb7992`, story #1102 — resume log shows the false skip.
- **Confirmed cause** — bolded bullet lead-in or heading; its value may be a specific claim or an honest non-assertion ("unknown", "not yet identified")
  Example: - **Confirmed cause:** `_is_already_merged` requires at least one commit ahead, so a zero-delta APPROVE is misclassified as unmerged.
- **Affected code path** — bolded bullet lead-in or heading naming the module/function
  Example: - **Affected code path:** `sprint.runner._is_already_merged`.
- **Fix-success criterion** — bolded bullet lead-in or heading stating the observable pass condition
  Example: - **Fix-success criterion:** resume identifies a zero-delta APPROVE story as already merged.

### Lifecycle states

| State | Admits implementation | Meaning |
| --- | --- | --- |
| `undiagnosed` | no (`needs_diagnosis`) | symptom-only: no Diagnosis section, so no cause a reviewer could check |
| `investigation_ready` | no (`diagnosis_cause_unknown`) | Diagnosis section complete but the confirmed cause is a non-assertion; the next job is cause discovery, not hypothesized-cause implementation |
| `implementation_ready` | yes | the document satisfies its type's grammar and may enter a sprint |

### Type/shape contradiction

Bugs use observed/expected plus diagnosis.

- Refused on sight: `Acceptance criteria`
- Remediation: remove the feature-style checklist or relabel the issue

---

## `enhancement`

New or changed behavior, stated as outcomes a reviewer can check.

### Sections, in canonical order

| Section | Heading | Rule |
| --- | --- | --- |
| Acceptance criteria | `## Acceptance criteria` (also recognized: `Done criteria`, `Checklist`) | required — the gate refuses a body without it |
| Example | `## Example` (also recognized: `Examples`) | advisory — reported when absent, but it decides nothing |
| Observed | `## Observed` (also recognized: `What happened`) | forbidden — but only as part of the bug-report shape (see below) |
| Expected | `## Expected` (also recognized: `What was expected`) | forbidden — but only as part of the bug-report shape (see below) |
| Steps to reproduce | `## Steps to reproduce` (also recognized: `Reproduction`) | forbidden — its presence contradicts this type |
| Diagnosis | `## Diagnosis` (also recognized: `Root cause`) | forbidden — its presence contradicts this type |

### Lifecycle states

| State | Admits implementation | Meaning |
| --- | --- | --- |
| `ungroomed` | no (`missing_acceptance_criteria`) | no acceptance criteria, so no observable statement of done |
| `implementation_ready` | yes | the document satisfies its type's grammar and may enter a sprint |

### Type/shape contradiction

Enhancement issues use why/acceptance criteria/example, not bug-report sections.

- Refused on sight: `Steps to reproduce`, `Diagnosis`
- Refused only as part of the bug-report shape: `Observed`, `Expected` — a reproduction heading, or a symptom heading paired with an expectation heading, must be present before these count. One of them alone is ordinary prose.
- Remediation: relabel the issue as a bug or rewrite the body to the feature shape

---

## `task`

Operator-scoped or documentation-shaped work with a checkable outcome.

### Sections, in canonical order

| Section | Heading | Rule |
| --- | --- | --- |
| Acceptance criteria | `## Acceptance criteria` (also recognized: `Done criteria`, `Checklist`) | required — the gate refuses a body without it |
| Example | `## Example` (also recognized: `Examples`) | advisory — reported when absent, but it decides nothing |
| Observed | `## Observed` (also recognized: `What happened`) | forbidden — but only as part of the bug-report shape (see below) |
| Expected | `## Expected` (also recognized: `What was expected`) | forbidden — but only as part of the bug-report shape (see below) |
| Steps to reproduce | `## Steps to reproduce` (also recognized: `Reproduction`) | forbidden — its presence contradicts this type |
| Diagnosis | `## Diagnosis` (also recognized: `Root cause`) | forbidden — its presence contradicts this type |

### Lifecycle states

| State | Admits implementation | Meaning |
| --- | --- | --- |
| `ungroomed` | no (`missing_acceptance_criteria`) | no acceptance criteria, so no observable statement of done |
| `implementation_ready` | yes | the document satisfies its type's grammar and may enter a sprint |

### Type/shape contradiction

Task issues use why/acceptance criteria/example, not bug-report sections.

- Refused on sight: `Steps to reproduce`, `Diagnosis`
- Refused only as part of the bug-report shape: `Observed`, `Expected` — a reproduction heading, or a symptom heading paired with an expectation heading, must be present before these count. One of them alone is ordinary prose.
- Remediation: relabel the issue as a bug or rewrite the body to the task shape

---

## `spike`

A chartered question: design work and a validating POC, which closes only on a recorded outcome.

### Sections, in canonical order

| Section | Heading | Rule |
| --- | --- | --- |
| Acceptance criteria | `## Acceptance criteria` (also recognized: `Done criteria`, `Checklist`) | required — the gate refuses a body without it |
| Example | `## Example` (also recognized: `Examples`) | advisory — reported when absent, but it decides nothing |
| Observed | `## Observed` (also recognized: `What happened`) | forbidden — but only as part of the bug-report shape (see below) |
| Expected | `## Expected` (also recognized: `What was expected`) | forbidden — but only as part of the bug-report shape (see below) |
| Steps to reproduce | `## Steps to reproduce` (also recognized: `Reproduction`) | forbidden — its presence contradicts this type |
| Diagnosis | `## Diagnosis` (also recognized: `Root cause`) | forbidden — its presence contradicts this type |

### Lifecycle states

| State | Admits implementation | Meaning |
| --- | --- | --- |
| `ungroomed` | no (`missing_acceptance_criteria`) | no acceptance criteria, so no observable statement of done |
| `implementation_ready` | yes | the document satisfies its type's grammar and may enter a sprint |

### Type/shape contradiction

Spike issues use why/acceptance criteria/example, not bug-report sections.

- Refused on sight: `Steps to reproduce`, `Diagnosis`
- Refused only as part of the bug-report shape: `Observed`, `Expected` — a reproduction heading, or a symptom heading paired with an expectation heading, must be present before these count. One of them alone is ordinary prose.
- Remediation: relabel the issue as a bug or rewrite the body to the spike shape

---

## `epic`

A tracking entry grouping runnable children; never dispatched itself.

### Sections, in canonical order

| Section | Heading | Rule |
| --- | --- | --- |
| Acceptance criteria | `## Acceptance criteria` (also recognized: `Done criteria`, `Checklist`) | required — the gate refuses a body without it |
| Example | `## Example` (also recognized: `Examples`) | advisory — reported when absent, but it decides nothing |
| Observed | `## Observed` (also recognized: `What happened`) | forbidden — but only as part of the bug-report shape (see below) |
| Expected | `## Expected` (also recognized: `What was expected`) | forbidden — but only as part of the bug-report shape (see below) |
| Steps to reproduce | `## Steps to reproduce` (also recognized: `Reproduction`) | forbidden — its presence contradicts this type |
| Diagnosis | `## Diagnosis` (also recognized: `Root cause`) | forbidden — its presence contradicts this type |

### Lifecycle states

| State | Admits implementation | Meaning |
| --- | --- | --- |
| `tracking_only` | no (`epic_or_tracking`) | an entry that groups runnable children; never dispatched itself |

### Type/shape contradiction

Epic issues are tracking entries, not bug-report sections.

- Refused on sight: `Steps to reproduce`, `Diagnosis`
- Refused only as part of the bug-report shape: `Observed`, `Expected` — a reproduction heading, or a symptom heading paired with an expectation heading, must be present before these count. One of them alone is ordinary prose.
- Remediation: relabel the issue as a bug or file runnable child work instead

---

## `operator-action`

A deliverable only a human operator can produce; deliberately non-dispatched.

### Sections, in canonical order

| Section | Heading | Rule |
| --- | --- | --- |
| Acceptance criteria | `## Acceptance criteria` (also recognized: `Done criteria`, `Checklist`) | required — the gate refuses a body without it |

### Lifecycle states

| State | Admits implementation | Meaning |
| --- | --- | --- |
| `awaiting_operator` | no (`operator_action`) | the deliverable is human action no dev agent can perform |

---
