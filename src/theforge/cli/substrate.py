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
    """Return a one-line warning when CWD's theforge checkout disagrees with the
    binary's reported version. Returns None when no checkout context applies or
    the versions agree.

    The check is deliberately conservative: it only fires when the CWD is itself
    a theforge source tree (pyproject.toml ``name = "theforge"``) AND its declared
    version differs from the running package version. This catches the dominant
    failure mode (operator runs ``forge sprint`` from theforge's own repo while
    the installed runtime is a different version) without false-positiving on
    operators using forge against unrelated projects.
    """
    if sub is None:
        sub = detect_substrate()

    cwd = cwd or Path.cwd()
    pyproject = cwd / "pyproject.toml"
    if not pyproject.is_file():
        return None

    try:
        text = pyproject.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    if 'name = "theforge"' not in text:
        return None

    src_version: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("version") and "=" in stripped:
            _, _, rhs = stripped.partition("=")
            src_version = rhs.strip().strip('"').strip("'")
            break

    if not src_version:
        return None

    if src_version == sub.version:
        return None

    # If editable and source_root is the same as cwd, the version matches by
    # definition (importlib reads the same pyproject) — but a stale build can
    # diverge. Either way, we surface the warning.
    return (
        f"[forge] WARNING: substrate version {sub.version} (binary: {sub.binary}) "
        f"does not match this checkout's pyproject version {src_version} "
        f"(cwd: {cwd}). Editable installs may need `pip install -e .` to refresh; "
        "released installs may need re-cutting. Pass --force to bypass."
    )


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
