"""Task definition and prompt builders for agent invocations.

The orchestrator builds prompts mechanically from templates + spec content.
No LLM is involved in prompt construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent


@dataclass(frozen=True)
class TaskSpec:
    """A single unit of work for the orchestrator to execute."""

    name: str  # human-readable, e.g. "Phase 6H: per-user export"
    spec_path: Path  # path to the spec file
    slug: str  # workspace slug, e.g. "export-service"
    file_scope: list[str]  # paths the agent may modify
    pytest_target: str | None = None  # specific test target, or None for all


def load_spec(spec_path: Path) -> str:
    """Read the spec file content. Raises FileNotFoundError if missing."""
    return spec_path.read_text(encoding="utf-8")


# ── Dev prompt ────────────────────────────────────────────────────────


def build_dev_prompt(
    task: TaskSpec,
    *,
    workspace_path: Path,
    branch_name: str,
    spec_content: str,
    gate_command: str,
    review_findings: str | None = None,
    human_feedback: str | None = None,
    iteration: int = 1,
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

    pytest_line = task.pytest_target or "tests/"

    return dedent(f"""\
        You are implementing **{task.name}** for this project.

        ## Working Directory

        You are already in the correct workspace: `{workspace_path}`
        Branch: `{branch_name}`

        Do NOT create a new worktree. Do NOT switch branches. You are already set up.

        ## File Scope

        You may ONLY create or modify files in these locations:
        {file_scope_str}

        If the task requires changes outside this list, STOP. Add a note in your
        commit message that scope expansion is needed and describe what file(s).
        Do NOT make out-of-scope changes.

        ## Spec
        {spec_content}
        {feedback_section}
        ## Implementation Steps

        1. Read the spec above carefully before writing any code.
        2. Implement the spec. Write tests for new functionality.
        3. After implementation, run these commands in order:
           ```bash
           make fmt    # auto-fix formatting
           make lint   # verify style/types
           ```
        4. Fix any lint failures before proceeding.
        5. Run tests:
           ```bash
           poetry run pytest {pytest_line} -v
           ```
        6. Fix any test failures.
        7. Commit your changes with a conventional commit message:
           ```bash
           git add <files-you-changed>
           git commit -m "<type>(<scope>): <description>"
           ```
        8. Run the gate to generate the handoff artifact:
           ```bash
           {gate_command}
           ```
        9. If the gate generated `handoff.yaml`, fill in these fields:
           - `scope_completed`: list what you implemented
           - `deferred_followups`: list anything you couldn't finish
           - `next_recommended_step`: single next action

        ## Rules

        - Do NOT merge to main.
        - Do NOT modify docs/project_plan.md.
        - Do NOT leave uncommitted changes.
        - Do NOT skip `make fmt` or `make lint`.
        - If you cannot complete the task, commit what you have and note blockers
          in `deferred_followups`.
    """)


# ── Review prompt ─────────────────────────────────────────────────────


def build_review_prompt(
    task: TaskSpec,
    *,
    spec_content: str,
    diff_text: str,
    handoff_content: str,
) -> str:
    """Build the review agent prompt.

    The reviewer receives:
    - The diff (focused attention, not the full codebase)
    - The spec (to verify compliance)
    - The handoff.yaml (to cross-check validation claims)

    The reviewer outputs ONLY a YAML block. No prose.
    """
    return dedent(f"""\
        You are reviewing an implementation of **{task.name}**.

        ## Your Role

        You are a code reviewer. Your job is to verify:
        1. The implementation matches the spec
        2. The code is correct and safe
        3. Tests adequately cover the changes
        4. No regressions are introduced

        You are NOT implementing anything. Do NOT write code. Do NOT make changes.

        ## Spec

        {spec_content}

        ## Diff to Review

        ```diff
        {diff_text}
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
        - Do NOT invent issues. Only report real problems you can point to in the diff.
        - If the diff is empty or trivial, still verify against the spec.
        - Check that the handoff validation results are consistent (all PASS for PASS gate).
    """)
