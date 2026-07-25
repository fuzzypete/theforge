# Bug shape reference

> Generated from `theforge.shape_check.diagnosis_spec`. Do not edit by hand —
> run the shape-reference generator (or the test that regenerates it) instead.

A bug-typed issue is *fix-ready* when its body carries a complete
`## Diagnosis` section. The shape gate refuses symptom-only bug bodies;
an honest non-assertion confirmed cause (`unknown`, `not yet identified`,
`pending investigation`, `TBD`) is admissible and marks the bug
investigation-ready rather than implementation-ready.

## Required components

Each component is written as a bolded bullet lead-in under the
`## Diagnosis` heading. The gate matches the **label** literally (case-insensitive).

### Observed symptom

- Satisfies: bolded bullet lead-in or heading inside "## Diagnosis"
- Example: - **Observed symptom:** sprint resume false-skips zero-delta APPROVE stories, reporting them merged when no commit landed.

### Evidence

- Satisfies: bolded bullet lead-in or heading inside "## Diagnosis"
- Example: - **Evidence:** run id `1ff6b0bb7992`, story #1102 — resume log shows the false skip.

### Confirmed cause

- Satisfies: bolded bullet lead-in or heading; its value may be a specific claim or an honest non-assertion ("unknown", "not yet identified")
- Example: - **Confirmed cause:** `_is_already_merged` requires at least one commit ahead, so a zero-delta APPROVE is misclassified as unmerged.

### Affected code path

- Satisfies: bolded bullet lead-in or heading naming the module/function
- Example: - **Affected code path:** `sprint.runner._is_already_merged`.

### Fix-success criterion

- Satisfies: bolded bullet lead-in or heading stating the observable pass condition
- Example: - **Fix-success criterion:** resume identifies a zero-delta APPROVE story as already merged.

## Fileable skeleton

Copy this into a bug issue body and replace each example value. A body
that starts from this skeleton passes the shape gate by construction.

```markdown
## Diagnosis

- **Observed symptom:** sprint resume false-skips zero-delta APPROVE stories, reporting them merged when no commit landed.
- **Evidence:** run id `1ff6b0bb7992`, story #1102 — resume log shows the false skip.
- **Confirmed cause:** `_is_already_merged` requires at least one commit ahead, so a zero-delta APPROVE is misclassified as unmerged.
- **Affected code path:** `sprint.runner._is_already_merged`.
- **Fix-success criterion:** resume identifies a zero-delta APPROVE story as already merged.
```
