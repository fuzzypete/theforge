"""Model identity projected out of audit ``cost.agents`` entries.

This module owns *one* reading of the agent-entry identity contract. The
writer is :func:`theforge.coordinator.audit_render._agent_entry`; the readers
are the ``audit_substrate`` derivations that project a run record to the model
that ran (indexed ``dev_model``, escalation history, assignment history).

Issue #2201: those readers had each been written against ``provider`` /
``model`` / ``cli`` / ``name`` keys the renderer never emits, so every
derivation returned nothing on live records. Keeping the read in one place
means a future renderer/reader divergence breaks one seam-pinned helper rather
than silently emptying three projections.

Provenance matters as much as the value. ``model_used`` is the identity the
runner recorded at invocation time (:data:`SOURCE_DIRECT`). Anything
reconstructed afterwards — an invocation's configured preference list, or the
legacy identity keys older records carry — is a best-effort recovery
(:data:`SOURCE_RECOVERED`) and consumers must be able to tell the two apart.

Per-component ``model_usage`` billing is deliberately *not* a source: a model
billed inside an invocation is not the identity of the invocation.

Stdlib only, plus a lazy, failure-tolerant hop to ``model_profiles`` for
canonicalization.
"""

from __future__ import annotations

SOURCE_DIRECT = "direct"
"""Identity was recorded by the runner at invocation time (``model_used``)."""

SOURCE_RECOVERED = "recovered"
"""Identity was reconstructed after the fact (config block / legacy keys)."""


def _canonicalize(raw: str) -> str:
    """Return the canonical ``provider/model/transport`` id for ``raw``.

    Falls back to ``raw`` unchanged when the key cannot be resolved
    unambiguously — an unresolvable identity is still a truthful record of
    what ran, and guessing at it would be worse than reporting it verbatim.
    """
    try:
        from theforge.model_profiles import canonical_id_for_legacy_key  # noqa: PLC0415

        return canonical_id_for_legacy_key(raw) or raw
    except Exception:  # noqa: BLE001 - identity projection must never break indexing
        return raw


def is_dev_entry(entry: object) -> bool:
    """Return True when this ``cost.agents`` entry describes the dev agent.

    ``role`` is what :func:`audit_render._agent_entry` writes; ``phase`` is the
    key older records used.
    """
    if not isinstance(entry, dict):
        return False
    return entry.get("role") == "dev" or entry.get("phase") == "dev"


def entry_model_identity(entry: object) -> tuple[str, str] | None:
    """Return ``(identity, source)`` for one ``cost.agents`` entry, or None.

    Resolution order:
      1. ``model_used`` — the model the runner reports having invoked
         (:data:`SOURCE_DIRECT`).
      2. ``model_config`` — the configured preference list for the invocation.
         Which entry actually served is not recorded, so the preferred (first)
         entry is reported as a :data:`SOURCE_RECOVERED` reconstruction.
      3. Legacy ``provider``/``model``/``cli`` or ``name`` keys, kept only as
         compatibility for records written before the current renderer
         (:data:`SOURCE_RECOVERED`).
    """
    if not isinstance(entry, dict):
        return None

    model_used = str(entry.get("model_used") or "").strip()
    if model_used:
        return (_canonicalize(model_used), SOURCE_DIRECT)

    model_config = entry.get("model_config")
    if isinstance(model_config, list):
        for candidate in model_config:
            configured = str(candidate or "").strip()
            if configured:
                return (_canonicalize(configured), SOURCE_RECOVERED)

    provider = str(entry.get("provider") or "").strip()
    model = str(entry.get("model") or "").strip()
    cli = str(entry.get("cli") or "").strip()
    if provider and model:
        return (f"{provider}/{model}/{'cli' if cli else 'api'}", SOURCE_RECOVERED)
    name = str(entry.get("name") or "").strip()
    if name:
        return (name, SOURCE_RECOVERED)
    return None


def dev_model_identity(record: object) -> tuple[str | None, str | None]:
    """Return ``(identity, source)`` for the dev agent of an audit record.

    A directly recorded identity wins over a reconstructed one regardless of
    entry order, so a run whose later dev attempt recorded ``model_used`` is
    not reported as recovered. Returns ``(None, None)`` when no dev-role entry
    carries a trustworthy invocation identity.
    """
    if not isinstance(record, dict):
        return (None, None)
    cost_block = record.get("cost")
    if not isinstance(cost_block, dict):
        return (None, None)
    agents = cost_block.get("agents")
    if not isinstance(agents, list):
        return (None, None)

    recovered: tuple[str, str] | None = None
    for entry in agents:
        if not is_dev_entry(entry):
            continue
        resolved = entry_model_identity(entry)
        if resolved is None:
            continue
        if resolved[1] == SOURCE_DIRECT:
            return resolved
        if recovered is None:
            recovered = resolved
    if recovered is not None:
        return recovered
    return (None, None)
