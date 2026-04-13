"""forge eval-preflight subcommand — evaluate candidate preflight models."""

from __future__ import annotations

import sys
from pathlib import Path

from theforge.cli.shared import _find_config


def _infer_profile(
    model: str,
    budget_usd: float,
    timeout_seconds: int,
) -> object:
    """Build a minimal ModelProfile for a candidate model.

    Transport inference rules (from ModelProfile.mode: "api" if provider else "cli"):
      - "claude-*"      → CLI runner, cli="claude"
      - "gpt-*"         → API runner, provider="openai"
      - "deepseek-*"    → API runner, provider="deepseek"
      - "gemini-*"      → API runner, provider="google"
      - other           → CLI runner, cli=model (best-effort)

    The preflight phase uses allowed_tools for file exploration; use a broad
    default set that matches the existing preflight_profile convention.
    """
    from theforge.config.types import ModelProfile

    DEFAULT_TOOLS = (
        "Read",
        "Glob",
        "Grep",
        "Bash",
        "WebSearch",
        "WebFetch",
    )

    if model.startswith("claude-"):
        return ModelProfile(
            name=f"eval-{model}",
            model=model,
            budget_usd=budget_usd,
            timeout_seconds=timeout_seconds,
            allowed_tools=DEFAULT_TOOLS,
            cli="claude",
            phase="preflight",
        )
    elif model.startswith("gpt-"):
        return ModelProfile(
            name=f"eval-{model}",
            model=model,
            budget_usd=budget_usd,
            timeout_seconds=timeout_seconds,
            allowed_tools=DEFAULT_TOOLS,
            provider="openai",
            phase="preflight",
        )
    elif model.startswith("deepseek-"):
        return ModelProfile(
            name=f"eval-{model}",
            model=model,
            budget_usd=budget_usd,
            timeout_seconds=timeout_seconds,
            allowed_tools=DEFAULT_TOOLS,
            provider="deepseek",
            phase="preflight",
        )
    elif model.startswith("gemini-"):
        return ModelProfile(
            name=f"eval-{model}",
            model=model,
            budget_usd=budget_usd,
            timeout_seconds=timeout_seconds,
            allowed_tools=DEFAULT_TOOLS,
            provider="google",
            phase="preflight",
        )
    else:
        # Best-effort: treat as CLI with model name as cli identifier
        return ModelProfile(
            name=f"eval-{model}",
            model=model,
            budget_usd=budget_usd,
            timeout_seconds=timeout_seconds,
            allowed_tools=DEFAULT_TOOLS,
            cli=model,
            phase="preflight",
        )


def cmd_eval_preflight(args: object) -> int:
    """Run the preflight model evaluation harness."""
    from theforge.config import load_config
    from theforge.eval.golden_set import load_golden_set
    from theforge.eval.preflight_harness import run_harness
    from theforge.eval.preflight_report import generate_report, generate_report_json

    # ── Config ──────────────────────────────────────────────────────────────
    config_path_str = getattr(args, "config", None)
    if config_path_str:
        config_path = Path(config_path_str)
    else:
        config_path = _find_config()
    if config_path is None:
        print("[eval-preflight] No forge.yaml found", file=sys.stderr)
        return 1

    config = load_config(config_path)

    # ── Golden set ───────────────────────────────────────────────────────────
    golden_set_path_str = getattr(args, "golden_set", None)
    if golden_set_path_str:
        golden_set_path = Path(golden_set_path_str)
    else:
        golden_set_path = config_path.parent / "tests" / "eval" / "golden_stories.yaml"

    if not golden_set_path.exists():
        print(
            f"[eval-preflight] Golden set not found: {golden_set_path}",
            file=sys.stderr,
        )
        return 1

    import warnings

    with warnings.catch_warnings(record=True) as caught_warnings:
        warnings.simplefilter("always")
        golden = load_golden_set(golden_set_path)

    if caught_warnings:
        for w in caught_warnings:
            print(f"[eval-preflight] warning: {w.message}", file=sys.stderr)

    print(f"[eval-preflight] Loaded {len(golden)} golden stories from {golden_set_path}")

    # ── Model profiles ───────────────────────────────────────────────────────
    # Inherit budget and timeout from the existing preflight_profile as defaults.
    default_budget = config.preflight_profile.budget_usd
    default_timeout = config.preflight_profile.timeout_seconds

    models_arg = getattr(args, "models", None)
    if models_arg:
        model_names = [m.strip() for m in models_arg.split(",") if m.strip()]
    else:
        # Default candidate shortlist per spec AC: GPT-5.4, Claude Sonnet, DeepSeek-reasoner
        model_names = ["gpt-5.4", "claude-sonnet-4-5", "deepseek-reasoner"]

    profiles = [_infer_profile(m, default_budget, default_timeout) for m in model_names]
    print(f"[eval-preflight] Evaluating {len(profiles)} model(s): {', '.join(model_names)}")

    # ── Working directory ────────────────────────────────────────────────────
    working_dir_str = getattr(args, "working_dir", None)
    if working_dir_str:
        working_dir = Path(working_dir_str)
    else:
        working_dir = config.project_root

    # ── Run harness ──────────────────────────────────────────────────────────
    print(
        f"[eval-preflight] Running {len(golden) * len(profiles)} total evaluations "
        f"({len(golden)} stories × {len(profiles)} models)...\n"
    )
    all_metrics = run_harness(golden, profiles, working_dir, config)

    # ── Report ───────────────────────────────────────────────────────────────
    output_format = getattr(args, "output_format", "text")
    if output_format == "json":
        print(generate_report_json(all_metrics))
    else:
        print(generate_report(all_metrics))

    return 0


def register_parser(subparsers: object) -> None:
    """Register the 'eval-preflight' subcommand parser."""
    p = subparsers.add_parser(
        "eval-preflight",
        help="Evaluate candidate preflight models against a golden story set",
    )
    p.add_argument(
        "--golden-set",
        dest="golden_set",
        metavar="PATH",
        help="Path to golden_stories.yaml (default: tests/eval/golden_stories.yaml)",
    )
    p.add_argument(
        "--models",
        metavar="MODEL[,MODEL...]",
        help=(
            "Comma-separated model identifiers to evaluate. "
            "Default: gpt-5.4,claude-sonnet-4-5,deepseek-reasoner"
        ),
    )
    p.add_argument(
        "--working-dir",
        dest="working_dir",
        metavar="PATH",
        help="Working directory for agent invocations (default: project_root from forge.yaml)",
    )
    p.add_argument(
        "--output-format",
        dest="output_format",
        choices=["text", "json"],
        default="text",
        help="Report format: 'text' (markdown table) or 'json' (default: text)",
    )
    p.add_argument(
        "--config",
        metavar="PATH",
        help="Path to forge.yaml (default: auto-detect)",
    )
