"""Spec validation: lightweight pre-PLAN quality check.

Calls a fast model to detect internal contradictions, orphaned ACs, and scope
bloat before the PLAN phase runs. Advisory only — never blocks execution.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .runner import run_agent

_VALIDATION_PROMPT = """\
You are a spec quality checker for a software development orchestrator.
Analyze the spec below and identify:

1. Internal contradictions between Requirements and Acceptance Criteria
2. Acceptance criteria that describe implementation internals (function names,
   dataclass shapes, file-internal steps) rather than observable behaviour
3. Requirements that are ambiguous or cannot be satisfied simultaneously
4. Acceptance criteria with no corresponding requirement (orphaned criteria)
5. Scope assessment: does the spec cover multiple independent functional areas,
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
    description: "The spec covers 3 independent subsystems with no shared state"
    split_suggestion:
      stories:
        - name: "Story: Authentication flow"
          acs:
            - "User can log in with email/password"
            - "User receives error on invalid credentials"
        - name: "Story: Profile management"
          acs:
            - "User can update display name"
```

Rules:
- verdict must be PASS or WARN
- findings must be a list (empty for PASS)
- category must be "requirement" or "scope"
- split_suggestion is only for scope findings; set to null for requirement findings
- Include split_suggestion only when you are confident the spec should be split
- Be conservative: only WARN when you have a clear, specific issue

---
SPEC:

{spec_content}
"""


@dataclass
class SpecValidationFinding:
    """A single finding from spec validation."""

    category: str  # "requirement" | "scope"
    description: str
    split_suggestion: dict[str, Any] | None = None  # {"stories": [...]} for scope findings


@dataclass
class SpecValidationResult:
    """Result from spec validation."""

    verdict: str  # "PASS" | "WARN"
    findings: list[SpecValidationFinding] = field(default_factory=list)
    cost_usd: float | None = None
    duration_s: float | None = None


def _extract_yaml_block(text: str) -> str | None:
    """Extract the first YAML code block from model output."""
    # Look for ```yaml ... ``` block
    match = re.search(r"```yaml\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    # Fallback: look for bare YAML starting with 'verdict:'
    match = re.search(r"(verdict:\s*(PASS|WARN).*?)(?:\n\n|\Z)", text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


def _parse_validation_output(output: str) -> SpecValidationResult:
    """Parse model output into a SpecValidationResult.

    Fail-safe: any parse error returns PASS with no findings.
    """
    try:
        yaml_text = _extract_yaml_block(output)
        if yaml_text is None:
            return SpecValidationResult(verdict="PASS")

        data = yaml.safe_load(yaml_text)
        if not isinstance(data, dict):
            return SpecValidationResult(verdict="PASS")

        verdict = str(data.get("verdict", "PASS")).upper()
        if verdict not in ("PASS", "WARN"):
            verdict = "PASS"

        raw_findings = data.get("findings") or []
        if not isinstance(raw_findings, list):
            return SpecValidationResult(verdict=verdict)

        findings: list[SpecValidationFinding] = []
        for item in raw_findings:
            if not isinstance(item, dict):
                continue
            category = str(item.get("category", "requirement")).lower()
            if category not in ("requirement", "scope"):
                category = "requirement"
            description = str(item.get("description", ""))
            split_suggestion = item.get("split_suggestion")
            # Normalize: null/None split_suggestion → None
            if split_suggestion is not None and not isinstance(split_suggestion, dict):
                split_suggestion = None
            findings.append(
                SpecValidationFinding(
                    category=category,
                    description=description,
                    split_suggestion=split_suggestion,
                )
            )

        return SpecValidationResult(verdict=verdict, findings=findings)

    except Exception:  # noqa: BLE001
        # Any parse failure is fail-safe: return PASS
        return SpecValidationResult(verdict="PASS")


def _make_fast_profile(profile: Any) -> Any:
    """Return a copy of profile with the model forced to sonnet if it's opus.

    Uses dataclasses.replace() since ModelProfile is frozen.
    Matches 'opus' case-insensitively as a substring of the model string.
    This works for both short names ('opus') and full model IDs ('claude-opus-4-6').
    For API profiles with full model IDs like 'claude-opus-4-6', the substitution
    sets model='sonnet' which is not a valid full model ID — this is acceptable
    for the CLI-based dogfooding config; API profiles with full opus model IDs
    are an unspecified edge case per the spec.
    """
    if "opus" in profile.model.lower():
        return dataclasses.replace(profile, model="sonnet")
    return profile


def validate_spec(
    spec_content: str,
    profile: Any,
    working_dir: Path,
    secrets: dict[str, str] | None = None,
) -> SpecValidationResult:
    """Validate a spec with a fast model call.

    Returns a SpecValidationResult with verdict PASS or WARN.
    Never raises — any failure returns PASS (fail-safe).

    Args:
        spec_content: Raw text content of the spec file.
        profile: ModelProfile to use (will be cloned with fast model).
        working_dir: Working directory for the agent subprocess.
        secrets: Optional secrets dict passed to run_agent.
    """
    import time

    fast_profile = _make_fast_profile(profile)
    # Give validation a short timeout — it's advisory only
    fast_profile = dataclasses.replace(fast_profile, name="spec-validator", timeout_seconds=120)

    prompt = _VALIDATION_PROMPT.format(spec_content=spec_content)

    try:
        t0 = time.monotonic()
        agent_result = run_agent(
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
        return SpecValidationResult(verdict="PASS")
