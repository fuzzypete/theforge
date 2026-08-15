"""Durable record of what a model has been *demonstrated* to do (#2466).

``forge check-providers`` exercises each configured identity and reports the
result to whoever ran it. Before this module the finding died with the
invocation: routing had no way to consult it, so every dispatch assumed any
eligible model could serve whatever the phase required, and a capability
mismatch was discovered by failing a phase an hour later.

This module is the fact's storage. It maintains ``.forge/model_capabilities.yaml``
— deliberately separate from ``model_profiles.yaml``, which aggregates *run
outcomes* (success rate, cost, iterations) rather than *demonstrations*. A run
outcome is a statistic about attempts; a capability record is a claim about what
an identity did when a probe exercised exactly that capability.

**What counts as a demonstration.** Only a probe that actually validated the
capability establishes it. The readiness check exercises some roles without
validating structured output at all (the dev and preflight phases accept
unstructured output by design), so those probes establish *nothing* here — a
READY status is not, by itself, evidence of structured capability. The probe
classifies its own attempts (see
``theforge.cli.provider_readiness.capability_observations``); this module stores
what it is told and never re-derives a capability from a readiness status.

**Three states, and the fourth that is not a state.** A lookup answers
``demonstrated``, ``demonstrated_absent``, or ``never_established`` for any
identity+capability. Never-established is *not* absence: an unprobed identity
stays eligible, so introducing this record cannot silently narrow the routing
pool to whatever has been probed. ``stale`` is reported alongside those three:
a record whose probe subject no longer matches the identity it describes is not
presented as current.

Pure stdlib + YAML. No LLM calls, no policy about *which* role needs *which*
capability — that lives in :mod:`theforge.assignment`, which is the component
that decides where work goes.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

# ── Capability vocabulary ─────────────────────────────────────────────
#
# Kept as a closed set so the persisted record stays machine-queryable. These
# are the names the readiness probe already uses for the two shapes it can
# exercise; ``provider_readiness`` aliases them rather than redeclaring them so
# the probe and the store can never drift.
CAPABILITY_PLAIN_STRUCTURED = "plain-structured"
CAPABILITY_TOOL_STRUCTURED = "tool-structured"

CAPABILITIES: tuple[str, ...] = (
    CAPABILITY_PLAIN_STRUCTURED,
    CAPABILITY_TOOL_STRUCTURED,
)

# What a probe attempt established about a capability. ``None`` (no outcome) is
# the third possibility at the probe boundary and means "this attempt is not
# evidence" — it is never written.
OUTCOME_DEMONSTRATED = "demonstrated"
OUTCOME_ABSENT = "absent"

OUTCOMES: tuple[str, ...] = (OUTCOME_DEMONSTRATED, OUTCOME_ABSENT)

# What a lookup reports to the router.
STATE_DEMONSTRATED = "demonstrated"
STATE_DEMONSTRATED_ABSENT = "demonstrated_absent"
STATE_NEVER_ESTABLISHED = "never_established"

STATES: tuple[str, ...] = (
    STATE_DEMONSTRATED,
    STATE_DEMONSTRATED_ABSENT,
    STATE_NEVER_ESTABLISHED,
)

SCHEMA_VERSION = 1

CAPABILITIES_FILENAME = "model_capabilities.yaml"


def capabilities_path(project_root: Path) -> Path:
    """Return the durable capability-record path for a project."""
    return Path(project_root) / ".forge" / CAPABILITIES_FILENAME


# ── Identity ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CapabilityIdentity:
    """The identity a probe exercised: (provider family, model, transport).

    This is the same tuple routing dispatches on, so a record written by the
    probe is addressable by the router without a translation table. Transport is
    part of the identity because the same model reached over CLI and over API is
    two different things to exercise — one can demonstrate a capability the
    other cannot.
    """

    provider: str
    model: str
    transport: str

    @property
    def key(self) -> str:
        """Canonical storage key: ``provider/model/transport``."""
        return f"{self.provider}/{self.model}/{self.transport}"

    def as_dict(self) -> dict[str, str]:
        return {"provider": self.provider, "model": self.model, "transport": self.transport}


def make_identity(
    provider: str | None,
    model: str | None,
    transport: str | None,
) -> CapabilityIdentity | None:
    """Build a canonical identity, or None when any half of it is unknown.

    An identity missing its provider family or model cannot be addressed later,
    so it is not recorded at all rather than recorded under a placeholder that
    would collide with every other unknown.
    """
    provider = (provider or "").strip().lower()
    model = (model or "").strip()
    transport = (transport or "").strip().lower() or "cli"
    if not provider or not model:
        return None
    return CapabilityIdentity(provider=provider, model=model, transport=transport)


def identity_for_profile(profile: Any) -> CapabilityIdentity | None:
    """Canonical identity of a :class:`~theforge.config.types.ModelProfile`.

    ``ModelProfile`` exposes the cross-transport provider as ``provider_family``
    and the transport kind as ``mode``.
    """
    return make_identity(
        getattr(profile, "provider_family", None),
        getattr(profile, "model", None),
        getattr(profile, "mode", None),
    )


def identity_for_agent(agent: Any) -> CapabilityIdentity | None:
    """Canonical identity of an :class:`~theforge.config.models.AgentDef`.

    ``AgentDef`` has no ``provider_family``; its cross-transport provider is
    ``effective_provider`` (``provider`` stays None on CLI entries). The
    transport comes from the carried ``TransportSpec``, defaulting to CLI when
    the entry declares no provider — the same default dispatch uses.
    """
    transport = getattr(agent, "transport", None)
    kind = getattr(transport, "kind", None)
    if kind is None:
        kind = "api" if getattr(agent, "provider", None) else "cli"
    return make_identity(
        getattr(agent, "effective_provider", None),
        getattr(agent, "model", None),
        kind,
    )


def _signature(
    *,
    provider: str | None,
    model: str | None,
    transport: str | None,
    base_url: str | None,
    cli: str | None,
) -> str:
    """Fingerprint of the *subject* a probe exercised.

    The canonical identity (provider/model/transport) names the record; this
    signature covers the rest of what determines what actually gets invoked —
    the endpoint an API identity is pointed at, the binary a CLI identity runs
    through. Repointing either means the recorded outcome describes a subject
    that no longer exists, which is what :func:`lookup` reports as stale. It
    never changes the identity, so a re-probe updates the same record rather
    than orphaning it.
    """
    raw = "|".join(
        [
            (provider or "").strip().lower(),
            (model or "").strip(),
            (transport or "").strip().lower(),
            (base_url or "").strip(),
            (cli or "").strip(),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def signature_for_profile(profile: Any) -> str:
    """Probe-subject signature for a ``ModelProfile``."""
    return _signature(
        provider=getattr(profile, "provider_family", None),
        model=getattr(profile, "model", None),
        transport=getattr(profile, "mode", None),
        base_url=getattr(profile, "base_url", None),
        cli=getattr(profile, "cli", None),
    )


def signature_for_agent(agent: Any) -> str:
    """Probe-subject signature for an ``AgentDef``."""
    transport = getattr(agent, "transport", None)
    kind = getattr(transport, "kind", None)
    if kind is None:
        kind = "api" if getattr(agent, "provider", None) else "cli"
    return _signature(
        provider=getattr(agent, "effective_provider", None),
        model=getattr(agent, "model", None),
        transport=kind,
        base_url=getattr(agent, "base_url", None),
        cli=getattr(agent, "cli", None),
    )


# ── Values ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CapabilityObservation:
    """One probe attempt that actually established a capability outcome.

    Produced by the readiness probe (which alone knows whether an attempt
    validated the capability) and consumed by :func:`record_observations`.
    ``detail`` carries the probe's own explanation so an operator reading a
    routing rationale can tell a structured-output failure from an unsupported
    request shape.
    """

    identity: CapabilityIdentity
    capability: str
    outcome: str
    subject_signature: str
    detail: str = ""
    probe_role: str = ""


@dataclass(frozen=True)
class CapabilityLookup:
    """What the record says about one identity+capability, right now.

    ``state`` is one of :data:`STATES`. ``stale`` is orthogonal: a stale record
    still reports the outcome it recorded, but callers must not treat it as
    current — see :func:`is_demonstrated_absent`, which declines to block on a
    stale record.
    """

    state: str
    established_at: str | None = None
    detail: str = ""
    probe_role: str = ""
    stale: bool = False
    recorded_signature: str = ""

    @property
    def current_absent(self) -> bool:
        """True only for a *current* demonstrated-absent record."""
        return self.state == STATE_DEMONSTRATED_ABSENT and not self.stale


NEVER_ESTABLISHED = CapabilityLookup(state=STATE_NEVER_ESTABLISHED)


# ── I/O ───────────────────────────────────────────────────────────────


def _empty() -> dict[str, Any]:
    return {"version": SCHEMA_VERSION, "identities": {}}


def load_capabilities(path: Path) -> dict[str, Any]:
    """Read ``model_capabilities.yaml``; return an empty record if absent/bad.

    Never raises: an unreadable record means *nothing has been established*,
    which leaves every identity eligible. A malformed file must not take routing
    down, and it must not silently narrow the pool either.
    """
    if not Path(path).exists():
        return _empty()
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as exc:  # noqa: BLE001
        log.warning("[model_capabilities] Failed to load %s: %s", path, exc)
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    if not isinstance(data.get("identities"), dict):
        data["identities"] = {}
    data.setdefault("version", SCHEMA_VERSION)
    return data


def save_capabilities(path: Path, data: dict[str, Any]) -> None:
    """Write the capability record to disk; best-effort (warns, never raises)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("[model_capabilities] Failed to write %s: %s", path, exc)


def utc_now() -> str:
    """Second-resolution UTC timestamp, matching the profile store's format."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ── Recording ─────────────────────────────────────────────────────────


def record_observations(
    data: dict[str, Any],
    observations: list[CapabilityObservation],
    *,
    established_at: str | None = None,
) -> dict[str, Any]:
    """Fold a batch of probe observations into ``data`` (mutated and returned).

    One ``check-providers`` run can produce several observations for the same
    identity+capability — the agent pool probes the same model under several
    roles, and both transports of one profile are exercised. Merging is
    deterministic and independent of the order threads happen to finish:
    **absent wins over demonstrated within a batch**. A model that failed to
    produce the capability in any probe of that capability has shown it cannot
    be relied on for it, and the probe detail records which attempt said so.

    A record is overwritten wholesale (outcome, timestamp, subject signature) —
    the latest demonstration is the current fact, so a re-probe of a changed
    subject *updates* rather than leaving a stale record behind.
    """
    if not observations:
        return data
    if not isinstance(data.get("identities"), dict):
        data["identities"] = {}
    stamp = established_at or utc_now()

    merged: dict[tuple[str, str], CapabilityObservation] = {}
    for obs in observations:
        if obs.outcome not in OUTCOMES:
            continue
        key = (obs.identity.key, obs.capability)
        prior = merged.get(key)
        if prior is None or (
            prior.outcome == OUTCOME_DEMONSTRATED and obs.outcome == OUTCOME_ABSENT
        ):
            merged[key] = obs

    for (identity_key, capability), obs in sorted(merged.items()):
        entry = data["identities"].get(identity_key)
        if not isinstance(entry, dict):
            entry = dict(obs.identity.as_dict())
            data["identities"][identity_key] = entry
        else:
            entry.update(obs.identity.as_dict())
        capabilities = entry.get("capabilities")
        if not isinstance(capabilities, dict):
            capabilities = {}
            entry["capabilities"] = capabilities
        capabilities[capability] = {
            "outcome": obs.outcome,
            "established_at": stamp,
            "subject_signature": obs.subject_signature,
            "detail": obs.detail,
            "probe_role": obs.probe_role,
        }
    return data


# ── Reading ───────────────────────────────────────────────────────────


def lookup(
    data: dict[str, Any] | None,
    identity: CapabilityIdentity | None,
    capability: str,
    *,
    subject_signature: str | None = None,
) -> CapabilityLookup:
    """Report what the record establishes for ``identity`` + ``capability``.

    Returns :data:`NEVER_ESTABLISHED` when there is no record, no identity, or
    the stored entry is unreadable — never-established, never "absent". When
    ``subject_signature`` is supplied and differs from the one the probe
    recorded, the result is flagged ``stale``: the outcome describes a subject
    that has since changed, so it is reported but not presented as current.
    """
    if not data or identity is None or not capability:
        return NEVER_ESTABLISHED
    identities = data.get("identities")
    if not isinstance(identities, dict):
        return NEVER_ESTABLISHED
    entry = identities.get(identity.key)
    if not isinstance(entry, dict):
        return NEVER_ESTABLISHED
    capabilities = entry.get("capabilities")
    if not isinstance(capabilities, dict):
        return NEVER_ESTABLISHED
    record = capabilities.get(capability)
    if not isinstance(record, dict):
        return NEVER_ESTABLISHED

    outcome = record.get("outcome")
    if outcome == OUTCOME_DEMONSTRATED:
        state = STATE_DEMONSTRATED
    elif outcome == OUTCOME_ABSENT:
        state = STATE_DEMONSTRATED_ABSENT
    else:
        return NEVER_ESTABLISHED

    recorded_signature = str(record.get("subject_signature") or "")
    stale = bool(
        subject_signature and recorded_signature and subject_signature != recorded_signature
    )
    return CapabilityLookup(
        state=state,
        established_at=(str(record["established_at"]) if record.get("established_at") else None),
        detail=str(record.get("detail") or ""),
        probe_role=str(record.get("probe_role") or ""),
        stale=stale,
        recorded_signature=recorded_signature,
    )


def is_demonstrated_absent(
    data: dict[str, Any] | None,
    identity: CapabilityIdentity | None,
    capability: str,
    *,
    subject_signature: str | None = None,
) -> bool:
    """True only when the capability is *currently* demonstrated to be absent.

    The one predicate routing needs. Never-established and stale both answer
    False: an unprobed identity stays eligible, and a record about a subject
    that has since changed is not current evidence.
    """
    return lookup(
        data,
        identity,
        capability,
        subject_signature=subject_signature,
    ).current_absent
