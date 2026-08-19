"""Post-DONE control flow for evidence-backed run summaries (Layer 2).

Generation is a **side effect of a run that already finished**, not a phase of
it. The DONE transition is committed and the authoritative audit record is
written before this module is reached, so nothing here can change a run's
outcome: every failure path — disabled config, no dispatchable profile, agent
error, unparseable output, evidence that does not resolve, a filesystem error —
logs a warning and returns None. The caller's audit write path is unchanged
either way.

Two containment properties are enforced here rather than asked for in the
prompt:

* **Tool-free dispatch.** The summary agent gets an API-transport profile with
  an empty tool allowlist, which ``runners.api`` serves as a single stateless
  call. An empty allowlist on a *CLI* profile means the opposite (``claude``
  omits ``--allowedTools`` and grants its unrestricted default set), so a
  CLI-transport profile is never dispatched here — if no API transport can be
  derived, generation is skipped and says so.
* **Exactly once per run.** Several terminal seams write a finished run's
  audit. Generation is guarded on the artifact's own existence, so a run that
  reaches more than one of them is summarised once and billed once.

Schema/validation lives in ``theforge.knowledge_summary``; prompt construction
lives in ``theforge.task.summary_prompts``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from theforge.config.model_identity import DEFAULT_PHASE_ELIGIBILITY, PHASE_KNOWLEDGE_SUMMARY
from theforge.knowledge_summary import (
    SummaryValidationError,
    build_summary_artifact,
    extract_anchors,
    parse_summary_output,
    summary_exists,
    validate_proposed_summary,
    write_summary,
)
from theforge.model_capabilities import identity_for_agent, identity_for_profile
from theforge.task.summary_prompts import build_run_summary_prompt

from . import util as _cu

if TYPE_CHECKING:
    from theforge.config import ForgeConfig, ModelProfile
    from theforge.coordinator.state import CoordinatorResult

_log = _cu._log

# Lazy runner slot (mirrors escalation_advisor_flow): None until first call so
# tests can replace it. Patch target:
#   theforge.coordinator.knowledge_summary_flow.run_agent
run_agent = None


@dataclass(frozen=True)
class RunSummaryOutcome:
    """Outcome of post-DONE knowledge summary generation for one run."""

    status: str
    attempted: bool
    written: bool
    reason: str | None = None
    path: "Path | None" = None

    def to_audit_dict(self) -> dict:
        payload: dict[str, object] = {
            "status": self.status,
            "attempted": self.attempted,
            "written": self.written,
        }
        if self.reason:
            payload["reason"] = self.reason
        if self.path is not None:
            payload["path"] = str(self.path)
        return payload

    @classmethod
    def from_audit_dict(cls, payload: object) -> "RunSummaryOutcome | None":
        """Decode a previously-recorded audit outcome if it is well-formed."""
        if not isinstance(payload, dict):
            return None

        status = payload.get("status")
        attempted = payload.get("attempted")
        written = payload.get("written")
        if not isinstance(status, str):
            return None
        if not isinstance(attempted, bool) or not isinstance(written, bool):
            return None

        reason = payload.get("reason")
        if reason is not None and not isinstance(reason, str):
            reason = None

        path_value = payload.get("path")
        path = path_value if isinstance(path_value, Path) else None
        if isinstance(path_value, str) and path_value:
            path = Path(path_value)

        return cls(
            status=status,
            attempted=attempted,
            written=written,
            reason=reason,
            path=path,
        )


def _record_summary_outcome(audit: dict, outcome: RunSummaryOutcome) -> RunSummaryOutcome:
    """Persist the generation outcome onto the audit payload in place."""
    audit["knowledge_summary"] = outcome.to_audit_dict()
    return outcome


def _existing_summary_outcome(
    config: "ForgeConfig",
    run_id: str,
    audit: dict,
) -> RunSummaryOutcome | None:
    """Return the durable outcome already known for this run, if any."""
    recorded = RunSummaryOutcome.from_audit_dict(audit.get("knowledge_summary"))
    if recorded is not None:
        return recorded

    if not run_id:
        return None

    run_record = config.project_root / ".forge" / "audits" / "runs" / f"{run_id}.json"
    try:
        with open(run_record, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return RunSummaryOutcome.from_audit_dict(payload.get("knowledge_summary"))


def _reuse_existing_outcome(outcome: RunSummaryOutcome | None) -> bool:
    """Report whether a durable prior outcome should block redispatch."""
    if outcome is None:
        return False
    return outcome.attempted or outcome.written


def _not_attempted_reason(
    config: "ForgeConfig",
    result: "CoordinatorResult",
    run_id: str,
) -> str:
    """Classify why summary generation was not entered."""
    if not getattr(getattr(config, "knowledge", None), "run_summaries", False):
        return "disabled"
    if not result.success or result.phase.name != "DONE":
        return "run_not_done"
    if not run_id:
        return "missing_run_id"
    return "already_exists"


def _ensure_runner() -> None:
    global run_agent
    if run_agent is not None:
        return
    import theforge.runners as _r  # noqa: PLC0415

    run_agent = _r.run_agent


def _agent_registry_spec(config: "ForgeConfig", agent: object) -> object | None:
    registry = getattr(config, "model_registry", None) or {}
    registry_id = getattr(agent, "registry_id", None)
    spec = registry.get(registry_id) if registry_id else None
    if spec is None:
        identity = identity_for_agent(agent)
        if identity is not None:
            spec = registry.get(identity.key)
    return spec


def _agent_phase_eligibility(config: "ForgeConfig", agent: object) -> frozenset[str]:
    spec = _agent_registry_spec(config, agent)
    if spec is not None:
        return spec.phase_eligibility
    return DEFAULT_PHASE_ELIGIBILITY


def _apply_summary_transport_fallback(
    config: "ForgeConfig",
    profile: "ModelProfile",
) -> "ModelProfile":
    fallbacks = getattr(config, "transport_fallbacks", None) or {}
    if not fallbacks or profile.api_fallback is not None:
        return profile

    from theforge.config.profiles import _apply_transport_fallback  # noqa: PLC0415

    return _apply_transport_fallback(profile, fallbacks)


def _project_summary_api_profile(profile: "ModelProfile") -> "ModelProfile | None":
    if profile.mode == "api":
        return profile

    fallback = profile.api_fallback
    if fallback is None:
        return None
    return replace(
        profile,
        cli=None,
        provider=fallback.provider,
        transport=fallback.transport(),
        model=fallback.model,
        fallback_models=(),
        timeout_seconds=fallback.timeout_seconds or profile.timeout_seconds,
        base_url=fallback.base_url if fallback.base_url is not None else profile.base_url,
        api_fallback=None,
    )


def _summary_dispatch_profile(
    summary_envelope: "ModelProfile",
    selected: "ModelProfile",
) -> "ModelProfile":
    """Keep the summary role envelope while swapping in the selected model identity."""
    return replace(
        summary_envelope,
        cli=selected.cli,
        provider=selected.provider,
        transport=selected.transport,
        base_url=selected.base_url,
        model=selected.model,
        fallback_models=selected.fallback_models,
        api_fallback=selected.api_fallback,
        registry_id=selected.registry_id,
        registry_source=selected.registry_source,
        reasoning_mode=selected.reasoning_mode,
    )


def _summary_profile(config: "ForgeConfig") -> tuple["ModelProfile | None", str | None]:
    """Resolve a tool-free API profile for the summary agent, or None."""
    from theforge.config.bridge import model_ref_to_profile  # noqa: PLC0415

    knowledge_cfg = getattr(config, "knowledge", None)
    ref = getattr(knowledge_cfg, "ref", None)
    if ref is None:
        ref = getattr(getattr(config, "plan", None), "ref", None)
        if ref is not None and ref.mode != "api":
            return (
                None,
                "knowledge summaries need knowledge.ref when plan.ref uses CLI transport",
            )
    if ref is None:
        return (None, None)

    summary_envelope = model_ref_to_profile(
        "knowledge_summary",
        ref,
        allowed_tools=(),
        phase=PHASE_KNOWLEDGE_SUMMARY,
        sandbox_mode="read-only",
    )
    base_profile = _project_summary_api_profile(summary_envelope)

    agents = getattr(config, "agents", None) or []
    if not agents:
        return (base_profile, None)

    phase_eligible = [
        agent
        for agent in agents
        if PHASE_KNOWLEDGE_SUMMARY in _agent_phase_eligibility(config, agent)
    ]
    if not phase_eligible:
        return (
            None,
            "routing.phase_eligibility excludes knowledge_summary for every configured candidate",
        )

    summary_identity = identity_for_profile(summary_envelope)
    base_identity = identity_for_profile(base_profile) if base_profile is not None else None
    projected: list[ModelProfile] = []
    for agent in phase_eligible:
        candidate = replace(
            agent.to_model_profile(allowed_tools=()),
            phase=PHASE_KNOWLEDGE_SUMMARY,
            sandbox_mode="read-only",
        )
        candidate = _apply_summary_transport_fallback(config, candidate)
        candidate = _project_summary_api_profile(candidate)
        if candidate is not None:
            projected.append(candidate)
    if not projected:
        if base_profile is not None and summary_identity is not None:
            if any(identity_for_agent(agent) == summary_identity for agent in phase_eligible):
                return (base_profile, None)
        return (
            None,
            "no phase-eligible configured model can dispatch knowledge_summary over a tool-free "
            "API transport",
        )

    if base_identity is not None:
        matching = next(
            (
                candidate
                for candidate in projected
                if identity_for_profile(candidate) == base_identity
            ),
            None,
        )
        if matching is not None:
            return (base_profile, None)
    return (_summary_dispatch_profile(summary_envelope, projected[0]), None)


def _should_generate(config: "ForgeConfig", result: "CoordinatorResult", run_id: str) -> bool:
    """Report whether this terminal run is one that gets summarised."""
    if not getattr(getattr(config, "knowledge", None), "run_summaries", False):
        return False
    if not result.success or result.phase.name != "DONE":
        return False
    if not run_id:
        return False
    return not summary_exists(config.project_root, run_id)


def maybe_generate_run_summary(
    config: "ForgeConfig",
    result: "CoordinatorResult",
    audit: dict,
) -> RunSummaryOutcome:
    """Generate and persist this run's knowledge summary; return its outcome.

    Never raises. The run's outcome is unchanged either way, but the audit is
    annotated so operator-facing surfaces can distinguish not-attempted,
    attempted-and-written, and attempted-but-not-written outcomes.
    """
    try:
        run_id = str(audit.get("run_id") or "")
        existing = _existing_summary_outcome(config, run_id, audit)
        if _reuse_existing_outcome(existing):
            return _record_summary_outcome(audit, existing)
        if not _should_generate(config, result, run_id):
            if existing is not None:
                return _record_summary_outcome(audit, existing)
            return _record_summary_outcome(
                audit,
                RunSummaryOutcome(
                    status="not_attempted",
                    attempted=False,
                    written=False,
                    reason=_not_attempted_reason(config, result, run_id),
                ),
            )

        anchors = extract_anchors(audit)
        if anchors.is_empty():
            reason = "run offers no citable evidence"
            _log(f"  ⚠ knowledge summary skipped: {reason}")
            return _record_summary_outcome(
                audit,
                RunSummaryOutcome(
                    status="skipped",
                    attempted=True,
                    written=False,
                    reason=reason,
                ),
            )

        profile, profile_reason = _summary_profile(config)
        if profile is None:
            reason = profile_reason or (
                "no tool-free API transport available for knowledge_summary "
                "(configure knowledge.ref to enable summaries)"
            )
            _log(f"  ⚠ knowledge summary skipped: {reason}")
            return _record_summary_outcome(
                audit,
                RunSummaryOutcome(
                    status="skipped",
                    attempted=True,
                    written=False,
                    reason=reason,
                ),
            )

        _ensure_runner()
        agent_result = run_agent(
            prompt=build_run_summary_prompt(audit, anchors),
            profile=profile,
            working_dir=config.project_root,
            secrets=config.secrets,
            quiet=True,
            plain_text=True,
        )
        if not getattr(agent_result, "success", False):
            reason = "summary agent returned failure"
            _log(f"  ⚠ knowledge summary skipped: {reason}")
            return _record_summary_outcome(
                audit,
                RunSummaryOutcome(
                    status="failed",
                    attempted=True,
                    written=False,
                    reason=reason,
                ),
            )

        proposed = validate_proposed_summary(
            parse_summary_output(getattr(agent_result, "output", "") or ""),
            run_id=run_id,
            anchors=anchors,
        )
        cost_usd = getattr(agent_result, "cost_usd", None)
        artifact = build_summary_artifact(
            proposed,
            audit,
            generation={
                # This spend happens after the run's own cost accounting closed,
                # so it lands in no run ledger. Recording it on the artifact it
                # paid for keeps it visible rather than invisible (#1992's shape).
                "model": profile.model,
                "transport": profile.mode,
                "cost_usd": cost_usd,
            },
        )
        path = write_summary(config.project_root, run_id, artifact)
        _log(f"  ✓ knowledge summary written: {path}")
        return _record_summary_outcome(
            audit,
            RunSummaryOutcome(status="written", attempted=True, written=True, path=path),
        )
    except SummaryValidationError as exc:
        _log(f"  ⚠ knowledge summary rejected: {exc}")
        return _record_summary_outcome(
            audit,
            RunSummaryOutcome(
                status="rejected",
                attempted=True,
                written=False,
                reason=str(exc),
            ),
        )
    except Exception as exc:  # noqa: BLE001 — a side effect must never break a finished run
        _log(f"  ⚠ knowledge summary failed: {exc}")
        return _record_summary_outcome(
            audit,
            RunSummaryOutcome(
                status="failed",
                attempted=True,
                written=False,
                reason=str(exc),
            ),
        )
