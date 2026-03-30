from textwrap import dedent
from typing import TYPE_CHECKING

from .plan_parser import PlanData
from .story import TaskStory

if TYPE_CHECKING:
    pass


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
          - References to functions or APIs that do not exist
          - Conflicts with the current architecture
          - **Internal contradictions** (e.g., requirements that conflict with
            acceptance criteria, or acceptance criteria that contradict each other)
          - **Ambiguous acceptance criteria** that a dev agent cannot objectively
            verify (e.g., "should be fast" without a measurable threshold)
          - A dependency is missing
          Provide a clear reason so a human can fix the spec.

        **File path references**: If the spec mentions file paths that don't exist
        on disk, do NOT set verdict to BLOCKED for this reason alone. Instead,
        list the missing paths in the `warnings` field and proceed normally.
        The plan agent will discover the correct paths.

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
        warnings:
          - "<missing file path or other non-blocking advisory>"
        criteria_checked:
          - criterion: "<acceptance criterion text>"
            satisfied: true | false
            evidence: "<where in the code this is satisfied, or what is missing>"
        ```

        Use `spec_issues: []` if the spec is clean.
        Use `warnings: []` if there are no non-blocking advisories.

        ## Rules

        - Check EVERY acceptance criterion individually. Do not shortcut.
        - "Related code exists" is NOT the same as "criterion is satisfied."
        - If even ONE criterion is unsatisfied, the verdict cannot be ALREADY_DONE.
        - If the spec has internal contradictions or untestable criteria, verdict is BLOCKED.
        - If the spec contains a **Notes** section, treat it as informal hints that
          may be stale or wrong. Notes are NOT requirements — do not BLOCK a spec
          because a Note references a nonexistent file or outdated pattern. Only
          acceptance criteria and explicit requirements can trigger BLOCKED.
        - BLOCKED is not a failure — it's a save. Fixing a spec costs minutes; a
          doomed plan+dev+review loop costs hours and dollars.
    """)


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

        - **P0** (impossible): Plan cannot be implemented as written. Wrong API,
          hallucinated function, missing caller that would break at runtime.
        - **P1** (must fix): Plan has a real gap that will probably cause dev to
          fail or produce broken code. The plan will be regenerated to address it.
        - **P2** (improvement): Plan could be more precise but dev can work it out.
          Does not block the plan.

        ## Rules

        - verdict MUST be APPROVE if there are zero P0 and zero P1 findings
        - verdict MUST be REJECT if any P0 or P1 finding exists
        - REJECT MUST include at least one P0 or P1 finding
        - **List ALL issues in a single pass.** Multiple findings in one REJECT
          is far better than discovering new issues across multiple cycles.
        - APPROVE with P2 suggestions is valid and encouraged
        - Be specific: cite the plan section, the actual codebase function/file,
          and why it would fail
        - A plan does not need to be perfect — it needs to not be wrong
    """)


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
        - If the spec contains a **Notes** section, treat it as informal hints.
          Notes may reference files, patterns, or gotchas that were relevant when
          the story was written but may be stale or wrong. Verify anything in Notes
          against the actual codebase before relying on it.
        - If something in the spec is ambiguous or contradictory, say so
          explicitly in risks rather than guessing.
    """)
