from pathlib import Path
from textwrap import dedent

from theforge.coordinator.state import CycleHistory

from .context_assembler import ContextPack
from .conventions import render_conventions_block
from .plan_parser import PlanData
from .story import TaskStory


def build_dev_prompt(
    task: TaskStory,
    *,
    workspace_path: Path,
    branch_name: str,
    story_content: str,
    gate_command: str,
    test_command: str | None = None,
    gate_skipped: bool = False,
    review_findings: str | None = None,
    human_feedback: str | None = None,
    preflight_output: str | None = None,
    plan_output: str | PlanData | None = None,
    plan_review_advisory: str | None = None,
    iteration: int = 1,
    escalation_note: str | None = None,
    cycle_history: list[CycleHistory] | None = None,
    preflight_sufficiency: str | None = None,
    contract_change: bool = False,
    conventions: list[str] | None = None,
    assembled_context: ContextPack | None = None,
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

    _relaxed = preflight_sufficiency == "implementation_ready"
    _obedience_text = (
        "Use the plan as a guide — adapt freely if you discover the approach needs adjustment."
        if _relaxed
        else "Follow it closely — do not re-derive the approach from scratch."
    )

    plan_section = ""
    if plan_output:
        if isinstance(plan_output, dict):
            # Structured plan: render as step-by-step checklist
            plan_lines = [
                "## Implementation Plan (from planning agent)",
                "",
                "The planning agent has already analysed this codebase and produced a",
                f"detailed implementation plan. {_obedience_text}",
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
                detailed implementation plan. {_obedience_text}

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

    context_section = ""
    if assembled_context and assembled_context.content:
        context_section = dedent(f"""\

            ## Repository Context Pack

            Use this curated repository context before exploring additional files.

            {assembled_context.content}
        """)

    if test_command and test_command != gate_command:
        test_section = dedent(f"""\

            ## Testing During Development

            When running tests during development, use exactly:
            ```bash
            {test_command}
            ```
            Do not hand-roll pytest invocations — always use this command.
        """)
    else:
        test_section = ""

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
            Full gate completion is a hard prerequisite for done. Fix any failures.
            Do NOT declare success, write a completed handoff, or treat the task as
            finished until the gate passes.
        """)

    if contract_change:
        test_rule = (
            "- This story intentionally changes an existing behavioral contract.\n"
            "  You MAY update test files that assert the old contract behavior.\n"
            "  Do NOT modify tests unrelated to the contract change."
        )
    else:
        test_rule = (
            "- Do NOT modify existing test files that are not directly related to\n"
            "  your story. If existing tests fail after your changes, your\n"
            "  implementation is wrong — fix your code, not the tests."
        )

    provider_sdk_test_rule = (
        "- Tests must not rely on optional provider SDKs (e.g. `openai`, `anthropic`,\n"
        "  `google-generativeai`) being installed. If code under test performs a\n"
        "  provider SDK import check, mock or stub that boundary so the test passes\n"
        "  whether the environment has `.[dev]` or `.[all,dev]` installed. Only\n"
        "  stories explicitly about real provider integration may skip this isolation\n"
        "  requirement."
    )

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
        > If the spec contains a **Notes** section, treat it as informal hints from
        > whoever wrote the story. Notes may reference files, patterns, or gotchas
        > that were relevant at writing time but may be stale or wrong. Verify
        > anything in Notes against the actual codebase before relying on it.
        > If an AC is ambiguous or contradicts another section, implement the
        > most reasonable interpretation and flag the ambiguity in `dev_notes`.

        {story_content}
        {feedback_section}{preflight_section}{context_section}{test_section}{
        render_conventions_block(conventions)
    }
        ## Workflow

        1. Implement the spec. Write tests for new functionality.
        2. Run `make fmt` then `make lint`. Fix any failures.
        3. {gate_section}
        4. Only after the gate passes, commit your changes:
           ```bash
           git add <files-you-changed>
           git commit -m "<type>(<scope>): <description>"
           ```
        5. Emit a `<forge_handoff>` block in your **final message** (outside any code
           fence, exactly once). This allows the orchestrator to capture your handoff
           without reading the filesystem. The block must contain a YAML mapping with
           the same keys as the `dev_notes` section above:

           ```
           <forge_handoff>
           summary: "One paragraph: what you implemented and how."
           commits:
             - sha: "abc1234"
               message: "feat(scope): what this commit does"
           acceptance_criteria:
             - criterion: "AC text from the spec"
               status: MET | PARTIAL | NOT_MET
               notes: "how it was met, or why not"
           story_deviations: none
           deferred_items: none
           </forge_handoff>
           ```

           Rules for the block:
           - Appear **once**, at the end of your message, outside any ``` fences.
           - Content must be valid YAML (no embedded code fences inside the block).
           - `commits` and `acceptance_criteria` must be lists (even if empty: `[]`).
           - `story_deviations` and `deferred_items` may be the word `none` or a list.

        ## Rules

        - Do NOT merge to main.
        - Do NOT leave uncommitted changes.
        - Do not create files outside `src/`, `tests/`, or `docs/`. If you need a
          scratch file for exploration, delete it before committing.
        {test_rule}
        {provider_sdk_test_rule}
        - If you cannot finish, commit what you have and list blockers in
          `deferred_items`.
    """)
