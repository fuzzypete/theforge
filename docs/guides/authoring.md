# Authoring Issues and Stories

How to write input that TheForge can actually run. Organized by **use case** —
feature, bug, refactor, rollup, and docs/chore — because the pipeline treats
these shapes differently. Each section ends with a complete worked example
that, copied verbatim into a GitHub issue (or a `stories/*.md` file), passes
TheForge's sprint-entry validation as-is.

You do not need to read the validator's source to follow this guide. Following
the per-section rules will produce input that passes.

## Shaping rough drafts first

Before an issue can be authored against the rules below, it has to *be the
right kind of thing*. If you captured a thought with `forge todo` or filed a
freshly-rough issue without a clear type, run `forge shape <issue>` first.

`forge shape` proposes one of TheForge's typed work objects — bug,
enhancement, epic, operator-action, documentation, adr-candidate, or
duplicate/stale — and prints a body restructure. It is **refusal-capable**:
when confidence is low it keeps the item as `todo:draft` and asks you
structured ambiguity questions rather than force-classifying. Use `--apply`
to commit the proposal via `gh`, and `--next` to print the recommended next
operator command (`forge diagnose`, `forge groom`, etc.). The next command is
never auto-invoked — you run it.

Once `forge shape` has put the issue in a typed bucket, the per-use-case
rules below apply.

## Storage: GitHub issue vs local story file

TheForge accepts the same content in two storage backends:

- **GitHub issue** — preferred for projects that already track work on GitHub.
  TheForge itself uses this mode.
- **Local file** under `stories/*.md` (or any path) — supported for downstream
  projects that do not use GitHub. The file may carry a small YAML frontmatter
  block with `name:` and `slug:` (see [Inputs Reference](inputs-reference.md)).

Content rules below are identical for both backends. Where this guide says
"issue body," read it as "issue body or the prose section of the story file."
Storage is incidental; shape is what matters.

## The five use cases

| Use case | Required sections | Optional but recommended |
|----------|------------------|--------------------------|
| Feature / enhancement | Title, Why, Acceptance criteria, Example | Notes |
| Bug report | Title, What happened, What was expected | (nothing) |
| Refactor / mechanical change | Title, Why, Acceptance criteria, Example (before/after) | Notes |
| Rollup | Title, Why, Acceptance criteria (one per child), Example | Notes |
| Docs / chore | Title, Why, Acceptance criteria, Example | Notes |

There is also a sixth, deliberately-not-dev-runnable type:

| Use case | Required sections | Required label |
|----------|------------------|----------------|
| Operator action | Title, Why, Acceptance criteria | `operator-action` |

See [Operator-action issues](#operator-action-issues) below.

A short shared rule across every use case: the issue body says **what** and
**why** — never **how**. Do not list file paths, function names, numbered
implementation steps, or test-strategy hints. Those belong in planning, not in
the input.

---

## Feature or enhancement

### Purpose

You want TheForge to add a capability or change observable behavior in a way
that does not yet exist.

### Required sections

- **Title** — short, action-shaped. No `Epic:` prefix.
- **Why** — one short paragraph of motivation. Context for the agent, not a
  task list.
- **Acceptance criteria** — heading literally `## Acceptance criteria` (the
  validator also accepts `## Done criteria` or `## Checklist`). At least one
  bullet. Each bullet should describe an observable outcome using a behavioral
  verb such as *returns, emits, writes, logs, creates, raises, reports, blocks,
  accepts, rejects, fails, passes, warns, exits*. Avoid embedding code, YAML,
  or function signatures inside AC bullets — the validator flags AC sections
  that look like implementation dumps.
- **Example** — heading `## Example` (or `## Examples`, `## Target output`,
  `## What it should look like`). Include a fenced code block, a bullet list,
  or a small table that shows what success looks like. Must be substantive:
  a one-word example does not satisfy the check.

### Optional

- **Notes** — pitfalls, prior art, links. Read by the agent as background, not
  as requirements.

### What to leave out

- File paths and function names (unless the story is intrinsically about a
  specific file).
- Numbered implementation steps.
- "Add a test in `tests/test_foo.py`" — the agent decides where tests live.
- Acceptance criteria that read like a design document (multiple fenced Python
  or YAML blocks inside the AC section will trip the implementation-dump
  guard).

### Worked example

````markdown
# Add `forge status --json` flag for machine-readable output

## Why

Operators scripting against `forge status` currently parse human-formatted
text, which breaks whenever the display layout changes. A stable JSON shape
lets them script reliably without coupling to the TTY rendering.

## Acceptance criteria

- `forge status --json` writes a single JSON object to stdout and exits 0
  when there is an active sprint.
- The JSON object reports sprint id, current phase, completed story count,
  remaining story count, and elapsed seconds.
- `forge status --json` with no active sprint writes `{"active": false}` and
  exits 0.
- `forge status --json --invalid-flag` rejects the unknown flag and exits
  non-zero.
- Existing human output produced by `forge status` (no flag) is unchanged.

## Example

```
$ forge status --json
{"active": true, "sprint_id": "2026-05-01-1430", "phase": "review",
 "completed": 2, "remaining": 1, "elapsed_seconds": 412}

$ forge status --json   # no active sprint
{"active": false}
```

## Notes

The existing `forge status` renderer already computes these values for the
TTY path; surfacing them as JSON should not require a second source of
truth.
````

---

## Bug report

### Purpose

You observed behavior that contradicts what TheForge should do. You want it
fixed.

### Required sections

- **Title** — names the misbehavior, not the suspected cause.
- **What happened** — heading literally `## What happened`. Concrete
  observation: command run, output, environment, link to logs if relevant.
- **What was expected** — heading literally `## What was expected`. Describe
  the **category-level rule** that was violated, written as prose. The
  expected behavior should generalize past the single triggering case so the
  fix has a defined scope.

### What to leave out

- **No acceptance criteria.** Bug reports use the observed/expected shape
  instead. Adding an AC checklist turns a bug into a feature spec and pushes
  the dev agent toward fixing the symptom rather than the cause.
- **No implementation hints.** No file paths, no "probably in
  `coordinator/foo.py`", no suggested patch.
- **No test requirements.** "Add a regression test" is implementation, not
  expected behavior.
- **No anchoring to one provider, one model, or one issue number.** The rule
  in *What was expected* should hold across the category.

If the bug needs root-cause analysis before it can be implemented (a
"symptom-only" bug), the diagnosis lives in a follow-up comment on the issue —
not in the original body. See `CONVENTIONS.md` for the diagnosis checklist.

### Worked example

````markdown
# `forge sprint --resume` re-runs already-merged stories

## What happened

Ran `forge sprint --resume` on a sprint where two of three stories had been
reviewed, approved, and merged to main in a previous session. The resume run
re-entered both merged stories at the dev phase and produced a second set of
commits for work that had already landed.

## What was expected

Resuming a sprint should never repeat work that has already reached a
terminal merged state. A story whose branch has been merged into the base
branch is finished from the sprint runner's perspective, regardless of which
phase the audit log last recorded for it. Resume should advance only stories
that are still in flight.
````

This issue uses the bug-label OR the observed/expected heading pair, so the
sprint-entry validator exempts it from acceptance-criteria and example
requirements.

---

## Refactor or mechanical change

### Purpose

You want the same external behavior implemented differently — extracting a
module, renaming a field, replacing a dependency, normalizing call sites.

### Required sections

- **Title** — name the move, not the rationale.
- **Why** — what makes the current shape painful: duplication, leaky
  abstraction, brittle test surface.
- **Acceptance criteria** — observable checks that prove the refactor
  preserved behavior. Good AC bullets here are *equivalence* statements (input
  X still produces output Y) and *structural* invariants (callers no longer
  import from the old module). Use behavioral verbs: *returns, passes,
  emits, fails, exits, blocks*.
- **Example** — typically a **before / after** sketch of the structural
  change. A small two-column table or two short fenced snippets are ideal.

### What to leave out

- A migration plan for every call site. The agent will discover those.
- Numbered task lists. If the refactor really needs ordering, file separate
  stories.

### Worked example

````markdown
# Move retry-policy fields out of `profiles.dev` into top-level `retry:`

## Why

Retry counts (`max_dev_iterations`, `max_review_cycles`) are policy that
applies to the whole sprint, not to a single profile. Having them nested
under `profiles.dev` confuses operators reading `forge.yaml` and forces
duplicate values whenever a project defines multiple dev profiles.

## Acceptance criteria

- `forge.yaml` files that already declare a top-level `retry:` block load
  unchanged and produce the same effective config they do today.
- `forge.yaml` files that still declare retry fields under `profiles.dev`
  load successfully, emit a deprecation warning naming the moved fields,
  and produce the same effective behavior as the top-level form.
- `forge check-config` reports the resolved retry policy from a single
  source (the top-level block), regardless of which form was written.
- The full test suite passes with no changes to existing behavioral tests.

## Example

Before:

```yaml
profiles:
  dev:
    model: sonnet
    max_dev_iterations: 3
    max_review_cycles: 2
```

After:

```yaml
profiles:
  dev:
    model: sonnet

retry:
  max_dev_iterations: 3
  max_review_cycles: 2
```
````

---

## Rollup

### Purpose

A small, related batch of changes worth running as one sprint entry rather
than as N separate issues — usually because each piece is too small to justify
its own issue and the pieces share context.

A rollup is **not** an epic. Epics are tracking-only and are blocked by the
sprint-entry validator. If your work decomposes into independent stories that
need their own review cycles, file them separately and link them in a
milestone or label, not in a rollup.

### Required sections

- **Title** — names the theme of the batch.
- **Why** — one paragraph on why these belong together.
- **Acceptance criteria** — one bullet per child change, each phrased as an
  observable outcome with a behavioral verb. Keep the count small: more than a
  handful of distinct subsystems in one rollup will trip the
  too-many-clusters guard, and that is the validator telling you to split.
- **Example** — show the externally visible result of one or two of the
  changes (sample output, table of renamed flags, etc.).

### What to leave out

- Words like *epic*, *parent*, or *umbrella* in the body — those mark an
  issue as tracking-only and the validator will refuse to sprint it.

### Worked example

````markdown
# Tighten three `forge status` output fields

## Why

Three small display issues in `forge status` keep getting reported
separately. They share rendering code and reviewing them in isolation costs
more than batching them.

## Acceptance criteria

- `forge status` emits elapsed time as `Hh Mm Ss` (e.g. `1h 04m 12s`)
  instead of raw seconds.
- `forge status` reports the active phase in lowercase and rejects an
  unknown phase value with a clear error rather than rendering it raw.
- `forge status` writes "no active sprint" to stdout (not stderr) and exits
  0 when there is nothing to report.

## Example

```
$ forge status
sprint: 2026-05-01-1430
phase:  review
elapsed: 1h 04m 12s
stories: 2 done / 1 remaining
```
````

---

## Docs or chore

### Purpose

Documentation work, build configuration, dependency bumps, lint rule changes
— work that has no runtime behavior to verify but still needs an observable
"done" condition.

### Required sections

- **Title** — names the doc or chore.
- **Why** — what is wrong or missing today.
- **Acceptance criteria** — observable checks. For docs: "the page contains
  X," "the example in section Y produces Z." For chores: "lint command exits
  0," "`pip install -e .` succeeds with the new pin."
- **Example** — table of contents sketch, sample paragraph, target command
  output, before/after snippet. Docs and chore stories are the most prone to
  vague ACs; the example is what keeps them honest.

### Worked example

````markdown
# Document `forge.yaml` `retry:` block in the inputs reference

## Why

Operators tuning retry behavior have to read source to discover which keys
are valid under `retry:` and what their defaults are. The inputs reference
covers `validation:` and `workspace:` in detail but skips `retry:`.

## Acceptance criteria

- `docs/guides/inputs-reference.md` contains a `### Retry policy` subsection
  under the project-config heading.
- That subsection lists every key currently accepted under `retry:` in a
  table with name, default, and one-line description.
- The reference reports each key's default by reading from the loader, not
  by hand-copying — so the doc passes review against current code.
- The page renders without broken internal links when built with the docs
  toolchain.

## Example

The new subsection follows the existing pattern used for `validation:`:

```markdown
### Retry policy

| Field | Default | Description |
|-------|---------|-------------|
| `max_dev_iterations` | 3 | Dev attempts within one review cycle |
| `max_review_cycles`  | 2 | Full dev→review loops before ESCALATE |
```
````

---

## Repairing a rejected issue with `forge groom`

If an issue is typed but fails the shape gate, `forge groom <N>` will
propose a body restructure to fix it. The command applies the three-state
bug rule from ADR-0001 — bugs without a diagnosis are refused with a
pointer to `forge diagnose`, bugs whose cause is still unknown can only
be normalized (never labeled `ready`), and bugs with a confirmed cause are
restructured normally. See `cli-reference.md#forge-groom` for usage.

Groom does **not** invoke `forge diagnose` or `forge shape` for you; the
operator runs them in order (`shape → diagnose → groom`).

---
## Operator-action issues

Some work cannot be performed by a dev agent — running real validation
sprints, capturing live operational evidence, signing a release, filing an
incident report. The `operator-action` label declares an issue whose
deliverable is human action by design.

`forge sprint` refuses to dispatch operator-action issues to dev cycles. They
appear in sprint output as deliberately non-dispatched (operator paid `$0`),
not as failed and not as "wrong shape." The output uses a distinct status row
so they cannot be confused with shape-gate skips.

### Required shape

- Apply the `operator-action` label.
- Include an `## Acceptance criteria` section describing the operator
  deliverable. The gate refuses operator-action issues without this section
  (distinct skip code: `operator_action_missing_ac`).
- Do not also apply `bug`, `enhancement`, `epic`, or `task`. Those are the
  dev-runnable types and conflict with operator-action by design — pick
  exactly one. The gate refuses multi-typed issues with the
  `operator_action_label_conflict` code naming the conflict.

### What the operator sees

```
$ forge sprint --verbose --issues 1326,1471 --budget 50 --parallel=3
[forge] 1 issue(s) deliberately non-dispatched (operator-action):
  - #1471 (label): operator-action — Validate v0.11 substrate
[sprint] "issues-1326" 1 story budget=$50.00 parallel=3
$ forge sprint-status <run-id>
  ⊘ Issue #1471            operator-action       —         ...   not sprintable; operator deliverable
```

`--force` does not bypass operator-action; the label is the operator's
deliberate signal, not a shape-gate guard to override.

### What is out of scope

Operator-action issues remain unautomated by design. There is no
"`forge run-validation-sprint`" macro — the type exists precisely to mark the
boundary between system-runnable work and operator-runnable work. If you want
the system to do the work, file a `bug`/`enhancement`/`task` instead.

## Common reasons sprint entry rejects an issue

If you followed the use-case templates above you should not see these, but
they are useful to recognize:

- **`missing_acceptance_criteria`** — no `## Acceptance criteria` heading
  with at least one bullet (feature/refactor/rollup/chore only — bugs are
  exempt).
- **`no_observable_done_state`** — AC bullets exist but none use a
  behavioral verb a reviewer can check.
- **`missing_example`** — feature-shaped issues with no `## Example` (or
  equivalent) section, or with one that has no fenced block, bullets, or
  table rows. Advisory, but worth fixing.
- **`implementation_design_dump`** — AC section is loaded with code or YAML
  blocks. Move that material into Notes or out of the issue entirely.
- **`epic_or_tracking`** — title starts with `Epic:`, an `epic` label is
  applied, or the body says "tracking issue" / "umbrella" / "parent issue".
  Tracking issues are not runnable — file the runnable children separately.
- **`too_many_behavioral_clusters`** — AC bullets touch too many distinct
  subsystems. Split the issue.
- **`operator_action_label_conflict`** — `operator-action` is applied
  alongside `bug`/`enhancement`/`epic`/`task`. Pick exactly one issue type.
- **`operator_action_missing_ac`** — `operator-action` issue is missing the
  `## Acceptance criteria` section describing the operator deliverable.

---

## Verifying authoring docs against code

This guide was written with a deliberate practice: every field name, heading,
and command mentioned was cross-checked against current code at authoring
time. If you find a section that references something the code no longer has
(a removed flag, a renamed field, a deleted heading), treat that as a doc bug
worth filing — drift in the authoring guide is what motivated this rewrite.

The two anchors to verify against are:

- `src/theforge/shape_check/heuristics.py` — defines the headings, verbs, and
  example patterns the sprint-entry validator looks for.
- `CONVENTIONS.md` — the project-level rules this guide operationalizes
  (bug-format minimalism, what-not-how, runnable-at-creation).

## See also

- [Inputs Reference](inputs-reference.md) — file formats, frontmatter fields,
  sprint manifest schema, and `forge.yaml` keys.
- [CLI Reference](cli-reference.md) — the commands referenced in worked
  examples.
- `CONVENTIONS.md` (repo root) — project conventions that this guide makes
  concrete with templates.
