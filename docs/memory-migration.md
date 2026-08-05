# Memory Migration Audit

Status: record — one-time migration audit.

This document records the migration of project-level lessons out of
`~/.claude/projects/-Users-pwickersham-src-theforge/memory/` into
repo-versioned guidance.

Classification rule:

- Project-level: applies to any contributor or future TheForge dev/review agent,
  or records project-scoped context that should travel with the repo.
- User-local: specific to one operator's shell, timezone, command defaults,
  merge permission, verbosity preference, or interaction style.

Project-level files moved into `CONVENTIONS.md`: 36

User-local files retained in user memory: 11

User-local files remain in user memory unchanged. The source memory files were
not edited by this migration; `CONVENTIONS.md` is now the canonical repo copy
for the project-level subset.

## Project-level files moved into `CONVENTIONS.md`

| Memory file | Destination in `CONVENTIONS.md` | Notes |
| --- | --- | --- |
| `MEMORY.md` | `## Migration Scope` | Old index superseded by this audit doc and the new root conventions file. |
| `discipline_rca_and_symptom_verification.md` | `### Symptom bugs require diagnosis before sprinting` | Preserved as bug-intake and review discipline. |
| `feedback_acs_required.md` | `### Feature and enhancement issues must be runnable at creation time` | AC requirement for non-bug stories. |
| `feedback_auto_merge_after_intervention.md` | `### Manual PR intervention changes merge state` | Preserved the GitHub behavior and failure mode, with operator permission kept separate. |
| `feedback_bug_expected_must_generalize.md` | `### Bug stories stay minimal and the expected behavior must generalize` | Preserved both generalization and prose-shape rules. |
| `feedback_bug_story_format.md` | `### Bug stories stay minimal and the expected behavior must generalize` | Preserved observed-vs-expected-only bug format. |
| `feedback_capture_decisions.md` | `### Capture converged decisions in repo-visible artifacts` | Migration-specific boundary rule now points to repo artifacts first. |
| `feedback_cite_pushback.md` | `### Push back on alternatives with evidence, not adjectives` | Preserved evidence-first comparison discipline. |
| `feedback_cleanup_caution.md` | `### Do not clean up worktrees or branches blindly` | Preserved worktree cleanup safety rule. |
| `feedback_dag_discoverable.md` | `### Sprint DAG behavior should be obvious and collision diagnosis should prefer existing machinery` | Preserved discoverability requirement. |
| `feedback_dev_prompt_test_rule.md` | `### Contract changes may require test updates` | Preserved as a prompt-maintainer caveat. |
| `feedback_doc_review_vs_code.md` | `### Documentation reviews verify against code, not against other docs` | Preserved code-as-ground-truth review rule. |
| `feedback_examples_in_features.md` | `### Feature and documentation issues should include a concrete example` | Preserved example-first issue shaping. |
| `feedback_issue_labels.md` | `### Feature and enhancement issues must be runnable at creation time` | Preserved label-at-creation rule. |
| `feedback_never_fix_gate.md` | `### Gate failures after sprint escalation are pipeline signals` | Preserved no-manual-hotfix discipline for escalated sprints. |
| `feedback_no_closed_issues.md` | `### Candidate lists should contain only open work` | Preserved issue-filtering rule. |
| `feedback_no_direct_main.md` | `### Never develop on \`main\`` | Preserved worktree-and-PR-only workflow. |
| `feedback_no_force_add.md` | `### Never force-add ignored files` | Preserved ignored-file hygiene. |
| `feedback_retro_to_story_temptation.md` | `### Story bodies describe WHAT and WHY, not HOW` | Preserved "retros do not justify HOW in stories" rule. |
| `feedback_what_not_how.md` | `### Story bodies describe WHAT and WHY, not HOW` | Preserved WHAT/WHY-only story shaping with high-level capability carve-out. |
| `project_checkconfig_sequence.md` | `### Verify stale-sequencing claims before repeating them` | Preserved as time-bound project context. |
| `project_churn_root_cause.md` | `### Review-cycle churn usually means missing cross-cycle context first` | Preserved root-cause diagnosis and anchor issue. |
| `project_collision_dag.md` | `### Sprint DAG behavior should be obvious and collision diagnosis should prefer existing machinery` | Preserved automatic collision-edge reminder. |
| `project_commit_centric_review.md` | `### Review should stay commit-centric and PR-shaped` | Preserved commit-first review model. |
| `project_conventions_input.md` | `### Project conventions should be explicit inputs, not rediscovered after the fact` | Preserved motivation for a first-class conventions system. |
| `project_epic_representation_origin.md` | `### Epic representation was chosen under incomplete comparison` | Preserved rationale gap and revisit cue. |
| `project_forge_todo_story.md` | `### Non-runnable project work belongs in \`forge todo\`` | Preserved shipped status and canonical label. |
| `project_full_audit_trail.md` | `### Preserve full audit evidence when the platform is still learning` | Preserved audit-visibility principle. |
| `project_hdp_vision.md` | `### Review should stay commit-centric and PR-shaped` | Preserved HDP origin and PR-shaped review model. |
| `project_north_star.md` | `### TheForge's core property is refusal-capable execution` | Preserved product north star. |
| `project_phase_module_ownership.md` | `### Coordinator phase ownership currently maps to split modules` | Preserved as codebase map, marked time-bound. |
| `project_release_floor_dogfood.md` | `### Use released TheForge to build unreleased TheForge` | Preserved release-floor dogfood policy and proportionality rule. |
| `project_stories_gh_only.md` | `### TheForge uses GitHub issues as stories in this repo` | Preserved repo-vs-tool distinction for story storage. |
| `project_v010_milestone.md` | `### Milestone-theme notes are historical context, not standing policy` | Preserved as dated milestone context. |
| `project_v010_milestone_autonomy.md` | `### Milestone-theme notes are historical context, not standing policy` | Preserved conflicting dated milestone framing for auditability. |
| `project_v080_adaptive.md` | `### Milestone-theme notes are historical context, not standing policy` | Preserved as dated milestone context. |

## User-local files retained in user memory

| Memory file | Why it stays user-local |
| --- | --- |
| `feedback_handoff_print.md` | Interactive-output preference for how this operator wants handoffs shown. |
| `feedback_investigate_dont_fix.md` | Conversation-level preference about when to stop at diagnosis versus proceed to implementation. |
| `feedback_merge_gate.md` | Operator permission boundary about when this user wants merges or auto-merge armed. |
| `feedback_no_background.md` | Operator cost/visibility preference for foreground vs background execution. |
| `feedback_share_opinions.md` | Collaboration-style preference about pushback and proactivity. |
| `feedback_spec_confirmation.md` | Operator workflow preference for confirming spec intent before implementation. |
| `feedback_sprint_command.md` | This operator's preferred sprint flags and command shape. |
| `feedback_sprint_resume.md` | Operator-specific retry habit plus a temporary local workaround for current resume behavior. |
| `feedback_verbose.md` | Explicitly called out in the story as an example of a user-local verbosity preference. |
| `feedback_zsh_shell_blocks.md` | Shell-specific pasteability rule tied to the operator's local zsh configuration. |
| `user_timezone.md` | Personal timezone preference. |

## Result

The canonical project-level guidance is now repo-versioned and discoverable by
anyone who clones the repo. Only the 11 user-local files above still need
memory propagation, which makes the separate propagation story materially
smaller and lower-priority than it was before this migration.
