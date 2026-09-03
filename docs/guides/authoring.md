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

### Use `forge author` before filing or promoting a draft

`forge author` is the interactive path for collecting the parts a typed issue
still needs *before* you submit it. Start fresh with `forge author --type
enhancement`, resume a local draft with `forge author --from-draft PATH`, or
finish an existing `todo:draft` issue with `forge author --from-issue <N>`.
Use `--output PATH` to write the current state as a local draft, and `--create`
only when you want the command to create or update the GitHub issue after the
shared shape gate says the body is runnable.

The command does not invent its own checklist. Required parts come from the
typed issue specification, the finished body is rendered through that shared
specification, and the result is re-checked with the same shape gate every
other intake surface uses. Where a part has a constrained form, the prompt
states the property to satisfy at the moment you answer it: acceptance
criteria are reviewer-checkable outcomes, and implementation-plan details such
as file paths, call sequences, and design notes belong outside the issue body.
If you decline a required part, `forge author` keeps or adds `todo:draft` and
writes an honestly incomplete draft instead of something that reads ready.

### Groom before the sprint, not during it

Making a typed issue shape-gate-clean is the job of **`forge groom <issue>`**,
run *before* sprint selection. Sprint entry is the final readiness *check*, not
the place readiness gets created.

Inline remediation is the fallback when pre-sprint grooming was skipped, not the
primary workflow. The `intake.grooming` flag (disabled by default) exists as an
opt-in safety net for incident-time pressure; when it fires it warns you to run
`forge groom` next time. Do not treat it as a substitute for grooming. See
[ADR-0001](../adr/0001-intake-readiness-workflow.md) and the
[`intake.grooming` reference](inputs-reference.md#inline-intake-remediation-intakegrooming).

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

## The six use cases

| Use case | Required sections | Optional but recommended |
|----------|------------------|--------------------------|
| Feature / enhancement | Title, Why, Acceptance criteria, Example | Notes |
| Bug report | Title, Observed, Expected, Diagnosis (landed by `forge diagnose`) | (nothing) |
| Refactor / mechanical change | Title, Why, Acceptance criteria, Example (before/after) | Notes |
| Rollup | Title, Why, Acceptance criteria (one per child), Example | Notes |
| Docs / chore | Title, Why, Acceptance criteria, Example | Notes |
| Spike | Title, Why, Acceptance criteria, Example | Notes |

There is also a sixth, deliberately-not-dev-runnable type:

| Use case | Required sections | Required label |
|----------|------------------|----------------|
| Operator action | Title, Why, Acceptance criteria | `operator-action` |

See [Operator-action issues](#operator-action-issues) below.

A short shared rule across every use case: the issue body says **what** and
**why** — never **how**. Do not list file paths, function names, numbered
implementation steps, or test-strategy hints. Those belong in planning, not in
the input.

## Shared vocabulary

The issue corpus uses a small set of operational terms with specific meanings.
Use these words consistently in issue bodies so the reader does not have to
infer which spend mechanism, retry boundary, or run boundary you meant.

- **generation** — one process image of a sprint run. A re-exec starts a new
  generation of the same run; prior-generation work may still be carried
  forward into the resumed audit.
- **seating** — the act of assigning the actual planner, dev, and reviewer
  agents for a story. An agent is **seated** once that assignment has been
  made.
- **allocation** — the per-story spend envelope derived at seating from the
  story's shape and historical evidence. It is story-local and informational:
  it explains routing, telemetry, and allocation-shortfall reporting.
- **budget** — an operator-supplied spend limit. Unqualified, this means the
  sprint-level `--budget` / `budget_usd` ceiling that governs whether more
  stories may launch.
- **ceiling** — a hard upper bound inside a narrower scope than the sprint
  budget, such as a worker-timeout window, a per-phase accepted-cost bound, or
  a reviewer-count maximum. Use this when you mean "cannot exceed," not when
  you mean an estimate.
- **band** — one of the three complexity buckets `small`, `medium`, or `large`
  that groups stories for routing and historical comparisons. Do not use
  **band** for pricing tiers or role strength; those are separate concepts.
- **invocation** — one discrete agent or command call. A single story run may
  contain multiple invocations, and a run may span multiple generations.

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
  bullet. Each bullet should describe a reviewer-checkable outcome. Avoid
  embedding code, YAML, function signatures, file paths, or stepwise build
  instructions inside AC bullets — the validator flags AC sections that look
  like implementation dumps.
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

### Lifecycle: capture, then diagnose, then groom

A bug goes through two states, and the shape gate enforces the boundary:

1. **Capture** the symptom: file the issue with `## Observed` and
   `## Expected`. This is a valid capture, but it is *symptom-only*
   — the sprint-entry gate hard-blocks it (`needs_diagnosis`: "Bug has no
   Diagnosis section — not fix-ready").
2. **Diagnose**: run `forge diagnose --issue <N>` (or investigate manually).
   The diagnosis lands as a `## Diagnosis` section **in the issue body**, after
   `## Observed` and `## Expected`, with the required components listed in
   [the bug shape reference](../reference/bug-shape.md): observed symptom,
   evidence, confirmed cause, affected code path, fix-success criterion. A
   confirmed cause of `unknown` / `not yet identified` is admissible — the bug
   is then investigation-ready but still not implementation-runnable.
3. **Groom and ready**: `forge groom <N>` repairs any remaining shape issues,
   then apply the `ready` label. Only a bug whose Diagnosis asserts a
   confirmed cause can reach the runnable verdict.

### Required sections

- **Title** — names the misbehavior, not the suspected cause.
- **Observed** — canonical heading `## Observed`; `## What happened` is
  recognized too, and producers rewrite it to the canonical spelling. Concrete
  observation: command run, output, environment, link to logs if relevant.
- **Expected** — canonical heading `## Expected` (`## What was expected` is
  also recognized). Describe
  the **category-level rule** that was violated, written as prose. The
  expected behavior should generalize past the single triggering case so the
  fix has a defined scope.
- **Diagnosis** — heading literally `## Diagnosis`, required in the body
  before sprint entry. It sits **after** `## Observed` and `## Expected`.
  Normally landed by `forge diagnose`, not written at capture time. See the
  [bug shape reference](../reference/bug-shape.md) for the exact component
  labels and a fileable skeleton.

### What to leave out

- **No acceptance criteria.** Bug reports use the observed/expected shape
  instead. Adding an AC checklist turns a bug into a feature spec and pushes
  the dev agent toward fixing the symptom rather than the cause.
- **No implementation hints in the symptom capture.** No file paths, no
  "probably in `coordinator/foo.py`", no suggested patch. Cause claims and
  the affected code path belong in the `## Diagnosis` section, backed by
  investigation evidence — not guessed at capture time.
- **No test requirements.** "Add a regression test" is implementation, not
  expected behavior.
- **No anchoring to one provider, one model, or one issue number.** The rule
  in *Expected* should hold across the category.

### Worked example

The example below is a fully diagnosed bug — the state an issue must reach
before sprint entry. At capture time you would file only the first two
sections; the `## Diagnosis` section is what `forge diagnose` adds.

````markdown
# `forge sprint --resume` re-runs already-merged stories

## Observed

Ran `forge sprint --resume` on a sprint where two of three stories had been
reviewed, approved, and merged to main in a previous session. The resume run
re-entered both merged stories at the dev phase and produced a second set of
commits for work that had already landed.

## Expected

Resuming a sprint should never repeat work that has already reached a
terminal merged state. A story whose branch has been merged into the base
branch is finished from the sprint runner's perspective, regardless of which
phase the audit log last recorded for it. Resume should advance only stories
that are still in flight.

## Diagnosis

- **Observed symptom:** sprint resume re-enters already-merged stories at the
  dev phase, producing duplicate commits for landed work.
- **Evidence:** run id `1ff6b0bb7992` — resume log shows both merged stories
  re-entering dev.
- **Confirmed cause:** `_is_already_merged` requires at least one commit
  ahead, so a zero-delta APPROVE is misclassified as unmerged.
- **Affected code path:** `sprint.runner._is_already_merged`.
- **Fix-success criterion:** resume identifies a zero-delta APPROVE story as
  already merged and does not re-dispatch it.
````

The bug label OR the observed/expected heading pair exempts this issue from
the acceptance-criteria and example requirements; the complete `## Diagnosis`
section with an asserted confirmed cause is what makes it fix-ready.

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
  import from the old module).
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

## Spike

### Purpose

A chartered question: is this approach worth adopting? The deliverables are a
design document and a validating POC. A spike is a normal dev-runnable story —
it carries the `spike` label and an `## Acceptance criteria` section like any
other — with one extra rule at the *other* end of its life.

### The two legal exits

A spike cannot be closed without recording one of exactly two outcomes:

| outcome | what must exist afterwards |
|---|---|
| do not proceed | a recorded decision on the spike, naming why |
| proceed, or proceed when X | a follow-on issue in the pipeline, carrying the trigger condition if there is one |
| *(nothing)* | not a legal exit |

"Do not do this" is a complete and successful outcome. Nothing here obliges a
spike to recommend proceeding — it obliges the answer to survive as an
artifact rather than as operator memory.

Record the outcome with a marker in the spike's body or in a comment on it:

```markdown
<!-- forge-spike-outcome-v1
outcome: do_not_proceed
reason: the ranking never clears its trust threshold on real sprint data
-->
```

```markdown
<!-- forge-spike-outcome-v1
outcome: follow_up
follow-up: #2599
-->
```

```markdown
<!-- forge-spike-outcome-v1
outcome: conditional_follow_up
follow-up: #2599
-->
```

A follow-on issue must be **open**, carry exactly one type label, and not be a
`todo:draft`. A `conditional_follow_up` additionally requires the follow-on
issue to carry the condition — a closed spike's prose does not count, because
that is precisely the artifact nobody re-reads:

```markdown
## Spike trigger condition

- **What must be true:** the observer's ranking beats the naive baseline on
  three consecutive real sprints.
- **How to know:** the comparison hook reports its trust threshold met in the
  sprint audit rather than declining to be used.
```

### How this is enforced

Mechanically, at every close, not as guidance:

- the sprint's landing close and its ALREADY_DONE dispositions;
- `forge todo` triage and `forge triage` ratification;
- `close-on-merge.yml` and `close-epic-on-last-subissue.yml`;
- a spike's PR references its issue with `Refs #N`, never `Closes #N`, so
  GitHub's native auto-close cannot decide the question first.

GitHub has no synchronous pre-close hook, so the one path that cannot be
refused in advance — a human pressing "Close issue" in the web UI — is caught
after the fact: `enforce-spike-outcome.yml` re-asks the same guard and reopens
the spike with the reason when the close was not a legal exit.

The rule lives in one place, `theforge.spike_guard`; every path above calls it,
and `python -m theforge.spike_guard <issue>` is how the workflows ask.

---

## Epic

### Purpose

A roadmap container that groups a body of related work into a single
trackable parent — never a sprintable unit itself. The sprint-entry
validator refuses epics outright (`epic_or_tracking`); their job is to
organize slices, not to run.

### Required shape

- **Title** — `Epic: <theme>`, or the `epic` label applied. Either marker is
  sufficient to trip `epic_or_tracking`; use both for clarity.
- **Slices via native GitHub sub-issues** — every runnable piece of the
  epic's work is linked as a GitHub sub-issue, not just referenced by number
  in prose. "The epic's issues" must be answerable by reading the sub-issue
  list, not by re-reading the body for `#refs`.
- **Bounded body** — the epic describes a finite scope that is done when its
  sub-issues are done. Do not write "post-MVP enhancements tracked here" or
  "tracking issue, add more as they come up" — framing that keeps the epic
  open forever. Genuinely new work that surfaces later is a *new* issue
  (optionally linked as a sub-issue if it belongs to the same scope), not an
  addition to a container that never closes.
- **No work milestone** — an epic is never assigned to a work milestone.
  Milestones hold sprintable slices; an epic's slices carry the milestone
  individually. Use a label or a project view to see epics by roadmap
  horizon instead. If you find an epic on a work milestone, remove the
  milestone — that is always the fix, never re-target it to a different
  milestone.

### What to leave out

- A milestone field. If `forge groom` or manual editing sets one on an
  epic, clear it.
- Perpetual-tracker language ("tracked here forever," "future work goes
  here"). If the epic's current slices are all closed, the epic is done —
  close it, and file whatever comes next as its own issue.

---

## Rollup

### Purpose

A small, related batch of changes worth running as one sprint entry rather
than as N separate issues — usually because each piece is too small to justify
its own issue and the pieces share context.

A rollup is **not** an epic. Epics are tracking-only and are blocked by the
sprint-entry validator. If your work decomposes into independent stories that
need their own review cycles, file them separately and link them as GitHub
sub-issues of an epic, or share a milestone/label — not in a rollup.

### Required sections

- **Title** — names the theme of the batch.
- **Why** — one paragraph on why these belong together.
- **Acceptance criteria** — one bullet per child change, each phrased as an
  observable outcome a reviewer can check. Keep the count small: more than a
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

## Mid-sprint workflow

When a running sprint surfaces a new bug or work item, you do **not** stop the
sprint or edit its selected work. You capture the item, bring it to a runnable,
`ready` state through the normal intake steps, and let the *next* sprint pick it
up by ordinary selection. The running sprint's topology is never modified.

The canonical step sequence, in order:

1. **Capture** — file the issue (`gh issue create`).
2. **Shape** — classify it into a typed work object (`forge shape`).
3. **Diagnose** — for bug-typed items that need root cause
   (`forge diagnose --issue <N>`).
4. **Groom** — repair the body to a runnable verdict (`forge groom`).
5. **Ready** — apply the `ready` label so the next sprint is eligible to select
   it (`gh issue edit --add-label ready`).

```bash
gh issue create --label bug --title "..." --body "..."   # capture → returns #1512
forge shape 1512                                          # classify (often immediate)
forge diagnose --issue 1512                               # if bug-typed and needs a cause
forge groom 1512                                          # repair body → runnable verdict
gh issue edit 1512 --add-label ready                      # eligible for the next sprint
```

Not every step applies to every item — a cleanly-captured enhancement may skip
`forge diagnose`, and `forge shape` may classify immediately. Run the steps the
item needs; the order above is the pipeline, not a mandatory checklist.

### There is no `forge queue` command

"Queue for next sprint" is a convention, not a command. There is **no
`forge queue`** and no queue ordering, priority, or cross-sprint dependency
semantics. The `ready` label *is* the eligibility signal: an open, `ready`-labeled
issue is what normal sprint selection considers. To see the current eligible set,
use `forge status --ready` (scope it with `--milestone`):

```
$ forge status --ready --milestone v0.13.0
Ready for next sprint in v0.13.0 (2 issues, 1 blocked by shape gate):
  #1487  bug  ready                    status --watch blank during preflight
  #1512  bug  BLOCKED:needs_diagnosis  cut-rc.sh shim wrapper regression

1 issue carries the `ready` label but would be refused at sprint entry:
  #1512  needs_diagnosis: Bug has no Diagnosis section — not fix-ready. …
Run `forge shape <n>` for the full verdict, then `forge groom <n>` / `forge diagnose <n>` before sprint selection.
```

The `ready` label is applied by hand, so the listing does not take it at face
value: every entry is run through the same shape gate that guards sprint entry,
and one the gate would refuse is marked `BLOCKED:<verdict>` rather than
presented as eligible. Groom or diagnose those before selecting a sprint —
otherwise the sprint discovers the problem after budget is committed.

### Live sprint injection is out of scope

Modifying a *running* sprint's selected work — injecting the new item into the
sprint already in flight — is deliberately **out of scope** here. It belongs to
the v0.12+ autonomy roadmap, where the orchestrator may re-plan in-flight work.
Until then, the boundary is firm: mid-sprint discoveries are groomed-and-readied
for the *next* sprint, never injected into the current one.

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
$ forge status <run-id>
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
- **`no_observable_done_state`** — the acceptance-criteria section is absent
  or carries no bullets at all. It no longer scores your *wording*: the closed
  verb vocabulary that once decided this was retired as an admission input
  (ADR-0009), because it made admission depend on word choice, verb tense and
  line wrapping rather than on whether a reviewer could check the outcome.
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

The structural rules are not restated here at all: they live in one
declarative specification and are published, generated from it, as
[the issue shape reference](../reference/issue-shape.md). When this guide and
that reference disagree, the reference is right — it cannot drift, because CI
regenerates it from the same data the gate validates against.

The two anchors to verify against are:

- `docs/reference/issue-shape.md` — generated from
  `src/theforge/shape_check/issue_spec.py`, the specification the sprint-entry
  validator derives its structural rules from.
- `CONVENTIONS.md` — the project-level rules this guide operationalizes
  (bug-format minimalism, what-not-how, runnable-at-creation).

## See also

- [Inputs Reference](inputs-reference.md) — file formats, frontmatter fields,
  sprint manifest schema, and `forge.yaml` keys.
- [CLI Reference](cli-reference.md) — the commands referenced in worked
  examples.
- `CONVENTIONS.md` (repo root) — project conventions that this guide makes
  concrete with templates.
