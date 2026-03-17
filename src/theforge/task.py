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

        Before committing expensive dev+review cycles, determine whether this
        spec should proceed to implementation. You are NOT implementing anything.
        You are classifying the spec's current status.

        ## Spec

        {spec_content}

        {scope_feasibility_section}
        ## Current File Contents (file_scope)

        These are the files the spec targets, as they exist RIGHT NOW on the
        main branch:

        {files_block}

        ## Classification

        Evaluate the spec against the current code and output ONE of these verdicts:

        - **PROCEED** — The spec describes work that has NOT been done yet.
          The files exist (or should be created), and the acceptance criteria
          are NOT already satisfied. Implementation should begin.

        - **ALREADY_DONE** — Every acceptance criterion in the spec is ALREADY
          satisfied by the current code. There is nothing to implement.
          You MUST verify each criterion individually — do not assume "related
          code exists" means "spec is satisfied."

        - **BLOCKED** — The spec cannot be implemented as written because:
          - It references files, functions, or APIs that do not exist
          - It conflicts with the current architecture
          - It has unresolvable ambiguities
          - A dependency is missing
          Provide a clear reason so a human can fix the spec.

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
        criteria_checked:
          - criterion: "<acceptance criterion text>"
            satisfied: true | false
            evidence: "<where in the code this is satisfied, or what is missing>"
        ```

        ## Rules

        - Check EVERY acceptance criterion individually. Do not shortcut.
        - "Related code exists" is NOT the same as "criterion is satisfied."
        - If even ONE criterion is unsatisfied, the verdict cannot be ALREADY_DONE.
        - If the spec references things that don't exist, verdict is BLOCKED.
        - When in doubt, verdict is PROCEED — it's cheaper to try than to skip.
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

        You are evaluating whether an implementation plan is sound BEFORE any dev
        budget is spent. You are NOT implementing anything. You produce a structured
        verdict: APPROVE (plan looks sound) or REJECT (plan has problems that will
        cause dev to fail).

        ## Story / Spec

        {story_content}

        ## Generated Plan

        {plan_content}

        ## Current Codebase (file_scope)

        {files_block}
        {preflight_section}{rejection_section}
        ## Evaluation Criteria

        1. Does the plan address ALL acceptance criteria in the story?
        2. Are there technical errors (wrong APIs, hallucinated functions, blast radius gaps)?
        3. Is the implementation order sound (dependencies respected)?
        4. Do the proposed function signatures and module references match the actual codebase?

        ## Output Format

        You MUST output ONLY a YAML block. No prose before or after.
        Start your response with ```yaml and end with ```.

        ```yaml
        verdict: APPROVE | REJECT
        findings:
          - severity: P1
            description: "<what is wrong with the plan>"
            suggestion: "<how to fix it>"
        ```

        ## Rules

        - verdict MUST be APPROVE if no blocking issues found
        - verdict MUST be REJECT if any finding would cause dev to fail
        - REJECT MUST include at least one finding
        - APPROVE with no findings is valid (plan is sound)
        - Be specific: cite the plan section and what is wrong
        - Do NOT flag style preferences — only flag issues that will cause dev failure
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

        You are NOT implementing anything. You are producing a detailed implementation
        plan that a dev agent will follow. Your output is a structured markdown document
        that covers exactly what needs to be done, in what order, with what edge cases.

        ## Spec

        {spec_content}

        ## Current File Contents (file_scope)

        These are the files the spec will modify, as they exist right now:

        {files_block}
        {preflight_section}
        ## Output Format

        You MUST output ONLY the plan document. No prose before or after.
        Start your response with `# Implementation Plan` and produce valid markdown.

        The plan MUST cover all of these sections:

        ```
        # Implementation Plan: {task.name}

        ## Summary
        One paragraph: what we're implementing and why.

        ## Implementation Order
        1. Step one — why first
        2. Step two — depends on step one
        ...

        ## Functions to Modify

        ### `module.function_name(args) -> return_type`
        - **File**: `src/theforge/module.py`
        - **Change**: Add parameter X, handle edge case Y
        - **Signature**: `def function_name(a: str, b: int = 0) -> bool:`

        ## Edge Cases

        | Condition | Expected Behavior | Notes |
        |-----------|-------------------|-------|
        | Empty list passed | Return [] immediately | No error |

        ## Test Scenarios

        ### `test_scenario_name`
        - **Setup**: mock X returns Y
        - **Call**: `function_name("input")`
        - **Assert**: returns True, log contains "message"

        ## Risks and Ambiguities

        - **Risk**: The spec says X but the code does Y — resolve by doing Z
        ```

        ## Rules

        - Do NOT write any code.
        - Do NOT modify any files.
        - Output ONLY the plan document starting with `# Implementation Plan`.
        - Be specific: cite exact function names, file paths, and line numbers where known.
        - Cover ALL acceptance criteria from the spec.
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
    iteration: int = 1,
    escalation_note: str | None = None,
    cycle_history: list[CycleHistory] | None = None,
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
        gate_steps = dedent("""\
            8. Gate: none (spec override) — the coordinator will skip the gate for
               this spec. Do NOT run a gate command. Commit all changes and confirm
               your work is done.
        """)
    else:
        gate_steps = dedent(f"""\
            8. Run the gate to generate the handoff artifact:
               ```bash
               {gate_command}
               ```
            9. If the gate generated `handoff.yaml`, fill in these fields:
               - `scope_completed`: list what you implemented
               - `deferred_followups`: list anything you couldn't finish
               - `next_recommended_step`: single next action
            10. Add a `dev_notes` section to handoff.yaml using this exact YAML structure:

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
                        justification: "Why you deviated — cite the spec section and your reason"
                    deferred_items:
                      - description: "What was deferred"
                        reason: "Why it was deferred"
                    gate_result: PASS

                Use `spec_deviations: none` if you followed the spec exactly.
                Use `deferred_items: none` if nothing was deferred.
                List ALL commits you made (use `git log --oneline` to get shas).
                List EVERY acceptance criterion from the spec with its status.

                The coordinator validates this structure. If it's malformed you'll
                be asked to rewrite it, so get the format right the first time.
                This is your voice in the review — the reviewer reads it before
                the diff.
        """)

    return dedent(f"""\
        You are implementing **{task.name}** for this project.

        ## Working Directory

        You are already in the correct workspace: `{workspace_path}`
        Branch: `{branch_name}`

        Do NOT create a new worktree. Do NOT switch branches. You are already set up.

        ## File Scope

        Focus your changes on these files:
        {file_scope_str}

        If you need to touch a file not listed here, do so — but keep changes
        minimal and directly related to the spec. The reviewer will flag any
        unexpected out-of-scope changes.

        {plan_section}
        ## Spec
        {spec_content}
        {feedback_section}{preflight_section}
        ## Implementation Steps

        1. Read the spec above carefully before writing any code.
        2. Implement the spec. Write tests for new functionality.
        3. After implementation, run these commands in order:
           ```bash
           make fmt    # auto-fix formatting
           make lint   # verify style/types
           ```
        4. Fix any lint failures before proceeding.
        5. Run the full gate (not just your test file — the gate runs everything):
           ```bash
           {gate_command}
           ```
        6. Fix any failures. Do NOT declare success until the full gate passes.
        7. Commit your changes with a conventional commit message:
           ```bash
           git add <files-you-changed>
           git commit -m "<type>(<scope>): <description>"
           ```
        {gate_steps}
        ## Rules

        - Do NOT merge to main.
        - Do NOT modify docs/project_plan.md.
        - Do NOT leave uncommitted changes.
        - Do NOT skip `make fmt` or `make lint`.
        - If you cannot complete the task for any reason, commit what you
          have and note blockers in `deferred_followups`.
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
        ## P1 Findings to Fix

        The code reviewer identified the following issues that MUST be fixed:

        {review_findings}

        ## Your Task

        1. Read the findings above carefully.
        2. Fix each P1 finding. Address P2 findings if feasible.
        3. Run `make fmt` to auto-fix formatting.
        4. Commit your changes:
           ```bash
           git add <files-you-changed>
           git commit -m "fix(<scope>): address review findings (iter {iteration})"
           ```

        ## Important

        {gate_bullet}- Do NOT re-read the full spec — you already have the context from
          your previous session.
        - Do NOT leave uncommitted changes.
        - Focus ONLY on fixing the identified findings. Do not refactor
          unrelated code.
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
        You are a code reviewer focused on **correctness**.
        Your job is to verify:
        1. The implementation matches the spec
        2. Logic and correctness bugs (wrong conditions, off-by-one, bad state)
        3. Data integrity risks (corruption, lost writes, inconsistent state)
        4. Security issues (injection, auth bypass, unsafe defaults)

        You are NOT implementing anything. Do NOT write code. Do NOT make changes."""),
    "patterns": dedent("""\
        You are a code reviewer focused on **patterns and design**.
        Your job is to verify:
        1. API usage patterns and idiom violations (wrong abstractions, leaky boundaries)
        2. Error handling completeness (unhandled exceptions, silent failures)
        3. Test coverage gaps and missing edge-case tests
        4. Code organization and interface design (coupling, cohesion, naming)

        You are NOT implementing anything. Do NOT write code. Do NOT make changes."""),
    "edge-cases": dedent("""\
        You are a code reviewer focused on **edge cases and failure modes**.
        Your job is to verify:
        1. Boundary conditions and off-by-one errors (empty inputs, max values, zero)
        2. Race conditions and concurrency hazards (shared state, ordering assumptions)
        3. State that survives when it shouldn't (cleanup paths, reset logic, teardown)
        4. Failure modes under unexpected input or timing (partial writes, timeouts, retries)

        You are NOT implementing anything. Do NOT write code. Do NOT make changes."""),
}

_REVIEW_ROLE_GENERIC = dedent("""\
    You are a code reviewer. Your job is to verify:
    1. The implementation matches the spec
    2. The code is correct and safe
    3. Tests adequately cover the changes
    4. No regressions are introduced

    You are NOT implementing anything. Do NOT write code. Do NOT make changes.""")


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
        Review them as you would a pull request — read the commit messages to
        understand what was done, then use `git show <sha>` or your Read/Bash/Glob/Grep
        tools to inspect the actual source in the worktree at: {workspace_path}

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

        - **P1** (blocking): Correctness bug, data integrity risk, spec violation,
          security issue, missing critical test. MUST be fixed before merge.
        - **P2** (non-blocking): Style issue, minor improvement, non-critical missing
          test, documentation gap. SHOULD be fixed but does not block merge.

        ## Rules

        - verdict MUST be `APPROVE` if there are zero P1 findings
        - verdict MUST be `REQUEST_CHANGES` if any P1 finding exists
        - Be concrete: cite file + line + what is wrong + how to fix
        - Verify against the spec. Do NOT approve just because it looks reasonable.
        - Do NOT invent issues. Only report real problems you can find in the source.
        - If no files were changed or the change is trivial, still verify against the spec.
        - Check that the handoff validation results are consistent (all PASS for PASS gate).
    """)
