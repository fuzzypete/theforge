"""Forge-owned sandbox capability presets.

Some stacks cannot *develop* a change inside the default write-containment
sandbox — not run the gate, develop. The forcing case is iOS/Xcode, where
``xcodegen``/``xcodebuild`` need writes under ``~/Library/Developer`` and
mach-lookups for the simulator/toolchain; inside the default profile those
return ``Operation not permitted``, so the agent cannot build and therefore
cannot verify its own work (#1947).

A project widens the sandbox by *selecting a preset by name* in ``forge.yaml``,
and may add its own bounded grants alongside it:

.. code-block:: yaml

    sandbox:
      capability_profile: xcode
      write_roots:
        - ~/Library/Preferences
      mach_services:
        - com.apple.dt.Xcode.something

Presets stay forge-owned: a project cannot author, override or subtract from
the contents of one. What a project *can* do is add write roots and mach
services of its own, because the set of things a real toolchain needs is not
knowable in advance from inside this repository (#2038). Project grants are
strictly **additive** to the selected preset — there is deliberately no value
that disables the sandbox or grants ``allow default``, so widening is always a
bounded, enumerated capability set. Declaring nothing keeps today's behavior
exactly.

The declared capability set is pure data: :func:`resolve_capabilities` expands
it without probing the host for an installed toolchain, so a declaration
resolves identically on any machine. That is what makes "what does ``xcode``
grant?" answerable without running an agent. What it will *not* do is resolve
quietly when an axis cannot be expressed: a mach service on a ``bwrap`` host,
or a write root that resolves to ``/`` or the invoking home, raises
:class:`UnsupportedCapabilityProfileError` naming exactly what was denied.

This module is stdlib-only and dependency-free by design: ``config.load``
validates preset names at load time and ``runners.sandbox`` consumes the
resolved capabilities, and neither should have to import the other.
"""

from __future__ import annotations

from collections.abc import Iterable
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
    """The concrete capability set a declaration expands to.

    ``write_roots`` and ``mach_services`` are the *merged* set actually applied:
    the selected preset's grants plus any the project declared inline.
    ``project_write_roots``/``project_mach_services`` carry the project-declared
    subset separately so an audit reader can tell a forge-owned grant from one
    the project added.

    ``profile`` is ``None`` when no preset was selected. With no project grants
    either, that is the default case — no extra write roots and no extra
    services, byte-for-byte the containment boundary forge has always applied.
    """

    profile: str | None
    write_roots: tuple[Path, ...] = ()
    mach_services: tuple[str, ...] = ()
    project_write_roots: tuple[Path, ...] = ()
    project_mach_services: tuple[str, ...] = ()

    @property
    def is_default(self) -> bool:
        """True when nothing was declared at all (default containment)."""
        return self.profile is None and not self.write_roots and not self.mach_services

    def audit_payload(self) -> dict:
        """Serialise for the run audit record.

        Always emits the applied set — an explicit ``None``/empty value records
        "default containment" rather than "capability data omitted", so a
        reviewer can tell the two apart in an audit log.

        The ``project_*`` provenance keys appear only when the project actually
        declared a grant. A record without them is a run whose capabilities were
        entirely forge-owned, which is what every record said before project
        grants existed — so the base shape is unchanged and no reader has to
        migrate across this addition.
        """
        payload: dict = {
            "profile": self.profile,
            "write_roots": [str(path) for path in self.write_roots],
            "mach_services": list(self.mach_services),
        }
        if self.project_write_roots:
            payload["project_write_roots"] = [str(path) for path in self.project_write_roots]
        if self.project_mach_services:
            payload["project_mach_services"] = list(self.project_mach_services)
        return payload


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


def _expand_write_roots(
    templates: Iterable[str],
    *,
    home: Path,
    source: str,
    into: list[Path],
    seen: set[Path],
) -> list[Path]:
    """Expand *templates*, appending newly-seen paths to *into*.

    Returns the expanded paths from *templates* alone (before de-duplication
    against *seen*), so a caller can record provenance separately from the
    merged set.

    A grant that resolves to a filesystem or home root is a wholesale sandbox
    escape wearing a grant's name, whatever declared it — so the guard applies
    to forge's own preset table and to project declarations alike.
    """
    expanded: list[Path] = []
    for template in templates:
        path = _expand(template, home)
        if path in (Path("/"), home):
            raise UnsupportedCapabilityProfileError(
                f"{source} declares write root {template!r} which resolves to "
                f"{path} — a sandbox grant may not include a filesystem or home root."
            )
        expanded.append(path)
        if path in seen:
            continue
        seen.add(path)
        into.append(path)
    return expanded


def resolve_capabilities(
    profile: str | None,
    *,
    home: Path | None = None,
    system: str | None = None,
    write_roots: Iterable[str] = (),
    mach_services: Iterable[str] = (),
) -> ResolvedSandboxCapabilities:
    """Expand a capability declaration into its concrete capability set.

    Resolution is pure: it expands ``~`` and normalises paths, and never probes
    the host for an installed toolchain, an existing directory, or a registered
    mach service. The result is therefore exactly the declared set and nothing
    more, on any host.

    Args:
        profile: preset name, or ``None`` when no preset is selected.
        home: home directory used to expand ``~``; defaults to the real one.
        system: ``platform.system()`` value to validate the declaration against.
            ``None`` (the default) skips the platform check, which is what
            makes inspection possible on a host that could not run it.
        write_roots: project-declared write-root templates, added to whatever
            the selected preset grants. Keyword-only with an empty default so
            every existing caller keeps resolving exactly the preset.
        mach_services: project-declared mach services, likewise additive.

    Raises:
        UnknownCapabilityProfileError: *profile* names no forge-owned preset.
        UnsupportedCapabilityProfileError: *system* is given and that platform's
            sandbox backend cannot express the declaration, or a declared write
            root resolves to a filesystem or home root.
    """
    project_root_templates = tuple(write_roots)
    project_services = tuple(dict.fromkeys(mach_services))

    if profile is None and not project_root_templates and not project_services:
        return ResolvedSandboxCapabilities(profile=None)

    preset = get_preset(profile) if profile is not None else None
    if preset is not None and system is not None and system not in preset.supported_platforms:
        raise UnsupportedCapabilityProfileError(
            f"sandbox capability profile {preset.name!r} is not supported on "
            f"{system} — it declares capabilities "
            f"({'mach services, ' if preset.mach_services else ''}"
            f"platform-specific paths) that this host's sandbox backend cannot "
            f"express. Supported platforms: {sorted(preset.supported_platforms)}. "
            "Refusing to run with the declared capability absent."
        )
    # A project may declare a mach service without selecting a preset, so the
    # preset's supported_platforms check above does not cover it. Only Darwin's
    # sandbox backend has a mach-lookup axis; bwrap has none, so a declaration
    # there must refuse rather than resolve with the service silently absent.
    if project_services and system is not None and system != "Darwin":
        raise UnsupportedCapabilityProfileError(
            f"forge.yaml 'sandbox.mach_services' declares {list(project_services)}, "
            f"which the {system} sandbox backend (bwrap) cannot express — it has no "
            "mach-lookup axis. Refusing to run with the declared capability absent."
        )

    resolved_home = (home or Path.home()).resolve()
    merged_roots: list[Path] = []
    seen: set[Path] = set()
    if preset is not None:
        _expand_write_roots(
            preset.write_roots,
            home=resolved_home,
            source=f"sandbox capability profile {preset.name!r}",
            into=merged_roots,
            seen=seen,
        )
    project_roots = _expand_write_roots(
        project_root_templates,
        home=resolved_home,
        source="forge.yaml 'sandbox.write_roots'",
        into=merged_roots,
        seen=seen,
    )

    preset_services = preset.mach_services if preset is not None else ()
    merged_services = tuple(dict.fromkeys((*preset_services, *project_services)))

    return ResolvedSandboxCapabilities(
        profile=preset.name if preset is not None else None,
        write_roots=tuple(merged_roots),
        mach_services=merged_services,
        project_write_roots=tuple(dict.fromkeys(project_roots)),
        project_mach_services=project_services,
    )
