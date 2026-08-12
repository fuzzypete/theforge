"""Capability-level readiness probes for ``forge check-providers``."""

from __future__ import annotations

import dataclasses
import json
import time
from dataclasses import dataclass
from pathlib import Path

from theforge.agent_types import AgentResult
from theforge.assignment import _agent_to_profile
from theforge.config import DEFAULT_INVESTIGATION_TOOLS, DEFAULT_PREFLIGHT_PROFILE, ForgeConfig
from theforge.config.profiles import iter_config_profiles, iter_plan_phase_profiles
from theforge.runners.api import run_api_agent
from theforge.runners.schema_utils import openai_function_tool_request_shape

READINESS_STATUS_READY = "ready"
READINESS_STATUS_FAILED = "failed"
READINESS_STATUS_UNSUPPORTED = "unsupported"
READINESS_STATUS_COST_UNAVAILABLE = "cost-unavailable"

READINESS_STATUSES: tuple[str, ...] = (
    READINESS_STATUS_READY,
    READINESS_STATUS_FAILED,
    READINESS_STATUS_UNSUPPORTED,
    READINESS_STATUS_COST_UNAVAILABLE,
)

READINESS_CAPABILITY_PLAIN_STRUCTURED = "plain-structured"
READINESS_CAPABILITY_TOOL_STRUCTURED = "tool-structured"

_READINESS_PROMPT = (
    "Review this diff: -    x = 1\n+    x = 2\n\n"
    "Respond with JSON containing a 'verdict' field set to APPROVE or REQUEST_CHANGES, "
    "and a 'summary' field with a one-line explanation."
)


@dataclass(frozen=True)
class ReadinessProbe:
    """One capability the provider check should exercise."""

    role: str
    capability: str
    profile: object


@dataclass(frozen=True)
class ReadinessResult:
    """Outcome of one readiness probe."""

    probe: ReadinessProbe
    elapsed: float
    status: str
    detail: str
    outcome: AgentResult | Exception | None = None

    @property
    def ready(self) -> bool:
        return self.status == READINESS_STATUS_READY


def build_readiness_probes(config: ForgeConfig) -> list[ReadinessProbe]:
    """Derive the API capability probes production can actually dispatch."""
    probes: list[ReadinessProbe] = []

    for role, profile in iter_config_profiles(config):
        if role == "agent-pool":
            continue
        probes.append(_probe_for_profile(role, profile))
    for role, profile in iter_plan_phase_profiles(config):
        probes.append(_probe_for_profile(role, profile))

    advisor_profile = dataclasses.replace(
        config.preflight_profile,
        allowed_tools=DEFAULT_INVESTIGATION_TOOLS,
        phase="advisor",
    )
    probes.append(_probe_for_profile("advisor", advisor_profile))

    for agent in config.agents:
        probes.extend(
            [
                _probe_for_profile("agent-dev", _agent_to_profile(agent, role="dev")),
                _probe_for_profile(
                    "agent-preflight",
                    _agent_to_profile(
                        agent,
                        role="preflight",
                        allowed_tools=DEFAULT_PREFLIGHT_PROFILE.allowed_tools,
                    ),
                ),
                _probe_for_profile("agent-planner", _agent_to_profile(agent, role="review")),
                _probe_for_profile("agent-plan-review", _agent_to_profile(agent, role="review")),
                _probe_for_profile("agent-code-review", _agent_to_profile(agent, role="review")),
            ]
        )

    deduped: list[ReadinessProbe] = []
    seen: set[tuple[str, str, object]] = set()
    for probe in probes:
        profile = probe.profile
        if getattr(profile, "mode", None) != "api":
            continue
        key = (probe.role, probe.capability, profile)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(probe)
    return deduped


def run_readiness_probe(
    probe: ReadinessProbe,
    *,
    working_dir: Path,
    secrets: dict[str, str] | None,
) -> ReadinessResult:
    """Exercise one capability probe and classify the outcome."""
    profile = probe.profile
    if (
        probe.capability == READINESS_CAPABILITY_TOOL_STRUCTURED
        and getattr(profile, "provider", None) == "openai"
        and not _is_local_endpoint(getattr(profile, "base_url", None))
    ):
        tool_shape = openai_function_tool_request_shape(profile.model)
        if tool_shape.transport == "unsupported":
            return ReadinessResult(
                probe=probe,
                elapsed=0.0,
                status=READINESS_STATUS_UNSUPPORTED,
                detail=(
                    "not exercised: no supported OpenAI tool-bearing request shape "
                    "is configured for this model"
                ),
            )

    t0 = time.perf_counter()
    try:
        result = run_api_agent(
            prompt=_READINESS_PROMPT,
            profile=profile,
            working_dir=working_dir,
            quiet=True,
            secrets=secrets,
        )
    except Exception as exc:
        return ReadinessResult(
            probe=probe,
            elapsed=time.perf_counter() - t0,
            status=READINESS_STATUS_FAILED,
            detail=f"{type(exc).__name__}: {exc}",
            outcome=exc,
        )

    elapsed = time.perf_counter() - t0
    if not result.success:
        short_err = (result.output or "unknown error")[:120]
        return ReadinessResult(
            probe=probe,
            elapsed=elapsed,
            status=READINESS_STATUS_FAILED,
            detail=short_err,
            outcome=result,
        )

    payload = _structured_payload(result)
    if payload is None or "verdict" not in payload:
        return ReadinessResult(
            probe=probe,
            elapsed=elapsed,
            status=READINESS_STATUS_FAILED,
            detail="no valid verdict in structured output",
            outcome=result,
        )

    if not _is_local_endpoint(getattr(profile, "base_url", None)) and result.cost_usd is None:
        return ReadinessResult(
            probe=probe,
            elapsed=elapsed,
            status=READINESS_STATUS_COST_UNAVAILABLE,
            detail="structured result returned but cost is unavailable",
            outcome=result,
        )

    return ReadinessResult(
        probe=probe,
        elapsed=elapsed,
        status=READINESS_STATUS_READY,
        detail=_success_suffix(profile=profile, result=result, elapsed=elapsed),
        outcome=result,
    )


def _probe_for_profile(role: str, profile: object) -> ReadinessProbe:
    allowed_tools = tuple(getattr(profile, "allowed_tools", ()) or ())
    capability = (
        READINESS_CAPABILITY_TOOL_STRUCTURED
        if allowed_tools
        else READINESS_CAPABILITY_PLAIN_STRUCTURED
    )
    return ReadinessProbe(role=role, capability=capability, profile=profile)


def _structured_payload(result: AgentResult) -> dict | None:
    if isinstance(result.structured_data, dict):
        return result.structured_data
    try:
        payload = json.loads(result.output)
    except (TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _is_local_endpoint(base_url: str | None) -> bool:
    return bool(base_url and ("localhost" in base_url or "127.0.0.1" in base_url))


def _success_suffix(*, profile: object, result: AgentResult, elapsed: float) -> str:
    if _is_local_endpoint(getattr(profile, "base_url", None)) or result.cost_usd == 0.0:
        cost_str = "$0.000"
    else:
        cost_str = f"${result.cost_usd:.3f}"
    local_tag = " [local]" if _is_local_endpoint(getattr(profile, "base_url", None)) else ""
    return f"{elapsed:.1f}s  {cost_str}{local_tag}"
