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

Per-component ``model_usage`` billing is deliberately *not* a source for the
*invocation's* identity: a model billed inside an invocation is not the identity
of the invocation. Since #2205 those components are recorded as their own
first-class identities in the entry's ``ledger.billed_components``, readable via
:func:`entry_identity_ledger`, so "not the invocation identity" no longer means
"discarded".

Issue #2205 also ends the one-identity-per-invocation collapse this module was
built around. An entry written by the current renderer carries a ``ledger``
block holding the configured identity, the resolved primary identity, and every
billed component as separate values; :func:`entry_identity_ledger` reads it and
reports ``full_ledger`` so a consumer can tell a record that carries the whole
set from one that does not. The single-identity projections below remain, now
defined as *the resolved primary identity* — which is what they were always
trying to approximate — and keep their legacy reconstruction path for records
written before the ledger existed.

Stdlib only, plus a lazy, failure-tolerant hop to ``model_profiles`` for
canonicalization.
"""

from __future__ import annotations

SOURCE_DIRECT = "direct"
"""Identity was recorded by the runner at invocation time (``model_used``)."""

SOURCE_RECOVERED = "recovered"
"""Identity was reconstructed after the fact (config block / legacy keys)."""

RESOLUTION_CANONICAL = "canonical"
"""Identity is in the canonical ``provider/model/transport`` namespace."""

RESOLUTION_UNRESOLVED = "unresolved"
"""Identity is the runner's verbatim spelling — no canonical id resolved."""


def _canonicalize(raw: str, entry: object = None) -> tuple[str, str]:
    """Return ``(identity, resolution)`` for the raw identity ``raw``.

    Falls back to ``raw`` unchanged when the key cannot be resolved
    unambiguously — an unresolvable identity is still a truthful record of
    what ran, and guessing at it would be worse than reporting it verbatim.
    The returned resolution status is what keeps that fallback *visible*: a
    consumer grouping by identity can tell one namespace from a mixture
    (#2225), which a bare value in the same field cannot express.

    ``entry`` is the ``cost.agents`` entry the identity was read from, passed
    through as a hint source: its ``transport_used`` disambiguates bare model
    names the registry offers over both CLI and API.
    """
    try:
        from theforge.model_profiles import canonical_id_for_legacy_key  # noqa: PLC0415

        resolved = canonical_id_for_legacy_key(raw, entry if isinstance(entry, dict) else None)
    except Exception:  # noqa: BLE001 - identity projection must never break indexing
        return (raw, RESOLUTION_UNRESOLVED)
    if resolved:
        return (resolved, RESOLUTION_CANONICAL)
    return (raw, RESOLUTION_UNRESOLVED)


def canonicalize_identity(raw: str, entry: object = None) -> tuple[str, str]:
    """Public alias of :func:`_canonicalize` for the writer side.

    ``audit_render`` canonicalizes as it writes the ledger so the record carries
    both the raw spelling and its canonical projection. Sharing this one helper
    is what keeps the writer's canonical id and the reader's from drifting into
    two namespaces that look the same.
    """
    return _canonicalize(raw, entry)


def _identity_detail(block: object) -> tuple[str, str, str] | None:
    """Project one ledger identity block to ``(identity, source, resolution)``.

    Ledger identities are :data:`SOURCE_DIRECT`: the runner recorded them at
    invocation time, which is exactly what this module means by direct. The
    stored canonical projection is preferred, but it is re-derived when the
    block carries only a raw spelling, so an entry written by an older ledger
    version still reads.
    """
    if not isinstance(block, dict):
        return None
    raw = str(block.get("raw") or "").strip()
    identity = str(block.get("identity") or "").strip()
    resolution = str(block.get("resolution") or "").strip()
    if identity and resolution in (RESOLUTION_CANONICAL, RESOLUTION_UNRESOLVED):
        return (identity, SOURCE_DIRECT, resolution)
    if not raw:
        return None
    transport = block.get("transport")
    hint = {"transport_used": transport} if transport else None
    resolved, derived_resolution = _canonicalize(raw, hint)
    return (resolved, SOURCE_DIRECT, derived_resolution)


def entry_identity_ledger(entry: object) -> dict | None:
    """Return the invocation ledger projection for one entry, or None (#2205).

    The returned mapping is::

        {
          "full_ledger": bool,   # entry carries the complete ledger block
          "version": int | None,
          "configured": (identity, source, resolution) | None,
          "resolved": (identity, source, resolution) | None,
          "differs": bool | None,
          "billed": [ (identity, source, resolution), ... ],
          "cost_usd": float | None,
          "cost_provenance": str | None,
          "reasoning_effort": str | None,
          "complexity": str | None,
          "complexity_score": int | None,
          "role": str | None,
        }

    ``full_ledger`` is the marker the "records written before this remain
    readable" contract turns on: it is True only when the entry carries a
    ``ledger`` block, so a consumer can tell a record that names configured and
    resolved separately from one that never could. Returns ``None`` for an entry
    that is not a mapping at all.

    Legacy entries are not faked up into a ledger. They get ``full_ledger:
    False`` and whatever the single-identity reconstruction below can recover,
    reported as ``resolved`` — because that is the identity the pre-ledger field
    was approximating — with ``configured`` left null rather than guessed.
    """
    if not isinstance(entry, dict):
        return None
    ledger = entry.get("ledger")
    if isinstance(ledger, dict):
        version = ledger.get("version")
        billed: list[tuple[str, str, str]] = []
        raw_components = ledger.get("billed_components")
        if isinstance(raw_components, list):
            for component in raw_components:
                if not isinstance(component, dict):
                    continue
                detail = _identity_detail(component.get("identity"))
                if detail is not None:
                    billed.append(detail)
        differs = ledger.get("configured_differs_from_resolved")
        return {
            "full_ledger": True,
            "version": version if isinstance(version, int) else None,
            "configured": _identity_detail(ledger.get("configured_identity")),
            "resolved": _identity_detail(ledger.get("resolved_primary_identity")),
            "differs": differs if isinstance(differs, bool) else None,
            "billed": billed,
            "cost_usd": ledger.get("cost_usd"),
            "cost_provenance": ledger.get("cost_provenance"),
            "reasoning_effort": ledger.get("reasoning_effort"),
            "complexity": ledger.get("complexity"),
            "complexity_score": ledger.get("complexity_score"),
            "role": ledger.get("role"),
        }
    return {
        "full_ledger": False,
        "version": None,
        "configured": None,
        "resolved": _legacy_entry_identity_detail(entry),
        "differs": None,
        "billed": [],
        "cost_usd": entry.get("cost_usd"),
        "cost_provenance": None,
        "reasoning_effort": None,
        "complexity": None,
        "complexity_score": None,
        "role": entry.get("role") or entry.get("phase"),
    }


def resolved_identity_for_result(result: object) -> str | None:
    """Return the identity an ``AgentResult`` actually served, or ``None``.

    The live-object counterpart of ``ledger.resolved_primary_identity``: the
    audit renderer reads the same two fields off the same object, so the value a
    profile fold records and the value the audit record carries are the same
    string. Duck-typed rather than importing ``AgentResult`` — this module is the
    identity leaf and must stay importable from anywhere.

    Returns the canonical id when the served spelling resolves onto one, and the
    verbatim spelling when it does not. Reporting a served version the catalog
    has not pinned is strictly better than reporting nothing, and the
    resolution-status distinction is preserved wherever it matters (the ledger,
    the substrate index) rather than being re-derived here.
    """
    model_used = str(getattr(result, "model_used", None) or "").strip()
    if not model_used:
        usage = getattr(result, "model_usage", None)
        if isinstance(usage, list) and usage:
            model_used = str(getattr(usage[-1], "model", None) or "").strip()
    if not model_used:
        return None
    transport = str(getattr(result, "transport_used", None) or "").strip() or None
    identity, _resolution = canonicalize_identity(
        model_used, {"transport_used": transport} if transport else None
    )
    return identity


def _record_ledgers(record: object) -> list[tuple[str, int, dict]]:
    """Return ``(source, position, entry)`` for every recorded invocation.

    Two sources, because "every recorded invocation" is genuinely two places:

    * ``cost.agents`` — the per-agent breakdown covering every phase that
      reached a completed invocation (preflight, plan, plan_review, dev, review,
      synthesis).
    * ``preflight.attempts`` — the earlier preflight parse-retry and fallback
      attempts. Only the *final* preflight attempt reaches ``cost.agents``, so an
      attempt that ran, resolved to a concrete version and was then superseded
      exists nowhere else (see ``preflight_flow._record_attempt``).

    Restricting to ``cost.agents`` would make the index quietly incomplete for
    exactly the retry path where a fallback model — a different identity from
    the configured one — is most likely to have served.
    """
    if not isinstance(record, dict):
        return []
    found: list[tuple[str, int, dict]] = []
    cost_block = record.get("cost")
    if isinstance(cost_block, dict):
        agents = cost_block.get("agents")
        if isinstance(agents, list):
            for position, entry in enumerate(agents):
                if isinstance(entry, dict):
                    found.append(("cost.agents", position, entry))
    preflight = record.get("preflight")
    if isinstance(preflight, dict):
        attempts = preflight.get("attempts")
        if isinstance(attempts, list):
            for position, attempt in enumerate(attempts):
                if isinstance(attempt, dict):
                    found.append(("preflight.attempts", position, attempt))
    return found


def invocation_identity_rows(record: object) -> list[dict]:
    """Project a run record to one identity row per recorded invocation (#2226).

    Role-neutral by construction: the dev-only ``dev_*`` projection above
    answers "what ran the dev phase", which cannot answer "what did this alias
    resolve to across the run" for any other phase. Each row is::

        {
          "source": "cost.agents" | "preflight.attempts",
          "position": int,          # index within that source, for stable ordering
          "role": str | None,
          "profile": str | None,
          "configured": (identity, source, resolution) | None,
          "resolved": (identity, source, resolution) | None,
          "differs": bool | None,
          "full_ledger": bool,
          "version": int | None,
        }

    A pre-ledger entry still produces a row: its reconstructed single identity is
    reported as ``resolved`` with ``configured`` null and ``full_ledger`` False,
    exactly as :func:`entry_identity_ledger` defines it. Entries carrying no
    recoverable identity at all are dropped — a row naming neither side records
    nothing.
    """
    rows: list[dict] = []
    for source, position, entry in _record_ledgers(record):
        if source == "preflight.attempts":
            # The attempt's own ledger, when it recorded one. An attempt without
            # a readable ledger has only the collapsed profile/model pair, which
            # is not an invocation identity — skip rather than fabricate.
            ledger = entry.get("ledger")
            if not isinstance(ledger, dict):
                continue
            projection = entry_identity_ledger({"ledger": ledger})
        else:
            projection = entry_identity_ledger(entry)
        if projection is None:
            continue
        if projection["configured"] is None and projection["resolved"] is None:
            continue
        role = projection["role"]
        if not role:
            role = entry.get("role") or entry.get("phase")
            if source == "preflight.attempts" and not role:
                role = "preflight"
        profile = entry.get("profile") or entry.get("profile_name")
        rows.append(
            {
                "source": source,
                "position": position,
                "role": str(role) if role else None,
                "profile": str(profile) if profile else None,
                "configured": projection["configured"],
                "resolved": projection["resolved"],
                "differs": projection["differs"],
                "full_ledger": projection["full_ledger"],
                "version": projection["version"],
            }
        )
    return rows


def is_dev_entry(entry: object) -> bool:
    """Return True when this ``cost.agents`` entry describes the dev agent.

    ``role`` is what :func:`audit_render._agent_entry` writes; ``phase`` is the
    key older records used.
    """
    if not isinstance(entry, dict):
        return False
    return entry.get("role") == "dev" or entry.get("phase") == "dev"


def entry_model_identity_detail(entry: object) -> tuple[str, str, str] | None:
    """Return the entry's *resolved primary* ``(identity, source, resolution)``.

    An entry carrying the #2205 ledger answers directly from
    ``ledger.resolved_primary_identity`` — the concrete model that served.
    Consumers that need the configured identity as well, or need to know whether
    the two differ, must read :func:`entry_identity_ledger` instead; this
    function is deliberately still one identity, because its callers index one
    column and widening the return type would break them silently.

    Older entries fall back to the legacy reconstruction:
      1. ``model_used`` — the model the runner reports having invoked
         (:data:`SOURCE_DIRECT`).
      2. ``model_config`` — the configured preference list for the invocation.
         Which entry actually served is not recorded, so the preferred (first)
         entry is reported as a :data:`SOURCE_RECOVERED` reconstruction.
      3. Legacy ``provider``/``model``/``cli`` or ``name`` keys, kept only as
         compatibility for records written before the current renderer
         (:data:`SOURCE_RECOVERED`).

    The third element says whether the identity was normalized onto the
    canonical namespace (:data:`RESOLUTION_CANONICAL`) or is the runner's
    spelling reported verbatim (:data:`RESOLUTION_UNRESOLVED`).
    """
    if not isinstance(entry, dict):
        return None
    ledger = entry.get("ledger")
    if isinstance(ledger, dict):
        resolved = _identity_detail(ledger.get("resolved_primary_identity"))
        if resolved is not None:
            return resolved
        # A ledger that recorded no resolved identity (a profile that never
        # dispatched) still knows what was configured. Reported as recovered:
        # it is what the invocation *would* have run, not what did.
        configured = _identity_detail(ledger.get("configured_identity"))
        if configured is not None:
            return (configured[0], SOURCE_RECOVERED, configured[2])
    return _legacy_entry_identity_detail(entry)


def _legacy_entry_identity_detail(entry: object) -> tuple[str, str, str] | None:
    """Reconstruct one identity from the pre-ledger entry fields.

    Kept intact so records written before #2205 read exactly as they did.
    """
    if not isinstance(entry, dict):
        return None

    model_used = str(entry.get("model_used") or "").strip()
    if model_used:
        # The entry is a hint source only for the identity the runner actually
        # invoked: ``transport_used`` describes *that* invocation.
        identity, resolution = _canonicalize(model_used, entry)
        return (identity, SOURCE_DIRECT, resolution)

    model_config = entry.get("model_config")
    if isinstance(model_config, list):
        for candidate in model_config:
            configured = str(candidate or "").strip()
            if configured:
                # No transport hint here: which preference-list entry served is
                # not recorded, so the invocation's transport does not
                # necessarily describe the preferred one.
                identity, resolution = _canonicalize(configured)
                return (identity, SOURCE_RECOVERED, resolution)

    provider = str(entry.get("provider") or "").strip()
    model = str(entry.get("model") or "").strip()
    cli = str(entry.get("cli") or "").strip()
    if provider and model:
        # Already assembled in canonical shape by construction.
        return (
            f"{provider}/{model}/{'cli' if cli else 'api'}",
            SOURCE_RECOVERED,
            RESOLUTION_CANONICAL,
        )
    name = str(entry.get("name") or "").strip()
    if name:
        identity, resolution = _canonicalize(name)
        return (identity, SOURCE_RECOVERED, resolution)
    return None


def entry_model_identity(entry: object) -> tuple[str, str] | None:
    """Return ``(identity, source)`` for one ``cost.agents`` entry, or None.

    Thin compatibility view over :func:`entry_model_identity_detail` for
    callers that do not care whether the identity was canonicalized.
    """
    detail = entry_model_identity_detail(entry)
    if detail is None:
        return None
    return (detail[0], detail[1])


def dev_model_identity_detail(
    record: object,
) -> tuple[str | None, str | None, str | None]:
    """Return ``(identity, source, resolution)`` for a record's dev agent.

    A directly recorded identity wins over a reconstructed one regardless of
    entry order, so a run whose later dev attempt recorded ``model_used`` is
    not reported as recovered. Returns ``(None, None, None)`` when no dev-role
    entry carries a trustworthy invocation identity.
    """
    empty: tuple[str | None, str | None, str | None] = (None, None, None)
    if not isinstance(record, dict):
        return empty
    cost_block = record.get("cost")
    if not isinstance(cost_block, dict):
        return empty
    agents = cost_block.get("agents")
    if not isinstance(agents, list):
        return empty

    recovered: tuple[str, str, str] | None = None
    for entry in agents:
        if not is_dev_entry(entry):
            continue
        resolved = entry_model_identity_detail(entry)
        if resolved is None:
            continue
        if resolved[1] == SOURCE_DIRECT:
            return resolved
        if recovered is None:
            recovered = resolved
    if recovered is not None:
        return recovered
    return empty


def dev_identity_ledger(record: object) -> dict:
    """Return the dev agent's invocation-ledger projection for a record (#2205).

    Same mapping shape as :func:`entry_identity_ledger`, selected across the
    record's dev entries. A full-ledger entry wins over a legacy one regardless
    of entry order — a run whose later dev attempt carries the ledger must not
    be reported as pre-ledger — mirroring how
    :func:`dev_model_identity_detail` prefers a direct identity over a
    recovered one.

    Returns the empty projection (``full_ledger: False``, all identities None)
    when the record has no dev entry at all, so callers can index unconditionally
    without distinguishing "no dev agent" from "malformed record" at every site.
    """
    empty: dict = {
        "full_ledger": False,
        "version": None,
        "configured": None,
        "resolved": None,
        "differs": None,
        "billed": [],
        "cost_usd": None,
        "cost_provenance": None,
        "reasoning_effort": None,
        "complexity": None,
        "complexity_score": None,
        "role": None,
    }
    if not isinstance(record, dict):
        return empty
    cost_block = record.get("cost")
    if not isinstance(cost_block, dict):
        return empty
    agents = cost_block.get("agents")
    if not isinstance(agents, list):
        return empty

    legacy: dict | None = None
    for entry in agents:
        if not is_dev_entry(entry):
            continue
        projection = entry_identity_ledger(entry)
        if projection is None:
            continue
        if projection["full_ledger"]:
            return projection
        if legacy is None:
            legacy = projection
    return legacy if legacy is not None else empty


def dev_model_identity(record: object) -> tuple[str | None, str | None]:
    """Return ``(identity, source)`` for the dev agent of an audit record.

    Compatibility view over :func:`dev_model_identity_detail`; consumers that
    persist or expose the identity should prefer the detailed projection so an
    unresolved spelling stays distinguishable from a canonical one (#2225).
    """
    identity, source, _resolution = dev_model_identity_detail(record)
    return (identity, source)
