"""Task definition and prompt builders for agent invocations.

The orchestrator builds prompts mechanically from templates + spec content.
No LLM is involved in prompt construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from .coord_state import CycleHistory


@dataclass(frozen=True)
class TaskSpec:
    """A single unit of work for the orchestrator to execute."""

    name: str  # human-readable, e.g. "Phase 6H: per-user export"
    spec_path: Path  # path to the spec file
    slug: str  # workspace slug, e.g. "export-service"
    file_scope: list[str]  # paths the agent may modify
    pytest_target: str | None = None  # specific test target, or None for all
    gate_override: str | None = None  # from frontmatter "gate" key; "none" skips gate
    depends_on: list[str] = field(default_factory=list)  # slugs that must have merged first


def load_spec(spec_path: Path) -> str:
    """Read the spec file content. Raises FileNotFoundError if missing."""
    return spec_path.read_text(encoding="utf-8")


def parse_spec_frontmatter(spec_path: Path) -> dict:
    """Extract YAML frontmatter from a spec file.

    Spec files can optionally have YAML frontmatter delimited by ---::

        ---
        name: Phase 6H: per-user export
        slug: export-service
        gate: none
        file_scope:
          - src/export/
        ---

        # Spec content starts here...

    If no frontmatter is present, returns empty dict.
    """
    text = spec_path.read_text(encoding="utf-8")
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


# ── Preflight prompt ─────────────────────────────────────────────────


def build_preflight_prompt(
    task: TaskSpec,
    *,
    spec_content: str,
    file_contents: dict[str, str],
) -> str:
    """Build the preflight check prompt.

    The preflight agent receives the spec and current file contents for the
    file_scope. It determines whether the spec is already implemented, valid
    and ready for implementation, or blocked/stale.

    This is a one-shot classification call — the agent outputs a structured
    YAML verdict, not code.
    """
    if file_contents:
        files_block = "\n\n".join(
            f"### `{path}`\n```\n{content}\n```" for path, content in file_contents.items()
        )
    else:
        files_block = "(no file_scope defined — spec applies to entire project)"

    if task.file_scope:
        scope_feasibility_section = dedent("""\
            ## Scope Feasibility Check

            If file_scope is non-empty, scan the spec body for files explicitly named
            as requiring modification (look for file paths, function signatures tied to
            specific files, "in X.py change Y", acceptance criteria referencing specific
            files). For each such file, note whether it appears in the file_scope list below.
            If required files are absent from file_scope, include a warning in your reason
            but still return PROCEED — the dev agent will receive guidance about the scope
            mismatch and can work around it.

            This check is advisory when file_scope is non-empty.
        """)
    else:
        scope_feasibility_section = ""

    return dedent(f"""\
        You are a preflight validator for **{task.name}**.

        ## Your Role

        You are a cheap gate that stops doomed work before expensive dev+review
        cycles begin. You are NOT implementing anything. You are classifying the
        spec and catching problems that would waste downstream budget.

        ## Spec

        {spec_content}

        {scope_feasibility_section}
        ## Current File Contents (file_scope)

        These are the files the spec targets, as they exist RIGHT NOW on the
        main branch:

        {files_block}

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
    task: TaskSpec,
    *,
    story_content: str,
    plan_content: str,
    file_contents: dict[str, str],
    preflight_output: str | None = None,
    rejection_findings: str | None = None,
) -> str:
    """Build the plan review agent prompt.

    The plan review agent reads the story + generated plan (and optionally
    the file_scope contents and preflight output) and produces a structured
    APPROVE/REJECT verdict.
    """
    if file_contents:
        files_block = "\n\n".join(
            f"### `{path}`\n```\n{content}\n```" for path, content in file_contents.items()
        )
    else:
        files_block = "(no file_scope defined — spec applies to entire project)"

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

        {plan_content}

        ## Current Codebase (file_scope)

        {files_block}
        {preflight_section}{rejection_section}
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
    task: TaskSpec,
    *,
    spec_content: str,
    file_contents: dict[str, str],
    preflight_output: str | None = None,
) -> str:
    """Build the planning agent prompt.

    The planning agent reads the spec and file_scope contents, then produces
    a structured forge_plan.md document. It does NOT write code.

    Output is ONLY the plan document, starting with '# Implementation Plan'.
    """
    if file_contents:
        files_block = "\n\n".join(
            f"### `{path}`\n```\n{content}\n```" for path, content in file_contents.items()
        )
    else:
        files_block = "(no file_scope defined — spec applies to entire project)"

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

        {spec_content}

        ## Current File Contents (file_scope)

        These are the files the spec will modify, as they exist right now:

        {files_block}
        {preflight_section}
        ## Output Format

        You MUST output ONLY the plan document. No prose before or after.
        Start your response with `# Implementation Plan` and produce valid markdown.

        The plan MUST cover:

        ```
        # Implementation Plan: {task.name}

        ## Summary
        One paragraph: what we're doing and the key decision(s).

        ## Approach
        The implementation strategy in 3-5 sentences. What is the core idea?
        What is the main trade-off or design choice?

        ## Changes
        For each file to modify:
        - **File**: path
        - **What**: what changes and why (1-2 sentences)
        - **Callers**: list any other files that call the functions being changed

        ## Acceptance Criteria Map
        For each AC in the spec, state which change satisfies it.

        ## Risks
        Anything ambiguous in the spec or risky in the approach. If the spec
        has internal contradictions, call them out here — do not silently
        pick one interpretation.
        ```

        ## Rules

        - Do NOT write code. Do NOT modify files.
        - Do NOT invent function signatures — cite what exists in the codebase.
        - Do NOT pad the plan with edge case tables or test scenario details
          that the dev agent will derive from the code. Keep it lean.
        - Cover ALL acceptance criteria from the spec.
        - If something in the spec is ambiguous or contradictory, say so
          explicitly in Risks rather than guessing.
    """)


# ── Dev prompt ────────────────────────────────────────────────────────


def build_dev_prompt(
    task: TaskSpec,
    *,
    workspace_path: Path,
    branch_name: str,
    spec_content: str,
    gate_command: str,
    gate_skipped: bool = False,
    review_findings: str | None = None,
    human_feedback: str | None = None,
    preflight_output: str | None = None,
    plan_output: str | None = None,
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
    if task.file_scope:
        file_scope_str = "\n".join(f"- `{p}`" for p in task.file_scope)
    else:
        file_scope_str = "- (no scope restriction — all project files)"

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
        plan_section = dedent(f"""\

            ## Implementation Plan (from planning agent)

            The planning agent has already analysed this codebase and produced a detailed
            implementation plan. Follow it closely — do not re-derive the approach from scratch.

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

        ## File Scope

        {file_scope_str}

        Touch other files if needed — keep out-of-scope changes minimal.
        {plan_section}
        ## Spec
        {spec_content}
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
             spec_deviations: none  # or list deviations with justification
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
    task: TaskSpec,
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
          spec_deviations:
            - description: "What deviated from spec"
              justification: "Why you deviated"
          deferred_items:
            - description: "What was deferred"
              reason: "Why it was deferred"
          gate_result: PASS
        ```

        Use `spec_deviations: none` if you followed the spec exactly.
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
    task: TaskSpec,
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

    return dedent(f"""\
        You are continuing work on **{task.name}** (iteration {iteration}).

        ## Working Directory

        `{workspace_path}`  (branch: `{branch_name}`)

        You are already in the correct workspace. Do NOT create a new worktree.
        Do NOT switch branches.
        {context_sections}
        ## Review Findings

        {review_findings}

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
    task: TaskSpec,
    review_outputs: list[str],
    review_names: list[str],
    spec_content: str,
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

        {spec_content}

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
        spec_compliance:
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
    task: TaskSpec,
    *,
    spec_content: str,
    commit_log: str,
    workspace_path: str,
    branch: str,
    handoff_content: str,
    review_role: str | None = None,
    dev_notes: str | None = None,
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
    return dedent(f"""\
        You are reviewing an implementation of **{task.name}**.

        ## Your Role

        {role_section}

        ## Spec

        {spec_content}
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
        spec_compliance:
          matches_spec: true | false
          mismatches:
            - "<description of mismatch>"
        test_coverage:
          adequate: true | false
          gaps:
            - "<description of missing test>"
        ```

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
    """)
