"""YAML loading and ForgeConfig construction."""

from __future__ import annotations

import dataclasses
import importlib
import logging
import math
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

from theforge.root_file_conventions import normalize_root_file_stacks

from ._loaders import _parse_plan_agent_review, _parse_workspace, _validate_v0_8_schema
from .auth import check_agent_auth
from .bridge import model_ref_to_profile as _model_ref_to_profile
from .bridge import role_assignment_to_profiles
from .defaults import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    PROVIDER_SDK_MAP,
    SUPPORTED_CLIS,
)
from .model_catalog import (
    PROVENANCE_FIELDS,
    ResolvedModel,
    parse_definition,
    parse_transport_block,
    resolve_project,
)
from .model_duplicates import DuplicateDeclaration, compare_duplicate_declaration
from .model_identity import MODEL_TIERS, PHASE_PLAN_REVIEW, PHASE_PREFLIGHT
from .models import (
    AGENT_REGISTRY,
    AgentSpec,
    _parse_assignment,
    known_model_overlay_providers,
    normalize_model_key,
    overlay_transport,
    resolve_agent_spec,
)
from .pricing import PRICING_PROVENANCE_OPERATOR_DECLARED
from .profiles import (
    CLI_PROVIDER_MAP,
    _agents_from_models,
    _apply_profile_overrides,
    _apply_transport_fallback,
    _parse_transport_fallbacks,
    override_constrains_model,
)
from .provenance import build_provenance, collect_leaf_paths
from .role_derivation import derive_roles
from .sandbox_capabilities import get_preset
from .secrets import _parse_notifications
from .types import (
    DEFAULT_PREFLIGHT_COMPLEXITY_GATE_THRESHOLD,
    ESCALATE_TIMEOUT_POLICIES,
    ESCALATE_TIMEOUT_PRESERVE,
    PREFLIGHT_GATE_DECOMPOSE,
    SUPPORTED_PROVIDERS,
    VALIDATION_AUTHORITIES,
    VALIDATION_AUTHORITY_ADVISORY,
    VALIDATION_AUTHORITY_MERGE,
    VALIDATION_PROFILE_NAMES,
    AdvisoryConventionsConfig,
    AdvisoryIssueFilingConfig,
    ContextConfig,
    DevConfig,
    DevVerificationCommand,
    DiagnoseConfig,
    FindingClassifierConfig,
    ForgeConfig,
    GithubConfig,
    HardConventionsConfig,
    HooksConfig,
    IntakeConfig,
    KnowledgeConfig,
    LogConfig,
    ModelProfile,
    ModelRef,
    PlanAgentReviewConfig,
    PlanConfig,
    PlanReviewConfig,
    RetryPolicy,
    SandboxConfig,
    ShapeCheckConfig,
    SprintBatchConfig,
    SprintConfig,
    StuckDetectionConfig,
    TransportFallbackConfig,
    ValidationConfig,
    ValidationProfile,
)

log = logging.getLogger("theforge.config")

_MODEL_REF_SOURCE_FIELDS = {
    "cli": "cli",
    "provider": "provider",
    "model": "model",
    "budget_usd": "budget_usd",
    "timeout": "timeout_seconds",
    "timeout_medium": "timeout_medium_seconds",
    "timeout_large": "timeout_large_seconds",
    "fallback_models": "fallback_models",
    "reasoning_effort": "reasoning_effort",
    "thinking_budget": "thinking_budget",
    "base_url": "base_url",
    "max_iterations": "max_iterations",
    "max_tool_output_bytes": "max_tool_output_bytes",
}


def _resolved_yaml_leaf_paths(raw_leaf_paths: tuple[str, ...]) -> tuple[str, ...]:
    """Map raw YAML leaf paths to the resolved config paths they directly feed."""
    resolved = set(raw_leaf_paths)
    for path in raw_leaf_paths:
        if path.startswith("plan."):
            suffix = path.removeprefix("plan.")
            head = suffix.split(".", 1)[0]
            mapped = _MODEL_REF_SOURCE_FIELDS.get(head)
            if mapped is not None:
                tail = suffix[len(head) :]
                resolved.add(f"plan.ref.{mapped}{tail}")
        if path.startswith("plan_agent_review."):
            suffix = path.removeprefix("plan_agent_review.")
            head = suffix.split(".", 1)[0]
            mapped = _MODEL_REF_SOURCE_FIELDS.get(head)
            if mapped is not None:
                tail = suffix[len(head) :]
                resolved.add(f"plan_agent_review.ref.{mapped}{tail}")
    return tuple(sorted(resolved))


_VALID_DEV_P2_POLICIES = frozenset({"in_scope", "all", "p1_only"})
_VALID_SANDBOX_KEYS = frozenset({"capability_profile", "write_roots", "mach_services"})
# Keys that would let a project *weaken* containment rather than enumerate a
# bounded addition to it. Additive grants are supported (#2038); switching the
# sandbox off, or opening it wholesale, never is.
_REJECTED_INLINE_SANDBOX_KEYS = frozenset({"allow_default", "disabled", "mode"})


def _parse_dev_config(dev_raw: Any) -> DevConfig:
    """Parse top-level DEV behavior settings from forge.yaml."""
    if dev_raw is None:
        return DevConfig()
    if not isinstance(dev_raw, dict):
        raise ValueError(
            "forge.yaml 'dev' section must be a mapping when present, "
            f"got {type(dev_raw).__name__}"
        )
    p2_policy = str(dev_raw.get("p2_policy", DevConfig.p2_policy))
    if p2_policy not in _VALID_DEV_P2_POLICIES:
        allowed = ", ".join(sorted(_VALID_DEV_P2_POLICIES))
        raise ValueError(f"dev.p2_policy must be one of {allowed}; got {p2_policy!r}")
    return DevConfig(p2_policy=p2_policy)


def _parse_sandbox_config(sandbox_raw: Any) -> SandboxConfig:
    """Parse top-level sandbox capability-profile selection from forge.yaml."""
    if sandbox_raw is None:
        return SandboxConfig()
    if not isinstance(sandbox_raw, dict):
        raise ValueError(
            "forge.yaml 'sandbox' section must be a mapping when present, "
            f"got {type(sandbox_raw).__name__}"
        )
    rejected = sorted(_REJECTED_INLINE_SANDBOX_KEYS & sandbox_raw.keys())
    if rejected:
        raise ValueError(
            "forge.yaml 'sandbox' grants are additive only — a project may add "
            "'write_roots'/'mach_services' but may not weaken containment; "
            f"remove key(s): {rejected}"
        )
    unknown = sorted(set(sandbox_raw) - _VALID_SANDBOX_KEYS)
    if unknown:
        raise ValueError(
            "forge.yaml 'sandbox' mapping supports "
            f"{sorted(_VALID_SANDBOX_KEYS)}; unknown key(s): {unknown}"
        )
    write_roots = _parse_sandbox_grant_list(sandbox_raw.get("write_roots"), "write_roots")
    mach_services = _parse_sandbox_grant_list(sandbox_raw.get("mach_services"), "mach_services")
    profile = sandbox_raw.get("capability_profile")
    if profile is None:
        return SandboxConfig(write_roots=write_roots, mach_services=mach_services)
    if not isinstance(profile, str) or not profile.strip():
        raise ValueError(
            "forge.yaml 'sandbox.capability_profile' must be a non-empty string when set"
        )
    name = profile.strip()
    # Reject unknown preset names at load time rather than at dev-run time —
    # a typo'd profile must not silently resolve to default containment.
    get_preset(name)
    return SandboxConfig(
        capability_profile=name,
        write_roots=write_roots,
        mach_services=mach_services,
    )


def _parse_sandbox_grant_list(raw: Any, key: str) -> tuple[str, ...]:
    """Normalize an additive ``sandbox.<key>`` grant list from forge.yaml.

    Malformed shapes are an error rather than a coercion: a grant silently
    dropped at load time is a capability the run believes it has and does not,
    which is the exact failure this surface exists to remove.
    """
    if raw is None:
        return ()
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        raise ValueError(
            f"forge.yaml 'sandbox.{key}' must be a list of strings when set, "
            f"got {type(raw).__name__}"
        )
    values: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(
                f"forge.yaml 'sandbox.{key}' entries must be non-empty strings; got {entry!r}"
            )
        values.append(entry.strip())
    return tuple(dict.fromkeys(values))


def _parse_models_section(
    raw_models: Any,
) -> tuple[
    list[str] | None,
    dict[str, AgentSpec],
    dict[str, dict[str, str]],
    dict[str, Any],
    bool,
]:
    """Normalize the top-level models section.

    Returns ``(enabled_models, inline_specs, inline_field_sources, custom_raw,
    simple_mode_enabled)`` where:
    - ``enabled_models`` is the selected model list, as canonical
      ``provider/model/transport-kind`` identities
    - ``inline_specs`` holds AgentSpecs declared inline under ``models.enabled``
    - ``inline_field_sources`` reports, per inline declaration, which source
      supplied each resolved field
    - ``custom_raw`` is the raw ``models.custom`` mapping (possibly empty)
    - ``simple_mode_enabled`` indicates whether the config opted into simple mode
    """
    if isinstance(raw_models, list):
        ids, inline, inline_sources = _normalize_enabled_entries(raw_models)
        return ids, inline, inline_sources, {}, True
    if raw_models is None:
        raise ValueError("'models' must be a non-empty list")
    if not isinstance(raw_models, dict):
        raise ValueError(
            "forge.yaml 'models' must be either a non-empty list or a mapping with "
            "'enabled' and/or 'custom'"
        )

    unknown = set(raw_models) - {"enabled", "custom"}
    if unknown:
        raise ValueError(
            "forge.yaml 'models' mapping only supports 'enabled' and 'custom'; "
            f"unknown key(s): {sorted(unknown)}"
        )

    enabled_raw = raw_models.get("enabled")
    custom_raw = raw_models.get("custom", {})
    if enabled_raw is not None and (not isinstance(enabled_raw, list) or len(enabled_raw) == 0):
        raise ValueError("forge.yaml 'models.enabled' must be a non-empty list")
    if not isinstance(custom_raw, dict):
        raise ValueError("forge.yaml 'models.custom' must be a mapping")

    enabled_ids, inline_specs, inline_sources = (
        _normalize_enabled_entries(enabled_raw) if enabled_raw is not None else (None, {}, {})
    )
    return enabled_ids, inline_specs, inline_sources, custom_raw, enabled_raw is not None


def _parse_enabled_mapping_entry(
    entry: dict[str, Any], index: int
) -> tuple[str, ResolvedModel | None]:
    """Normalize one canonical ``models.enabled`` mapping entry.

    Returns ``(canonical_id, resolved_or_None)``. The resolution is None when the
    entry only selects a model the built-in registry already knows — declaring
    ``provider``/``model``/``transport`` is enough to name it, and the built-in
    routing policy and pricing apply unchanged.

    An entry that *does* carry ``routing``/``cost``/``base_url`` is a definition,
    and goes through the same canonical parser as the packaged catalog.
    """
    where = f"models.enabled[{index}]"
    defn = parse_definition(entry, where=where)
    builtin = AGENT_REGISTRY.get(defn.canonical_id)
    if builtin is not None and not defn.declared:
        return defn.canonical_id, None
    return defn.canonical_id, resolve_project(defn, where=where, builtin=builtin)


def _normalize_enabled_entries(
    enabled_raw: list[Any],
) -> tuple[list[str], dict[str, AgentSpec], dict[str, dict[str, str]]]:
    """Normalize ``models.enabled`` into canonical ids plus any inline declarations.

    Two spellings are accepted at this boundary and both leave it as canonical
    ``provider/model/kind`` identities:

    - a mapping (the recommended shape): ``{provider, model, transport: {kind}}``
      plus optional ``routing``/``base_url``/``cost`` metadata;
    - a canonical id string, or one of the legacy provider-prefix spellings
      (``openai-api/…``, ``gemini-cli/…``), which is rewritten here.

    The third element carries per-field provenance for the inline declarations.
    """
    ids: list[str] = []
    inline: dict[str, AgentSpec] = {}
    field_sources: dict[str, dict[str, str]] = {}
    for index, entry in enumerate(enabled_raw):
        if isinstance(entry, dict):
            canonical_id, resolved = _parse_enabled_mapping_entry(entry, index)
            if resolved is not None:
                inline[canonical_id] = resolved.spec
                field_sources[canonical_id] = resolved.field_sources
            ids.append(canonical_id)
        else:
            ids.append(normalize_model_key(str(entry)))
    return ids, inline, field_sources


_CUSTOM_REQUIRED_KEYS = (
    "provider",
    "model",
    "tier",
    "input_cost_per_mtok",
    "output_cost_per_mtok",
)
# Top-level keys that exist only in the legacy flat shape. Their presence is what
# tells the two shapes apart: ``provider``/``model``/``transport``/``base_url``
# are spelled the same in both, so they cannot discriminate.
_CUSTOM_LEGACY_ONLY_KEYS = frozenset({"tier", "input_cost_per_mtok", "output_cost_per_mtok"})
# Operator control keys that ride alongside a declaration without being part of
# the definition itself, and so are stripped before the definition is parsed.
_CUSTOM_CONTROL_KEYS = frozenset({"override"})


def _custom_declaration_to_definition(canonical_id: str, decl: dict[str, Any]) -> dict[str, Any]:
    """Translate a legacy flat ``models.custom`` declaration into the canonical shape.

    The flat form predates the canonical schema and stays loadable unchanged:
    the fields are rearranged here (``tier`` under ``routing``,
    ``input_cost_per_mtok``/``output_cost_per_mtok`` under ``cost``) and the
    provider-like alias tokens (``openai-api``, ``gemini-cli``) are resolved to a
    provider family plus a transport kind, so the declaration is validated and
    resolved by the same parser everything else goes through.

    Field-level validation stays here rather than being delegated, so the
    messages keep naming the keys the operator actually wrote.
    """
    where = f"models.custom.{canonical_id}"
    missing = [key for key in _CUSTOM_REQUIRED_KEYS if key not in decl]
    if missing:
        raise ValueError(f"forge.yaml '{where}' is missing required field(s): {missing}")

    provider = decl["provider"]
    model = decl["model"]
    tier = decl["tier"]
    if not isinstance(provider, str) or not provider:
        raise ValueError(f"forge.yaml '{where}.provider' must be a non-empty string")
    if not isinstance(model, str) or not model:
        raise ValueError(f"forge.yaml '{where}.model' must be a non-empty string")
    if not isinstance(tier, str) or not tier:
        raise ValueError(f"forge.yaml '{where}.tier' must be a non-empty string")
    # Checked here as well as in the canonical parser: this shape spells it at the
    # top level, so delegating would report it as '<id>.routing.tier' — a key this
    # operator did not write.
    if tier not in MODEL_TIERS:
        raise ValueError(
            f"forge.yaml '{where}.tier' must be one of {sorted(MODEL_TIERS)}, got {tier!r}"
        )
    if provider not in known_model_overlay_providers():
        known = ", ".join(known_model_overlay_providers())
        raise ValueError(
            f"Unknown provider {provider!r} in {where}. Known providers/adapters: {known}"
        )

    for cost_key in ("input_cost_per_mtok", "output_cost_per_mtok"):
        value = decl[cost_key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) < 0:
            raise ValueError(
                f"forge.yaml '{where}.{cost_key}' must be a non-negative number, got {value!r}"
            )

    transport_kind = (
        parse_transport_block(decl["transport"], where)[0] if "transport" in decl else None
    )
    # Alias tokens carry an implied kind; an explicit transport block wins.
    normalized_provider, transport = overlay_transport(provider, transport_kind)
    definition: dict[str, Any] = {
        "provider": normalized_provider,
        "model": model,
        "transport": {"kind": transport.kind},
        "routing": {"tier": tier},
        # A models.custom declaration states its own prices for its own
        # identity, so the pair is attributed to the operator's declaration.
        "cost": {
            "input_per_mtok": float(decl["input_cost_per_mtok"]),
            "output_per_mtok": float(decl["output_cost_per_mtok"]),
            "pricing_provenance": PRICING_PROVENANCE_OPERATOR_DECLARED,
        },
    }
    base_url = decl.get("base_url")
    if base_url is not None:
        if not isinstance(base_url, str) or not base_url:
            raise ValueError(f"forge.yaml '{where}.base_url' must be a non-empty string")
        definition["base_url"] = base_url
    return definition


def _custom_definition(canonical_id: str, decl: Any) -> dict[str, Any]:
    """Return the canonical definition for one ``models.custom`` declaration.

    Two shapes are accepted under this key, and which one was written is decided
    by the legacy-only top-level fields (``tier``, ``input_cost_per_mtok``,
    ``output_cost_per_mtok``):

    - none present → the declaration is already canonical and goes to the shared
      parser untouched, so a reusable ``models.custom`` entry can express every
      field the shipped catalog can;
    - any present → the legacy flat shape, translated field by field.

    A declaration carrying both is rejected rather than silently resolved under
    one reading: config loading is an integrity boundary, and guessing which
    ``tier`` (flat or ``routing.tier``) the operator meant would decide routing
    on a coin flip.
    """
    where = f"models.custom.{canonical_id}"
    if not isinstance(decl, dict):
        raise ValueError(f"forge.yaml '{where}' must be a mapping")
    legacy_keys = sorted(_CUSTOM_LEGACY_ONLY_KEYS & decl.keys())
    canonical_keys = sorted({"routing", "cost"} & decl.keys())
    if legacy_keys and canonical_keys:
        raise ValueError(
            f"forge.yaml '{where}' mixes the flat declaration shape ({legacy_keys}) with the "
            f"canonical one ({canonical_keys}). Use one: move {legacy_keys} under "
            "'routing'/'cost', or drop the canonical block(s)."
        )
    if legacy_keys:
        return _custom_declaration_to_definition(canonical_id, decl)
    # Canonical: strip the operator control keys, which are not part of the
    # definition, and let the shared parser validate the rest — including the
    # adapter, so an unsupported provider fails here naming the adapters that do
    # exist.
    return {key: value for key, value in decl.items() if key not in _CUSTOM_CONTROL_KEYS}


def _parse_custom_model_registry(
    custom_raw: dict[str, Any],
) -> tuple[dict[str, AgentSpec], dict[str, str], dict[str, dict[str, str]]]:
    """Parse and validate user-declared model overlays from forge.yaml.

    Returns ``(registry, declaration_aliases, field_sources)``. The registry is
    keyed by canonical identity (``provider/model/transport-kind``); the alias map
    translates the operator-chosen declaration key into that identity so a
    ``models.enabled`` entry may still refer to the declaration by name. The
    declaration key is raw input and never reaches the registry.

    Both declaration shapes are accepted here. A declaration written in the
    canonical schema (``routing``/``cost`` blocks) is handed to the shared parser
    as-is, so ``models.custom`` is a fully expressive *reusable* definition
    surface rather than a lesser one — a definition no longer has to be inlined
    into ``models.enabled`` to set capability or phase eligibility. The legacy
    flat shape is translated into the canonical one first
    (:func:`_custom_declaration_to_definition`) and keeps loading unchanged.

    A ``models.custom`` entry is a standalone declaration, not a refinement of a
    built-in one (replacing a built-in identity requires ``override: true``), so
    it resolves without a built-in fallback and every field is its own.
    """
    registry: dict[str, AgentSpec] = {}
    aliases: dict[str, str] = {}
    field_sources: dict[str, dict[str, str]] = {}
    for canonical_id, decl in custom_raw.items():
        if not isinstance(canonical_id, str) or not canonical_id:
            raise ValueError("forge.yaml 'models.custom' keys must be non-empty strings")
        where = f"models.custom.{canonical_id}"
        definition = _custom_definition(canonical_id, decl)
        resolved = resolve_project(
            parse_definition(definition, where=where), where=where, builtin=None
        )
        # The declaration key is operator-chosen; the identity is not. Register
        # the spec under its canonical id so nothing downstream can select the
        # same model twice under two different names.
        registry[resolved.canonical_id] = resolved.spec
        field_sources[resolved.canonical_id] = resolved.field_sources
        aliases[canonical_id] = resolved.canonical_id
    return registry, aliases, field_sources


def _merge_model_registry(
    custom_registry: dict[str, AgentSpec],
    override_ids: frozenset[str] = frozenset(),
) -> tuple[dict[str, AgentSpec], tuple[DuplicateDeclaration, ...]]:
    """Merge built-in and forge.yaml model registries.

    Returns ``(merged, duplicates)``. ``duplicates`` describes every canonical
    identity defined on *both* sides, whether or not it was permitted, so a
    duplicate declaration is never resolved silently — see
    :mod:`theforge.config.model_duplicates` for why a duplicate is not assumed
    inert.

    ``override_ids`` are canonical identities the operator is allowed to replace:
    a ``models.custom`` declaration carrying ``override: true``, or an inline
    ``models.enabled`` mapping (which names the model *and* selects it in one
    place, so refining a built-in entry there is unambiguous).

    The guard is scoped to a duplicate that actually *changes dispatch*. That is
    the case ``override: true`` exists to make deliberate, and refusing it is
    what stops a project from redefining a shipped model by accident. A
    declaration that resolves to the same routing as the shipped entry is not
    redefining anything — it restates it, which is the state a configuration is
    left in when a model it declared gets promoted into the catalog it already
    matched. Refusing *that* would mean promoting a model breaks every
    configuration that already declared it, so it loads and is reported instead.
    """
    merged = dict(AGENT_REGISTRY)
    duplicates: list[DuplicateDeclaration] = []
    for canonical_id, spec in custom_registry.items():
        builtin_spec = AGENT_REGISTRY.get(canonical_id)
        if builtin_spec is not None:
            duplicate = compare_duplicate_declaration(canonical_id, spec, builtin_spec)
            duplicates.append(duplicate)
            if duplicate.routing_differs and canonical_id not in override_ids:
                differences = "; ".join(
                    difference.describe() for difference in duplicate.routing_differences
                )
                raise ValueError(
                    f"forge.yaml 'models.custom' declares {canonical_id!r}, which duplicates a "
                    "built-in model identity and resolves to different routing "
                    f"({differences}). Set override: true to replace the built-in entry "
                    "explicitly."
                )
        merged[canonical_id] = spec
    return merged, tuple(sorted(duplicates, key=lambda d: d.canonical_id))


def _log_duplicate_declarations(duplicates: tuple[DuplicateDeclaration, ...]) -> None:
    """Warn about a duplicate declaration whose presence changes routing.

    An operator deciding whether a declaration is safe to delete cannot see this
    from the file: both halves name the same model with the same numbers, and the
    difference is which identity those numbers are *attributed* to. Warning at
    load time puts it in the log for every entry point, and ``check-config``
    captures ``theforge.config`` warnings into its own WARNINGS section.
    """
    for duplicate in duplicates:
        if not duplicate.routing_differs:
            continue
        log.warning(
            "forge.yaml declares %s, which the shipped catalog also defines, and the two "
            "resolve to different routing (%s). Removing the declaration would change "
            "model selection.",
            duplicate.canonical_id,
            "; ".join(difference.describe() for difference in duplicate.routing_differences),
        )


def _validate_selected_models(models: list[str], registry: dict[str, AgentSpec]) -> None:
    """Validate the selected simple-mode model ids against the merged registry."""
    for model_key in models:
        if model_key not in registry and "/" not in model_key:
            raise ValueError(
                f"Model entry {model_key!r} must be in 'provider/model' format (contains '/') "
                "or be declared under models.custom"
            )
        resolve_agent_spec(model_key, registry=registry)


def _derive_auto_transport_fallbacks(
    models: list[str],
    *,
    registry: dict[str, AgentSpec],
) -> dict[str, TransportFallbackConfig]:
    """Auto-wire same-provider API fallbacks for CLI transport models.

    Returns only unambiguous per-provider fallbacks. If multiple CLI models from the
    same provider appear in models:, auto-wiring is skipped for that provider so we
    never attach the wrong API model to sibling CLI profiles.
    """
    cli_models_by_provider: dict[str, set[str]] = {}
    api_models_by_provider: dict[str, set[str]] = {}

    for spec in registry.values():
        if spec.transport.kind == "api":
            api_models_by_provider.setdefault(spec.provider, set()).add(spec.model)

    for model_key in models:
        spec = resolve_agent_spec(model_key, registry=registry)
        if spec.transport.kind != "cli":
            continue
        cli_models_by_provider.setdefault(spec.provider, set()).add(spec.model)

    fallbacks: dict[str, TransportFallbackConfig] = {}
    for provider, cli_models in cli_models_by_provider.items():
        if len(cli_models) != 1:
            continue
        model = next(iter(cli_models))
        if model not in api_models_by_provider.get(provider, set()):
            continue
        fallbacks[provider] = TransportFallbackConfig(provider=provider, model=model)
    return fallbacks


def _validate_auto_transport_fallback_schema(raw: dict[str, Any]) -> None:
    """Reject legacy plan_agent_review scalar config only when auto-pairing needs it.

    v0.8 generally rejects legacy scalar plan_agent_review fields alongside models:.
    For this story we still need to support the legacy scalar shape when it is a CLI
    profile that can receive the same-provider auto API fallback. Keep the integrity
    boundary strict for all other mixed-mode cases.
    """
    if "models" not in raw:
        return
    models_raw = raw.get("models")
    if isinstance(models_raw, dict):
        models_raw = models_raw.get("enabled")
    if not isinstance(models_raw, list):
        return

    par_raw = raw.get("plan_agent_review")
    if not isinstance(par_raw, dict):
        return

    legacy_fields = {"model", "cli", "provider", "budget_usd"} & par_raw.keys()
    if not legacy_fields:
        return

    cli = par_raw.get("cli")
    provider = par_raw.get("provider")
    model = par_raw.get("model")
    if provider is not None or not isinstance(cli, str) or not isinstance(model, str):
        _validate_v0_8_schema(raw)
        return

    # models_raw is still raw here (this runs before _parse_models_section), so
    # keys may be legacy spellings and entries may be mappings. Resolve each one
    # through the normalizing lookup rather than matching registry keys directly.
    def _matches(entry: Any) -> bool:
        if isinstance(entry, dict):
            return False
        try:
            spec = resolve_agent_spec(str(entry))
        except ValueError:
            return False
        return (
            spec.transport.kind == "cli"
            and spec.provider == CLI_PROVIDER_MAP.get(cli)
            and spec.model == model
        )

    if not any(_matches(entry) for entry in models_raw):
        _validate_v0_8_schema(raw)


def _validate_plan_provider(plan_cfg: "PlanConfig", secrets: dict[str, str]) -> None:
    """Raise ValueError if plan_cfg has an invalid or unconfigured provider.

    Called both from load_config (YAML path) and after --plan-model CLI override.
    """
    if plan_cfg.provider is None:
        return
    if plan_cfg.provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Unsupported provider {plan_cfg.provider!r} in plan section. "
            f"Supported: {sorted(SUPPORTED_PROVIDERS)}"
        )
    sdk = PROVIDER_SDK_MAP.get(plan_cfg.provider)
    if sdk:
        try:
            importlib.import_module(sdk)
        except ImportError:
            raise ValueError(
                f"plan section uses provider '{plan_cfg.provider}' but the required "
                f"SDK '{sdk}' is not installed. Please install it."
            )
    # Build a stub profile to reuse check_agent_auth for key validation
    _stub = ModelProfile(
        name="plan",
        cli=None,
        provider=plan_cfg.provider,
        model=plan_cfg.model,
        budget_usd=plan_cfg.budget_usd,
        timeout_seconds=plan_cfg.timeout,
        allowed_tools=(),
    )
    _ready, _reason = check_agent_auth(_stub, secrets)
    if not _ready:
        raise ValueError(f"plan section uses provider '{plan_cfg.provider}': {_reason}")


def _parse_diagnose_config(raw: Any) -> "DiagnoseConfig":
    """Build a DiagnoseConfig from the optional ``diagnose:`` block."""
    from theforge.diagnose_types import DIAGNOSE_OUTPUT_DESTINATIONS

    defaults = DiagnoseConfig()
    if raw is None or raw == {}:
        return defaults
    if not isinstance(raw, dict):
        raise ValueError(f"forge.yaml 'diagnose' must be a mapping, got {type(raw).__name__}")

    dest = raw.get("output_destination", defaults.output_destination)
    if not isinstance(dest, str) or dest not in DIAGNOSE_OUTPUT_DESTINATIONS:
        raise ValueError(
            f"forge.yaml 'diagnose.output_destination' must be one of "
            f"{sorted(DIAGNOSE_OUTPUT_DESTINATIONS)}, got {dest!r}"
        )

    budget = raw.get("budget_usd", defaults.budget_usd)
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= 0:
        raise ValueError(
            f"forge.yaml 'diagnose.budget_usd' must be a positive number, got {budget!r}"
        )

    timeout = raw.get("timeout_seconds", defaults.timeout_seconds)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise ValueError(
            f"forge.yaml 'diagnose.timeout_seconds' must be a positive integer, got {timeout!r}"
        )

    autonomous = raw.get("autonomous_default", defaults.autonomous_default)
    if not isinstance(autonomous, bool):
        raise ValueError(
            f"forge.yaml 'diagnose.autonomous_default' must be a bool, got {autonomous!r}"
        )

    return DiagnoseConfig(
        output_destination=dest,
        budget_usd=float(budget),
        timeout_seconds=int(timeout),
        autonomous_default=autonomous,
    )


def _parse_stuck_detection(raw: Any) -> "StuckDetectionConfig":
    """Build a StuckDetectionConfig from the optional 'stuck_detection:' block.

    Defaults are applied when keys are missing. Type errors raise ValueError so
    misconfiguration surfaces at load time rather than mid-run.
    """
    if raw is None:
        return StuckDetectionConfig()
    if not isinstance(raw, dict):
        raise ValueError(
            f"forge.yaml 'stuck_detection' must be a mapping, got {type(raw).__name__}"
        )
    defaults = StuckDetectionConfig()
    fields = {
        "enabled": (bool, defaults.enabled),
        "no_progress_iterations": (int, defaults.no_progress_iterations),
        "repeat_threshold": (int, defaults.repeat_threshold),
        "error_threshold": (int, defaults.error_threshold),
        "post_nudge_iterations": (int, defaults.post_nudge_iterations),
    }
    multiplier_fields = {
        "no_progress_multipliers": defaults.no_progress_multipliers,
        "post_nudge_multipliers": defaults.post_nudge_multipliers,
    }
    kwargs: dict[str, Any] = {}
    for key, (typ, default) in fields.items():
        val = raw.get(key, default)
        if typ is bool:
            if not isinstance(val, bool):
                raise ValueError(f"forge.yaml 'stuck_detection.{key}' must be bool, got {val!r}")
        else:
            if isinstance(val, bool) or not isinstance(val, int) or val < 1:
                raise ValueError(
                    f"forge.yaml 'stuck_detection.{key}' must be a positive int, got {val!r}"
                )
        kwargs[key] = val
    for key, default in multiplier_fields.items():
        if key not in raw:
            kwargs[key] = default
            continue
        val = raw[key]
        if not isinstance(val, dict):
            raise ValueError(
                f"forge.yaml 'stuck_detection.{key}' must be a mapping, got {type(val).__name__}"
            )
        parsed: dict[str, float] = {}
        for k, v in val.items():
            if not isinstance(k, str):
                raise ValueError(
                    f"forge.yaml 'stuck_detection.{key}' keys must be strings, "
                    f"got {type(k).__name__}"
                )
            if (
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(v)
                or v <= 0
            ):
                raise ValueError(
                    f"forge.yaml 'stuck_detection.{key}.{k}' must be a finite positive number, "
                    f"got {v!r}"
                )
            parsed[k] = float(v)
        kwargs[key] = parsed
    return StuckDetectionConfig(**kwargs)


def _validated_failed_test_pattern(raw: Any) -> str | None:
    """Validate the optional ``validation.failed_test_pattern`` regex.

    Returns the pattern unchanged when it is a compilable regex, ``None`` when
    unset. A malformed pattern is a config error the operator must fix, so it
    fails loudly at load time rather than silently disabling extraction at
    runtime.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(
            "forge.yaml validation.failed_test_pattern must be a string regex, "
            f"got {type(raw).__name__}."
        )
    try:
        re.compile(raw)
    except re.error as exc:
        raise ValueError(
            f"forge.yaml validation.failed_test_pattern is not a valid regex: {exc}"
        ) from exc
    return raw


def _validated_gate_timeout(raw: Any) -> int | None:
    """Validate ``validation.gate_timeout``.

    Returns ``None`` when unset (the adaptive resolver applies its own
    default). A non-numeric or non-positive value is a config error the
    operator must fix, so it fails loudly at load time rather than silently
    disabling adaptive gate-timeout scaling at sprint-run time.
    """
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(
            f"forge.yaml validation.gate_timeout must be an integer number of "
            f"seconds, got {raw!r} ({type(raw).__name__})."
        )
    if raw <= 0:
        raise ValueError(f"forge.yaml validation.gate_timeout must be positive, got {raw!r}.")
    return raw


def _validated_gate_cpu_cores(raw: Any) -> int | None:
    """Validate ``validation.gate_cpu_cores``.

    Returns ``None`` when unset (the adaptive resolver falls back to host
    core count). A non-numeric or non-positive value is a config error the
    operator must fix, so it fails loudly at load time rather than silently
    disabling adaptive gate-timeout scaling at sprint-run time.
    """
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(
            f"forge.yaml validation.gate_cpu_cores must be an integer core count, "
            f"got {raw!r} ({type(raw).__name__})."
        )
    if raw <= 0:
        raise ValueError(f"forge.yaml validation.gate_cpu_cores must be positive, got {raw!r}.")
    return raw


def _validated_gate_timeout_scale(raw: Any) -> str:
    """Validate ``validation.gate_timeout_scale`` against supported modes."""
    if raw is None:
        return "adaptive"
    if not isinstance(raw, str):
        raise ValueError(
            "forge.yaml validation.gate_timeout_scale must be a string "
            "('adaptive' or 'fixed'), "
            f"got {type(raw).__name__}."
        )
    if raw not in {"adaptive", "fixed"}:
        raise ValueError(
            f"forge.yaml validation.gate_timeout_scale must be 'adaptive' or 'fixed', got {raw!r}."
        )
    return raw


# A verification command name is the only token the dev agent controls, and it
# is used to build a filesystem path in the request channel. Constrain it to a
# flat, traversal-free token at load time so nothing downstream has to.
_DEV_VERIFICATION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _validated_validation_profiles(raw: Any) -> tuple[ValidationProfile, ...]:
    """Validate ``validation.profiles`` (issue #2358).

    A project declares what each of its checks costs and what its result is
    worth::

        validation:
          profiles:
            complete:
              command: make gate
              authority: merge
            fast: make test-fast
            targeted: make test TARGET={test_target}

    Everything here is a load-time error rather than a runtime surprise, because
    every one of these mistakes would otherwise be invisible at the only moment
    it matters — a run whose result is trusted for a merge:

    * an unrecognised profile name would load and then never be selected, since
      forge selects by meaning and knows only these three;
    * an empty or non-string command would resolve to a shell no-op that exits
      zero, which is a passing gate that ran nothing;
    * zero or several merge-authority profiles would leave "which result decides
      the merge" ambiguous, which is the exact question profiles exist to answer.

    An absent or empty declaration returns ``()`` — the project declared nothing
    new and keeps the legacy gate_command/test_command behaviour untouched.
    """
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ValueError(
            "forge.yaml validation.profiles must be a mapping of profile name -> "
            f"command, got {type(raw).__name__}."
        )
    if not raw:
        return ()
    entries: list[ValidationProfile] = []
    for name, spec in raw.items():
        if not isinstance(name, str) or name not in VALIDATION_PROFILE_NAMES:
            raise ValueError(
                f"forge.yaml validation.profiles has an unknown profile name {name!r}: "
                f"must be one of {', '.join(VALIDATION_PROFILE_NAMES)}. Forge selects a "
                "profile by meaning, so a name it does not recognise would load and "
                "then never run."
            )
        if isinstance(spec, str):
            spec = {"command": spec}
        if not isinstance(spec, dict):
            raise ValueError(
                f"forge.yaml validation.profiles.{name} must be a command string or a "
                f"mapping, got {type(spec).__name__}."
            )
        command = spec.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError(
                f"forge.yaml validation.profiles.{name}.command must be a non-empty string."
            )
        authority = spec.get("authority", VALIDATION_AUTHORITY_ADVISORY)
        if not isinstance(authority, str) or authority not in VALIDATION_AUTHORITIES:
            raise ValueError(
                f"forge.yaml validation.profiles.{name}.authority must be one of "
                f"{', '.join(VALIDATION_AUTHORITIES)}, got {authority!r}."
            )
        unknown_keys = set(spec) - {"command", "authority"}
        if unknown_keys:
            raise ValueError(
                f"forge.yaml validation.profiles.{name} has unknown key(s) "
                f"{sorted(unknown_keys)}: only 'command' and 'authority' are supported."
            )
        entries.append(ValidationProfile(name=name, command=command.strip(), authority=authority))
    merge_profiles = [entry.name for entry in entries if entry.is_merge_authority]
    if len(merge_profiles) != 1:
        raise ValueError(
            "forge.yaml validation.profiles must declare exactly one profile with "
            f"'authority: {VALIDATION_AUTHORITY_MERGE}' (found {len(merge_profiles)}"
            + (f": {', '.join(merge_profiles)}" if merge_profiles else "")
            + "). Exactly one result may establish merge authority."
        )
    return tuple(entries)


def _validated_dev_verification_commands(raw: Any) -> tuple[DevVerificationCommand, ...]:
    """Validate ``validation.dev_verification_commands`` (ADR-0007).

    These commands run **outside** the dev sandbox, so the declaration is an
    integrity boundary: every field is checked here and a malformed entry is a
    load-time error rather than a runtime surprise on a command that is already
    executing unconfined. Accepts either the short spelling::

        dev_verification_commands:
          verify-watch: xcodebuild -scheme Watch test

    or the full mapping form with bounded limits::

        dev_verification_commands:
          verify-watch:
            command: xcodebuild -scheme Watch test
            timeout: 900
            output_tail_chars: 8000
    """
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ValueError(
            "forge.yaml validation.dev_verification_commands must be a mapping of "
            f"name -> command, got {type(raw).__name__}."
        )
    entries: list[DevVerificationCommand] = []
    for name, spec in raw.items():
        if not isinstance(name, str) or not _DEV_VERIFICATION_NAME_RE.match(name):
            raise ValueError(
                "forge.yaml validation.dev_verification_commands has an invalid command "
                f"name {name!r}: names must be 1-64 characters of letters, digits, "
                "'.', '_' or '-' and start with a letter or digit."
            )
        defaults = DevVerificationCommand(name=name, command="")
        if isinstance(spec, str):
            spec = {"command": spec}
        if not isinstance(spec, dict):
            raise ValueError(
                f"forge.yaml validation.dev_verification_commands.{name} must be a command "
                f"string or a mapping, got {type(spec).__name__}."
            )
        unknown = sorted(set(spec) - {"command", "timeout", "output_tail_chars"})
        if unknown:
            raise ValueError(
                f"forge.yaml validation.dev_verification_commands.{name} has unknown "
                f"field(s) {unknown}: allowed fields are command, timeout, "
                "output_tail_chars."
            )
        command = spec.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError(
                f"forge.yaml validation.dev_verification_commands.{name}.command must be a "
                f"non-empty shell command string, got {command!r}."
            )
        limits: dict[str, int] = {}
        for field_name, default in (
            ("timeout", defaults.timeout),
            ("output_tail_chars", defaults.output_tail_chars),
        ):
            value = spec.get(field_name, default)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(
                    f"forge.yaml validation.dev_verification_commands.{name}.{field_name} "
                    f"must be a positive integer, got {value!r}."
                )
            limits[field_name] = value
        entries.append(DevVerificationCommand(name=name, command=command.strip(), **limits))
    return tuple(entries)


def _validated_dev_verification_max_requests(raw: Any) -> int:
    """Validate ``validation.dev_verification_max_requests`` (ADR-0007).

    The per-iteration request budget is what keeps a mediated verification
    channel from degenerating into a per-token gate run, so a non-positive or
    non-integer value is refused rather than silently treated as "unbounded".
    """
    if raw is None:
        return DEFAULT_VALIDATION.dev_verification_max_requests
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(
            "forge.yaml validation.dev_verification_max_requests must be an integer "
            f"request count, got {raw!r} ({type(raw).__name__})."
        )
    if raw <= 0:
        raise ValueError(
            f"forge.yaml validation.dev_verification_max_requests must be positive, got {raw!r}."
        )
    return raw


def _resolve_project_root(config_path: Path) -> Path:
    """Resolve the project root for a given forge.yaml path.

    Forge-created worktrees live at ``<project_root>/.forge/worktrees/<slug>/``
    and contain a synced ``forge.yaml`` but no ``.forge/.env``. When config is
    loaded from such a worktree, the project-scoped secret store and other
    project-level state belong to the parent checkout, not the worktree.
    Detect that layout and walk up so secrets and project_root reference the
    real project root.
    """
    parent = config_path.parent.resolve()
    grandparent = parent.parent
    great_grandparent = grandparent.parent
    if (
        grandparent.name == "worktrees"
        and great_grandparent.name == ".forge"
        and great_grandparent.parent != great_grandparent
    ):
        return great_grandparent.parent
    return parent


def load_config(config_path: Path) -> ForgeConfig:
    """Load forge.yaml and return a typed ForgeConfig.

    The config file path is used to derive the project root (its parent directory),
    except when ``config_path`` lives inside a forge-created worktree at
    ``<root>/.forge/worktrees/<slug>/``, in which case the project root is
    resolved to the parent checkout so project-scoped secrets remain accessible.
    Missing sections fall back to sensible defaults.

    Raises ValueError for invalid configurations (empty pool, duplicate names,
    unsupported CLI, missing synthesis profile when pool size > 1).
    """
    project_root = _resolve_project_root(config_path)

    # Load project-scoped secrets before profile validation so _resolve_secret() works.
    env_path = project_root / ".forge" / ".env"
    secrets_yaml_path = project_root / ".forge" / "secrets.yaml"
    secrets: dict[str, str] = {}
    if env_path.exists():
        raw = dotenv_values(env_path)
        if any(v is None for v in raw.values()):
            raise ValueError(f"{env_path}: malformed .env")
        secrets = {k: v for k, v in raw.items() if v is not None}
    elif secrets_yaml_path.exists():
        log.warning(
            "⚠ .forge/secrets.yaml detected — migrate to .forge/.env (see .forge/.env.example)"
        )

    with open(config_path, encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}
    yaml_leaf_paths = _resolved_yaml_leaf_paths(collect_leaf_paths(raw))
    derived_path_prefixes: set[str] = {
        "review_pool_is_default",
        "plan_model_is_default",
        "dev_profile_is_default",
    }

    try:
        _validate_v0_8_schema(raw)
    except ValueError as exc:
        if "plan_agent_review has legacy scalar field(s)" not in str(exc):
            raise
    _validate_auto_transport_fallback_schema(raw)

    if "provider_fallbacks" in raw:
        raise ValueError(
            "forge.yaml 'provider_fallbacks' was renamed to 'transport_fallback': it configures "
            "a CLI→API transport fallback within one provider, not a fallback to a different "
            "provider. Rename the key."
        )
    transport_fallbacks = _parse_transport_fallbacks(
        raw.get("transport_fallback", {}),
        secrets=secrets,
    )
    auto_transport_fallback = bool(raw.get("auto_transport_fallback", True))

    workspace = _parse_workspace(raw.get("workspace", {}))

    # Validation
    val_data = raw.get("validation", {})
    _stale_keys = [k for k in ("handoff_file", "gate_decision_key") if k in val_data]
    if _stale_keys:
        raise ValueError(
            f"forge.yaml sets {_stale_keys} under validation: "
            "— these fields were removed in v0.8. "
            "Gate pass/fail is now determined by exit code only. "
            "Remove handoff_file and gate_decision_key from your forge.yaml."
        )
    validation = ValidationConfig(
        gate_command=val_data.get("gate_command", DEFAULT_VALIDATION.gate_command),
        gate_timeout=_validated_gate_timeout(val_data.get("gate_timeout")),
        gate_output_tail_chars=int(
            val_data.get("gate_output_tail_chars", DEFAULT_VALIDATION.gate_output_tail_chars)
        ),
        gate_debug_command=val_data.get("gate_debug_command"),
        gate_debug_timeout=val_data.get("gate_debug_timeout"),
        gate_diagnostic_enabled=bool(
            val_data.get("gate_diagnostic_enabled", DEFAULT_VALIDATION.gate_diagnostic_enabled)
        ),
        gate_diagnostic_command=val_data.get("gate_diagnostic_command"),
        gate_diagnostic_per_test_timeout=int(
            val_data.get(
                "gate_diagnostic_per_test_timeout",
                DEFAULT_VALIDATION.gate_diagnostic_per_test_timeout,
            )
        ),
        gate_diagnostic_budget=int(
            val_data.get("gate_diagnostic_budget", DEFAULT_VALIDATION.gate_diagnostic_budget)
        ),
        test_command=val_data.get("test_command"),
        pre_validate_command=val_data.get("pre_validate_command"),
        failed_test_pattern=_validated_failed_test_pattern(val_data.get("failed_test_pattern")),
        gate_cpu_cores=_validated_gate_cpu_cores(val_data.get("gate_cpu_cores")),
        gate_timeout_scale=_validated_gate_timeout_scale(val_data.get("gate_timeout_scale")),
        default_test_target=str(
            val_data.get("default_test_target", DEFAULT_VALIDATION.default_test_target)
        ),
        dev_verification_commands=_validated_dev_verification_commands(
            val_data.get("dev_verification_commands")
        ),
        dev_verification_max_requests=_validated_dev_verification_max_requests(
            val_data.get("dev_verification_max_requests")
        ),
        profiles=_validated_validation_profiles(val_data.get("profiles")),
    )

    # ── v0.8 models: key ──────────────────────────────────────────────
    models: list[str] | None = None
    _review_pool_is_default = False
    _dev_profile_is_default = False
    _derived_plan_profile: ModelProfile | None = None
    _derived_plan_validate_spec: bool | None = None
    _derived_par_profile: ModelProfile | None = None
    budget_usd_val: float | None = None
    _raw_overrides: dict[str, Any] | None = None
    model_registry = dict(AGENT_REGISTRY)
    model_registry_sources = {key: "builtin" for key in AGENT_REGISTRY}
    # Entry-level source (above) says which file an entry came from; this says
    # which file supplied each *field* of it, which is the only way to read a
    # partial project overlay of a shipped definition.
    model_registry_field_sources: dict[str, dict[str, str]] = {
        key: {field: "builtin" for field in PROVENANCE_FIELDS} for key in AGENT_REGISTRY
    }
    custom_models: tuple[str, ...] = ()
    model_registry_duplicates: tuple[DuplicateDeclaration, ...] = ()
    if "models" in raw:
        (
            models_list,
            inline_specs,
            inline_field_sources,
            custom_raw,
            simple_mode_enabled,
        ) = _parse_models_section(raw["models"])
    else:
        models_list, inline_specs, inline_field_sources, custom_raw, simple_mode_enabled = (
            None,
            {},
            {},
            {},
            False,
        )
    overlay_registry, overlay_aliases, overlay_field_sources = _parse_custom_model_registry(
        custom_raw
    )
    custom_registry = {**overlay_registry, **inline_specs}
    if models_list is not None and overlay_aliases:
        models_list = [overlay_aliases.get(key, key) for key in models_list]
    if custom_registry:
        override_ids = frozenset(inline_specs) | frozenset(
            identity
            for decl_key, identity in overlay_aliases.items()
            if custom_raw.get(decl_key, {}).get("override", False)
        )
        model_registry, model_registry_duplicates = _merge_model_registry(
            custom_registry, override_ids
        )
        _log_duplicate_declarations(model_registry_duplicates)
        custom_models = tuple(sorted(custom_registry))
        model_registry_sources.update({key: "forge.yaml" for key in custom_registry})
        model_registry_field_sources.update({**overlay_field_sources, **inline_field_sources})

    if simple_mode_enabled:
        assert models_list is not None
        _validate_selected_models(models_list, model_registry)
        budget_usd_raw = raw.get("budget_usd", 50.0)
        budget_usd_val = float(budget_usd_raw)
        if budget_usd_val <= 0:
            raise ValueError("budget_usd must be positive")

        # v0.8: overrides: key carries partial role overrides.
        # plan_agent_review overrides are passed into derive_roles() so the bridge
        # can lower them to a ModelProfile (fixes silent loss of that config).
        overrides = raw.get("overrides") or {}
        _raw_overrides = dict(overrides) if overrides else None
        _par_derive_overrides: dict[str, Any] | None = (
            {"plan_agent_review": overrides["plan_agent_review"]}
            if "plan_agent_review" in overrides
            else None
        )
        _ra = derive_roles(
            models_list,
            overrides=_par_derive_overrides,
            budget_usd=budget_usd_val,
            registry=model_registry,
        )
        _bridge = role_assignment_to_profiles(_ra)
        dev_profile = _bridge["dev_profile"]
        preflight_profile = _bridge["preflight_profile"]
        review_pool = _bridge["review_pool"]
        synthesis_profile = _bridge["synthesis_profile"]
        _derived_plan_profile = _bridge["plan_profile"]
        _derived_plan_validate_spec = _bridge["plan_validate_spec"]
        _derived_par_profile = _bridge.get("plan_agent_review_profile")
        derived_path_prefixes.update(
            {
                "dev_profile",
                "preflight_profile",
                "review_pool",
                "synthesis_profile",
                "agents",
                "models_budget_usd",
            }
        )

        # Apply explicit profile overrides (partial override supported)
        if "dev" in overrides:
            dev_profile = _apply_profile_overrides(dev_profile, overrides["dev"])
        if "preflight" in overrides:
            preflight_profile = _apply_profile_overrides(preflight_profile, overrides["preflight"])
        preflight_fallback_profile = None
        if "preflight_fallback" in overrides:
            preflight_fallback_profile = _apply_profile_overrides(
                preflight_profile,
                overrides["preflight_fallback"],
            )
            preflight_fallback_profile = dataclasses.replace(
                preflight_fallback_profile,
                name="preflight_fallback",
                phase=PHASE_PREFLIGHT,
            )
        if synthesis_profile is not None and "synthesis" in overrides:
            synthesis_profile = _apply_profile_overrides(synthesis_profile, overrides["synthesis"])
        # Apply per-reviewer overrides matched by name
        if "review_pool" in overrides:
            pool_overrides = overrides["review_pool"]
            if isinstance(pool_overrides, list):
                override_by_name: dict[str, dict[str, Any]] = {
                    e["name"]: e for e in pool_overrides if isinstance(e, dict) and "name" in e
                }
                review_pool = [
                    _apply_profile_overrides(p, override_by_name[p.name])
                    if p.name in override_by_name
                    else p
                    for p in review_pool
                ]

        models = list(models_list)
        if auto_transport_fallback:
            auto_transport_fallbacks = _derive_auto_transport_fallbacks(
                models,
                registry=model_registry,
            )
            transport_fallbacks = {**auto_transport_fallbacks, **transport_fallbacks}
            derived_path_prefixes.add("transport_fallbacks")
        # Track which roles were auto-derived vs explicitly overridden. Complexity-aware
        # adaptation (preflight._apply_complexity_adaptation) only rewrites auto-derived
        # roles so explicit overrides bypass routing. A dev override that only tunes
        # resource limits (timeouts, budget) expresses a preference about budgets, not
        # model selection, and must leave routing active — only a model-constraining
        # override pins the role. See #1764.
        _dev_profile_is_default = not override_constrains_model(overrides.get("dev"))
        _review_pool_is_default = "review_pool" not in overrides

    else:
        # No v0.8 models: key — fall back to built-in defaults.
        dev_profile = DEFAULT_DEV_PROFILE
        preflight_profile = DEFAULT_PREFLIGHT_PROFILE
        preflight_fallback_profile = None
        review_pool = [DEFAULT_REVIEW_PROFILE]
        synthesis_profile = None
        _review_pool_is_default = True

    dev_profile = _apply_transport_fallback(dev_profile, transport_fallbacks)
    preflight_profile = _apply_transport_fallback(preflight_profile, transport_fallbacks)
    if preflight_fallback_profile is not None:
        preflight_fallback_profile = _apply_transport_fallback(
            preflight_fallback_profile, transport_fallbacks
        )
    review_pool = [
        _apply_transport_fallback(profile, transport_fallbacks) for profile in review_pool
    ]
    if synthesis_profile is not None:
        synthesis_profile = _apply_transport_fallback(synthesis_profile, transport_fallbacks)

    # Retry
    retry_data = raw.get("retry", {})
    # Config loading is an integrity boundary: a typo here silently decides what
    # an unattended overnight expiry does, so refuse it rather than falling back
    # (#2279). Only the new field is validated — escalate_policy's existing
    # tolerance is left exactly as it was so opting in changes nothing else.
    escalate_timeout_policy = str(
        retry_data.get("escalate_timeout_policy", ESCALATE_TIMEOUT_PRESERVE)
    )
    if escalate_timeout_policy not in ESCALATE_TIMEOUT_POLICIES:
        raise ValueError(
            f"retry.escalate_timeout_policy: unknown value {escalate_timeout_policy!r} "
            f"(expected one of {', '.join(ESCALATE_TIMEOUT_POLICIES)})"
        )
    # Config loading is an integrity boundary (convention 2): a negative
    # allowance would silently read as "disabled" while looking like a limit.
    max_spec_gap_pauses = int(retry_data.get("max_spec_gap_pauses", 1))
    if max_spec_gap_pauses < 0:
        raise ValueError(
            f"retry.max_spec_gap_pauses: must be >= 0, got {max_spec_gap_pauses} "
            "(0 disables the specification-gap channel)"
        )
    # Preflight complexity gate (#2681). The threshold is an ordinary integer:
    # a value above the highest score preflight can assign disables the gate, so
    # no separate enable switch exists and none is validated here. The
    # no-decision action is deliberately NOT validated at load: unlike
    # escalate_timeout_policy, whose two values are equally safe, a typo here
    # must not be able to authorise spend — it is resolved fail-closed to
    # ``decompose`` at the gate, which records that a fallback was applied.
    preflight_complexity_gate_threshold = int(
        retry_data.get(
            "preflight_complexity_gate_threshold",
            DEFAULT_PREFLIGHT_COMPLEXITY_GATE_THRESHOLD,
        )
    )
    preflight_complexity_gate_no_decision = str(
        retry_data.get("preflight_complexity_gate_no_decision", PREFLIGHT_GATE_DECOMPOSE)
    )
    retry = RetryPolicy(
        max_dev_iterations=int(retry_data.get("max_dev_iterations", 3)),
        max_spec_gap_pauses=max_spec_gap_pauses,
        preflight_complexity_gate_threshold=preflight_complexity_gate_threshold,
        preflight_complexity_gate_no_decision=preflight_complexity_gate_no_decision,
        max_dev_transport_retries=int(retry_data.get("max_dev_transport_retries", 1)),
        max_plan_transport_retries=int(retry_data.get("max_plan_transport_retries", 2)),
        max_review_cycles=int(retry_data.get("max_review_cycles", 2)),
        max_review_parse_retries=int(retry_data.get("max_review_parse_retries", 2)),
        max_diagnose_parse_retries=int(retry_data.get("max_diagnose_parse_retries", 2)),
        max_plan_review_transport_retries=int(
            retry_data.get("max_plan_review_transport_retries", 2)
        ),
        max_plan_review_parse_retries=int(retry_data.get("max_plan_review_parse_retries", 2)),
        max_review_transport_retries=int(retry_data.get("max_review_transport_retries", 2)),
        review_transport_retry_backoff_seconds=float(
            retry_data.get("review_transport_retry_backoff_seconds", 8.0)
        ),
        review_quorum_threshold=int(retry_data.get("review_quorum_threshold", 2)),
        review_degrade_on_infra_failure=bool(
            retry_data.get("review_degrade_on_infra_failure", True)
        ),
        review_transient_failure_codes=tuple(
            str(code)
            for code in retry_data.get(
                "review_transient_failure_codes",
                ("rate_limit", "provider_internal_error", "connection_reset"),
            )
        ),
        review_transient_output_patterns=tuple(
            str(p).lower()
            for p in retry_data.get(
                "review_transient_output_patterns",
                RetryPolicy.__dataclass_fields__["review_transient_output_patterns"].default,
            )
        ),
        max_plan_regen_attempts=int(retry_data.get("max_plan_regen_attempts", 3)),
        demotion_threshold=int(retry_data.get("demotion_threshold", 2)),
        plan_escalation_threshold=int(retry_data.get("plan_escalation_threshold", 2)),
        escalate_policy=str(retry_data.get("escalate_policy", "prompt")),
        escalate_timeout_policy=escalate_timeout_policy,
        auto_model_escalation=bool(retry_data.get("auto_model_escalation", False)),
        adaptive_iterations=bool(retry_data.get("adaptive_iterations", True)),
        max_dev_iterations_cap=int(retry_data.get("max_dev_iterations_cap", 0)),
        max_review_cycles_cap=int(retry_data.get("max_review_cycles_cap", 0)),
        review_zero_findings_stop=int(retry_data.get("review_zero_findings_stop", 0)),
        p2_cleanup_enabled=bool(retry_data.get("p2_cleanup_enabled", True)),
        p2_cleanup_max_iterations=int(retry_data.get("p2_cleanup_max_iterations", 0)),
    )

    notifications, notification_environment_sources = _parse_notifications(
        raw.get("notifications", {}), secrets
    )
    dev_cfg = _parse_dev_config(raw.get("dev"))
    sandbox_cfg = _parse_sandbox_config(raw.get("sandbox"))

    github_data = raw.get("github", {})
    github_cfg = GithubConfig(enabled=bool(github_data.get("enabled", False)))

    # Plan
    plan_data = raw.get("plan", {})

    _plan_model_is_default = (
        "cli" not in plan_data and "model" not in plan_data and "provider" not in plan_data
    )

    plan_timeout_medium_raw = plan_data.get("timeout_medium")
    plan_timeout_large_raw = plan_data.get("timeout_large")

    # Smart-config: when the user supplies `models:` and has not overridden the
    # plan section's transport/model, source PlanConfig from the derived plan
    # role so adaptive routing actually reaches the PLAN phase. Otherwise fall
    # back to the legacy defaults (cli=claude, model=sonnet).
    if _derived_plan_profile is not None and _plan_model_is_default:
        derived_path_prefixes.add("plan")
        _plan_default_cli: str | None = _derived_plan_profile.cli
        _plan_default_model: str = _derived_plan_profile.model
        _plan_default_provider: str | None = _derived_plan_profile.provider
        _plan_default_budget: float = _derived_plan_profile.budget_usd
        _plan_default_timeout: int = _derived_plan_profile.timeout_seconds
        _plan_default_timeout_medium: int | None = _derived_plan_profile.timeout_medium_seconds
        _plan_default_timeout_large: int | None = _derived_plan_profile.timeout_large_seconds
        _plan_default_validate_spec: bool = (
            _derived_plan_validate_spec if _derived_plan_validate_spec is not None else True
        )
    else:
        _plan_default_cli = "claude"
        _plan_default_model = "sonnet"
        _plan_default_provider = None
        _plan_default_budget = 0.50
        _plan_default_timeout = 600
        _plan_default_timeout_medium = None
        _plan_default_timeout_large = None
        _plan_default_validate_spec = True

    _plan_provider = plan_data.get("provider", _plan_default_provider)
    _plan_cli = plan_data.get("cli", _plan_default_cli) if _plan_provider is None else None
    plan_cfg = PlanConfig.of(
        enabled=bool(plan_data.get("enabled", False)),
        cli=_plan_cli,
        model=str(plan_data.get("model", _plan_default_model)),
        provider=_plan_provider,
        budget_usd=float(plan_data.get("budget_usd", _plan_default_budget)),
        timeout=int(plan_data.get("timeout", _plan_default_timeout)),
        timeout_medium=int(plan_timeout_medium_raw)
        if plan_timeout_medium_raw is not None
        else _plan_default_timeout_medium,
        timeout_large=int(plan_timeout_large_raw)
        if plan_timeout_large_raw is not None
        else _plan_default_timeout_large,
        validate_spec=bool(plan_data.get("validate_spec", _plan_default_validate_spec)),
        api_fallback=_derived_plan_profile.api_fallback
        if _derived_plan_profile is not None
        else None,
    )
    if plan_cfg.cli is not None:
        plan_profile = _model_ref_to_profile("plan", plan_cfg.ref)
        plan_profile = _apply_transport_fallback(plan_profile, transport_fallbacks)
        plan_cfg = dataclasses.replace(
            plan_cfg,
            ref=dataclasses.replace(plan_cfg.ref, api_fallback=plan_profile.api_fallback),
        )

    # ── Load-time validation for plan section ────────────────────────────
    if plan_cfg.enabled:
        if plan_cfg.cli is not None and plan_cfg.cli not in SUPPORTED_CLIS:
            raise ValueError(
                f"Unsupported CLI {plan_cfg.cli!r} in plan section. "
                f"Supported: {sorted(SUPPORTED_CLIS)}"
            )
        _validate_plan_provider(plan_cfg, secrets)

    # Plan review
    plan_review_data = raw.get("plan_review", {})
    plan_review_cfg = PlanReviewConfig(
        enabled=bool(plan_review_data.get("enabled", False)),
        mode=str(plan_review_data.get("mode", "blocking")),
        timeout_seconds=int(plan_review_data.get("timeout_seconds", 14400)),
    )

    # Derive the adaptive agent pool from the v0.8 models: list so adaptive
    # assignment (assign_models) has candidates to pick from. When no models:
    # key is configured, the pool is empty and assignment falls back to the
    # static dev/review profiles above.
    agents_list = (
        _agents_from_models(models, budget_usd_val, registry=model_registry) if models else []
    )
    assignment_cfg = _parse_assignment(raw.get("assignment", {}))

    _raw_par = raw.get("plan_agent_review", {})
    if not _raw_par and _derived_par_profile is not None:
        derived_path_prefixes.add("plan_agent_review")
        # v0.8: plan_agent_review was configured via overrides.plan_agent_review;
        # the bridge lowered it to a ModelProfile. Wrap it in PlanAgentReviewConfig.
        plan_agent_review_cfg = PlanAgentReviewConfig(enabled=True, pool=[_derived_par_profile])
    else:
        plan_agent_review_cfg = _parse_plan_agent_review(
            _raw_par,
            secrets,
            plan_cfg,
            agents_list,
            assignment_cfg.enabled,
            _plan_model_is_default,
        )
        # v0.8: if overrides.plan_agent_review provided a derived profile but the
        # explicit plan_agent_review: section didn't specify a pool, inject the
        # derived profile so overrides are never silently dropped.
        if _derived_par_profile is not None and not plan_agent_review_cfg.pool:
            plan_agent_review_cfg = dataclasses.replace(
                plan_agent_review_cfg, pool=[_derived_par_profile]
            )
    if plan_agent_review_cfg.pool:
        plan_agent_review_cfg = dataclasses.replace(
            plan_agent_review_cfg,
            pool=[
                _apply_transport_fallback(profile, transport_fallbacks)
                for profile in plan_agent_review_cfg.pool
            ],
        )
    elif plan_agent_review_cfg.cli is not None:
        legacy_plan_review_profile = ModelProfile(
            name="plan-review",
            cli=plan_agent_review_cfg.cli,
            provider=plan_agent_review_cfg.provider,
            model=plan_agent_review_cfg.model or "sonnet",
            budget_usd=plan_agent_review_cfg.budget_usd,
            timeout_seconds=plan_agent_review_cfg.timeout,
            allowed_tools=(),
            phase=PHASE_PLAN_REVIEW,
        )
        legacy_plan_review_profile = _apply_transport_fallback(
            legacy_plan_review_profile, transport_fallbacks
        )
        plan_agent_review_cfg = dataclasses.replace(
            plan_agent_review_cfg,
            ref=dataclasses.replace(
                plan_agent_review_cfg.ref,
                api_fallback=legacy_plan_review_profile.api_fallback,
            ),
        )

    # Logging
    log_data = raw.get("logging", {})
    log_cfg = LogConfig(
        log_file=str(log_data.get("log_file", LogConfig.log_file)),
        enabled=bool(log_data.get("enabled", True)),
    )

    # Hooks
    hooks_data = raw.get("hooks")
    hooks_cfg: HooksConfig | None = None
    if hooks_data:
        hooks_cfg = HooksConfig(
            post_run=hooks_data.get("post_run"),
            post_merge=hooks_data.get("post_merge"),
            post_sprint=hooks_data.get("post_sprint"),
            pre_run=hooks_data.get("pre_run"),
            timeout_seconds=int(hooks_data.get("timeout_seconds", 30)),
        )

    # ── Assignment reviewer auth cross-check ────────────────────────────
    # When assignment is enabled, every review cycle will select from mid/strong
    # agents (PHASE_TIER["code_review"] in assignment.py maps all complexity levels
    # to "mid" or "strong").  Raise at load time if all reviewer-eligible agents
    # lack auth so the sprint fails fast rather than at the first review cycle.
    #
    # Skip the check when an explicit review_pool is configured: preflight_flow
    # preserves explicit reviewer overrides and bypasses adaptive code-reviewer
    # selection entirely (mirrors the `review_pool not in _explicit_roles` guard
    # in preflight_flow.py).
    _adaptive_reviewers_active = assignment_cfg.enabled and _review_pool_is_default
    if _adaptive_reviewers_active and agents_list:
        _REVIEWER_TIERS = {"mid", "strong"}
        reviewer_candidates = [a for a in agents_list if a.tier in _REVIEWER_TIERS]
        if reviewer_candidates:
            failed_agents: list[tuple[str, str]] = []
            for _agent in reviewer_candidates:
                _ready, _reason = check_agent_auth(_agent.to_model_profile(), secrets)
                if not _ready:
                    failed_agents.append((_agent.name, _reason))
            if len(failed_agents) == len(reviewer_candidates):
                _names = ", ".join(f"{n!r} ({r})" for n, r in failed_agents)
                raise ValueError(
                    f"assignment.enabled is true but no reviewer-eligible agents have auth. "
                    f"Failed: {_names}"
                )

    # Sprint config
    sprint_data = raw.get("sprint", {})
    sprint_max_parallel_raw = sprint_data.get("max_parallel", 1)
    if not isinstance(sprint_max_parallel_raw, int):
        raise ValueError(
            f"forge.yaml 'sprint.max_parallel' must be an integer, got {sprint_max_parallel_raw!r}"
        )
    if sprint_max_parallel_raw < 1:
        raise ValueError(
            f"forge.yaml 'sprint.max_parallel' must be >= 1, got {sprint_max_parallel_raw}"
        )
    sprint_worker_timeout_raw = sprint_data.get("worker_timeout_seconds", 3600)
    if not isinstance(sprint_worker_timeout_raw, int):
        raise ValueError(
            f"forge.yaml 'sprint.worker_timeout_seconds' must be an integer, "
            f"got {sprint_worker_timeout_raw!r}"
        )
    if sprint_worker_timeout_raw < 1:
        raise ValueError(
            f"forge.yaml 'sprint.worker_timeout_seconds' must be >= 1, "
            f"got {sprint_worker_timeout_raw}"
        )
    sprint_batch_data = sprint_data.get("batch", {}) or {}
    if not isinstance(sprint_batch_data, dict):
        raise ValueError(f"forge.yaml 'sprint.batch' must be a mapping, got {sprint_batch_data!r}")
    _batch_defaults = SprintBatchConfig()
    _batch_values: dict[str, int] = {}
    for _key, _default in (
        ("max_stories", _batch_defaults.max_stories),
        ("max_complexity_budget", _batch_defaults.max_complexity_budget),
        ("max_touched_files", _batch_defaults.max_touched_files),
    ):
        _raw_value = sprint_batch_data.get(_key, _default)
        # bool is an int subclass; `max_stories: true` is a config error, not a 1.
        if isinstance(_raw_value, bool) or not isinstance(_raw_value, int):
            raise ValueError(
                f"forge.yaml 'sprint.batch.{_key}' must be an integer, got {_raw_value!r}"
            )
        if _raw_value < 1:
            raise ValueError(f"forge.yaml 'sprint.batch.{_key}' must be >= 1, got {_raw_value}")
        _batch_values[_key] = _raw_value
    sprint_post_triage_raw = sprint_data.get("post_sprint_triage", False)
    if not isinstance(sprint_post_triage_raw, bool):
        raise ValueError(
            f"forge.yaml 'sprint.post_sprint_triage' must be a boolean, "
            f"got {sprint_post_triage_raw!r}"
        )
    sprint_cfg = SprintConfig(
        max_parallel=sprint_max_parallel_raw,
        worker_timeout_seconds=sprint_worker_timeout_raw,
        batch=SprintBatchConfig(**_batch_values),
        post_sprint_triage=sprint_post_triage_raw,
    )

    shape_check_data = raw.get("shape_check", {}) or {}
    if not isinstance(shape_check_data, dict):
        raise ValueError(f"forge.yaml 'shape_check' must be a mapping, got {shape_check_data!r}")
    shape_check_classifier = shape_check_data.get("classifier", "heuristic")
    if not isinstance(shape_check_classifier, str) or not shape_check_classifier.strip():
        raise ValueError(
            "forge.yaml 'shape_check.classifier' must be a non-empty string, "
            f"got {shape_check_classifier!r}"
        )
    shape_check_threshold = shape_check_data.get("stuck_issue_threshold", 3)
    if isinstance(shape_check_threshold, bool) or not isinstance(shape_check_threshold, int):
        raise ValueError(
            "forge.yaml 'shape_check.stuck_issue_threshold' must be an integer, "
            f"got {shape_check_threshold!r}"
        )
    if shape_check_threshold < 1:
        raise ValueError(
            "forge.yaml 'shape_check.stuck_issue_threshold' must be >= 1, "
            f"got {shape_check_threshold!r}"
        )
    shape_check_cfg = ShapeCheckConfig(
        classifier=shape_check_classifier.strip(),
        stuck_issue_threshold=shape_check_threshold,
    )

    intake_data = raw.get("intake", {}) or {}
    if not isinstance(intake_data, dict):
        raise ValueError(f"forge.yaml 'intake' must be a mapping, got {intake_data!r}")
    intake_grooming = intake_data.get("grooming", False)
    intake_auto_fix = intake_data.get("auto_fix", False)
    intake_auto_fix_mode = intake_data.get("auto_fix_mode", "comment")
    if not isinstance(intake_grooming, bool):
        raise ValueError(f"forge.yaml 'intake.grooming' must be a bool, got {intake_grooming!r}")
    if not isinstance(intake_auto_fix, bool):
        raise ValueError(f"forge.yaml 'intake.auto_fix' must be a bool, got {intake_auto_fix!r}")
    if intake_auto_fix_mode not in {"comment", "edit"}:
        raise ValueError(
            "forge.yaml 'intake.auto_fix_mode' must be 'comment' or 'edit',"
            f" got {intake_auto_fix_mode!r}"
        )
    intake_cfg = IntakeConfig(
        grooming=intake_grooming,
        auto_fix=intake_auto_fix,
        auto_fix_mode=intake_auto_fix_mode,
    )

    knowledge_data = raw.get("knowledge", {}) or {}
    if not isinstance(knowledge_data, dict):
        raise ValueError(f"forge.yaml 'knowledge' must be a mapping, got {knowledge_data!r}")
    _knowledge_values: dict[str, bool] = {}
    for _key in ("run_summaries", "prior_run_context", "invariant_context"):
        _value = knowledge_data.get(_key, getattr(KnowledgeConfig, _key))
        if not isinstance(_value, bool):
            raise ValueError(f"forge.yaml 'knowledge.{_key}' must be a bool, got {_value!r}")
        _knowledge_values[_key] = _value
    # Source globs are a list, not a gate, so they are parsed off the bool loop.
    _invariant_sources_raw = knowledge_data.get(
        "invariant_sources", list(KnowledgeConfig.invariant_sources)
    )
    if not isinstance(_invariant_sources_raw, list):
        raise ValueError(
            "forge.yaml 'knowledge.invariant_sources' must be a list of glob strings,"
            f" got {_invariant_sources_raw!r}"
        )
    for _glob in _invariant_sources_raw:
        if not isinstance(_glob, str) or not _glob.strip():
            raise ValueError(
                "forge.yaml 'knowledge.invariant_sources' items must be non-empty strings,"
                f" got {_glob!r}"
            )
    _knowledge_ref_raw = knowledge_data.get("ref")
    _knowledge_ref: ModelRef | None = None
    if _knowledge_ref_raw is not None:
        if not isinstance(_knowledge_ref_raw, dict):
            raise ValueError(
                "forge.yaml 'knowledge.ref' must be a mapping when present, "
                f"got {_knowledge_ref_raw!r}"
            )
        if "cli" in _knowledge_ref_raw:
            raise ValueError(
                "forge.yaml 'knowledge.ref' must dispatch over API transport; "
                "remove 'cli' and set 'provider' instead"
            )
        _provider = _knowledge_ref_raw.get("provider")
        if not isinstance(_provider, str) or not _provider.strip():
            raise ValueError(
                "forge.yaml 'knowledge.ref.provider' must be a non-empty string when "
                "knowledge.ref is configured"
            )
        if _provider.strip() not in SUPPORTED_PROVIDERS:
            raise ValueError(
                f"Unsupported provider {_provider!r} in knowledge.ref. "
                f"Supported: {sorted(SUPPORTED_PROVIDERS)}"
            )
        _model = _knowledge_ref_raw.get("model")
        if not isinstance(_model, str) or not _model.strip():
            raise ValueError(
                "forge.yaml 'knowledge.ref.model' must be a non-empty string when "
                "knowledge.ref is configured"
            )
        _fallback_models_raw = _knowledge_ref_raw.get("fallback_models", ())
        if not isinstance(_fallback_models_raw, (list, tuple)):
            raise ValueError(
                "forge.yaml 'knowledge.ref.fallback_models' must be a list of strings, "
                f"got {_fallback_models_raw!r}"
            )
        for _fallback_model in _fallback_models_raw:
            if not isinstance(_fallback_model, str) or not _fallback_model.strip():
                raise ValueError(
                    "forge.yaml 'knowledge.ref.fallback_models' items must be non-empty "
                    f"strings, got {_fallback_model!r}"
                )
        if "transport" in _knowledge_ref_raw:
            _transport_raw = _knowledge_ref_raw["transport"]
            if not isinstance(_transport_raw, dict):
                raise ValueError(
                    "forge.yaml 'knowledge.ref.transport' must be a mapping, "
                    f"got {_transport_raw!r}"
                )
            _transport_kind = str(_transport_raw.get("kind", "")).strip().lower()
            if _transport_kind and _transport_kind != "api":
                raise ValueError(
                    "forge.yaml 'knowledge.ref.transport.kind' must be 'api' for "
                    "knowledge summaries"
                )
        _knowledge_ref = ModelRef(
            provider=_provider.strip(),
            model=_model.strip(),
            budget_usd=float(_knowledge_ref_raw.get("budget_usd", 0.50)),
            timeout_seconds=int(_knowledge_ref_raw.get("timeout_seconds", 300)),
            fallback_models=tuple(
                _fallback_model.strip() for _fallback_model in _fallback_models_raw
            ),
            reasoning_effort=(
                str(_knowledge_ref_raw["reasoning_effort"]).strip()
                if _knowledge_ref_raw.get("reasoning_effort") is not None
                else None
            ),
            thinking_budget=(
                int(_knowledge_ref_raw["thinking_budget"])
                if _knowledge_ref_raw.get("thinking_budget") is not None
                else None
            ),
            base_url=(
                str(_knowledge_ref_raw["base_url"]).strip()
                if _knowledge_ref_raw.get("base_url") is not None
                else None
            ),
            max_iterations=(
                int(_knowledge_ref_raw["max_iterations"])
                if _knowledge_ref_raw.get("max_iterations") is not None
                else None
            ),
            max_tool_output_bytes=int(
                _knowledge_ref_raw.get("max_tool_output_bytes", ModelRef.max_tool_output_bytes)
            ),
        )
    knowledge_cfg = KnowledgeConfig(
        **_knowledge_values,
        ref=_knowledge_ref,
        invariant_sources=tuple(_glob.strip() for _glob in _invariant_sources_raw),
    )

    context_data = raw.get("context", {})
    context_cfg = ContextConfig(
        preflight_budget=int(context_data.get("preflight_budget", ContextConfig.preflight_budget)),
        plan_budget=int(context_data.get("plan_budget", ContextConfig.plan_budget)),
        dev_budget=int(context_data.get("dev_budget", ContextConfig.dev_budget)),
        review_budget=int(context_data.get("review_budget", ContextConfig.review_budget)),
    )

    conventions_raw = raw.get("conventions", {})

    # Conventions config — soft
    conventions_soft_raw = conventions_raw.get("soft", [])
    if not isinstance(conventions_soft_raw, list):
        raise ValueError(
            "forge.yaml 'conventions.soft' must be a list of strings,"
            f" got {conventions_soft_raw!r}"
        )
    for _item in conventions_soft_raw:
        if not isinstance(_item, str):
            raise ValueError(f"forge.yaml 'conventions.soft' items must be strings, got {_item!r}")
    conventions_soft_list: list[str] = conventions_soft_raw

    # Conventions config — hard
    conventions_hard_raw = conventions_raw.get("hard", None)
    if conventions_hard_raw is None:
        conventions_hard_cfg: HardConventionsConfig | None = None
    else:
        _max_module = conventions_hard_raw.get("max_module_lines", 500)
        _max_test = conventions_hard_raw.get("max_test_file_lines", 1000)
        _no_circular = conventions_hard_raw.get("no_circular_imports", True)
        _test_mirrors = conventions_hard_raw.get("test_mirrors_source", True)
        _no_scratch = conventions_hard_raw.get("no_scratch_files", True)
        _stack = conventions_hard_raw.get("stack", [])
        _allowed_root_files = conventions_hard_raw.get("allowed_root_files", [])
        _package_roots = conventions_hard_raw.get("package_roots", [])
        if not isinstance(_max_module, int):
            raise ValueError(
                "forge.yaml 'conventions.hard.max_module_lines' must be an int,"
                f" got {_max_module!r}"
            )
        if not isinstance(_max_test, int):
            raise ValueError(
                "forge.yaml 'conventions.hard.max_test_file_lines' must be an int,"
                f" got {_max_test!r}"
            )
        if not isinstance(_no_circular, bool):
            raise ValueError(
                "forge.yaml 'conventions.hard.no_circular_imports' must be a bool,"
                f" got {_no_circular!r}"
            )
        if not isinstance(_test_mirrors, bool):
            raise ValueError(
                "forge.yaml 'conventions.hard.test_mirrors_source' must be a bool,"
                f" got {_test_mirrors!r}"
            )
        if not isinstance(_no_scratch, bool):
            raise ValueError(
                "forge.yaml 'conventions.hard.no_scratch_files' must be a bool,"
                f" got {_no_scratch!r}"
            )
        if isinstance(_stack, str):
            _stack_items = [_stack]
        elif isinstance(_stack, list) and all(isinstance(item, str) for item in _stack):
            _stack_items = _stack
        else:
            raise ValueError(
                "forge.yaml 'conventions.hard.stack' must be a string or list of strings,"
                f" got {_stack!r}"
            )
        if not isinstance(_allowed_root_files, list) or not all(
            isinstance(item, str) for item in _allowed_root_files
        ):
            raise ValueError(
                "forge.yaml 'conventions.hard.allowed_root_files' must be a list of strings,"
                f" got {_allowed_root_files!r}"
            )
        if not isinstance(_package_roots, list) or not all(
            isinstance(item, str) for item in _package_roots
        ):
            raise ValueError(
                "forge.yaml 'conventions.hard.package_roots' must be a list of strings,"
                f" got {_package_roots!r}"
            )
        for _root in _package_roots:
            if not (project_root / _root).exists():
                log.warning(
                    "forge.yaml 'conventions.hard.package_roots' entry %r does not exist "
                    "under the project root — its checks will find nothing",
                    _root,
                )
        conventions_hard_cfg = HardConventionsConfig(
            max_module_lines=_max_module,
            max_test_file_lines=_max_test,
            no_circular_imports=_no_circular,
            test_mirrors_source=_test_mirrors,
            no_scratch_files=_no_scratch,
            stack=normalize_root_file_stacks(_stack_items),
            allowed_root_files=tuple(_allowed_root_files),
            package_roots=tuple(_package_roots),
        )

    conventions_advisory_raw = conventions_raw.get("advisory", {})
    if not isinstance(conventions_advisory_raw, dict):
        raise ValueError(
            "forge.yaml 'conventions.advisory' must be a mapping,"
            f" got {conventions_advisory_raw!r}"
        )

    _artifact_path = conventions_advisory_raw.get(
        "artifact_path", AdvisoryConventionsConfig.artifact_path
    )
    _summary_top_n = conventions_advisory_raw.get(
        "summary_top_n", AdvisoryConventionsConfig.summary_top_n
    )
    _noteworthy_threshold = conventions_advisory_raw.get(
        "noteworthy_threshold_percent", AdvisoryConventionsConfig.noteworthy_threshold_percent
    )
    _commit_shared = conventions_advisory_raw.get(
        "commit_shared_artifact", AdvisoryConventionsConfig.commit_shared_artifact
    )
    _shared_artifact_path = conventions_advisory_raw.get(
        "shared_artifact_path", AdvisoryConventionsConfig.shared_artifact_path
    )
    _issue_filing_raw = conventions_advisory_raw.get("issue_filing", {})

    if not isinstance(_artifact_path, str) or not _artifact_path.strip():
        raise ValueError(
            "forge.yaml 'conventions.advisory.artifact_path' must be a non-empty string,"
            f" got {_artifact_path!r}"
        )
    if not isinstance(_summary_top_n, int) or _summary_top_n < 1:
        raise ValueError(
            "forge.yaml 'conventions.advisory.summary_top_n' must be an int >= 1,"
            f" got {_summary_top_n!r}"
        )
    if not isinstance(_noteworthy_threshold, (int, float)) or _noteworthy_threshold < 0:
        raise ValueError(
            "forge.yaml 'conventions.advisory.noteworthy_threshold_percent' must be a number >= 0,"
            f" got {_noteworthy_threshold!r}"
        )
    if not isinstance(_commit_shared, bool):
        raise ValueError(
            "forge.yaml 'conventions.advisory.commit_shared_artifact' must be a bool,"
            f" got {_commit_shared!r}"
        )
    if _shared_artifact_path is not None and (
        not isinstance(_shared_artifact_path, str) or not _shared_artifact_path.strip()
    ):
        raise ValueError(
            "forge.yaml 'conventions.advisory.shared_artifact_path' must be "
            "a non-empty string or null,"
            f" got {_shared_artifact_path!r}"
        )
    if _commit_shared and _shared_artifact_path is None:
        raise ValueError(
            "forge.yaml 'conventions.advisory.shared_artifact_path' must be set when "
            "'commit_shared_artifact' is true"
        )
    if not isinstance(_issue_filing_raw, dict):
        raise ValueError(
            "forge.yaml 'conventions.advisory.issue_filing' must be a mapping,"
            f" got {_issue_filing_raw!r}"
        )

    _issue_filing_enabled = _issue_filing_raw.get("enabled", AdvisoryIssueFilingConfig.enabled)
    _issue_filing_threshold = _issue_filing_raw.get(
        "threshold_percent", AdvisoryIssueFilingConfig.threshold_percent
    )
    _issue_filing_label = _issue_filing_raw.get("label", AdvisoryIssueFilingConfig.label)
    _issue_filing_milestone = _issue_filing_raw.get(
        "milestone", AdvisoryIssueFilingConfig.milestone
    )

    if not isinstance(_issue_filing_enabled, bool):
        raise ValueError(
            "forge.yaml 'conventions.advisory.issue_filing.enabled' must be a bool,"
            f" got {_issue_filing_enabled!r}"
        )
    if not isinstance(_issue_filing_threshold, (int, float)) or _issue_filing_threshold < 0:
        raise ValueError(
            "forge.yaml 'conventions.advisory.issue_filing.threshold_percent'"
            f" must be a number >= 0, got {_issue_filing_threshold!r}"
        )
    if not isinstance(_issue_filing_label, str) or not _issue_filing_label.strip():
        raise ValueError(
            "forge.yaml 'conventions.advisory.issue_filing.label' must be a non-empty string,"
            f" got {_issue_filing_label!r}"
        )
    if _issue_filing_milestone is not None and not isinstance(_issue_filing_milestone, str):
        raise ValueError(
            "forge.yaml 'conventions.advisory.issue_filing.milestone' must be a string or null,"
            f" got {_issue_filing_milestone!r}"
        )

    conventions_advisory_cfg = AdvisoryConventionsConfig(
        artifact_path=_artifact_path,
        summary_top_n=_summary_top_n,
        noteworthy_threshold_percent=float(_noteworthy_threshold),
        commit_shared_artifact=_commit_shared,
        shared_artifact_path=(
            _shared_artifact_path.strip() if isinstance(_shared_artifact_path, str) else None
        ),
        issue_filing=AdvisoryIssueFilingConfig(
            enabled=_issue_filing_enabled,
            threshold_percent=float(_issue_filing_threshold),
            label=_issue_filing_label,
            milestone=_issue_filing_milestone,
        ),
    )

    _fc_raw = raw.get("finding_classifier", {})
    _allow_bypass = _fc_raw.get("allow_net_new_bypass", False)
    if not isinstance(_allow_bypass, bool):
        raise ValueError(
            "forge.yaml 'finding_classifier.allow_net_new_bypass' must be a bool,"
            f" got {_allow_bypass!r}"
        )
    finding_classifier_cfg = FindingClassifierConfig(allow_net_new_bypass=_allow_bypass)

    stuck_detection_cfg = _parse_stuck_detection(raw.get("stuck_detection", {}))

    diagnose_cfg = _parse_diagnose_config(raw.get("diagnose", {}))

    config = ForgeConfig(
        project=raw.get("project", project_root.name),
        project_root=project_root,
        workspace=workspace,
        validation=validation,
        dev_profile=dev_profile,
        preflight_profile=preflight_profile,
        preflight_fallback_profile=preflight_fallback_profile,
        review_pool=review_pool,
        synthesis_profile=synthesis_profile,
        retry=retry,
        notifications=notifications,
        github=github_cfg,
        models=models,
        plan=plan_cfg,
        plan_review=plan_review_cfg,
        plan_agent_review=plan_agent_review_cfg,
        log=log_cfg,
        hooks=hooks_cfg,
        dev=dev_cfg,
        sandbox=sandbox_cfg,
        sprint=sprint_cfg,
        shape_check=shape_check_cfg,
        intake=intake_cfg,
        context=context_cfg,
        knowledge=knowledge_cfg,
        secrets=secrets,
        agents=agents_list,
        assignment=assignment_cfg,
        transport_fallbacks=transport_fallbacks,
        auto_transport_fallback=auto_transport_fallback,
        review_pool_is_default=_review_pool_is_default,
        plan_model_is_default=_plan_model_is_default,
        dev_profile_is_default=_dev_profile_is_default,
        conventions_hard=conventions_hard_cfg,
        conventions_soft=conventions_soft_list,
        conventions_advisory=conventions_advisory_cfg,
        finding_classifier=finding_classifier_cfg,
        stuck_detection=stuck_detection_cfg,
        models_budget_usd=budget_usd_val,
        models_overrides=_raw_overrides,
        model_registry=model_registry,
        model_registry_sources=model_registry_sources,
        model_registry_field_sources=model_registry_field_sources,
        model_registry_duplicates=model_registry_duplicates,
        custom_models=custom_models,
        diagnose=diagnose_cfg,
    )
    # Configuration identity is derived here, once, from the fully-resolved
    # config (#2056) — consumers record what the run executed under instead of
    # each re-deriving it (or, as before, recording nothing at all).
    config = dataclasses.replace(
        config,
        provenance=build_provenance(
            config,
            config_path,
            yaml_leaf_paths=yaml_leaf_paths,
            environment_sources=notification_environment_sources,
            derived_path_prefixes=tuple(sorted(derived_path_prefixes)),
        ),
    )
    # Pricing is resolved ONCE, here, from the same merged registry routing reads
    # its figures from, into a process-level registry every accounting site
    # consults by the identity that actually dispatched (#2335). Installed only
    # after the ForgeConfig is fully constructed, so a load that raises during
    # validation never leaves partial rates active; last install wins.
    from .dispatch_rates import install_rate_registry  # noqa: PLC0415

    install_rate_registry(config)
    return config
