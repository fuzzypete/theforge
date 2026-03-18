"""Multi-LLM deliberation for spec generation.

Runs a structured deliberation protocol across a model pool to produce a
synthesized spec from an ambiguous brief. The protocol has three phases:

  Phase 1: Independent generation — each model produces ideas independently.
  Phase 2: Cross-review — each model critiques all Phase 1 outputs.
  Phase 3: Synthesis — a synthesis model consolidates to a draft spec.

The coordinator remains fully deterministic. Only the ideation agents are LLMs.
"""

from __future__ import annotations

import re
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import ForgeConfig
from .runner import run_agent, run_agent_pool

# ── Constants ────────────────────────────────────────────────────────

_SPEC_LINE_LIMIT = 150

# ── Data structures ──────────────────────────────────────────────────


@dataclass(frozen=True)
class IdeationRound:
    """One full Phase 1 + Phase 2 cycle."""

    round_number: int
    phase1_outputs: dict[str, str]  # model_name → output
    phase2_outputs: dict[str, str]  # model_name → cross-review output
    converged_items: list[str]  # items where N/M models agreed
    divergent_items: list[str]  # items still in disagreement
    synthesis_output: str  # full synthesis text


@dataclass
class IdeationResult:
    success: bool
    spec_path: Path | None  # written spec file, or None if human-decision needed
    rounds: list[IdeationRound]
    final_synthesis: str  # final synthesized spec text
    residual_divergence: list[str]  # items needing human executive decision
    total_cost_usd: float
    human_decision_required: bool


# ── Prompt builders ──────────────────────────────────────────────────


def _extract_pytest_target(config: "ForgeConfig | None") -> str:
    """Derive pytest target path from config gate_command, defaulting to 'tests/'.

    This is a best-effort heuristic. Gate commands like 'make test' or 'make gate'
    silently fall back to 'tests/'. The '-m' token can also match ambiguously
    (e.g. 'python -m pytest tests/'). The spec hardcodes 'pytest_target: tests/'
    but this heuristic reflects the actual project configuration when it can be
    inferred from the gate command.
    """
    if config is None:
        return "tests/"
    gate_cmd = config.validation.gate_command
    try:
        tokens = shlex.split(gate_cmd)
        for i, tok in enumerate(tokens):
            if tok in ("pytest", "python", "-m") and i + 1 < len(tokens):
                candidate = tokens[i + 1]
                if candidate.startswith("tests") and not candidate.startswith("-"):
                    return candidate
    except ValueError:
        pass
    return "tests/"


def _build_phase1_prompt(brief: str) -> str:
    return f"""You are participating in a structured ideation process.

BRIEF:
{brief}

Your task: Independently produce your best thinking on this brief.
Do NOT hedge or hold back ideas because another model might disagree.

Structure your response as:
## Core Ideas
[3-7 concrete ideas or approaches]

## Key Constraints
[constraints the solution must satisfy]

## Risks and Blind Spots
[what could go wrong, what this approach might miss]

## Recommended Approach
[your single strongest recommendation]"""


def _build_single_model_prompt(brief: str, *, config: "ForgeConfig | None" = None) -> str:
    """Combined ideation + spec-writing prompt for single-model pools.

    Multi-model pools use Phase 1 (ideas) → Phase 2 (cross-review) → Phase 3
    (synthesis). For a single-model pool there is no cross-review and no
    separate synthesis step — one model produces the full spec directly.
    """
    pytest_target = _extract_pytest_target(config)

    return f"""You are writing a structured implementation spec.

BRIEF:
{brief}

Think through the brief, then produce a spec in the exact format below.

The spec describes WHAT to build and WHY — not HOW. A separate plan phase
derives the implementation from the codebase. Acceptance criteria must describe
observable behavior that a human or automated test can verify from outside the
system.

Prohibited in the spec output:
- Function signatures or method names
- Dataclass, class, or type definitions
- Code snippets or pseudocode
- File-internal implementation steps (e.g. "in foo.py, add a field")
- Specific variable or parameter names

Keep the spec body under 150 lines.

SPEC:
---
name: "<derived from brief>"
slug: "<kebab-case slug>"
file_scope:
  - <primary files this change touches>
pytest_target: {pytest_target}
---

# <Spec Title>

## Problem
<What is broken or missing>

## Requirements
<Numbered list of requirements>

## Acceptance Criteria
- [ ] <criterion describing observable behavior>

## Out of Scope
<What this spec does NOT address>

## Human Decisions Required
<Any design choices left to the human, or "None">
"""


def _build_phase2_prompt(brief: str, phase1_outputs: dict[str, str]) -> str:
    outputs_section = "\n\n".join(
        f"### {model_name}\n{output}" for model_name, output in phase1_outputs.items()
    )
    return f"""You are participating in a structured ideation process.

BRIEF:
{brief}

## Phase 1 Outputs (from all models)

{outputs_section}

Your task: Cross-review the above outputs.

Structure your response as:
## Agreements
[items where you and other models converged]

## Disagreements
[items where models genuinely differed — describe the tension]

## Blind Spots Identified
[things no model addressed that should have been]

## Your Updated Position
[how (if at all) seeing other models' outputs changed your thinking]"""


def _build_synthesis_prompt(
    brief: str,
    phase1_outputs: dict[str, str],
    phase2_outputs: dict[str, str],
    *,
    config: "ForgeConfig | None" = None,
) -> str:
    pytest_target = _extract_pytest_target(config)

    phase1_section = "\n\n".join(
        f"### {model_name} (Phase 1)\n{output}" for model_name, output in phase1_outputs.items()
    )
    phase2_section = "\n\n".join(
        f"### {model_name} (Phase 2 cross-review)\n{output}"
        for model_name, output in phase2_outputs.items()
    )
    return f"""You are synthesizing a multi-model deliberation into a structured spec.

BRIEF:
{brief}

## Phase 1 Outputs

{phase1_section}

## Phase 2 Cross-Reviews

{phase2_section}

Your task: Synthesize all of the above into a draft spec.

First, assess convergence:
- For each idea or constraint that appeared in Phase 1 outputs, assess whether
  it was agreed by 2 or more models. List converged items as bullet points under
  CONVERGED_ITEMS. List items where models genuinely disagreed under DIVERGENT_ITEMS.
  Be conservative: if in doubt, mark as divergent.

Then produce the draft spec in this exact format:

CONVERGED_ITEMS:
- <item>

DIVERGENT_ITEMS:
- <item>

SPEC:

The spec describes WHAT to build and WHY — not HOW. A separate plan phase
derives the implementation from the codebase. Acceptance criteria must describe
observable behavior that a human or automated test can verify from outside the
system.

Prohibited in the spec output:
- Function signatures or method names
- Dataclass, class, or type definitions
- Code snippets or pseudocode
- File-internal implementation steps (e.g. "in foo.py, add a field")
- Specific variable or parameter names

Keep the spec body under 150 lines.

---
name: "<derived from brief>"
slug: "<kebab-case slug>"
file_scope: []
pytest_target: {pytest_target}
---

# <title>

## Problem
<problem statement derived from brief and deliberation>

## Requirements
<requirements derived from converged items>

## Acceptance Criteria
- [ ] <criterion describing observable behavior>

## Human Decisions Required
<list any items from DIVERGENT_ITEMS that remained unresolved; omit section if empty>"""


# ── Synthesis output parsing ─────────────────────────────────────────


def _parse_synthesis_output(
    synthesis_text: str,
) -> tuple[list[str], list[str], str]:
    """Parse synthesis output into (converged_items, divergent_items, spec_text).

    Returns ([], [], synthesis_text) on parse failure so callers get a usable spec.
    """
    converged: list[str] = []
    divergent: list[str] = []
    spec_text = synthesis_text

    # Extract CONVERGED_ITEMS section
    if "CONVERGED_ITEMS:" in synthesis_text:
        start = synthesis_text.index("CONVERGED_ITEMS:") + len("CONVERGED_ITEMS:")
        end = synthesis_text.find("\n\n", start)
        section = (
            synthesis_text[start:end].strip() if end != -1 else synthesis_text[start:].strip()
        )
        converged = [
            line.strip().removeprefix("- ")
            for line in section.splitlines()
            if line.strip().startswith("-")
        ]

    # Extract DIVERGENT_ITEMS section
    if "DIVERGENT_ITEMS:" in synthesis_text:
        start = synthesis_text.index("DIVERGENT_ITEMS:") + len("DIVERGENT_ITEMS:")
        end = synthesis_text.find("\n\n", start)
        section = (
            synthesis_text[start:end].strip() if end != -1 else synthesis_text[start:].strip()
        )
        divergent = [
            line.strip().removeprefix("- ")
            for line in section.splitlines()
            if line.strip().startswith("-")
        ]

    # Extract SPEC section (from "SPEC:\n" onward, or from "---" frontmatter)
    if "SPEC:" in synthesis_text:
        spec_start = synthesis_text.index("SPEC:") + len("SPEC:")
        spec_text = synthesis_text[spec_start:].strip()
    elif synthesis_text.strip().startswith("---"):
        spec_text = synthesis_text.strip()

    # Strip markdown code fences that models sometimes wrap the spec in
    if spec_text.startswith("```"):
        first_newline = spec_text.find("\n")
        if first_newline != -1:
            spec_text = spec_text[first_newline + 1 :]
    if spec_text.endswith("```"):
        spec_text = spec_text[: spec_text.rfind("```")].rstrip()

    return converged, divergent, spec_text


def _validate_frontmatter(spec_text: str) -> bool:
    """Return True if spec_text has parseable YAML frontmatter and a non-empty body."""
    stripped = spec_text.strip()
    if not stripped.startswith("---"):
        return False
    end = stripped.find("---", 3)
    if end == -1:
        return False
    frontmatter = stripped[3:end].strip()
    try:
        result = yaml.safe_load(frontmatter)
        if not isinstance(result, dict):
            return False
    except yaml.YAMLError:
        return False
    # Require a non-empty markdown body after the closing ---
    body = stripped[end + 3 :].strip()
    return len(body) > 0


def _extract_slug_from_spec(spec_text: str) -> str | None:
    """Extract slug from spec frontmatter, or None if not found."""
    if not spec_text.strip().startswith("---"):
        return None
    end = spec_text.find("---", 3)
    if end == -1:
        return None
    frontmatter = spec_text[3:end].strip()
    try:
        data = yaml.safe_load(frontmatter)
        if isinstance(data, dict):
            return data.get("slug")
    except yaml.YAMLError:
        pass
    return None


def _has_prohibited_content(spec_text: str) -> tuple[bool, str]:
    """Return (True, reason) if the spec body contains prohibited implementation detail.

    Scans only the markdown body (after the closing --- of frontmatter) for:
    - Fenced code blocks (lines starting with ```)
    - Python/JS function definitions (def name()
    - Class or dataclass definitions
    """
    # Strip frontmatter; scan only the body.
    body = spec_text
    stripped = spec_text.strip()
    if stripped.startswith("---"):
        end = stripped.find("---", 3)
        if end != -1:
            body = stripped[end + 3 :]

    # Regex for bare typed signatures: "name(args) -> Type" or "name(args):"
    # Requires no space between name and '(' to avoid matching prose like
    # "Background (current state)" or "Context (current state):".
    _sig_re = re.compile(r"^\w[\w.]*\(.*\)\s*(->.*)?:?\s*$")

    for line in body.splitlines():
        s = line.strip()
        if s.startswith("```"):
            return True, "fenced code block"
        if s.startswith("def ") and "(" in s:
            return True, "function definition"
        if s.startswith("function ") and "(" in s:
            return True, "JS function definition"
        if s.startswith("class ") and (":" in s or "(" in s):
            return True, "class definition"
        if s == "@dataclass" or s.startswith("@dataclass("):
            return True, "@dataclass decorator"
        if _sig_re.match(s) and len(s) > 10:
            return True, "bare function signature"

    return False, ""


# ── Core orchestration ───────────────────────────────────────────────


def _log(msg: str) -> None:
    print(f"[forge] {msg}", file=sys.stderr, flush=True)


def _failed_result(
    msg: str,
    rounds: list[IdeationRound],
    total_cost: float,
) -> IdeationResult:
    """Return a failed IdeationResult with an error message in final_synthesis."""
    return IdeationResult(
        success=False,
        spec_path=None,
        rounds=rounds,
        final_synthesis=msg,
        residual_divergence=[],
        total_cost_usd=total_cost,
        human_decision_required=False,
    )


def run_ideation(
    config: ForgeConfig,
    brief: str,
    output_path: Path | None,
    *,
    specs_dir: Path | None = None,
    max_rounds: int = 2,
) -> IdeationResult:
    """Execute the full deliberation protocol.

    Phases:
      1. Fan out phase1 prompt to all models in review_pool independently.
      2. Fan out phase2 prompt (includes all phase1 outputs) to all models.
         (Skipped for single-model pool — no cross-review for a single model.)
      3. Run synthesis model to consolidate Phase 1 (and Phase 2) into a draft spec.
         Single-model pool: lone model acts as synthesizer (Phase 1 + synthesis,
         no cross-review).
      4. If divergent items remain and rounds remain, loop with narrowed brief.

    Args:
        config: ForgeConfig with review pool and synthesis profile.
        brief: The ideation brief (text).
        output_path: Where to write the generated spec. Pass None to skip writing
            (caller handles output, or use specs_dir for auto-naming).
        specs_dir: When output_path is None and specs_dir is provided, the spec
            is written to specs_dir/<slug>.md after synthesis (slug derived from
            the synthesized frontmatter). Ignored when output_path is given.
        max_rounds: Maximum deliberation rounds before surfacing residual divergence.
            Must be >= 1; values < 1 are clamped to 1.

    Pool > 1 without synthesis_profile: raises ValueError.
    """
    if max_rounds < 1:
        max_rounds = 1

    pool = config.review_pool
    synthesis_profile = config.synthesis_profile

    if len(pool) > 1 and synthesis_profile is None:
        raise ValueError(
            "synthesis profile is required when review_pool has more than 1 entry. "
            "Configure profiles.synthesis in forge.yaml."
        )

    pool_names = "+".join(p.name for p in pool)
    working_dir = config.project_root
    ideation_start = time.monotonic()

    all_rounds: list[IdeationRound] = []
    total_cost = 0.0
    current_brief = brief
    residual_divergence: list[str] = []
    final_synthesis = ""

    for round_num in range(1, max_rounds + 1):
        divergence_suffix = (
            f"   ({len(residual_divergence)} divergent items remain)"
            if residual_divergence
            else ""
        )
        _log(f"▸ IDEATE   {pool_names}  round={round_num}{divergence_suffix}")

        # ── Single-model fast path ────────────────────────────────────
        # Intentional design deviation from the spec's "Phase 1 only" wording:
        # The spec says single-model runs use "Phase 1 only", but Phase 1's
        # prompt (ideas/constraints/risks) deliberately avoids frontmatter and
        # cannot produce a valid spec file. Instead we use a combined prompt
        # (_build_single_model_prompt) that merges ideation and spec-writing
        # into one call. This achieves the spec's intent (AC7: "single-model
        # pool produces a spec") via a better mechanism. Cross-review and
        # synthesis are still skipped — only one LLM call is made.
        if len(pool) == 1:
            _log("  ▸ Phase 1   generating spec (single-model)...")
            single_prompt = _build_single_model_prompt(current_brief, config=config)
            agent_start = time.monotonic()
            result = run_agent(prompt=single_prompt, profile=pool[0], working_dir=working_dir)
            agent_elapsed = time.monotonic() - agent_start
            total_cost += result.cost_usd
            _log(f"  ↳ {pool[0].name} done ({agent_elapsed:.0f}s)")
            if not result.success:
                _log(f"  ✗ {pool[0].name} failed: {result.output[:120]}")
                return _failed_result(
                    f"Single-model agent failed: {result.output}",
                    all_rounds,
                    total_cost,
                )
            spec_text = result.output
            # Extract the SPEC: block if the model wrapped it.
            if "SPEC:" in spec_text:
                spec_text = spec_text.split("SPEC:", 1)[1].strip()
            if not _validate_frontmatter(spec_text):
                _log("  ✗ single-model output missing valid frontmatter")
                return _failed_result(
                    "Single-model output does not contain valid YAML frontmatter.",
                    all_rounds,
                    total_cost,
                )
            all_rounds.append(
                IdeationRound(
                    round_number=round_num,
                    phase1_outputs={pool[0].name: result.output},
                    phase2_outputs={},
                    converged_items=[],
                    divergent_items=[],
                    synthesis_output=spec_text,
                )
            )
            final_synthesis = spec_text
            # Parse any Human Decisions Required section the model emitted.
            # Forcing [] would silently drop legitimate unresolved items.
            _hdr = "## Human Decisions Required"
            if _hdr in spec_text:
                _section_start = spec_text.index(_hdr) + len(_hdr)
                _section_text = spec_text[_section_start:].strip()
                residual_divergence = [
                    line.strip().removeprefix("- ")
                    for line in _section_text.splitlines()
                    if line.strip().startswith("-")
                ]
            else:
                residual_divergence = []
            break  # single round always produces the final spec

        # ── Phase 1: Independent generation (multi-model) ────────────
        _log("  ▸ Phase 1   generating independently...")
        phase1_prompt = _build_phase1_prompt(current_brief)
        phase1_outputs: dict[str, str] = {}

        p1_start = time.monotonic()
        p1_results = run_agent_pool(prompt=phase1_prompt, profiles=pool, working_dir=working_dir)
        p1_elapsed = time.monotonic() - p1_start
        # Accumulate all pool costs before checking failures so partial runs
        # are fully accounted for in total_cost_usd.
        for r in p1_results:
            total_cost += r.cost_usd
        failed_p1 = next(
            ((profile, r) for profile, r in zip(pool, p1_results) if not r.success), None
        )
        if failed_p1 is not None:
            profile, r = failed_p1
            _log(f"  ✗ {profile.name} failed: {r.output[:120]}")
            return _failed_result(
                f"Phase 1 agent {profile.name!r} failed: {r.output}",
                all_rounds,
                total_cost,
            )
        for profile, result in zip(pool, p1_results):
            phase1_outputs[profile.name] = result.output
            _log(f"  ↳ {profile.name} done")
        _log(f"  Phase 1 total: {p1_elapsed:.0f}s")

        # ── Phase 2: Cross-review ─────────────────────────────────────
        phase2_outputs: dict[str, str] = {}
        _log("  ▸ Phase 2   cross-reviewing...")
        phase2_prompt = _build_phase2_prompt(current_brief, phase1_outputs)

        p2_start = time.monotonic()
        p2_results = run_agent_pool(prompt=phase2_prompt, profiles=pool, working_dir=working_dir)
        p2_elapsed = time.monotonic() - p2_start
        # Accumulate all pool costs before checking failures.
        for r in p2_results:
            total_cost += r.cost_usd
        failed_p2 = next(
            ((profile, r) for profile, r in zip(pool, p2_results) if not r.success), None
        )
        if failed_p2 is not None:
            profile, r = failed_p2
            _log(f"  ✗ {profile.name} failed: {r.output[:120]}")
            return _failed_result(
                f"Phase 2 agent {profile.name!r} failed: {r.output}",
                all_rounds,
                total_cost,
            )
        for profile, result in zip(pool, p2_results):
            phase2_outputs[profile.name] = result.output
            _log(f"  ↳ {profile.name} done")
        _log(f"  Phase 2 total: {p2_elapsed:.0f}s")

        # ── Phase 3: Synthesis ────────────────────────────────────────
        _log("  ▸ Synthesis   consolidating...")
        synth_start = time.monotonic()

        assert synthesis_profile is not None  # guaranteed by ValueError check above
        synth_profile = synthesis_profile
        synth_prompt = _build_synthesis_prompt(
            current_brief, phase1_outputs, phase2_outputs, config=config
        )

        synth_result = run_agent(
            prompt=synth_prompt,
            profile=synth_profile,
            working_dir=working_dir,
        )
        synth_elapsed = time.monotonic() - synth_start
        _log(f"  ↳ synthesis done ({synth_elapsed:.0f}s)")
        total_cost += synth_result.cost_usd

        if not synth_result.success:
            _log(f"  ✗ synthesis failed: {synth_result.output[:120]}")
            return _failed_result(
                f"Synthesis agent failed: {synth_result.output}",
                all_rounds,
                total_cost,
            )

        # ── Parse synthesis output ────────────────────────────────────
        converged_items, divergent_items, spec_text = _parse_synthesis_output(synth_result.output)

        if not _validate_frontmatter(spec_text):
            _log("  ✗ synthesis produced invalid frontmatter")
            return _failed_result(
                f"Synthesis output does not contain valid YAML frontmatter. "
                f"Raw output:\n{synth_result.output}",
                all_rounds,
                total_cost,
            )

        final_synthesis = spec_text

        _log(f"  Converged: {len(converged_items)} items  Divergent: {len(divergent_items)} items")

        round_result = IdeationRound(
            round_number=round_num,
            phase1_outputs=phase1_outputs,
            phase2_outputs=phase2_outputs,
            converged_items=converged_items,
            divergent_items=divergent_items,
            synthesis_output=synth_result.output,
        )
        all_rounds.append(round_result)

        residual_divergence = divergent_items

        # Continue if divergence remains and rounds remain.
        # The follow-up brief is narrowed to ONLY the divergent items — the original
        # brief is intentionally excluded so already-converged topics are not reopened.
        if divergent_items and round_num < max_rounds:
            divergence_text = "\n".join(f"- {item}" for item in divergent_items)
            current_brief = (
                f"The following design questions remain unresolved from a prior "
                f"deliberation round. Focus exclusively on resolving these items:\n\n"
                f"{divergence_text}"
            )
        else:
            break

    # ── Output ───────────────────────────────────────────────────────
    # Unconditionally normalize the ## Human Decisions Required section.
    # The synthesis model may have emitted a partial, empty, or malformed
    # section. We strip any existing section and rewrite it from the
    # authoritative residual_divergence list so the written spec always
    # reflects the actual unresolved items.
    hdr = "## Human Decisions Required"
    # Strip any existing section (and everything after it) then reappend.
    if hdr in final_synthesis:
        final_synthesis = final_synthesis[: final_synthesis.index(hdr)].rstrip()

    if residual_divergence:
        decisions = "\n".join(f"- {item}" for item in residual_divergence)
        final_synthesis = final_synthesis + f"\n\n{hdr}\n{decisions}\n"

    human_decision_required = bool(residual_divergence)

    # ── Prohibited-content enforcement ───────────────────────────────
    # Deterministically reject specs containing fenced code blocks,
    # function definitions, or class/dataclass definitions.
    has_prohibited, prohibited_reason = _has_prohibited_content(final_synthesis)
    if has_prohibited:
        _log(f"✗ IDEATE   spec contains prohibited content: {prohibited_reason}")
        return _failed_result(
            f"Spec contains prohibited implementation detail ({prohibited_reason}). "
            f"Regenerate without code blocks, function signatures, or class definitions.",
            all_rounds,
            total_cost,
        )

    # ── Line-count enforcement ────────────────────────────────────────
    # Deterministically reject specs that exceed the line limit.
    # The prompt instructs the model to stay under _SPEC_LINE_LIMIT lines;
    # this check ensures overlong responses never reach the output file.
    spec_line_count = len(final_synthesis.splitlines())
    if spec_line_count > _SPEC_LINE_LIMIT:
        _log(f"✗ IDEATE   spec exceeds {_SPEC_LINE_LIMIT}-line limit ({spec_line_count} lines)")
        return _failed_result(
            f"Spec exceeds {_SPEC_LINE_LIMIT}-line limit ({spec_line_count} lines). "
            f"Revise the brief to reduce scope.",
            all_rounds,
            total_cost,
        )

    # Resolve output path: explicit path takes precedence; fall back to specs_dir + slug.
    written_path: Path | None = None
    if output_path is not None:
        written_path = output_path
    elif specs_dir is not None:
        slug = _extract_slug_from_spec(final_synthesis) or "ideated-spec"
        written_path = specs_dir / f"{slug}.md"

    # Write spec file
    if written_path is not None:
        written_path.parent.mkdir(parents=True, exist_ok=True)
        written_path.write_text(final_synthesis, encoding="utf-8")

    # Log final result
    elapsed_total = time.monotonic() - ideation_start
    mins, secs = divmod(int(elapsed_total), 60)
    dur_str = f"{mins}m {secs:02d}s" if mins else f"{secs}s"
    spec_label = str(written_path) if written_path else "(no output)"
    _log(f"✓ IDEATE   spec written: {spec_label}  ${total_cost:.2f}  {dur_str}")
    if human_decision_required:
        _log(
            f"⚠ {len(residual_divergence)} item(s) require human decision "
            f"— see ## Human Decisions Required"
        )

    return IdeationResult(
        success=True,
        spec_path=written_path,
        rounds=all_rounds,
        final_synthesis=final_synthesis,
        residual_divergence=residual_divergence,
        total_cost_usd=total_cost,
        human_decision_required=human_decision_required,
    )


# ── Audit generation ─────────────────────────────────────────────────


def generate_ideation_audit(
    config: ForgeConfig,
    brief: str,
    result: IdeationResult,
    *,
    started_at: str | None = None,
    finished_at: str | None = None,
    duration_seconds: float | None = None,
) -> dict:
    """Return a serialisable audit record for an ideation run.

    The returned dict can be written directly to YAML and read back by
    ``forge audit``. The ``type: ideate`` field lets ``cmd_audit`` detect
    and display ideation-specific sections instead of the coordinator fields.
    """
    rounds_data = [
        {
            "round_number": r.round_number,
            "converged_count": len(r.converged_items),
            "divergent_count": len(r.divergent_items),
            "converged_items": r.converged_items,
            "divergent_items": r.divergent_items,
        }
        for r in result.rounds
    ]
    return {
        "type": "ideate",
        "brief": brief[:500],  # truncate very long briefs for readability
        "spec_path": str(result.spec_path) if result.spec_path else None,
        "success": result.success,
        "human_decision_required": result.human_decision_required,
        "residual_divergence": result.residual_divergence,
        "rounds": rounds_data,
        "cost": {
            "total_usd": result.total_cost_usd,
        },
        "timing": {
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": duration_seconds,
        },
        "model_pool": [p.name for p in config.review_pool],
        "synthesis_profile": (config.synthesis_profile.name if config.synthesis_profile else None),
    }
