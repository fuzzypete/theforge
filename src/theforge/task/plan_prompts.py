from textwrap import dedent
from typing import TYPE_CHECKING

from theforge.domains import taxonomy_prompt_lines

from .context_assembler import ContextPack
from .conventions import render_conventions_block
from .debrief_prompts import STYLE_YAML_BLOCK, render_debrief_section
from .plan_parser import PlanData
from .story import TaskStory

if TYPE_CHECKING:
    pass


def build_preflight_prompt(
    task: TaskStory,
    *,
    story_content: str,
    assembled_context: ContextPack | None = None,
) -> str:
    """Build the preflight check prompt.

    The preflight agent receives the spec and determines whether it is already
    implemented, valid and ready for implementation, or blocked/stale.

    This is a one-shot classification call — the agent outputs a structured
    YAML verdict, not code.
    """
    context_section = ""
    if assembled_context and assembled_context.content:
        context_section = dedent(f"""\

            ## Repository Context Pack

            Use this curated repository context before exploring additional files.

            {assembled_context.content}
        """)

    domain_taxonomy = taxonomy_prompt_lines()

    return dedent(f"""\
        You are a bounded forensic classifier for **{task.name}**.

        ## Your Role

        You are NOT reviewing code quality. You are NOT planning implementation.
        You are NOT critiquing requirements.

        Your only job is sufficiency judgment — does this spec describe work that
        a dev agent can do, has already done, or cannot do at all?

        ## Spec

        {story_content}{context_section}

        ## Classification

        Evaluate the spec against the repository content you can inspect in the
        provided working directory and output ONE of these verdicts:

        - **PROCEED** — PROCEED is the default verdict. If the work is implementable
          and unfinished, output PROCEED. The spec describes unfinished work that a
          dev agent can implement.

        - **ALREADY_DONE** — Every acceptance criterion is ALREADY satisfied by
          the target baseline branch content. You MUST verify each criterion
          individually with direct code evidence.

        - **BLOCKED** — Reserved for exceptional cases only. Use BLOCKED when:
          - Missing credentials or secrets that the dev agent cannot create
          - Requirements that directly contradict each other (not mere ambiguity)
          - An external dependency that does not exist and cannot be installed
          - A required fact that is absent and cannot be inferred
          Provide a clear reason so a human can fix the blocker.

        When you output BLOCKED you MUST also state `blocking_basis` — which of
        those kinds of blocker this is. Use `policy_assertion` when what stops the
        story is a standing policy, doctrine, or architectural decision you read
        somewhere (a comment, a guide, a source-of-truth document) that the story
        would contradict.

        ## Policy Assertions

        A story that contradicts written policy prose is NOT automatically blocked.
        Prose written by a previous run carries no operator authority; only a
        ratified decision (an ADR clause or a recorded operator decision) does. The
        coordinator decides which — you supply the citation, not the verdict.

        Whenever your `reason` leans on a standing policy or architectural decision,
        list every such assertion in `policy_assertions_cited`, quoting the assertion
        as written and naming where you read it. Do not paraphrase the assertion into
        your own words: the coordinator matches your quote against the ratified-policy
        registry. If the source names an ADR clause or operator decision, put it in
        `claimed_reference`; if it names none, say so with
        `claimed_provenance: unknown` rather than assuming the assertion is settled.

        If you cite no assertion but your reason still appeals to something being
        "intentional", "deliberate", "by design", or "already decided", the
        coordinator treats that as unmarked prose and will not let it stop the story.

        **Ambiguity is NOT grounds for BLOCKED.** Route ambiguous specs to PROCEED
        with `sufficiency: needs_planning` — the plan agent resolves ambiguity.

        **File path references**: If the spec mentions file paths that don't exist
        on disk, do NOT set verdict to BLOCKED for this reason alone. Instead,
        list the missing paths in the `warnings` field and proceed normally.
        The plan agent will discover the correct paths.

        ## Spec Quality Check

        Before classifying, scan the spec for these problems:

        1. **Contradictions**: Do any requirements directly conflict with acceptance
           criteria? Do any acceptance criteria directly contradict each other?
        2. **Impossible constraints**: Does the spec require mutually exclusive
           behaviors (e.g., "never overwrite files" + a fixed filename that runs
           multiple times)?
        3. **Ambiguity**: Can every acceptance criterion be objectively verified by
           reading code or running tests? If not, add to `spec_issues` with type
           `ambiguity` and set `sufficiency: needs_planning` — do NOT set BLOCKED.

        Only direct contradictions and impossible constraints can trigger BLOCKED.
        Ambiguity is routed to needs_planning, not BLOCKED.

        ## Investigation Budget

        Do targeted searches. Inspect the minimum number of files needed to answer
        each question, then stop. Do not read entire files when a grep or a targeted
        line read suffices. Do not expand investigation because a result was
        surprising — record the finding and move on.

        If you have not found conclusive evidence after inspecting 3-5 files per
        acceptance criterion, record what you found, note the uncertainty in the
        `evidence` field, and produce your output.

        ## No Delegation

        Do the investigation yourself, in this turn. Do NOT hand work to a
        sub-agent, background process, scheduled wakeup, or any other
        out-of-band task, and do NOT end your turn waiting for something to
        complete or notify you. Nothing can resume you: this execution context
        has no mechanism to wake you when a delegated task finishes, so a turn
        that ends while waiting is a turn that produced nothing and is killed.
        Whatever you have inspected by the time you stop, emit the YAML verdict
        from that evidence.

        ## Uncertainty

        Uncertainty does NOT cause BLOCKED and does NOT justify more investigation.
        Instead:
        - Lower your confidence in the verdict (use `reason` to say so)
        - Increase complexity assessment (unknown blast radius → medium or large)
        - Set `sufficiency: needs_planning` so the plan agent investigates further

        You MUST always produce output. Output under uncertainty is better than
        no output.

        ## Complexity Assessment

        When verdict is PROCEED, output BOTH a numeric `complexity_score` (integer
        1–10) AND the three-level `complexity` enum. The score is the primary
        signal; the enum is kept for compatibility.

        Anchor the score against these concrete examples so scores stay
        comparable across stories and across model invocations:

        - **1–2 (trivial)**: typo fix, single-word rename in one file, comment
          tweak, adjusting a literal constant in a config file. No test changes.
        - **3 (small)**: localized single-function bug fix within one module,
          adding a new optional argument with a default, a config flag read.
          Maybe one test updated.
        - **4–5 (medium-small)**: new feature contained to 2–3 files, a new
          helper plus its tests, a bug fix that spans caller and callee.
        - **6–7 (medium-large)**: multi-module feature, changes that touch a
          shared interface field consumed by a handful of callers, a contract
          change with a small but non-trivial blast radius. Score 7 if the
          change requires careful planning but is still localizable.
        - **8 (large)**: cross-module refactor, a rename of a widely-referenced
          field or function, schema migration with multiple consumers.
        - **9 (very large, still one story)**: the largest scope that is still
          coherent as a single story — architectural change, new subsystem, a
          rewrite of a phase of the state machine, a change touching >10 files
          or requiring coordinated migration, where the whole change is one
          seam that a single dev agent can hold and land in one pass. Hard,
          but not divisible without breaking the change apart mid-seam.
        - **10 (beyond one story)**: the work exceeds what a single story
          should attempt and should be decomposed before it runs — it contains
          two or more independently landable units, each with its own
          acceptance criteria, that happen to be described together. The test
          is divisibility, not size: if you can name the pieces and each piece
          could be reviewed and merged on its own, this is a 10.

        Scores 9 and 10 are mutually exclusive. A change that is one seam is a
        9 no matter how large; a change that is several landable units is a 10
        no matter how small each unit is. Never score 10 merely because a story
        is hard, unfamiliar, or heavy to validate.

        Whenever you assign `complexity_score: 10`, also emit
        `scope_exceeded: true`; for every other score emit
        `scope_exceeded: false`. `scope_exceeded` reports one thing only —
        that the *implementation* work should be split into separate stories.
        A story whose implementation is one coherent unit is
        `scope_exceeded: false` even when it is expensive to validate,
        long-running, or execution-heavy.

        Treat the following as **LARGE (score 8+) even if the local edit count
        looks modest**:
        - Concurrency control: threads, processes, async cancellation,
          inter-thread/inter-process coordination
        - Lifecycle or cancellation propagation across module boundaries
        - Multi-phase state-machine modifications, phase-boundary changes, or
          phase handoff changes
        - Cross-module coordinator surgery where correctness depends on runner /
          engine / phase sites changing together

        Size the **category of work**, not just the apparent line count. Apply
        this rule regardless of provider model, story wording, product area, or
        repository language.

        Keep the enum consistent with the score using the default mapping
        (1–3 → small, 4–7 → medium, 8–10 → large) unless you have a specific
        reason to diverge. If a change touches a shared interface field,
        prompt template string, or schema field, do not score below 4 —
        blast radius dominates conceptual simplicity.

        When verdict is ALREADY_DONE or BLOCKED, set `complexity: small`,
        `complexity_score: 2`, and `scope_exceeded: false` as placeholders.

        ## Work-Type Classification

        When verdict is PROCEED, also classify the work type from the story text alone:

        - **feature**: New capability or user-facing change — behavior that does not exist yet
        - **refactor**: Structural reorganization with no behavior change — moves, renames,
          extractions
        - **mechanical**: Rename, format, split, merge — zero judgment calls required
        - **bug**: Fix broken behavior or regression — restoring expected behavior

        When verdict is ALREADY_DONE or BLOCKED, set work_type to "feature" as a placeholder.

        ## Domain Classification

        When verdict is PROCEED, also tag the story with zero or more **domains**
        describing the *kind* of work it involves (what area of the codebase it
        stresses), distinct from work_type (which describes the shape of the
        change). Select tags ONLY from this fixed taxonomy — do not invent new
        tags; any tag outside this set is dropped:

{domain_taxonomy}

        Emit `domains: []` when no taxonomy tag clearly applies. Prefer a small,
        precise set (usually 1–3 tags) over speculative breadth — a tag should
        reflect a domain the story genuinely exercises, not one it merely
        mentions in passing. When verdict is ALREADY_DONE or BLOCKED, emit
        `domains: []`.

        ## Spec Sufficiency Classification

        When verdict is PROCEED, also assess whether this spec is
        **implementation-ready** or **needs planning**.

        The gating signal is **fix-shape decomposition**, NOT spec quality.
        Ask: "does the work require coordinated multi-step decomposition before
        a dev agent can begin?" — not "does the spec have a detailed Notes
        section?" A bounded single-area fix is implementation_ready even if
        the spec is terse; a cross-cutting refactor needs_planning even if
        the spec is verbose.

        `implementation_ready` is the **default** for bounded work. Classify
        as `implementation_ready` when the fix shape is localized — a dev
        agent can read the relevant code, make a single coordinated change,
        and verify the acceptance criteria without first decomposing the
        work into ordered steps. Typical signals:
        - The change is contained to one module, one function, or one small
          cluster of closely related call sites
        - The work is a diagnosed bug fix where the symptom and expected
          behavior already point at a single area
        - No interface, schema, or shared-prompt field is being renamed,
          removed, or migrated
        - Acceptance criteria describe **observable behaviors** that can be
          verified by reading code or running tests
        - Spec terseness alone is NOT a reason to escalate to needs_planning
          — bounded work may legitimately have short specs

        Classify as `needs_planning` ONLY when the work genuinely requires
        decomposition into ordered steps before dev can begin. Concrete
        triggers:
        - The fix spans multiple modules or coordinator phases that must
          change together (cross-module surgery, phase-handoff changes)
        - The story is a **contract change** — it removes, renames, or
          migrates a field or function name that is part of a shared
          interface (coordinator-agent output schema, prompt template field,
          review YAML field, config field). The blast radius (callers,
          parsers, prompt templates, fix prompts, exact-string test
          assertions) cannot be safely enumerated by a dev agent within its
          iteration budget.
        - The story modifies a **prompt template string or field name** that
          is likely referenced in exact-string test assertions
        - Concurrency, cancellation, or lifecycle propagation across module
          boundaries is in scope
        - Multiple plausible implementation approaches exist and the choice
          materially changes the blast radius (the plan agent must pick one)
        - Acceptance criteria are mutually contradictory or fundamentally
          ambiguous in a way the dev agent cannot resolve from the codebase
          (also add to spec_issues with type `ambiguity`)

        Do NOT escalate to needs_planning for any of these reasons alone:
        - Sparse or missing Notes section (Notes are advisory, not required)
        - Short spec body or terse acceptance criteria
        - The dev agent will need to read a few files to find exact line
          numbers (that is normal investigation, not decomposition)
        - The spec wording is generic or domain-vocabulary-heavy

        ## Output Format

        You MUST output ONLY a YAML block. No prose before or after.
        Start your response with ```yaml and end with ```.

        ```yaml
        verdict: PROCEED | ALREADY_DONE | BLOCKED
        complexity_score: <integer 1-10>
        scope_exceeded: true | false  # true only when complexity_score is 10
        complexity: small | medium | large
        work_type: feature | refactor | mechanical | bug
        domains: []  # or a list of tags from the fixed domain taxonomy
        contract_change: true | false
        reason: "<1-2 sentence explanation of your classification>"
        blocking_basis: <none | policy_assertion | missing_credentials |
          contradiction | missing_dependency | missing_fact>
        policy_assertions_cited:
          - text: "<the assertion, quoted as written in its source>"
            source: "<repo-relative path[:line] where you read it>"
            id: "<registry id if the source names one, else empty>"
            claimed_provenance: ratified | generated | unknown
            claimed_reference: "<ADR clause or operator decision the source names, else empty>"
        sufficiency: implementation_ready | needs_planning
        sufficiency_reason: "<1-2 sentence explanation of the sufficiency classification>"
        spec_issues:
          - type: contradiction | ambiguity | impossible_constraint
            description: "<what conflicts or is unclear>"
        warnings:
          - "<missing file path or other non-blocking advisory>"
        likely_files:
          - "<repo-relative file path likely to be edited if implementation proceeds>"
        criteria_checked:
          - criterion: "<acceptance criterion text>"
            files_checked:
              - "<repo-relative path[:line] inspected to check this criterion>"
            runtime_path: >-
              <path:line or function chain proving the live pipeline
              executes this behavior>
            satisfied: true | false
            evidence: >-
              <conclusion: how the cited runtime path turns the relevant
              input into the observable behavior/output, or what is missing>
        symptom_verification:
          status: verified_resolved | not_reproduced | not_feasible | not_attempted
          reproduces_now: true | false
          evidence: >-
            <what concrete inspection or reproduction step was performed
            against the current baseline to determine whether the
            originally observed symptom still fires>
        ```

        Use `spec_issues: []` if the spec is clean.
        Use `warnings: []` if there are no non-blocking advisories.
        Use `blocking_basis: none` and `policy_assertions_cited: []` for any verdict
        other than BLOCKED, and for a BLOCKED that cites no standing policy.
        Use `likely_files: []` when no likely edit targets can be identified.
        Include repo-relative paths only; do not invent nonexistent files just to fill the list.
        Always include `sufficiency` and `sufficiency_reason` when verdict is PROCEED.
        Set `contract_change: true` when the story explicitly changes an existing behavioral
        contract — a previously tested behavior is intentionally being altered (e.g., changing
        an error to an escalation, changing a return value, renaming a field). When true, the
        dev agent is permitted to update test files that assert the old behavior. Set
        `contract_change: false` (the default) for all other stories.

        The `files_checked` list is the evidence map: AC → files inspected → conclusion.
        A valid ALREADY_DONE verdict requires non-empty `files_checked` for every criterion.
        An empty `files_checked` means the criterion was not actually checked.
        A valid ALREADY_DONE verdict also requires a non-empty `runtime_path` for every
        criterion. `runtime_path` must cite the live orchestrator/coordinator/sprint call
        path that actually exercises the behavior, not a related helper that merely exists.

        ## Symptom Verification (bug stories)

        For **bug** stories, ALREADY_DONE means "the originally observed symptom no
        longer reproduces against the current baseline." Refuting a hypothesized
        cause in the body's Diagnosis section is NOT equivalent to confirming the
        defect is fixed — issue bodies routinely state a wrong theory of the
        defect alongside a real symptom, and a wrong theory does not entail a
        fixed symptom.

        Whenever you classify a bug story, emit a `symptom_verification` block:

        - `status: verified_resolved` — you reproduced or otherwise exercised the
          originally reported symptom against the configured baseline branch and
          confirmed the symptom no longer fires. Set `reproduces_now: false` and
          provide concrete `evidence` (what you ran, inspected, or executed).
        - `status: not_reproduced` — you attempted to verify but the originally
          reported behavior could not be exercised from preflight context (no
          test scenario, no reproduction harness, behavior depends on external
          state). Set `reproduces_now: false` only if you have reason to believe
          it does not — otherwise omit or set null.
        - `status: not_feasible` — symptom reproduction is not feasible from
          preflight (e.g., requires a live external service, multi-sprint state,
          quota-exhausted CLI). Use this when the symptom is real but a
          preflight-time verifier cannot exercise it.
        - `status: not_attempted` — you did not attempt symptom verification.

        Only `status: verified_resolved` with `reproduces_now: false` and concrete
        `evidence` justifies a bug ALREADY_DONE verdict. The other statuses
        will be downgraded by the coordinator to PROCEED so the dev cycle can
        verify symptom resolution.

        For non-bug stories, set `status: not_attempted` (or omit the block);
        AC-evidence checks via `criteria_checked` are sufficient.

        ## Rules

        - Check EVERY acceptance criterion individually. Do not shortcut.
        - "Related code exists" is NOT the same as "criterion is satisfied."
        - For ALREADY_DONE, every `criteria_checked` entry must cite the actual runtime
          path that executes the criterion on the configured pipeline. Naming a helper,
          config field, or nearby module is insufficient unless you prove the live path
          calls into it.
        - For ALREADY_DONE, every `evidence` field must explain observable behavior:
          input/state → executed runtime path → output/side effect that satisfies the
          criterion. Merely saying a symbol exists is insufficient.
        - Counter-example: "a helper module defines `assign_models()`" does NOT satisfy
          a routing acceptance criterion unless you also cite the coordinator or sprint
          runtime path that actually invokes it for this story's configured flow.
        - Evaluate ALREADY_DONE against the configured target baseline branch
          content from `config.workspace.base_branch`, not the resumed worktree contents.
        - If even ONE criterion is unsatisfied, the verdict cannot be ALREADY_DONE.
        - Ambiguity in a spec is not BLOCKED — it is needs_planning.
        - If the spec contains a **Notes** section, treat it as informal hints that
          may be stale or wrong. Notes are NOT requirements — do not BLOCK a spec
          because a Note references a nonexistent file or outdated pattern. Only
          acceptance criteria and explicit requirements can trigger BLOCKED.
        - You MUST output a verdict even if you are uncertain. Withholding output
          is not an option.
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

        risks = plan_content.get("risks")
        if risks:
            risk_lines = ["## Risks Stated By The Plan (from structured plan)", ""]
            for entry in risks:
                description = entry.get("description", "")
                mitigation = entry.get("mitigation", "")
                risk_lines.append(f"- **Risk:** {description}")
                mitigation_text = mitigation if mitigation else "(none stated)"
                risk_lines.append(f"  **Mitigation:** {mitigation_text}")
            criteria_mapping_section += "\n" + "\n".join(risk_lines) + "\n"
    else:
        plan_content_str = plan_content

    output_format_section = dedent("""\
        ## Output Format

        You MUST output ONLY a YAML block. No prose before or after.
        Start your response with ```yaml and end with ```.

        ```yaml
        verdict: APPROVE | REJECT
        summary: "<one-line summary of your review>"
        criteria_coverage:
          - criterion: "<acceptance criterion text from the spec>"
            covered: true | false
            plan_section: "<which part of the plan addresses this, or 'missing'>"
        findings:
          - severity: P0 | P1 | P1-impl | P2
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
        4. **Risk reconciliation** — if the plan states a "Risks" list (rendered
           above), you MUST resolve every stated risk against the plan's coverage
           before you can APPROVE. For each risk, determine one of:
           - **Retired**: the plan's steps or mitigation actually enumerate the
             affected surface, or add a test that would fail if the risk
             materialized. Cite where.
           - **Carried**: the risk is real and NOT resolved by the plan as
             written. You MUST report it as a `findings` entry (severity
             `P1-impl` unless it threatens an acceptance criterion, in which
             case `P1`) so the carried risk is visible in the review output —
             not silently dropped. A risk on a surface an acceptance criterion
             depends on (e.g. "some paths may not reach the code this story
             must fix") is a P1: the plan cannot be said to cover that
             criterion while the risk is unaddressed.
           - **Does not apply**: record in `summary` why the risk is moot for
             this plan.
           A stated risk with no mention anywhere in your output is not a
           valid review — every risk the plan names must show up as retired,
           carried, or explicitly dismissed.

        Do NOT evaluate: code style, plan verbosity, alternative approaches,
        or hypothetical edge cases that the dev agent can handle at implementation time.
        {output_format_section}
        ## Severity Definitions

        - **P0** (impossible): Plan cannot be implemented as written. Wrong API,
          hallucinated function, missing caller that would break at runtime.
        - **P1** (architectural blocker): Plan proposes an approach that is
          structurally wrong — wrong API, hallucinated function, missing callers,
          broken data flow. The plan itself needs a different approach.
        - **P1-impl** (implementation detail): Plan's approach is sound but doesn't
          pre-solve an implementation concern — edge case handling, specific guard
          clause, key collision strategy, monotonic counter details. The dev agent
          can and should resolve these during implementation. Does not block the plan.
        - **P2** (improvement): Plan could be more precise but dev can work it out.
          Does not block the plan.

        ## Rules

        - verdict MUST be APPROVE if there are zero P0 and zero P1 findings
          (P1-impl findings do NOT count as blockers)
        - verdict MUST be REJECT if any P0 or P1 finding exists
        - REJECT MUST include at least one P0 or P1 finding
        - A plan risk that names a coverage gap on a surface an acceptance
          criterion depends on, and that the plan does not retire (no
          enumeration, no test, no recorded "does not apply" judgement), MUST
          be reported as a P1 finding. You cannot APPROVE a plan while such a
          risk is unaddressed and unreported — an approval with an empty or
          risk-silent `findings` list is invalid if the plan's own risk list
          named an unresolved gap on AC-relevant surface.
        - A carried risk that does NOT threaten an acceptance criterion still
          MUST be reported (as `P1-impl` or `P2`) so it is visible in the
          approval rather than dropped.
        - **List ALL issues in a single pass.** Multiple findings in one REJECT
          is far better than discovering new issues across multiple cycles.
        - APPROVE with P1-impl and P2 suggestions is valid and encouraged
        - `findings[]` is only for current defects in the plan under review.
          Do NOT use it for closure notes such as "Previous rejection is fixed".
          If you want to acknowledge that a prior issue is now addressed, put it
          in `summary` or omit it entirely.
        - Be specific: cite the plan section, the actual codebase function/file,
          and why it would fail
        - A plan does not need to be perfect — it needs to not be wrong
    """)


def build_plan_prompt(
    task: TaskStory,
    *,
    story_content: str,
    preflight_output: str | None = None,
    partial_preflight_evidence: str | None = None,
    assembled_context: ContextPack | None = None,
    conventions: list[str] | None = None,
    work_type: str | None = None,
) -> str:
    """Build the planning agent prompt.

    The planning agent reads the story and produces a structured YAML plan.
    It does NOT write code.

    ``partial_preflight_evidence`` is an optional pre-rendered markdown block
    salvaged from a *failed* preflight run (issue #706) — files it already
    inspected, tool calls, any partial conclusion — surfaced so the planner
    can skip re-reading the same files. It is mutually informative with, and
    independent of, ``preflight_output`` (which is only present on success).

    Output is ONLY the plan document in YAML format.
    """
    preflight_section = ""
    if preflight_output:
        preflight_section = dedent(f"""\

            ## Preflight Analysis

            The preflight agent already analysed the codebase:

            {preflight_output}
        """)

    partial_evidence_section = ""
    if partial_preflight_evidence:
        partial_evidence_section = dedent(f"""\

            ## Partial Evidence from a Failed Preflight

            {partial_preflight_evidence}
        """)

    work_type_instruction = ""
    if work_type in ("refactor", "mechanical"):
        work_type_instruction = dedent("""\

            ## Plan Depth Constraint

            This story is classified as a **{work_type}** task. Produce a high-level file
            mapping only — do not specify implementation steps, import fixes, or line-level
            changes. The dev agent will discover those empirically.
        """).format(work_type=work_type)

    context_section = ""
    if assembled_context and assembled_context.content:
        context_section = dedent(f"""\

            ## Repository Context Pack

            Use this curated repository context before exploring additional files.

            {assembled_context.content}
        """)
    # Audit-only receipt on the injected prior-run claims (#2866). Empty unless
    # this pack actually carried claims, so a phase is never asked to account
    # for knowledge it did not receive.
    debrief_section = render_debrief_section(assembled_context, style=STYLE_YAML_BLOCK)

    return (
        dedent(f"""\
        You are a planning agent for **{task.name}**.

        ## Your Role

        You are NOT implementing anything. You are deciding the smallest viable
        path to satisfy every acceptance criterion in the spec. Your plan will
        be reviewed and then handed to a dev agent. Keep it short and decisive.

        ## Spec

        {story_content}
        {preflight_section}{partial_evidence_section}{context_section}{work_type_instruction}
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
                - path/to/example.file
              action: modify  # modify | create | delete
              details: "<concrete implementation details for this step>"
            - id: 2
              description: "<what this step does>"
              files:
                - path/to/other.file
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

        ## When the Story Is Too Large

        If — and only if — the story cannot be planned and implemented as a
        single coherent unit (it bundles multiple independent deliverables that
        each warrant their own plan and review cycle), do NOT force a plan.
        Instead, signal that the story must be split, using this schema:

        ```yaml
        plan:
          decompose: true
          decompose_reason: "<why too large; name the independent pieces it should split into>"
        ```

        When you signal `decompose: true`, omit `steps` — a step plan is not
        expected. The coordinator will escalate the story for a human to split
        it manually; it will NOT auto-split. Use this sparingly: most stories,
        even sizable ones, should be planned normally. Reserve the signal for
        stories that genuinely bundle separable concerns.

        ## Rules

        - Do NOT write code. Do NOT modify files.
        - Do NOT set `decompose: true` merely because the story is non-trivial.
          Only use it when the story bundles independent deliverables that fight
          each other in one implementation cycle.
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
        + render_conventions_block(conventions)
        + debrief_section
    )
