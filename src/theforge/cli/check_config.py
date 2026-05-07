"""forge check-config subcommand — show effective config and surface problems."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from theforge.cli.hooks import post_run_hook_upgrade_warnings
from theforge.cli.shared import _find_config
from theforge.config import (
    ForgeConfig,
    ModelProfile,
    TransportSpec,
    load_config,
    resolve_agent_spec,
)
from theforge.config.auth import check_agent_auth
from theforge.config.profiles import _apply_provider_fallback
from theforge.config.role_derivation import derive_roles
from theforge.config.types import PlanConfig


def _check_audit_substrate(project_root: Path) -> list[str]:
    """Return warning strings describing substrate health problems, if any.

    A missing substrate when ``.forge/audits/`` does not exist is a fresh
    repo and is OK. A substrate that fails to open or its
    ``PRAGMA integrity_check`` is reported with the recovery command. An
    existing legacy ``history.jsonl`` without a substrate is also a
    warning so operators know to rebuild before runtime readers fire.
    """
    from theforge.coordinator import audit_substrate

    audits_root = audit_substrate.audits_dir(project_root)
    if not audits_root.exists():
        return []
    sub_path = audit_substrate.substrate_path(project_root)
    history_path = audit_substrate.history_jsonl_path(project_root)
    warnings: list[str] = []
    if not sub_path.exists():
        if history_path.exists():
            warnings.append(
                f"audit substrate missing at {sub_path}; legacy "
                f"history.jsonl present. Run "
                f"`forge audits rebuild --include-legacy-history` to backfill."
            )
        return warnings
    try:
        import sqlite3

        conn = sqlite3.connect(str(sub_path))
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            if not row or row[0] != "ok":
                warnings.append(
                    f"audit substrate at {sub_path} failed integrity check: "
                    f"{row[0] if row else 'no result'}. "
                    f"Run `forge audits rebuild [--include-legacy-history]` to recover."
                )
                return warnings
            conn.execute("SELECT count(*) FROM audit_records").fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        warnings.append(
            f"audit substrate at {sub_path} could not be read: {exc}. "
            f"Run `forge audits rebuild [--include-legacy-history]` to recover."
        )
    return warnings


class _CapturingHandler(logging.Handler):
    """Logging handler that captures WARNING+ records into a list."""

    def __init__(self, records: list[str]) -> None:
        super().__init__(level=logging.WARNING)
        self._records = records

    def emit(self, record: logging.LogRecord) -> None:
        self._records.append(record.getMessage())


# Auth results are keyed by "section:name" to avoid collisions between
# profiles in different sections that happen to share a name.
AuthResults = dict[str, tuple[bool, str]]


def _auth_key(section: str, name: str) -> str:
    return f"{section}:{name}"


def _run_auth(
    profile: ModelProfile,
    section: str,
    results: AuthResults,
    secrets: dict[str, str],
) -> None:
    """Check readiness for *profile* and store under section-scoped key."""
    key = _auth_key(section, profile.name)
    try:
        results[key] = check_agent_auth(profile, secrets)
    except ValueError as exc:
        results[key] = (False, str(exc))


_CLI_PROVIDER_MAP: dict[str, str] = {
    "claude": "anthropic",
    "codex": "openai",
    "gemini": "google",
}


def _split_provider_transport(
    cli: str | None,
    provider: str | None,
    transport: TransportSpec | None = None,
) -> tuple[str, str]:
    """Return (provider_label, transport_label) for config display.

    CLI profiles carry an implicit provider (claude → anthropic, codex → openai,
    gemini → google); API profiles carry the provider explicitly. Transport is
    rendered as 'cli:<binary>' for CLI profiles and 'api' for API profiles, so
    the operator never conflates CLI-backed and API-backed models.

    When an explicit TransportSpec is present, it is the same source of truth the
    runner uses for dispatch and wins over legacy cli/provider inference.
    """
    if transport is not None:
        if transport.kind == "api":
            return provider or transport.runner, "api"
        runner = transport.executable or transport.runner
        return provider or _CLI_PROVIDER_MAP.get(runner, runner), f"cli:{runner}"
    if cli is not None:
        return _CLI_PROVIDER_MAP.get(cli, cli), f"cli:{cli}"
    if provider is not None:
        return provider, "api"
    return "?", "?"


def _transport_label(profile: ModelProfile) -> str:
    """Return 'provider  transport  / model' with provider and transport separate."""
    provider_label, transport_label = _split_provider_transport(
        profile.cli,
        profile.provider,
        profile.transport,
    )
    return f"{provider_label:<10}{transport_label:<10}{profile.model}"


def _plan_transport_label(plan: PlanConfig) -> str:
    """Return provider/transport/model label for a PlanConfig."""
    provider_label, transport_label = _split_provider_transport(plan.cli, plan.provider)
    return f"{provider_label:<10}{transport_label:<10}{plan.model}"


def _thinking_budget_label(profile: ModelProfile) -> str:
    """Return display suffix for Gemini thinking budget when explicitly configured."""
    if profile.thinking_budget is None:
        return ""
    return f"  thinking_budget={profile.thinking_budget}"


def _provider_label(
    model_key: str,
    warnings_list: list[str] | None = None,
    registry: dict | None = None,
) -> str:
    """Return a short human-readable label for a model key like 'claude/sonnet'."""
    try:
        spec = resolve_agent_spec(model_key, registry=registry)
    except ValueError as exc:
        if warnings_list is not None:
            warnings_list.append(str(exc))
        return "provider=? transport=?"

    provider_label, transport_label = _split_provider_transport(
        spec.transport.executable if spec.transport.kind == "cli" else None,
        spec.provider,
        spec.transport,
    )
    return f"{provider_label} {transport_label}"


def _ref_transport_label(
    cli: str | None,
    provider: str | None,
    model: str,
    transport: TransportSpec | None = None,
) -> str:
    provider_label, transport_label = _split_provider_transport(cli, provider, transport)
    return f"{provider_label:<10}{transport_label:<10}{model}"


def _format_complexity_aware_section(
    models: list[str],
    budget_usd: float,
    overrides: dict | None,
    registry: dict | None = None,
) -> list[str]:
    """Build the DERIVED ROLES section for v0.8 simple-mode configs.

    Calls derive_roles() at each complexity level and renders the tier×complexity
    routing table so the operator can verify complexity-aware routing at a glance.
    """
    _ov = overrides or {}
    ra_low = derive_roles(models, _ov, budget_usd=budget_usd, complexity="LOW", registry=registry)
    ra_mid = derive_roles(
        models,
        _ov,
        budget_usd=budget_usd,
        complexity="MEDIUM",
        registry=registry,
    )
    ra_high = derive_roles(
        models,
        _ov,
        budget_usd=budget_usd,
        complexity="HIGH",
        registry=registry,
    )

    lines: list[str] = []
    lines.append("DERIVED ROLES (complexity-aware)")
    lines.append("  Role selection is driven by preflight complexity. See #807 for tier mapping.")
    lines.append("")

    # Preflight: always static (runs before complexity is known)
    pre = ra_low.preflight.ref
    pre_label = _ref_transport_label(pre.cli, pre.provider, pre.model, pre.transport)
    lines.append(f"  {'preflight:':<14}{pre_label:<28} (static — runs before complexity known)")

    # Plan: show per-complexity, collapsing equal adjacent levels
    def _plan_label(ra) -> str:  # type: ignore[no-untyped-def]
        r = ra.plan.ref
        return _ref_transport_label(r.cli, r.provider, r.model, r.transport)

    pl, pm, ph = _plan_label(ra_low), _plan_label(ra_mid), _plan_label(ra_high)
    plan_parts = _collapse_complexity_labels({"LOW": pl, "MEDIUM": pm, "HIGH": ph})
    lines.append(f"  {'plan:':<14}{plan_parts}")

    # Dev: show per-complexity
    def _dev_label(ra) -> str:  # type: ignore[no-untyped-def]
        r = ra.dev.ref
        return _ref_transport_label(r.cli, r.provider, r.model, r.transport)

    dl, dm, dh = _dev_label(ra_low), _dev_label(ra_mid), _dev_label(ra_high)
    dev_parts = _collapse_complexity_labels({"LOW": dl, "MEDIUM": dm, "HIGH": dh})
    lines.append(f"  {'dev:':<14}{dev_parts}")

    # Review pool: show pool size + synthesis info per complexity
    def _review_label(ra) -> str:  # type: ignore[no-untyped-def]
        n = len(ra.review_pool)
        synth = " + synthesis" if ra.synthesis is not None else ""
        return f"{n} reviewer{'s' if n != 1 else ''}{synth}"

    rl, rm, rh = _review_label(ra_low), _review_label(ra_mid), _review_label(ra_high)
    review_parts = _collapse_complexity_labels({"LOW": rl, "MEDIUM": rm, "HIGH": rh})
    lines.append(f"  {'code_review:':<14}{review_parts}")

    lines.append("")
    has_overrides = bool(_ov) and any(k != "plan_agent_review" for k in _ov)
    lines.append(f"  Advanced overrides: {'none' if not has_overrides else ', '.join(_ov.keys())}")

    return lines


def _collapse_complexity_labels(mapping: dict[str, str]) -> str:
    """Collapse {'LOW': A, 'MEDIUM': B, 'HIGH': B} → 'A (LOW) / B (MEDIUM, HIGH)'.

    Groups consecutive identical labels under shared complexity annotations.
    """
    order = ["LOW", "MEDIUM", "HIGH"]
    groups: list[tuple[str, list[str]]] = []
    for level in order:
        label = mapping[level]
        if groups and groups[-1][0] == label:
            groups[-1][1].append(level)
        else:
            groups.append((label, [level]))

    if len(groups) == 1:
        return groups[0][0]

    parts = []
    for label, levels in groups:
        parts.append(f"{label:<28} ({', '.join(levels)})")
    return ("\n" + " " * 16).join(parts)


def _format_config(
    config: ForgeConfig,
    auth_results: AuthResults,
) -> tuple[str, int]:
    """Build the output string and determine exit code.

    Returns (output_text, exit_code) where exit_code is 0 (no warnings) or
    1 (warnings present).
    """
    lines: list[str] = []
    warnings_list: list[str] = []

    # ── Header ────────────────────────────────────────────────────────────
    lines.append(f"Project: {config.project}")
    if config.models_budget_usd is not None:
        lines.append("Mode:    simple")
        lines.append(f"Budget:  ${config.models_budget_usd:.2f}/story")
        if config.models:
            seen: set[str] = set()
            provider_labels: list[str] = []
            for mk in config.models:
                lbl = _provider_label(mk, warnings_list, registry=config.model_registry)
                if lbl not in seen:
                    seen.add(lbl)
                    provider_labels.append(lbl)
            lines.append(f"Providers: {', '.join(provider_labels)}")
    elif config.assignment.enabled:
        _cap = config.assignment.max_cost_per_story_usd
        if _cap is None:
            lines.append(
                "Per-story routing cost cap: unset "
                "(adaptive routes by complexity; only budget_usd enforces spend)"
            )
        else:
            lines.append(f"Per-story routing cost cap: ${_cap:.2f}/story")
    lines.append("")

    if config.models or config.custom_models:
        lines.append("MODEL REGISTRY")
        if config.models:
            builtin_models = [
                model_id
                for model_id in config.models
                if config.model_registry_sources.get(model_id, "builtin") == "builtin"
            ]
            forge_yaml_models = [
                model_id
                for model_id in config.models
                if config.model_registry_sources.get(model_id) == "forge.yaml"
            ]
            if builtin_models:
                lines.append(f"  {'selected builtin:':<18}{', '.join(builtin_models)}")
            if forge_yaml_models:
                lines.append(f"  {'selected forge.yaml:':<18}{', '.join(forge_yaml_models)}")
        if config.custom_models:
            custom_details = []
            for model_id in config.custom_models:
                spec = config.model_registry.get(model_id)
                if spec is None:
                    continue
                provider_label, transport_label = _split_provider_transport(
                    spec.transport.executable if spec.transport.kind == "cli" else None,
                    spec.provider,
                    spec.transport,
                )
                custom_details.append(
                    f"{model_id} [{provider_label} {transport_label} -> {spec.model}]"
                )
            if custom_details:
                lines.append(f"  {'declared forge.yaml:':<18}{', '.join(custom_details)}")
        lines.append("")

    # ── DERIVED ROLES (v0.8 simple mode) ─────────────────────────────────
    if config.models and config.models_budget_usd is not None:
        derived_lines = _format_complexity_aware_section(
            config.models,
            config.models_budget_usd,
            overrides=config.models_overrides,
            registry=config.model_registry,
        )
        lines.extend(derived_lines)
        lines.append("")

    # ── PHASES ────────────────────────────────────────────────────────────
    lines.append("PHASES")
    for label, profile in [
        ("preflight", config.preflight_profile),
        ("dev", config.dev_profile),
    ]:
        transport = _transport_label(profile)
        ready, reason = auth_results.get(_auth_key("phase", profile.name), (True, ""))
        auth_str = "  ✓ ready" if ready else f"  ✗ {reason}"
        lines.append(
            f"  {label:<12}{transport:<30}  timeout={profile.timeout_seconds}s"
            f"  budget=${profile.budget_usd:.2f}{_thinking_budget_label(profile)}{auth_str}"
        )
        if not ready:
            warnings_list.append(f"{label}: {reason}")

    if config.plan.enabled:
        transport = _plan_transport_label(config.plan)
        ready, reason = auth_results.get(_auth_key("phase", "plan"), (True, ""))
        auth_str = "  ✓ ready" if ready else f"  ✗ {reason}"
        lines.append(
            f"  {'plan':<12}{transport:<30}  timeout={config.plan.timeout}s"
            f"  budget=${config.plan.budget_usd:.2f}{auth_str}"
        )
        if not ready:
            warnings_list.append(f"plan: {reason}")
    lines.append("")

    # ── REVIEW POOL ───────────────────────────────────────────────────────
    lines.append("REVIEW POOL")
    for profile in config.review_pool:
        transport = _transport_label(profile)
        ready, reason = auth_results.get(_auth_key("review", profile.name), (True, ""))
        auth_str = "✓ ready" if ready else f"✗ {reason}"
        role_str = f"  role={profile.review_role}" if profile.review_role else ""
        lines.append(
            f"  {profile.name:<22}{transport:<30}{role_str}  budget=${profile.budget_usd:.2f}"
            f"{_thinking_budget_label(profile)}  {auth_str}"
        )
        if not ready:
            warnings_list.append(f"{profile.name}: {reason} — will be skipped at runtime")

    if config.synthesis_profile is not None:
        profile = config.synthesis_profile
        transport = _transport_label(profile)
        ready, reason = auth_results.get(_auth_key("synthesis", profile.name), (True, ""))
        auth_str = "✓ ready" if ready else f"✗ {reason}"
        lines.append(
            f"  {profile.name:<22}{transport:<30}  (synthesis)  budget=${profile.budget_usd:.2f}"
            f"{_thinking_budget_label(profile)}  {auth_str}"
        )
        if not ready:
            warnings_list.append(f"{profile.name}: {reason} — will be skipped at runtime")
    lines.append("")

    # ── PLAN REVIEWERS ────────────────────────────────────────────────────
    if config.plan_agent_review.enabled:
        lines.append("PLAN REVIEWERS")
        for profile in config.plan_agent_review.profiles:
            transport = _transport_label(profile)
            ready, reason = auth_results.get(_auth_key("plan_review", profile.name), (True, ""))
            auth_str = "✓ ready" if ready else f"✗ {reason}"
            lines.append(
                f"  {profile.name:<22}{transport:<30}  budget=${profile.budget_usd:.2f}"
                f"{_thinking_budget_label(profile)}  {auth_str}"
            )
            if not ready:
                warnings_list.append(f"{profile.name}: {reason} — will be skipped at runtime")
        lines.append("")

    # ── AGENTS (adaptive pool) ────────────────────────────────────────────
    if config.assignment.enabled and config.agents:
        lines.append("AGENTS (adaptive pool)")
        for agent in config.agents:
            provider_label, transport_label = _split_provider_transport(agent.cli, agent.provider)
            transport_str = f"{provider_label:<10}{transport_label:<10}{agent.model}"
            ready, reason = auth_results.get(_auth_key("agent", agent.name), (True, ""))
            auth_str = "✓ ready" if ready else f"✗ {reason}"
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
        if a.max_cost_per_story_usd is None:
            cap_str = "no per-story routing cost cap configured"
        else:
            cap_str = f"per-story routing cost cap=${a.max_cost_per_story_usd:.2f}/story"
        lines.append(
            f"  assignment:    enabled  (min={a.min_reviewers}, max={a.max_reviewers}, {cap_str})"
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
    if config.workspace.on_approve == "merge-pr":
        lines.append(f"  merge_strategy: {config.workspace.merge_strategy}")

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

    # Capture WARNING+ log records emitted by theforge.config during load
    captured_warnings: list[str] = []
    log_handler = _CapturingHandler(captured_warnings)
    config_logger = logging.getLogger("theforge.config")
    config_logger.addHandler(log_handler)
    try:
        config = load_config(config_path)
    except Exception as exc:
        print(f"[check-config] Invalid config: {exc}", file=sys.stderr)
        return 2
    finally:
        config_logger.removeHandler(log_handler)

    # Auth results are keyed "section:name" to prevent cross-section name
    # collisions (e.g., a plan reviewer named "plan" must not mask the
    # plan-phase stub check).
    auth_results: AuthResults = {}

    _run_auth(config.preflight_profile, "phase", auth_results, config.secrets)
    _run_auth(config.dev_profile, "phase", auth_results, config.secrets)

    for p in config.review_pool:
        _run_auth(p, "review", auth_results, config.secrets)

    if config.synthesis_profile is not None:
        _run_auth(config.synthesis_profile, "synthesis", auth_results, config.secrets)

    if config.plan_agent_review.enabled:
        for p in config.plan_agent_review.profiles:
            _run_auth(
                _apply_provider_fallback(p, config.provider_fallbacks),
                "plan_review",
                auth_results,
                config.secrets,
            )

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
        plan_profile = _apply_provider_fallback(plan_profile, config.provider_fallbacks)
        _run_auth(plan_profile, "phase", auth_results, config.secrets)

    for agent in config.agents:
        _run_auth(agent.to_model_profile(allowed_tools=()), "agent", auth_results, config.secrets)

    captured_warnings.extend(
        post_run_hook_upgrade_warnings(
            config.project_root,
            config.hooks.post_run if config.hooks else None,
        )
    )

    captured_warnings.extend(_check_audit_substrate(config.project_root))

    output, exit_code = _format_config(config, auth_results)

    # Fold in captured log warnings (e.g. deprecated fields)
    if captured_warnings:
        if exit_code == 0:
            exit_code = 1
        extra = "\n".join(f"  ⚠ {w}" for w in captured_warnings)
        if "WARNINGS\n" in output:
            output = output.replace("\nWARNINGS\n", "\nWARNINGS\n" + extra + "\n", 1)
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
