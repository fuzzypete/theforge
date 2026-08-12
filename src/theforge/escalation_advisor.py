"""Escalation advisor: fixed action taxonomy, evidence packet, and report parsing.

When a story escalates (max review cycles exhausted, dev cannot converge), the
old dead-end was: max-cycles → human-decision timeout → auto-reject. This module
is the pure-data + integrity boundary for the replacement: a **fresh-context
advisory report** that reads a prepared **evidence packet** and emits a small,
constrained menu of evidence-backed action choices. The operator must then
select one of those actions — the system no longer auto-rejects on timeout.

This module is deliberately low-dependency (stdlib + yaml only): it holds the
pure-data types (``EvidencePacket``, ``AdvisoryOption``, ``AdvisoryReport``), the
fixed action taxonomy, and the parser/validator that is the schema integrity
boundary for advisor output. Prompt construction lives in
``theforge.task.advisor_prompts``; the coordinator control flow that invokes the
fresh agent and wires the pending gate lives in
``theforge.coordinator.escalation_advisor_flow``.

The advisor's recommendation MUST be one of the fixed taxonomy values — free-form
prose is not an allowed output. Each presented option carries four fields:
evidence (cited from the packet), action (the concrete forge operation), risk,
and consequence (what happens if selected).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

# ── Fixed action taxonomy ─────────────────────────────────────────────────────
#
# The advisor's recommendation and every presented option's ``action`` field must
# be one of these values. This is the constrained-output contract: the advisor
# routes an escalation into exactly one of these, never free-form prose.
ACTION_TAXONOMY: tuple[str, ...] = (
    "accept",
    "land_core_defer_edges",
    "redirect",
    "decompose",
    "elevate",
    "defer_or_abandon",
)

# Human-readable label per taxonomy action, for rendering the pending report.
ACTION_LABELS: dict[str, str] = {
    "accept": "Accept",
    "land_core_defer_edges": "Land-core-defer-edges",
    "redirect": "Redirect",
    "decompose": "Decompose",
    "elevate": "Elevate",
    "defer_or_abandon": "Defer-or-Abandon",
}

# Each taxonomy action names the concrete forge operation it maps to. In v1 the
# report *names* the operation for every action; mechanical one-click execution
# is wired only for the accept/defer endpoints (approve/reject), while the middle
# actions preserve the worktree and surface the named next operation so the
# operator can run it. This mapping is the single discoverable place that ties
# the taxonomy to forge operations.
ACTION_FORGE_OPERATIONS: dict[str, str] = {
    "accept": "merge (finalize the current work as-is)",
    "land_core_defer_edges": "land-core (merge the core, file follow-ups for the edges)",
    "redirect": "re-run with constraint (re-dev under a corrected framing)",
    "decompose": "split (break the story into smaller runnable pieces)",
    "elevate": "bump (route to a human design/architecture decision)",
    "defer_or_abandon": "defer (defer or abandon the story)",
}

# Coordinator disposition each taxonomy action normalises to at the escalate gate.
#   approve  → finalize/merge the current work
#   reject   → escalate-reject (defer/abandon)
#   named    → preserve the worktree and surface the named next operation
_ACTION_DISPOSITIONS: dict[str, str] = {
    "accept": "approve",
    "defer_or_abandon": "reject",
    "land_core_defer_edges": "named",
    "redirect": "named",
    "decompose": "named",
    "elevate": "named",
}

_OPEN_TAG = "<advisory_report>"
_CLOSE_TAG = "</advisory_report>"
_FENCED_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)

# Cap long free-text fields so a runaway agent cannot balloon the packet/report.
_FIELD_MAX_LEN = 2000


def action_disposition(action: str) -> str:
    """Return the coordinator disposition for a taxonomy action.

    One of ``"approve"`` | ``"reject"`` | ``"named"``. Unknown actions map to
    ``"reject"`` (fail closed) so a malformed selection never silently merges.
    """
    return _ACTION_DISPOSITIONS.get((action or "").strip().lower(), "reject")


# ── Evidence packet ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CycleEvidence:
    """One review cycle's outcome, distilled for the evidence packet.

    The churn pattern across cycles — what reviewers kept flagging, what the dev
    kept re-breaking — is the most valuable escalation signal, so each cycle
    carries its verdict, summary, and the finding descriptions that fired.
    """

    cycle: int
    verdict: str  # "APPROVE" | "REQUEST_CHANGES"
    summary: str
    findings: list[str] = field(default_factory=list)  # finding descriptions this cycle


@dataclass(frozen=True)
class EvidencePacket:
    """Prepared evidence a fresh advisor reads to recommend an action.

    Assembled by the coordinator (``escalation_advisor_flow.build_evidence_packet``)
    from the escalated run's state: the issue body + acceptance criteria, the
    per-cycle review summaries/findings/verdicts, the dev diff, any test/gate
    failure output, and the final escalation reason. It never contains the failed
    dev/review agents' sessions — the advisor runs in a fresh context.
    """

    story_name: str
    issue_ref: str  # "#1365" or slug — for citing evidence
    issue_body: str
    acceptance_criteria: list[str]
    cycles: list[CycleEvidence]
    reviewer_verdicts: dict[str, str]
    final_verdict: str | None
    dev_diff: str
    test_failures: str
    escalation_reason: str
    # Deterministic topology-walk evidence (#2372), or None when the loop showed
    # no such pattern. Present when the escalation happened BEFORE the cycle
    # ceiling because successive cycles resolved their predecessor and raised the
    # same concern somewhere new — the advisor needs it to know it is being asked
    # about the framing, not about the latest finding. Defaulted so packets
    # assembled by paths that predate the detector remain constructible.
    topology_signal: dict | None = None

    def to_dict(self) -> dict:
        """Serialise the packet for the audit trail."""
        return {
            "story_name": self.story_name,
            "issue_ref": self.issue_ref,
            "issue_body": self.issue_body,
            "acceptance_criteria": list(self.acceptance_criteria),
            "cycles": [
                {
                    "cycle": c.cycle,
                    "verdict": c.verdict,
                    "summary": c.summary,
                    "findings": list(c.findings),
                }
                for c in self.cycles
            ],
            "reviewer_verdicts": dict(self.reviewer_verdicts),
            "final_verdict": self.final_verdict,
            "dev_diff": self.dev_diff,
            "test_failures": self.test_failures,
            "escalation_reason": self.escalation_reason,
            "topology_signal": dict(self.topology_signal) if self.topology_signal else None,
        }


# ── Advisory report ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AdvisoryOption:
    """One evidence-backed action choice presented to the operator.

    Every option carries the four required fields: cited ``evidence`` from the
    packet, the concrete ``action`` (forge operation), the ``risk`` of taking it,
    and the ``consequence`` of selecting it.
    """

    action: str  # one of ACTION_TAXONOMY
    evidence: str
    forge_operation: str  # concrete forge operation named for this action
    risk: str
    consequence: str

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "action_label": ACTION_LABELS.get(self.action, self.action),
            "evidence": self.evidence,
            "forge_operation": self.forge_operation,
            "risk": self.risk,
            "consequence": self.consequence,
        }


@dataclass(frozen=True)
class AdvisoryReport:
    """A parsed, validated advisory report.

    ``recommendation`` is one of ``ACTION_TAXONOMY`` and must appear among the
    presented ``options``. ``parse_errors`` is non-empty when the raw advisor
    output failed schema validation; callers treat a report with parse errors as
    unusable and fall back to preserving the escalation.
    """

    recommendation: str  # one of ACTION_TAXONOMY
    rationale: str
    options: list[AdvisoryOption]
    parse_errors: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.parse_errors

    def recommended_option(self) -> AdvisoryOption | None:
        for opt in self.options:
            if opt.action == self.recommendation:
                return opt
        return None

    def to_dict(self) -> dict:
        return {
            "recommendation": self.recommendation,
            "recommendation_label": ACTION_LABELS.get(self.recommendation, self.recommendation),
            "rationale": self.rationale,
            "options": [o.to_dict() for o in self.options],
            "parse_errors": list(self.parse_errors),
        }


def _truncate(value: object) -> str:
    text = "" if value is None else str(value).strip()
    if len(text) > _FIELD_MAX_LEN:
        return text[: _FIELD_MAX_LEN - 1] + "…"
    return text


def _extract_advisory_block(text: str) -> str | None:
    """Return the YAML body inside <advisory_report>…</advisory_report>, or None.

    Fenced code blocks are stripped first so an advisor quoting the schema spec
    in prose does not produce a false match.
    """
    stripped = _FENCED_BLOCK_RE.sub("", text)
    open_positions = [m.start() for m in re.finditer(re.escape(_OPEN_TAG), stripped)]
    close_positions = [m.start() for m in re.finditer(re.escape(_CLOSE_TAG), stripped)]
    if not open_positions or not close_positions:
        return None
    open_pos = open_positions[0]
    close_pos = close_positions[0]
    if close_pos < open_pos + len(_OPEN_TAG):
        return None
    body = stripped[open_pos + len(_OPEN_TAG) : close_pos].strip()
    return body or None


def _error_report(errors: list[str], raw: dict | None = None) -> AdvisoryReport:
    return AdvisoryReport(
        recommendation="",
        rationale="",
        options=[],
        parse_errors=errors,
        raw=raw or {},
    )


def parse_advisory_report(text: str) -> AdvisoryReport:
    """Parse and validate advisor output into an ``AdvisoryReport``.

    This is the schema integrity boundary for advisor output. Validation rules:

    * an ``<advisory_report>`` block must be present and parse as a YAML mapping,
    * ``recommendation`` must be one of ``ACTION_TAXONOMY``,
    * ``options`` must be a non-empty list; each option's ``action`` must be a
      taxonomy value and it must carry non-empty ``evidence``, ``risk``, and
      ``consequence``,
    * the ``recommendation`` must appear among the options' actions.

    Any violation yields a report with ``parse_errors`` populated (and empty
    recommendation/options) so the caller fails closed rather than acting on a
    malformed recommendation.
    """
    body = _extract_advisory_block(text)
    if body is None:
        return _error_report(["no <advisory_report> block found in advisor output"])

    try:
        data = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        return _error_report([f"YAML parse error in advisory report: {exc}"])

    if not isinstance(data, dict):
        return _error_report(
            [f"advisory report must be a YAML mapping, got {type(data).__name__}"]
        )

    errors: list[str] = []

    recommendation = str(data.get("recommendation") or "").strip().lower()
    if recommendation not in ACTION_TAXONOMY:
        errors.append(
            f"recommendation must be one of {list(ACTION_TAXONOMY)}, got {recommendation!r}"
        )

    rationale = _truncate(data.get("rationale"))

    raw_options = data.get("options")
    options: list[AdvisoryOption] = []
    if not isinstance(raw_options, list) or not raw_options:
        errors.append("options must be a non-empty list")
    else:
        for i, opt in enumerate(raw_options):
            if not isinstance(opt, dict):
                errors.append(f"option[{i}] must be a mapping")
                continue
            action = str(opt.get("action") or "").strip().lower()
            if action not in ACTION_TAXONOMY:
                errors.append(
                    f"option[{i}].action must be one of {list(ACTION_TAXONOMY)}, got {action!r}"
                )
            evidence = _truncate(opt.get("evidence"))
            risk = _truncate(opt.get("risk"))
            consequence = _truncate(opt.get("consequence"))
            forge_operation = _truncate(
                opt.get("forge_operation")
                or opt.get("operation")
                or ACTION_FORGE_OPERATIONS.get(action, "")
            )
            for field_name, val in (
                ("evidence", evidence),
                ("risk", risk),
                ("consequence", consequence),
            ):
                if not val:
                    errors.append(f"option[{i}] ({action or '?'}) missing {field_name}")
            options.append(
                AdvisoryOption(
                    action=action,
                    evidence=evidence,
                    forge_operation=forge_operation,
                    risk=risk,
                    consequence=consequence,
                )
            )

    # Cross-validation: the recommendation must be one of the presented options.
    if recommendation in ACTION_TAXONOMY and options:
        if recommendation not in {o.action for o in options}:
            errors.append(f"recommendation {recommendation!r} is not present among the options")

    if errors:
        return _error_report(errors, raw=data if isinstance(data, dict) else {})

    return AdvisoryReport(
        recommendation=recommendation,
        rationale=rationale,
        options=options,
        parse_errors=[],
        raw=data,
    )


# ── Rendering ─────────────────────────────────────────────────────────────────


def render_advisory_for_pending(report: AdvisoryReport, packet: EvidencePacket) -> str:
    """Render a human-readable advisory summary for the pending decision file.

    This is the ``reason`` an operator sees when deciding. It leads with the
    recommendation and then lists each option with its evidence, forge operation,
    risk, and consequence so the choice is legible without opening the raw packet.
    """
    lines: list[str] = []
    lines.append(f"ESCALATION ADVISORY — {packet.story_name} ({packet.issue_ref})")
    lines.append(f"Escalation reason: {packet.escalation_reason}")
    lines.append(f"Review cycles: {len(packet.cycles)}")
    rec_label = ACTION_LABELS.get(report.recommendation, report.recommendation)
    lines.append("")
    lines.append(f"RECOMMENDED ACTION: {rec_label}")
    if report.rationale:
        lines.append(f"  Rationale: {report.rationale}")
    lines.append("")
    lines.append("Options (select one):")
    for opt in report.options:
        label = ACTION_LABELS.get(opt.action, opt.action)
        marker = " ← recommended" if opt.action == report.recommendation else ""
        lines.append(f"  • {label} [{opt.action}]{marker}")
        lines.append(f"      operation: {opt.forge_operation}")
        lines.append(f"      evidence: {opt.evidence}")
        lines.append(f"      risk: {opt.risk}")
        lines.append(f"      consequence: {opt.consequence}")
    return "\n".join(lines)
