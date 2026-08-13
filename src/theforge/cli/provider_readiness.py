"""Capability-level readiness probes for ``forge check-providers``."""

from __future__ import annotations

import dataclasses
import json
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from theforge.agent_types import AgentResult
from theforge.assignment import _agent_to_profile
from theforge.config import (
    DEFAULT_INVESTIGATION_TOOLS,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    ForgeConfig,
    TransportSpec,
    resolve_preflight_tools,
    transport_for,
)
from theforge.config.auth import check_agent_auth
from theforge.config.models import mirror_fields_for_transport
from theforge.config.profiles import iter_config_profiles, iter_plan_phase_profiles
from theforge.config.types import ModelProfile
from theforge.runners.api import run_api_agent
from theforge.runners.cli import run_agent
from theforge.runners.schema_utils import openai_function_tool_request_shape

READINESS_STATUS_READY = "ready"
READINESS_STATUS_FAILED = "failed"
READINESS_STATUS_UNSUPPORTED = "unsupported"
READINESS_STATUS_UNVERIFIED = "unverified"
READINESS_STATUS_COST_UNAVAILABLE = "cost-unavailable"

READINESS_STATUSES: tuple[str, ...] = (
    READINESS_STATUS_READY,
    READINESS_STATUS_FAILED,
    READINESS_STATUS_UNSUPPORTED,
    READINESS_STATUS_UNVERIFIED,
    READINESS_STATUS_COST_UNAVAILABLE,
)

READINESS_CAPABILITY_PLAIN_STRUCTURED = "plain-structured"
READINESS_CAPABILITY_TOOL_STRUCTURED = "tool-structured"

_READINESS_PROMPT = (
    "Review this diff: -    x = 1\n+    x = 2\n\n"
    "Respond with JSON containing a 'verdict' field set to APPROVE or REQUEST_CHANGES, "
    "and a 'summary' field with a one-line explanation."
)

_AUTH_FAILURE_PATTERNS: tuple[str, ...] = (
    "failed to authenticate",
    "oauth access token has been revoked",
    "authentication_error",
    "authenticationerror",
    "invalid api key",
    "invalid_api_key",
    "invalid x-api-key",
    "unauthorized",
    "not logged in",
    "login required",
    "bad credentials",
    "permission_error",
)
_AUTH_STATUS_CODE_CONTEXT = (
    r"\b(?:http|https|status|statuscode|status[ _-]code|error|errors|err|code|response)\b"
    r"[^0-9a-z]{0,8}"
)
_AUTH_STATUS_CODES = r"401|403"
_AUTH_STATUS_REASONS = r"unauthorized|forbidden|permission denied"
_AUTH_CODE_RE = re.compile(
    rf"{_AUTH_STATUS_CODE_CONTEXT}(?:{_AUTH_STATUS_CODES})\b"
    rf"|\b(?:{_AUTH_STATUS_CODES})\s+(?:{_AUTH_STATUS_REASONS})\b"
)


@dataclass(frozen=True)
class ReadinessProbe:
    """One capability the provider check should exercise."""

    role: str
    capability: str
    profile: ModelProfile

    @property
    def declared_transport_kind(self) -> str:
        return self.profile.mode


@dataclass(frozen=True)
class ReadinessAttempt:
    """Outcome of exercising one transport for a readiness probe."""

    probe: ReadinessProbe
    attempted_transport_kind: str
    declared_transport_kind: str
    elapsed: float
    status: str
    detail: str
    outcome: AgentResult | Exception | None = None

    @property
    def ready(self) -> bool:
        return self.status == READINESS_STATUS_READY

    @property
    def is_declared_transport(self) -> bool:
        return self.attempted_transport_kind == self.declared_transport_kind


@dataclass(frozen=True)
class ReadinessResult:
    """Grouped readiness evidence for one declared profile/capability probe."""

    probe: ReadinessProbe
    attempts: tuple[ReadinessAttempt, ...]

    @property
    def declared_attempt(self) -> ReadinessAttempt:
        for attempt in self.attempts:
            if attempt.is_declared_transport:
                return attempt
        raise ValueError("readiness result is missing the declared transport attempt")

    @property
    def elapsed(self) -> float:
        return self.declared_attempt.elapsed

    @property
    def status(self) -> str:
        return self.declared_attempt.status

    @property
    def detail(self) -> str:
        return self.declared_attempt.detail

    @property
    def outcome(self) -> AgentResult | Exception | None:
        return self.declared_attempt.outcome

    @property
    def ready(self) -> bool:
        return self.declared_attempt.ready

    @property
    def ready_alternate_transport_kinds(self) -> tuple[str, ...]:
        return tuple(
            attempt.attempted_transport_kind
            for attempt in self.attempts
            if not attempt.is_declared_transport and attempt.ready
        )


def build_readiness_probes(config: ForgeConfig) -> list[ReadinessProbe]:
    """Derive the capability probes production can actually dispatch."""
    probes: list[ReadinessProbe] = []

    for role, profile in iter_config_profiles(config):
        if role == "agent-pool":
            continue
        probes.append(_probe_for_profile(role, profile))
    for role, profile in iter_plan_phase_profiles(config):
        probes.append(_probe_for_profile(role, _stamp_phase(profile, role)))

    advisor_profile = dataclasses.replace(
        config.preflight_profile,
        allowed_tools=DEFAULT_INVESTIGATION_TOOLS,
    )
    probes.append(_probe_for_profile("advisor", advisor_profile))

    for agent in config.agents:
        probes.extend(_agent_role_probes(agent))

    deduped: list[ReadinessProbe] = []
    seen: set[tuple[str, str, str, str | None, str, str, tuple[str, ...], str | None]] = set()
    for probe in probes:
        profile = probe.profile
        key = (
            probe.role,
            probe.capability,
            profile.name,
            profile.provider_family,
            probe.declared_transport_kind,
            profile.model,
            profile.allowed_tools,
            profile.phase,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(probe)
    return deduped


def run_readiness_probe(
    probe: ReadinessProbe,
    *,
    secrets: dict[str, str] | None,
    include_alternate_transports: bool = True,
) -> ReadinessResult:
    """Exercise one capability probe on its declared and alternate transports."""
    attempts: list[ReadinessAttempt] = []
    for transport_attempt in _transport_attempts_for_probe(
        probe,
        include_alternate_transports=include_alternate_transports,
    ):
        if isinstance(transport_attempt, ReadinessAttempt):
            attempts.append(transport_attempt)
            continue
        attempts.append(
            _run_transport_attempt(
                probe,
                transport=transport_attempt,
                secrets=secrets,
            )
        )
    return ReadinessResult(probe=probe, attempts=tuple(attempts))


def _transport_attempts_for_probe(
    probe: ReadinessProbe,
    *,
    include_alternate_transports: bool,
) -> list[TransportSpec | ReadinessAttempt]:
    provider = probe.profile.provider_family
    if not provider:
        return [
            _unverified_attempt(
                probe,
                attempted_transport_kind=probe.declared_transport_kind,
                detail="not exercised: provider identity is unavailable for this profile",
            )
        ]

    attempts = [_transport_attempt_for_probe(probe, probe.declared_transport_kind)]
    if include_alternate_transports:
        attempts.append(_transport_attempt_for_probe(probe, _alternate_transport_kind(probe)))
    return attempts


def _transport_attempt_for_probe(
    probe: ReadinessProbe,
    transport_kind: str,
) -> TransportSpec | ReadinessAttempt:
    provider = probe.profile.provider_family
    if transport_kind == probe.declared_transport_kind and probe.profile.transport is not None:
        return probe.profile.transport
    if not provider:
        return _unverified_attempt(
            probe,
            attempted_transport_kind=transport_kind,
            detail="not exercised: provider identity is unavailable for this profile",
        )
    try:
        return transport_for(provider, transport_kind)
    except ValueError as exc:
        return _unverified_attempt(
            probe,
            attempted_transport_kind=transport_kind,
            detail=f"not exercised: {exc}",
        )


def _alternate_transport_kind(probe: ReadinessProbe) -> str:
    return "cli" if probe.declared_transport_kind == "api" else "api"


def _run_transport_attempt(
    probe: ReadinessProbe,
    *,
    transport: TransportSpec,
    secrets: dict[str, str] | None,
) -> ReadinessAttempt:
    profile = _profile_for_transport(probe.profile, transport)
    unavailable = _preflight_unverified_reason(profile, secrets)
    if unavailable is not None:
        return _unverified_attempt(
            probe,
            attempted_transport_kind=transport.kind,
            detail=f"not exercised: {unavailable}",
        )

    if (
        probe.capability == READINESS_CAPABILITY_TOOL_STRUCTURED
        and profile.provider == "openai"
        and not _is_local_endpoint(profile.base_url)
    ):
        tool_shape = openai_function_tool_request_shape(profile.model)
        if tool_shape.transport == "unsupported":
            return ReadinessAttempt(
                probe=probe,
                attempted_transport_kind=transport.kind,
                declared_transport_kind=probe.declared_transport_kind,
                elapsed=0.0,
                status=READINESS_STATUS_UNSUPPORTED,
                detail=(
                    "not exercised: no supported OpenAI tool-bearing request shape "
                    "is configured for this model"
                ),
            )

    t0 = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory(prefix="forge-check-providers-") as probe_dir:
            result = _dispatch_probe_attempt(
                prompt=_READINESS_PROMPT,
                profile=profile,
                working_dir=Path(probe_dir),
                secrets=secrets,
            )
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        detail = _runtime_unverified_reason(profile, secrets=secrets, exc=exc)
        if detail is not None:
            return _unverified_attempt(
                probe,
                attempted_transport_kind=transport.kind,
                detail=f"not exercised: {detail}",
                elapsed=elapsed,
                outcome=exc,
            )
        return ReadinessAttempt(
            probe=probe,
            attempted_transport_kind=transport.kind,
            declared_transport_kind=probe.declared_transport_kind,
            elapsed=elapsed,
            status=READINESS_STATUS_FAILED,
            detail=f"{type(exc).__name__}: {exc}",
            outcome=exc,
        )

    elapsed = time.perf_counter() - t0

    identity_mismatch = _identity_mismatch_reason(profile, result)
    if identity_mismatch is not None:
        return _unverified_attempt(
            probe,
            attempted_transport_kind=transport.kind,
            detail=f"not exercised: {identity_mismatch}",
            elapsed=elapsed,
            outcome=result,
        )

    if not result.success:
        unverified_reason = _runtime_unverified_reason(profile, secrets=secrets, result=result)
        short_err = (result.output or "unknown error")[:120]
        return ReadinessAttempt(
            probe=probe,
            attempted_transport_kind=transport.kind,
            declared_transport_kind=probe.declared_transport_kind,
            elapsed=elapsed,
            status=(
                READINESS_STATUS_UNVERIFIED
                if unverified_reason is not None
                else READINESS_STATUS_FAILED
            ),
            detail=unverified_reason or short_err,
            outcome=result,
        )

    if _requires_structured_verdict(probe):
        payload = _structured_payload(result)
        if payload is None or "verdict" not in payload:
            return ReadinessAttempt(
                probe=probe,
                attempted_transport_kind=transport.kind,
                declared_transport_kind=probe.declared_transport_kind,
                elapsed=elapsed,
                status=READINESS_STATUS_FAILED,
                detail="no valid verdict in structured output",
                outcome=result,
            )

    if result.cost_usd is None:
        if transport.kind == "cli":
            return _unverified_attempt(
                probe,
                attempted_transport_kind=transport.kind,
                detail="not exercised: structured result returned but CLI cost is unavailable",
                elapsed=elapsed,
                outcome=result,
            )
        if not _is_local_endpoint(getattr(profile, "base_url", None)):
            return ReadinessAttempt(
                probe=probe,
                attempted_transport_kind=transport.kind,
                declared_transport_kind=probe.declared_transport_kind,
                elapsed=elapsed,
                status=READINESS_STATUS_COST_UNAVAILABLE,
                detail="structured result returned but cost is unavailable",
                outcome=result,
            )

    return ReadinessAttempt(
        probe=probe,
        attempted_transport_kind=transport.kind,
        declared_transport_kind=probe.declared_transport_kind,
        elapsed=elapsed,
        status=READINESS_STATUS_READY,
        detail=_success_suffix(profile=profile, result=result, elapsed=elapsed),
        outcome=result,
    )


def _dispatch_probe_attempt(
    *,
    prompt: str,
    profile: ModelProfile,
    working_dir: Path,
    secrets: dict[str, str] | None,
) -> AgentResult:
    if profile.mode == "api":
        return run_api_agent(
            prompt=prompt,
            profile=profile,
            working_dir=working_dir,
            quiet=True,
            secrets=secrets,
        )
    return run_agent(
        prompt=prompt,
        profile=profile,
        working_dir=working_dir,
        quiet=True,
        secrets=secrets,
    )


def _profile_for_transport(profile: ModelProfile, transport: TransportSpec) -> ModelProfile:
    cli, provider = mirror_fields_for_transport(transport, profile.cli, profile.provider)
    return dataclasses.replace(
        profile,
        transport=transport,
        cli=cli,
        provider=provider,
        api_fallback=None,
        fallback_models=(),
    )


def _preflight_unverified_reason(
    profile: ModelProfile,
    secrets: dict[str, str] | None,
) -> str | None:
    if profile.mode != "cli":
        return None
    ready, reason = check_agent_auth(
        profile,
        secrets,
        include_sandbox_readiness=False,
        include_credential_probe=True,
    )
    if ready:
        return None
    return reason


def _runtime_unverified_reason(
    profile: ModelProfile,
    *,
    secrets: dict[str, str] | None,
    result: AgentResult | None = None,
    exc: Exception | None = None,
) -> str | None:
    ready, reason = _safe_auth_check(profile, secrets)
    if not ready:
        return reason

    if result is not None and result.startup_failure:
        return (result.output or "startup failure")[:120]

    message = ""
    if result is not None:
        message = result.output or ""
    if exc is not None:
        message = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, (ImportError, ModuleNotFoundError, FileNotFoundError)):
            return message

    lowered = message.lower()
    if _looks_like_auth_failure(lowered):
        return message[:120]
    return None


def _safe_auth_check(
    profile: ModelProfile,
    secrets: dict[str, str] | None,
) -> tuple[bool, str]:
    try:
        return check_agent_auth(
            profile,
            secrets,
            include_sandbox_readiness=False,
            include_credential_probe=True,
        )
    except ValueError as exc:
        return (False, str(exc))


def _looks_like_auth_failure(message: str) -> bool:
    return any(pattern in message for pattern in _AUTH_FAILURE_PATTERNS) or bool(
        _AUTH_CODE_RE.search(message)
    )


def _identity_mismatch_reason(profile: ModelProfile, result: AgentResult) -> str | None:
    expected_transport = profile.mode
    observed_transport = result.transport_used or expected_transport
    if observed_transport != expected_transport:
        return (
            "requested transport "
            f"{expected_transport!r} but runner reported {observed_transport!r}"
        )

    expected_model = profile.model
    observed_model = result.model_used or expected_model
    if observed_model != expected_model:
        return f"requested model {expected_model!r} but runner reported {observed_model!r}"
    return None


def _unverified_attempt(
    probe: ReadinessProbe,
    *,
    attempted_transport_kind: str,
    detail: str,
    elapsed: float = 0.0,
    outcome: AgentResult | Exception | None = None,
) -> ReadinessAttempt:
    return ReadinessAttempt(
        probe=probe,
        attempted_transport_kind=attempted_transport_kind,
        declared_transport_kind=probe.declared_transport_kind,
        elapsed=elapsed,
        status=READINESS_STATUS_UNVERIFIED,
        detail=detail,
        outcome=outcome,
    )


def _probe_for_profile(role: str, profile: ModelProfile) -> ReadinessProbe:
    profile = _project_probe_profile(_stamp_phase(profile, role))
    allowed_tools = tuple(profile.allowed_tools or ())
    capability = (
        READINESS_CAPABILITY_TOOL_STRUCTURED
        if allowed_tools
        else READINESS_CAPABILITY_PLAIN_STRUCTURED
    )
    return ReadinessProbe(role=role, capability=capability, profile=profile)


def _stamp_phase(profile: ModelProfile, role: str) -> ModelProfile:
    phase_by_role = {
        "dev": "dev",
        "preflight": "preflight",
        "preflight-fallback": "preflight",
        "review": "review",
        "synthesis": "synthesis",
        "plan": "plan",
        "plan-review": "plan_review",
        "advisor": "preflight",
        "agent-dev": "dev",
        "agent-preflight": "preflight",
        "agent-planner": "plan",
        "agent-plan-review": "plan_review",
        "agent-code-review": "review",
    }
    phase = phase_by_role.get(role, profile.phase)
    return dataclasses.replace(profile, phase=phase)


def _agent_role_probes(agent: object) -> list[ReadinessProbe]:
    planner_profile = _agent_to_profile(
        agent,
        role="plan",
        allowed_tools=DEFAULT_INVESTIGATION_TOOLS,
    )
    review_tools = DEFAULT_REVIEW_PROFILE.allowed_tools
    plan_review_profile = dataclasses.replace(
        _agent_to_profile(agent, role="review", allowed_tools=review_tools),
        phase="plan_review",
    )
    return [
        _probe_for_profile("agent-dev", _agent_to_profile(agent, role="dev")),
        _probe_for_profile(
            "agent-preflight",
            _agent_to_profile(
                agent,
                role="preflight",
                allowed_tools=DEFAULT_PREFLIGHT_PROFILE.allowed_tools,
            ),
        ),
        _probe_for_profile("agent-planner", planner_profile),
        _probe_for_profile("agent-plan-review", plan_review_profile),
        _probe_for_profile(
            "agent-code-review",
            _agent_to_profile(agent, role="review", allowed_tools=review_tools),
        ),
    ]


def _project_probe_profile(profile: ModelProfile) -> ModelProfile:
    if profile.phase != "dev":
        return profile
    return dataclasses.replace(
        profile,
        allowed_tools=resolve_preflight_tools(profile.allowed_tools),
    )


def _requires_structured_verdict(probe: ReadinessProbe) -> bool:
    return probe.profile.phase not in {"dev", "preflight"}


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


def _success_suffix(*, profile: ModelProfile, result: AgentResult, elapsed: float) -> str:
    if _is_local_endpoint(profile.base_url) or result.cost_usd == 0.0:
        cost_str = "$0.000"
    else:
        cost_str = f"${result.cost_usd:.3f}"
    local_tag = " [local]" if _is_local_endpoint(profile.base_url) else ""
    return f"{elapsed:.1f}s  {cost_str}{local_tag}"
