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
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

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
    """Render the plan-reviewer value_check (#1443): P1-uniqueness + wall-clock cost.

    Surfaces, per consulted reviewer, the uniqueness rate and latency-per-P1 so an
    operator can answer "is this reviewer earning its wall-clock cost?" directly
    from the explain view. Absent block → mechanism not checked (opt-in/disabled).
    """
    if not value:
        # Omitted when the mechanism was not consulted (opt-in disabled / no
        # profiles), mirroring how completion_check is only added when present.
        return
    depri = value.get("deprioritized") or []
    detail = f"threshold={value.get('uniqueness_threshold')} band={value.get('complexity')}"
    if depri:
        detail += f" — deprioritized: {', '.join(depri)}"
    _render_mechanism(
        f"value ({value.get('mechanism', 'reviewer_value')})",
        _mechanism_state(checked=True, fired=bool(value.get("fired"))),
        detail,
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
    # reasoning_effort is a score axis not tied to a role (#1019): recorded
    # top-level as intentionally NOT score-controlled. Surface it so the operator
    # sees the axis was considered and deliberately excluded, not overlooked.
    reasoning = block.get("reasoning_effort")
    if isinstance(reasoning, dict) and reasoning:
        lines.append(
            f"reasoning_effort: not score-controlled — {reasoning.get('reason', '')} "
            f"({reasoning.get('rationale', '')})".rstrip()
        )
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


def _print_block(block: dict, label: str) -> int:
    print(f"# {label}")
    for line in render_routing_decision(block):
        print(line)
    return 0


def _explain_from_record(record: dict, label: str) -> int:
    """Render the routing_decision from a loaded audit record.

    Distinguishes an absent block (pre-#1391 records, where the reader-side
    migration backfills ``routing_decision: None``) from a present one so the
    operator is never shown a misleading empty summary.
    """
    if "routing_decision" not in record or record.get("routing_decision") is None:
        print(
            f"[forge] {label}: no routing_decision block recorded for this run "
            "(it predates the #1391 routing-decision record, so the assignment "
            "rationale cannot be reconstructed).",
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
    return _print_block(block, label)


def cmd_explain(args: object) -> int:
    """Render the routing_decision block for a story, run, or per-run file."""
    file_arg = getattr(args, "file", None)
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

    if not audit_substrate.has_audit_inputs(project_root):
        print(
            "[forge] no audit records found — nothing to explain.",
            file=sys.stderr,
        )
        return 1
    # Strictly read-only: open_readonly never creates, migrates, or rebuilds the
    # substrate (unlike require_substrate), so `forge explain` cannot mutate the
    # index as a side effect. A missing/stale index fails with an explicit
    # rebuild instruction rather than being silently regenerated.
    try:
        conn = audit_substrate.open_readonly(project_root)
    except audit_substrate.SubstrateError as exc:
        print(f"[forge] {exc}", file=sys.stderr)
        return 1

    try:
        run_id = getattr(args, "run", None)
        story = getattr(args, "story", None)
        if run_id:
            record = audit_substrate.latest_record_for(conn, run_id=run_id)
            label = f"run {run_id}"
        else:
            slug, issue_id = _resolve_story(story)
            record = audit_substrate.latest_record_for(conn, slug=slug, issue_id=issue_id)
            label = f"story {story}"
    finally:
        conn.close()

    if record is None:
        print(f"[forge] no audit record found for {label}.", file=sys.stderr)
        return 1
    return _explain_from_record(record, label)


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
