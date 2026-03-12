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

    return dedent(f"""\
        You are a preflight validator for **{task.name}**.

        ## Your Role

        Before committing expensive dev+review cycles, determine whether this
        spec should proceed to implementation. You are NOT implementing anything.
        You are classifying the spec's current status.

        ## Spec

        {spec_content}

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

        ## Output Format

        You MUST output ONLY a YAML block. No prose before or after.
        Start your response with ```yaml and end with ```.

        ```yaml
        verdict: PROCEED | ALREADY_DONE | BLOCKED
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
