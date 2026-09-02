"""Coordinator control flow for the fresh-context escalation advisor.

On escalation, this module assembles an evidence packet from the escalated run's
state, invokes a **fresh** advisor agent (a new context — never the failed
dev/review sessions) to produce a constrained advisory report, and records both
on the state for the audit trail. The pending-gate wiring that turns the report
into an operator selection lives in ``pending_hitl``/``review_phase``; this module
owns packet construction and the agent invocation.

The advisor is a reasoning agent that produces *advice* for a human — it does not
make a routing decision. The coordinator stays pure Python: it requires the
operator to select an action and never auto-acts on the advisor's recommendation.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from theforge.assignment import NoCapableCandidateError, _capability_exclusions, _capability_pool
from theforge.config import DEFAULT_INVESTIGATION_TOOLS
from theforge.config.model_identity import DEFAULT_PHASE_ELIGIBILITY, PHASE_ADVISOR
from theforge.config.pricing import price_tiebreak_signal_for
from theforge.escalation_advisor import (
    ACTION_TAXONOMY,
    CycleEvidence,
    EvidencePacket,
    parse_advisory_report,
    resolve_advisory_assertions,
)
from theforge.model_capabilities import (
    capabilities_path,
    identity_for_agent,
    identity_for_profile,
    load_capabilities,
)
from theforge.policy_provenance import load_policy_assertions
from theforge.task.advisor_prompts import build_advisor_prompt
from theforge.task.story import extract_acceptance_criteria

from . import util as _cu
from .agent_failure import classify_launch_failure
from .baseline_checkout import prepare_baseline_checkout
from .escalate_actions import available_escalate_actions

if TYPE_CHECKING:
    from pathlib import Path

    from theforge.config import ForgeConfig
    from theforge.escalation_advisor import AdvisoryReport
    from theforge.task import TaskStory

    from .state import CoordinatorState

_log = _cu._log
_log_verbose = _cu._log_verbose

# ── Lazy runner slots (mirrors preflight_flow) ────────────────────────────────
# None until first call; tests may replace before invoking the advisor.
# Patch targets:
#   theforge.coordinator.escalation_advisor_flow.run_agent
#   theforge.coordinator.escalation_advisor_flow.log_agent_result
run_agent = None
log_agent_result = None

# Cap the dev-diff captured into the packet so a large change cannot balloon the
# advisor prompt.
_DIFF_MAX_BYTES = 20_000


def _agent_registry_spec(config: "ForgeConfig", agent: object) -> object | None:
    registry = getattr(config, "model_registry", None) or {}
    registry_id = getattr(agent, "registry_id", None)
    spec = registry.get(registry_id) if registry_id else None
    if spec is None:
        identity = identity_for_agent(agent)
        if identity is not None:
            spec = registry.get(identity.key)
    return spec


def _sort_advisor_candidates(config: "ForgeConfig", agents: list[object]) -> list[object]:
    fallback_rank = {"cheap": 1, "fast": 1, "mid": 2, "strong": 3}

    def _sort_key(agent: object) -> tuple[object, ...]:
        spec = _agent_registry_spec(config, agent)
        if spec is not None:
            return (spec.cost_rank, -spec.capability, price_tiebreak_signal_for(spec))
        return (
            fallback_rank.get(getattr(agent, "tier", None), 2),
            0,
            price_tiebreak_signal_for(agent),
        )

    return sorted(
        agents,
        key=_sort_key,
    )


def _agent_phase_eligibility(config: "ForgeConfig", agent: object) -> frozenset[str]:
    spec = _agent_registry_spec(config, agent)
    if spec is not None:
        return spec.phase_eligibility
    return DEFAULT_PHASE_ELIGIBILITY


def _select_advisor_profile(config: "ForgeConfig") -> object:
    """Choose the advisor model from the configured pool, fail-closed on ineligibility."""
    agents = getattr(config, "agents", None) or []
    if not agents:
        raise ValueError("no configured model candidates are available for the advisor role")

    phase_eligible = [
        agent for agent in agents if PHASE_ADVISOR in _agent_phase_eligibility(config, agent)
    ]
    if not phase_eligible:
        raise ValueError(
            "no configured model is phase-eligible for advisor "
            "(routing.phase_eligibility excludes advisor)"
        )

    capabilities = load_capabilities(capabilities_path(config.project_root))
    excluded = _capability_exclusions(phase_eligible, "advisor", capabilities)
    capable = _capability_pool(phase_eligible, excluded)
    if not capable:
        raise NoCapableCandidateError("advisor", "tool-structured", excluded)

    sorted_capable = _sort_advisor_candidates(config, capable)
    preflight_identity = identity_for_profile(config.preflight_profile)
    selected = next(
        (
            agent
            for agent in sorted_capable
            if preflight_identity is not None and identity_for_agent(agent) == preflight_identity
        ),
        None,
    )
    if selected is None:
        fast = [
            agent
            for agent in sorted_capable
            if getattr(_agent_registry_spec(config, agent), "tier", None) == "fast"
        ]
        selected = fast[0] if fast else sorted_capable[0]

    return replace(
        config.preflight_profile,
        name="advisor",
        phase=PHASE_ADVISOR,
        cli=selected.cli,
        provider=selected.provider,
        transport=selected.transport,
        base_url=selected.base_url,
        model=selected.model,
        registry_id=selected.registry_id,
        registry_source=selected.registry_source,
        allowed_tools=DEFAULT_INVESTIGATION_TOOLS,
    )


def _ensure_runners() -> None:
    global run_agent, log_agent_result
    if run_agent is not None and log_agent_result is not None:
        return
    import theforge.runners as _r  # noqa: PLC0415

    if run_agent is None:
        run_agent = _r.run_agent
    if log_agent_result is None:
        log_agent_result = _r.log_agent_result


def _issue_ref(task: "TaskStory") -> str:
    if task.github_issue:
        return f"#{task.github_issue}"
    return task.slug


#: Reading a story's acceptance criteria is story parsing, so it lives in
#: ``task.story`` where every consumer can reach it. The local name is kept for
#: this module's own call site and its tests.
_extract_acceptance_criteria = extract_acceptance_criteria


def _capture_dev_diff(workspace_path: "Path", base_branch: str) -> str:
    """Capture a bounded summary of the dev's work: commit log + diffstat + diff."""
    from pathlib import Path  # noqa: PLC0415

    if workspace_path is None or not Path(workspace_path).exists():
        return ""
    parts: list[str] = []
    ok_log, log_out = _cu._run_shell(
        f"git log --oneline {base_branch}..HEAD", Path(workspace_path)
    )
    if ok_log and log_out.strip():
        parts.append("commits:\n" + log_out.strip())
    ok_stat, stat_out = _cu._run_shell(
        f"git diff --stat {base_branch}...HEAD", Path(workspace_path)
    )
    if ok_stat and stat_out.strip():
        parts.append("diffstat:\n" + stat_out.strip())
    ok_diff, diff_out = _cu._run_shell(f"git diff {base_branch}...HEAD", Path(workspace_path))
    if ok_diff and diff_out.strip():
        diff = diff_out.strip()
        if len(diff) > _DIFF_MAX_BYTES:
            diff = diff[:_DIFF_MAX_BYTES] + "\n… (diff truncated)"
        parts.append("diff:\n" + diff)
    return "\n\n".join(parts)


def _capture_test_failures(state: "CoordinatorState") -> str:
    """Distil gate/test failure evidence from the escalated state."""
    parts: list[str] = []
    if state.gate_decisions:
        parts.append(f"last gate result: {state.gate_decisions[-1]}")
    if state.review_results:
        last = state.review_results[-1]
        if not last.test_adequate and last.test_gaps:
            parts.append("test coverage gaps: " + "; ".join(last.test_gaps))
    return "\n".join(parts)


def build_evidence_packet(
    state: "CoordinatorState",
    task: "TaskStory",
    config: "ForgeConfig",
    workspace_path: "Path",
) -> EvidencePacket:
    """Assemble the fresh-advisor evidence packet from the escalated run's state.

    Pulls the issue body + acceptance criteria, the per-cycle review
    summaries/findings/verdicts, the dev diff, gate/test failures, and the final
    escalation reason. Never carries the dev/review agent sessions.
    """
    story_body = state.story_content or task.story_text or ""
    acceptance_criteria = _extract_acceptance_criteria(story_body)

    cycles: list[CycleEvidence] = []
    for entry in state.cycle_history:
        cycles.append(
            CycleEvidence(
                cycle=entry.cycle,
                verdict=entry.verdict,
                summary=entry.summary,
                findings=list(entry.p1_findings),
            )
        )
    # Fall back to review_results when cycle_history was not populated (e.g. the
    # reviewer pool never produced a candidate) so the packet is never cycle-empty.
    if not cycles and state.review_results:
        for i, rr in enumerate(state.review_results, start=1):
            cycles.append(
                CycleEvidence(
                    cycle=i,
                    verdict=rr.verdict,
                    summary=rr.summary,
                    findings=[f.description for f in rr.findings],
                )
            )

    reviewer_verdicts = {name: rr.verdict for name, rr in state.last_cycle_reviewer_results}
    final_verdict = state.review_results[-1].verdict if state.review_results else None

    base_branch = config.workspace.base_branch
    dev_diff = _capture_dev_diff(workspace_path, base_branch)
    test_failures = _capture_test_failures(state)

    return EvidencePacket(
        story_name=task.name,
        issue_ref=_issue_ref(task),
        issue_body=story_body,
        acceptance_criteria=acceptance_criteria,
        cycles=cycles,
        reviewer_verdicts=reviewer_verdicts,
        final_verdict=final_verdict,
        dev_diff=dev_diff,
        test_failures=test_failures,
        escalation_reason=state.error or state.escalate_reason or "ESCALATE",
        topology_signal=state.review_topology_signal or None,
        topology_triggered=bool(state.review_topology_triggered),
    )


#: "Did this agent ever reach the model?" is the subject of ``agent_failure``,
#: not of the advisor, so the classifier lives there and every advisory step
#: shares one answer. The local name is kept for this module's own call site.
_classify_advisor_launch_failure = classify_launch_failure


def run_escalation_advisor(
    state: "CoordinatorState",
    config: "ForgeConfig",
    task: "TaskStory",
    workspace_path: "Path",
) -> "AdvisoryReport | None":
    """Generate a fresh-context advisory report for an escalated story.

    Assembles the evidence packet, invokes the advisor agent in a fresh context
    (the preflight profile, run against a clean baseline checkout — never the
    failed dev/review sessions), parses the output through the schema boundary,
    and records the packet + report on ``state`` for the audit trail.

    Returns the parsed ``AdvisoryReport`` (which may carry ``parse_errors``), or
    ``None`` when the advisor could not be invoked at all. Callers treat a missing
    or errored report as "preserve the escalation" — never as an auto-decision.
    """
    _ensure_runners()
    state.advisory_generated = False
    state.advisory_report = None
    state.advisory_launch_failure = False
    state.advisory_launch_reason = None
    state.advisory_unavailable_reason = None

    packet = build_evidence_packet(state, task, config, workspace_path)
    state.advisory_packet = packet.to_dict()

    available_actions, _omitted_actions = available_escalate_actions(state, ACTION_TAXONOMY)
    prompt = build_advisor_prompt(packet, available_actions)
    try:
        profile = _select_advisor_profile(config)
    except NoCapableCandidateError as exc:
        state.advisory_unavailable_reason = str(exc)
        _log(f"  ⚠ advisor unavailable: {exc}")
        return None
    except ValueError as exc:
        state.advisory_unavailable_reason = str(exc)
        _log(f"  ⚠ advisor unavailable: {exc}")
        return None

    _log("─── Escalation Advisor (fresh context) ───")
    _log(f"  Profile: {profile.model}")
    _log(f"  Evidence: {len(packet.cycles)} cycle(s), issue {packet.issue_ref}")

    baseline_dir: "Path | None" = None
    cleanup = None
    try:
        baseline_dir, cleanup = prepare_baseline_checkout(
            config.project_root, config.workspace.base_branch
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log(f"  ⚠ advisor baseline checkout failed: {exc}; using workspace")
        baseline_dir = workspace_path

    try:
        result = run_agent(
            prompt=prompt,
            profile=profile,
            working_dir=baseline_dir,
            secrets=config.secrets,
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log(f"  ⚠ advisor invocation failed: {exc}")
        state.advisory_unavailable_reason = (
            f"advisor invocation failed before a usable report: {exc}"
        )
        return None
    finally:
        if cleanup is not None:
            cleanup()

    log_agent_result(result, "ESCALATION_ADVISOR")
    if not getattr(result, "success", False):
        launch_reason = _classify_advisor_launch_failure(result)
        if launch_reason is not None:
            # The advisor never reached the model: this is a defect in the
            # environment forge launched it into, not an investigation that
            # reached no conclusion. Record it as such (and as a measured $0.00)
            # so the operator checkpoint can say so and the run can be retried
            # after the configuration is repaired.
            state.advisory_launch_failure = True
            state.advisory_launch_reason = launch_reason
            _log(
                "  ⚠ advisor agent FAILED TO LAUNCH — forge configuration/tool-invocation "
                "defect; the model was never contacted and $0.00 was spent"
            )
            _log(f"     launch failure: {launch_reason}")
        else:
            _log("  ⚠ advisor agent returned failure — preserving escalation")
            state.advisory_unavailable_reason = "advisor returned failure before a usable report"
        return None

    report = parse_advisory_report(result.output or "")
    # Any policy assertion the advisor cited is adjudicated against the repo-local
    # ratified-policy registry, not against the advisor's own claim about it
    # (#2137). Resolution happens here, in coordinator control flow, so the
    # rendered advisory names the provenance class the operator can act on.
    report = resolve_advisory_assertions(report, load_policy_assertions(config.project_root))
    state.advisory_report = report.to_dict()
    state.advisory_generated = report.ok
    if report.ok:
        _log(
            f"  Advisory recommendation: {report.recommendation} ({len(report.options)} option(s))"
        )
    else:
        state.advisory_unavailable_reason = (
            "advisor output failed advisory-report validation: " + "; ".join(report.parse_errors)
        )
        _log(f"  ⚠ advisory report failed validation: {report.parse_errors}")
    return report
