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
* **Exactly once per generation input.** Several terminal seams write a
  finished run's audit. Generation is guarded on what the persisted artifact
  records being generated from, so a run that reaches more than one of them is
  summarised once and billed once — while a run re-entered under the same
  run_id with different material is still allowed a fresh attempt (#2520).

Schema/validation lives in ``theforge.knowledge_summary``; prompt construction
lives in ``theforge.task.summary_prompts``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from theforge.config.auth import check_agent_auth
from theforge.config.model_identity import DEFAULT_PHASE_ELIGIBILITY, PHASE_KNOWLEDGE_SUMMARY
from theforge.knowledge_index import rebuild_knowledge_index
from theforge.knowledge_summary import (
    SummaryValidationError,
    build_summary_artifact,
    extract_anchors,
    parse_summary_output,
    summary_exists,
    summary_generation_input_digest,
    validate_proposed_summary,
    write_summary,
)
from theforge.model_capabilities import identity_for_agent
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
class KnowledgeIndexMaintenanceOutcome:
    """Outcome of rebuilding the derived knowledge index after a summary write."""

    status: str
    reason: str | None = None
    path: "Path | None" = None

    def to_audit_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"status": self.status}
        if self.reason:
            payload["reason"] = self.reason
        if self.path is not None:
            payload["path"] = str(self.path)
        return payload

    @classmethod
    def from_audit_dict(cls, payload: object) -> "KnowledgeIndexMaintenanceOutcome | None":
        if not isinstance(payload, dict):
            return None

        status = payload.get("status")
        if not isinstance(status, str):
            return None

        reason = payload.get("reason")
        if reason is not None and not isinstance(reason, str):
            reason = None

        path_value = payload.get("path")
        path = path_value if isinstance(path_value, Path) else None
        if isinstance(path_value, str) and path_value:
            path = Path(path_value)

        return cls(status=status, reason=reason, path=path)


@dataclass(frozen=True)
class RunSummaryOutcome:
    """Outcome of post-DONE knowledge summary generation for one run."""

    status: str
    attempted: bool
    written: bool
    reason: str | None = None
    path: "Path | None" = None
    index_rebuild: KnowledgeIndexMaintenanceOutcome | None = None
    # Digest of the exact generation input this outcome was reached from — the
    # rendered summary prompt, which is the whole of what a dispatch would see.
    # It is what tells a repeated terminal write for an unchanged run apart from
    # a genuine re-entry carrying different material, so the first reuses the
    # outcome and the second is allowed a fresh attempt (#2520). ``None`` means
    # "this outcome cannot say what it was generated from" — a pre-digest record
    # — and is never backfilled from a later payload, which would assert a match
    # the outcome never made.
    generation_input_digest: str | None = None

    def to_audit_dict(self) -> dict:
        payload: dict[str, object] = {
            "status": self.status,
            "attempted": self.attempted,
            "written": self.written,
        }
        if self.generation_input_digest:
            payload["generation_input_digest"] = self.generation_input_digest
        if self.reason:
            payload["reason"] = self.reason
        if self.path is not None:
            payload["path"] = str(self.path)
        if self.index_rebuild is not None:
            payload["index_rebuild"] = self.index_rebuild.to_audit_dict()
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

        index_rebuild = KnowledgeIndexMaintenanceOutcome.from_audit_dict(
            payload.get("index_rebuild")
        )

        digest = payload.get("generation_input_digest")
        if not isinstance(digest, str) or not digest:
            digest = None

        return cls(
            status=status,
            attempted=attempted,
            written=written,
            reason=reason,
            path=path,
            index_rebuild=index_rebuild,
            generation_input_digest=digest,
        )


def _generation_input_digest(audit: dict) -> str:
    """Return a stable digest of exactly what a dispatch for this run would see.

    The digest is taken over the rendered prompt rather than over the anchor
    labels alone. The anchors are only the *citable* references; the prompt also
    carries the story text, the plan steps, the finding descriptions, the review
    cycles and the run signals — material that can change substantively while
    every id, path and ref stays identical. Digesting the prompt is therefore
    the honest question: is a second dispatch the same dispatch?

    ``build_run_summary_prompt`` is a pure function of the audit and its
    anchors, and the run's own cost accounting is closed before a summary is
    generated (the summary's spend lands on the artifact, not in the run
    ledger), so the sprint's repeated terminal writes for one unchanged story
    render byte-identical prompts.
    """
    material = build_run_summary_prompt(audit, extract_anchors(audit))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _record_summary_outcome(audit: dict, outcome: RunSummaryOutcome) -> RunSummaryOutcome:
    """Persist an outcome *this call produced* onto the audit payload in place.

    Only outcomes reached by this call come through here, and they are stamped
    with the generation input they were reached from. A previously-recorded
    outcome being carried onto a fresh audit payload goes through
    :func:`_echo_existing_outcome` instead, so a pre-digest record is never
    backfilled with today's digest — that would have it assert it matched a
    generation input it was never compared against.
    """
    if outcome.generation_input_digest is None:
        outcome = replace(outcome, generation_input_digest=_generation_input_digest(audit))
    audit["knowledge_summary"] = outcome.to_audit_dict()
    return outcome


def _echo_existing_outcome(audit: dict, outcome: RunSummaryOutcome) -> RunSummaryOutcome:
    """Carry a previously-recorded outcome onto this audit payload, verbatim."""
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


def _reuse_existing_outcome(outcome: RunSummaryOutcome | None, digest: str) -> bool:
    """Report whether a durable prior outcome describes *this* dispatch.

    A prior outcome stands in for a fresh attempt only when it was reached from
    the same generation input — which is what one sprint's repeated terminal
    writes for a finished story are, and what a run re-entered under the same
    run_id with different material is not (#2520). An outcome recorded before
    the digest existed carries ``None`` and therefore never matches; it cannot
    say what it was generated from, and the caller resolves that case against
    whether a summary artifact actually exists rather than by assuming.
    """
    if outcome is None or outcome.generation_input_digest is None:
        return False
    if not (outcome.attempted or outcome.written):
        return False
    return outcome.generation_input_digest == digest


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


def _summary_auth_reason(config: "ForgeConfig", profile: "ModelProfile") -> str | None:
    """Return the missing-auth reason for ``profile``, if any."""
    ready, reason = check_agent_auth(
        profile,
        config.secrets,
        include_sandbox_readiness=False,
    )
    if ready:
        return None
    return reason


def _summary_profile(config: "ForgeConfig") -> tuple["ModelProfile | None", str | None]:
    """Resolve the tool-free API summary profile after eligibility checks.

    Model selection is authoritative from ``knowledge.ref`` when present,
    otherwise from the inherited ``plan.ref``. The configured agent pool only
    gates whether ``knowledge_summary`` is allowed to run at all; it never
    supplies or swaps the role's identity.
    """
    from theforge.config.bridge import model_ref_to_profile  # noqa: PLC0415

    knowledge_cfg = getattr(config, "knowledge", None)
    knowledge_ref = getattr(knowledge_cfg, "ref", None)
    ref = knowledge_ref
    if ref is None:
        ref = getattr(getattr(config, "plan", None), "ref", None)
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
    if knowledge_ref is None and ref.mode != "api" and base_profile is None:
        return (
            None,
            "knowledge summaries need knowledge.ref or transport_fallback "
            "when plan.ref uses CLI transport",
        )
    if knowledge_ref is not None:
        return (base_profile, None)

    agents = getattr(config, "agents", None) or []
    if not agents:
        return (base_profile, None)

    # A configured pool can veto this role entirely, but it does not participate
    # in model selection once the summary profile has been derived above.
    any_phase_eligible = any(
        PHASE_KNOWLEDGE_SUMMARY in _agent_phase_eligibility(config, agent) for agent in agents
    )
    if not any_phase_eligible:
        return (
            None,
            "routing.phase_eligibility excludes knowledge_summary for every configured candidate",
        )

    return (base_profile, None)


def _is_eligible_run(config: "ForgeConfig", result: "CoordinatorResult", run_id: str) -> bool:
    """Report whether this terminal run is one that gets summarised at all.

    Deliberately does *not* consult the persisted summary artifact. Whether a
    summary already exists answers "has this generation input been summarised?",
    which is the digest comparison's question, not "is this the kind of run we
    summarise?" — conflating them is what let an artifact written from earlier
    evidence permanently disqualify a run that re-entered and did more (#2520).
    """
    if not getattr(getattr(config, "knowledge", None), "run_summaries", False):
        return False
    if not result.success or result.phase.name != "DONE":
        return False
    return bool(run_id)


def _should_generate(config: "ForgeConfig", result: "CoordinatorResult", run_id: str) -> bool:
    """Report whether this terminal run is one that gets summarised."""
    return _is_eligible_run(config, result, run_id) and not summary_exists(
        config.project_root, run_id
    )


def _refresh_knowledge_index(project_root: Path) -> KnowledgeIndexMaintenanceOutcome:
    """Rebuild the derived knowledge index after summary persistence."""
    try:
        result = rebuild_knowledge_index(project_root)
    except Exception as exc:  # noqa: BLE001 - post-write maintenance must not flip the run outcome
        return KnowledgeIndexMaintenanceOutcome(status="failed", reason=str(exc))
    return KnowledgeIndexMaintenanceOutcome(status="rebuilt", path=result.path)


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
        # Eligibility for the run in front of us is consulted first, and it is
        # eligibility only — not "has a summary ever been written for this
        # run_id". A durable prior outcome stands in for a fresh attempt only
        # once the run is eligible AND the outcome was reached from this same
        # generation input; otherwise a record made from earlier material
        # silently blocks a re-entered run forever (#2520).
        if not _is_eligible_run(config, result, run_id):
            if existing is not None:
                return _echo_existing_outcome(audit, existing)
            return _record_summary_outcome(
                audit,
                RunSummaryOutcome(
                    status="not_attempted",
                    attempted=False,
                    written=False,
                    reason=_not_attempted_reason(config, result, run_id),
                ),
            )

        digest = _generation_input_digest(audit)
        if summary_generation_input_digest(config.project_root, run_id) == digest:
            # The artifact on disk records being generated from exactly this
            # input, so this write is a repeat of the one that produced it. The
            # artifact is asked rather than the outcome because it is the thing
            # that persists: a run record that was never mirrored leaves no
            # outcome to consult, and that gap is what let a later repeat
            # re-dispatch and overwrite a summary that was already correct.
            if existing is not None:
                return _echo_existing_outcome(audit, existing)
            return _record_summary_outcome(
                audit,
                RunSummaryOutcome(
                    status="not_attempted",
                    attempted=False,
                    written=False,
                    reason=_not_attempted_reason(config, result, run_id),
                ),
            )

        if not summary_exists(config.project_root, run_id) and _reuse_existing_outcome(
            existing, digest
        ):
            # No artifact was written, and the recorded outcome says this same
            # input was already attempted: one of the sprint's repeated terminal
            # writes for a story whose summary attempt did not produce one.
            return _echo_existing_outcome(audit, existing)

        # Everything else generates. That deliberately includes an artifact
        # whose ``generation.input_digest`` is absent (written before the digest
        # existed) or different from this run's: neither can say the artifact
        # still describes the run in front of us, and a summary that may be
        # stale is exactly what a re-entered run needs regenerated (#2520). The
        # attempt is bounded — the artifact it writes carries the digest, so the
        # next repeat of this same input reuses it.
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
        auth_reason = _summary_auth_reason(config, profile)
        if auth_reason is not None:
            reason = f"summary API credential missing: {auth_reason}"
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
                # What this summary was generated from. The artifact is the
                # durable record of its own provenance, so a later write can ask
                # it whether the run has changed since (#2520).
                "input_digest": digest,
            },
        )
        path = write_summary(config.project_root, run_id, artifact)
        index_rebuild = _refresh_knowledge_index(config.project_root)
        if index_rebuild.status == "failed":
            _log(f"  ⚠ knowledge index rebuild failed: {index_rebuild.reason}")
        _log(f"  ✓ knowledge summary written: {path}")
        return _record_summary_outcome(
            audit,
            RunSummaryOutcome(
                status="written",
                attempted=True,
                written=True,
                path=path,
                index_rebuild=index_rebuild,
            ),
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
