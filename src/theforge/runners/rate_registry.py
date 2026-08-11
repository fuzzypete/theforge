"""One resolved rate card per dispatched identity, keyed by what actually ran.

Pricing used to be resolved twice from two different surfaces: routing read the
figures declared on the model registry, accounting read a table compiled into
:mod:`theforge.runners.schema_utils`. A model present in the first and absent
from the second routed as priced and recorded its spend as unknown (#2335).

The fix inverts the direction of resolution. Nothing *carries* a rate. One
registry is compiled at configuration load, keyed by :class:`DispatchIdentity` —
``(provider, model, transport)``, the same triple that forms a canonical model
id — and every accounting site looks up the identity that ACTUALLY RAN at the
moment it prices tokens. That is why no candidate-construction path has to be
converted and none can be missed: a profile produced by ``dataclasses.replace``,
by ``apply_model_info``, by a transport fallback or by the adaptive router is
priced by what it dispatched, not by what it was seated as.

**The lookup never crosses transports.** Every key in a compiled registry is a
concrete ``(provider, model, transport)``. An exact miss is unpriced, full stop.
Cross-transport reuse of a legacy, transport-agnostic price exists only as a
COMPILE-TIME materialization (see :mod:`theforge.config.dispatch_rates`) that is
recorded on the entry as :attr:`RateSource.LEGACY_COMPAT` with the key it came
from, so it is inspectable rather than implicit at query time. That is what stops
a model priced by the operator on the CLI from silently lending its price to the
same model reached over the API.

Deliberately stdlib-only at module scope. :class:`ModelRates` and
:func:`pricing_for` live in ``schema_utils`` and are imported lazily where the
no-registry baseline needs them, so ``schema_utils`` may import this module but
never the reverse at import time.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from theforge.config.types import ModelProfile

    from .schema_utils import ModelRates

logger = logging.getLogger(__name__)


class AccountingMode(str, Enum):
    """How — and whether — spend on a transport can be established at all.

    Static rates and measurable spend are different questions, and conflating
    them is half of #2335: the Claude CLI reports a billed total and needs no
    rate card, while the Gemini CLI carries perfectly good catalog rates and
    reports nothing to multiply them by on most invocations.
    """

    #: The transport reports a price it was billed (Claude CLI's total_cost_usd).
    PROVIDER_REPORTED = "provider_reported"
    #: The transport measures spend by its own means, independent of any rate
    #: card (gh-aw converts AI-credit consumption).
    INDEPENDENTLY_MEASURED = "independently_measured"
    #: Spend is token count × rate card. No rates means no cost record.
    TOKEN_ESTIMATED = "token_estimated"
    #: Token count × rate card *when the transport reports a token count at all*.
    #: The Gemini CLI reports usage on some invocations and not others; where it
    #: does not, cost stays unknown rather than being fabricated.
    TOKEN_ESTIMATED_IF_REPORTED = "token_estimated_if_reported"
    #: Nothing observable to price. Recorded so it is reported, not guessed at.
    UNMEASURABLE = "unmeasurable"
    #: Not classified — an identity that never went through a compiled registry.
    UNKNOWN = "unknown"

    @property
    def needs_rates(self) -> bool:
        """Does an absent rate card make this transport's spend unrecordable?"""
        return self in (
            AccountingMode.TOKEN_ESTIMATED,
            AccountingMode.TOKEN_ESTIMATED_IF_REPORTED,
        )

    @property
    def measures_without_rates(self) -> bool:
        """Does this transport record spend whether or not a rate card exists?"""
        return self in (
            AccountingMode.PROVIDER_REPORTED,
            AccountingMode.INDEPENDENTLY_MEASURED,
        )


class RateSource(str, Enum):
    """Where an entry's figures came from — reported, never inferred."""

    PROJECT = "project"  # declared in forge.yaml
    CATALOG = "catalog"  # shipped model catalog entry
    LEGACY_COMPAT = "legacy_compat"  # widened from the transport-agnostic table
    BASELINE = "baseline"  # no registry installed: today's pricing_for answer
    NONE = "none"  # unpriced


@dataclass(frozen=True)
class DispatchIdentity:
    """``(provider, model, transport)`` — the thing a price belongs to.

    ``transport`` is the transport *kind* (``"cli"`` / ``"api"``), matching
    :func:`theforge.config.model_identity.canonical_model_id`. Two runners of the
    same kind (Claude CLI and Codex CLI) never share a provider+model, so the
    kind is a sufficient discriminator and the key stays the canonical triple.
    """

    provider: str
    model: str
    transport: str

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model} ({self.transport})"


def make_identity(
    provider: str | None,
    model: str | None,
    transport: str | None,
) -> DispatchIdentity | None:
    """Build a *concrete* identity, or None when any component is missing.

    A partial key can never match a registry entry, so it is refused at
    construction rather than inserted and silently missed forever. Callers treat
    ``None`` as "this profile is not resolvable to a billed identity" and fall
    back to the no-registry baseline (see :func:`resolve`).
    """
    if not provider or not model or not transport:
        return None
    return DispatchIdentity(
        provider=str(provider).strip(),
        model=str(model).strip(),
        transport=str(transport).strip(),
    )


def identity_of(profile: "ModelProfile") -> DispatchIdentity | None:
    """The one place a :class:`ModelProfile` becomes a pricing key.

    Reads ``provider_family`` (the provider identity for *both* transports) and
    ``mode`` (the transport kind), so a replaced/derived profile resolves to the
    identity it will actually dispatch on. ``provider_family`` is None on a
    profile carrying neither a provider nor a transport; that yields None here.
    """
    return make_identity(
        getattr(profile, "provider_family", None),
        getattr(profile, "model", None),
        getattr(profile, "mode", None),
    )


# ── Accounting capability by transport ────────────────────────────────

# Keyed by TransportSpec.runner for CLI transports. A runner absent from this
# map is UNMEASURABLE rather than assumed estimable: an unrecognised CLI has no
# demonstrated usage-reporting path, and claiming otherwise is how "has rates"
# came to be read as "spend is captured".
_CLI_ACCOUNTING: dict[str, AccountingMode] = {
    # Reports total_cost_usd per run; that figure wins over any estimate.
    "claude": AccountingMode.PROVIDER_REPORTED,
    # `codex exec --json` reports a real per-turn token split (#2019).
    "codex": AccountingMode.TOKEN_ESTIMATED,
    # Reports usage only on invocations that emit a stats block.
    "gemini": AccountingMode.TOKEN_ESTIMATED_IF_REPORTED,
    # Converts AI-credit consumption to a cost independently of any rate card.
    "ghaw": AccountingMode.INDEPENDENTLY_MEASURED,
}


def accounting_mode_for(transport_kind: str | None, runner: str | None) -> AccountingMode:
    """Classify how spend on ``(transport_kind, runner)`` can be established."""
    if transport_kind == "api":
        # No API transport forge speaks returns a price; every one reports tokens.
        return AccountingMode.TOKEN_ESTIMATED
    if transport_kind == "cli":
        return _CLI_ACCOUNTING.get(runner or "", AccountingMode.UNMEASURABLE)
    return AccountingMode.UNKNOWN


# ── Entries and the registry ──────────────────────────────────────────


@dataclass(frozen=True)
class RateEntry:
    """The resolved accounting answer for one identity."""

    identity: DispatchIdentity | None
    rates: "ModelRates | None" = None
    mode: AccountingMode = AccountingMode.UNKNOWN
    source: RateSource = RateSource.NONE
    #: For LEGACY_COMPAT, the transport-agnostic key this was materialized from;
    #: otherwise the registry key or catalog id the figures were declared on.
    origin: str = ""

    @property
    def priced(self) -> bool:
        return self.rates is not None

    @property
    def accountable(self) -> bool:
        """Can a run on this identity produce a cost record at all?"""
        if self.mode.measures_without_rates:
            return True
        if self.mode is AccountingMode.UNMEASURABLE:
            return False
        return self.priced

    def unaccountable_reason(self) -> str | None:
        """Operator-facing reason this identity cannot be accounted for."""
        if self.accountable:
            return None
        if self.mode is AccountingMode.UNMEASURABLE:
            return "transport reports no usage and no price"
        return "no rate card for this transport"


@dataclass(frozen=True)
class RateRegistry:
    """Compiled ``DispatchIdentity -> RateEntry`` map with a strict lookup.

    ``lookup`` matches the exact key only. There is no transport-agnostic tier
    and no fallthrough to :data:`PRICING_TABLE`: once a registry is installed, a
    miss is unpriced and says so. Cross-transport reuse happens at compile time
    or not at all.
    """

    entries: Mapping[DispatchIdentity, RateEntry] = field(default_factory=dict)
    #: Identities the loaded configuration can dispatch on, with the paths each
    #: is reachable through. Populated at compile time; consulted by the
    #: load-time report.
    reachable: Mapping[DispatchIdentity, tuple[str, ...]] = field(default_factory=dict)

    def lookup(self, identity: DispatchIdentity) -> RateEntry:
        entry = self.entries.get(identity)
        if entry is not None:
            return entry
        # Compilation records an entry for every reachable identity, priced or
        # not, so a miss here is an identity the loaded configuration cannot
        # dispatch on. Classify it from its transport kind alone.
        return RateEntry(
            identity=identity,
            rates=None,
            mode=accounting_mode_for(identity.transport, None),
            source=RateSource.NONE,
        )


# ── Process-level install contract ────────────────────────────────────
#
# The compiled registry is consumed ONCE, at configuration load, into this
# process-level slot. No function signature gains a registry parameter and no
# runner imports configuration loading; the alternative — threading the registry
# through every dispatch signature — is the shape that made the previous attempt
# miss a path per review cycle.
#
# Semantics: last install wins, and installation happens only after a
# ForgeConfig has been fully constructed, so a load that raises during
# validation never leaves its partial rates active. A second load (a second
# project in one process, or a test) replaces the previous registry and logs at
# debug so the swap is visible in the audit.

_ACTIVE: RateRegistry | None = None
_LOCK = threading.Lock()


def install(registry: RateRegistry) -> None:
    """Make ``registry`` the process-wide answer for pricing lookups."""
    global _ACTIVE
    with _LOCK:
        previous = _ACTIVE
        _ACTIVE = registry
    if previous is not None and previous.entries:
        logger.debug(
            "Rate registry replaced: %d entries -> %d entries (last install wins)",
            len(previous.entries),
            len(registry.entries),
        )


def active() -> RateRegistry | None:
    """The installed registry, or None when nothing has been installed."""
    return _ACTIVE


def reset() -> None:
    """Uninstall any registry, restoring the no-registry baseline."""
    global _ACTIVE
    with _LOCK:
        _ACTIVE = None


@contextmanager
def scoped_registry(registry: RateRegistry | None) -> Iterator[RateRegistry | None]:
    """Install ``registry`` for the duration of the block, then restore.

    ``None`` scopes the *no-registry baseline* in, which is how a test asserts
    that uninstalled behaviour is unchanged without leaking a previous load's
    rates into the next test.
    """
    global _ACTIVE
    with _LOCK:
        previous = _ACTIVE
        _ACTIVE = registry
    try:
        yield registry
    finally:
        with _LOCK:
            _ACTIVE = previous


# ── Resolution ────────────────────────────────────────────────────────


def resolve(identity: DispatchIdentity | None) -> RateEntry:
    """Resolve one identity to its accounting entry.

    With a registry installed this is an exact, transport-respecting lookup.
    With nothing installed — unit tests, and any code path that never loads a
    configuration — it delegates to :func:`pricing_for`, reproducing today's
    transport-blind behaviour bit for bit. That baseline is the ONLY place
    transport-blind pricing survives.
    """
    if identity is None:
        return RateEntry(identity=None)
    registry = active()
    if registry is None:
        from .schema_utils import pricing_for  # noqa: PLC0415

        rates = pricing_for(identity.provider, identity.model)
        return RateEntry(
            identity=identity,
            rates=rates,
            mode=AccountingMode.UNKNOWN,
            source=RateSource.BASELINE if rates is not None else RateSource.NONE,
            origin="no-registry-baseline",
        )
    return registry.lookup(identity)


def rates_for(
    provider: str | None,
    model: str | None,
    transport: str | None,
) -> "ModelRates | None":
    """Rate card for the identity that ran, or None when it is not priced."""
    identity = make_identity(provider, model, transport)
    if identity is None:
        # A caller that cannot name the transport takes the baseline rather than
        # a key that could never match — see make_identity.
        from .schema_utils import pricing_for  # noqa: PLC0415

        if not provider or not model:
            return None
        return pricing_for(provider, model)
    return resolve(identity).rates


def entry_for_profile(profile: "ModelProfile") -> RateEntry:
    """Resolve the entry for the identity ``profile`` would dispatch on."""
    return resolve(identity_of(profile))


def known_models(provider: str, transport: str) -> tuple[str, ...]:
    """Model names the installed registry prices for ``(provider, transport)``.

    Used by the Claude CLI's dated-id prefix match, which must run against keys
    of the SAME transport only. Empty when nothing is installed; the caller
    falls back to the packaged table in that case.
    """
    registry = active()
    if registry is None:
        return ()
    return tuple(
        sorted(
            identity.model
            for identity, entry in registry.entries.items()
            if identity.provider == provider and identity.transport == transport and entry.priced
        )
    )
