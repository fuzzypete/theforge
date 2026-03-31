"""forge check-config subcommand — show effective config and surface problems."""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

from theforge.cli.shared import _find_config
from theforge.config import ForgeConfig, ModelProfile, load_config
from theforge.config.auth import check_agent_auth
from theforge.config.types import PlanConfig


def _transport_label(profile: ModelProfile) -> str:
    """Return 'cli / model' or 'provider / model'."""
    transport = profile.cli if profile.cli is not None else (profile.provider or "?")
    return f"{transport} / {profile.model}"


def _plan_transport_label(plan: PlanConfig) -> str:
    """Return transport label for a PlanConfig."""
    transport = plan.cli if plan.cli is not None else (plan.provider or "?")
    return f"{transport} / {plan.model}"


def _format_config(
    config: ForgeConfig,
    auth_results: dict[str, tuple[bool, str]],
) -> tuple[str, int]:
    """Build the output string and determine exit code.

    Returns (output_text, exit_code) where exit_code is 0 (no warnings) or
    1 (warnings present).
    """
    lines: list[str] = []
    warnings_list: list[str] = []

    # ── Header ────────────────────────────────────────────────────────────
    lines.append(f"Project: {config.project}")
    if config.assignment.enabled:
        lines.append(f"Budget:  ${config.assignment.budget_per_story_usd:.2f}/story")
    lines.append("")

    # ── PHASES ────────────────────────────────────────────────────────────
    lines.append("PHASES")
    for label, profile in [
        ("preflight", config.preflight_profile),
        ("dev", config.dev_profile),
    ]:
        transport = _transport_label(profile)
        lines.append(
            f"  {label:<12}{transport:<30}  timeout={profile.timeout_seconds}s"
            f"  budget=${profile.budget_usd:.2f}"
        )

    # Plan phase — show if enabled, from PlanConfig
    if config.plan.enabled:
        transport = _plan_transport_label(config.plan)
        lines.append(
            f"  {'plan':<12}{transport:<30}  timeout={config.plan.timeout}s"
            f"  budget=${config.plan.budget_usd:.2f}"
        )
    lines.append("")

    # ── REVIEW POOL ───────────────────────────────────────────────────────
    lines.append("REVIEW POOL")
    for profile in config.review_pool:
        transport = _transport_label(profile)
        ready, reason = auth_results.get(profile.name, (True, ""))
        auth_str = "✓ auth" if ready else f"✗ {reason}"
        role_str = f"  role={profile.review_role}" if profile.review_role else ""
        lines.append(
            f"  {profile.name:<22}{transport:<30}{role_str}  budget=${profile.budget_usd:.2f}"
            f"  {auth_str}"
        )
        if not ready:
            warnings_list.append(f"{profile.name}: {reason} — will be skipped at runtime")

    if config.synthesis_profile is not None:
        profile = config.synthesis_profile
        transport = _transport_label(profile)
        ready, reason = auth_results.get(profile.name, (True, ""))
        auth_str = "✓ auth" if ready else f"✗ {reason}"
        lines.append(
            f"  {profile.name:<22}{transport:<30}  (synthesis)  budget=${profile.budget_usd:.2f}"
            f"  {auth_str}"
        )
        if not ready:
            warnings_list.append(f"{profile.name}: {reason} — will be skipped at runtime")
    lines.append("")

    # ── PLAN REVIEWERS ────────────────────────────────────────────────────
    if config.plan_agent_review.enabled:
        lines.append("PLAN REVIEWERS")
        for profile in config.plan_agent_review.profiles:
            transport = _transport_label(profile)
            ready, reason = auth_results.get(profile.name, (True, ""))
            auth_str = "✓ auth" if ready else f"✗ {reason}"
            lines.append(
                f"  {profile.name:<22}{transport:<30}  budget=${profile.budget_usd:.2f}"
                f"  {auth_str}"
            )
            if not ready:
                warnings_list.append(f"{profile.name}: {reason} — will be skipped at runtime")
        lines.append("")

    # ── AGENTS (adaptive pool) ────────────────────────────────────────────
    if config.assignment.enabled and config.agents:
        lines.append("AGENTS (adaptive pool)")
        for agent in config.agents:
            transport = agent.cli if agent.cli is not None else (agent.provider or "?")
            transport_str = f"{transport} / {agent.model}"
            ready, reason = auth_results.get(agent.name, (True, ""))
            auth_str = "✓ auth" if ready else f"✗ {reason}"
            lines.append(f"  {agent.name:<22}{transport_str:<30}  tier={agent.tier:<8}{auth_str}")
            if not ready:
                warnings_list.append(f"{agent.name}: {reason} — will be skipped at runtime")
        lines.append("")

    # ── WARNINGS ─────────────────────────────────────────────────────────
    if warnings_list:
        lines.append("WARNINGS")
        for w in warnings_list:
            lines.append(f"  ⚠ {w}")
        lines.append("")

    # ── SETTINGS ──────────────────────────────────────────────────────────
    lines.append("SETTINGS")
    if config.assignment.enabled:
        a = config.assignment
        lines.append(
            f"  assignment:    enabled  (min={a.min_reviewers}, max={a.max_reviewers},"
            f" budget=${a.budget_per_story_usd:.2f}/story)"
        )
    else:
        lines.append("  assignment:    disabled")

    plan_str = "enabled" if config.plan.enabled else "disabled"
    lines.append(f"  plan:          {plan_str}")

    if config.plan_agent_review.enabled:
        lines.append("  plan_review:   enabled (agent)")
    elif config.plan_review.enabled:
        lines.append("  plan_review:   enabled (human)")
    else:
        lines.append("  plan_review:   disabled")

    lines.append(f"  max_parallel:  {config.sprint.max_parallel}")
    lines.append(f"  on_approve:    {config.workspace.on_approve}")

    exit_code = 1 if warnings_list else 0
    return "\n".join(lines), exit_code


def cmd_check_config(args: object) -> int:
    """Show effective config and surface problems."""
    config_path = getattr(args, "config", None)
    if config_path:
        config_path = Path(config_path)
    else:
        config_path = _find_config()
    if config_path is None:
        print("[check-config] No forge.yaml found", file=sys.stderr)
        return 2

    # Capture deprecation warnings emitted by load_config
    captured_warnings: list[str] = []
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            config = load_config(config_path)
        for w in caught:
            captured_warnings.append(str(w.message))
    except Exception as exc:
        print(f"[check-config] Invalid config: {exc}", file=sys.stderr)
        return 2

    # Collect all profiles to auth-check
    auth_results: dict[str, tuple[bool, str]] = {}

    def _check(profile: ModelProfile) -> None:
        if profile.name in auth_results:
            return
        try:
            auth_results[profile.name] = check_agent_auth(profile, config.secrets)
        except ValueError as exc:
            auth_results[profile.name] = (False, str(exc))

    _check(config.preflight_profile)
    _check(config.dev_profile)
    for p in config.review_pool:
        _check(p)
    if config.synthesis_profile is not None:
        _check(config.synthesis_profile)
    if config.plan_agent_review.enabled:
        for p in config.plan_agent_review.profiles:
            _check(p)

    # Plan phase auth check (P1 from plan review: include planner transport)
    if config.plan.enabled:
        plan_profile = ModelProfile(
            name="plan",
            cli=config.plan.cli,
            provider=config.plan.provider,
            model=config.plan.model,
            budget_usd=config.plan.budget_usd,
            timeout_seconds=config.plan.timeout,
            allowed_tools=(),
        )
        _check(plan_profile)

    # Adaptive pool agents
    for agent in config.agents:
        agent_profile = agent.to_model_profile(allowed_tools=())
        _check(agent_profile)

    output, exit_code = _format_config(config, auth_results)

    # Fold in captured deprecation warnings
    if captured_warnings:
        if exit_code == 0:
            exit_code = 1
        extra = "\n".join(f"  ⚠ {w}" for w in captured_warnings)
        if "WARNINGS" in output:
            output = output.rstrip() + "\n" + extra
        else:
            output = output.rstrip() + "\n\nWARNINGS\n" + extra

    print(output)
    return exit_code


def register_parser(subparsers: object) -> None:
    """Register the 'check-config' subcommand parser."""
    p = subparsers.add_parser(
        "check-config",
        help="Show effective config and surface problems",
    )
    p.add_argument(
        "config",
        nargs="?",
        default=None,
        metavar="forge.yaml",
        help="Path to forge.yaml (default: auto-detect)",
    )
