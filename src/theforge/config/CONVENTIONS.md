# Config subsystem guidance

## Purpose

The config subsystem loads, validates, and normalizes TheForge configuration,
including defaults, profiles, model selection data, authentication settings,
and secrets handling.

## Invariants

- Configuration loading is an integrity boundary. Prefer explicit validation
  errors over permissive fallback behavior that hides bad config.
- Keep secrets handling narrow and deliberate; do not log secret values or make
  them easier to leak through convenience helpers.
- Preserve clear separation between raw loading, defaults application, and typed
  normalization so configuration behavior stays understandable.
- Foundational config types should remain low-dependency and broadly reusable.
- Changes to model/profile configuration must not quietly undermine coordinator
  assumptions about assignment or provider capabilities.

## Context

- `load.py` and `_loaders.py` are the entry points for reading configuration from
  disk and environment.
- `defaults.py`, `profiles.py`, and `models.py` define how omitted values are
  filled and how model/provider capabilities are represented.
- Model definitions are layered so the shipped set and project-declared models
  share one schema: `model_identity.py` (leaf — canonical identity, transport,
  routing policy, `AgentSpec`) ← `model_catalog.py` (the one parser, plus the
  packaged `data/models.yaml` catalog) ← `models.py` (`AGENT_REGISTRY`, legacy
  views, lookups) ← `load.py`. Keep that direction: a back-edge would put the
  parser and the registry it builds in an import cycle.
- `types.py` contains typed config structures used throughout the codebase.
- `auth.py` and `secrets.py` are the main places to inspect when credentials or
  provider authentication behavior changes.
- Misconfigurations often surface far from their source, so preserving precise
  validation messages here saves debugging time elsewhere.
