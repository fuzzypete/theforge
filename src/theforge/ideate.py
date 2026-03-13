"""Multi-LLM deliberation for spec generation.

Runs a structured deliberation protocol across a model pool to produce a
synthesized spec from an ambiguous brief. The protocol has three phases:

  Phase 1: Independent generation — each model produces ideas independently.
  Phase 2: Cross-review — each model critiques all Phase 1 outputs.
  Phase 3: Synthesis — a synthesis model consolidates to a draft spec.

The coordinator remains fully deterministic. Only the ideation agents are LLMs.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import ForgeConfig
from .runner import AgentResult, run_agent, run_agent_pool

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
) -> str:
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
---
name: "<derived from brief>"
slug: "<kebab-case slug>"
file_scope: []
pytest_target: tests/
---

# <title>

## Problem
<problem statement derived from brief and deliberation>

## Requirements
<requirements derived from converged items>

## Acceptance Criteria
- [ ] <criterion>

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
            line.lstrip("- ").strip()
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
            line.lstrip("- ").strip()
            for line in section.splitlines()
            if line.strip().startswith("-")
        ]

    # Extract SPEC section (from "SPEC:\n" onward, or from "---" frontmatter)
    if "SPEC:" in synthesis_text:
        spec_start = synthesis_text.index("SPEC:") + len("SPEC:")
        spec_text = synthesis_text[spec_start:].strip()
    elif synthesis_text.strip().startswith("---"):
        spec_text = synthesis_text.strip()

    return converged, divergent, spec_text


def _validate_frontmatter(spec_text: str) -> bool:
    """Return True if spec_text has parseable YAML frontmatter."""
    if not spec_text.strip().startswith("---"):
        return False
    end = spec_text.find("---", 3)
    if end == -1:
        return False
    frontmatter = spec_text[3:end].strip()
    try:
        result = yaml.safe_load(frontmatter)
        return isinstance(result, dict)
    except yaml.YAMLError:
        return False


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
    max_rounds: int = 2,
) -> IdeationResult:
    """Execute the full deliberation protocol.

    Phases:
      1. Fan out phase1 prompt to all models in review_pool independently.
      2. Fan out phase2 prompt (includes all phase1 outputs) to all models.
         (Skipped for single-model pool — Phase 1 output goes directly to result.)
      3. Run synthesis model to consolidate into a draft spec.
         (Skipped for single-model pool — Phase 1 output is the spec.)
      4. If divergent items remain and rounds remain, loop with narrowed brief.

    Single-model pool: Phase 1 only, no cross-review or synthesis.
    Pool > 1 without synthesis_profile: raises ValueError.
    """
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

        # ── Phase 1: Independent generation ──────────────────────────
        _log("  ▸ Phase 1   generating independently...")
        phase1_start = time.monotonic()
        phase1_prompt = _build_phase1_prompt(current_brief)

        phase1_results: list[AgentResult] = run_agent_pool(
            prompt=phase1_prompt,
            profiles=pool,
            working_dir=working_dir,
        )

        phase1_outputs: dict[str, str] = {}
        for profile, result in zip(pool, phase1_results):
            elapsed = time.monotonic() - phase1_start
            _log(f"  ↳ {profile.name} done ({elapsed:.0f}s)")
            if not result.success:
                _log(f"  ✗ {profile.name} failed: {result.output[:120]}")
                return _failed_result(
                    f"Phase 1 agent {profile.name!r} failed: {result.output}",
                    all_rounds,
                    total_cost + result.cost_usd,
                )
            phase1_outputs[profile.name] = result.output
            total_cost += result.cost_usd

        # ── Single-model fast path: no cross-review or synthesis ─────
        if len(pool) == 1:
            # Phase 1 output IS the spec for single-model pools
            spec_text = list(phase1_outputs.values())[0]
            round_result = IdeationRound(
                round_number=round_num,
                phase1_outputs=phase1_outputs,
                phase2_outputs={},
                converged_items=[],
                divergent_items=[],
                synthesis_output=spec_text,
            )
            all_rounds.append(round_result)
            final_synthesis = spec_text
            residual_divergence = []
            break

        # ── Phase 2: Cross-review ────────────────────────────────────
        phase2_outputs: dict[str, str] = {}

        _log("  ▸ Phase 2   cross-reviewing...")
        phase2_start = time.monotonic()
        phase2_prompt = _build_phase2_prompt(current_brief, phase1_outputs)

        phase2_results: list[AgentResult] = run_agent_pool(
            prompt=phase2_prompt,
            profiles=pool,
            working_dir=working_dir,
        )

        for profile, result in zip(pool, phase2_results):
            elapsed = time.monotonic() - phase2_start
            _log(f"  ↳ {profile.name} done ({elapsed:.0f}s)")
            if not result.success:
                _log(f"  ✗ {profile.name} failed: {result.output[:120]}")
                return _failed_result(
                    f"Phase 2 agent {profile.name!r} failed: {result.output}",
                    all_rounds,
                    total_cost + result.cost_usd,
                )
            phase2_outputs[profile.name] = result.output
            total_cost += result.cost_usd

        # ── Phase 3: Synthesis ────────────────────────────────────────
        _log("  ▸ Synthesis   consolidating...")
        synth_start = time.monotonic()

        assert synthesis_profile is not None  # guaranteed by check at top for len > 1
        synth_prompt = _build_synthesis_prompt(current_brief, phase1_outputs, phase2_outputs)

        synth_result = run_agent(
            prompt=synth_prompt,
            profile=synthesis_profile,
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

        # Continue if divergence remains and rounds remain
        if divergent_items and round_num < max_rounds:
            divergence_text = "\n".join(f"- {item}" for item in divergent_items)
            current_brief = (
                f"{brief}\n\n"
                f"FOCUS: The following items remain in disagreement from the previous round. "
                f"Resolve these specifically:\n{divergence_text}"
            )
        else:
            break

    # ── Output ───────────────────────────────────────────────────────
    # Append Human Decisions Required section if residual divergence
    if residual_divergence and "## Human Decisions Required" not in final_synthesis:
        decisions = "\n".join(f"- {item}" for item in residual_divergence)
        final_synthesis = (
            final_synthesis.rstrip() + f"\n\n## Human Decisions Required\n{decisions}\n"
        )

    human_decision_required = bool(residual_divergence)

    # Write spec file
    written_path: Path | None = None
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(final_synthesis, encoding="utf-8")
        written_path = output_path

    # Log final result
    elapsed_total = time.monotonic() - ideation_start
    mins, secs = divmod(int(elapsed_total), 60)
    dur_str = f"{mins}m {secs:02d}s" if mins else f"{secs}s"
    spec_label = str(written_path) if written_path else "(dry run)"
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
