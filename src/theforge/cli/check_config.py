"""forge check-config subcommand — show effective config and surface problems."""

from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

from theforge.cli.hooks import post_run_hook_upgrade_warnings
from theforge.cli.shared import _find_config, print_config_load_error
from theforge.config import (
    ForgeConfig,
    ModelProfile,
    TransportSpec,
    load_config,
    resolve_agent_spec,
)
from theforge.config.auth import check_agent_auth
from theforge.config.bridge import model_ref_to_profile
from theforge.config.models import provider_for_transport, transport_from_raw_fields
from theforge.config.profiles import _apply_transport_fallback
from theforge.config.role_derivation import derive_roles
from theforge.config.types import PlanConfig


def _format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.1f} GB"


def _format_audit_storage(
    project_root: Path,
) -> tuple[list[str], list[str], list[str]]:
    """Build the AUDIT STORAGE section for check-config output.

    Returns ``(lines, errors, warnings)``. ``lines`` is always non-empty so
    the operator sees substrate state on every run. ``errors`` indicate a
    hard failure (bumps exit code to 2); ``warnings`` are soft
    notifications also surfaced under the WARNINGS section.
    """
    from theforge.coordinator import audit_substrate

    sub_path = audit_substrate.substrate_path(project_root)
    lines: list[str] = ["AUDIT STORAGE"]
    lines.append(f"  {'mode:':<16}sqlite")
    lines.append(f"  {'path:':<16}{sub_path}")
    errors: list[str] = []
    warnings: list[str] = []

    if not sub_path.exists():
        runs = audit_substrate.runs_dir(project_root)
        has_runs = runs.exists() and any(runs.glob("*.json"))
        has_history = audit_substrate.history_jsonl_path(project_root).exists()
        if has_runs and has_history:
            status = "not initialized (per-run files and legacy history.jsonl present)"
            remediation = "forge audits rebuild --include-legacy-history"
        elif has_runs:
            status = "not initialized (per-run audit files present)"
            remediation = "forge audits rebuild"
        elif has_history:
            status = "not initialized (legacy history.jsonl present)"
            remediation = "forge audits rebuild --include-legacy-history"
        else:
            status = "not initialized"
            remediation = "run `forge audits rebuild` or your next `forge sprint` will create it"
        lines.append(f"  {'status:':<16}{status}")
        lines.append(f"  {'remediation:':<16}{remediation}")
        return lines, errors, warnings

    import sqlite3

    try:
        conn = sqlite3.connect(str(sub_path))
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            if not row or row[0] != "ok":
                detail = row[0] if row else "no result"
                msg = (
                    f"audit substrate at {sub_path} failed integrity check: "
                    f"{detail}. Run `forge audits rebuild "
                    "[--include-legacy-history]` to recover."
                )
                lines.append(f"  {'status:':<16}✗ ERROR — integrity check failed: {detail}")
                lines.append(
                    f"  {'remediation:':<16}forge audits rebuild [--include-legacy-history]"
                )
                errors.append(msg)
                return lines, errors, warnings
            schema_row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            schema_version = schema_row[0] if schema_row else "?"
            last_write_row = conn.execute(
                "SELECT MAX(COALESCE(finished_at, started_at)) FROM audit_records"
            ).fetchone()
            last_write = last_write_row[0] if last_write_row and last_write_row[0] else None
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        msg = (
            f"audit substrate at {sub_path} could not be read: {exc}. "
            "Run `forge audits rebuild [--include-legacy-history]` to recover."
        )
        lines.append(f"  {'status:':<16}✗ ERROR — could not be read: {exc}")
        lines.append(f"  {'remediation:':<16}forge audits rebuild [--include-legacy-history]")
        errors.append(msg)
        return lines, errors, warnings

    try:
        size_bytes = sub_path.stat().st_size
        size_str = _format_size(size_bytes)
    except OSError as exc:
        size_str = f"unknown ({exc})"
    last_write_str = last_write if last_write else "(no records yet)"

    lines.append(f"  {'schema version:':<16}{schema_version}")
    lines.append(f"  {'size:':<16}{size_str}")
    lines.append(f"  {'last write:':<16}{last_write_str}")
    return lines, errors, warnings


def _format_conventions(
    config: ForgeConfig,
) -> tuple[list[str], list[str]]:
    """Build the CONVENTIONS (hard) section for check-config output.

    Returns ``(lines, warnings)``. Prints the effective package roots the
    circular-import, test-mirror, and line-count checks scan so an operator
    can confirm a consumer layout is covered rather than silently no-oping.
    A configured root that doesn't exist becomes a warning (bumps exit to 1).
    """
    hard = config.conventions_hard
    if hard is None:
        return [], []

    lines: list[str] = ["CONVENTIONS (hard)"]
    warnings: list[str] = []

    if hard.package_roots:
        lines.append(f"  {'package_roots:':<16}{', '.join(hard.package_roots)}")
        for root in hard.package_roots:
            if not (config.project_root / root).exists():
                warnings.append(
                    f"conventions.hard.package_roots entry '{root}' does not exist "
                    "under the project root — its checks will find nothing"
                )
    else:
        lines.append(f"  {'package_roots:':<16}src/theforge (default scope)")

    return lines, warnings


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


def _split_provider_transport(
    provider: str | None,
    transport: TransportSpec | None,
) -> tuple[str, str]:
    """Return (provider_label, transport_label) for config display.

    Identity and transport are rendered as two separate columns on purpose: an
    operator must never have to decode ``openai-api`` to learn that a model runs
    over the API. ``provider`` is the provider family for both transports, and
    the transport column shows ``cli:<runner>`` or ``api``.
    """
    if transport is None:
        return provider or "?", "?"
    family = provider or provider_for_transport(transport) or "?"
    if transport.kind == "api":
        return family, "api"
    return family, f"cli:{transport.runner}"


def _transport_label(profile: ModelProfile) -> str:
    """Return 'provider  transport  / model' with provider and transport separate."""
    provider_label, transport_label = _split_provider_transport(
        profile.provider_family,
        profile.transport,
    )
    return f"{provider_label:<10}{transport_label:<10}{profile.model}"


def _plan_transport_label(plan: PlanConfig) -> str:
    """Return provider/transport/model label for a PlanConfig."""
    provider_label, transport_label = _split_provider_transport(
        plan.provider_family, plan.transport
    )
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

    provider_label, transport_label = _split_provider_transport(spec.provider, spec.transport)
    return f"{provider_label} {transport_label}"


def _unattributed_pricing_models(
    model_keys: list[str],
    registry: dict | None = None,
) -> list[str]:
    """Return enabled models that record a price routing is not allowed to use.

    These entries carry per-MTok figures whose attribution is unknown (typically
    a vendor-shorthand identity that resolves elsewhere at invocation time), so
    the cost band and price tie-break ignore them — see config/pricing.py.
    """
    flagged: list[str] = []
    for model_key in model_keys:
        try:
            spec = resolve_agent_spec(model_key, registry=registry)
        except ValueError:
            continue  # unresolvable keys are already reported elsewhere
        has_price = spec.input_cost_per_mtok is not None or spec.output_cost_per_mtok is not None
        if has_price and not spec.pricing_attributable:
            flagged.append(model_key)
    return flagged


def _unconfirmed_identity_models(
    model_keys: list[str],
    registry: dict | None = None,
) -> list[tuple[str, str]]:
    """Return ``(model_key, explanation)`` for enabled models with an unchecked identifier.

    A model identifier is a claim about something outside this repository, and it
    can stop being true without anything here changing — providers retire and
    re-point names, and a retired name that keeps resolving upstream produces
    successful runs whose declarations describe a different model. That is only
    catchable before spend if it is reported where configuration is read (#2352).

    Reported, never fatal: an unchecked identifier is usually fine. What it must
    not be is invisible.
    """
    today = date.today()
    rows: list[tuple[str, str]] = []
    for model_key in model_keys:
        try:
            spec = resolve_agent_spec(model_key, registry=registry)
        except ValueError:
            continue  # unresolvable keys (including retired ones) report elsewhere
        if not spec.identity.confirmed_on(today):
            rows.append((model_key, spec.identity.describe(today)))
    return rows


def _cost_band_bases(
    model_keys: list[str],
    registry: dict | None = None,
) -> list[tuple[str, int, str]]:
    """Return ``(model_key, cost_rank, basis)`` for each resolvable enabled model.

    ``cost_rank`` selects which tier a role is filled from, so it steers routing
    the same way the price tie-break does. Reporting the basis alongside it makes
    a band that came from a price distinguishable from one declared on non-price
    grounds, without the operator having to read the registry (#2203).
    """
    rows: list[tuple[str, int, str]] = []
    for model_key in model_keys:
        try:
            spec = resolve_agent_spec(model_key, registry=registry)
        except ValueError:
            continue  # unresolvable keys are already reported elsewhere
        rows.append((model_key, spec.cost_rank, spec.routing.cost_rank_basis or "unrecorded"))
    return rows


def _ref_transport_label(
    provider: str | None,
    model: str,
    transport: TransportSpec | None = None,
) -> str:
    provider_label, transport_label = _split_provider_transport(provider, transport)
    return f"{provider_label:<10}{transport_label:<10}{model}"


def _format_complexity_aware_section(
    models: list[str],
    budget_usd: float,
    overrides: dict | None,
    registry: dict | None = None,
    *,
    dev_routing_active: bool = True,
) -> list[str]:
    """Build the DERIVED ROLES section for v0.8 simple-mode configs.

    Calls derive_roles() at each complexity level and renders the tier×complexity
    routing table so the operator can verify complexity-aware routing at a glance.

    ``dev_routing_active`` reflects the effective runtime state
    (``ForgeConfig.dev_profile_is_default``): when an ``overrides.dev`` block pins
    the model, complexity-aware dev routing is disabled and the same model runs at
    every complexity. Surfacing this here makes the coupling visible at
    config-check time rather than only through changed runtime behavior. See #1764.
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
    pre_label = _ref_transport_label(pre.provider_family, pre.model, pre.transport)
    lines.append(f"  {'preflight:':<14}{pre_label:<28} (static — runs before complexity known)")

    # Plan: show per-complexity, collapsing equal adjacent levels
    def _plan_label(ra) -> str:  # type: ignore[no-untyped-def]
        r = ra.plan.ref
        return _ref_transport_label(r.provider_family, r.model, r.transport)

    pl, pm, ph = _plan_label(ra_low), _plan_label(ra_mid), _plan_label(ra_high)
    plan_parts = _collapse_complexity_labels({"LOW": pl, "MEDIUM": pm, "HIGH": ph})
    lines.append(f"  {'plan:':<14}{plan_parts}")

    # Dev: show per-complexity
    def _dev_label(ra) -> str:  # type: ignore[no-untyped-def]
        r = ra.dev.ref
        return _ref_transport_label(r.provider_family, r.model, r.transport)

    dl, dm, dh = _dev_label(ra_low), _dev_label(ra_mid), _dev_label(ra_high)
    dev_parts = _collapse_complexity_labels({"LOW": dl, "MEDIUM": dm, "HIGH": dh})
    lines.append(f"  {'dev:':<14}{dev_parts}")
    # Surface routing state whenever an overrides.dev block exists so the operator
    # can see whether their override left complexity-aware routing active (a
    # resource-only override such as a timeout) or pinned it (a model override).
    # Emitted on its own line so it stays visible even when dev_parts wraps across
    # complexity levels. See #1764.
    if (_ov or {}).get("dev"):
        if dev_routing_active:
            dev_note = "complexity-aware routing active — overrides.dev tunes limits only"
        else:
            dev_note = "routing disabled — pinned by overrides.dev"
        lines.append(f"  {'':<14}({dev_note})")

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


def _format_auth_status(profile, ready, reason, config) -> str:
    if ready is None:
        auth_str = "unverified"
        unconfirmed = _unconfirmed_identity_models(config)
        model_val = getattr(profile, "model", None)
        model_key_val = getattr(profile, "model_key", None)
        is_unconfirmed = False
        if model_val and (model_val in unconfirmed or any(model_val in u for u in unconfirmed) or any(u in model_val for u in unconfirmed)):
            is_unconfirmed = True
        if model_key_val and (model_key_val in unconfirmed or any(model_key_val in u for u in unconfirmed) or any(u in model_key_val for u in unconfirmed)):
            is_unconfirmed = True
        if is_unconfirmed:
            auth_str += " (never checked against the provider's published model list)"
        return auth_str
    return "✓ ready" if ready else f"✗ {reason}"


def _format_config(
    config: ForgeConfig,
    auth_results: AuthResults,
) -> tuple[str, int]:
    """Build the output string and determine exit code.

    Returns (output_text, exit_code) where exit_code is 0 (clean), 1
    (warnings present), or 2 (a hard error such as a corrupt audit
    substrate).
    """
    lines: list[str] = []
    warnings_list: list[str] = []
    errors_list: list[str] = []

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
                "Per-story routing cost target: unset "
                "(adaptive routes by complexity; only budget_usd enforces spend)"
            )
        else:
            lines.append(f"Per-story routing cost target: ${_cap:.2f}/story")
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
            # Prices that cannot be attributed to the identity they describe do
            # not band or break ties (#2203). Say so, or the operator reads the
            # registry literal as the figure routing used.
            unattributed = _unattributed_pricing_models(
                config.models, registry=config.model_registry
            )
            if unattributed:
                lines.append(f"  {'unattributed price:':<18}{', '.join(unattributed)}")
                lines.append(
                    "    (recorded price is not attributable to the resolved model — "
                    "ignored for cost banding and price tie-breaks)"
                )
            # The cost band is the other price-shaped routing input, so show what
            # each enabled model's band is derived from — an operator diagnosing a
            # selection can otherwise only see the number, not its source (#2203).
            # An identifier nothing has checked against the provider recently is
            # the state that produced #2352: the declaration kept describing a
            # model the name had stopped designating, and the only symptom was a
            # cost figure that looked plausible.
            unconfirmed = _unconfirmed_identity_models(
                config.models, registry=config.model_registry
            )
            if unconfirmed:
                lines.append("  upstream identifier not confirmed:")
                for model_key, explanation in unconfirmed:
                    lines.append(f"    {model_key:<32}{explanation}")
            bands = _cost_band_bases(config.models, registry=config.model_registry)
            if bands:
                lines.append("  cost band basis:")
                for model_key, rank, basis in bands:
                    lines.append(f"    {model_key:<32}rank {rank}  from {basis}")
        if config.custom_models:
            custom_details = []
            for model_id in config.custom_models:
                spec = (config.model_registry or {}).get(model_id)
                if spec is None:
                    continue
                provider_label, transport_label = _split_provider_transport(
                    spec.provider, spec.transport
                )
                custom_details.append(
                    f"{model_id} [{provider_label} {transport_label} -> {spec.model}]"
                )
            if custom_details:
                lines.append(f"  {'declared forge.yaml:':<18}{', '.join(custom_details)}")
        # Where a project declaration refines a shipped definition, the entry is
        # neither wholly builtin nor wholly forge.yaml. Name the fields the
        # project actually supplied, or the overlay reads as a replacement.
        for model_id in config.custom_models:
            field_sources = config.model_registry_field_sources.get(model_id, {})
            overlaid = sorted(f for f, src in field_sources.items() if src == "builtin")
            if overlaid:
                declared = sorted(f for f, src in field_sources.items() if src != "builtin")
                lines.append(f"  field provenance for {model_id}:")
                lines.append(f"    {'forge.yaml:':<12}{', '.join(declared) or '(none)'}")
                lines.append(f"    {'builtin:':<12}{', '.join(overlaid)}")
        # A model defined on both sides resolves from forge.yaml, but "the
        # declaration wins" is not the whole answer: it re-derives its own pricing
        # attribution, and attribution gates the prices routing reads. Say whether
        # deleting the declaration would change selection, so that is a stated fact
        # rather than something the operator finds out by deleting it.
        if config.model_registry_duplicates:
            lines.append("  also defined in the shipped catalog (forge.yaml wins):")
            for duplicate in config.model_registry_duplicates:
                if duplicate.is_redundant:
                    verdict = "identical — safe to remove"
                elif duplicate.routing_differs:
                    verdict = "ROUTING DIFFERS — removing it changes model selection"
                else:
                    verdict = "same routing; differs only in attribution recorded"
                lines.append(f"    {duplicate.canonical_id:<32}{verdict}")
                for difference in duplicate.describe():
                    lines.append(f"      {difference}")
                if duplicate.routing_differs:
                    warnings_list.append(
                        f"{duplicate.canonical_id} is declared in forge.yaml and also shipped in "
                        "the catalog, and the two resolve to different routing "
                        f"({'; '.join(d.describe() for d in duplicate.routing_differences)}) — "
                        "removing the declaration would change model selection"
                    )
        lines.append("")

    # ── DERIVED ROLES (v0.8 simple mode) ─────────────────────────────────
    if config.models and config.models_budget_usd is not None:
        derived_lines = _format_complexity_aware_section(
            config.models,
            config.models_budget_usd,
            overrides=config.models_overrides,
            registry=config.model_registry,
            dev_routing_active=config.dev_profile_is_default,
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
        auth_str = _format_auth_status(profile, ready, reason, config)
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
        auth_str = _format_auth_status(profile, ready, reason, config)
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
            auth_str = _format_auth_status(profile, ready, reason, config)
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
            provider_label, transport_label = _split_provider_transport(
                agent.effective_provider, transport_from_raw_fields(agent.cli, agent.provider)
            )
            transport_str = f"{provider_label:<10}{transport_label:<10}{agent.model}"
            ready, reason = auth_results.get(_auth_key("agent", agent.name), (True, ""))
            auth_str = _format_auth_status(profile, ready, reason, config)
            lines.append(f"  {agent.name:<22}{transport_str:<30}  tier={agent.tier:<8}{auth_str}")
            if not ready:
                warnings_list.append(f"{agent.name}: {reason} — will be skipped at runtime")
        lines.append("")

    # ── CONVENTIONS (hard) ───────────────────────────────────────────────
    conv_lines, conv_warnings = _format_conventions(config)
    if conv_lines:
        lines.extend(conv_lines)
        lines.append("")
        warnings_list.extend(conv_warnings)

    # ── AUDIT STORAGE ────────────────────────────────────────────────────
    audit_lines, audit_errors, audit_warnings = _format_audit_storage(config.project_root)
    lines.extend(audit_lines)
    lines.append("")
    errors_list.extend(audit_errors)
    warnings_list.extend(audit_warnings)

    # ── ERRORS ───────────────────────────────────────────────────────────
    if errors_list:
        lines.append("ERRORS")
        for e in errors_list:
            lines.append(f"  ✗ {e}")
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
            cap_str = "no per-story routing cost target configured"
        else:
            cap_str = f"per-story routing cost target=${a.max_cost_per_story_usd:.2f}/story"
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

    if errors_list:
        exit_code = 2
    elif warnings_list:
        exit_code = 1
    else:
        exit_code = 0
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
    except ValueError as exc:
        print_config_load_error(config_path, exc, prefix="[check-config] Invalid config")
        return 2
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
                _apply_transport_fallback(p, config.transport_fallbacks),
                "plan_review",
                auth_results,
                config.secrets,
            )

    if config.plan.enabled:
        plan_profile = model_ref_to_profile("plan", config.plan.ref)
        plan_profile = _apply_transport_fallback(plan_profile, config.transport_fallbacks)
        _run_auth(plan_profile, "phase", auth_results, config.secrets)

    for agent in config.agents:
        _run_auth(agent.to_model_profile(allowed_tools=()), "agent", auth_results, config.secrets)

    captured_warnings.extend(
        post_run_hook_upgrade_warnings(
            config.project_root,
            config.hooks.post_run if config.hooks else None,
        )
    )

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
