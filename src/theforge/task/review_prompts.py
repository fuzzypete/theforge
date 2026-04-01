from textwrap import dedent

from theforge.coordinator.state import CycleHistory

from .conventions import render_conventions_block
from .story import TaskStory

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
    conventions: list[str] | None = None,
) -> str:
    """Build the review agent prompt.

    The reviewer receives:
    - The commit log (git log main..HEAD) as the primary handoff artifact
    - The spec (to verify compliance)
    - The handoff file (to cross-check validation claims)
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
            You may report new P1 findings ONLY if they are a direct regression
            from the fix — code that was correct before and is now broken by
            changes in these commits. Pre-existing issues in code not modified
            by these commits are P2, not P1, regardless of severity.
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
        {dev_notes_section}{render_conventions_block(conventions)}
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

        - **P1** (blocking): A concrete, demonstrable problem **in code introduced
          or modified by these commits** that would break at runtime, violate a
          specific acceptance criterion, corrupt data, or leave a critical path
          untested. You MUST be able to point to the exact file, line, and what
          would go wrong. Spec violations are P1 only if the code actually fails
          to satisfy the criterion — not if the approach differs from what you'd
          prefer.
        - **P2** (non-blocking): Style, minor improvement, non-critical missing
          test, suggestion for future work, or a project convention violation.
          Does NOT block merge. **Pre-existing issues in code not modified by
          these commits are always P2** — they are valuable signal but they do
          not block this change. Convention violations (from ## Project
          Conventions, if present) are P2 findings — cite the convention by
          name in the description.

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
        - If the spec contains a **Notes** section, treat it as informal hints
          that may be stale or wrong. Notes are NOT acceptance criteria — do not
          flag a spec mismatch because reality diverges from a Note. Only evaluate
          compliance against explicit acceptance criteria and requirements.
        - **Spec-to-runtime traceability**: For each AC in the spec, verify
          BOTH layers: (a) the logic exists in the codebase, AND (b) it is
          actually invoked at runtime by the calling code. Code that produces
          correct output but is never called does NOT satisfy the AC. When an
          AC depends on a function's output being consumed by its caller,
          verify the caller actually uses it — unused return values that are
          required to satisfy an AC are P1.
        - **YAML safety**: In `description` and `suggestion` fields, do NOT use
          backslashes or double-quote characters inside double-quoted strings —
          they break YAML parsing. Use single quotes or paraphrase instead.
          Bad:  description: "Regex changed from r\\"..."
          Good: description: "Regex changed from raw string pattern ..."
    """)
