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
empty scratch directory rather than the project checkout, holding only provider
API keys. There is no ``gh`` invocation, no GitHub API call, and no issue
mutation on any path — the stage is advisory, and a later slice owns
application.
"""

from __future__ import annotations

import tempfile
import uuid
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from theforge.agent_types import COST_PROVIDER_REPORTED, COST_UNKNOWN
from theforge.assignment import NoCapableCandidateError
from theforge.config import TRIAGE_PROPOSER_TOOLS
from theforge.config.defaults import PROVIDER_API_KEY_MAP
from theforge.config.model_identity import PHASE_ADVISOR
from theforge.task.triage_prompts import build_triage_prompt
from theforge.triage_proposal import (
    FindingPacket,
    FindingProposalResult,
    PacketEvidence,
    ProposalRunSummary,
    needs_verification_proposal,
    parse_triage_proposal,
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
    from collections.abc import Mapping

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


def proposer_secrets(secrets: "Mapping[str, str] | None") -> dict[str, str]:
    """Return only the credentials a proposer invocation legitimately needs.

    An ALLOW-list, and the distinction from a deny-list is the point: denying
    ``GH_TOKEN`` answers "is today's known tracker credential absent?", while
    allowing only provider API keys answers "is every credential this stage
    holds one that cannot mutate a tracker?" Only the second survives an
    operator putting a new forge-of-record credential in ``.forge/.env``.

    A key qualifies if it is a provider API key by name. Everything else —
    tracker tokens, webhook URLs, deploy credentials — is dropped, so an
    advisory stage cannot authenticate as the operator against anything.
    """
    if not secrets:
        return {}
    known = {value.upper() for value in PROVIDER_API_KEY_MAP.values()}
    return {
        key: value
        for key, value in secrets.items()
        if key.upper() in known or key.upper().endswith("_API_KEY")
    }


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


def _total_spend(results: list[FindingProposalResult]) -> tuple[float | None, str]:
    """Sum per-finding spend, keeping an unmeasured finding visible in the provenance."""
    total = 0.0
    provenance = COST_PROVIDER_REPORTED
    for result in results:
        if result.cost_usd is None:
            provenance = COST_UNKNOWN
            continue
        total += result.cost_usd
        if result.cost_provenance == COST_UNKNOWN:
            provenance = COST_UNKNOWN
    return total, provenance


def _record_run(project_root: "Path", summary: ProposalRunSummary) -> str:
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

    profile: object | None = None
    profile_error = ""
    results: list[FindingProposalResult] = []
    secrets = proposer_secrets(config.secrets)

    # The proposer runs in an empty scratch directory, never the project
    # checkout. Its packet is the record and the prompt forbids investigation;
    # handing it the repository anyway would mean the one thing standing between
    # an advisory agent and the working tree was the prompt. The directory is
    # removed when the run ends.
    with tempfile.TemporaryDirectory(prefix="forge-triage-") as scratch:
        working_dir = Path(scratch)
        for packet in packets:
            if not packet.has_checkable_evidence():
                results.append(_no_evidence_result(packet))
                continue
            if profile is None and not profile_error:
                _ensure_runners()
                try:
                    profile = seal_proposer_profile(_select_advisor_profile(config))
                except (NoCapableCandidateError, ValueError) as exc:
                    profile_error = str(exc)
                    _log(f"  ⚠ triage proposer unavailable: {exc}")
            if profile is None:
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
            results.append(
                _propose_for_packet(
                    packet,
                    profile=profile,
                    working_dir=working_dir,
                    secrets=secrets,
                )
            )

    total, provenance = _total_spend(results)
    summary = ProposalRunSummary(
        results=tuple(results),
        total_cost_usd=total,
        cost_provenance=provenance,
        triage_run_id=triage_run_id,
        report_path=report.source_path,
    )
    if record:
        summary = replace(summary, audit_error=_record_run(root, summary))
    return summary


__all__ = [
    "MAX_ATTEMPTS",
    "build_finding_packet",
    "proposer_secrets",
    "run_triage_proposals",
    "seal_proposer_profile",
]
