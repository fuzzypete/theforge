"""Substrate provenance — name the runtime executing this `forge` invocation.

The dogfood failure mode that motivates this module: TheForge's release script
historically installed the cut RC into the operator's default Python env,
silently overwriting any prior editable install. Subsequent operator activity
then ran against an unannounced substrate, and schema divergence between source
and installed code surfaced as cross-environment kwarg mismatches with no
operator-readable signal naming which runtime was in effect.

Every operator-launched action emits substrate provenance so the operator can
name the executing runtime within seconds: binary path, theforge.__file__,
package version, and (when the install is editable) the source-tree git ref.

The mismatch warning fires when the operator launches `forge` from a checkout
of theforge whose pyproject version disagrees with the installed package's
version. Non-blocking by default; ``--force`` (or any caller-supplied bypass)
suppresses it.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Substrate:
    binary: str
    package_file: str
    version: str
    editable: bool
    source_root: str | None
    git_ref: str | None


@dataclass(frozen=True)
class CheckoutProvenance:
    root: str
    pyproject_file: str
    version: str | None


def _read_direct_url_editable(dist_files_root: Path) -> tuple[bool, str | None]:
    """Best-effort detect editable install via PEP 610 direct_url.json.

    Returns (editable, source_root_or_none). If the metadata is missing or
    unreadable, returns (False, None).
    """
    try:
        from importlib.metadata import distribution

        dist = distribution("theforge")
    except Exception:
        return False, None

    try:
        raw = dist.read_text("direct_url.json")
    except Exception:
        raw = None
    if not raw:
        return False, None

    import json

    try:
        info = json.loads(raw)
    except Exception:
        return False, None

    dir_info = info.get("dir_info") or {}
    url = info.get("url") or ""
    if not dir_info.get("editable"):
        return False, None
    if not url.startswith("file://"):
        return True, None
    return True, url[len("file://") :]


def _git_ref(source_root: Path) -> str | None:
    if not source_root.exists():
        return None
    try:
        head = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if head.returncode != 0:
            return None
        ref = (head.stdout or "").strip()
        if not ref or ref == "HEAD":
            sha = subprocess.run(
                ["git", "-C", str(source_root), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            return (sha.stdout or "").strip() or None
        return ref
    except (OSError, subprocess.TimeoutExpired):
        return None


def _resolve_running_binary() -> str:
    """Return the absolute path of the `forge` entry point that started this
    process. Prefers ``sys.argv[0]`` (the actual executable invoked) over
    ``shutil.which('forge')`` (which returns the first forge on PATH and can
    point at a different binary entirely).

    The dogfood ladder this fix introduces invokes a path-qualified RC binary
    while the operator's PATH still resolves ``forge`` to the shell-default;
    PATH-based detection would name the wrong runtime in that workflow and
    silently violate the AC that every sprint launch reports the binary that
    is actually executing.
    """
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0:
        p = Path(argv0)
        if p.is_file():
            return str(p.resolve())
        # argv0 may be a bare name resolved via PATH (e.g. invoked as just
        # ``forge``); resolve by name through PATH.
        which = shutil.which(argv0)
        if which:
            return which
    on_path = shutil.which("forge")
    if on_path:
        return on_path
    return sys.executable


def detect_substrate() -> Substrate:
    """Inspect the running interpreter's theforge install and return Substrate."""
    import theforge

    pkg_file = getattr(theforge, "__file__", "") or ""
    version = getattr(theforge, "__version__", "0.0.0-dev")
    binary = _resolve_running_binary()

    editable, source_root_str = _read_direct_url_editable(Path(pkg_file).parent)
    source_root: str | None = None
    git_ref: str | None = None
    if editable:
        if source_root_str:
            source_root = source_root_str
            git_ref = _git_ref(Path(source_root_str))
        else:
            # Fall back to the package directory's parent — useful when the
            # direct_url.json is missing the file:// URL.
            cand = Path(pkg_file).parent.parent
            if cand.exists():
                source_root = str(cand)
                git_ref = _git_ref(cand)

    return Substrate(
        binary=binary,
        package_file=pkg_file,
        version=version,
        editable=editable,
        source_root=source_root,
        git_ref=git_ref,
    )


def _read_checkout_provenance(pyproject: Path) -> CheckoutProvenance | None:
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
    except (OSError, tomllib.TOMLDecodeError):
        return None

    project = data.get("project")
    if not isinstance(project, dict) or project.get("name") != "theforge":
        return None

    version = project.get("version")
    if version is not None:
        version = str(version)

    return CheckoutProvenance(
        root=str(pyproject.parent),
        pyproject_file=str(pyproject),
        version=version,
    )


def _find_checkout_provenance(cwd: Path) -> CheckoutProvenance | None:
    for parent in [cwd, *cwd.parents]:
        pyproject = parent / "pyproject.toml"
        if not pyproject.is_file():
            continue
        checkout = _read_checkout_provenance(pyproject)
        if checkout is not None:
            return checkout
    return None


def _checkout_version_ahead_of_runtime(checkout_version: str, runtime_version: str) -> bool | None:
    try:
        from packaging.version import InvalidVersion, Version
    except Exception:
        return None

    try:
        return Version(checkout_version) > Version(runtime_version)
    except InvalidVersion:
        return None


def _runtime_schema_catalog_paths(sub: Substrate) -> tuple[str, str] | None:
    package_file = sub.package_file
    if not package_file:
        return None
    package_root = Path(package_file).resolve().parent
    return (
        str(package_root / "config" / "model_catalog.py"),
        str(package_root / "config" / "data" / "models.yaml"),
    )


def format_provenance_lines(sub: Substrate | None = None) -> list[str]:
    """Return operator-facing substrate lines for launch banners.

    Lines are prefixed ``[forge] Substrate: ...`` and each line is self-contained
    so the operator can read any single line and know which runtime is active.
    """
    if sub is None:
        sub = detect_substrate()
    lines = []
    if sub.editable and sub.source_root:
        ref = sub.git_ref or "unknown"
        lines.append(
            f"[forge] Substrate: {sub.package_file} "
            f"(editable, source @ {sub.source_root}, ref={ref}, version={sub.version})"
        )
    else:
        lines.append(f"[forge] Substrate: {sub.package_file} (installed, version={sub.version})")
    lines.append(f"[forge] Binary:    {sub.binary}")
    return lines


def detect_mismatch(cwd: Path | None = None, sub: Substrate | None = None) -> str | None:
    """Return a one-line warning when the running substrate's code path is
    incoherent with the theforge checkout in CWD. Returns None on the documented
    dogfood happy path (a released RC orchestrating a newer checkout).

    The warning fires only when ``sub.editable`` is True — i.e., the running
    ``forge`` is itself resolving package code out of a source tree — and that
    source tree's version disagrees with the checkout in CWD. That shape means
    the operator's ``forge`` is executing code that doesn't match the checkout
    they're operating on (stale build of this checkout, or an editable install
    of a *different* theforge repo shadowing this one).

    A non-editable substrate (released wheel) cannot reach into the checkout's
    source at runtime, so version-string drift between a released RC and a
    newer dev checkout is the *correct* shape of the dogfood model — not
    incoherence — and is intentionally silent.
    """
    if sub is None:
        sub = detect_substrate()

    cwd = cwd or Path.cwd()
    checkout = _find_checkout_provenance(cwd)
    if checkout is None:
        return None

    # Released (non-editable) substrate orchestrating a newer-version theforge
    # checkout is the documented dogfood model — the orchestrator runtime
    # never imports the patient's source, so a version-string difference is
    # expected. Stay silent.
    if not sub.editable:
        return None

    src_version = checkout.version
    if not src_version:
        return None

    if src_version == sub.version:
        return None

    source_note = f" editable source @ {sub.source_root};" if sub.source_root else ""
    return (
        f"[forge] WARNING: substrate version {sub.version} (binary: {sub.binary});"
        f"{source_note} does not match this checkout's pyproject version "
        f"{src_version} (cwd: {checkout.root}). The editable install resolving as `forge` "
        f"is out of sync with this checkout — either a stale build of this tree "
        f"or an editable install of a different theforge repo is shadowing it. "
        f"Pass --force to bypass."
    )


def format_config_validation_provenance_lines(
    *,
    config_path: Path | None = None,
    cwd: Path | None = None,
    sub: Substrate | None = None,
) -> list[str]:
    """Return extra context for a config ValueError caused by runtime drift.

    Silent on the documented dogfood happy path unless a structural config
    failure occurs under a non-editable runtime whose version differs from the
    checkout version that owns ``forge.yaml``.
    """
    if sub is None:
        sub = detect_substrate()

    start = cwd or (config_path.parent if config_path is not None else Path.cwd())
    checkout = _find_checkout_provenance(start)
    if checkout is None or checkout.version is None:
        return []
    if sub.editable or checkout.version == sub.version:
        return []

    schema_catalog = _runtime_schema_catalog_paths(sub)
    version_cmp = _checkout_version_ahead_of_runtime(checkout.version, sub.version)
    if version_cmp is False:
        relation = "does not match it"
    else:
        relation = "appears ahead of it"

    lines = [
        "[forge] Runtime/checkout mismatch: this installed runtime is validating a "
        "different checkout's forge.yaml.",
        f"[forge] Runtime binary:  {sub.binary}",
        f"[forge] Runtime package: {sub.package_file}",
        f"[forge] Runtime version: {sub.version}",
    ]
    if schema_catalog is not None:
        schema_path, catalog_path = schema_catalog
        lines.extend(
            [
                f"[forge] Runtime schema:  {schema_path}",
                f"[forge] Runtime catalog: {catalog_path}",
            ]
        )
    lines.extend(
        [
            f"[forge] Checkout root:   {checkout.root}",
            f"[forge] Checkout version: {checkout.version}",
            f"[forge] This installed runtime is judging a checkout that {relation}. "
            "Use the checkout-local launcher (for example `.venv/bin/forge`) or "
            "repoint the managed launcher, then retry.",
        ]
    )
    return lines


def emit_provenance(
    *,
    file=sys.stderr,
    cwd: Path | None = None,
    bypass_mismatch: bool = False,
) -> None:
    """Print substrate provenance lines and an optional mismatch warning."""
    sub = detect_substrate()
    for line in format_provenance_lines(sub):
        print(line, file=file, flush=True)
    if bypass_mismatch:
        return
    warning = detect_mismatch(cwd=cwd, sub=sub)
    if warning:
        print(warning, file=file, flush=True)
