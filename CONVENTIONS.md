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

## Planning, Review, And Documentation Discipline

### Capture converged decisions in repo-visible artifacts

When a design discussion reaches a decision, capture it before the session ends.
Choose the storage based on the kind of decision:

- GitHub issue for actionable work or enforceable policy.
- ADR or checked-in doc for structural rationale.
- `CONVENTIONS.md` for project-level conventions and lessons that should apply
  to future contributors.

Do not rely on user memory as the default sink for project policy.

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
- reviewers evaluate commits against the spec and project structure,
- and the audit trail lives in the repo rather than depending on GitHub.

Be skeptical of replacement metadata that compensates for missing commit context
instead of exposing the commits directly.

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

### Epic representation was chosen under incomplete comparison

The current epic convention (`Epic:` title prefix plus labeling and tracking
headers) emerged without a strong comparison against GitHub-native sub-issues
and relationships. If epic representation is revisited, compare the current
convention directly against the platform-native alternative rather than assuming
the original pushback still holds.

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
