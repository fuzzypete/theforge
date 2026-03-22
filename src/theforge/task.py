"""Task definition and prompt builders for agent invocations.

The orchestrator builds prompts mechanically from templates + story content.
No LLM is involved in prompt construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent
from typing import TYPE_CHECKING, TypedDict

import yaml

if TYPE_CHECKING:
    from .coord_state import CycleHistory


# ── Plan TypedDicts ───────────────────────────────────────────────────


class _PlanStepRequired(TypedDict):
    id: int
    description: str
    files: list[str]
    action: str  # "modify" | "create" | "delete"
    details: str


class PlanStep(_PlanStepRequired, total=False):
    """A single step in a structured plan. depends_on is optional."""

    depends_on: list[int]


class _PlanDataRequired(TypedDict):
    approach: str
    steps: list[PlanStep]


class PlanData(_PlanDataRequired, total=False):
    """Structured plan output parsed from YAML."""

    criteria_mapping: list[dict]
    risks: list[dict]


def parse_plan_output(text: str) -> PlanData | None:
    """Parse structured YAML plan output from a plan agent.

    Returns a PlanData dict on success, or None if the text is not valid
    structured plan YAML (e.g. freeform markdown fallback).
    """
    stripped = text.strip()

    # Strip fenced code block if present (```yaml ... ```)
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        # Remove first line (```yaml or ```) and last line (```)
        inner = lines[1:]
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        stripped = "\n".join(inner).strip()

    # Detect YAML plan: starts with 'plan:' or '---' followed by 'plan:'
    if not (stripped.startswith("plan:") or stripped.startswith("---")):
        return None

    # Strip YAML document markers if present
    if stripped.startswith("---"):
        stripped = stripped[3:].strip()

    try:
        data = yaml.safe_load(stripped)
    except yaml.YAMLError:
        return None

    if not isinstance(data, dict) or "plan" not in data:
        return None

    plan = data["plan"]
    if not isinstance(plan, dict):
        return None

    # Validate required top-level keys
    if "approach" not in plan or "steps" not in plan:
        return None

    if not isinstance(plan["steps"], list):
        return None

    # Validate each step has required fields
    for step in plan["steps"]:
        if not isinstance(step, dict):
            return None
        for required_field in ("id", "description", "files", "action", "details"):
            if required_field not in step:
                return None

    result: PlanData = {
        "approach": str(plan["approach"]),
        "steps": plan["steps"],
    }
    if "criteria_mapping" in plan and isinstance(plan["criteria_mapping"], list):
        result["criteria_mapping"] = plan["criteria_mapping"]
    if "risks" in plan and isinstance(plan["risks"], list):
        result["risks"] = plan["risks"]

    return result


@dataclass(frozen=True)
class TaskStory:
    """A single unit of work for the orchestrator to execute."""

    name: str  # human-readable, e.g. "Phase 6H: per-user export"
    story_path: Path  # path to the story file
    slug: str  # workspace slug, e.g. "export-service"
    pytest_target: str | None = None  # specific test target, or None for all
    gate_override: str | None = None  # from frontmatter "gate" key; "none" skips gate
    depends_on: list[str] = field(default_factory=list)  # slugs that must have merged first


# Backward-compat alias
TaskSpec = TaskStory


def load_story(story_path: Path) -> str:
    """Read the story file content. Raises FileNotFoundError if missing."""
    return story_path.read_text(encoding="utf-8")


# Backward-compat alias
load_spec = load_story


def parse_story_frontmatter(story_path: Path) -> dict:
    """Extract YAML frontmatter from a story file.

    Story files can optionally have YAML frontmatter delimited by ---::

        ---
        name: Phase 6H: per-user export
        slug: export-service
        gate: none
        ---

        # Story content starts here...

    If no frontmatter is present, returns empty dict.
    """
    text = story_path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}

    end = text.find("---", 3)
    if end == -1:
        return {}

    frontmatter = text[3:end].strip()
    try:
        result = yaml.safe_load(frontmatter) or {}
    except yaml.YAMLError:
        return {}

    if not isinstance(result, dict):
        return {}

    # R3: gate must be a string if present; drop non-string values to prevent
    # AttributeError when _is_gate_skip() calls .lower() on a non-string.
    if "gate" in result and not isinstance(result["gate"], str):
        result = {k: v for k, v in result.items() if k != "gate"}

    return result


# Backward-compat alias
parse_spec_frontmatter = parse_story_frontmatter


# ── Preflight prompt ─────────────────────────────────────────────────


def build_preflight_prompt(
    task: TaskStory,
    *,
    story_content: str,
) -> str:
    """Build the preflight check prompt.

    The preflight agent receives the spec and determines whether it is already
    implemented, valid and ready for implementation, or blocked/stale.

    This is a one-shot classification call — the agent outputs a structured
    YAML verdict, not code.
    """
    return dedent(f"""\
        You are a preflight validator for **{task.name}**.

        ## Your Role

        You are a cheap gate that stops doomed work before expensive dev+review
        cycles begin. You are NOT implementing anything. You are classifying the
        spec and catching problems that would waste downstream budget.

        ## Spec

        {story_content}

        ## Classification

        Evaluate the spec against the current code and output ONE of these verdicts:

        - **PROCEED** — The spec describes unfinished work. The acceptance criteria
          are clear, non-contradictory, and testable. Implementation should begin.

        - **ALREADY_DONE** — Every acceptance criterion is ALREADY satisfied by
          the current code. You MUST verify each criterion individually.

        - **BLOCKED** — The spec cannot be implemented as written. This includes:
          - References to files, functions, or APIs that do not exist
          - Conflicts with the current architecture
          - **Internal contradictions** (e.g., requirements that conflict with
            acceptance criteria, or acceptance criteria that contradict each other)
          - **Ambiguous acceptance criteria** that a dev agent cannot objectively
            verify (e.g., "should be fast" without a measurable threshold)
          - A dependency is missing
          Provide a clear reason so a human can fix the spec.

        ## Spec Quality Check

        Before classifying, scan the spec for these problems:

        1. **Contradictions**: Do any requirements conflict with acceptance criteria?
           Do any acceptance criteria conflict with each other?
        2. **Ambiguity**: Can every acceptance criterion be objectively verified by
           reading code or running tests? If not, it's ambiguous.
        3. **Impossible constraints**: Does the spec require mutually exclusive
           behaviors (e.g., "never overwrite files" + a fixed filename that runs
           multiple times)?

        If you find any of these, verdict is BLOCKED with a clear explanation of
        the contradiction or ambiguity. It is far cheaper to fix a spec than to
        burn plan+dev+review cycles on a doomed implementation.

        ## Complexity Assessment

        When verdict is PROCEED, also assess the implementation complexity:

        - **small**: Config change, typo fix, single-file edit, <50 lines changed
        - **medium**: New feature, multi-file change, requires tests, 50–500 lines
        - **large**: Cross-cutting refactor, architectural change, >500 lines, many modules

        When verdict is ALREADY_DONE or BLOCKED, set complexity to "small" as a placeholder.

        ## Output Format

        You MUST output ONLY a YAML block. No prose before or after.
        Start your response with ```yaml and end with ```.

        ```yaml
        verdict: PROCEED | ALREADY_DONE | BLOCKED
        complexity: small | medium | large
        reason: "<1-2 sentence explanation of your classification>"
        spec_issues:
          - type: contradiction | ambiguity | impossible_constraint
            description: "<what conflicts or is unclear>"
        criteria_checked:
          - criterion: "<acceptance criterion text>"
            satisfied: true | false
            evidence: "<where in the code this is satisfied, or what is missing>"
        ```

        Use `spec_issues: []` if the spec is clean.

        ## Rules

        - Check EVERY acceptance criterion individually. Do not shortcut.
        - "Related code exists" is NOT the same as "criterion is satisfied."
        - If even ONE criterion is unsatisfied, the verdict cannot be ALREADY_DONE.
        - If the spec has internal contradictions or untestable criteria, verdict is BLOCKED.
        - BLOCKED is not a failure — it's a save. Fixing a spec costs minutes; a
          doomed plan+dev+review loop costs hours and dollars.
    """)


# ── Plan review prompt ────────────────────────────────────────────────


def build_plan_review_prompt(
    task: TaskStory,
    *,
    story_content: str,
    plan_content: str | PlanData,
    mode: str = "cli",
    preflight_output: str | None = None,
    rejection_findings: str | None = None,
) -> str:
    """Build the plan review agent prompt.

    The plan review agent reads the story + generated plan (and optionally
    the preflight output) and produces a structured APPROVE/REJECT verdict.
    """
    preflight_section = ""
    if preflight_output:
        preflight_section = dedent(f"""\

            ## Preflight Analysis

            {preflight_output}
        """)

    rejection_section = ""
    if rejection_findings:
        rejection_section = dedent(f"""\

            ## Previous Rejection Findings

            This plan was previously REJECTED. The following issues were identified.
            Verify whether the regenerated plan addresses them:

            {rejection_findings}
        """)

    # Render plan content: convert PlanData dict to readable text with criteria_mapping
    criteria_mapping_section = ""
    if isinstance(plan_content, dict):
        plan_text_lines = [f"**Approach:** {plan_content.get('approach', '')}"]
        plan_text_lines.append("")
        for step in plan_content.get("steps", []):
            step_id = step.get("id", "?")
            plan_text_lines.append(f"Step {step_id}: {step.get('description', '')}")
            plan_text_lines.append(f"  Action: {step.get('action', '')}")
            if "depends_on" in step and step["depends_on"]:
                deps = ", ".join(f"Step {d}" for d in step["depends_on"])
                plan_text_lines.append(f"  Depends on: {deps}")
            plan_text_lines.append(f"  Files: {', '.join(step.get('files', []))}")
            plan_text_lines.append(f"  Details: {step.get('details', '')}")
            plan_text_lines.append("")
        plan_content_str = "\n".join(plan_text_lines)

        mapping = plan_content.get("criteria_mapping")
        if mapping:
            mapping_lines = ["## Criteria Mapping (from structured plan)", ""]
            for entry in mapping:
                criterion = entry.get("criterion", "")
                steps = entry.get("steps", [])
                steps_str = ", ".join(f"Step {s}" for s in steps)
                mapping_lines.append(f"- **{criterion}** → {steps_str}")
            criteria_mapping_section = "\n" + "\n".join(mapping_lines) + "\n"
    else:
        plan_content_str = plan_content

    output_format_section = dedent("""\
        ## Output Format

        You MUST output ONLY a YAML block. No prose before or after.
        Start your response with ```yaml and end with ```.

        ```yaml
        verdict: APPROVE | REJECT
        criteria_coverage:
          - criterion: "<acceptance criterion text from the spec>"
            covered: true | false
            plan_section: "<which part of the plan addresses this, or 'missing'>"
        findings:
          - severity: P0 | P1 | P2
            description: "<what is wrong with the plan>"
            suggestion: "<how to fix it>"
        ```
    """)
    if mode == "api":
        output_format_section = dedent("""\
            ## Output Format

            You MUST call the `submit_plan_review` tool to deliver your verdict.
            Do NOT return your review as plain text — it will be ignored.
            Use the submit_plan_review tool with your structured review data.
        """)

    return dedent(f"""\
        You are a plan reviewer for **{task.name}**.

        ## Your Role

        You are the last gate before dev budget is spent. Your job is to block
        only plans that would **predictably fail** — wrong APIs, missing callers,
        impossible constraints. Default to APPROVE unless you find a concrete
        blocker. A plan that is "not how I'd do it" but would still produce
        working code is an APPROVE.

        ## Story / Spec

        {story_content}

        ## Generated Plan

        {plan_content_str}
        {criteria_mapping_section}{preflight_section}{rejection_section}
        ## Evaluation Process

        1. **Acceptance criteria coverage** — walk through each AC in the spec
           and verify the plan addresses it. Report the result in `criteria_coverage`.
        2. **Blast radius check** — does the plan modify or change return types of
           functions that have callers outside the listed files? If so, are those
           callers accounted for?
        3. **Feasibility** — are the proposed APIs, function signatures, and module
           paths real? Use your tools to verify against the actual codebase.

        Do NOT evaluate: code style, plan verbosity, alternative approaches,
        or hypothetical edge cases that the dev agent can handle at implementation time.
        {output_format_section}
        ## Severity Definitions

        - **P0** (blocker): Plan is impossible to implement as written. Wrong API,
          hallucinated function, missing caller that would break at runtime.
          REJECT required.
        - **P1** (likely failure): Plan has a gap that will probably cause dev to
          fail or produce broken code. Log as advisory finding; coordinator
          downgrades to APPROVE and passes findings to the dev agent.
        - **P2** (suggestion): Plan could be improved but dev can figure it out.
          Does NOT trigger REJECT.

        ## Rules

        - verdict MUST be APPROVE if there are zero P0 findings
        - verdict MUST be REJECT if any P0 finding exists
        - verdict SHOULD be REJECT if any P1 finding exists (coordinator will
          downgrade to APPROVE and pass P1 findings as advisory notes to dev)
        - REJECT MUST include at least one P0 or P1 finding
        - **List ALL issues in a single pass.** Multiple findings in one REJECT
          is far better than discovering new issues across multiple cycles.
        - APPROVE with P2 suggestions is valid and encouraged
        - Be specific: cite the plan section, the actual codebase function/file,
          and why it would fail
        - A plan does not need to be perfect — it needs to not be wrong
    """)


# ── Plan prompt ───────────────────────────────────────────────────────


def build_plan_prompt(
    task: TaskStory,
    *,
    story_content: str,
    preflight_output: str | None = None,
) -> str:
    """Build the planning agent prompt.

    The planning agent reads the story and produces a structured YAML plan.
    It does NOT write code.

    Output is ONLY the plan document in YAML format.
    """
    preflight_section = ""
    if preflight_output:
        preflight_section = dedent(f"""\

            ## Preflight Analysis

            The preflight agent already analysed the codebase:

            {preflight_output}
        """)

    return dedent(f"""\
        You are a planning agent for **{task.name}**.

        ## Your Role

        You are NOT implementing anything. You are deciding the smallest viable
        path to satisfy every acceptance criterion in the spec. Your plan will
        be reviewed and then handed to a dev agent. Keep it short and decisive.

        ## Spec

        {story_content}
        {preflight_section}
        ## Output Format

        You MUST output ONLY a YAML block. No prose before or after.
        Start your response with `plan:` and produce valid YAML.

        The plan MUST follow this exact schema:

        ```yaml
        plan:
          approach: "<1-2 sentence summary of the implementation strategy>"
          steps:
            - id: 1
              description: "<what this step does>"
              files:
                - src/theforge/example.py
              action: modify  # modify | create | delete
              details: "<concrete implementation details for this step>"
            - id: 2
              description: "<what this step does>"
              files:
                - src/theforge/other.py
              action: create
              details: "<concrete implementation details for this step>"
              depends_on: [1]
          criteria_mapping:
            - criterion: "<acceptance criterion text from the spec>"
              steps: [1]
            - criterion: "<another acceptance criterion>"
              steps: [2]
          risks:
            - description: "<what is risky or ambiguous>"
              mitigation: "<how to address it>"
        ```

        ## Rules

        - Do NOT write code. Do NOT modify files.
        - Do NOT invent function signatures — cite what exists in the codebase.
        - Do NOT pad the plan with edge case tables or test scenario details
          that the dev agent will derive from the code. Keep it lean.
        - Cover ALL acceptance criteria from the spec in criteria_mapping.
        - Every step MUST include: id, description, files, action, details.
        - depends_on is optional — only include it when a step truly depends
          on another step completing first.
        - If something in the spec is ambiguous or contradictory, say so
          explicitly in risks rather than guessing.
    """)


# ── Dev prompt ────────────────────────────────────────────────────────


def build_dev_prompt(
    task: TaskStory,
    *,
    workspace_path: Path,
    branch_name: str,
    story_content: str,
    gate_command: str,
    gate_skipped: bool = False,
    review_findings: str | None = None,
    human_feedback: str | None = None,
    preflight_output: str | None = None,
    plan_output: str | PlanData | None = None,
    plan_review_advisory: str | None = None,
    iteration: int = 1,
    escalation_note: str | None = None,
    cycle_history: list[CycleHistory] | None = None,
    handoff_file: str = "handoff.yaml",
) -> str:
    """Build the complete dev agent prompt.

    The prompt tells the agent:
    - It is already in the correct workspace (orchestrator created it)
    - What to implement (full spec injected)
    - What files it can modify (scope restriction)
    - How to validate (fmt, lint, gate)
    - What NOT to do (merge, update plan)
    - Any review findings from previous iteration

    The orchestrator fills ALL placeholders. The agent makes zero process decisions.
    """
    feedback_section = ""
    if escalation_note:
        feedback_section += dedent(f"""\

            ## ⚠ Model Escalation

            {escalation_note}
        """)

    if cycle_history:
        history_lines = []
        for h in cycle_history:
            history_lines.append(f"### Cycle {h.cycle}: {h.verdict}")
            history_lines.append(h.summary)
            if h.p1_findings:
                history_lines.append("P1 findings:")
                for desc in h.p1_findings:
                    history_lines.append(f"- {desc}")
            history_lines.append("")
        feedback_section += dedent("""\

            ## Previous Review Cycles

        """) + "\n".join(history_lines)

    if review_findings:
        feedback_section += dedent(f"""\

            ## CRITICAL: Review Findings from Previous Iteration

            The following findings were identified by the code reviewer. You MUST address
            ALL P1 findings before considering your work complete. P2 findings should be
            addressed if feasible.

            {review_findings}

            This is iteration {iteration}. Focus specifically on fixing the identified issues.
        """)

    if human_feedback:
        feedback_section += dedent(f"""\

            ## CRITICAL: Human Feedback

            The project owner provided the following feedback. Address all points:

            {human_feedback}
        """)

    plan_section = ""
    if plan_output:
        if isinstance(plan_output, dict):
            # Structured plan: render as step-by-step checklist
            plan_lines = [
                "## Implementation Plan (from planning agent)",
                "",
                "The planning agent has already analysed this codebase and produced a",
                "detailed implementation plan. Follow it closely — do not re-derive the",
                "approach from scratch.",
                "",
                f"**Approach:** {plan_output.get('approach', '')}",
                "",
            ]
            for step in plan_output.get("steps", []):
                step_id = step.get("id", "?")
                plan_lines.append(f"Step {step_id}: {step.get('description', '')}")
                plan_lines.append(f"  Action: {step.get('action', '')}")
                if "depends_on" in step and step["depends_on"]:
                    deps = ", ".join(f"Step {d}" for d in step["depends_on"])
                    plan_lines.append(f"  Depends on: {deps}")
                plan_lines.append(f"  Details: {step.get('details', '')}")
                plan_lines.append("")
            plan_section = "\n" + "\n".join(plan_lines) + "\n"
        else:
            plan_section = dedent(f"""\

                ## Implementation Plan (from planning agent)

                The planning agent has already analysed this codebase and produced a
                detailed implementation plan. Follow it closely — do not re-derive the
                approach from scratch.

                {plan_output}
            """)
        if plan_review_advisory:
            plan_section += dedent(f"""\

                ## Plan Review Notes (advisory)

                The plan reviewer flagged the following. These are not blockers — the plan
                was approved — but watch for these edge cases during implementation:

                {plan_review_advisory}
            """)

    preflight_section = ""
    if preflight_output:
        preflight_section = dedent(f"""\

            ## Codebase Context (from preflight)

            The preflight agent already analysed the codebase. Use this to orient
            yourself — do NOT re-read files that are already summarised here unless
            you need the exact content for editing.

            {preflight_output}
        """)

    if gate_skipped:
        gate_section = dedent("""\
            Gate is disabled for this spec. Skip the gate command.
        """)
    else:
        gate_section = dedent(f"""\
            Run the gate command to validate your work:
            ```bash
            {gate_command}
            ```
            Fix any failures. Do NOT declare success until the gate passes.
        """)

    return dedent(f"""\
        You are implementing **{task.name}**.

        ## Working Directory

        `{workspace_path}` — branch `{branch_name}`

        You are already in the correct workspace. Do NOT create a new worktree
        or switch branches.
        {plan_section}
        ## Spec

        > **How to read this spec**
        > A story says WHAT and WHY — it is not a list of implementation tasks.
        > **Acceptance criteria are the definitive checklist.** Every other section
        > (background, context, motivation) is informational — do not treat it as a
        > requirement unless it appears in an AC.
        > If an AC is ambiguous or contradicts another section, implement the
        > most reasonable interpretation and flag the ambiguity in `dev_notes`.

        {story_content}
        {feedback_section}{preflight_section}
        ## Workflow

        1. Implement the spec. Write tests for new functionality.
        2. Run `make fmt` then `make lint`. Fix any failures.
        3. {gate_section}
        4. Commit your changes:
           ```bash
           git add <files-you-changed>
           git commit -m "<type>(<scope>): <description>"
           ```
        {
        "5. Write a `dev_notes` section in `"
        + handoff_file
        + "` with this structure:"
        + '''

           ```yaml
           dev_notes: |
             summary: "One paragraph: what you implemented and how."
             commits:
               - sha: "abc1234"
                 message: "feat(scope): what this commit does"
             acceptance_criteria:
               - criterion: "AC text from the spec"
                 status: MET | PARTIAL | NOT_MET
                 notes: "how it was met, or why not"
             story_deviations: none  # or list deviations with justification
             deferred_items: none   # or list with reason
             gate_result: PASS
           ```

           List ALL commits (`git log --oneline`). List EVERY acceptance criterion.
           This is your voice in the review — the reviewer reads it before the diff.'''
        if handoff_file
        else ""
    }

        ## Rules

        - Do NOT merge to main.
        - Do NOT leave uncommitted changes.
        - If you cannot finish, commit what you have and list blockers in
          `deferred_items`.
    """)


# ── Handoff fix prompt ────────────────────────────────────────────────


def build_handoff_fix_prompt(
    task: TaskStory,
    *,
    workspace_path: Path,
    branch_name: str,
    validation_errors: list[str],
) -> str:
    """Build a focused prompt to fix dev handoff formatting.

    Used when the gate passed but the dev_notes field in handoff.yaml
    doesn't conform to the required YAML schema. The agent only needs
    to rewrite dev_notes — not re-implement anything.
    """
    error_list = "\n".join(f"- {e}" for e in validation_errors)

    return dedent(f"""\
        You are fixing the dev handoff for **{task.name}**.

        ## Working Directory

        `{workspace_path}`  (branch: `{branch_name}`)

        You are already in the correct workspace. Do NOT create a new worktree.

        ## Problem

        Your implementation passed the gate, but the `dev_notes` field in
        `handoff.yaml` does not conform to the required structure.

        **Validation errors:**

        {error_list}

        ## Required Format

        The `dev_notes` field in `handoff.yaml` must contain valid YAML with
        this exact structure:

        ```yaml
        dev_notes: |
          summary: "One paragraph: what you implemented and how."
          commits:
            - sha: "abc1234"
              message: "feat(scope): what this commit does"
          acceptance_criteria:
            - criterion: "AC text from the spec"
              status: MET | PARTIAL | NOT_MET
              notes: "how it was met, or why not"
          story_deviations:
            - description: "What deviated from spec"
              justification: "Why you deviated"
          deferred_items:
            - description: "What was deferred"
              reason: "Why it was deferred"
          gate_result: PASS
        ```

        Use `story_deviations: none` if you followed the spec exactly.
        Use `deferred_items: none` if nothing was deferred.
        List ALL commits (use `git log --oneline` for shas).
        List EVERY acceptance criterion from the spec with its status.

        ## Your Task

        1. Open `handoff.yaml` and fix ONLY the `dev_notes` field.
        2. Do NOT change any code. Do NOT re-run the gate.
        3. Commit the fix:
           ```bash
           git add handoff.yaml
           git commit -m "fix({task.slug}): rewrite dev handoff to match schema"
           ```
    """)


# ── Fix prompt (iteration 2+) ─────────────────────────────────────────


def build_fix_prompt(
    task: TaskStory,
    *,
    workspace_path: Path,
    branch_name: str,
    review_findings: str,
    gate_command: str,
    gate_skipped: bool = False,
    iteration: int = 2,
    cycle_history: list[CycleHistory] | None = None,
    escalation_note: str | None = None,
    handoff_file: str = "handoff.yaml",
    plan_output: str | dict | None = None,
    classified_p1s: list | None = None,  # list[FindingRecord]
) -> str:
    """Build a minimal fix prompt for review iteration 2+.

    The agent already has full context from iteration 1's resumed session.
    This prompt contains ONLY what changes: the specific P1 findings to fix.

    The coordinator runs the gate after the agent completes — the agent
    should NOT re-run the gate (saves 5-8 minutes per iteration).
    """
    gate_bullet = (
        ""
        if gate_skipped
        else (
            f"- Do NOT re-run the gate (`{gate_command}`). "
            "The coordinator runs it automatically after you complete.\n        "
        )
    )

    context_sections = ""
    if escalation_note:
        context_sections += dedent(f"""\

            ## ⚠ Model Escalation

            {escalation_note}
        """)
    if plan_output:
        if isinstance(plan_output, dict):
            plan_lines = [
                "## Approved Plan",
                "",
                f"**Approach:** {plan_output.get('approach', '')}",
                "",
            ]
            for step in plan_output.get("steps", []):
                step_id = step.get("id", "?")
                plan_lines.append(f"Step {step_id}: {step.get('description', '')}")
                plan_lines.append(f"  Action: {step.get('action', '')}")
                if "depends_on" in step and step["depends_on"]:
                    deps = ", ".join(f"Step {d}" for d in step["depends_on"])
                    plan_lines.append(f"  Depends on: {deps}")
                plan_lines.append(f"  Details: {step.get('details', '')}")
                plan_lines.append("")
            context_sections += "\n" + "\n".join(plan_lines) + "\n"
        else:
            context_sections += dedent(f"""\

                ## Approved Plan

                {plan_output}
            """)
        context_sections += dedent("""\

            Fix the P1 findings **within this plan's approach**.
            Do NOT redesign or adopt a different strategy.
        """)
    if cycle_history:
        history_lines = []
        for h in cycle_history:
            history_lines.append(f"### Cycle {h.cycle}: {h.verdict}")
            history_lines.append(h.summary)
            if h.p1_findings:
                history_lines.append("P1 findings:")
                for desc in h.p1_findings:
                    history_lines.append(f"- {desc}")
            history_lines.append("")
        context_sections += dedent("""\

            ## Previous Review Cycles

        """) + "\n".join(history_lines)

    # Build the P1 findings section with disposition annotations if available
    if classified_p1s:
        p1_lines = []
        for r in classified_p1s:
            loc = f"{r.file}:{r.line}" if r.file else (r.file or "")
            loc_suffix = f" ({loc})" if loc else ""
            p1_lines.append(f"- [{r.disposition}] {r.description}{loc_suffix}")
        p1_section = "\n".join(p1_lines)
        findings_section = dedent(f"""\
            ## P1 Findings (with disposition)

            {p1_section}

            ## Full Review Output

            {review_findings}""")
    else:
        findings_section = dedent(f"""\
            ## Review Findings

            {review_findings}""")

    return dedent(f"""\
        You are continuing work on **{task.name}** (iteration {iteration}).

        ## Working Directory

        `{workspace_path}`  (branch: `{branch_name}`)

        You are already in the correct workspace. Do NOT create a new worktree.
        Do NOT switch branches.
        {context_sections}
        {findings_section}

        ## Your Task

        1. Fix each P1 finding. Address P2 findings if feasible.
        2. Run `make fmt` to auto-fix formatting.
        3. Commit your changes:
           ```bash
           git add <files-you-changed>
           git commit -m "fix(<scope>): address review findings (iter {iteration})"
           ```
        {
        "4. **Update `dev_notes` in `"
        + handoff_file
        + "`** to reflect what you changed"
        + '''
           in this iteration. The reviewer reads `dev_notes` before the diff —
           stale notes from a previous iteration will confuse the next review.
           Update the `summary`, `commits`, and `acceptance_criteria` fields.'''
        if handoff_file
        else ""
    }

        ## Important

        {gate_bullet}- Focus on fixing the identified findings. Do not refactor unrelated code.
        - Do NOT leave uncommitted changes.
    """)


# ── Synthesis prompt ──────────────────────────────────────────────────


def build_synthesis_prompt(
    task: TaskStory,
    review_outputs: list[str],
    review_names: list[str],
    story_content: str,
    *,
    failed_count: int = 0,
    total_count: int | None = None,
) -> str:
    """Build the synthesis agent prompt.

    The synthesis agent reads N independent reviews of the same diff and
    produces a single reconciled ReviewResult. Attribution uses profile
    name (not model) to handle cases where multiple profiles share a model.
    """
    # Build delimited review sections
    review_sections = []
    for name, output in zip(review_names, review_outputs):
        review_sections.append(
            f'## Review from "{name}"\n'
            f"--- BEGIN REVIEW OUTPUT ---\n"
            f"{output}\n"
            f"--- END REVIEW OUTPUT ---"
        )
    reviews_block = "\n\n".join(review_sections)

    degraded_note = ""
    if failed_count > 0 and total_count is not None:
        degraded_note = (
            f"\n**Note:** {failed_count} of {total_count} reviewers failed and "
            f"their outputs are excluded from this synthesis.\n"
        )

    return dedent(f"""\
        You are synthesizing {len(review_outputs)} independent code reviews of **{task.name}**.

        ## Your Role

        You have received {len(review_outputs)} independent reviews of the same code diff.
        The reviewers worked blind — they did not see each other's output.
        Your job is to reconcile these reviews into a single authoritative verdict.
        {degraded_note}
        ## Spec

        {story_content}

        ## Independent Reviews

        {reviews_block}

        ## Synthesis Instructions

        1. **Agreements** — findings reported by multiple reviewers carry high confidence.
        2. **Disagreements** — divergence between reviewers should be noted in your summary.
        3. **Unique contributions** — a finding from only one reviewer is still valid if
           well-reasoned.
        4. **P1 rule** — if ANY reviewer identified a reproducible P1 finding, your verdict
           MUST be REQUEST_CHANGES regardless of other reviewers' verdicts.
        5. Produce a reconciled set of findings. Do not duplicate the same finding multiple times.

        ## Output Format

        You MUST output ONLY a YAML block. No prose before or after.
        Start your response with ```yaml and end with ```.

        ```yaml
        verdict: APPROVE | REQUEST_CHANGES
        summary: "<one-line summary — note divergence if reviewers disagreed significantly>"
        findings:
          - severity: P1 | P2
            file: "<file path>"
            line: <line number or null>
            description: "<what is wrong>"
            suggestion: "<how to fix it>"
        story_compliance:
          matches_spec: true | false
          mismatches:
            - "<description of mismatch>"
        test_coverage:
          adequate: true | false
          gaps:
            - "<description of missing test>"
        ```

        ## Rules

        - verdict MUST be `APPROVE` if there are zero P1 findings
        - verdict MUST be `REQUEST_CHANGES` if any P1 finding exists
        - Be concrete: cite file + line + what is wrong + how to fix
        - Do NOT invent issues. Only report findings with evidence from the reviews.
    """)


# ── Review prompt ─────────────────────────────────────────────────────

_REVIEW_ROLE_SECTIONS: dict[str, str] = {
    "correctness": dedent("""\
        You are a code reviewer focused on **correctness**. Your lens:
        - Does the implementation match the spec's acceptance criteria?
        - Are there logic bugs that would fail at runtime (wrong conditions, bad state)?
        - Data integrity risks (corruption, lost writes)?

        You are NOT implementing anything. Do NOT write code."""),
    "patterns": dedent("""\
        You are a code reviewer focused on **patterns and design**. Your lens:
        - Error handling completeness (unhandled exceptions, silent failures)
        - Test coverage: are the important paths tested?
        - API boundaries: does the change leak internals or break callers?

        You are NOT implementing anything. Do NOT write code."""),
    "edge-cases": dedent("""\
        You are a code reviewer focused on **edge cases and failure modes**. Your lens:
        - Boundary conditions (empty inputs, max values, zero, None)
        - State that survives when it shouldn't (cleanup, reset, teardown)
        - Failure under unexpected input or timing (partial writes, timeouts)

        You are NOT implementing anything. Do NOT write code."""),
}

_REVIEW_ROLE_GENERIC = dedent("""\
    You are a code reviewer. Your job is to determine whether this change is
    safe to merge. You are NOT implementing anything. Do NOT write code.""")


def build_review_prompt(
    task: TaskStory,
    *,
    story_content: str,
    commit_log: str,
    workspace_path: str,
    branch: str,
    handoff_content: str,
    mode: str = "cli",
    review_role: str | None = None,
    dev_notes: str | None = None,
    cycle_history: list[CycleHistory] | None = None,
) -> str:
    """Build the review agent prompt.

    The reviewer receives:
    - The commit log (git log main..HEAD) as the primary handoff artifact
    - The spec (to verify compliance)
    - The handoff.yaml (to cross-check validation claims)
    - Instructions to use Read/Bash/Glob/Grep tools to inspect actual source

    This mirrors a PR review workflow: reviewers discover files from commits,
    not from a pre-enumerated file list.

    When review_role is set to a known role ("correctness", "patterns",
    "edge-cases"), the "Your Role" section uses a role-specific lens.
    Unknown or None values fall back to the generic prompt.

    The reviewer outputs ONLY a YAML block. No prose.
    """
    role_section = _REVIEW_ROLE_SECTIONS.get(review_role or "", _REVIEW_ROLE_GENERIC)

    # Cycle 2+: build tri-part framing section from prior cycle history.
    # Cycle 1 (empty/None): no framing — full independent review.
    cycle_framing_section = ""
    if cycle_history:
        # Collect all P1 findings from prior cycles for the "Verify fixes" section.
        prior_p1_lines: list[str] = []
        for ch in cycle_history:
            for desc in ch.p1_findings:
                prior_p1_lines.append(f"  - [Cycle {ch.cycle}] {desc}")
        prior_p1_block = "\n".join(prior_p1_lines) if prior_p1_lines else "  (none recorded)"
        cycle_framing_section = dedent(f"""\

            ## Cycle-Aware Review Framing

            This is review cycle {len(cycle_history) + 1}. Prior cycles raised the findings below.
            Structure your review in three parts:

            ### Part 1 — Verify Fixes
            Confirm that each prior P1 finding listed below is now resolved. For each:
            - If fixed: note it briefly (no need to re-report).
            - If still present: report it again as a P1 with its original description.

            Prior P1 findings:
            {prior_p1_block}

            ### Part 2 — Scan Regressions
            Examine the files touched in the latest dev iteration (check `git diff` or
            the commit log). Flag as P1 any new defect that was introduced by the fix
            (i.e., the code was correct before and is now broken). These are regressions —
            report them even if they are unrelated to the original spec.

            ### Part 3 — Additional Findings
            You may report new P1 findings ONLY if they are:
            (a) a direct regression from the fix (covered in Part 2), OR
            (b) a critical issue that is independently, concretely evidenced
                (file + line + what breaks).
            Do NOT escalate speculative or style concerns to P1 in this section.

        """)

    dev_notes_section = (
        dedent(f"""\

            ## Developer Notes

            {dev_notes}

            Read this before examining the diff. The developer has flagged intentional
            decisions and spec deviations here. If a deviation is justified, do NOT
            flag it as a spec violation — flag only unjustified or incorrect deviations.
        """)
        if dev_notes
        else ""
    )

    output_format_section = dedent("""\
        ## Output Format

        You MUST output ONLY a YAML block. No prose before or after.
        Start your response with ```yaml and end with ```.

        ```yaml
        verdict: APPROVE | REQUEST_CHANGES
        summary: "<one-line summary of your review>"
        findings:
          - severity: P1 | P2
            file: "<file path>"
            line: <line number or null>
            description: "<what is wrong>"
            suggestion: "<how to fix it>"
        story_compliance:
          matches_spec: true | false
          mismatches:
            - "<description of mismatch>"
        test_coverage:
          adequate: true | false
          gaps:
            - "<description of missing test>"
        ```
    """)
    if mode == "api":
        output_format_section = dedent("""\
            ## Output Format

            You MUST call the `submit_review` tool to deliver your verdict.
            Do NOT return your review as plain text — it will be ignored.
            Use the submit_review tool with your structured review data.
        """)

    return dedent(f"""\
        You are reviewing an implementation of **{task.name}**.

        ## Your Role

        {role_section}
        {cycle_framing_section}
        ## Spec

        {story_content}
        {dev_notes_section}
        ## Commits

        The following commits implement the spec on branch `{branch}`.
        Use `git show <sha>` or Read/Bash/Glob/Grep tools to inspect the source
        in the worktree at: {workspace_path}

        ```
        {commit_log}
        ```

        ## Handoff from Dev Agent

        ```yaml
        {handoff_content}
        ```

        {output_format_section}

        ## Severity Definitions

        - **P1** (blocking): A concrete, demonstrable problem that would break
          the code at runtime, violate a specific acceptance criterion, corrupt
          data, or leave a critical path untested. You MUST be able to point to
          the exact file, line, and what would go wrong. Spec violations are P1
          only if the code actually fails to satisfy the criterion — not if the
          approach differs from what you'd prefer.
        - **P2** (non-blocking): Style, minor improvement, non-critical missing
          test, suggestion for future work. Does NOT block merge.

        ## Rules

        - **Default to APPROVE.** If the code satisfies every acceptance criterion
          and the gate passes, it should be approved even if you'd do it differently.
        - verdict MUST be `APPROVE` if there are zero P1 findings
        - verdict MUST be `REQUEST_CHANGES` if any P1 finding exists
        - A P1 must cite a concrete failure: file + line + what breaks. "Could be
          improved" or "might cause issues" is P2, not P1.
        - Do NOT invent issues. Only report problems you can find in the source.
        - Do NOT flag the same issue that was already flagged and fixed in a
          previous review cycle.
        - This review may be merged with other reviewers' outputs. One speculative
          P1 from you blocks the entire pipeline. Be precise.
        - **Spec-to-runtime traceability**: For each AC the developer claims as
          MET, verify BOTH layers: (a) the logic exists in the codebase, AND
          (b) it is actually invoked at runtime by the coordinator/runner/CLI.
          Code that produces correct output but is never called does NOT satisfy
          the AC. A selector that returns a value the caller ignores is a P1.
        - **YAML safety**: In `description` and `suggestion` fields, do NOT use
          backslashes or double-quote characters inside double-quoted strings —
          they break YAML parsing. Use single quotes or paraphrase instead.
          Bad:  description: "Regex changed from r\\"..."
          Good: description: "Regex changed from raw string pattern ..."
    """)
