"""Sandbox capability profiles (#1947).

TheForge's own stack does not need a widened sandbox, and there is no iOS
toolchain here, so these tests exercise the *capability surface* — what a preset
resolves to and what stays denied — rather than a build succeeding. That is the
acceptance shape the story asks for; real-stack confirmation is out-of-band.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from theforge.config.sandbox_capabilities import (
    SandboxCapabilityError,
    UnknownCapabilityProfileError,
    UnsupportedCapabilityProfileError,
    get_preset,
    preset_names,
    resolve_capabilities,
)
from theforge.runners.sandbox import workspace_effect_sandbox_command

# ── Registry inspection ───────────────────────────────────────────────────


def test_xcode_preset_is_forge_owned_and_named() -> None:
    assert "xcode" in preset_names()
    preset = get_preset("xcode")
    assert preset.name == "xcode"
    assert preset.supported_platforms == frozenset({"Darwin"})


def test_unknown_preset_name_is_rejected() -> None:
    with pytest.raises(UnknownCapabilityProfileError, match="unknown sandbox capability profile"):
        get_preset("swiftly-made-up")


def test_no_profile_resolves_to_default_containment() -> None:
    resolved = resolve_capabilities(None)
    assert resolved.is_default
    assert resolved.profile is None
    assert resolved.write_roots == ()
    assert resolved.mach_services == ()


def test_preset_resolves_to_exactly_its_declared_set_on_any_host(tmp_path: Path) -> None:
    """Resolution is pure data — no toolchain probing, no host dependence.

    Pinning the resolved set to the *declared* set (rather than asserting a
    hand-written list) is the point: a preset cannot silently acquire a root at
    resolution time, on this host or any other.
    """
    preset = get_preset("xcode")
    fake_home = tmp_path / "fakehome"

    resolved = resolve_capabilities("xcode", home=fake_home)

    expected = tuple(
        (fake_home.resolve() / template[2:]) if template.startswith("~/") else Path(template)
        for template in preset.write_roots
    )
    assert resolved.write_roots == expected
    assert resolved.mach_services == preset.mach_services
    # Nothing beyond the declared set — the counts match exactly.
    assert len(resolved.write_roots) == len(preset.write_roots)


def test_preset_inspection_does_not_require_the_toolchain(tmp_path: Path) -> None:
    """A host with no Xcode (e.g. Linux CI) resolves the preset identically."""
    fake_home = tmp_path / "no-xcode-here"
    with patch("shutil.which", return_value=None):
        resolved = resolve_capabilities("xcode", home=fake_home)
    assert resolved.profile == "xcode"
    # The home-relative roots do not exist under this fake home; resolution
    # grants them anyway, so an uninstalled toolchain changes nothing.
    home_roots = [path for path in resolved.write_roots if fake_home.resolve() in path.parents]
    assert home_roots
    assert not any(path.exists() for path in home_roots)
    assert resolved.mach_services == get_preset("xcode").mach_services


def test_no_preset_grants_a_filesystem_or_home_root(tmp_path: Path) -> None:
    """Widening is always bounded — no preset may resolve to / or $HOME."""
    fake_home = (tmp_path / "home").resolve()
    for name in preset_names():
        resolved = resolve_capabilities(name, home=fake_home)
        for path in resolved.write_roots:
            assert path != Path("/")
            assert path != fake_home


def test_audit_payload_records_default_explicitly() -> None:
    """Default containment is recorded, not omitted — reviewers can tell them apart."""
    payload = resolve_capabilities(None).audit_payload()
    assert payload == {"profile": None, "write_roots": [], "mach_services": []}


def test_audit_payload_records_resolved_grants(tmp_path: Path) -> None:
    payload = resolve_capabilities("xcode", home=tmp_path).audit_payload()
    assert payload["profile"] == "xcode"
    assert all(isinstance(root, str) for root in payload["write_roots"])
    assert "com.apple.CoreSimulator.CoreSimulatorService" in payload["mach_services"]


# ── Fail-closed on an unexpressible platform ──────────────────────────────


def test_preset_unsupported_on_linux_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedCapabilityProfileError, match="not supported on Linux"):
        resolve_capabilities("xcode", home=tmp_path, system="Linux")


def test_sandbox_command_fails_closed_for_unsupported_backend(tmp_path: Path) -> None:
    """bwrap has no mach-service axis, so the xcode preset must refuse, not degrade."""
    with (
        patch("theforge.runners.sandbox._SYSTEM", "Linux"),
        patch("theforge.runners.sandbox._sandbox_available", return_value=True),
        pytest.raises(SandboxCapabilityError),
    ):
        workspace_effect_sandbox_command(["true"], tmp_path, capability_profile="xcode")


def test_sandbox_command_rejects_unknown_profile(tmp_path: Path) -> None:
    with (
        patch("theforge.runners.sandbox._SYSTEM", "Darwin"),
        patch("theforge.runners.sandbox._sandbox_available", return_value=True),
        pytest.raises(UnknownCapabilityProfileError),
    ):
        workspace_effect_sandbox_command(["true"], tmp_path, capability_profile="nope")


# ── Sandbox command construction ──────────────────────────────────────────


def test_no_profile_leaves_sandbox_command_unchanged(tmp_path: Path) -> None:
    """The unselected path must be byte-for-byte what forge produced before."""
    with (
        patch("theforge.runners.sandbox._SYSTEM", "Darwin"),
        patch("theforge.runners.sandbox._sandbox_available", return_value=True),
    ):
        baseline = workspace_effect_sandbox_command(["true"], tmp_path)
        explicit_none = workspace_effect_sandbox_command(
            ["true"], tmp_path, capability_profile=None
        )
    assert baseline == explicit_none


def test_macos_profile_grants_preset_roots_and_services(tmp_path: Path) -> None:
    with (
        patch("theforge.runners.sandbox._SYSTEM", "Darwin"),
        patch("theforge.runners.sandbox._sandbox_available", return_value=True),
    ):
        wrapped = workspace_effect_sandbox_command(["true"], tmp_path, capability_profile="xcode")
    profile_text = wrapped[2]
    resolved = resolve_capabilities("xcode")
    for root in resolved.write_roots:
        assert str(root) in profile_text
    for service in resolved.mach_services:
        assert f'(allow mach-lookup (global-name "{service}"))' in profile_text
    # Widening never becomes a wholesale escape.
    assert "(deny default)" in profile_text
    assert "(allow default)" not in profile_text


def test_macos_profile_grants_no_services_beyond_the_declared_set(tmp_path: Path) -> None:
    """The mach-lookup allow-set is the baseline plus exactly the preset's list."""
    with (
        patch("theforge.runners.sandbox._SYSTEM", "Darwin"),
        patch("theforge.runners.sandbox._sandbox_available", return_value=True),
    ):
        baseline = workspace_effect_sandbox_command(["true"], tmp_path)[2]
        widened = workspace_effect_sandbox_command(["true"], tmp_path, capability_profile="xcode")[
            2
        ]

    def lookups(text: str) -> set[str]:
        return {line for line in text.splitlines() if "mach-lookup" in line}

    added = lookups(widened) - lookups(baseline)
    expected = {
        f'(allow mach-lookup (global-name "{service}"))'
        for service in get_preset("xcode").mach_services
    }
    assert added == expected


# ── End-to-end narrowing, on a real macOS sandbox ─────────────────────────


def _require_usable_sandbox_exec() -> None:
    """Skip unless a real, permitted sandbox-exec is available.

    Mirrors the guard in tests/test_runner_sandbox.py: sandbox-exec can be
    present but denied when the test itself runs inside a sandbox, and a nested
    denial is not a failure of the code under test.
    """
    if platform.system() != "Darwin":
        pytest.skip("macOS-only sandbox behaviour")
    if shutil.which("sandbox-exec") is None:
        pytest.skip("sandbox-exec unavailable")
    probe = subprocess.run(
        ["sandbox-exec", "-p", "(version 1)(allow default)", "true"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("sandbox-exec present but denied by host environment")


def test_declared_root_writable_while_undeclared_path_stays_denied() -> None:
    """Containment is *narrowed* to the declared set, not removed.

    Uses a purpose-built preset-shaped grant rather than the xcode preset so the
    test never writes into the developer's real ``~/Library/Developer``.

    Anchored under /private/var/tmp rather than tmp_path for the same reason as
    the #1443 regression test: the default profile grants /tmp, and the scrubbed
    gate strips TMPDIR, which would make "the undeclared path is denied"
    vacuously pass.
    """
    _require_usable_sandbox_exec()
    with tempfile.TemporaryDirectory(dir="/private/var/tmp") as tmp:
        base = Path(tmp).resolve()
        worktree = base / "worktree"
        worktree.mkdir()
        declared = base / "declared-state"
        declared.mkdir()
        undeclared = base / "undeclared-state"
        undeclared.mkdir()

        def write_attempt(target: Path, extra_write_roots: tuple[Path, ...]) -> int:
            cmd = workspace_effect_sandbox_command(
                ["/usr/bin/touch", str(target / "probe")],
                worktree,
                extra_write_roots=extra_write_roots,
            )
            assert cmd[0] == "sandbox-exec", "sandbox-exec unavailable; cannot assert containment"
            return subprocess.run(
                cmd, capture_output=True, text=True, timeout=30, check=False
            ).returncode

        # A declared out-of-worktree root is writable...
        assert write_attempt(declared, (declared,)) == 0
        assert (declared / "probe").exists()
        # ...while an undeclared out-of-worktree path stays denied.
        assert write_attempt(undeclared, (declared,)) != 0
        assert not (undeclared / "probe").exists()


def test_generated_xcode_profile_is_accepted_by_sandbox_exec() -> None:
    """The widened profile must be syntactically valid Seatbelt, not just text."""
    _require_usable_sandbox_exec()
    with tempfile.TemporaryDirectory(dir="/private/var/tmp") as tmp:
        worktree = Path(tmp).resolve()
        cmd = workspace_effect_sandbox_command(
            ["/usr/bin/true"], worktree, capability_profile="xcode"
        )
        assert cmd[0] == "sandbox-exec"
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
        assert result.returncode == 0, result.stderr


def test_environment_is_not_consulted_for_capability_resolution(tmp_path: Path) -> None:
    """No env var can widen the sandbox — selection is config-only."""
    with patch.dict(os.environ, {"FORGE_SANDBOX_CAPABILITY_PROFILE": "xcode"}, clear=False):
        assert resolve_capabilities(None).is_default


# ── Project-authored additive grants (#2038) ──────────────────────────────


def test_project_grants_are_added_to_the_selected_preset(tmp_path: Path) -> None:
    """The fix-success criterion: a root and a service the presets never anticipated."""
    preset = get_preset("xcode")
    resolved = resolve_capabilities(
        "xcode",
        home=tmp_path,
        write_roots=("~/Library/Preferences", "/opt/toolchain"),
        mach_services=("com.example.toolchaind",),
    )

    assert resolved.profile == "xcode"
    # The preset's own grants survive intact — project grants extend, never replace.
    assert len(resolved.write_roots) == len(preset.write_roots) + 2
    assert (tmp_path.resolve() / "Library/Preferences") in resolved.write_roots
    assert Path("/opt/toolchain") in resolved.write_roots
    assert resolved.mach_services == (*preset.mach_services, "com.example.toolchaind")
    # Provenance is preserved: which grants came from the project is answerable.
    assert resolved.project_write_roots == (
        (tmp_path.resolve() / "Library/Preferences"),
        Path("/opt/toolchain"),
    )
    assert resolved.project_mach_services == ("com.example.toolchaind",)


def test_project_grants_resolve_with_no_preset_selected(tmp_path: Path) -> None:
    """Selecting a preset is not a precondition for declaring a capability."""
    resolved = resolve_capabilities(
        None,
        home=tmp_path,
        write_roots=("~/.cache/toolchain",),
        mach_services=("com.example.toolchaind",),
        system="Darwin",
    )

    assert resolved.profile is None
    assert not resolved.is_default
    assert resolved.write_roots == ((tmp_path.resolve() / ".cache/toolchain"),)
    assert resolved.mach_services == ("com.example.toolchaind",)


def test_project_grants_deduplicate_against_the_preset(tmp_path: Path) -> None:
    """Re-declaring what the preset already grants is a no-op, not a duplicate."""
    plain = resolve_capabilities("xcode", home=tmp_path)
    with_dupes = resolve_capabilities(
        "xcode",
        home=tmp_path,
        write_roots=("~/Library/Developer", "~/Library/Developer"),
        mach_services=("com.apple.lsd.mapdb", "com.apple.lsd.mapdb"),
    )
    assert with_dupes.write_roots == plain.write_roots
    assert with_dupes.mach_services == plain.mach_services


@pytest.mark.parametrize("template", ["/", "~"])
def test_project_write_root_at_a_filesystem_or_home_root_is_denied_by_name(
    tmp_path: Path, template: str
) -> None:
    """A project may extend the sandbox; it may not turn it inside out."""
    with pytest.raises(UnsupportedCapabilityProfileError) as excinfo:
        resolve_capabilities(None, home=tmp_path, write_roots=(template,))
    message = str(excinfo.value)
    # The refusal names the denied root, so the operator knows what to remove.
    assert template in message
    assert "may not include a filesystem or home root" in message


def test_project_mach_service_on_linux_fails_closed_naming_the_services(
    tmp_path: Path,
) -> None:
    """bwrap has no mach-lookup axis — declaring one there must refuse, not drop it."""
    with pytest.raises(UnsupportedCapabilityProfileError) as excinfo:
        resolve_capabilities(
            None, home=tmp_path, system="Linux", mach_services=("com.example.toolchaind",)
        )
    message = str(excinfo.value)
    assert "com.example.toolchaind" in message
    assert "Linux" in message


def test_project_write_roots_alone_resolve_on_linux(tmp_path: Path) -> None:
    """A write root has a bwrap expression, so it must not be refused there."""
    resolved = resolve_capabilities(
        None, home=tmp_path, system="Linux", write_roots=("/opt/toolchain",)
    )
    assert resolved.write_roots == (Path("/opt/toolchain"),)


def test_audit_payload_names_project_declared_grants(tmp_path: Path) -> None:
    """Convention: the values that drove the run are visible in the audit trail."""
    payload = resolve_capabilities(
        "xcode", home=tmp_path, write_roots=("/opt/toolchain",)
    ).audit_payload()
    assert "/opt/toolchain" in payload["write_roots"]
    assert payload["project_write_roots"] == ["/opt/toolchain"]
    # Absent — not empty — when nothing was project-declared, so the shape a
    # reader saw before project grants existed is unchanged.
    preset_only = resolve_capabilities("xcode", home=tmp_path).audit_payload()
    assert "project_write_roots" not in preset_only


def test_macos_profile_grants_project_roots_and_services(tmp_path: Path) -> None:
    """The declared grants reach the generated Seatbelt profile, deny-default intact."""
    extra_root = tmp_path / "toolchain-state"
    with (
        patch("theforge.runners.sandbox._SYSTEM", "Darwin"),
        patch("theforge.runners.sandbox._sandbox_available", return_value=True),
    ):
        wrapped = workspace_effect_sandbox_command(
            ["true"],
            tmp_path,
            capability_profile="xcode",
            capability_write_roots=(str(extra_root),),
            capability_mach_services=("com.example.toolchaind",),
        )
    profile_text = wrapped[2]
    assert str(extra_root.resolve()) in profile_text
    assert '(allow mach-lookup (global-name "com.example.toolchaind"))' in profile_text
    # The preset's own grants are still there, and containment still denies by default.
    assert '(allow mach-lookup (global-name "com.apple.lsd.mapdb"))' in profile_text
    assert "(deny default)" in profile_text
    assert "(allow default)" not in profile_text


def test_project_grants_apply_with_no_preset_selected(tmp_path: Path) -> None:
    """Inline-only grants must reach the profile — not require a preset to carry them."""
    extra_root = tmp_path / "toolchain-state"
    with (
        patch("theforge.runners.sandbox._SYSTEM", "Darwin"),
        patch("theforge.runners.sandbox._sandbox_available", return_value=True),
    ):
        baseline = workspace_effect_sandbox_command(["true"], tmp_path)[2]
        widened = workspace_effect_sandbox_command(
            ["true"], tmp_path, capability_write_roots=(str(extra_root),)
        )[2]
    assert str(extra_root.resolve()) not in baseline
    assert str(extra_root.resolve()) in widened


def test_project_mach_service_refused_by_the_linux_backend(tmp_path: Path) -> None:
    with (
        patch("theforge.runners.sandbox._SYSTEM", "Linux"),
        patch("theforge.runners.sandbox._sandbox_available", return_value=True),
        pytest.raises(SandboxCapabilityError, match="com.example.toolchaind"),
    ):
        workspace_effect_sandbox_command(
            ["true"], tmp_path, capability_mach_services=("com.example.toolchaind",)
        )


def test_generated_profile_with_project_grants_is_accepted_by_sandbox_exec() -> None:
    """A project-declared grant must produce valid Seatbelt, not just plausible text."""
    _require_usable_sandbox_exec()
    with tempfile.TemporaryDirectory(dir="/private/var/tmp") as tmp:
        worktree = Path(tmp).resolve()
        extra_root = worktree / "toolchain-state"
        extra_root.mkdir()
        cmd = workspace_effect_sandbox_command(
            ["/usr/bin/true"],
            worktree,
            capability_write_roots=(str(extra_root),),
            capability_mach_services=("com.example.toolchaind",),
        )
        assert cmd[0] == "sandbox-exec"
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
        assert result.returncode == 0, result.stderr
