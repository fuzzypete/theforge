"""forge explain subcommand — operator-facing render of routing_decision (#270).

Reads the top-level ``routing_decision`` block that the router records for each
run (#1391, ADR-0006 clause 7) and renders one coherent per-role assignment
summary: selected agent, excluded candidates with canonical reasons, the profile
signals the router weighed, adaptive-mechanism outcomes (distinguishing
not-checked / checked-did-not-fire / checked-and-fired), exploration state, and
the final rationale.

This is a read-only convenience over the recorded block — it invokes no agents,
re-reads no profiles, and never writes. The block is the contract; this view is
one presentation of it.

The block reaches the audit substrate only when a story finishes, so a story
that is running now — or that ``forge stop`` killed mid-flight — is looked up in
the stores that *do* hold its decision while it is unfinished (see
:mod:`theforge.cli.explain_live`). That is the state in which the question is
usually asked, and a decision that cannot be retrieved when asked has the cost
of being recorded and none of the benefit (#2923).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from theforge.cli import explain_live
from theforge.cli.shared import _find_config
from theforge.coordinator import audit_substrate

# Roles rendered in routing order (preflight → planner → dev → reviewers).
_ROLE_ORDER = ("preflight", "planner", "dev", "plan_review", "code_review")
_ROLE_LABELS = {
    "preflight": "PREFLIGHT",
    "planner": "PLANNER",
    "dev": "DEV",
    "plan_review": "PLAN REVIEW",
    "code_review": "CODE REVIEW",
}

# Human-readable expansion for the canonical exclusion-reason codes so the
# excluded-candidate line answers "why was model X never a reviewer" directly.
_REASON_TEXT = {
    "none": "included",
    "auth_missing": "excluded — provider credentials missing",
    "transport_unavailable": "excluded — transport unavailable",
    "tier_mismatch": "excluded — tier does not match role floor",
    "anti_self_review": "excluded — anti-self-review (same model as the code under review)",
    "phase_eligibility": "excluded — not eligible for this phase",
    "explicit_override_locked": "preserved — locked by explicit forge.yaml override",
}

# Adaptive-mechanism tri-state glyphs (AC: absence of a mechanism must not be
# confused with a failed condition).
_STATE_NOT_CHECKED = ("○", "not checked")
_STATE_DID_NOT_FIRE = ("◐", "checked, did not fire")
_STATE_FIRED = ("●", "fired")


def _reason_text(reason: str | None, detail: str | None = None) -> str:
    base = _REASON_TEXT.get(reason or "", f"excluded — {reason}" if reason else "excluded")
    if detail:
        return f"{base} ({detail})"
    return base


def _mechanism_state(*, checked: bool, fired: bool) -> tuple[str, str]:
    """Classify an adaptive mechanism into the tri-state render label."""
    if not checked:
        return _STATE_NOT_CHECKED
    if fired:
        return _STATE_FIRED
    return _STATE_DID_NOT_FIRE


def _fmt_signal(signal: dict) -> str:
    """Render a consulted profile signal with raw + weighted + floor status."""
    raw = signal.get("raw")
    weighted = signal.get("weighted")
    runs = signal.get("runs")
    floor = signal.get("floor")
    raw_s = f"{raw:.4f}" if isinstance(raw, (int, float)) else "—"
    weighted_s = f"{weighted:.4f}" if isinstance(weighted, (int, float)) else "—"
    floor_s = {"pass": "sample-floor pass", "fail": "sample-floor fail"}.get(
        floor or "", f"sample-floor {floor or '?'}"
    )
    runs_s = runs if runs is not None else "?"
    return f"raw={raw_s} weighted={weighted_s} runs={runs_s} ({floor_s})"


def _render_candidate_pool(pool: list[dict], lines: list[str]) -> None:
    for entry in pool or []:
        name = entry.get("name", "?")
        tier = entry.get("tier")
        included = bool(entry.get("included"))
        reason = entry.get("reason")
        detail = entry.get("detail")
        glyph = "✓" if included else "✗"
        tier_s = f" [{tier}]" if tier else ""
        lines.append(f"    {glyph} {name}{tier_s} — {_reason_text(reason, detail)}")
        signals = entry.get("signals") or {}
        success_rate = signals.get("success_rate")
        if isinstance(success_rate, dict):
            lines.append(f"        success_rate: {_fmt_signal(success_rate)}")
        cost_tiebreak = signals.get("cost_tiebreak")
        if isinstance(cost_tiebreak, dict):
            cohort = cost_tiebreak.get("cohort") or {}
            role = cohort.get("role", "?")
            complexity = cohort.get("complexity", "?")
            effort = cohort.get("reasoning_effort", "?")
            lines.append(
                "        cost_tiebreak: "
                f"value={cost_tiebreak.get('value')} source={cost_tiebreak.get('source')} "
                f"observations={cost_tiebreak.get('observations')} "
                f"cohort={role}/{complexity}/{effort}"
            )


def _render_score_policy(role_block: dict, lines: list[str]) -> None:
    """Render the per-axis score-to-routing policy (#1019) for a role.

    One line per axis naming the applied score bucket, its covering range, the
    thresholds, and the selected output — the operator-facing view of what the
    complexity score actually controlled. Axes not driven by the score (or with
    no numeric score) print their recorded reason instead of a fabricated bucket.
    """
    policy = role_block.get("score_policy") or {}
    if not isinstance(policy, dict) or not policy:
        return
    lines.append("  score policy:")
    for axis in policy.values():
        if not isinstance(axis, dict):
            continue
        name = axis.get("axis", "?")
        if axis.get("applied"):
            bucket = axis.get("bucket")
            rng = axis.get("range")
            thresholds = axis.get("thresholds")
            output = axis.get("output")
            detail = f"score={axis.get('score')} → bucket={bucket} range={rng} output={output}"
            if isinstance(axis.get("resolved_count"), int):
                detail += f" resolved_count={axis['resolved_count']}"
            if isinstance(axis.get("seated_count"), int):
                detail += f" seated={axis['seated_count']}"
            if thresholds:
                detail += f" (thresholds {thresholds})"
        else:
            detail = f"not applied — {axis.get('reason', 'not_score_controlled')}"
        lines.append(f"    {name}: {detail}")


def _render_mechanism(label: str, state: tuple[str, str], detail: str, lines: list[str]) -> None:
    glyph, state_text = state
    suffix = f" — {detail}" if detail else ""
    lines.append(f"    {glyph} {label}: {state_text}{suffix}")


def _render_dev_mechanisms(role_block: dict, lines: list[str]) -> None:
    promo = role_block.get("promotion_check") or {}
    # outcome == "not_checked" is the sentinel for a mechanism that never ran.
    promo_checked = promo.get("outcome") != "not_checked"
    promo_fired = bool(promo.get("fired"))
    # Profile-backed dev pre-promotion (#158): the evidence is the recency-weighted
    # success rate over the admissible sample vs. the configured threshold.
    _weighted = promo.get("weighted_success_rate")
    _weighted_txt = f"{_weighted:.2f}" if isinstance(_weighted, (int, float)) else "n/a"
    _threshold = promo.get("threshold")
    _threshold_txt = f"{_threshold:.2f}" if isinstance(_threshold, (int, float)) else "n/a"
    detail = (
        f"{promo.get('outcome', '?')} "
        f"(weighted rate {_weighted_txt} vs threshold {_threshold_txt} "
        f"over {promo.get('sample_size', 0)} admissible runs)"
        if promo_checked
        else "no promotion signal recorded"
    )
    _render_mechanism(
        "promotion", _mechanism_state(checked=promo_checked, fired=promo_fired), detail, lines
    )

    demo = role_block.get("demotion_check") or {}
    # applicable=False → no such mechanism exists in v1 (a complete explanation,
    # not a gap). Otherwise the recorded fired flag drives the state.
    demo_applicable = bool(demo.get("applicable", True))
    _render_mechanism(
        f"demotion ({demo.get('mechanism', '?')})",
        _mechanism_state(checked=demo_applicable, fired=bool(demo.get("fired"))),
        demo.get("reason", ""),
        lines,
    )

    checkpoint = role_block.get("post_plan_checkpoint") or {}
    # New shape (#1387): fired/decision/baseline_tier/final_tier/rationale. The
    # checkpoint only runs after plan-review, so a "pending" decision (or a legacy
    # applied/reason block) means it never ran for this story.
    if "decision" in checkpoint or "rationale" in checkpoint:
        decision = checkpoint.get("decision", "pending")
        fired = bool(checkpoint.get("fired"))
        checked = decision not in ("pending", "not_run")
        rationale = checkpoint.get("rationale") or checkpoint.get("reason", "")
        baseline = checkpoint.get("baseline_tier")
        final_tier = checkpoint.get("final_tier")
        if checked and baseline is not None and final_tier is not None:
            detail = f"{decision} ({baseline} → {final_tier}) — {rationale}".rstrip(" —")
        elif checked:
            detail = f"{decision} — {rationale}".rstrip(" —")
        else:
            detail = rationale
        _render_mechanism(
            "post-plan checkpoint",
            _mechanism_state(checked=checked, fired=fired),
            detail,
            lines,
        )
    else:
        # Legacy applied/reason placeholder — tolerate defensively.
        applied = bool(checkpoint.get("applied"))
        _render_mechanism(
            "post-plan checkpoint",
            _mechanism_state(checked=applied, fired=applied),
            checkpoint.get("reason", ""),
            lines,
        )

    runtime_escalation = role_block.get("persistent_p1_dev_escalation") or {}
    if runtime_escalation:
        signal = runtime_escalation.get("signal") or {}
        model_swap = runtime_escalation.get("model_swap") or {}
        descriptions = signal.get("descriptions") or []
        desc_text = f"; findings: {'; '.join(descriptions)}" if descriptions else ""
        detail = (
            f"{signal.get('kind', 'persistent_p1')} in {signal.get('file', '?')} "
            f"(cycle {signal.get('review_cycle', '?')}) "
            f"{model_swap.get('from_model', '?')} → {model_swap.get('to_model', '?')} "
            f"[scope={runtime_escalation.get('scope', '?')}, "
            f"return={runtime_escalation.get('return_path', '?')}]"
            f"{desc_text}"
        )
        _render_mechanism(
            "in-run persistent-P1 escalation",
            _mechanism_state(checked=True, fired=bool(runtime_escalation.get("fired", True))),
            detail,
            lines,
        )


def _render_planner_mechanisms(role_block: dict, lines: list[str]) -> None:
    escalation = role_block.get("plan_model_escalation") or {}
    if not escalation:
        return
    signal = escalation.get("signal") or {}
    model_swap = escalation.get("model_swap") or {}
    detail = (
        f"{signal.get('kind', 'consecutive_plan_rejections')} "
        f"({signal.get('rejections', '?')} rejection(s)) "
        f"{model_swap.get('from_model', '?')} → {model_swap.get('to_model', '?')} "
        f"[scope={escalation.get('scope', '?')}, "
        f"return={escalation.get('return_path', '?')}]"
    )
    _render_mechanism(
        "in-run plan-model escalation",
        _mechanism_state(checked=True, fired=bool(escalation.get("fired", True))),
        detail,
        lines,
    )


def _render_reviewer_mechanisms(role_block: dict, lines: list[str]) -> None:
    demo = role_block.get("demotion_check") or {}
    reason = demo.get("reason", "")
    # Reviewer provider-health demotion is a LIVE mechanism the router always
    # runs, so whenever the demotion_check block is present it WAS checked —
    # `fired` alone distinguishes fired vs did-not-fire. A no-op outcome
    # (e.g. reason "no_unhealthy_candidates") is "checked, did not fire", not
    # "not checked". Only a wholly-absent block reads as not checked.
    checked = bool(demo)
    detail = reason
    depri = demo.get("deprioritized") or []
    if depri:
        detail = f"{reason} — deprioritized: {', '.join(depri)}"
    _render_mechanism(
        f"demotion ({demo.get('mechanism', '?')})",
        _mechanism_state(checked=checked, fired=bool(demo.get("fired"))),
        detail,
        lines,
    )
    _render_reviewer_value_check(role_block.get("value_check") or {}, lines)


def _fmt_value_signal(signal: dict) -> str:
    """Render a uniqueness / latency-per-P1 sub-signal (raw + weighted + floor)."""
    raw = signal.get("raw")
    weighted = signal.get("weighted")
    floor = signal.get("floor")
    raw_s = f"{raw:.4f}" if isinstance(raw, (int, float)) else "—"
    weighted_s = f"{weighted:.4f}" if isinstance(weighted, (int, float)) else "—"
    floor_s = {"pass": "floor pass", "fail": "floor fail"}.get(
        floor or "", f"floor {floor or '?'}"
    )
    return f"raw={raw_s} weighted={weighted_s} ({floor_s})"


def _render_reviewer_value_check(value: dict, lines: list[str]) -> None:
    """Render a reviewer value_check (#1443/#2156): P1-uniqueness + wall-clock cost.

    Surfaces, per consulted reviewer, the uniqueness rate and latency-per-P1 so an
    operator can answer "is this reviewer earning its wall-clock cost?" directly
    from the explain view. Absent block → mechanism not checked (opt-in/disabled).
    Rendered per reviewer role, so a plan-review and a code-review value check both
    appear under their own role, each labelled with the phase it consulted.
    """
    if not value:
        # Omitted when the mechanism was not consulted (opt-in disabled / no
        # profiles), mirroring how completion_check is only added when present.
        return
    depri = value.get("deprioritized") or []
    detail = (
        f"phase={value.get('phase', '?')} threshold={value.get('uniqueness_threshold')} "
        f"band={value.get('complexity')}"
    )
    if depri:
        detail += f" — deprioritized: {', '.join(depri)}"
    _render_mechanism(
        f"value ({value.get('mechanism', 'reviewer_value')})",
        _mechanism_state(checked=True, fired=bool(value.get("fired"))),
        detail,
        lines,
    )
    # The registered inverse (ADR-0006 clause 7): always present when the value
    # mechanism was consulted, so the return path is never a silent gap.
    recovery = value.get("recovery_check") or {}
    if recovery:
        _render_mechanism(
            f"value recovery ({recovery.get('mechanism', '?')})",
            _mechanism_state(checked=True, fired=bool(recovery.get("fired"))),
            str(recovery.get("reason", "")),
            lines,
        )
    for name, sig in (value.get("signals") or {}).items():
        if not isinstance(sig, dict):
            continue
        sel = "✓" if sig.get("selected") else "✗"
        runs = sig.get("runs")
        tainted = sig.get("tainted_runs")
        uniq = sig.get("uniqueness_rate") or {}
        latency = sig.get("latency_per_p1") or {}
        lines.append(
            f"        {sel} {name}: runs={runs if runs is not None else '?'}"
            f" tainted={tainted if tainted is not None else '?'}"
        )
        lines.append(f"            uniqueness: {_fmt_value_signal(uniq)}")
        lines.append(f"            latency/P1: {_fmt_value_signal(latency)}")


def _render_final(role_block: dict, lines: list[str]) -> None:
    final = role_block.get("final") or {}
    if "models" in final:
        models = final.get("models") or []
        selected = ", ".join(models) if models else "(none)"
    else:
        model = final.get("model")
        tier = final.get("tier")
        selected = f"{model} [{tier}]" if tier else str(model)
    lines.append(f"    selected: {selected}")
    rationale = (final.get("rationale") or "").strip()
    if rationale:
        lines.append(f"    rationale: {rationale}")
    cost = role_block.get("cost_tiebreak") or {}
    if isinstance(cost, dict) and cost:
        cohort = cost.get("cohort") or {}
        role = cohort.get("role", "?")
        complexity = cohort.get("complexity", "?")
        effort = cohort.get("reasoning_effort", "?")
        lines.append(
            "    cost tiebreak: "
            f"{cost.get('source')} value={cost.get('value')} "
            f"observations={cost.get('observations')} "
            f"cohort={role}/{complexity}/{effort}"
        )


def _render_reasoning_effort(reasoning: object, lines: list[str]) -> None:
    """Render the phase-scoped reasoning-effort axis (#1108).

    Tolerant of blocks written before this axis became score-controlled (no
    ``phases`` key): those render their recorded reason and stop.
    """
    if not isinstance(reasoning, dict) or not reasoning:
        return
    phases = reasoning.get("phases")
    if not isinstance(phases, dict) or not phases:
        reason = reasoning.get("reason", "")
        state = "applied" if reasoning.get("applied") else "not applied"
        lines.append(f"reasoning_effort: {state} — {reason}".rstrip(" —"))
        return
    lines.append(f"reasoning_effort (score={reasoning.get('score')}):")
    for phase, phase_block in phases.items():
        if not isinstance(phase_block, dict):
            continue
        if not phase_block.get("applied") and not phase_block.get("models"):
            lines.append(f"  {phase}: not applied — {phase_block.get('reason', '')}".rstrip(" —"))
            continue
        # ``varies_by_provider`` means the seated models resolved through
        # different effective tables (per-provider bucket overrides), so the
        # phase line is a summary and the per-model bands below are authoritative.
        varies = bool(phase_block.get("varies_by_provider"))
        lines.append(
            f"  {phase}: bucket={phase_block.get('bucket')} "
            f"range={phase_block.get('range')} "
            f"thresholds={phase_block.get('thresholds')} "
            f"output={phase_block.get('output')} "
            f"support={phase_block.get('provider_support')}"
            + (" (varies by provider — see per-model bands)" if varies else "")
        )
        for model in phase_block.get("models") or []:
            if not isinstance(model, dict):
                continue
            mark = "✓" if model.get("applied") else "✗"
            detail = (
                f"{model.get('field')}={model.get('value')}"
                if model.get("field") is not None
                else str(model.get("reason", ""))
            )
            bands = (
                f" [bucket={model.get('bucket')} range={model.get('range')} "
                f"thresholds={model.get('thresholds')}]"
                if varies
                else ""
            )
            lines.append(
                f"    {mark} {model.get('model')} [{model.get('transport')}]: "
                f"{model.get('provider_support')} {detail}{bands}".rstrip()
            )


def render_routing_decision(block: dict) -> list[str]:
    """Render the routing_decision block to a list of output lines.

    Pure formatting over the recorded block — no I/O, no lookups.
    """
    lines: list[str] = []
    origin = block.get("origin", "?")
    lines.append(f"Routing decision (origin: {origin})")
    lines.append("=" * 60)
    # Excluded-for-taint (ADR-0006 clause 4/7, #1852): how much router-consumed
    # history was set aside because it failed its own trust checks. Only shown
    # when present and non-zero so legacy blocks (no field) render unchanged.
    excluded_for_taint = block.get("excluded_for_taint")
    if isinstance(excluded_for_taint, int) and excluded_for_taint > 0:
        lines.append(f"excluded for taint: {excluded_for_taint} run(s) set aside (not deleted)")
    # reasoning_effort is a phase-scoped score axis, not a role-scoped one
    # (#1108): recorded top-level with one block per phase. Rendered straight
    # from routing_decision — this feature adds no separate rationale surface.
    _render_reasoning_effort(block.get("reasoning_effort"), lines)
    for role in _ROLE_ORDER:
        role_block = block.get(role)
        if not isinstance(role_block, dict):
            continue
        lines.append("")
        header = _ROLE_LABELS.get(role, role.upper())
        if role == "dev":
            score = role_block.get("score")
            base = role_block.get("base_tier_from_score")
            extra = []
            if score is not None:
                extra.append(f"score={score}")
            if base:
                extra.append(f"base tier={base}")
            header += f"  ({', '.join(extra)})" if extra else ""
        lines.append(header)
        lines.append("-" * 60)

        _render_final(role_block, lines)

        _render_score_policy(role_block, lines)

        lines.append("  candidate pool:")
        _render_candidate_pool(role_block.get("candidate_pool") or [], lines)

        exploration = role_block.get("exploration") or {}
        lines.append(f"  exploration: {exploration.get('mode', '?')}")

        if role == "dev":
            lines.append("  adaptive mechanisms:")
            _render_dev_mechanisms(role_block, lines)
        elif role == "planner" and role_block.get("plan_model_escalation"):
            lines.append("  adaptive mechanisms:")
            _render_planner_mechanisms(role_block, lines)
        elif role in ("plan_review", "code_review"):
            lines.append("  adaptive mechanisms:")
            _render_reviewer_mechanisms(role_block, lines)

    lines.append("")
    lines.append("=" * 60)
    lines.append(
        "Legend: ✓ included/selected  ✗ excluded   ● fired  ◐ checked, did not fire  ○ not checked"
    )
    return lines


def render_configuration(block: object, *, absent_reason: str | None = None) -> list[str]:
    """Render the record's configuration-provenance block (#2056).

    Pure formatting over fields the audit layer already wrote. An absent block
    (a record predating configuration identity, backfilled to ``None`` on read)
    is stated as such rather than rendered as "unchanged" — a run whose
    configuration cannot be named must not look like one that can.

    ``absent_reason`` replaces that message when the *store* the record came
    from does not carry configuration provenance at all (the resume record).
    "This store does not hold it" and "this run never recorded it" are different
    facts, and neither may be printed as the other.
    """
    lines = ["Configuration", "-" * 60]
    if not isinstance(block, dict):
        lines.append(
            f"  {absent_reason}"
            if absent_reason
            else (
                "  not recorded — this run predates configuration provenance, so the "
                "configuration it executed under cannot be established from the audit trail"
            )
        )
        lines.append("")
        return lines
    lines.append(f"  resolved digest: {block.get('resolved_sha256') or '(unknown)'}")
    lines.append(f"  source: {block.get('source_path') or '(unknown)'}")
    lines.append(f"  source digest: {block.get('source_sha256') or '(unknown)'}")
    changed = block.get("changed_during_run")
    if changed is True:
        lines.append(
            "  ⚠ changed during run: yes — the source file differed at finish "
            f"({block.get('source_sha256_at_finish') or '(unknown)'}); this run did not "
            "execute under a single configuration"
        )
    elif changed is False:
        lines.append("  changed during run: no")
    else:
        detail = block.get("finish_read_error")
        suffix = f" ({detail})" if detail else ""
        lines.append(f"  changed during run: undetermined{suffix}")
    lines.append("")
    return lines


def _print_block(block: dict) -> int:
    for line in render_routing_decision(block):
        print(line)
    return 0


def _format_config_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def _config_lookup_label(record: dict, fallback: str) -> str:
    run_id = record.get("run_id")
    return str(run_id) if isinstance(run_id, str) and run_id else fallback


def _print_recorded_config_value(record: dict, *, key: str, fallback_label: str) -> int:
    lookup = audit_substrate.lookup_recorded_configuration_value(record, key)
    run_label = _config_lookup_label(record, fallback_label)
    forge_version = lookup.get("forge_version") or record.get("forge_version") or "unknown"
    prefix = f"{run_label}  forge v{forge_version}"
    status = lookup.get("status")
    if status == "absent":
        print(f"{prefix}  no configuration record for this run (predates capture)")
        return 0
    if status == "missing":
        print(f"{prefix}  {key} was not recorded in this run's resolved configuration")
        return 1
    if status == "uninterpreted":
        print(
            f"{prefix}  {key} = {_format_config_value(lookup.get('value'))}  "
            f"(source: {lookup.get('source') or '?'}, uninterpreted by this forge version)"
        )
        return 0
    print(
        f"{prefix}  {key} = {_format_config_value(lookup.get('value'))}  "
        f"(source: {lookup.get('source') or '?'})"
    )
    return 0


def _explain_from_record(
    record: dict,
    label: str,
    *,
    configuration_absent_reason: str | None = None,
    routing_absent_reason: str | None = None,
) -> int:
    """Render the routing_decision from a loaded audit record.

    Distinguishes an absent block (pre-#1391 records, where the reader-side
    migration backfills ``routing_decision: None``) from a present one so the
    operator is never shown a misleading empty summary.

    ``configuration_absent_reason`` / ``routing_absent_reason`` let a caller
    reading an unfinished record (#2923) say why a block is missing from *that*
    record instead of asserting the finished-record reason, which would be
    false: a story stopped before the router ran has no routing block for a
    reason that has nothing to do with the record's age.
    """
    # Configuration identity first: it says what this run was a run *of*, and it
    # is worth printing even when the routing rationale below is unavailable.
    print(f"# {label}")
    for line in render_configuration(
        record.get("configuration"), absent_reason=configuration_absent_reason
    ):
        print(line)

    if "routing_decision" not in record or record.get("routing_decision") is None:
        print(
            f"[forge] {label}: {routing_absent_reason}"
            if routing_absent_reason
            else (
                f"[forge] {label}: no routing_decision block recorded for this run "
                "(it predates the #1391 routing-decision record, so the assignment "
                "rationale cannot be reconstructed)."
            ),
            file=sys.stderr,
        )
        return 1
    block = record["routing_decision"]
    if not isinstance(block, dict):
        print(
            f"[forge] {label}: routing_decision block is malformed ({type(block).__name__}).",
            file=sys.stderr,
        )
        return 1
    return _print_block(block)


def cmd_explain(args: object) -> int:
    """Render the routing_decision block for a story, run, or per-run file."""
    file_arg = getattr(args, "file", None)
    config_key = getattr(args, "config_key", None)
    if file_arg:
        path = Path(file_arg).resolve()
        if not path.exists():
            print(f"[forge] audit file not found: {path}", file=sys.stderr)
            return 1
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[forge] could not read audit file {path}: {exc}", file=sys.stderr)
            return 1
        if not isinstance(record, dict):
            print(f"[forge] audit file {path} is not a JSON object.", file=sys.stderr)
            return 1
        version = record.get("schema_version")
        if isinstance(version, int):
            record = audit_substrate._migrate_record(record, from_version=version)
        if config_key:
            return _print_recorded_config_value(record, key=config_key, fallback_label=path.name)
        return _explain_from_record(record, path.name)

    config_arg = getattr(args, "config", None)
    config_path = _find_config(Path(config_arg).resolve() if config_arg else None)
    if config_path is None:
        print(
            "[forge] forge.yaml not found. Run from a forge project root.",
            file=sys.stderr,
        )
        return 1
    project_root = config_path.parent

    run_id = getattr(args, "run", None)
    story = getattr(args, "story", None)
    slug: str | None = None
    issue_id: int | None = None
    if run_id:
        label = f"run {run_id}"
    else:
        slug, issue_id = _resolve_story(story)
        label = f"story {story}"

    record: dict | None = None
    substrate_error: str | None = None
    substrate_has_inputs = audit_substrate.has_audit_inputs(project_root)
    if substrate_has_inputs:
        # Strictly read-only: open_readonly never creates, migrates, or rebuilds
        # the substrate (unlike require_substrate), so `forge explain` cannot
        # mutate the index as a side effect. A missing/stale index is reported
        # rather than silently regenerated.
        try:
            conn = audit_substrate.open_readonly(project_root)
        except audit_substrate.SubstrateError as exc:
            substrate_error = str(exc)
        else:
            try:
                if run_id:
                    record = audit_substrate.latest_record_for(conn, run_id=run_id)
                else:
                    record = audit_substrate.latest_record_for(conn, slug=slug, issue_id=issue_id)
            finally:
                conn.close()

    if record is not None:
        if config_key:
            return _print_recorded_config_value(record, key=config_key, fallback_label=label)
        return _explain_from_record(record, label)

    # No finished run is indexed. The decision may still be recorded on disk by
    # a story that is running now, or one `forge stop` killed mid-flight (#2923)
    # — those never reach the substrate, and they are the states in which this
    # question is asked most often.
    lookup = explain_live.find_live_record(
        project_root, run_id=run_id, slug=slug, issue_id=issue_id
    )
    if lookup.found is not None:
        return _explain_from_live_record(
            lookup.found, label, config_key=config_key, substrate_error=substrate_error
        )

    if substrate_error:
        print(f"[forge] {substrate_error}", file=sys.stderr)
        return 1
    return _report_no_record(
        label, lookup, substrate_has_inputs=substrate_has_inputs, project_root=project_root
    )


def _explain_from_live_record(
    found: explain_live.LiveRecord,
    label: str,
    *,
    config_key: str | None,
    substrate_error: str | None,
) -> int:
    """Render an unfinished story's record, stating which store it came from.

    The provenance line is not decoration: an operator reading this output must
    know they are looking at a record that may still change, and which of the
    two non-substrate stores answered.
    """
    if substrate_error:
        print(
            f"[forge] the audit substrate could not be read ({substrate_error}); "
            "reading the on-disk record for this story instead.",
            file=sys.stderr,
        )
    print(
        f"[forge] {label}: no completed run is recorded in the audit substrate; "
        f"explaining the {found.label} at {found.path} — this story has not been "
        "published as finished, so its record may still change.",
        file=sys.stderr,
    )
    record = explain_live.migrate_if_versioned(found.record)
    if config_key:
        return _print_recorded_config_value(record, key=config_key, fallback_label=label)
    configuration_absent_reason = (
        None
        if found.carries_configuration
        else (
            "not carried by this store — the resume record holds phase blocks only; "
            "configuration provenance is written with the run's audit record"
        )
    )
    return _explain_from_record(
        record,
        f"{label}  ({found.label}: {found.path})",
        configuration_absent_reason=configuration_absent_reason,
        routing_absent_reason=(
            f"the {found.label} at {found.path} carries no routing_decision block yet — "
            "the run had not recorded a routing decision when this record was written, "
            "so there is nothing to explain rather than something unreadable."
        ),
    )


def _report_no_record(
    label: str,
    lookup: explain_live.LiveLookup,
    *,
    substrate_has_inputs: bool,
    project_root: Path,
) -> int:
    """Say which of the "not found" answers this is (#2923).

    An operator directed here by the documentation must be able to tell "nothing
    ever recorded a decision for this story" from "a record exists and this
    command could not read it". They are different problems with different next
    steps, and one message for both hides that.

    The claim that a record exists is only made when the unreadable file's path
    names the story asked about. A ``--run`` lookup has no such signal, so a
    corrupt audit belonging to another story is reported as exactly what it is —
    a file that was skipped — rather than as the requested run's record.
    """
    if lookup.unreadable:
        for path, error in lookup.unreadable:
            print(f"[forge] could not read {path}: {error}", file=sys.stderr)
        print(
            f"[forge] a routing record for {label} exists on disk but could not be read "
            "(see above) — this is not the same as no record having been written.",
            file=sys.stderr,
        )
        return 1
    for path, error in lookup.unattributed:
        print(
            f"[forge] skipped {path}: {error} — this file names no story or run that "
            "could be checked against the one you asked for.",
            file=sys.stderr,
        )
    # With unattributed files skipped, "nothing recorded it" would be a claim the
    # search cannot support: one of those files may be the record asked for.
    closing = (
        f"{len(lookup.unattributed)} unreadable file(s) were skipped (above), so a "
        "record for it may exist among them."
        if lookup.unattributed
        else "Nothing has recorded a routing decision for it."
    )
    if not substrate_has_inputs:
        print(
            "[forge] no audit records found — nothing to explain. Searched the audit "
            f"substrate under {project_root / '.forge' / 'audits'} and the in-flight "
            f"stores: {', '.join(lookup.searched)}. {closing}",
            file=sys.stderr,
        )
        return 1
    print(
        f"[forge] no audit record found for {label} — no completed run in the audit "
        "substrate, and no in-flight, interrupted or resume record either "
        f"({', '.join(lookup.searched)}). {closing}",
        file=sys.stderr,
    )
    return 1


def _resolve_story(story: str) -> tuple[str | None, int | None]:
    """Map a --story argument to a (slug, issue_id) lookup pair.

    A bare or ``#``-prefixed integer is treated as a GitHub issue number; any
    other string is treated as a slug (e.g. ``issue-270``).
    """
    stripped = story.lstrip("#").strip()
    if stripped.isdigit():
        return None, int(stripped)
    return story, None


def register_parser(subparsers: object) -> None:
    """Register the 'explain' subcommand parser."""
    parser = subparsers.add_parser(
        "explain",
        help="Render the routing_decision (assignment rationale) for a story or run",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--story",
        help="Story identifier: GitHub issue number (e.g. 270) or slug (e.g. issue-270)",
    )
    target.add_argument(
        "--run",
        help="Exact run id to explain",
    )
    target.add_argument(
        "--file",
        help="Path to a per-run audit JSON file (.forge/audits/runs/<run_id>.json)",
    )
    parser.add_argument(
        "--config",
        help="Path to forge.yaml (default: auto-detect)",
    )
    parser.add_argument(
        "--config-key",
        help=(
            "Recorded resolved-config key to read from the run record "
            "(e.g. knowledge.prior_run_context)"
        ),
    )
