"""Forge-owned sandbox capability presets.

Some stacks cannot *develop* a change inside the default write-containment
sandbox — not run the gate, develop. The forcing case is iOS/Xcode, where
``xcodegen``/``xcodebuild`` need writes under ``~/Library/Developer`` and
mach-lookups for the simulator/toolchain; inside the default profile those
return ``Operation not permitted``, so the agent cannot build and therefore
cannot verify its own work (#1947).

A project widens the sandbox by *selecting a preset by name* in ``forge.yaml``:

.. code-block:: yaml

    sandbox:
      capability_profile: xcode

Presets are forge-owned. A project cannot author, extend, or override the
contents of one, and there is deliberately no value that disables the sandbox
or grants ``allow default`` — widening is always a named, bounded capability
set. Selecting nothing keeps today's behavior exactly.

The declared capability set is pure data: :func:`resolve_capabilities` expands
it without probing the host for an installed toolchain, so a preset resolves
identically on any machine. That is what makes "what does ``xcode`` grant?"
answerable without running an agent.

This module is stdlib-only and dependency-free by design: ``config.load``
validates preset names at load time and ``runners.sandbox`` consumes the
resolved capabilities, and neither should have to import the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ResolvedSandboxCapabilities",
    "SandboxCapabilityError",
    "SandboxCapabilityPreset",
    "UnknownCapabilityProfileError",
    "UnsupportedCapabilityProfileError",
    "get_preset",
    "preset_names",
    "resolve_capabilities",
]


class SandboxCapabilityError(ValueError):
    """Base class for capability-profile resolution failures."""


class UnknownCapabilityProfileError(SandboxCapabilityError):
    """Raised when a project selects a preset name forge does not own."""


class UnsupportedCapabilityProfileError(SandboxCapabilityError):
    """Raised when the host platform's sandbox cannot express a preset.

    Fail-closed signal: running with the declared capability silently absent is
    exactly the trap this feature exists to remove, so callers must surface this
    rather than degrade to the default capability set.
    """


@dataclass(frozen=True)
class SandboxCapabilityPreset:
    """A named, forge-owned capability set.

    ``write_roots`` are declarative path templates, not resolved paths — a
    leading ``~`` is expanded at resolution time against the invoking user's
    home. They are stored unexpanded so the preset table stays host-independent.

    ``supported_platforms`` holds ``platform.system()`` values whose sandbox
    backend can express every axis this preset declares. A preset that grants
    mach services is macOS-only, because ``bwrap`` has no mach-service axis.
    """

    name: str
    description: str
    supported_platforms: frozenset[str]
    write_roots: tuple[str, ...] = ()
    mach_services: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedSandboxCapabilities:
    """The concrete capability set a selected preset expands to.

    ``profile`` is ``None`` for the default (no preset selected) case, which
    resolves to no extra write roots and no extra services — byte-for-byte the
    containment boundary forge has always applied.
    """

    profile: str | None
    write_roots: tuple[Path, ...] = ()
    mach_services: tuple[str, ...] = ()

    @property
    def is_default(self) -> bool:
        """True when no preset was selected (default containment)."""
        return self.profile is None

    def audit_payload(self) -> dict:
        """Serialise for the run audit record.

        Always emits every key — an explicit ``None``/empty value records
        "default containment" rather than "capability data omitted", so a
        reviewer can tell the two apart in an audit log.
        """
        return {
            "profile": self.profile,
            "write_roots": [str(path) for path in self.write_roots],
            "mach_services": list(self.mach_services),
        }


# ── Preset table ──────────────────────────────────────────────────────────
#
# Keep every entry declarative and minimal. A preset is a promise about what
# stays *denied* as much as what is granted, so add a root only when a stack
# genuinely cannot produce a buildable artifact without it.

_XCODE = SandboxCapabilityPreset(
    name="xcode",
    description=(
        "Apple/Xcode toolchain: xcodegen, xcodebuild, swiftpm and the iOS "
        "simulator. Grants the DerivedData/Developer state roots and the "
        "simulator/launch-services mach services those tools require."
    ),
    # bwrap cannot express mach-lookup, so this preset is macOS-only and must
    # fail closed elsewhere rather than run with the services missing.
    supported_platforms=frozenset({"Darwin"}),
    write_roots=(
        # Xcode's DerivedData, device support, toolchains and simulator state.
        "~/Library/Developer",
        "~/Library/Caches/com.apple.dt.Xcode",
        "~/Library/Caches/org.swift.swiftpm",
        "~/Library/org.swift.swiftpm",
        # CoreSimulator's runtime/device state lives outside ~/Library/Developer.
        "~/Library/Logs/CoreSimulator",
        # macOS $TMPDIR resolves under /var/folders; xcodebuild writes response
        # files and build intermediates there. /tmp is already granted by default.
        "/private/var/folders",
    ),
    mach_services=(
        "com.apple.CoreSimulator.CoreSimulatorService",
        "com.apple.coreservices.launchservicesd",
        "com.apple.lsd.mapdb",
        "com.apple.system.notification_center",
    ),
)

_PRESETS: dict[str, SandboxCapabilityPreset] = {_XCODE.name: _XCODE}


def preset_names() -> tuple[str, ...]:
    """Every preset name a project may select, sorted."""
    return tuple(sorted(_PRESETS))


def get_preset(name: str) -> SandboxCapabilityPreset:
    """Return the forge-owned preset called *name*.

    Raises:
        UnknownCapabilityProfileError: if forge does not own a preset by that
            name. Projects select presets; they do not define them, so an
            unrecognised name is always an error, never an implicit passthrough.
    """
    try:
        return _PRESETS[name]
    except KeyError:
        raise UnknownCapabilityProfileError(
            f"unknown sandbox capability profile {name!r}; "
            f"forge-owned profiles are: {list(preset_names())}"
        ) from None


def _expand(template: str, home: Path) -> Path:
    if template == "~" or template.startswith("~/"):
        return (home / template[2:]).resolve() if template != "~" else home.resolve()
    return Path(template).resolve()


def resolve_capabilities(
    profile: str | None,
    *,
    home: Path | None = None,
    system: str | None = None,
) -> ResolvedSandboxCapabilities:
    """Expand a selected preset name into its concrete capability set.

    Resolution is pure: it expands ``~`` and normalises paths, and never probes
    the host for an installed toolchain or an existing directory. The result is
    therefore exactly the preset's declared set and nothing more, on any host.

    Args:
        profile: preset name, or ``None`` for default containment.
        home: home directory used to expand ``~``; defaults to the real one.
        system: ``platform.system()`` value to validate the preset against.
            ``None`` (the default) skips the platform check, which is what
            makes inspection possible on a host that could not run the preset.

    Raises:
        UnknownCapabilityProfileError: *profile* names no forge-owned preset.
        UnsupportedCapabilityProfileError: *system* is given and that platform's
            sandbox backend cannot express the preset.
    """
    if profile is None:
        return ResolvedSandboxCapabilities(profile=None)

    preset = get_preset(profile)
    if system is not None and system not in preset.supported_platforms:
        raise UnsupportedCapabilityProfileError(
            f"sandbox capability profile {preset.name!r} is not supported on "
            f"{system} — it declares capabilities "
            f"({'mach services, ' if preset.mach_services else ''}"
            f"platform-specific paths) that this host's sandbox backend cannot "
            f"express. Supported platforms: {sorted(preset.supported_platforms)}. "
            "Refusing to run with the declared capability absent."
        )

    resolved_home = (home or Path.home()).resolve()
    write_roots: list[Path] = []
    seen: set[Path] = set()
    for template in preset.write_roots:
        path = _expand(template, resolved_home)
        # A preset that resolves to a filesystem or home root would be a
        # wholesale sandbox escape wearing a preset's name. Presets are
        # forge-owned, so this is a guard against our own table, not user input.
        if path in (Path("/"), resolved_home):
            raise UnsupportedCapabilityProfileError(
                f"sandbox capability profile {preset.name!r} declares write root "
                f"{template!r} which resolves to {path} — presets may not grant "
                "a filesystem or home root."
            )
        if path in seen:
            continue
        seen.add(path)
        write_roots.append(path)

    return ResolvedSandboxCapabilities(
        profile=preset.name,
        write_roots=tuple(write_roots),
        mach_services=preset.mach_services,
    )
