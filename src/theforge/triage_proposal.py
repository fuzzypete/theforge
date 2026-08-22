"""Backlog triage: fixed disposition taxonomy, evidence packet, and proposal parsing.

``forge triage`` reads a backlog report and, for each finding in it, asks a
fresh-context agent to propose exactly one **disposition** from a fixed
taxonomy. This module is the pure-data + integrity boundary for that: the
taxonomy and its required payloads, the evidence packet a proposal must be
grounded in, and the parser/validator that decides whether agent output is a
usable proposal at all.

It is the escalation advisor's shape (``theforge.escalation_advisor``) applied
to the backlog rather than to one escalated story, and the same three-way split
holds: prompt construction lives in ``theforge.task.triage_prompts``, the
control flow that invokes the agent and persists the run lives in
``theforge.coordinator.triage_proposal_flow``.

Two rules make a proposal reviewable rather than a guess:

* **Fixed taxonomy with required payload.** ``fix_now`` carries the current
  milestone, ``fix_later`` a named milestone or the standing ``Hygiene`` pool,
  ``punt`` a reason code from :data:`PUNT_REASON_CODES`, and
  ``needs_verification`` carries no target at all. Anything else is rejected.
* **Grounding.** Every proposal must cite ``evidence_refs`` — IDs of evidence
  entries that are actually *in* the packet it was given. A claim resting on
  something the packet does not contain is rejected exactly like schema-invalid
  output, because an unsupported disposition is the failure mode the whole
  stage exists to prevent.

Proposals are advisory. Nothing here applies anything, and nothing here writes
to a tracker.

Stdlib + yaml only.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

import yaml

# ── Fixed disposition taxonomy ────────────────────────────────────────────────
#
# A proposal's ``disposition`` must be one of these values. Free-form triage
# prose is not an allowed output.
DISPOSITION_FIX_NOW = "fix_now"
DISPOSITION_FIX_LATER = "fix_later"
DISPOSITION_PUNT = "punt"
DISPOSITION_NEEDS_VERIFICATION = "needs_verification"

DISPOSITION_TAXONOMY: tuple[str, ...] = (
    DISPOSITION_FIX_NOW,
    DISPOSITION_FIX_LATER,
    DISPOSITION_PUNT,
    DISPOSITION_NEEDS_VERIFICATION,
)

DISPOSITION_LABELS: dict[str, str] = {
    DISPOSITION_FIX_NOW: "Fix now",
    DISPOSITION_FIX_LATER: "Fix later",
    DISPOSITION_PUNT: "Punt",
    DISPOSITION_NEEDS_VERIFICATION: "Needs verification",
}

# The standing pool a ``fix_later`` may target when no named milestone fits.
HYGIENE_POOL = "Hygiene"

# ── Punt reason codes ─────────────────────────────────────────────────────────
#
# A punt discards a finding, so it is the one disposition where a wrong call is
# silently unrecoverable — the reason has to be a checkable claim, not prose.
# This is the single place the set is defined: the prompt renders it from here
# and the validator rejects anything outside it, so a later slice that ships an
# authoritative enumeration replaces one constant.
PUNT_REASON_VERIFIED_STALE = "verified-stale"
PUNT_REASON_SUPERSEDED = "superseded"
PUNT_REASON_NOT_REPRODUCIBLE = "not-reproducible"
PUNT_REASON_DUPLICATE = "duplicate"
PUNT_REASON_OUT_OF_SCOPE_BY_POLICY = "out-of-scope-by-policy"

PUNT_REASON_CODES: tuple[str, ...] = (
    PUNT_REASON_VERIFIED_STALE,
    PUNT_REASON_SUPERSEDED,
    PUNT_REASON_NOT_REPRODUCIBLE,
    PUNT_REASON_DUPLICATE,
    PUNT_REASON_OUT_OF_SCOPE_BY_POLICY,
)

PUNT_REASON_GUIDE: dict[str, str] = {
    PUNT_REASON_VERIFIED_STALE: (
        "The packet shows the thing the finding is about is gone from the current "
        "tree — the cited symbol, file, or code path no longer exists."
    ),
    PUNT_REASON_SUPERSEDED: (
        "A later change already did what the finding asks for, or replaced the "
        "surface it names, and the packet shows it."
    ),
    PUNT_REASON_NOT_REPRODUCIBLE: (
        "The packet carries a check that was run and shows the described behaviour does not occur."
    ),
    PUNT_REASON_DUPLICATE: ("The packet shows the same finding is already tracked elsewhere."),
    PUNT_REASON_OUT_OF_SCOPE_BY_POLICY: (
        "A standing policy cited in the packet puts this permanently out of scope — "
        "not merely later than now, which is fix_later."
    ),
}

_OPEN_TAG = "<triage_proposal>"
_CLOSE_TAG = "</triage_proposal>"
_FENCED_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)

# Cap free-text fields so a runaway agent cannot balloon the packet/proposal.
_FIELD_MAX_LEN = 2000


# ── Evidence packet ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PacketEvidence:
    """One citable evidence entry in a finding's packet.

    ``evidence_id`` is what a proposal must cite; ``checkable`` says whether
    this entry is a fact somebody re-ran (a staleness probe against the current
    tree, a registry row) as opposed to a restatement of the finding's own
    claim. A packet with no checkable entry cannot support a discard — see
    :meth:`FindingPacket.has_checkable_evidence`.
    """

    evidence_id: str
    kind: str  # e.g. "staleness" | "disposition_history" | "finding_body"
    summary: str
    checkable: bool = True
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.evidence_id,
            "kind": self.kind,
            "summary": self.summary,
            "checkable": self.checkable,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class FindingPacket:
    """Everything a fresh agent is given about one backlog finding.

    The finding body, the deterministic evidence the report slice computed, the
    disposition history the audit substrate holds for this finding, and the
    milestone vocabulary a fix disposition may target. Nothing else — the agent
    did not file the finding and does not inherit whatever framing did.
    """

    finding_id: str
    issue_ref: str
    finding_body: str
    evidence: tuple[PacketEvidence, ...] = ()
    # The milestone a ``fix_now`` must target. ``None`` means the caller could
    # not name one, which makes ``fix_now`` unavailable rather than unchecked.
    current_milestone: str | None = None
    # Milestones a ``fix_later`` may target, alongside the standing Hygiene pool.
    named_milestones: tuple[str, ...] = ()
    # Prior proposal/disposition rows for this finding, oldest first.
    disposition_history: tuple[dict, ...] = ()

    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(item.evidence_id for item in self.evidence)

    def checkable_evidence_ids(self) -> tuple[str, ...]:
        return tuple(item.evidence_id for item in self.evidence if item.checkable)

    def has_checkable_evidence(self) -> bool:
        return bool(self.checkable_evidence_ids())

    def available_dispositions(self) -> tuple[str, ...]:
        """Dispositions this packet can actually support.

        ``fix_now`` drops out when no current milestone is known: the taxonomy
        requires it to carry one, and an unnameable target is not a proposal a
        validator could check.
        """
        if self.current_milestone:
            return DISPOSITION_TAXONOMY
        return tuple(d for d in DISPOSITION_TAXONOMY if d != DISPOSITION_FIX_NOW)

    def fix_later_targets(self) -> tuple[str, ...]:
        return (*self.named_milestones, HYGIENE_POOL)

    def to_dict(self) -> dict:
        return {
            "finding_id": self.finding_id,
            "issue_ref": self.issue_ref,
            "finding_body": self.finding_body,
            "evidence": [item.to_dict() for item in self.evidence],
            "current_milestone": self.current_milestone,
            "named_milestones": list(self.named_milestones),
            "disposition_history": [dict(row) for row in self.disposition_history],
        }

    def packet_hash(self) -> str:
        """Stable digest of the packet contents, for the audit trail.

        Lets an operator tell "the same evidence produced a different
        disposition" from "the evidence changed" without diffing two packets.
        """
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ── Proposal ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TriageProposal:
    """One parsed, validated disposition proposal for one finding.

    ``parse_errors`` is non-empty when the raw agent output failed validation;
    such a proposal is unusable and the caller must not act on its
    ``disposition`` field.
    """

    finding_id: str
    issue_ref: str
    disposition: str
    evidence: str = ""
    evidence_refs: tuple[str, ...] = ()
    rationale: str = ""
    target_milestone: str | None = None
    punt_reason_code: str | None = None
    parse_errors: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.parse_errors

    def target_display(self) -> str:
        """The payload rendered the way the operator reads it."""
        if self.disposition == DISPOSITION_PUNT:
            return f"reason: {self.punt_reason_code}"
        if self.target_milestone:
            return f"→ {self.target_milestone}"
        return ""

    def to_dict(self) -> dict:
        return {
            "finding_id": self.finding_id,
            "issue_ref": self.issue_ref,
            "disposition": self.disposition,
            "disposition_label": DISPOSITION_LABELS.get(self.disposition, self.disposition),
            "evidence": self.evidence,
            "evidence_refs": list(self.evidence_refs),
            "rationale": self.rationale,
            "target_milestone": self.target_milestone,
            "punt_reason_code": self.punt_reason_code,
            "parse_errors": list(self.parse_errors),
        }


@dataclass(frozen=True)
class FindingProposalResult:
    """The proposal stage's outcome for one finding, with what it cost.

    ``fallback_reason`` is empty when the agent's own proposal was accepted. It
    is set when this run resolved to ``needs_verification`` for a reason other
    than the agent proposing it: either the packet carried no checkable
    evidence (no agent was invoked at all) or the agent's output never passed
    validation. ``validation_errors`` carries why, so a
    ``needs_verification`` that means "the agent could not produce valid
    output" is never mistaken for one that means "the evidence is thin".
    """

    finding_id: str
    issue_ref: str
    packet_hash: str
    proposal: TriageProposal
    attempts: int = 0
    retry_count: int = 0
    validation_errors: tuple[str, ...] = ()
    fallback_reason: str = ""
    cost_usd: float | None = None
    cost_provenance: str = "unknown"

    def to_dict(self) -> dict:
        return {
            "finding_id": self.finding_id,
            "issue_ref": self.issue_ref,
            "packet_hash": self.packet_hash,
            "proposal": self.proposal.to_dict(),
            "attempts": self.attempts,
            "retry_count": self.retry_count,
            "validation_errors": list(self.validation_errors),
            "fallback_reason": self.fallback_reason,
            "cost_usd": self.cost_usd,
            "cost_provenance": self.cost_provenance,
        }


@dataclass(frozen=True)
class ProposalRunSummary:
    """One ``forge triage`` proposal run: every result plus the total spend."""

    results: tuple[FindingProposalResult, ...] = ()
    total_cost_usd: float | None = None
    cost_provenance: str = "unknown"
    triage_run_id: str = ""
    report_path: str = ""
    audit_error: str = ""

    @property
    def findings_count(self) -> int:
        return len(self.results)

    def to_dict(self) -> dict:
        return {
            "triage_run_id": self.triage_run_id,
            "report_path": self.report_path,
            "findings_count": self.findings_count,
            "results": [r.to_dict() for r in self.results],
            "total_cost_usd": self.total_cost_usd,
            "cost_provenance": self.cost_provenance,
            "audit_error": self.audit_error,
        }


def needs_verification_proposal(
    packet: FindingPacket,
    *,
    evidence: str,
    evidence_refs: tuple[str, ...] = (),
    rationale: str = "",
) -> TriageProposal:
    """Build the deterministic ``needs_verification`` proposal for a packet.

    The single constructor for every path that resolves to
    ``needs_verification`` without the agent having proposed it, so no caller
    can accidentally synthesise a *different* disposition when it cannot decide.
    """
    return TriageProposal(
        finding_id=packet.finding_id,
        issue_ref=packet.issue_ref,
        disposition=DISPOSITION_NEEDS_VERIFICATION,
        evidence=evidence,
        evidence_refs=evidence_refs,
        rationale=rationale,
    )


def _truncate(value: object) -> str:
    text = "" if value is None else str(value).strip()
    if len(text) > _FIELD_MAX_LEN:
        return text[: _FIELD_MAX_LEN - 1] + "…"
    return text


def _extract_proposal_block(text: str) -> str | None:
    """Return the YAML body inside <triage_proposal>…</triage_proposal>, or None.

    Fenced code blocks are stripped first so an agent quoting the schema in
    prose does not produce a false match.
    """
    stripped = _FENCED_BLOCK_RE.sub("", text)
    open_pos = stripped.find(_OPEN_TAG)
    close_pos = stripped.find(_CLOSE_TAG)
    if open_pos < 0 or close_pos < 0:
        return None
    if close_pos < open_pos + len(_OPEN_TAG):
        return None
    body = stripped[open_pos + len(_OPEN_TAG) : close_pos].strip()
    return body or None


def _error_proposal(
    packet: FindingPacket, errors: list[str], raw: dict | None = None
) -> TriageProposal:
    return TriageProposal(
        finding_id=packet.finding_id,
        issue_ref=packet.issue_ref,
        disposition="",
        parse_errors=tuple(errors),
        raw=raw or {},
    )


def _coerce_refs(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [piece.strip() for piece in raw.split(",") if piece.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def _validate_payload(
    packet: FindingPacket, disposition: str, target: str, reason_code: str
) -> list[str]:
    """Check the payload the taxonomy requires for ``disposition``."""
    errors: list[str] = []
    if disposition == DISPOSITION_FIX_NOW:
        if not packet.current_milestone:
            errors.append(
                "fix_now is unavailable for this finding: the packet names no current "
                "milestone to target"
            )
        elif not target:
            errors.append("fix_now requires target_milestone (the current milestone)")
        elif target.lower() != packet.current_milestone.lower():
            errors.append(
                f"fix_now must target the current milestone "
                f"{packet.current_milestone!r}, got {target!r}"
            )
        if reason_code:
            errors.append("fix_now must not carry punt_reason_code")
    elif disposition == DISPOSITION_FIX_LATER:
        allowed = packet.fix_later_targets()
        if not target:
            errors.append(f"fix_later requires target_milestone — one of {list(allowed)}")
        elif not any(target.lower() == candidate.lower() for candidate in allowed):
            errors.append(
                f"fix_later target_milestone must be one of {list(allowed)}, got {target!r}"
            )
        if reason_code:
            errors.append("fix_later must not carry punt_reason_code")
    elif disposition == DISPOSITION_PUNT:
        if not reason_code:
            errors.append(f"punt requires punt_reason_code — one of {list(PUNT_REASON_CODES)}")
        elif reason_code not in PUNT_REASON_CODES:
            errors.append(
                f"punt_reason_code must be one of {list(PUNT_REASON_CODES)}, got {reason_code!r}"
            )
        if target:
            errors.append("punt must not carry target_milestone")
    elif disposition == DISPOSITION_NEEDS_VERIFICATION:
        if target:
            errors.append("needs_verification must not carry target_milestone")
        if reason_code:
            errors.append("needs_verification must not carry punt_reason_code")
    return errors


def _validate_grounding(packet: FindingPacket, refs: list[str], evidence: str) -> list[str]:
    """Check the proposal cites evidence that is actually in the packet."""
    errors: list[str] = []
    if not evidence:
        errors.append("proposal missing evidence")
    known = set(packet.evidence_ids())
    if not refs:
        if known:
            errors.append(
                f"proposal must cite evidence_refs from the packet — available: {sorted(known)}"
            )
        return errors
    unknown = [ref for ref in refs if ref not in known]
    if unknown:
        errors.append(
            f"evidence_refs cite ids not present in the packet: {sorted(unknown)} "
            f"(available: {sorted(known)})"
        )
    return errors


def parse_triage_proposal(text: str, packet: FindingPacket) -> TriageProposal:
    """Parse and validate agent output into a ``TriageProposal``.

    This is the schema integrity boundary. Validation rules:

    * a ``<triage_proposal>`` block must be present and parse as a YAML mapping,
    * ``disposition`` must be one of ``DISPOSITION_TAXONOMY`` and must be
      available for this packet,
    * the payload the disposition requires must be present and admissible
      (see :func:`_validate_payload`),
    * ``evidence`` must be non-empty and ``evidence_refs`` must cite only
      evidence IDs present in ``packet``.

    Any violation yields a proposal with ``parse_errors`` populated so the
    caller retries or falls back to ``needs_verification`` — never acts on a
    disposition the packet does not support.
    """
    body = _extract_proposal_block(text)
    if body is None:
        return _error_proposal(packet, ["no <triage_proposal> block found in agent output"])

    try:
        data = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        return _error_proposal(packet, [f"YAML parse error in triage proposal: {exc}"])

    if not isinstance(data, dict):
        return _error_proposal(
            packet, [f"triage proposal must be a YAML mapping, got {type(data).__name__}"]
        )

    errors: list[str] = []
    disposition = str(data.get("disposition") or "").strip().lower()
    available = packet.available_dispositions()
    if disposition not in DISPOSITION_TAXONOMY:
        errors.append(
            f"disposition must be one of {list(DISPOSITION_TAXONOMY)}, got {disposition!r}"
        )
    elif disposition not in available:
        errors.append(
            f"disposition {disposition!r} is not available for this finding — "
            f"available: {list(available)}"
        )

    target = _truncate(data.get("target_milestone") or data.get("target") or "")
    reason_code = str(data.get("punt_reason_code") or data.get("reason_code") or "").strip()
    evidence = _truncate(data.get("evidence"))
    rationale = _truncate(data.get("rationale"))
    refs = _coerce_refs(data.get("evidence_refs"))

    if disposition in DISPOSITION_TAXONOMY:
        errors.extend(_validate_payload(packet, disposition, target, reason_code))
    errors.extend(_validate_grounding(packet, refs, evidence))

    if errors:
        return _error_proposal(packet, errors, raw=data)

    return TriageProposal(
        finding_id=packet.finding_id,
        issue_ref=packet.issue_ref,
        disposition=disposition,
        evidence=evidence,
        evidence_refs=tuple(refs),
        rationale=rationale,
        target_milestone=target or None,
        punt_reason_code=reason_code or None,
        raw=data,
    )


# ── Rendering ─────────────────────────────────────────────────────────────────


def _format_cost(cost_usd: float | None) -> str:
    return "unmeasured" if cost_usd is None else f"${cost_usd:.4f}"


def render_result(result: FindingProposalResult) -> str:
    """Render one finding's proposal the way the operator reads it."""
    proposal = result.proposal
    payload = proposal.target_display()
    header = f"{result.issue_ref or result.finding_id}  PROPOSE {proposal.disposition}"
    if payload:
        header += f" ({payload})" if payload.startswith("reason:") else f" {payload}"
    lines = [header]
    if proposal.evidence:
        lines.append(f"       evidence: {proposal.evidence}")
    if proposal.evidence_refs:
        lines.append(f"       cites: {', '.join(proposal.evidence_refs)}")
    if result.fallback_reason:
        lines.append(f"       fallback: {result.fallback_reason}")
    for error in result.validation_errors:
        lines.append(f"       validation error: {error}")
    lines.append(f"       cost: {_format_cost(result.cost_usd)} ({result.cost_provenance})")
    return "\n".join(lines)


def render_run_summary(summary: ProposalRunSummary) -> str:
    """Render a whole proposal run: one block per finding, then total spend."""
    if not summary.results:
        return (
            "TRIAGE PROPOSALS — backlog report contains no findings.\n"
            "Nothing was proposed and no agent was invoked; total spend $0.0000."
        )
    lines = [f"TRIAGE PROPOSALS — {summary.findings_count} finding(s)", ""]
    for result in summary.results:
        lines.append(render_result(result))
        lines.append("")
    lines.append(
        f"TOTAL SPEND: {_format_cost(summary.total_cost_usd)} ({summary.cost_provenance})"
    )
    lines.append("Advisory only — no issue was modified.")
    if summary.audit_error:
        lines.append(f"⚠ audit substrate write failed: {summary.audit_error}")
    return "\n".join(lines)
