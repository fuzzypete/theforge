"""Coordinator control flow for the preflight decomposition assessment (#2686).

One agent invocation, at the moment the preflight complexity gate opens and
before the operator is asked anything. It reads the evidence preflight already
assembled, and it produces one artifact: a candidate split, or a recorded
statement that none was produced. It decides nothing — the pause still offers
the same two actions, and this module never mutates the story, the tracker, or
the repository.

That non-mutation is a property of the *invocation*, not of the prompt.
:func:`seal_assessment_profile` sets a read-only tool surface with no shell, a
read-only sandbox, and a bounded budget and timeout; :func:`assessment_secrets`
hands over inference credentials only, so the assessment cannot authenticate as
the operator against a tracker even if it somehow found a way to try. The agent
runs against a clean baseline checkout rather than the story's worktree.

Three bounds, because this step exists to be cheaper than the planning spend it
displaces:

* **One attempt.** No retry, no model pool, no cross-review. An assessment that
  fails validation becomes a recorded absence, not a second invocation.
* **A budget below planning's.** :data:`ASSESSMENT_BUDGET_FRACTION` of the
  configured planning budget, and never more than the base profile allowed.
* **A timeout well inside the pause window.** The operator's notification is
  written *after* this call, so a hung assessment would delay the pause itself.
  It cannot: the invocation is capped at a fraction of the gate's wait.

Every failure path — unavailable model, launch failure, invocation exception,
agent failure, invalid output, atomic result — returns the same shape: no
assessment plus the reason. The gate opens either way.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from theforge.agent_types import COST_PROVIDER_REPORTED, COST_UNKNOWN
from theforge.config.auth import inference_only_secrets as assessment_secrets
from theforge.config.defaults import PREFLIGHT_READ_ONLY_TOOLS
from theforge.config.model_identity import PHASE_ADVISOR
from theforge.decomposition_assessment import (
    NONE_AGENT_FAILED,
    NONE_LAUNCH_FAILURE,
    NONE_UNAVAILABLE,
    AssessmentPacket,
    AssessmentResult,
    no_assessment,
    parse_decomposition_assessment,
)
from theforge.task.decomposition_assessment_prompts import (
    build_decomposition_assessment_prompt,
)
from theforge.task.story import extract_acceptance_criteria

from . import util as _cu

# Every shared helper below is imported from the module that owns the *concept*,
# not from whichever flow happened to write it first. An advisory step reaching
# into a phase module (or into a sibling flow) for a helper is what turned three
# of these into import cycles; the concepts themselves — a clean baseline
# checkout, "did this agent reach the model?", "which credentials may an
# advisory stage hold?", "what are this story's acceptance criteria?" — belong
# to nobody's phase in particular.
from .agent_failure import classify_launch_failure
from .baseline_checkout import prepare_baseline_checkout

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from theforge.config import ForgeConfig
    from theforge.task import TaskStory

    from .state import CoordinatorState

_log = _cu._log
_log_verbose = _cu._log_verbose

# ── Lazy runner slots (mirrors preflight_flow) ────────────────────────────────
# None until first call; tests may replace before invoking the assessment.
# Patch targets:
#   theforge.coordinator.preflight_decomposition_flow.run_agent
#   theforge.coordinator.preflight_decomposition_flow.log_agent_result
run_agent = None
log_agent_result = None

#: The assessment may spend at most this share of the configured planning
#: budget. The step's whole justification is being cheap relative to the spend
#: it might displace, so the bound is expressed against that spend rather than
#: against the preflight profile it happens to borrow a model from.
ASSESSMENT_BUDGET_FRACTION = 0.5

#: And at most this share of the gate's wait window, because the pause is
#: written after the assessment returns.
ASSESSMENT_TIMEOUT_FRACTION = 0.25

#: Never below this, so a short configured pause window cannot reduce the
#: assessment to an invocation that cannot finish.
ASSESSMENT_TIMEOUT_FLOOR_SECONDS = 60

#: Identifies the invocation in logs, the audit, and the invocation ledger.
ASSESSMENT_PROFILE_NAME = "preflight_decomposition_assessment"


@dataclass(frozen=True)
class AssessmentAttempt:
    """What one assessment attempt produced, and what it cost.

    ``invoked`` is the difference between "an agent ran and reported no cost"
    and "no agent ran at all". Only the first may poison a run's measured total;
    the second is a genuine, measured zero.
    """

    result: AssessmentResult
    invoked: bool = False
    cost_usd: float | None = None
    cost_provenance: str = COST_PROVIDER_REPORTED
    duration_s: float | None = None
    model: str | None = None
    profile_name: str | None = None


def _ensure_runners() -> None:
    global run_agent, log_agent_result
    if run_agent is not None and log_agent_result is not None:
        return
    import theforge.runners as _r  # noqa: PLC0415

    if run_agent is None:
        run_agent = _r.run_agent
    if log_agent_result is None:
        log_agent_result = _r.log_agent_result


def assessment_budget_usd(config: "ForgeConfig", base_profile: object) -> float:
    """The spend ceiling for one assessment.

    Half the planning budget when planning has one, so the artifact is bounded
    by the thing it exists to be cheaper than — and never above whatever the
    borrowed profile was already allowed, so a project that lowered its preflight
    budget does not silently get a more expensive assessment.
    """
    base = float(getattr(base_profile, "budget_usd", 0.0) or 0.0)
    planning = float(getattr(getattr(config, "plan", None), "budget_usd", 0.0) or 0.0)
    if planning <= 0:
        return base
    capped = planning * ASSESSMENT_BUDGET_FRACTION
    return min(base, capped) if base > 0 else capped


def assessment_timeout_seconds(base_profile: object, gate_wait_seconds: int) -> int:
    """The subprocess timeout for one assessment, bounded inside the pause window."""
    base = int(getattr(base_profile, "timeout_seconds", 0) or 0)
    window = max(
        ASSESSMENT_TIMEOUT_FLOOR_SECONDS,
        int(max(0, gate_wait_seconds) * ASSESSMENT_TIMEOUT_FRACTION),
    )
    return min(base, window) if base > 0 else window


def seal_assessment_profile(profile: object, *, budget_usd: float, timeout_seconds: int) -> object:
    """Return ``profile`` narrowed to what a non-mutating assessment may hold.

    The acceptance criterion is that the assessment mutates nothing, so the
    inability to write has to be mechanical. Four things are overridden here, at
    the one place every assessment invocation passes through, rather than
    trusted from whatever built the profile:

    * ``allowed_tools`` becomes :data:`PREFLIGHT_READ_ONLY_TOOLS` — read-only and
      specifically without a shell, which is the capability an issue mutation
      would need. It is *set*, never filtered: an empty ``allowed_tools`` means
      unrestricted at CLI dispatch, so a surface derived by subtraction would
      fail open on its most dangerous input.
    * ``sandbox_mode`` becomes ``read-only`` so the host sandbox refuses a write
      even if a tool were somehow granted.
    * ``budget_usd`` and ``timeout_seconds`` carry the bounds computed above.
    * ``name``/``phase`` identify the invocation in the audit trail.
    """
    return replace(
        profile,
        name=ASSESSMENT_PROFILE_NAME,
        phase=PHASE_ADVISOR,
        allowed_tools=PREFLIGHT_READ_ONLY_TOOLS,
        sandbox_mode="read-only",
        budget_usd=budget_usd,
        timeout_seconds=timeout_seconds,
    )


def _issue_ref(task: "TaskStory") -> str:
    if getattr(task, "github_issue", None):
        return f"#{task.github_issue}"
    return task.slug


def build_assessment_packet(
    state: "CoordinatorState", task: "TaskStory", *, score_provenance_note: str | None
) -> AssessmentPacket:
    """Assemble what the assessment reads, entirely from preflight's own output."""
    story_body = state.story_content or getattr(task, "story_text", "") or ""
    return AssessmentPacket(
        story_name=task.name,
        issue_ref=_issue_ref(task),
        story_body=story_body,
        acceptance_criteria=extract_acceptance_criteria(story_body),
        complexity_score=state.preflight_complexity_score,
        implementation_complexity_score=state.preflight_implementation_complexity_score,
        validation_complexity_score=state.preflight_validation_complexity_score,
        scope_exceeded=bool(state.preflight_scope_exceeded),
        score_provenance_note=score_provenance_note,
        likely_files=[str(f) for f in (state.preflight_likely_files or [])],
        warnings=[str(w) for w in (state.preflight_warnings or [])],
        criteria_checked=[
            entry for entry in (state.preflight_criteria_checked or []) if isinstance(entry, dict)
        ],
    )


def _unavailable(reason: str) -> AssessmentAttempt:
    """No agent ran: a measured zero, not an unmeasured one."""
    return AssessmentAttempt(
        result=no_assessment(f"{NONE_UNAVAILABLE} ({reason})"),
        invoked=False,
        cost_usd=0.0,
        cost_provenance=COST_PROVIDER_REPORTED,
    )


def generate_decomposition_assessment(
    state: "CoordinatorState",
    config: "ForgeConfig",
    task: "TaskStory",
    *,
    gate_wait_seconds: int,
    score_provenance_note: str | None = None,
) -> AssessmentAttempt:
    """Produce one decomposition assessment for a story held at the gate.

    Never raises: every path returns an :class:`AssessmentAttempt`, and the
    caller opens the same pause whether or not an artifact came back.
    """
    _ensure_runners()

    base_profile = getattr(config, "preflight_profile", None)
    if base_profile is None:
        return _unavailable("no preflight profile is configured")

    packet = build_assessment_packet(state, task, score_provenance_note=score_provenance_note)
    prompt = build_decomposition_assessment_prompt(packet)
    timeout_seconds = assessment_timeout_seconds(base_profile, gate_wait_seconds)
    profile = seal_assessment_profile(
        base_profile,
        budget_usd=assessment_budget_usd(config, base_profile),
        timeout_seconds=timeout_seconds,
    )

    _log("  ⋯ PREFLIGHT gate  producing a decomposition assessment before asking")
    _log_verbose(
        f"     model {getattr(profile, 'model', '?')}, "
        f"budget ${getattr(profile, 'budget_usd', 0.0):.2f}, timeout {timeout_seconds}s, "
        f"{len(packet.acceptance_criteria)} acceptance criteria"
    )

    baseline_dir: "Path | None" = None
    cleanup = None
    try:
        baseline_dir, cleanup = prepare_baseline_checkout(
            config.project_root, config.workspace.base_branch
        )
    except Exception as exc:  # noqa: BLE001 - a checkout failure is not an assessment
        _log_verbose(f"     assessment baseline checkout failed: {exc}")
        return _unavailable(f"baseline checkout failed: {exc}")

    started = time.monotonic()
    try:
        agent_result = run_agent(
            prompt=prompt,
            profile=profile,
            working_dir=baseline_dir,
            secrets=assessment_secrets(getattr(config, "secrets", None)),
        )
    except Exception as exc:  # noqa: BLE001 - an unusable agent is not an assessment
        _log(f"  ⚠ decomposition assessment invocation failed: {exc}")
        return AssessmentAttempt(
            result=no_assessment(f"{NONE_UNAVAILABLE} (invocation failed: {exc})"),
            invoked=False,
            cost_usd=0.0,
            duration_s=round(time.monotonic() - started, 2),
            model=getattr(profile, "model", None),
            profile_name=ASSESSMENT_PROFILE_NAME,
        )
    finally:
        if cleanup is not None:
            cleanup()

    duration = round(time.monotonic() - started, 2)
    log_agent_result(agent_result, "PREFLIGHT_DECOMPOSITION_ASSESSMENT")

    cost_usd = getattr(agent_result, "cost_usd", None)
    provenance = (
        COST_UNKNOWN
        if cost_usd is None
        else str(getattr(agent_result, "cost_provenance", COST_PROVIDER_REPORTED) or COST_UNKNOWN)
    )

    def _attempt(result: AssessmentResult) -> AssessmentAttempt:
        return AssessmentAttempt(
            result=result,
            invoked=True,
            cost_usd=cost_usd,
            cost_provenance=provenance,
            duration_s=duration,
            model=getattr(profile, "model", None),
            profile_name=ASSESSMENT_PROFILE_NAME,
        )

    if not getattr(agent_result, "success", False):
        launch_reason = classify_launch_failure(agent_result)
        if launch_reason is not None:
            _log(f"  ⚠ decomposition assessment never launched: {launch_reason}")
            return _attempt(no_assessment(f"{NONE_LAUNCH_FAILURE}: {launch_reason}"))
        _log("  ⚠ decomposition assessment agent returned failure — asking without one")
        return _attempt(no_assessment(NONE_AGENT_FAILED))

    parsed = parse_decomposition_assessment(
        getattr(agent_result, "output", "") or "", packet.acceptance_criteria
    )
    if parsed.produced:
        _log(
            f"  ✎ PREFLIGHT gate  assessment: {len(parsed.assessment.slices)} candidate "
            f"slice(s), {len(parsed.assessment.unsettled)} unsettled"
        )
    else:
        _log(f"  ✎ PREFLIGHT gate  no assessment — {parsed.none_produced_reason}")
        if parsed.validation_errors:
            _log_verbose(f"     validation errors: {list(parsed.validation_errors)}")
    return _attempt(parsed)
