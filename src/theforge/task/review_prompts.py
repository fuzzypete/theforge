from textwrap import dedent

from theforge.coordinator.state import CycleHistory

from .context_assembler import ContextPack
from .conventions import render_conventions_block, render_hard_conventions_block
from .debrief_prompts import STYLE_TOOL_CALL, STYLE_YAML_BLOCK, render_debrief_section
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
        ac_verification:
          - criterion: "<verbatim AC text, or 'Symptom resolution: ...' for bug-type issues>"
            status: VERIFIED | PARTIAL | NOT_VERIFIED
            evidence: >-
              <For VERIFIED: file:line diff hunks implementing the criterion AND
              tests covering the issue's specific failure mode (signature, error
              string, retry context). Pointers must be concrete, not generic.
              For PARTIAL or NOT_VERIFIED: explain what is missing.>
        criteria_enumerable: true
        criteria_enumerable_rationale: ""
        ```

        ## Rules

        - verdict MUST be `APPROVE` if there are zero P1 findings AND every
          ac_verification entry is VERIFIED
        - If the issue genuinely has NO enumerable acceptance criteria (some bug
          fixes, chores), you MAY APPROVE with an empty `ac_verification` by
          setting `criteria_enumerable: false` and giving a one-sentence
          `criteria_enumerable_rationale`. Use this only when there is truly
          nothing to enumerate — never to skip verifying criteria that exist.
        - verdict MUST be `REQUEST_CHANGES` if any P1 finding exists OR any
          ac_verification entry is PARTIAL or NOT_VERIFIED
        - Reconcile per-AC verification across reviewers — if any reviewer
          reported an AC as PARTIAL or NOT_VERIFIED, do NOT mark it VERIFIED
          unless a counter-reviewer cited concrete evidence that resolves the gap.
        - For bug-type issues with a categorical symptom claim, treat
          verification as two checks: the reported instance is fixed, and the
          stated scope is covered across structurally analogous locations.
        - Be concrete: cite file + line + what is wrong + how to fix
        - Do NOT invent issues. Only report findings with evidence from the reviews.
    """)


def build_review_prompt(
    task: TaskStory,
    *,
    story_content: str,
    commit_log: str,
    diff_stat: str = "",
    diff_content: str = "",
    commit_diffs: str,
    workspace_path: str,
    branch: str,
    handoff_content: str,
    handoff_commit_warning: str | None = None,
    mode: str = "cli",
    review_role: str | None = None,
    dev_notes: str | None = None,
    cycle_history: list[CycleHistory] | None = None,
    conventions: list[str] | None = None,
    stack: tuple[str, ...] | list[str] | None = None,
    allowed_root_files: tuple[str, ...] | list[str] | None = None,
    no_scratch_files: bool | None = None,
    assembled_context: ContextPack | None = None,
    sandboxed: bool = True,
    containment: str | None = None,
    authoritative_gate_decision: str | None = None,
    authoritative_gate_commit: str | None = None,
    # Which validation profile produced that verdict and what standing it
    # carries (#2358). A reviewer handed a result must be able to tell a verdict
    # from a signal; absent (legacy configs, older records) the line is omitted
    # and the prompt reads exactly as it did before.
    authoritative_gate_profile: str | None = None,
    authoritative_gate_authority: str | None = None,
    fix_claim_flags: list[str] | None = None,
    p2_policy: str = "in_scope",
) -> str:
    """Build the review agent prompt.

    The reviewer receives:
    - The commit log (git log main..HEAD --oneline) in commit order
    - The full per-commit diffs (git show for each commit) as the primary source of truth
    - The spec (to verify compliance)
    - The handoff file (to cross-check validation claims)

    This mirrors a PR review workflow: reviewers discover files from commits and
    review the embedded diffs, not a pre-enumerated file list.

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
            - If fixed: mention it briefly in `summary` if useful, or omit it entirely.
              Do NOT put closure notes or acknowledgements of resolved prior bugs in
              `findings[]`.
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

            When reporting a P2, do NOT frame it as "pre-existing, so skip it"
            or "out of scope" in a way that creates binding permission for the
            next dev agent to ignore it. Describe the defect and why it matters;
            the dev agent decides whether that P2 is in-scope for the current
            run under the active P2 policy (`{p2_policy}`) and its proximity to
            the code being changed.

        """)

    _dev_notes_base = (
        dedent(f"""\

            ## Developer Notes

            {dev_notes}

            Read this before examining the diff. The developer has flagged intentional
            decisions and spec deviations here. Developer notes are claims, not
            evidence — verify technical claims against the actual code before
            accepting them. Flag deviations that are unjustified, incorrect, or
            whose justification does not hold up on inspection.
        """)
        if dev_notes
        else ""
    )

    # Append fix-claim warnings after dedent so dynamic multi-line content does
    # not interfere with dedent's common-indent calculation.
    if _dev_notes_base and fix_claim_flags:
        _warn_lines = ["⚠ Unsubstantiated fix claims detected (heuristic — verify independently):"]
        for _flag in fix_claim_flags:
            _warn_lines.append(f"- {_flag}")
        dev_notes_section = _dev_notes_base + "\n".join(_warn_lines) + "\n"
    else:
        dev_notes_section = _dev_notes_base

    context_section = ""
    if assembled_context and assembled_context.content:
        context_section = dedent(f"""\

            ## Repository Context Pack

            Use this curated repository context before exploring additional files.

            {assembled_context.content}
        """)

    output_format_section = dedent("""\
        ## Output Format

        You MUST output ONLY a YAML block. No prose before or after.
        Start your response with ```yaml and end with ```.

        Each finding has four prose fields. Write them with care: the post_run
        hook files one GitHub issue per finding, and the body is rendered
        directly from these fields without manual editing. They must read as a
        bug story, not as code-review feedback on a merged PR.

        - `observed`: ONE sentence in past/present tense, behaviour-only. No
          fix theory, no verdict ("APPROVE"/"REJECT"), no "the developer should…".
        - `expected`: a CATEGORY-LEVEL rule in flowing prose that generalises
          beyond this single trigger. Not a checklist, not a test plan, not the
          specific symptom restated. State the contract that this code path is
          violating, in language that would make sense even if a different
          input later triggered the same class of bug.
        - `evidence`: file path plus line/anchor; one short clause is enough.
        - `suggestion`: OPTIONAL non-binding fix sketch, or empty string. The
          dev agent is not bound by it; do not phrase it as a directive.

        Worked example for a status/audit consistency bug (paths are
        illustrative — substitute the real file from your test_target):

        ```yaml
        - severity: P1
          file: "<path/to/affected_module>"
          line: <line>
          observed: >-
            When a dependency's queued task fails its poll, the user-visible
            status report records the outcome as DONE while the audit log
            records FAILED, so the status command and the audit log disagree
            about the dependency's final state.
          expected: >-
            When a unit of work enters a terminal failure state through any
            code path, every operator-facing report and the audit record must
            agree on that terminal state, so status output and the audit
            record never disagree about a final outcome.
          evidence: "<path/to/affected_module> near line <line> (failure branch)"
          suggestion: ""
        ```

        Only the `## Spec` section (and its `## Acceptance criteria`, if present)
        is authoritative for `story_compliance.matches_spec` and
        `ac_verification`. The Repository Context Pack, including any injected
        prior-run summaries, is advisory background — never cite it as the
        reason an AC failed or `matches_spec` is false.

        ```yaml
        verdict: APPROVE | REQUEST_CHANGES
        summary: "<one-line summary of your review>"
        findings:
          - severity: P1 | P2
            file: "<file path>"
            line: <line number or null>
            observed: "<one sentence describing the observed behaviour>"
            expected: >-
              <category-level rule in flowing prose that generalises beyond
              this trigger>
            evidence: "<file path + line/anchor>"
            suggestion: "<optional non-binding fix sketch, or empty string>"
        story_compliance:
          matches_spec: true | false
          mismatches:
            - "<description of mismatch>"
        test_coverage:
          adequate: true | false
          gaps:
            - "<description of missing test>"
        ac_verification:
          - criterion: "<verbatim AC text, or 'Symptom resolution: ...' for bug issues>"
            status: VERIFIED | PARTIAL | NOT_VERIFIED
            evidence: >-
              <For VERIFIED: file:line diff hunks implementing the criterion AND
              tests covering the issue's specific failure mode (signature, error
              string, retry context). Pointers must be concrete, not generic.
              For PARTIAL or NOT_VERIFIED: explain what is missing.>
        criteria_enumerable: true
        criteria_enumerable_rationale: ""
        ```

        If the issue genuinely has NO enumerable acceptance criteria (some bug
        fixes, chores), you MAY APPROVE with an empty `ac_verification` by setting
        `criteria_enumerable: false` and a one-sentence
        `criteria_enumerable_rationale`. Use this only when there is truly nothing
        to enumerate — never to skip verifying criteria that exist.
    """)
    if mode == "api":
        output_format_section = dedent("""\
            ## Output Format

            You MUST call the `submit_review` tool to deliver your verdict.
            Do NOT return your review as plain text — it will be ignored.
            Use the submit_review tool with your structured review data.

            Each finding takes four prose fields. They are rendered directly
            into a GitHub bug story by the post_run hook, so write them as a
            bug report rather than as code-review feedback on a merged PR:

            - `observed`: ONE sentence, behaviour-only — no fix theory, no
              verdict, no "the developer should…".
            - `expected`: a CATEGORY-LEVEL rule in flowing prose that
              generalises beyond this trigger. Not a checklist, not the
              symptom restated. State the contract this code path violates.
            - `evidence`: file path + line/anchor; one short clause.
            - `suggestion`: OPTIONAL non-binding sketch, or empty string. The
              dev agent is not bound by it; do not phrase it as a directive.

            Worked example values for a status/audit consistency bug (paths
            are illustrative — substitute the real file from your test_target):

              observed: "When a dependency's queued task fails its poll, the
              user-visible status report records the outcome as DONE while
              the audit log records FAILED, so the status command and the
              audit log disagree about the dependency's final state."

              expected: "When a unit of work enters a terminal failure state
              through any code path, every operator-facing report and the
              audit record must agree on that terminal state, so status
              output and the audit record never disagree about a final
              outcome."

              evidence: "<path/to/affected_module> near line <line>
              (failure branch)"

              suggestion: ""

            Only the `## Spec` section (and its `## Acceptance criteria`, if
            present) is authoritative for `story_compliance.matches_spec` and
            `ac_verification` in the submit_review call. The Repository Context
            Pack, including any injected prior-run summaries, is advisory
            background — never cite it as the reason an AC failed or
            `matches_spec` is false.
        """)

    # Distinguish mechanical containment from native-flag / prompt-only runs so
    # the reviewer knows whether write confinement was syscall-enforced (#1907).
    _containment_labels = {
        "mechanical": "MECHANICAL (host sandbox wrapper confined writes to the worktree)",
        "native": "NATIVE (provider sandbox flag confined writes to the worktree)",
        "unavailable": "DISABLED (containment requested but host sandbox unavailable)",
        "none": "DISABLED (unsandboxed — effects ran unconfined)",
    }
    if containment is not None:
        sandbox_label = _containment_labels.get(
            containment, "enabled" if sandboxed else _containment_labels["none"]
        )
    else:
        sandbox_label = "enabled" if sandboxed else _containment_labels["none"]
    gate_decision_label = authoritative_gate_decision or "UNAVAILABLE"
    gate_commit_label = authoritative_gate_commit or "UNAVAILABLE"
    gate_profile_line = (
        ""
        if not authoritative_gate_profile
        else (
            f"\n        Validation profile: {authoritative_gate_profile} "
            f"({authoritative_gate_authority or 'merge'} authority)"
        )
    )
    _hard_block = render_hard_conventions_block(
        stack=stack,
        allowed_root_files=allowed_root_files,
        no_scratch_files=no_scratch_files,
    )

    return dedent(f"""\
        You are reviewing an implementation of **{task.name}**.

        ## Your Role

        {role_section}
        {cycle_framing_section}
        ## Spec

        {story_content}
        {dev_notes_section}{context_section}{_hard_block}{render_conventions_block(conventions)}
        ## Verified Git Metadata

        This section is coordinator-verified ground truth captured from git before review.
        Treat it as authoritative over any self-reported handoff claims.

        Sandbox isolation: {sandbox_label}
        Authoritative gate verdict: {gate_decision_label}
        Authoritative gate commit: {gate_commit_label}{gate_profile_line}

        Verified commit log (`git log --oneline main..HEAD`):
        ```
        {commit_log}
        ```

        Verified diff stat (`git diff main --stat`):
        ```
        {diff_stat}
        ```

        Verified diff content (`git diff main`):
        ```
        {diff_content}
        ```

        {handoff_commit_warning or ""}

        ## Commits

        The following commits implement the spec on branch `{branch}`.
        The full diffs are embedded above. Use Read/Bash/Glob tools only if you need
        to inspect surrounding context not shown in the diff. Worktree: {workspace_path}

        Commit history:
        ```
        {commit_log}
        ```

        Full diffs:
        ```
        {commit_diffs}
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
          and the coordinator's authoritative gate verdict for the reviewed HEAD is
          `PASS`, it should be approved even if you'd do it differently.
        - verdict MUST be `APPROVE` if there are zero P1 findings
        - verdict MUST be `REQUEST_CHANGES` if any P1 finding exists
        - **Full-gate ownership is coordinator-only.** Do NOT run the full
          authoritative gate yourself when the supplied `Authoritative gate commit`
          still matches the HEAD you are reviewing; rely on the supplied verdict.
        - When the supplied gate evidence is current for the reviewed HEAD but
          the authoritative verdict is a non-`PASS` value such as `FAIL`,
          `ERROR`, or `SKIPPED`, do NOT rerun the full gate. Review the diff on
          its merits, and if your conclusion depends on the non-`PASS` gate
          state, say so explicitly in `summary`.
        - You MAY run the full authoritative gate only when the supplied gate
          evidence is missing or stale for the current HEAD — for example, if
          `Authoritative gate commit` is `UNAVAILABLE` or no longer matches
          `git rev-parse HEAD`. When that happens, note the mismatch in `summary`
          so the coordinator can refresh the authoritative result.
        - A P1 must cite a concrete failure: file + line + what breaks. "Could be
          improved" or "might cause issues" is P2, not P1.
        - **Verify before asserting.** Before filing a P1 about a runtime behavior,
          confirm the behavior exists in the current code. Check the actual type,
          call site, mutability, and control flow — do not assume. A P1 based on
          an unverified assumption is a false positive that blocks the entire
          pipeline.
        - **List ALL issues in a single pass.** Multiple findings in one
          REQUEST_CHANGES is far better than discovering new issues across
          review cycles.
        - Do NOT invent issues. Only report problems you can find in the source.
        - Do NOT use phrases like `pre-existing`, `out of scope`, or similar
          as binding permission for the next dev agent to skip a P2. Report the
          defect and why it matters; the dev agent decides whether that P2 is
          in-scope for the current run under the active P2 policy (`{p2_policy}`)
          and its proximity to the code being changed.
        - Do NOT flag the same issue that was already flagged and fixed in a
          previous review cycle.
        - `findings[]` is only for defects in the current code under review.
          Do NOT use it for closure notes such as "Prior P1 from Cycle 2 is fixed".
          If you want to acknowledge a resolved prior issue, put it in `summary`
          or omit it entirely.
        - This review may be merged with other reviewers' outputs. One speculative
          P1 from you blocks the entire pipeline. Be precise.
        - If the spec contains a **Notes** section, treat it as informal hints
          that may be stale or wrong. Notes are NOT acceptance criteria — do not
          flag a spec mismatch because reality diverges from a Note. Only evaluate
          compliance against explicit acceptance criteria and requirements.
        - **WHAT-not-HOW body rule**: An issue body must describe observable
          behavior, not implementation. If the spec body contains a `## Design`
          (or equivalent) section, file paths, function names, or line numbers
          in its acceptance criteria, treat those as informational only —
          verify the implementation against the *behavior* the criterion
          describes, not against the named code locations. The shape gate
          flags such bodies as `implementation_plan_in_body`; if you see a
          body that should have been flagged but slipped through, note it
          in `summary` so the operator can re-shape the issue.
        - **Only `## Spec` decides compliance**: `story_compliance.matches_spec`
          and every `ac_verification` entry must be judged solely against the
          `## Spec` section (its `## Acceptance criteria`, if present). The
          Repository Context Pack — including any injected prior-run
          summaries — is advisory background for orienting your investigation,
          never a source of truth for what the story requires. Do NOT mark an
          AC `NOT_VERIFIED`/`PARTIAL` or set `matches_spec: false` because the
          implementation diverges from something stated only in the Repository
          Context Pack.
        - **Spec-to-runtime traceability**: For each AC in the spec, verify
          BOTH layers: (a) the logic exists in the codebase, AND (b) it is
          actually invoked at runtime by the calling code. Code that produces
          correct output but is never called does NOT satisfy the AC. When an
          AC depends on a function's output being consumed by its caller,
          verify the caller actually uses it — unused return values that are
          required to satisfy an AC are P1.
        - **Per-AC verification table is mandatory**: You MUST emit an
          `ac_verification:` entry for every acceptance criterion declared in
          the spec — verbatim from the `## Acceptance criteria` section, in
          order. For each entry, status is `VERIFIED` only when (a) you can
          point to specific diff hunks that implement the criterion AND (b)
          tests exist that exercise the **specific failure mode the issue was
          filed against** (concrete signatures, error strings, paths from the
          spec — not a generic "could plausibly cover this category" test).
          A generic implementation without a test for the production-observed
          symptom is `PARTIAL`, not `VERIFIED`. If you cannot find evidence,
          mark it `NOT_VERIFIED` with an explanation. Do NOT mark VERIFIED
          based on the dev's handoff claims — verify against the actual diff
          and test files.
        - **Bug issues** (no `## Acceptance criteria` section, only
          observed/expected) get a single ac_verification entry with criterion
          `"Symptom resolution: <one-line restatement of the observed symptom>"`,
          status VERIFIED only when you verify BOTH of these separately:
          (1) the originally reported instance is fixed, with a test or other
          evidence that would have caught it before the fix; and
          (2) when the symptom text is categorical ("every", "any",
          "regardless", etc.), the symptom's stated scope is covered across
          structurally analogous locations. If a diagnosis scope-coverage
          record is visible, use its examined locations to judge that claimed
          scope; otherwise evaluate the categorical claim directly from the
          issue's stated symptom. Fixing only the first reproducing instance is
          PARTIAL for a categorical bug symptom.
        - **Cross-validation enforced**: APPROVE with any PARTIAL or
          NOT_VERIFIED ac_verification entry will be rejected by the schema
          validator and your review will be discarded — do not produce that
          combination. APPROVE with an empty ac_verification is also rejected
          UNLESS you set `criteria_enumerable: false` with a rationale (only
          legitimate when the issue has no enumerable criteria).
        - **YAML safety**: In `description` and `suggestion` fields, do NOT use
          backslashes or double-quote characters inside double-quoted strings —
          they break YAML parsing. Use single quotes or paraphrase instead.
          Bad:  description: "Regex changed from r\\"..."
          Good: description: "Regex changed from raw string pattern ..."
    """) + render_debrief_section(
        assembled_context,
        style=STYLE_TOOL_CALL if mode == "api" else STYLE_YAML_BLOCK,
    )
