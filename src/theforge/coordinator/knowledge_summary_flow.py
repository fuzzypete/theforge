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

from dataclasses import replace
from typing import TYPE_CHECKING

from theforge.knowledge_summary import (
    SummaryValidationError,
    build_summary_artifact,
    extract_anchors,
    parse_summary_output,
    summary_exists,
    validate_proposed_summary,
    write_summary,
)
from theforge.task.summary_prompts import build_run_summary_prompt

from . import util as _cu

if TYPE_CHECKING:
    from pathlib import Path

    from theforge.config import ForgeConfig, ModelProfile
    from theforge.coordinator.state import CoordinatorResult

_log = _cu._log

# Lazy runner slot (mirrors escalation_advisor_flow): None until first call so
# tests can replace it. Patch target:
#   theforge.coordinator.knowledge_summary_flow.run_agent
run_agent = None


def _ensure_runner() -> None:
    global run_agent
    if run_agent is not None:
        return
    import theforge.runners as _r  # noqa: PLC0415

    run_agent = _r.run_agent


def _summary_profile(config: "ForgeConfig") -> "ModelProfile | None":
    """Derive a tool-free API profile for the summary agent, or None.

    Built from ``config.plan.ref`` — the plan model is the cheap bounded-writing
    role and the summary is the same kind of work. A CLI-transport ref is
    projected onto its configured same-provider API fallback; without one there
    is no dispatch path that is mechanically tool-free, and the caller skips.
    """
    from theforge.config.bridge import model_ref_to_profile  # noqa: PLC0415

    ref = getattr(getattr(config, "plan", None), "ref", None)
    if ref is None:
        return None

    profile = model_ref_to_profile(
        "knowledge_summary",
        ref,
        allowed_tools=(),
        phase="knowledge_summary",
        sandbox_mode="read-only",
    )
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
) -> "Path | None":
    """Generate and persist this run's knowledge summary; return its path or None.

    Never raises. Returns None whenever the summary was not written, for any
    reason — the run's outcome and its audit trail are identical either way.
    """
    try:
        run_id = str(audit.get("run_id") or "")
        if not _should_generate(config, result, run_id):
            return None

        anchors = extract_anchors(audit)
        if anchors.is_empty():
            _log("  ⚠ knowledge summary skipped: run offers no citable evidence")
            return None

        profile = _summary_profile(config)
        if profile is None:
            _log(
                "  ⚠ knowledge summary skipped: no tool-free API transport available "
                "for the plan model (configure transport_fallback to enable summaries)"
            )
            return None

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
            _log("  ⚠ knowledge summary skipped: summary agent returned failure")
            return None

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
        return path
    except SummaryValidationError as exc:
        _log(f"  ⚠ knowledge summary rejected: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001 — a side effect must never break a finished run
        _log(f"  ⚠ knowledge summary failed: {exc}")
        return None
