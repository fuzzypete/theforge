"""Story validation: lightweight pre-PLAN quality check.

Calls a fast model to detect internal contradictions, orphaned ACs, and scope
bloat before the PLAN phase runs. Advisory only — never blocks execution.
"""

from __future__ import annotations

import dataclasses
import re
import sys
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Lazy runner slot — None until first call; tests may replace with a mock.
run_agent = None


def _warn_story_validation_shape(event: str, **details: Any) -> None:
    """Emit diagnostics for unexpected story validation shapes."""
    lines = [f"[forge] WARNING: story validation {event}"]
    for key, value in details.items():
        lines.append(f"  {key}: {value}")
    lines.append(f"  thread: {threading.current_thread().name}")
    lines.append("  stack:")
    lines.extend(f"    {line}" for line in traceback.format_stack(limit=8))
    print("\n".join(lines), file=sys.stderr)


def _ensure_runner() -> None:
    global run_agent
    if run_agent is None:
        from .runners import run_agent as _ra  # noqa: PLC0415

        run_agent = _ra


_VALIDATION_PROMPT = """\
You are a story quality checker for a software development orchestrator.
Analyze the story below and identify:

1. Internal contradictions between Requirements and Acceptance Criteria
2. Acceptance criteria that describe implementation internals (function names,
   dataclass shapes, file-internal steps) rather than observable behaviour
3. Requirements that are ambiguous or cannot be satisfied simultaneously
4. Acceptance criteria with no corresponding requirement (orphaned criteria)
5. Scope assessment: does the story cover multiple independent functional areas,
   distinct technology domains, or too many unrelated ACs?

Scope heuristics (not hard rules):
- 7+ acceptance criteria is a signal
- 2+ distinct tech stacks (e.g. iOS + React + watchOS) almost always need splitting
- ACs that are fully independent and touch different parts of the codebase
- Would a single developer struggle to review this as one coherent PR?

Output ONLY a YAML block in this exact format (no other text):

```yaml
verdict: PASS
findings: []
```

Or if issues are found:

```yaml
verdict: WARN
findings:
  - category: requirement
    description: "Clear description of the contradiction or issue"
    split_suggestion: null
  - category: scope
    description: "The story covers 3 independent subsystems with no shared state"
    split_suggestion:
      stories:
        - name: "Suggested sub-story name"
          acs:
            - "Observable acceptance criterion"
```

Rules:
- verdict must be PASS or WARN
- findings must be a list (empty for PASS)
- category must be "requirement" or "scope"
- split_suggestion is only for scope findings; set to null for requirement findings
- Include split_suggestion only when you are confident the story should be split
- Be conservative: only WARN when you have a clear, specific issue

## Story

{story_content}
"""


@dataclass
class StoryValidationFinding:
    """A single finding from the story validation check."""

    category: str
    description: str
    split_suggestion: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        """Normalize external inputs to the expected internal shape."""
        category = str(self.category).lower()
        if category in {"contradiction", "internals", "ambiguity", "orphaned"}:
            category = "requirement"
        if category not in (
            "requirement",
            "scope",
        ):
            category = "requirement"
        self.category = category
        self.description = str(self.description)
        if self.split_suggestion is not None and not isinstance(self.split_suggestion, dict):
            self.split_suggestion = None


@dataclass
class StoryValidationResult:
    """The result of a story validation check."""

    verdict: str = "PASS"  # PASS | WARN
    findings: list[StoryValidationFinding] = field(default_factory=list)
    cost_usd: float | None = None
    duration_s: float | None = None

    def __post_init__(self) -> None:
        """Coerce findings so downstream code can trust attribute access."""
        verdict = str(self.verdict).upper()
        self.verdict = verdict if verdict in ("PASS", "WARN") else "PASS"

        normalized: list[StoryValidationFinding] = []
        for finding in self.findings:
            if isinstance(finding, StoryValidationFinding):
                normalized.append(finding)
                continue
            if isinstance(finding, dict):
                _warn_story_validation_shape(
                    "coercing raw dict finding",
                    finding_type=type(finding).__name__,
                    finding_module=type(finding).__module__,
                    finding_repr=repr(finding)[:500],
                )
                normalized.append(
                    StoryValidationFinding(
                        category=finding.get("category", "requirement"),
                        description=finding.get("description", ""),
                        split_suggestion=finding.get("split_suggestion"),
                    )
                )
                continue
            _warn_story_validation_shape(
                "dropping unexpected finding",
                finding_type=type(finding).__name__,
                finding_module=type(finding).__module__,
                finding_repr=repr(finding)[:500],
            )
        self.findings = normalized


def _make_fast_profile(profile: Any) -> Any:
    """Return a fast model profile (sonnet) for advisory validation.

    If the provided profile is already using a fast model (or anything not opus),
    it is returned as-is.
    """
    if "opus" in profile.model.lower():
        return dataclasses.replace(profile, model="sonnet")
    return profile


def _extract_yaml_block(text: str) -> str | None:
    """Extract first YAML block from text, or None if not found."""
    # [P2] More tolerant: allow trailing whitespace after yaml marker
    match = re.search(r"```[yY][aA][mM][lL][ \t]*\n(.*?)\n```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


def _parse_validation_output(output: str) -> StoryValidationResult:
    """Parse YAML block from model output."""
    yaml_text = _extract_yaml_block(output)
    v_match = None
    if yaml_text:
        try:
            data = yaml.safe_load(yaml_text)
        except Exception:
            return StoryValidationResult(verdict="PASS")
    else:
        v_match = re.search(r"^verdict:\s*(\w+)", output, re.MULTILINE | re.IGNORECASE)
        bare_yaml = re.search(
            r"(?mi)^(verdict:\s*\w+.*?)(?:\n\s*\n|\Z)",
            output,
            re.DOTALL,
        )
        if bare_yaml:
            try:
                data = yaml.safe_load(bare_yaml.group(1).strip())
            except Exception:
                if v_match:
                    data = {"verdict": v_match.group(1)}
                else:
                    return StoryValidationResult(verdict="PASS")
        else:
            # If full-text parse fails, try searching for verdict: pattern
            # as a last-resort fallback for messy LLM output.
            if v_match:
                data = {"verdict": v_match.group(1)}
            else:
                return StoryValidationResult(verdict="PASS")

    if not isinstance(data, dict):
        if v_match:
            data = {"verdict": v_match.group(1)}
        else:
            return StoryValidationResult(verdict="PASS")

    # [P1] Normalize verdict case: warn -> WARN
    verdict = str(data.get("verdict", "PASS")).upper()
    if verdict not in ("PASS", "WARN"):
        verdict = "PASS"

    _KNOWN_CATEGORIES = {
        "requirement",
        "scope",
        "contradiction",
        "internals",
        "ambiguity",
        "orphaned",
    }
    findings = []
    for f_data in data.get("findings", []):
        if not isinstance(f_data, dict):
            continue

        # [P2] Normalize category case: Scope -> scope
        category = str(f_data.get("category", "requirement")).lower()
        if category not in _KNOWN_CATEGORIES:
            category = "requirement"

        # [P1] Fail-safe split_suggestion: must be a dict or None
        split_suggestion = f_data.get("split_suggestion")
        if split_suggestion is not None and not isinstance(split_suggestion, dict):
            split_suggestion = None

        findings.append(
            StoryValidationFinding(
                category=category,
                description=f_data.get("description", "No description provided"),
                split_suggestion=split_suggestion,
            )
        )

    return StoryValidationResult(
        verdict=verdict,
        findings=findings,
    )


def validate_story(
    story_content: str,
    profile: Any,
    working_dir: Path,
    secrets: dict[str, str] | None = None,
    runner: Callable | None = None,
) -> StoryValidationResult:
    """Validate a story with a fast model call.

    Returns a StoryValidationResult with verdict PASS or WARN.
    Never raises — any failure returns PASS (fail-safe).

    Args:
        story_content: Raw text content of the story file.
        profile: ModelProfile to use (will be cloned with fast model).
        working_dir: Working directory for the agent subprocess.
        secrets: Optional secrets dict passed to run_agent.
        runner: Optional agent runner to use.
    """
    import time

    fast_profile = _make_fast_profile(profile)
    # Give validation a short timeout — it's advisory only
    fast_profile = dataclasses.replace(
        fast_profile,
        name="story-validator",
        timeout_seconds=120,
    )

    prompt = _VALIDATION_PROMPT.format(story_content=story_content)

    try:
        resolved_runner = runner or run_agent
        if resolved_runner is None:
            _ensure_runner()
            resolved_runner = run_agent

        t0 = time.monotonic()
        agent_result = resolved_runner(
            prompt=prompt,
            profile=fast_profile,
            working_dir=working_dir,
            secrets=secrets or {},
            quiet=True,
        )
        duration_s = time.monotonic() - t0

        result = _parse_validation_output(agent_result.output or "")
        result.cost_usd = agent_result.cost_usd
        result.duration_s = duration_s
        return result

    except Exception:  # noqa: BLE001
        # Any failure is fail-safe: return PASS
        return StoryValidationResult(verdict="PASS")
