# Repo-Versioned Project Conventions

This file is the canonical home for project-level conventions and project-scoped
lessons that should travel with TheForge.

Project conventions live in the repo. User memory stays for operator-local
preferences.

Use this boundary going forward:

- If a lesson should apply to any contributor or future TheForge dev/review
  agent, record it in a repo artifact: `CONVENTIONS.md`, a GitHub issue, an ADR,
  or another checked-in doc.
- If a lesson is specific to one operator's workflow, shell, timezone,
  verbosity preference, merge permission, or collaboration style, keep it in
  user memory instead of the repo.
- When a design discussion converges on a decision, capture it before the
  session ends. Do not let project policy default back into user memory because
  that happened to be the easiest write target at the time.

This file was populated by migrating project-level lessons out of
`~/.claude/projects/-Users-pwickersham-src-theforge/memory/`. The audit mapping
for every source file lives in `docs/memory-migration.md`. That migration leaves
only a smaller, genuinely user-local propagation surface for the separate
memory-propagation work.

## Story And Issue Discipline

### TheForge uses GitHub issues as stories in this repo

For TheForge itself, GitHub issues are the source of truth for stories. Do not
re-introduce local story files into this repo. The tool still supports
file-based stories for downstream projects, so docs should present "story" and
"issue" as the same content model with different storage backends.

Source: `project_stories_gh_only.md`

### Feature and enhancement issues must be runnable at creation time

When filing feature or enhancement work in this repo:

- Add the right label at creation time (`bug`, `enhancement`, or another
  appropriate routing label).
- Include an `## Acceptance criteria` section with observable outcomes.
- Treat filing as incomplete until the issue is actually runnable by the shape
  gate and sprint tooling.

Bug reports are exempt from the AC rule because bugs use a different shape.

Sources: `feedback_acs_required.md`, `feedback_issue_labels.md`

### A spike closes on a recorded outcome or not at all

A spike (the `spike` label) is chartered to answer a question, and it has
exactly two legal exits: a recorded decision not to proceed, carrying its
reasoning, or a follow-on issue that exists in the pipeline. "Do not do this"
is a complete and successful outcome; *nothing* is not an exit. Where the
answer is conditional, the condition is carried by the follow-on issue — a
`## Spike trigger condition` section naming what must be true and how anyone
would know — because prose in a closed spike is the artifact nobody re-reads.

This is enforced mechanically at every close path, not offered as guidance:
`theforge.spike_guard` is the single implementation, and the web-UI close that
no pre-close hook can intercept is reversed after the fact by
`enforce-spike-outcome.yml`. See
[the authoring guide](docs/guides/authoring.md#spike) for the marker syntax.

### Feature and documentation issues should include a concrete example

Default to including a concrete example that shows what success looks like:
sample output, a before/after walkthrough, a target table of contents, or a
sketched interaction. The example anchors preflight, planning, review, and
future re-reading without over-specifying implementation.

Source: `feedback_examples_in_features.md`

### Story bodies describe WHAT and WHY, not HOW

Do not put file paths, function names, line numbers, numbered implementation
steps, or testing-strategy hints in issue bodies unless the story is explicitly
about a specific file. High-level capability requirements are valid when they
clarify scope; implementation paths are not.

A failed sprint is not license to smuggle HOW into the story on the next try.
If a gap belongs in planning or conventions, put it there instead of amending
the story body.

Sources: `feedback_what_not_how.md`, `feedback_retro_to_story_temptation.md`

### Bug stories stay minimal and the expected behavior must generalize

Bug reports contain only:

1. What happened.
2. What was expected.

The expected behavior must describe a category-level rule that generalizes
beyond the triggering incident. Keep it as prose, not bullet lists or
sub-headings that read like feature acceptance criteria. Avoid anchoring bug
success to one story number, one provider, or one implementation theory.

Sources: `feedback_bug_story_format.md`,
`feedback_bug_expected_must_generalize.md`

### Symptom bugs require diagnosis before sprinting

Do not send symptom-only bugs into implementation sprints. A fix-ready bug
needs:

- the observed symptom,
- concrete evidence or reproduction,
- ruled-out hypotheses,
- the confirmed cause,
- the affected code path,
- and a fix-success observable.

Reviewing a bug fix requires checking both that the implementation matches the
plan and that the original symptom is actually gone. Otherwise the work silently
swaps from "fix the symptom" to "fix the guessed cause."

Source: `discipline_rca_and_symptom_verification.md`

### Non-runnable project work belongs in `forge todo`

Use `forge todo` and the `todo:draft` label for decisions, investigations,
architectural debt, and other milestone-relevant work that is not yet runnable
through the normal sprint pipeline. Do not invent a second "task issue" concept
for this repo.

Source: `project_forge_todo_story.md`

### Candidate lists should contain only open work

When proposing sprint candidates, dependency bundles, or follow-on work, filter
to open issues before surfacing them. Closed issues may be cited as resolved
context, but they should not appear in actionable candidate lists.

Source: `feedback_no_closed_issues.md`

### Mid-sprint new bugs are groomed-and-readied for the next sprint, not injected into the running sprint

When a running sprint surfaces a new bug or work item, capture it and bring it to
a runnable state through the normal intake steps
(`capture → shape → diagnose → groom`), then apply the `ready` label so ordinary
sprint selection picks it up in the *next* sprint. Do not modify the running
sprint's selected work.

There is no `forge queue` command — the `ready` label is the queue-for-next-sprint
convention, and it carries no ordering or priority semantics. Use
`forge status --ready [--milestone …]` to see the eligible set; it checks each
entry against the sprint shape gate and marks `BLOCKED:<verdict>` any issue the
gate would refuse, so the listing cannot disagree with sprint entry about
admissibility. Live injection of
new work into an in-flight sprint is deliberately out of scope; it belongs to the
v0.12+ autonomy roadmap. See ADR-0001 and `docs/guides/authoring.md`
(Mid-sprint workflow).

## Planning, Review, And Documentation Discipline

### Capture converged decisions in repo-visible artifacts

When a design discussion reaches a decision, capture it before the session ends.
Choose the storage based on the kind of decision:

- GitHub issue for actionable work or enforceable policy.
- ADR or checked-in doc for structural rationale.
- `CONVENTIONS.md` for project-level conventions and lessons that should apply
  to future contributors.

Do not rely on user memory as the default sink for project policy.

Historical records under `docs/` must be identifiable as records without
reading them: either they live in a record directory (`docs/archive/`,
`docs/postmortems/`, `docs/post-release-reviews/`), or they open with a status
line — `Status: record (YYYY-MM-DD[, issue #N])`, or the older
`Status: Shipped (vX.Y) … retained for historical context` form. A short
qualifier after the form is fine. Living documents need no marker; add
`Status: living` only where a document could otherwise be mistaken for a
record (an active plan in `docs/plans/`).
`docs/README.md` indexes the living-vs-record classification.

Source: `feedback_capture_decisions.md`

### Push back on alternatives with evidence, not adjectives

If rejecting a native or built-in alternative, cite a specific limitation,
command failure, repro, or sourced compatibility gap. "Beta-ish", "awkward",
and similar adjectives are not enough. When the evidence is missing, say that
explicitly and go verify it instead of freezing in a weak argument.

Source: `feedback_cite_pushback.md`

### Documentation reviews verify against code, not against other docs

When reviewing docs, treat code and actual command behavior as the source of
truth. Do not validate one document by comparing it to another document. The
point of the review is to catch drift between docs and the system itself.

Source: `feedback_doc_review_vs_code.md`

### Sprint DAG behavior should be obvious and collision diagnosis should prefer existing machinery

The sprint runner already supports DAG execution, `depends_on` ordering,
parallel execution of independent work, and automatic collision-driven edges
derived from overlapping `likely_files`. Document that capability prominently
and assume the dependency inference machinery exists before recommending manual
workarounds.

If collisions slip through, investigate why `likely_files` or edge inference did
not fire instead of defaulting to manual `depends_on`.

Sources: `feedback_dag_discoverable.md`, `project_collision_dag.md`

### Logical dependencies are declared at filing; scheduling dependencies are inferred

The convention above is about *scheduling* — do not hand-declare an edge to keep
two issues off the same files, because collision inference already does that from
`likely_files` and does it better. It is not a reason to leave a dependency
undeclared.

A **logical dependency** is a different thing and is never inferable. It exists
when one issue produces something another consumes: an interface, a contract, a
recorded fact, a vocabulary. The clearest case is one issue specifying an API and
another building against it, but it is the same relationship whenever the second
issue would have to invent, re-derive, or assume the first issue's output in
order to proceed. Nothing in the file set reveals this — two issues can share no
files at all and still stand in this relationship.

**Issues filed together must state their logical dependencies in the body at
creation.** Declare with YAML frontmatter at the very start of the body, and say
in prose what the downstream issue consumes and what goes wrong if it is built
first:

```
---
depends_on:
  - issue-2377
---
```

The prose half is the point. The edge is documentation of a development
relationship first and a scheduling input second — a reader picking up either
issue needs to know the relationship exists, and a reader deciding whether the
edge still holds needs to know what it was for. A bare frontmatter block orders
the work without explaining it, and is the first thing to go stale silently.

Two mechanical notes: comments are never parsed, so an edge added as a
`gh issue comment` is invisible to the scheduler no matter how clearly it is
worded; and fenced blocks, blockquotes and inline code are stripped before the
prose scan, so an edge mentioned only inside an example does not count. Prove the
result with `forge sprint --dry-run` and confirm more than one `batch=` value
before calling a queue ready.

Where a relationship is real but not a dependency — two issues that are halves of
one concern, or a fix that would make another easier to diagnose without being
required by it — cross-reference them in prose and do not declare an edge.
Ordering work that does not need ordering serialises it for nothing.

### Contract changes may require test updates

The default "do not modify unrelated tests" rule is correct, but prompt and
review maintainers should remember the exception: when a story intentionally
changes a behavioral contract, tests asserting the old contract are part of the
story and should change with it. Over-engineering around stale tests is a known
failure mode.

Source: `feedback_dev_prompt_test_rule.md`

## Workflow And Git Hygiene

### Never develop on `main`

All work goes on a feature branch in a worktree and moves through a PR. Direct
commits to `main` break active runs and destroy the audit trail this project is
built around.

Source: `feedback_no_direct_main.md`

### Never force-add ignored files

If `git add` refuses a file because it is ignored, stop and treat that as a
signal rather than something to override by default. Force-adding ignored files
pollutes history and has already caused avoidable merge conflicts in this repo.

Source: `feedback_no_force_add.md`

### Release and CI tooling lives in `scripts/`, not in the package

The "no files outside `src/`, `tests/`, `docs/`" convention carves out `scripts/`
for tooling that operates *on* the repository rather than shipping with it:
`cut-rc.sh`, `promote-rc.sh`, `release.sh`, `derive_changelog.py`,
`apply-branch-protection.sh`, `forward_port_guard.py`. The test is who calls it —
a Makefile target or a GitHub workflow, never `import theforge`. Such helpers
must still be unit-tested from `tests/` (see `tests/test_derive_changelog.py`,
`tests/test_apply_branch_protection.py`, `tests/test_forward_port_guard.py`),
because the release path breaks in exactly the places nothing exercises. Anything
importable by the package belongs under `src/`, and scratch files still belong
nowhere.

### Do not clean up worktrees or branches blindly

Before deleting a worktree or branch, verify that it has no unique work beyond
`main` and that no sprint is actively using it. Diverged worktrees can be live
state for running automation even when they look abandoned from a quick glance.

Source: `feedback_cleanup_caution.md`

### Gate failures after sprint escalation are pipeline signals

If a forge sprint escalates with a gate failure, do not patch the worktree
manually as a shortcut. Treat the failure as part of the pipeline's audit trail
and re-run via the forge process once the next step is chosen.

Source: `feedback_never_fix_gate.md`

### Review the commit you pushed, not the ref you assume

Pushing `HEAD:branch` from a detached worktree updates the remote ref and the
remote-tracking ref, but **not** the local branch ref. `git show branch:file`,
`git worktree add <path> branch`, and anything else resolving the local name then
silently serve an older commit. Reviewing a pushed change off the stale local ref
reads code that is not under review, and the findings that come back are about
nothing.

This has already produced a near-miss: a reported defect on an open PR was
derived from a superseded commit, and an independent review agent hit the same
trap on the same branch. Before reviewing or verifying a pushed change, confirm
what you are actually reading:

```bash
git rev-parse --short HEAD origin/<branch>
```

Prefer explicit SHAs or `origin/<branch>` for review worktrees, and after a
detached-worktree push, fast-forward the local ref
(`git update-ref refs/heads/<branch> refs/remotes/origin/<branch>`) so the two
cannot drift. A line-number mismatch against another reviewer's citations is a
symptom of this, not a disagreement.

### Manual PR intervention changes merge state

Force-pushes clear GitHub auto-merge, and `gh pr create` does not arm
auto-merge in the first place. After a manual PR intervention, check whether the
PR still has the intended merge state instead of assuming forge's original
automation still applies.

Operator permission still governs whether auto-merge should be re-armed; this
section records the platform behavior and the failure mode, not blanket
authorization to merge on someone's behalf.

Source: `feedback_auto_merge_after_intervention.md`

## Product And Architecture North Stars

### TheForge's core property is refusal-capable execution

TheForge is not valuable because it guesses faster. It is valuable when it knows
that work is not ready to implement and refuses to proceed with a legible reason.
That means refusing symptom bugs without diagnosis, stories without observable
acceptance criteria, ambiguous scope, and work with unresolved dependencies.

The full doctrine — failure mode, evaluation test, and worked examples — lives in
[`docs/vision/refusal-capability.md`](docs/vision/refusal-capability.md), the
sibling of the compound-engineering doctrine.

Source: `project_north_star.md`

### Use released TheForge to build unreleased TheForge

Dogfood on the latest released tag, not on editable `main`. Treat `main` as the
next release under construction, and decide whether new bugs block the released
dogfood floor or belong to the next minor. Distinguish the stability floor
("safe to ship") from the substrate bar ("good enough to build the next release
on"), and do not claim success merely because everything did not catastrophically
fail.

Heavyweight process should be spent only when it buys value; small or already
diagnosed work should not pay full ceremony tax without justification.

Source: `project_release_floor_dogfood.md`

### Review should stay commit-centric and PR-shaped

The architectural direction is HDP-style review:

- dev gets the spec and implements freely,
- commits are the primary handoff artifact,
- reviewers evaluate commits against the spec and project structure — never
  against a pre-groomed file scope or a diff-stat summary,
- and the audit trail lives in the repo rather than depending on GitHub.

Be skeptical of replacement metadata (expected-file lists, diff-stat digests)
that compensates for missing commit context instead of exposing the commits
directly — that pattern is the specific failure mode this rule rules out.

Full rationale, the failure mode it prevents, and the connection to the
audit-substrate trust model: [ADR-0005: Commit-Centric Review
Handoff](docs/adr/0005-commit-centric-review-handoff.md).

Sources: `project_commit_centric_review.md`, `project_hdp_vision.md`

### Project conventions should be explicit inputs, not rediscovered after the fact

TheForge should expose project conventions as first-class inputs to planning,
development, review, and validation rather than learning them only through
cleanup or refactor stories. This migration is part of that direction: project
policy belongs in repo-visible artifacts, not hidden operator memory.

Source: `project_conventions_input.md`

### Preserve full audit evidence when the platform is still learning

When choosing between thinner and richer audit capture, bias toward preserving
full agent outputs and actual routing evidence so failures can be diagnosed from
facts instead of reconstructed guesses. Historical analysis of quality,
proportionality, and churn depends on that data being present.

Source: `project_full_audit_trail.md`

### Module size is reported, never refused

`max_module_lines` is 600 and is advisory. Every module over it is reported with
its distance from the limit, and no story is ever refused for module size —
not for growing an already-large module, not for crossing the limit, not for
adding a large new module.

**Do not relocate code to satisfy a line count.** If the module a change belongs
in is already large, put the change there anyway. Extract when a responsibility
genuinely separates from its neighbours, not when a file gets long: a module
carved at whatever line got the count under a threshold is worse than the large
module it came from, because the seam is arbitrary and the coupling survives as
import statements between the pieces.

Bringing the large modules down is real work, sequenced and funded as its own
stories, and it starts by naming the abstraction the module is missing — not by
splitting files. A story that arrives at a large module is not the mechanism for
that and should not try to be.

ADR-0008 briefly enforced this as a blocking ratchet and it was withdrawn the
same day; the ADR records why, and the reasoning generalises past module size to
any codebase-scoped property enforced at story scope.

Source: `docs/adr/0008-module-size-ratchet.md`

### Review-cycle churn usually means missing cross-cycle context first

When sprints churn through repeated review cycles, the first hypothesis should
be missing cross-cycle dev memory and plan anchoring rather than immediately
assuming the underlying bug is inherently hard. The known anchor for that gap is
issue `#297`; older names for the same diagnosis are obsolete.

Source: `project_churn_root_cause.md`

## Historical Project Notes

These notes are preserved because they are project-scoped and still useful
context, but they are time-bound. Verify them against the current codebase,
milestones, and issue tracker before relying on them as current state.

### Verify stale-sequencing claims before repeating them

As of 2026-04-20, `forge check-config` was already shipped and auth
normalization already existed as issue `#291`, with much of the implementation
apparently in place. The durable lesson is not the exact status snapshot; it is
that project memory ages quickly and should be re-audited before making
sequencing claims.

Source: `project_checkconfig_sequence.md`

### Epic representation was revisited and resolved

The prior epic convention (`Epic:` title prefix with prose tracking headers,
no native linkage) was compared against GitHub-native sub-issues and
superseded. Epics now use native sub-issues to enumerate their slices, use
bounded framing instead of "tracked here forever," and never carry a work
milestone — see the Epic section in `docs/guides/authoring.md`. This note is
retained only as a historical record of the earlier undecided state.

Source: `project_epic_representation_origin.md`

### Coordinator phase ownership currently maps to split modules

The old `phases.py` monolith was split into phase-owned modules such as
`dev_phase.py`, `review_phase.py`, and `validate_phase.py`, with corresponding
test splits. Treat this as a useful codebase map for contributors touching those
areas, not as a license to stop verifying the current layout.

Source: `project_phase_module_ownership.md`

### Milestone-theme notes are historical context, not standing policy

The following milestone memories are preserved for auditability:

- `v0.8.0` was framed as adaptive intelligence and config simplification.
- One `v0.10.0` memory framed the milestone as compounding memory.
- Another `v0.10.0` memory framed it as milestone-scale autonomy built around
  four epics.

Because those notes conflict and can age, use the live milestone description,
issue set, and roadmap docs as the authority before planning work.

Sources: `project_v080_adaptive.md`, `project_v010_milestone.md`,
`project_v010_milestone_autonomy.md`

## Migration Scope

This document is the destination for the project-level subset of the old memory
directory, including both evergreen rules and time-bound project notes. The
memory index itself was superseded by `docs/memory-migration.md`, which records
where every source file landed and which files deliberately remain user-local.

Sources: `MEMORY.md`, `docs/memory-migration.md`

## Current State - Start Here

To understand what's in progress and what's next, check the open milestone on
GitHub (https://github.com/fuzzypete/theforge/milestones), then:
```bash
gh issue list --milestone "<open milestone>"   # issues in the current milestone
gh project item-list 1 --owner fuzzypete       # full project board
```

Project board: https://github.com/users/fuzzypete/projects/1

Stories are GitHub issues — there are no local story files. GH milestones + issues
are the single source of truth for priorities, status, and story content.

## Interactive Development Workflow

All interactive dev work — whether by a human or an agent in an interactive session —
**must follow this sequence without exception:**

1. **Create a GitHub issue** describing the change (story format: WHAT + WHY, not HOW).
   ```bash
   gh issue create --title "..." --body "..."
   ```

2. **Create a worktree and branch** tied to that issue number.
   ```bash
   git worktree add .forge/worktrees/issue-<N> -b feat/issue-<N>
   cd .forge/worktrees/issue-<N>
   ```

3. **Commit changes referencing the issue** so the audit trail is complete.
   ```bash
   git commit -m "fix: description (#<N>)"
   ```
   Every commit must reference the issue number. Do not commit directly to main.

4. **Verify the gate passes** — run `make gate` locally to confirm lint, format, and
   tests all pass (exit 0). Print the summary here in the CLI session.
   ```bash
   make gate   # lint + format check + pytest; passes on exit 0
   ```
   Work is not done until `make gate` exits 0 and all tests pass.

**None of these steps are optional.** Starting to code before the issue exists, or
finishing without a passing gate, leaves work unreviewed and untracked.

## Directory-Level Guidance

Directory-level `CONVENTIONS.md` files under `src/theforge/` provide subsystem-specific
guidance for major areas of the codebase. Consult them before making changes in
those directories, especially:
- `coordinator/`
- `runners/`
- `sprint/`
- `task/`
- `config/`
- `cli/`

## Architecture

**The coordinator (not an LLM) makes all process decisions.** Every state transition
is deterministic Python code. Agents only write code and write reviews. The coordinator
validates boundaries mechanically.

State machine: `INIT → WORKSPACE → PREFLIGHT → PLAN → PLAN_REVIEW → DEV → VALIDATE → REVIEW → DONE/ESCALATE`, plus `HUMAN_REVIEW` (interactive operator pause after review) and `MERGE_FAILED` (auto-merge could not land the story) — see the `Phase` enum in `src/theforge/coordinator/state.py` for the full set.

Key modules:
- `src/theforge/coordinator/engine.py` — state machine, the heart of the system
- `src/theforge/coordinator/` — all coordinator phases (dev_phase, review_phase, validate_phase, plan_flow, preflight_flow, workspace, etc.)
- `src/theforge/runners/` — API and CLI agent runners; adapters per provider
- `src/theforge/config/` — forge.yaml parsing and model profiles
- `src/theforge/task/` — prompt builders (dev, review, plan, preflight)
- `src/theforge/review.py` — review output parsing
- `src/theforge/schemas.py` — review schema validation
- `src/theforge/cli/main.py` — `forge` CLI entry point
- `src/theforge/sprint/` — sprint lifecycle, DAG scheduler, GitHub query

## Key Commands

```bash
make fmt        # ruff format + ruff check --fix (auto-fix)
make lint       # ruff check + ruff format --check (no auto-fix)
make test       # pytest tests/ -v
make gate       # forge index + check-story-config + lint + format check +
                # tests, under a scrubbed env (exit code only, no file written)
```

`make gate` is the single standard: it is what `validation.gate_command` runs
for a story and what the required merge check runs in CI
(`.github/workflows/ci.yml`). The one deliberate difference between the two is
platform — stories are gated on the macOS development host, CI on
`ubuntu-latest` — kept on purpose to catch host-specific assumptions. Do not
add a second, independently composed command list to either side; a merge check
that is not a superset of the story gate is not a gate (#1945).

### Language and toolchain agnosticism
TheForge is a generic orchestrator — it must work for Python, Node, Go, Java, Rust,
or any other stack. Coordinator logic, prompt templates, task schemas, and CLI
scaffolding must not assume a specific language, test framework, or build tool.

#### Concrete convention rules
- **Core orchestrator modules must be stack-neutral.** Code in shared coordinator,
  task, sprint, and related config layers must not bake in assumptions about one
  language, package manager, test runner, or repository layout.
- **Shared schemas may not encode stack-specific concepts.** `TaskStory`,
  `ForgeConfig`, coordinator state, and other shared models must not introduce
  fields like `pytest_target`, `npm_script`, or similar stack-shaped concepts.
  Use generic names such as `test_target`, `gate_command`, and
  `gate_debug_command`.
- **Prompt templates must reference configured commands, not literal tool
  invocations.** Reusable prompts should talk about the configured gate/test
  commands rather than embedding `make fmt`, `pytest`, `npm test`, `cargo test`,
  or `go test`.
- **Generated scaffolding must use generic names or omit the concept.** Reusable
  examples and templates should prefer neutral placeholders like `test_target`
  instead of assuming `tests/`, `src/`, `docs/`, or a language-specific layout.
- **Stack-specific assumptions belong in `forge.yaml` or repo-local conventions,
  not TheForge core.** Repo-local dogfooding config, self-hosting examples, and
  clearly marked stack-specific docs may be specific; shared orchestrator code may
  not.

#### Reviewer smell list
Treat the following as concrete smells in stack-neutral layers:
- Shared models with `pytest_`, `npm_`, `cargo_`, `maven_`, or `gradle_` prefixes
- Core prompt templates containing literal `make fmt`, `pytest`, `npm test`,
  `cargo test`, or `go test`
- Reusable prompt logic that hardcodes `src/`, `tests/`, or `docs/`
- Language-specific story parsing in shared orchestrator code

#### Mechanical enforcement scope
The hard conventions check scans only stack-neutral layers:
- `src/theforge/task/`
- `src/theforge/coordinator/`
- `src/theforge/sprint/`
- shared schema modules
- relevant shared config modules under `src/theforge/config/`

It intentionally exempts repo-local dogfooding config such as `forge.yaml`,
provider/adapter code, migration tests that mention old names, and docs/examples
that are clearly marked as Python-specific examples.

## Pipeline Phases

### Preflight is a reasoning task, not a cheap classifier

Preflight looks like a classifier (structured YAML output, one-shot call) but the
work is heavy. It must:

1. **Read the codebase** and verify every acceptance criterion against actual code
   to determine PROCEED / ALREADY_DONE / BLOCKED
2. **Assess complexity** — emit a 1–10 `complexity_score`; the small/medium/large
   bands are derived from the score via a compat shim (`score_to_band` in
   `coordinator/preflight.py`) for consumers that still read the string enum.
   This drives adaptive model selection for all downstream phases
3. **Classify sufficiency** (implementation_ready/needs_planning) — controls whether
   the plan phase runs at all
4. **Classify work type** (feature/refactor/mechanical/bug) — feeds prompt construction
5. **Drive adaptive assignment** — complexity feeds `assign_models()` which picks
   agent tiers, escalation history, reviewer pool selection

A wrong ALREADY_DONE wastes a correct implementation. A wrong PROCEED on finished
work burns $20+ on dev+review for nothing. A wrong complexity classification puts
the wrong model on the job. **Do not suggest replacing preflight with a cheap/fast
model.** Spending ~$0.30 on a careful classification that controls $20-50 of
downstream spend is correct. (TheForge now derives the preflight model
adaptively from the `models:` list rather than pinning a single model — the
principle stands regardless of which model wins the cheap-bucket tie-break.)

## Conventions

### Coordinator seam changes require integration tests
Changes that affect coordinator phase boundaries, state handoff between phases, or adaptive routing/config propagation must include seam-level integration tests covering the touched boundary. Unit tests alone are insufficient when correctness depends on cross-phase state flow.

### No LLM in the loop for process decisions
<!-- forge-invariant id="coordinator-pure-python" scope="area:coordinator phase:plan,dev,review files:src/theforge/coordinator/*.py" enforcement="review" -->
The coordinator is pure Python. If you find yourself writing code where an LLM
decides whether to retry or escalate, stop — that decision belongs in the coordinator.
<!-- /forge-invariant -->

### Schema enforcement is mandatory
<!-- forge-invariant id="schema-integrity-boundary" scope="area:schema area:review phase:plan,dev,review files:src/theforge/schemas.py" enforcement="gate" -->
The review output schema in `schemas.py` is the integrity boundary. Do not relax
cross-validation rules (APPROVE+P1 or REQUEST_CHANGES+no P1 are always errors).
<!-- /forge-invariant -->

### Review YAML structure
```yaml
verdict: APPROVE | REQUEST_CHANGES
summary: "<one-line>"
findings:
  - severity: P1 | P2
    file: "<path>"
    line: <number or null>
    description: "<what is wrong>"
    suggestion: "<how to fix>"
story_compliance:
  matches_spec: true | false
  mismatches: []
test_coverage:
  adequate: true | false
  gaps: []
ac_verification:            # APPROVE requires a non-empty table, all VERIFIED
  - criterion: "<acceptance criterion text, or 'Symptom resolution' for bugs>"
    status: VERIFIED | PARTIAL | NOT_VERIFIED
    evidence: "<diff hunks + test pointers for VERIFIED; reason otherwise>"
```

The one escape valve: an APPROVE with an empty `ac_verification` table is legal
only when the reviewer declares `criteria_enumerable: false` with a non-empty
`criteria_enumerable_rationale` (see `schemas.py`).

### Writing stories
Stories describe WHAT and WHY — never HOW. The plan phase produces the HOW.

- **No function names, class names, or file paths** unless the story IS about
  a specific file (e.g., a refactoring story). The plan agent will find these.
- **Acceptance criteria describe observable behavior**, not implementation steps.
  "Warns on unmapped acceptance criteria" ✓. "Calls `validate_plan()` in
  `coordinator.py` after line 1460" ✗.
- **If preflight can't understand a story without reading the codebase, the
  story is too implementation-coupled.** Preflight should be able to classify
  it from the story text alone.
- **`## Notes` is for soft hints, not requirements.** Use a Notes section to
  capture file paths, patterns, or gotchas discovered during investigation.
  Notes are informational — they may be stale or wrong by the time the story
  runs. Agents are instructed to verify Notes against the codebase. Never put
  acceptance criteria or requirements in Notes.
- The primary term is "story" throughout the codebase. `TaskSpec` is a
  backward-compat alias for `TaskStory`; prefer `TaskStory` in new code.

### Writing bug reports
Bug reports contain exactly two things:

1. **What happened** — the observed behavior, with evidence (log lines, audit
   trail entries, run IDs).
2. **What was expected** — the correct behavior.

That's it. No acceptance criteria, no implementation hints, no file paths, no
suggestions about which module or function to fix. The dev agent should discover
the fix from the codebase. Over-constraining the report biases the agent toward
a specific fix path that may not be the right one.

### Dogfooding config
`forge.yaml` at the project root configures theforge to develop itself. Worktrees
land in `.forge/worktrees/<slug>/` on branch `feat/<slug>`.

## Testing

- All tests must pass before committing
- New coordinator behaviour → add a `tests/test_coord_*.py` file matching the phase
- New runner behaviour → `tests/test_runner_*.py`
- `make gate` runs in a scrubbed environment: agent credentials, CLI auth state, and dotenv autoload inputs are stripped before tests execute.
- **Never invoke real provider CLIs** (`claude`, `codex`, `gemini`, etc.) in the default gate — they require credentials, cost money, and are non-deterministic. Any forgotten real-CLI call should fail fast under the gate scrub sentinel.
- **Use fake-CLI subprocess fixtures for runner lifecycle tests.** Runner tests that exercise subprocess lifecycle (pipe semantics, stdin/stdout EOF, process exit timing, watchdog behaviour) must use a real subprocess with a fake binary (see `tests/fake_bin/`). Mocking `subprocess.Popen`/`subprocess.run` is appropriate only for non-runner code paths where lifecycle semantics are not under test.
- Tests that legitimately require real credentials must be marked `@pytest.mark.network_integration` and run via `make test-integration`; they are not part of `make gate`.
- **Never use `fcntl.flock` in tests that also use `threading`.** pytest runs with
  `-n auto --dist worksteal` (xdist), which forks worker processes. A forked worker
  inherits open file descriptors with held locks, causing sibling threads to block
  indefinitely — deadlock, memory balloon, and eventual OOM. Mock the lock instead.
- **Never write tests that can hang.** No `while True`, no unbounded retry loops,
  no `time.sleep()` longer than 1 second, no blocking I/O without a timeout, no
  `threading.Event.wait()` without a timeout. **Every test must complete in under
  5 seconds run on its own** — that is the convention you write to, and it is a
  property of the test. A hanging test kills the entire gate run for every story
  in the sprint.
  - **The gate enforces a wider bound than the convention, and deliberately so.**
    `pyproject.toml` declares `timeout = 60` and `timeout_method = "thread"`
    under `[tool.pytest.ini_options]`, so every invocation of the suite — the
    gate, `make dev-check`, a bare `pytest` in a terminal — inherits a single
    shared 60s per-test bound without a flag. A test that runs past it is failed
    by name with a stack trace of where it was stuck, so the run stays
    attributable instead of dying anonymously.
  - **Sixty, not five, because of what the concurrent gate actually measures.**
    Under `-n auto --dist worksteal` a per-test wall clock measures the test
    *plus* scheduling delay, CPU contention, cold imports, and subprocess
    pressure. Measured inflation on this suite reached roughly 9x: a 3.44s test
    crossed a 30s bound. A tight threshold there is not a correctness verdict,
    it is a load reading reported as a test failure — in two days it produced an
    unstable verdict on unchanged code (#2825) and a baseline that refused every
    sprint until cleared by hand (#2831). The shared bound is now set above
    anything this suite's own contention produces, so a timeout means the test
    is stuck (#2833). Do not tighten it back without evidence that inflation
    cannot reach the smaller value.
  - **The tight per-test judgement is made in the post-stall serial
    diagnostic.** When the ordinary gate exceeds its outer budget, that pass
    re-runs serially under its own tight bound (shipped default 10s per test,
    60s for the whole pass) to name the culprit. That is where load is
    controlled, so a wall-clock threshold measures the test rather than the
    machine. Its bound and its role are unchanged by any of the above.
  - The method must stay `thread`. Measured against the `-n auto --dist
    worksteal` addopts, the `signal` method parks every xdist worker at 0% CPU
    and the run never finishes — the same lock-inheritance hazard as the
    `fcntl.flock` rule above.
  - The thread method ends a timed-out test with `os._exit(1)`, which in an
    xdist worker kills the process before pytest-timeout's stack dump reaches
    the controller. `tests/timeout_enforcement.py` recovers it by wrapping
    `pytest_timeout.timeout_timer`: on the way out the worker writes the
    culprit's nodeid and stacks to a file, and the controller re-emits it
    under a "per-test timeout stack dumps" heading. The wrapper runs on
    pytest-timeout's existing timer thread and does no per-test work — do not
    replace it with a per-test `faulthandler.dump_traceback_later`, which was
    measured deadlocking this suite at 99% for the same lock-inheritance
    reason as above. Keep the module loaded: a child pytest project with its
    own rootdir needs `-p timeout_enforcement`, and setting
    `THEFORGE_TIMEOUT_DUMP_DIR` hands a run a dump directory it will use and
    leave in place, which is how a dump survives a serial run whose timeout
    kills the controller itself.
  - A test may **shorten** its own bound with `@pytest.mark.timeout(n)`.
    Nothing may widen it, disable it (`0` or negative), or change how it is
    enforced (`method=`, `func_only=True`); `tests/timeout_enforcement.py`
    rejects those at collection and fails the offending test by name.
  - **One bound, no exemptions, no category.** No test in the default suite
    carries a bound above the shared one and there is no mechanism to grant
    one. The `orchestration` marker, the 30s category, and
    `tests/orchestration_scope.py` — which derived category membership by
    parsing test source — were removed rather than widened (#2833). Nothing
    needs exempting once the shared bound is above what contention produces,
    and every attempt to classify *which* tests deserve headroom re-litigated
    a question whose real variable was load. Do not reintroduce a marker, a
    category, or a list of nodeids: a list can only ever be complete up to the
    last red gate, and the tests it is missing announce themselves by failing
    a release cut that a re-run turns green — which is the gate lying (#2825).
- **Never import optional provider SDKs unconditionally in tests.** Tests must pass whether the environment has `.[dev]` or `.[all,dev]` installed. Mock or stub provider SDK boundaries.

### Flake discipline

A flaky gate is a trust violation: a red-when-it-should-be-green (or the reverse)
gate is the gate lying, and combined with auto-merge a lucky re-run can land
unreviewed code (this happened on 2026-07-17 — a flaky gate blocked a PR, the
re-run passed, and armed auto-merge landed out-of-scope code, #1717).
The rules:

- **Fix flakes by removing nondeterminism, not by retrying.** Retrying a flaky
  test masks it and trains everyone to ignore red — worse than the flake. Remove
  the timing/ordering assumption: synchronize on the actual condition (poll the
  observed state with a bounded timeout), not on a `sleep`, a schedule, or an
  assumed ordering. Do **not** add retry-on-flake and do **not** widen a timeout
  to chase a contention flake — a wider window hides the race instead of removing
  it.
- **A green-on-re-run after a same-SHA red is a flake signal, not a pass.** If the
  gate goes red and a plain re-run of the *same commit* goes green, treat the test
  that flipped as flaky. Do not merge on the green — file the flake first.
- **Every known flake is an open issue labelled `flake`** (test id, symptom,
  expected determinism). The register is the label query, nothing else:

  ```bash
  gh issue list --label flake --state open
  ```

  Issue state is the flake's state — there is no separate ledger to reconcile,
  and a flake without an issue does not exist as far as burndown is concerned.
  The sprint baseline gate re-runs an unreproduced failure once and records it
  (`baseline_gate_failure_not_reproduced` in the sprint log, `failure_reproduced:
  false` in the audit's `baseline_check`); each of those is a flake candidate to
  file, not a pass to forget.

## Cutting a Release

The full release process is documented in [`RELEASING.md`](RELEASING.md). Use the
script — do not run steps manually:

```bash
scripts/release.sh X.Y.Z          # release
scripts/release.sh --dry-run X.Y.Z # preview
```

Key points:
- Verify the CHANGELOG release section against the milestone and commit range
  before tagging; GitHub release notes are generated from that section.
- Tag and push **before** bumping back to `X.Y.Z+1.dev0`
- Hotfixes branch from `release/vX.Y`, not `main`

## What NOT to do

- Do NOT have the coordinator call an LLM for routing decisions
- Do NOT merge to main without a PASS gate + review APPROVE
- Do NOT skip `make fmt` before committing
- Do NOT relax schema validation to make tests pass
- Do NOT suggest replacing preflight with a cheap/fast model — it is load-bearing
- Do NOT modify CLAUDE.md or AGENTS.md unless the story explicitly requires it
