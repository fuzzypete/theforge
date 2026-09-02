"""Coordinator control flow for the ``forge triage`` proposal stage.

For each finding in a backlog report this module assembles an evidence packet
(finding body + the report's deterministic evidence + this finding's own
disposition history from the audit substrate), invokes a **fresh** agent to
propose one disposition from the fixed taxonomy, validates the output through
:mod:`theforge.triage_proposal`, and records the proposal and its cost in the
audit substrate.

The control flow stays pure Python. Three decisions are made here and none of
them is delegated to a model:

* **No checkable evidence → no agent.** A packet whose evidence entries are all
  unchecked restatements cannot support any disposition, so the finding resolves
  to ``needs_verification`` deterministically and nothing is spent on it.
* **Invalid output → one retry → ``needs_verification``.** The retry names the
  validator's own errors. Output that is still invalid never becomes a
  disposition; it becomes ``needs_verification`` with the errors recorded.
* **Empty backlog → no runner.** No profile is selected and no agent is
  invoked; the run records an explicit zero cost.

Nothing here writes to a tracker, and that is enforced by what the proposer is
*given*, not by what the prompt asks of it. Every invocation goes through
:func:`seal_proposer_profile` and :func:`proposer_secrets`, so the agent runs
with a read-only tool surface that has no shell, in a read-only sandbox, in an
empty scratch directory rather than the project checkout, holding only
inference credentials (provider API keys or Claude CLI auth tokens). There is
no ``gh`` invocation, no GitHub API call, and no issue mutation on any path —
the stage is advisory, and a later slice owns application.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from theforge.agent_types import COST_PROVIDER_REPORTED, COST_UNKNOWN
from theforge.assignment import NoCapableCandidateError
from theforge.config import TRIAGE_PROPOSER_TOOLS
from theforge.config.auth import check_claude_credentials, inference_only_secrets
from theforge.config.model_identity import PHASE_ADVISOR
from theforge.task.triage_prompts import (
    build_triage_prompt,
    build_triage_punt_review_prompt,
)
from theforge.triage_proposal import (
    DISPOSITION_PUNT,
    PUNT_REVIEW_CHALLENGE,
    FindingPacket,
    FindingProposalResult,
    PacketEvidence,
    ProposalRunSummary,
    PuntReviewStage,
    challenged_punt_review,
    needs_verification_proposal,
    parse_triage_proposal,
    parse_triage_punt_review,
    stable_triage_digest,
)
from theforge.triage_shelved import (
    TriageProposalsShelvedError,
    raise_triage_proposals_shelved,
)

from . import util as _cu

# Reused, not re-derived: choosing a fresh, phase-eligible, tool-capable advisor
# model is the same decision here as at an escalation, and duplicating it would
# let the two drift into disagreeing about which models may advise. Only the
# selector is borrowed — ``run_escalation_advisor`` itself takes a
# ``CoordinatorState`` and prepares a baseline checkout, neither of which a
# standalone triage run has.
from .escalation_advisor_flow import _select_advisor_profile

if TYPE_CHECKING:
    from theforge.config import ForgeConfig
    from theforge.triage_report import BacklogFinding, BacklogReport

_log = _cu._log
_log_verbose = _cu._log_verbose

# ── Lazy runner slots (mirrors escalation_advisor_flow) ───────────────────────
# None until first call; tests may replace before invoking the proposer.
# Patch targets:
#   theforge.coordinator.triage_proposal_flow.run_agent
#   theforge.coordinator.triage_proposal_flow.log_agent_result
run_agent = None
log_agent_result = None

# One retry on schema-invalid or ungrounded output, then the deterministic
# fallback. Two attempts is the whole budget: a proposer that cannot satisfy a
# named validator error on the second try is not going to on the fifth, and
# every extra attempt is spend on a finding the operator will have to look at
# anyway.
MAX_ATTEMPTS = 2

# Reasons a finding resolved to needs_verification without the agent proposing it.
FALLBACK_NO_CHECKABLE_EVIDENCE = (
    "packet carried no checkable evidence — cannot distinguish stale from active"
)
FALLBACK_INVALID_OUTPUT = "agent output failed validation on every attempt"
FALLBACK_AGENT_UNAVAILABLE = "the proposer agent could not be invoked"
FALLBACK_REVIEW_INVALID_OUTPUT = "reviewer output failed validation on every attempt"
FALLBACK_REVIEWER_UNAVAILABLE = "the reviewer agent could not be invoked"
TRIAGE_CREDENTIAL_SOURCE = "shell environment overlaid by project .forge/.env secrets"


def _ensure_runners() -> None:
    global run_agent, log_agent_result
    if run_agent is not None and log_agent_result is not None:
        return
    import theforge.runners as _r  # noqa: PLC0415

    if run_agent is None:
        run_agent = _r.run_agent
    if log_agent_result is None:
        log_agent_result = _r.log_agent_result


def _history_evidence(history: list[dict]) -> tuple[PacketEvidence, ...]:
    """Turn recorded disposition rows into citable, checkable evidence.

    A registry row is a fact about what forge did, not a claim about the code,
    so it is checkable — which is what lets "the registry holds no disposition
    rows for this finding" be evidence a proposal can actually cite.
    """
    if not history:
        return ()
    rendered = "; ".join(
        f"{row.get('emitted_at', '?')} → {row.get('disposition', '?')}"
        f"{' ' + row['target_milestone'] if row.get('target_milestone') else ''}"
        f"{' (' + row['punt_reason_code'] + ')' if row.get('punt_reason_code') else ''}"
        for row in history
    )
    return (
        PacketEvidence(
            evidence_id="disposition-history",
            kind="disposition_history",
            summary=f"{len(history)} prior disposition row(s) recorded for this finding",
            checkable=True,
            detail=rendered,
        ),
    )


def _load_disposition_history(project_root: "Path", finding_id: str) -> list[dict]:
    """Read this finding's prior proposals, degrading to empty on a missing substrate."""
    from . import audit_read_model, audit_storage  # noqa: PLC0415

    try:
        conn = audit_storage.open_readonly(project_root)
    except audit_storage.SubstrateError:
        return []
    try:
        return audit_read_model.triage_disposition_history(conn, finding_id)
    except Exception:  # noqa: BLE001 - history is context, never a gate
        return []
    finally:
        conn.close()


def build_finding_packet(
    finding: "BacklogFinding",
    report: "BacklogReport",
    *,
    project_root: "Path",
    current_milestone: str | None = None,
) -> FindingPacket:
    """Assemble one finding's evidence packet.

    ``current_milestone`` overrides the report's own value (the CLI flag). When
    neither names one, ``fix_now`` is simply unavailable for this packet rather
    than proposable against an unnameable target.
    """
    history = _load_disposition_history(project_root, finding.finding_id)
    return FindingPacket(
        finding_id=finding.finding_id,
        issue_ref=finding.issue_ref,
        finding_body=finding.body,
        evidence=(*finding.evidence, *_history_evidence(history)),
        current_milestone=current_milestone or report.current_milestone,
        named_milestones=report.named_milestones,
        disposition_history=tuple(history),
    )


def _combine_cost(
    total: float | None, provenance: str, result: object
) -> tuple[float | None, str]:
    """Fold one agent result's cost into a running total and its provenance.

    An unmeasured attempt taints the total: the number that survives is a lower
    bound, and saying so is the difference between "this run cost $0.02" and
    "this run cost at least $0.02 and at least one attempt reported nothing".
    """
    cost = getattr(result, "cost_usd", None)
    attempt_provenance = str(getattr(result, "cost_provenance", COST_UNKNOWN) or COST_UNKNOWN)
    if cost is None:
        return total, COST_UNKNOWN
    running = (total or 0.0) + float(cost)
    if provenance == COST_UNKNOWN:
        return running, COST_UNKNOWN
    return running, attempt_provenance


def seal_proposer_profile(profile: object) -> object:
    """Return ``profile`` narrowed to what an advisory proposer may hold.

    The proposal stage is advisory, so its inability to write anywhere has to be
    a property of the invocation rather than of the prompt. Three things are
    overridden here, at the one place every proposer invocation passes through,
    rather than trusted from whatever built the profile:

    * ``allowed_tools`` becomes :data:`TRIAGE_PROPOSER_TOOLS` — read-only, and
      specifically without a shell, which is the capability ``gh issue edit``
      would need. It is set, never filtered: an empty ``allowed_tools`` means
      *unrestricted* at CLI dispatch, so a surface derived by subtraction from
      config would fail open on exactly its most dangerous input.
    * ``sandbox_mode`` becomes ``read-only`` so the host sandbox refuses a write
      even if a tool were somehow granted.
    * ``phase``/``name`` identify the invocation as triage in the audit trail.

    A model whose configured profile granted Bash therefore cannot bring it
    here, which is what makes the no-tracker-writes guarantee mechanical.
    """
    return replace(
        profile,
        name="triage_proposer",
        phase=PHASE_ADVISOR,
        allowed_tools=TRIAGE_PROPOSER_TOOLS,
        sandbox_mode="read-only",
    )


def seal_reviewer_profile(profile: object) -> object:
    """Return ``profile`` narrowed to what an advisory punt reviewer may hold."""
    return replace(
        profile,
        name="triage_punt_reviewer",
        phase=PHASE_ADVISOR,
        allowed_tools=TRIAGE_PROPOSER_TOOLS,
        sandbox_mode="read-only",
    )


#: The credentials a proposer invocation may hold: inference keys only, never a
#: tracker token. The allow-list lives in ``config.auth`` beside the credential
#: vocabulary it is derived from, because the preflight decomposition assessment
#: needs the same answer and there must be only one.
proposer_secrets = inference_only_secrets


def _triage_auth_failure(reason: str) -> str:
    """Render the one run-level Claude auth failure triage should report.

    Triage checks the exact environment it would dispatch with, so the operator
    message needs to name that resolution path explicitly rather than reading as
    though the OAuth store were the only credential source that mattered.
    """
    return (
        "triage aborted agent dispatch before any proposer ran: the selected "
        "Claude profile could not authenticate with the same proposer "
        f"environment triage would pass to the CLI ({TRIAGE_CREDENTIAL_SOURCE}). "
        f"{reason}"
    )


def _check_triage_dispatch_readiness(profile: object, *, secrets: dict[str, str]) -> str:
    """Return the run-level auth failure, or ``\"\"`` when dispatch is ready."""
    if getattr(profile, "cli", None) != "claude":
        return ""
    ready, reason = check_claude_credentials({**os.environ, **secrets})
    if ready:
        return ""
    return _triage_auth_failure(reason)


def _propose_for_packet(
    packet: FindingPacket,
    *,
    profile: object,
    working_dir: "Path",
    secrets: dict[str, str],
) -> FindingProposalResult:
    """Run the proposer against one packet, with one retry and the fallback."""
    errors: list[str] = []
    total_cost: float | None = None
    provenance = COST_PROVIDER_REPORTED
    attempts = 0

    for attempt in range(MAX_ATTEMPTS):
        prompt = build_triage_prompt(packet, previous_errors=errors)
        attempts += 1
        try:
            result = run_agent(
                prompt=prompt,
                profile=profile,
                working_dir=working_dir,
                secrets=secrets,
            )
        except Exception as exc:  # noqa: BLE001 - an unusable proposer is not a disposition
            errors = [f"proposer invocation failed: {exc}"]
            _log(f"  ⚠ triage proposer invocation failed for {packet.finding_id}: {exc}")
            return _fallback_result(
                packet,
                attempts=attempts,
                errors=errors,
                reason=FALLBACK_AGENT_UNAVAILABLE,
                cost_usd=total_cost,
                provenance=provenance,
            )

        log_agent_result(result, "TRIAGE_PROPOSER")
        total_cost, provenance = _combine_cost(total_cost, provenance, result)

        if not getattr(result, "success", False):
            errors = ["proposer agent returned failure before a usable proposal"]
            continue

        proposal = parse_triage_proposal(getattr(result, "output", "") or "", packet)
        if proposal.ok:
            return FindingProposalResult(
                finding_id=packet.finding_id,
                issue_ref=packet.issue_ref,
                packet_hash=packet.packet_hash(),
                proposal=proposal,
                attempts=attempts,
                retry_count=attempt,
                cost_usd=total_cost,
                cost_provenance=provenance,
            )
        errors = list(proposal.parse_errors)
        _log_verbose(f"  triage proposal rejected for {packet.finding_id}: {errors}")

    return _fallback_result(
        packet,
        attempts=attempts,
        errors=errors,
        reason=FALLBACK_INVALID_OUTPUT,
        cost_usd=total_cost,
        provenance=provenance,
    )


def _review_punt_result(
    packet: FindingPacket,
    result: FindingProposalResult,
    *,
    profile: object,
    working_dir: "Path",
    secrets: dict[str, str],
) -> FindingProposalResult:
    """Run the adversarial reviewer against one accepted punt proposal."""
    errors: list[str] = []
    total_cost: float | None = None
    provenance = COST_PROVIDER_REPORTED
    attempts = 0

    for attempt in range(MAX_ATTEMPTS):
        prompt = build_triage_punt_review_prompt(packet, result.proposal, previous_errors=errors)
        attempts += 1
        try:
            review_result = run_agent(
                prompt=prompt,
                profile=profile,
                working_dir=working_dir,
                secrets=secrets,
            )
        except Exception as exc:  # noqa: BLE001 - reviewer failure challenges safely
            errors = [f"reviewer invocation failed: {exc}"]
            _log(f"  ⚠ triage punt reviewer invocation failed for {packet.finding_id}: {exc}")
            return _review_fallback_result(
                result,
                attempts=attempts,
                errors=errors,
                reason=FALLBACK_REVIEWER_UNAVAILABLE,
                cost_usd=total_cost,
                provenance=provenance,
            )

        log_agent_result(review_result, "TRIAGE_PUNT_REVIEWER")
        total_cost, provenance = _combine_cost(total_cost, provenance, review_result)

        if not getattr(review_result, "success", False):
            errors = ["reviewer agent returned failure before a usable review"]
            continue

        review = parse_triage_punt_review(getattr(review_result, "output", "") or "", packet)
        if review.ok:
            return replace(
                result,
                punt_review=review,
                review_attempts=attempts,
                review_retry_count=attempt,
                review_cost_usd=total_cost,
                review_cost_provenance=provenance,
            )
        errors = list(review.parse_errors)
        _log_verbose(f"  triage punt review rejected for {packet.finding_id}: {errors}")

    return _review_fallback_result(
        result,
        attempts=attempts,
        errors=errors,
        reason=FALLBACK_REVIEW_INVALID_OUTPUT,
        cost_usd=total_cost,
        provenance=provenance,
    )


def _fallback_result(
    packet: FindingPacket,
    *,
    attempts: int,
    errors: list[str],
    reason: str,
    cost_usd: float | None,
    provenance: str,
) -> FindingProposalResult:
    """The needs_verification a finding gets when no valid proposal survived."""
    return FindingProposalResult(
        finding_id=packet.finding_id,
        issue_ref=packet.issue_ref,
        packet_hash=packet.packet_hash(),
        proposal=needs_verification_proposal(
            packet,
            basis=(
                "No valid proposal survived validation for this packet; the "
                f"disposition is withheld rather than guessed ({reason})."
            ),
        ),
        attempts=attempts,
        retry_count=max(attempts - 1, 0),
        validation_errors=tuple(errors),
        fallback_reason=reason,
        cost_usd=cost_usd,
        cost_provenance=provenance,
    )


def _review_fallback_result(
    result: FindingProposalResult,
    *,
    attempts: int,
    errors: list[str],
    reason: str,
    cost_usd: float | None,
    provenance: str,
) -> FindingProposalResult:
    """Attach the deterministic challenged review when no valid review survived."""
    return replace(
        result,
        punt_review=challenged_punt_review(
            basis=(
                "No valid adversarial review survived validation for this punt; "
                f"it is challenged rather than presented as clean ({reason})."
            )
        ),
        review_attempts=attempts,
        review_retry_count=max(attempts - 1, 0),
        review_validation_errors=tuple(errors),
        review_fallback_reason=reason,
        review_cost_usd=cost_usd,
        review_cost_provenance=provenance,
    )


def _no_evidence_result(packet: FindingPacket) -> FindingProposalResult:
    """The deterministic needs_verification for a packet with nothing checkable.

    Costs a measured ``0.0`` — no agent ran — rather than an unmeasured None,
    because "nothing was spent here" is a fact this path actually knows.
    """
    return FindingProposalResult(
        finding_id=packet.finding_id,
        issue_ref=packet.issue_ref,
        packet_hash=packet.packet_hash(),
        proposal=needs_verification_proposal(
            packet,
            basis=(
                "No checkable artifact is cited for this finding; stale and active "
                "are indistinguishable from this packet."
            ),
        ),
        attempts=0,
        retry_count=0,
        fallback_reason=FALLBACK_NO_CHECKABLE_EVIDENCE,
        cost_usd=0.0,
        cost_provenance=COST_PROVIDER_REPORTED,
    )


def _sum_cost_leg(
    total: float,
    provenance: str,
    *,
    include: bool,
    cost_usd: float | None,
    cost_provenance: str,
) -> tuple[float, str]:
    if not include:
        return total, provenance
    if cost_usd is None:
        return total, COST_UNKNOWN
    total += cost_usd
    if cost_provenance == COST_UNKNOWN:
        provenance = COST_UNKNOWN
    return total, provenance


def _total_spend(results: list[FindingProposalResult]) -> tuple[float | None, str]:
    """Sum per-finding spend, keeping an unmeasured finding visible in the provenance."""
    total = 0.0
    provenance = COST_PROVIDER_REPORTED
    for result in results:
        total, provenance = _sum_cost_leg(
            total,
            provenance,
            include=True,
            cost_usd=result.cost_usd,
            cost_provenance=result.cost_provenance,
        )
        total, provenance = _sum_cost_leg(
            total,
            provenance,
            include=(
                result.punt_review is not None
                or result.review_attempts > 0
                or bool(result.review_fallback_reason)
            ),
            cost_usd=result.review_cost_usd,
            cost_provenance=result.review_cost_provenance,
        )
    return total, provenance


def _review_stage(results: list[FindingProposalResult]) -> PuntReviewStage:
    punts = [result for result in results if result.proposal.disposition == DISPOSITION_PUNT]
    if not punts:
        return PuntReviewStage()
    challenged = sum(
        1
        for result in punts
        if result.punt_review is not None and result.punt_review.verdict == PUNT_REVIEW_CHALLENGE
    )
    return PuntReviewStage(
        reviewed_punt_count=len(punts),
        challenged_punt_count=challenged,
        no_op=False,
    )


def _record_run(
    project_root: "Path",
    summary: ProposalRunSummary,
    *,
    packets_by_id: dict[str, FindingPacket] | None = None,
    findings_by_id: dict[str, "BacklogFinding"] | None = None,
) -> str:
    """Persist the run summary and every per-finding proposal. Returns an error string."""
    from . import audit_storage  # noqa: PLC0415

    try:
        for result in summary.results:
            event = result.to_dict()
            event["triage_run_id"] = summary.triage_run_id
            event["disposition"] = result.proposal.disposition
            event["target_milestone"] = result.proposal.target_milestone
            event["punt_reason_code"] = result.proposal.punt_reason_code
            event["evidence_refs"] = list(result.proposal.evidence_refs)
            if packets_by_id is not None and result.finding_id in packets_by_id:
                event["packet"] = packets_by_id[result.finding_id].to_dict()
            if findings_by_id is not None and result.finding_id in findings_by_id:
                snapshot = findings_by_id[result.finding_id].snapshot_dict()
                event["finding_snapshot"] = snapshot
                event["finding_snapshot_digest"] = stable_triage_digest(snapshot)
            audit_storage.record_triage_proposal_event(project_root, event)
        audit_storage.record_triage_proposal_run(project_root, summary.to_dict())
    except Exception as exc:  # noqa: BLE001 - report the audit gap, don't lose the output
        _log(f"  ⚠ triage audit write failed: {exc}")
        return str(exc)
    return ""


def run_triage_proposals(
    report: "BacklogReport",
    config: "ForgeConfig",
    *,
    project_root: "Path | None" = None,
    current_milestone: str | None = None,
    record: bool = True,
) -> ProposalRunSummary:
    """Reject the shelved public proposal entry point (ADR-0010)."""

    raise_triage_proposals_shelved()


def _run_triage_proposals_impl(
    report: "BacklogReport",
    config: "ForgeConfig",
    *,
    project_root: "Path | None" = None,
    current_milestone: str | None = None,
    record: bool = True,
) -> ProposalRunSummary:
    """Propose a disposition for every finding in ``report``.

    Returns a :class:`ProposalRunSummary` carrying one result per finding plus
    the run's total spend. Never mutates a tracker; never applies a proposal.
    """
    root = project_root or config.project_root
    triage_run_id = uuid.uuid4().hex[:12]

    if not report.findings:
        # Empty backlog: no profile selection, no runner, an explicit zero.
        summary = ProposalRunSummary(
            results=(),
            total_cost_usd=0.0,
            cost_provenance=COST_PROVIDER_REPORTED,
            triage_run_id=triage_run_id,
            report_path=report.source_path,
            review_stage=PuntReviewStage(),
        )
        if record:
            summary = replace(summary, audit_error=_record_run(root, summary))
        return summary

    packets = [
        build_finding_packet(
            finding, report, project_root=root, current_milestone=current_milestone
        )
        for finding in report.findings
    ]
    packets_by_id = {packet.finding_id: packet for packet in packets}
    findings_by_id = {finding.finding_id: finding for finding in report.findings}

    proposer_profile: object | None = None
    reviewer_profile: object | None = None
    profile_error = ""
    run_level_failure = ""
    results: list[FindingProposalResult] = []
    secrets = proposer_secrets(config.secrets)
    checkable_packets = [packet for packet in packets if packet.has_checkable_evidence()]

    if checkable_packets:
        try:
            base_profile = _select_advisor_profile(config)
            proposer_profile = seal_proposer_profile(base_profile)
            reviewer_profile = seal_reviewer_profile(base_profile)
            run_level_failure = _check_triage_dispatch_readiness(proposer_profile, secrets=secrets)
            if run_level_failure:
                _log(f"  ⚠ {run_level_failure}")
        except (NoCapableCandidateError, ValueError) as exc:
            profile_error = str(exc)
            _log(f"  ⚠ triage proposer unavailable: {exc}")

    # The proposer runs in an empty scratch directory, never the project
    # checkout. Its packet is the record and the prompt forbids investigation;
    # handing it the repository anyway would mean the one thing standing between
    # an advisory agent and the working tree was the prompt. The directory is
    # removed when the run ends.
    with tempfile.TemporaryDirectory(prefix="forge-triage-") as scratch:
        working_dir = Path(scratch)
        if proposer_profile is not None and not run_level_failure:
            _ensure_runners()
        for packet in packets:
            if not packet.has_checkable_evidence():
                results.append(_no_evidence_result(packet))
                continue
            if proposer_profile is None:
                results.append(
                    _fallback_result(
                        packet,
                        attempts=0,
                        errors=[profile_error],
                        reason=FALLBACK_AGENT_UNAVAILABLE,
                        cost_usd=0.0,
                        provenance=COST_PROVIDER_REPORTED,
                    )
                )
                continue
            if run_level_failure:
                results.append(
                    _fallback_result(
                        packet,
                        attempts=0,
                        errors=[],
                        reason=FALLBACK_AGENT_UNAVAILABLE,
                        cost_usd=0.0,
                        provenance=COST_PROVIDER_REPORTED,
                    )
                )
                continue
            results.append(
                _propose_for_packet(
                    packet,
                    profile=proposer_profile,
                    working_dir=working_dir,
                    secrets=secrets,
                )
            )

        for index, result in enumerate(results):
            if result.proposal.disposition != DISPOSITION_PUNT:
                continue
            if reviewer_profile is None:
                results[index] = _review_fallback_result(
                    result,
                    attempts=0,
                    errors=[profile_error or FALLBACK_REVIEWER_UNAVAILABLE],
                    reason=FALLBACK_REVIEWER_UNAVAILABLE,
                    cost_usd=0.0,
                    provenance=COST_PROVIDER_REPORTED,
                )
                continue
            results[index] = _review_punt_result(
                packets[index],
                result,
                profile=reviewer_profile,
                working_dir=working_dir,
                secrets=secrets,
            )

    total, provenance = _total_spend(results)
    summary = ProposalRunSummary(
        results=tuple(results),
        total_cost_usd=total,
        cost_provenance=provenance,
        triage_run_id=triage_run_id,
        report_path=report.source_path,
        review_stage=_review_stage(results),
        run_level_failure=run_level_failure,
    )
    if record:
        summary = replace(
            summary,
            audit_error=_record_run(
                root,
                summary,
                packets_by_id=packets_by_id,
                findings_by_id=findings_by_id,
            ),
        )
    return summary


__all__ = [
    "MAX_ATTEMPTS",
    "TriageProposalsShelvedError",
    "_run_triage_proposals_impl",
    "build_finding_packet",
    "proposer_secrets",
    "run_triage_proposals",
    "seal_proposer_profile",
    "seal_reviewer_profile",
]
