from pathlib import Path
from textwrap import dedent

from theforge.coordinator.state import CycleHistory

from .conventions import render_conventions_block
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
        ```

        Use `story_deviations: none` if you followed the spec exactly.
        Use `deferred_items: none` if nothing was deferred.
        List ALL commits (use `git log --oneline` for shas).
        List EVERY acceptance criterion from the spec with its status.

        ## Your Task

        1. Open `{handoff_file}` and fix ONLY the `dev_notes` field.
        2. Do NOT change any code. Do NOT re-run the gate.
        3. Write the updated handoff file to disk and stop.
           Do NOT run `git add` or `git commit` for `{handoff_file}`.
    """)


def _build_task_framing(surviving_families: list[dict] | None) -> tuple[str, int]:
    """Build the first numbered task item(s) for the 'Your Task' section.

    Returns (framing_text, next_step_number) so callers can number subsequent
    steps correctly.

    When surviving families are present the framing switches from
    'fix each P1 finding' to 'reconsider approach for persistent issues'.
    """
    if surviving_families:
        n = len(surviving_families)
        family_labels = "; ".join(
            f"`{f.get('seed_anchor', '?')}` ({len(set(f.get('cycles', [])))} cycles)"
            for f in surviving_families
        )
        text = (
            f"1. **Reconsider your approach** for the surviving issue(s) below — "
            f"local patching has not resolved them across {n} cycle(s) "
            f"({family_labels}). "
            "Step back and address the underlying design tension rather than applying "
            "another incremental fix.\n"
            "2. Fix remaining new P1 findings. Address P2 findings if feasible."
        )
        return text, 3
    return "1. Fix each P1 finding. Address P2 findings if feasible.", 2


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
    prior_open_p1s: list | None = None,  # list[FindingRecord]
    classified_p1s: list | None = None,  # list[FindingRecord]
    surviving_families: list[dict] | None = None,
    conventions: list[str] | None = None,
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
        if surviving_families:
            context_sections += dedent("""\

                The following issue(s) have persisted across multiple review cycles.
                You may need to reconsider the approach for these specific areas —
                staying within the plan's approach has not resolved them.
            """)
        else:
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

    _conventions_block = render_conventions_block(conventions)
    if _conventions_block:
        context_sections += _conventions_block

    if surviving_families:
        traj_lines: list[str] = []
        for family in surviving_families:
            seed = family.get("seed_anchor", "")
            cycles = family.get("cycles", [])
            descriptions = family.get("descriptions", [])
            n_cycles = len(set(cycles))
            # Truncate to most recent 5 descriptions if the family is long-lived
            if len(descriptions) > 5:
                descriptions = descriptions[-5:]
            traj_lines.append(f"### Family: `{seed}` ({n_cycles} cycles)")
            for i, desc in enumerate(descriptions):
                traj_lines.append(f"- Cycle appearance {i + 1}: {desc}")
            traj_lines.append("")
        context_sections += dedent("""\

            ## Trajectory Summary

        """) + "\n".join(traj_lines)

    carry_forward_section = ""
    if prior_open_p1s:
        prior_lines = []
        for record in prior_open_p1s:
            location_parts = []
            if record.file is not None:
                location_parts.append(f"file={record.file}")
            if record.line is not None:
                location_parts.append(f"line={record.line}")
            location = ", ".join(location_parts) if location_parts else "location=unknown"
            prior_lines.append(f"- {location}: {record.description}")
        carry_forward_section = dedent(
            f"""\
            ## Still-open Findings from Prior Review

            {chr(10).join(prior_lines)}

            Treat these as unresolved constraints from earlier review cycles. Do not
            regress or ignore them while addressing the current review feedback.

            """
        )

    # Build the P1 findings section with disposition annotations if available
    if classified_p1s:
        p1_lines = []
        for r in classified_p1s:
            loc = f"{r.file}:{r.line}" if r.file else (r.file or "")
            loc_suffix = f" ({loc})" if loc else ""
            p1_lines.append(f"- [{r.disposition}] {r.description}{loc_suffix}")
        p1_section = "\n".join(p1_lines)
        findings_section = dedent(f"""\
            {carry_forward_section}\
            ## P1 Findings (with disposition)

            {p1_section}

            ## Full Review Output

            {review_findings}""")
    else:
        findings_section = dedent(f"""\
            {carry_forward_section}\
            ## Review Findings

            {review_findings}""")

    _task_framing, _next_step = _build_task_framing(surviving_families)
    _fmt_step = _next_step
    _commit_step = _next_step + 1
    _handoff_step = _next_step + 2

    return dedent(f"""\
        You are continuing work on **{task.name}** (iteration {iteration}).

        ## Working Directory

        `{workspace_path}`  (branch: `{branch_name}`)

        You are already in the correct workspace. Do NOT create a new worktree.
        Do NOT switch branches.
        {context_sections}
        {findings_section}

        ## Your Task

        {_task_framing}
        {_fmt_step}. Run `make fmt` to auto-fix formatting.
        {_commit_step}. Commit your changes (do NOT commit `{handoff_file}` — it is gitignored):
           ```bash
           git add <files-you-changed>
           git commit -m "fix(<scope>): address review findings (iter {iteration})"
           ```
        {
        f"{_handoff_step}. **Update `dev_notes` in `"
        + handoff_file
        + "`** to reflect what you changed"
        + '''
           in this iteration. The reviewer reads `dev_notes` before the diff —
           stale notes from a previous iteration will confuse the next review.
           Update the `summary`, `commits`, and `acceptance_criteria` fields.
           Do NOT `git add` this file — it is read from disk, not from git.'''
        if handoff_file
        else ""
    }

        ## Important

        {gate_bullet}- Focus on fixing the identified findings. Do not refactor unrelated code.
        - When a finding describes a **pattern bug** (e.g., a flawed lookup key,
          an unsafe cast, a missing guard), search the entire file — and related
          files — for **all occurrences** of that pattern before committing your
          fix. Do not patch only the line the reviewer cited.
        - **After fixing the finding, audit all code you changed in prior cycles
          for collateral effects.** Each fix can shift execution paths and expose
          new failures in adjacent code you already touched. Re-read your earlier
          commits, check edge cases in those paths, and verify your new tests
          actually exercise the full flow with all external dependencies mocked.
        - Do NOT leave uncommitted changes.
    """)
