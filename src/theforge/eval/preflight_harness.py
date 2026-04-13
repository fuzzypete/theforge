"""Preflight model evaluation harness.

Replays the preflight prompt against each story in the golden set using one or
more candidate ModelProfiles. Collects per-run metrics for comparison reporting.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from theforge.config.types import ForgeConfig, ModelProfile
    from theforge.eval.golden_set import GoldenStory

_VALID_PREFLIGHT_VERDICTS = frozenset({"PROCEED", "ALREADY_DONE", "BLOCKED"})


def _output_has_valid_yaml(output: str) -> bool:
    """Return True only when the output contains a parseable YAML dict with a
    recognized verdict field — i.e., the agent produced genuine structured output,
    not just text that the fallback parser rescued as BLOCKED.
    """
    yaml_text = output
    if "```yaml" in output:
        start = output.index("```yaml") + len("```yaml")
        end = output.find("```", start)
        if end == -1:
            return False
        yaml_text = output[start:end]
    elif "```" in output:
        start = output.index("```") + len("```")
        end = output.find("```", start)
        if end == -1:
            return False
        yaml_text = output[start:end]

    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return False

    if not isinstance(parsed, dict):
        return False

    verdict = str(parsed.get("verdict", "")).upper()
    return verdict in _VALID_PREFLIGHT_VERDICTS


# Heuristic: output mentions a file path (src/ subtree or filename:lineno pattern).
_FILE_REF_RE = re.compile(
    r"(?:[\w/]+\.py)"  # any .py path
    r"|"
    r"(?:[\w/]+\.[a-z]{1,4}:\d+)"  # file:lineno pattern
)

# ── Lazy function slots ──────────────────────────────────────────────────────
# None until first call; tests may replace before calling run_preflight_for_model.
# Patch targets:
#   theforge.eval.preflight_harness.run_agent             — agent invocation
#   theforge.eval.preflight_harness.build_preflight_prompt — prompt construction
run_agent = None
build_preflight_prompt = None


def _ensure_deps() -> None:
    global run_agent, build_preflight_prompt
    if run_agent is None:
        from theforge.runners import run_agent as _ra  # noqa: PLC0415

        run_agent = _ra
    if build_preflight_prompt is None:
        from theforge.task import build_preflight_prompt as _bp  # noqa: PLC0415

        build_preflight_prompt = _bp


@dataclass
class RunMetrics:
    """Metrics collected from a single preflight run against one story."""

    story_id: str
    model_name: str
    verdict: str | None
    expected_verdict: str
    complexity: str | None
    expected_complexity: str | None
    cost_usd: float | None
    duration_s: float | None
    tool_iterations: int | None
    submitted: bool
    valid_yaml: bool
    rationale_cites_code: bool
    raw_output: str


def run_preflight_for_model(
    story: "GoldenStory",
    profile: "ModelProfile",
    working_dir: Path,
    config: "ForgeConfig",
) -> RunMetrics:
    """Run the preflight prompt for a single story/model combination.

    Mirrors the call pattern in coordinator/preflight_flow.py:_run_preflight_phase
    but skips workspace setup (evaluation always runs against the provided
    working_dir rather than a clean archive).
    """
    from theforge.coordinator.preflight import (  # noqa: PLC0415
        _parse_preflight_complexity,
        _parse_preflight_verdict,
    )
    from theforge.task.story import TaskStory  # noqa: PLC0415

    _ensure_deps()

    # Build a minimal TaskStory wrapper so build_preflight_prompt has a name field.
    task = TaskStory(
        name=story.title,
        slug=story.id,
        story_text=story.story_text,
    )

    prompt = build_preflight_prompt(
        task,
        story_content=story.story_text,
        assembled_context=None,
    )

    start = time.monotonic()
    try:
        result = run_agent(
            prompt=prompt,
            profile=profile,
            working_dir=working_dir,
            secrets=config.secrets,
        )
    except Exception:
        # Runner raised — treat as non-submission.
        duration_s = time.monotonic() - start
        return RunMetrics(
            story_id=story.id,
            model_name=profile.model,
            verdict=None,
            expected_verdict=story.expected_verdict,
            complexity=None,
            expected_complexity=story.expected_complexity,
            cost_usd=None,
            duration_s=duration_s,
            tool_iterations=None,
            submitted=False,
            valid_yaml=False,
            rationale_cites_code=False,
            raw_output="",
        )

    duration_s = time.monotonic() - start

    submitted = result.success and bool(result.output.strip())

    if submitted:
        valid_yaml = _output_has_valid_yaml(result.output)
        if valid_yaml:
            verdict, _, _degraded = _parse_preflight_verdict(result.output)
            complexity = _parse_preflight_complexity(result.output)
        else:
            # Output is garbage — don't report the fallback-BLOCKED as a real verdict.
            verdict = None
            complexity = None
    else:
        verdict = None
        valid_yaml = False
        complexity = None

    # Extract tool_iterations from result.raw if present.
    # Claude CLI --output-format json exposes "num_turns"; other backends return None.
    tool_iterations: int | None = None
    if result.raw:
        tool_iterations = result.raw.get("num_turns") or result.raw.get("turns")

    # Heuristic: does the output cite a concrete file reference?
    rationale_cites_code = bool(_FILE_REF_RE.search(result.output or ""))

    return RunMetrics(
        story_id=story.id,
        model_name=profile.model,
        verdict=verdict,
        expected_verdict=story.expected_verdict,
        complexity=complexity,
        expected_complexity=story.expected_complexity,
        cost_usd=result.cost_usd,
        duration_s=duration_s,
        tool_iterations=tool_iterations,
        submitted=submitted,
        valid_yaml=valid_yaml,
        rationale_cites_code=rationale_cites_code,
        raw_output=result.output or "",
    )


def run_harness(
    golden: list["GoldenStory"],
    profiles: list["ModelProfile"],
    working_dir: Path,
    config: "ForgeConfig",
) -> list[RunMetrics]:
    """Evaluate all stories against all profiles sequentially.

    Returns a flat list of RunMetrics (len = len(golden) * len(profiles)).
    """
    metrics: list[RunMetrics] = []
    for story in golden:
        for profile in profiles:
            m = run_preflight_for_model(story, profile, working_dir, config)
            metrics.append(m)
    return metrics
