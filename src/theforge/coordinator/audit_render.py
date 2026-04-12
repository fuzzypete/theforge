"""Audit rendering helpers for coordinator audit logs."""

from __future__ import annotations

from theforge.config import ForgeConfig

from .state import CoordinatorState


def _model_usage_entries(r: object) -> list[dict]:
    """Build the model_usage list for an AgentResult."""
    from theforge.agent_types import AgentResult  # noqa: PLC0415

    assert isinstance(r, AgentResult)
    return [
        {
            "model": u.model,
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_read_tokens": u.cache_read_tokens,
            "cache_creation_tokens": u.cache_creation_tokens,
            "cost_usd": u.cost_usd,
        }
        for u in r.model_usage
    ]


def _agent_entry(r: object, role: str, profile_fallback: str, dur: float | None) -> dict:
    """Build a single audit agent entry, recording model_used and model_config when present.

    model_used is sourced from the explicit AgentResult.model_used field (set by all
    runners for CLI profiles and by API runners when a preference list is configured).
    Falls back to model_usage[-1].model for API results that predate this field.
    """
    from theforge.agent_types import AgentResult  # noqa: PLC0415

    assert isinstance(r, AgentResult)
    entry: dict = {
        "role": role,
        "profile": r.profile_name or profile_fallback,
        "cost_usd": r.cost_usd,
        "duration_seconds": dur,
    }
    if r.model_usage:
        entry["model_usage"] = _model_usage_entries(r)
    # model_used: prefer the explicit field (set by runners for CLI and preference-list
    # API profiles); fall back to model_usage for legacy single-model API results.
    model_for_audit = r.model_used or (r.model_usage[-1].model if r.model_usage else None)
    if model_for_audit:
        entry["model_used"] = model_for_audit
    if r.model_config:
        # Preference list configured for this profile — present whenever list has >1 entry
        entry["model_config"] = list(r.model_config)
    return entry


def build_agent_entries(state: CoordinatorState, config: ForgeConfig) -> list[dict]:
    """Build the per-agent cost/timing breakdown for audit output."""
    agents: list[dict] = []
    for i, r in enumerate(state.dev_results):
        dur = state.dev_durations[i] if i < len(state.dev_durations) else None
        agents.append(_agent_entry(r, "dev", config.dev_profile.name, dur))
    for r in state.dev_handoff_fix_results:
        agents.append(_agent_entry(r, "dev/handoff-fix", config.dev_profile.name, None))
    for i, r in enumerate(state.review_agent_results):
        dur = state.review_durations[i] if i < len(state.review_durations) else None
        role = "synthesis" if r.profile_name == "synthesis" else "review"
        agents.append(_agent_entry(r, role, r.profile_name, dur))
    return agents


def build_reviews(state: CoordinatorState) -> list[dict]:
    """Build the per-cycle review audit list."""
    reviews = []
    for i, meta in enumerate(state.review_cycle_metadata):
        entry: dict = {
            "cycle": i + 1,
            "pool_models": meta.pool_models,
            "successful": meta.successful,
            "failed": meta.failed,
            "failed_detail": meta.failed_detail,
            "synthesized": meta.synthesized,
            "parse_retries": meta.parse_retries,
        }
        if i < len(state.review_results):
            r = state.review_results[i]
            findings_list = [
                {
                    "severity": f.severity,
                    "file": f.file,
                    "line": f.line,
                    "description": f.description,
                    "reviewers": list(f.reviewers),
                }
                for f in r.findings
            ]
            entry.update(
                {
                    "verdict": r.verdict,
                    "summary": r.summary,
                    "p1_count": sum(1 for f in r.findings if f.severity == "P1"),
                    "p2_count": sum(1 for f in r.findings if f.severity == "P2"),
                    "findings": findings_list,
                }
            )
        reviews.append(entry)
    return reviews
