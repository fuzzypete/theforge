"""macOS launchd integration for the forge daemon.

Provides install_launchd and uninstall_launchd which write and manage
~/Library/LaunchAgents/com.theforge.daemon.plist.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_LAUNCHD_LABEL = "com.theforge.daemon"
_LAUNCHD_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"


def install_launchd(forge_root: Path, forge_bin: Path) -> Path:
    """Write ~/Library/LaunchAgents/com.theforge.daemon.plist and load it."""
    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{forge_bin}</string>
        <string>daemon</string>
        <string>start</string>
        <string>--no-daemonize</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{forge_root}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{forge_root / ".forge" / "logs" / "daemon.log"}</string>
    <key>StandardErrorPath</key>
    <string>{forge_root / ".forge" / "logs" / "daemon.log"}</string>
</dict>
</plist>
"""
    _LAUNCHD_PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LAUNCHD_PLIST_PATH.write_text(plist_content, encoding="utf-8")

    try:
        subprocess.run(
            ["launchctl", "load", str(_LAUNCHD_PLIST_PATH)],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise RuntimeError(f"launchctl load failed: {exc}") from exc

    return _LAUNCHD_PLIST_PATH


def uninstall_launchd() -> None:
    """Unload and remove the launchd plist."""
    if _LAUNCHD_PLIST_PATH.exists():
        try:
            subprocess.run(
                ["launchctl", "unload", str(_LAUNCHD_PLIST_PATH)],
                check=False,
                capture_output=True,
            )
        except FileNotFoundError:
            pass
        _LAUNCHD_PLIST_PATH.unlink(missing_ok=True)
