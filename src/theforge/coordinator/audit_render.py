"""Audit rendering helpers for coordinator audit logs."""

from __future__ import annotations

from theforge.config import ForgeConfig

from .agent_identity import canonicalize_identity
from .state import CoordinatorState, ReviewedCommitVerification

# Version of the per-invocation ledger block written into each ``cost.agents``
# entry (#2205). Bumped when the ledger's own shape changes, independently of
# the record schema version, so a reader can tell which ledger contract a given
# entry was written under without consulting the record.
INVOCATION_LEDGER_VERSION = 1


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


def _identity_block(raw: object, transport: object) -> dict | None:
    """Render one identity as ``raw`` + canonical projection, or None.

    Both halves are kept. The raw spelling is what the runner actually reported
    and is the only thing that stays true if the catalog changes underneath the
    record; the canonical id is what a consumer groups by. Collapsing to either
    one alone is how an alias and the concrete model it resolved to became
    indistinguishable in the first place.
    """
    value = str(raw or "").strip()
    if not value:
        return None
    transport_value = str(transport or "").strip() or None
    identity, resolution = canonicalize_identity(
        value, {"transport_used": transport_value} if transport_value else None
    )
    return {
        "raw": value,
        "transport": transport_value,
        "identity": identity,
        "resolution": resolution,
    }


def _billed_components(r: object) -> list[dict]:
    """Render every model billed inside this invocation as its own identity.

    A component here is emphatically *not* the identity of the invocation — a
    strong-tier dev run bills a small component model that was never configured
    and may not be in the catalog at all. It is recorded as a separate,
    separately-attributable identity so the spend attaches to the model that
    incurred it instead of being folded into whichever identity survived.
    """
    from theforge.agent_types import AgentResult  # noqa: PLC0415

    assert isinstance(r, AgentResult)
    components: list[dict] = []
    for u in r.model_usage:
        components.append(
            {
                "identity": _identity_block(u.model, r.transport_used),
                "input_tokens": u.input_tokens,
                "output_tokens": u.output_tokens,
                "cache_read_tokens": u.cache_read_tokens,
                "cache_creation_tokens": u.cache_creation_tokens,
                "thinking_tokens": u.thinking_tokens,
                "cost_usd": u.cost_usd,
                "cost_provenance": u.cost_provenance,
            }
        )
    return components


def build_invocation_ledger(
    r: object,
    role: str,
    profile_fallback: str,
    *,
    complexity: str | None = None,
    complexity_score: int | None = None,
) -> dict:
    """Build the per-invocation ledger block for one AgentResult (#2205).

    One invocation carries three distinct identities — what was configured, what
    that resolved to, and what was billed — plus the conditions it ran under
    (role, complexity, reasoning effort) and its usage and cost. Every one of
    those is a separate value here. The collapsed ``model_used``/``model_config``
    fields remain on the entry beside this block as compatibility projections,
    but they are views of the ledger, not the record.
    """
    from theforge.agent_types import AgentResult  # noqa: PLC0415

    assert isinstance(r, AgentResult)
    configured = _identity_block(r.configured_model, r.configured_transport)
    resolved = _identity_block(
        r.model_used or (r.model_usage[-1].model if r.model_usage else None),
        r.transport_used,
    )
    # Compare on the canonical projection when both resolved onto it — that is
    # the whole point of canonicalizing — and fall back to the raw spellings so
    # two unresolvable values are still comparable. Null when either side is
    # absent: "not recorded" is not "the same".
    differs: bool | None
    if configured is None or resolved is None:
        differs = None
    elif configured["resolution"] == "canonical" and resolved["resolution"] == "canonical":
        differs = configured["identity"] != resolved["identity"]
    else:
        differs = configured["raw"] != resolved["raw"]
    return {
        "version": INVOCATION_LEDGER_VERSION,
        "role": role,
        "profile": r.profile_name or profile_fallback,
        "complexity": complexity,
        "complexity_score": complexity_score,
        "reasoning_effort": r.reasoning_effort,
        "cost_usd": r.cost_usd,
        "cost_provenance": r.cost_provenance,
        "configured_identity": configured,
        "resolved_primary_identity": resolved,
        "configured_differs_from_resolved": differs,
        "usage": {
            "input_tokens": sum(u.input_tokens for u in r.model_usage),
            "output_tokens": sum(u.output_tokens for u in r.model_usage),
            "cache_read_tokens": sum(u.cache_read_tokens for u in r.model_usage),
            "cache_creation_tokens": sum(u.cache_creation_tokens for u in r.model_usage),
            "thinking_tokens": sum(u.thinking_tokens for u in r.model_usage),
        },
        "billed_components": _billed_components(r),
    }


def _agent_entry(
    r: object,
    role: str,
    profile_fallback: str,
    dur: float | None,
    *,
    complexity: str | None = None,
    complexity_score: int | None = None,
) -> dict:
    """Build a single audit agent entry: the invocation ledger plus legacy views.

    ``ledger`` (#2205) is the record. ``model_used``, ``model_config``,
    ``model_usage`` and ``transport_used`` are retained beside it as
    compatibility projections for readers written against the pre-ledger shape;
    they say strictly less than the ledger does and nothing new should be built
    on them.
    """
    from theforge.agent_types import AgentResult  # noqa: PLC0415

    assert isinstance(r, AgentResult)
    entry: dict = {
        "role": role,
        "profile": r.profile_name or profile_fallback,
        "cost_usd": r.cost_usd,
        "duration_seconds": dur,
        "success": r.success,
        "exit_code": r.exit_code,
        "ledger": build_invocation_ledger(
            r,
            role,
            profile_fallback,
            complexity=complexity,
            complexity_score=complexity_score,
        ),
    }
    if r.failure_code:
        entry["failure_code"] = r.failure_code
    if r.startup_failure:
        entry["startup_failure"] = True
    if r.model_usage:
        entry["model_usage"] = _model_usage_entries(r)
    # model_used: prefer the explicit field (set by all runners); fall back to
    # model_usage for legacy single-model API results. Compatibility view of
    # ``ledger.resolved_primary_identity``.
    model_for_audit = r.model_used or (r.model_usage[-1].model if r.model_usage else None)
    if model_for_audit:
        entry["model_used"] = model_for_audit
    if r.model_config:
        # Preference list configured for this profile — present whenever list has >1 entry
        entry["model_config"] = list(r.model_config)
    if r.transport_used:
        # The transport that actually served. Recorded because it is the hint
        # that makes a bare ``model_used`` (e.g. ``gpt-5.4``, offered over both
        # CLI and API) resolvable to one canonical identity (#2225).
        entry["transport_used"] = r.transport_used
    return entry


def build_agent_entries(state: CoordinatorState, config: ForgeConfig) -> list[dict]:
    """Build the per-agent cost/timing breakdown for audit output.

    Covers every phase that invokes an agent — preflight, plan, plan review,
    dev, review, synthesis — so "every agent invocation records its ledger" is
    a property of this list rather than of the two phases that happened to be
    here before (#2205). Complexity is stamped from the run's preflight
    classification, which is the condition each of those invocations ran under.
    """
    from theforge.agent_types import AgentResult  # noqa: PLC0415

    agents: list[dict] = []
    complexity = state.preflight_complexity
    complexity_score = state.preflight_complexity_score

    def _add(r: object, role: str, profile_fallback: str, dur: float | None) -> None:
        agents.append(
            _agent_entry(
                r,
                role,
                profile_fallback,
                dur,
                complexity=complexity,
                complexity_score=complexity_score,
            )
        )

    # ``isinstance`` rather than a bare None check, because this is the one
    # AgentResult the renderer reads that is a single field rather than a
    # phase-appended list: ``preflight_result`` is reassigned across the
    # parse-retry/fallback path and read duck-typed elsewhere in the writer
    # (``audit.py`` takes ``.cost_usd`` off it without a type check). Rendering
    # the audit must never be the thing that kills a run — an unreadable
    # preflight result costs this one entry, not the entire record, and an
    # entry is not fabricated from an object whose fields cannot be trusted to
    # be identities.
    if isinstance(state.preflight_result, AgentResult):
        # Only the final preflight attempt lands here; the earlier parse-retry
        # and fallback attempts carry their own ledger inside
        # ``preflight.attempts`` (see preflight_flow._record_attempt).
        _add(
            state.preflight_result,
            "preflight",
            config.preflight_profile.name if config.preflight_profile else "preflight",
            state.preflight_duration_s,
        )
    for i, r in enumerate(state.plan_results):
        dur = state.plan_durations[i] if i < len(state.plan_durations) else None
        _add(r, "plan", r.profile_name or "plan", dur)
    for i, r in enumerate(state.plan_review_results):
        dur = state.plan_review_durations[i] if i < len(state.plan_review_durations) else None
        _add(r, "plan_review", r.profile_name or "plan_review", dur)
    for i, r in enumerate(state.dev_results):
        dur = state.dev_durations[i] if i < len(state.dev_durations) else None
        _add(r, "dev", config.dev_profile.name, dur)
    for i, r in enumerate(state.review_agent_results):
        dur = state.review_durations[i] if i < len(state.review_durations) else None
        role = "synthesis" if r.profile_name == "synthesis" else "review"
        _add(r, role, r.profile_name, dur)
    return agents


def build_reviews(state: CoordinatorState) -> list[dict]:
    """Build the per-cycle review audit list."""
    reviews = []
    for i, meta in enumerate(state.review_cycle_metadata):
        entry: dict = {
            "cycle": i + 1,
            # Provenance of what this cycle judged (#2052). Without it a stored
            # verdict is indistinguishable from one later commits superseded.
            # Metadata from older runs carries neither, and renders as an
            # explicit "unknown" verification state rather than as current.
            "commit": meta.reviewed_commit,
            "verification": (
                meta.verification.to_audit_dict()
                if meta.verification is not None
                else ReviewedCommitVerification().to_audit_dict()
            ),
            "pool_models": meta.pool_models,
            "successful": meta.successful,
            # Which concrete model produced each reviewer's output for THIS
            # cycle (#2226). A parse retry can be served by a different version
            # than the invocation it superseded, so the cycle's own attribution
            # is not reconstructable from the per-invocation records alone —
            # it has to be visible in the record it describes.
            "resolved_by_reviewer": dict(getattr(meta, "resolved_by_reviewer", None) or {}),
            "failed": meta.failed,
            "failed_detail": meta.failed_detail,
            "synthesized": meta.synthesized,
            "parse_retries": meta.parse_retries,
            "transient_retries": dict(meta.transient_retries),
            "transient_outcomes": dict(meta.transient_outcomes),
            "quorum_threshold": meta.quorum_threshold,
            "quorum_met": meta.quorum_met,
            "degraded_quorum": meta.degraded_quorum,
            "degraded_quorum_warning": meta.degraded_quorum_warning,
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
                    "reporter": f.reporter,
                }
                for f in r.findings
            ]
            ac_verification_list = [
                {
                    "criterion": ac.criterion,
                    "status": ac.status,
                    "evidence": ac.evidence,
                }
                for ac in r.ac_verification
            ]
            parse_errors_list = [
                {"stage": pe.stage, "message": pe.message} for pe in r.parse_errors
            ]
            entry.update(
                {
                    "verdict": r.verdict,
                    "summary": r.summary,
                    "sanitization_audit": r.sanitization_audit or None,
                    "p1_count": sum(1 for f in r.findings if f.severity == "P1"),
                    "p2_count": sum(1 for f in r.findings if f.severity == "P2"),
                    "findings": findings_list,
                    "ac_verification": ac_verification_list,
                    "criteria_enumerable": r.criteria_enumerable,
                    "criteria_enumerable_rationale": (r.criteria_enumerable_rationale or None),
                    "parse_errors": parse_errors_list,
                }
            )
        reviews.append(entry)
    return reviews
