from pathlib import Path
from textwrap import dedent

from theforge.coord_state import CycleHistory

from theforge.coord_state import CycleHistory, FindingRecord
from .story import TaskStory


def build_handoff_fix_prompt(
    task: TaskStory,
    *,
    workspace_path: Path,
    branch_name: str,
    validation_errors: list[str],
    handoff_file: str = "handoff.yaml",
) -> str:
    """Build a focused prompt to fix dev handoff formatting.

    Used when the gate passed but the dev_notes field in the configured handoff
    file doesn't conform to the required YAML schema. The agent only needs
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
        `{handoff_file}` does not conform to the required structure.

        **Validation errors:**

        {error_list}

        ## Required Format

        The `dev_notes` field in `{handoff_file}` must contain valid YAML with
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

        1. Open `{handoff_file}` and fix ONLY the `dev_notes` field.
        2. Do NOT change any code. Do NOT re-run the gate.
        3. Commit the fix:
           ```bash
           git add {handoff_file}
           git commit -m "fix({task.slug}): rewrite dev handoff to match schema"
           ```
    """)


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
