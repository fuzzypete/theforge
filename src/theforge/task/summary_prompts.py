"""Prompt construction for the post-run knowledge summary agent.

The summary agent summarises — it does not decide. It receives a bounded
rendering of one completed run's audit record (story, plan steps, changed files,
review findings, iteration/cost signals) plus the exact catalogue of references
it is permitted to cite, and returns YAML.

This module only builds the prompt string. The output schema and its validation
live in ``theforge.knowledge_summary``; the coordinator control flow that
invokes the agent and persists the artifact lives in
``theforge.coordinator.knowledge_summary_flow``.
"""

from __future__ import annotations

from typing import Any

from theforge.knowledge_summary import RunAnchors

# Bounds on what the prompt renders. A summary is a distillation — it does not
# need the whole record, and an unbounded render would make the cheap post-run
# call the expensive one.
_MAX_STORY_CHARS = 4000
_MAX_FINDINGS = 30
_MAX_STEPS = 30
_MAX_FILES = 60
_MAX_DESCRIPTION = 300


def _bullets(items: list[str], empty: str = "(none)") -> str:
    return "\n".join(f"  - {item}" for item in items) if items else f"  {empty}"


def _render_plan_steps(audit: dict) -> str:
    plan = ((audit.get("phases") or {}).get("plan") or {}).get("plan_structured")
    if not isinstance(plan, dict):
        return "  (no structured plan — this run did not plan)"
    lines = []
    for step in (plan.get("steps") or [])[:_MAX_STEPS]:
        if not isinstance(step, dict):
            continue
        desc = str(step.get("description") or "")[:_MAX_DESCRIPTION]
        lines.append(f"step_id {step.get('id')}: {desc}")
    return _bullets(lines)


def _render_findings(audit: dict) -> str:
    lines = []
    for entry in (audit.get("finding_registry") or [])[:_MAX_FINDINGS]:
        if not isinstance(entry, dict):
            continue
        desc = str(entry.get("description") or "")[:_MAX_DESCRIPTION]
        lines.append(
            f"finding_id {entry.get('finding_id')} "
            f"[{entry.get('severity')}] "
            f"cycles {entry.get('cycle_first_seen')}–{entry.get('cycle_last_seen')} "
            f"({entry.get('disposition')}) {entry.get('file') or ''}: {desc}"
        )
    return _bullets(lines)


def _render_changed_files(audit: dict) -> str:
    files = (audit.get("changed_files") or {}).get("files") or []
    lines = []
    for entry in files[:_MAX_FILES]:
        if not isinstance(entry, dict):
            continue
        lines.append(f"{entry.get('path')} (+{entry.get('insertions')}/-{entry.get('deletions')})")
    return _bullets(lines)


def _render_cycles(audit: dict) -> str:
    lines = []
    for review in audit.get("reviews") or []:
        if not isinstance(review, dict):
            continue
        summary = str(review.get("summary") or "")[:_MAX_DESCRIPTION]
        lines.append(f"cycle {review.get('cycle')}: {review.get('verdict') or ''} {summary}")
    return _bullets(lines)


def _render_signals(audit: dict) -> str:
    preflight = audit.get("preflight") or {}
    iterations = audit.get("iterations") or {}
    cost = audit.get("cost") or {}
    return _bullets(
        [
            f"work_type: {preflight.get('work_type')}",
            f"complexity: {preflight.get('complexity')} "
            f"(score {preflight.get('complexity_score')})",
            f"domains: {', '.join(str(d) for d in (preflight.get('domains') or [])) or '(none)'}",
            f"dev iterations: {iterations.get('dev_iterations_productive')}",
            f"review cycles: {iterations.get('review_cycles_total')}",
            f"plan regenerated: {(audit.get('plan_review') or {}).get('regenerated')}",
            f"total cost usd: {cost.get('total_usd')}",
        ]
    )


def _render_anchor_catalogue(anchors: RunAnchors) -> str:
    def _listing(label: str, key: str, values: frozenset[str]) -> str:
        if not values:
            return f"  {label}: (none — you may not cite this type)"
        shown = sorted(values)[:_MAX_FILES]
        return f"  {label} ({key}): {', '.join(shown)}"

    return "\n".join(
        [
            _listing("review_finding", "finding_id", anchors.finding_ids),
            _listing("plan_step", "step_id", anchors.plan_step_ids),
            _listing("review_cycle", "cycle", anchors.review_cycles),
            _listing("file", "path", anchors.file_paths),
            _listing("diff", "ref", anchors.diff_refs),
        ]
    )


def build_run_summary_prompt(audit: dict[str, Any], anchors: RunAnchors) -> str:
    """Build the bounded summary prompt for one completed run.

    The anchor catalogue is rendered verbatim into the prompt because the
    validator rejects anything outside it: telling the model exactly which
    references resolve is what makes the mechanical check something it can
    satisfy rather than something it fails blind.
    """
    task = audit.get("task") or {}
    story = str(task.get("story_text") or "")[:_MAX_STORY_CHARS]
    issue = task.get("github_issue")
    issue_ref = f"#{issue}" if issue else str(task.get("slug") or "")

    return f"""You are summarising a completed TheForge run into a structured knowledge
artifact that future runs may read. Summarise — do not judge, do not recommend,
do not propose follow-up work.

## The run

Story: {task.get("name")} ({issue_ref})
run_id: {audit.get("run_id")}

Story text:
{story or "(not recorded)"}

Plan steps:
{_render_plan_steps(audit)}

Changed files:
{_render_changed_files(audit)}

Review findings:
{_render_findings(audit)}

Review cycles:
{_render_cycles(audit)}

Run signals:
{_render_signals(audit)}

## Evidence rules (mechanically enforced)

Every entry in `what_was_learned` MUST carry a non-empty `evidence` list, and
every evidence item MUST cite one of the references below **exactly as
written**. A citation that is not in this catalogue is rejected and the whole
summary is discarded — so cite only what is listed, and omit any claim you
cannot back:

{_render_anchor_catalogue(anchors)}

Evidence item shapes:
  - {{type: review_finding, finding_id: <id>, description: "..."}}
  - {{type: plan_step, step_id: <id>, description: "..."}}
  - {{type: review_cycle, cycle: <n>, description: "..."}}
  - {{type: file, path: <path>, description: "..."}}
  - {{type: diff, ref: <sha>, description: "..."}}

A broad claim you cannot cite is worse than no claim. Omit it.

## What counts as a lesson (mandatory filter)

`what_was_learned` is for claims that would change a *future* run's plan,
implementation, or verification. Before including a claim, ask: if a future
run read only this claim, would it act differently? If not, it is not a
lesson — do not include it.

Disqualified — do NOT emit these, no matter how the sentence is phrased:
  - Restating what changed or where it changed ("X was implemented as Y",
    "X now does Y", "the change added Y to Z").
  - Restating that tests or coverage now exist ("regression coverage was
    added for X", "tests now cover Y").
  - "Indicating" claims that dress up a description of the diff as an
    insight ("X was done as Y, indicating the repo supports Z") when Z is
    just a paraphrase of the change itself, not something a future run
    could not already tell by reading the code.

A clean run whose review surfaced nothing surprising has little or nothing
to teach. In that case `what_was_learned` should be short — often empty. An
empty list is a correct, expected output for a low-friction run. Do not
manufacture claims to avoid returning an empty list.

Order surviving claims strongest-first: the claim most likely to redirect a
future run's behavior goes first. Only the first few claims of a summary are
ever shown to a future run, so a weaker claim must never precede a stronger
one.

Every evidence `description` must explain the causal mechanism: *how* the
cited finding, cycle, file, or diff demonstrates the claim — not merely that
the file was touched or the finding occurred. "This file was modified" is
not evidence of anything; "review flagged this file twice for the same
omission, showing the check X must be applied per-instance, not once" is.

## Output

Emit ONLY this YAML, rooted at `run_summary:`, with no prose before or after and
no code fence. Counts, file lists, domains and costs are filled in by the
coordinator from the audit record — do not restate them.

run_summary:
  run_id: {audit.get("run_id")}
  what_changed:
    description: "One or two sentences: what this run actually changed."
    approach: "How it was done — the shape of the change, not a diff replay."
  what_was_learned:
    - claim: "A reusable lesson a future run could apply — omit this list if there is none."
      evidence:
        - type: review_finding
          finding_id: "<an id from the catalogue above>"
          description: "How this reference demonstrates the claim, not just that it was involved."
  learned_patterns:
    - "short-kebab-tag"
  review_insights:
    - "What the review loop kept surfacing, in one line."
  complexity_signal:
    dominant_difficulty: "The one thing that made this run hard."
"""
