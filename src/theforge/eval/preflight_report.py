"""Scoring aggregation and comparison report for preflight model evaluation."""

from __future__ import annotations

import json
from dataclasses import dataclass

from theforge.eval.preflight_harness import RunMetrics


@dataclass
class ModelScores:
    """Aggregate scores for a single model across all golden stories."""

    model_name: str
    n_stories: int

    # Primary error rates (0.0–1.0)
    timeout_rate: float  # submitted=False
    invalid_yaml_rate: float  # valid_yaml=False (among submitted)
    false_already_done_rate: float  # verdict=ALREADY_DONE when expected=PROCEED
    false_blocked_rate: float  # verdict=BLOCKED when expected=PROCEED

    # Derived composite (sum of the three primary error rates above)
    composite_error_rate: float

    # Quality metrics
    complexity_agreement_rate: float
    rationale_cites_code_rate: float

    # Performance (None when no data available for that metric)
    avg_tool_iterations: float | None
    avg_duration_s: float | None
    avg_cost_usd: float | None


def score_model(metrics: list[RunMetrics]) -> ModelScores:
    """Compute aggregate scores for a single model from its RunMetrics list."""
    if not metrics:
        raise ValueError("Cannot score an empty metrics list")

    n = len(metrics)
    model_name = metrics[0].model_name

    # Timeout / non-submission rate
    n_timeout = sum(1 for m in metrics if not m.submitted)
    timeout_rate = n_timeout / n

    submitted_metrics = [m for m in metrics if m.submitted]

    # Invalid YAML rate (denominator: submitted runs only; if none submitted, rate is 0)
    if submitted_metrics:
        n_invalid = sum(1 for m in submitted_metrics if not m.valid_yaml)
        invalid_yaml_rate = n_invalid / len(submitted_metrics)
    else:
        invalid_yaml_rate = 0.0

    # False ALREADY_DONE: verdict=ALREADY_DONE when expected=PROCEED
    n_proceed_expected = sum(1 for m in metrics if m.expected_verdict == "PROCEED")
    if n_proceed_expected:
        n_false_ad = sum(
            1 for m in metrics if m.expected_verdict == "PROCEED" and m.verdict == "ALREADY_DONE"
        )
        false_already_done_rate = n_false_ad / n_proceed_expected
    else:
        false_already_done_rate = 0.0

    # False BLOCKED: verdict=BLOCKED when expected=PROCEED
    if n_proceed_expected:
        n_false_blocked = sum(
            1 for m in metrics if m.expected_verdict == "PROCEED" and m.verdict == "BLOCKED"
        )
        false_blocked_rate = n_false_blocked / n_proceed_expected
    else:
        false_blocked_rate = 0.0

    composite_error_rate = timeout_rate + false_already_done_rate + false_blocked_rate

    # Complexity agreement: among runs where both complexity and expected_complexity
    # are known, what fraction agree?
    complexity_pairs = [
        (m.complexity, m.expected_complexity)
        for m in metrics
        if m.complexity is not None and m.expected_complexity is not None
    ]
    if complexity_pairs:
        n_agree = sum(1 for got, want in complexity_pairs if got == want)
        complexity_agreement_rate = n_agree / len(complexity_pairs)
    else:
        complexity_agreement_rate = 0.0

    # Rationale cites code rate
    rationale_cites_code_rate = sum(1 for m in metrics if m.rationale_cites_code) / n

    # Performance averages (exclude None values)
    iter_values = [m.tool_iterations for m in metrics if m.tool_iterations is not None]
    avg_tool_iterations: float | None = (
        sum(iter_values) / len(iter_values) if iter_values else None
    )

    dur_values = [m.duration_s for m in metrics if m.duration_s is not None]
    avg_duration_s: float | None = sum(dur_values) / len(dur_values) if dur_values else None

    cost_values = [m.cost_usd for m in metrics if m.cost_usd is not None]
    avg_cost_usd: float | None = sum(cost_values) / len(cost_values) if cost_values else None

    return ModelScores(
        model_name=model_name,
        n_stories=n,
        timeout_rate=timeout_rate,
        invalid_yaml_rate=invalid_yaml_rate,
        false_already_done_rate=false_already_done_rate,
        false_blocked_rate=false_blocked_rate,
        composite_error_rate=composite_error_rate,
        complexity_agreement_rate=complexity_agreement_rate,
        rationale_cites_code_rate=rationale_cites_code_rate,
        avg_tool_iterations=avg_tool_iterations,
        avg_duration_s=avg_duration_s,
        avg_cost_usd=avg_cost_usd,
    )


def _pct(rate: float) -> str:
    return f"{rate * 100:.1f}%"


def _fmt_opt(value: float | None, fmt: str = ".2f") -> str:
    if value is None:
        return "—"
    return format(value, fmt)


def generate_report(all_metrics: list[RunMetrics]) -> str:
    """Generate a markdown comparison report from all run metrics.

    Groups by model_name, scores each group, then produces:
    1. A summary table with one row per model.
    2. A per-story breakdown table showing each story's verdict per model.
    """
    if not all_metrics:
        return "# Preflight Evaluation Report\n\n*No metrics collected.*\n"

    # Group by model
    model_order: list[str] = []
    by_model: dict[str, list[RunMetrics]] = {}
    for m in all_metrics:
        if m.model_name not in by_model:
            by_model[m.model_name] = []
            model_order.append(m.model_name)
        by_model[m.model_name].append(m)

    scores: dict[str, ModelScores] = {
        name: score_model(metrics) for name, metrics in by_model.items()
    }

    lines: list[str] = []
    lines.append("# Preflight Evaluation Report\n")

    # Note about approximate metrics
    lines.append(
        "> **Note:** `avg_tool_iterations` is populated only for CLI runners that "
        "expose `num_turns` in JSON output (Claude CLI). Dashes (—) indicate the "
        "metric is unavailable for that backend.\n"
    )
    lines.append(
        "> **Note:** `cites_code` is an approximate heuristic based on filename "
        "pattern matching. Treat it as informational, not authoritative.\n"
    )

    # ── Summary table ────────────────────────────────────────────────────────
    lines.append("## Model Comparison Summary\n")
    header = (
        "| Model | n | timeout% | invalid_yaml% | false_AD% | false_BLK% "
        "| **composite_err%** | complexity_agree% | cites_code% "
        "| avg_iters | avg_dur_s | avg_cost_usd |"
    )
    separator = (
        "|-------|---|----------|---------------|-----------|------------|"
        "--------------------|-------------------|-------------|"
        "-----------|-----------|--------------|"
    )
    lines.append(header)
    lines.append(separator)

    for name in model_order:
        s = scores[name]
        row = (
            f"| {s.model_name} "
            f"| {s.n_stories} "
            f"| {_pct(s.timeout_rate)} "
            f"| {_pct(s.invalid_yaml_rate)} "
            f"| {_pct(s.false_already_done_rate)} "
            f"| {_pct(s.false_blocked_rate)} "
            f"| **{_pct(s.composite_error_rate)}** "
            f"| {_pct(s.complexity_agreement_rate)} "
            f"| {_pct(s.rationale_cites_code_rate)} "
            f"| {_fmt_opt(s.avg_tool_iterations, '.1f')} "
            f"| {_fmt_opt(s.avg_duration_s, '.1f')} "
            f"| {_fmt_opt(s.avg_cost_usd, '.4f')} "
            f"|"
        )
        lines.append(row)

    lines.append("")

    # ── Per-story breakdown ──────────────────────────────────────────────────
    lines.append("## Per-Story Verdict Breakdown\n")

    # Collect all story IDs in order of first appearance
    story_order: list[str] = []
    seen: set[str] = set()
    for m in all_metrics:
        if m.story_id not in seen:
            story_order.append(m.story_id)
            seen.add(m.story_id)

    # Index: story_id → model_name → RunMetrics
    breakdown: dict[str, dict[str, RunMetrics]] = {sid: {} for sid in story_order}
    for m in all_metrics:
        breakdown[m.story_id][m.model_name] = m

    # Table header
    model_cols = " | ".join(model_order)
    hdr = f"| story_id | expected | {model_cols} |"
    sep_parts = ["-------", "--------"] + ["------"] * len(model_order)
    sep = "| " + " | ".join(sep_parts) + " |"
    lines.append(hdr)
    lines.append(sep)

    for sid in story_order:
        expected = next((m.expected_verdict for m in all_metrics if m.story_id == sid), "?")
        verdict_cells = []
        for name in model_order:
            m = breakdown[sid].get(name)
            if m is None:
                verdict_cells.append("—")
            elif not m.submitted:
                verdict_cells.append("TIMEOUT")
            elif not m.valid_yaml:
                verdict_cells.append("INVALID")
            else:
                match = "✓" if m.verdict == expected else "✗"
                verdict_cells.append(f"{m.verdict} {match}")
        row = f"| {sid} | {expected} | " + " | ".join(verdict_cells) + " |"
        lines.append(row)

    lines.append("")
    return "\n".join(lines)


def generate_report_json(all_metrics: list[RunMetrics]) -> str:
    """Serialize all RunMetrics to a JSON string for machine consumption."""
    data = [
        {
            "story_id": m.story_id,
            "model_name": m.model_name,
            "verdict": m.verdict,
            "expected_verdict": m.expected_verdict,
            "complexity": m.complexity,
            "expected_complexity": m.expected_complexity,
            "cost_usd": m.cost_usd,
            "duration_s": m.duration_s,
            "tool_iterations": m.tool_iterations,
            "submitted": m.submitted,
            "valid_yaml": m.valid_yaml,
            "rationale_cites_code": m.rationale_cites_code,
        }
        for m in all_metrics
    ]
    return json.dumps(data, indent=2)
