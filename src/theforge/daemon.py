"""Forge daemon — persistent background sprint runner.

Manages a single sprint queue, prevents duplicate runs, logs crash context,
and exposes status via unix socket and daemon.json.

Usage:
    forge daemon start          # daemonize and start
    forge daemon stop           # graceful shutdown
    forge daemon status         # show current state
    forge daemon install        # install launchd plist (macOS)
    forge daemon uninstall      # remove launchd plist (macOS)
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import signal
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from theforge.daemon_launchd import (  # noqa: F401
    _LAUNCHD_LABEL,
    _LAUNCHD_PLIST_PATH,
    install_launchd,
    uninstall_launchd,
)

if TYPE_CHECKING:
    from .config import ForgeConfig

# ── Constants ──────────────────────────────────────────────────────────

_PID_FILE = ".forge/daemon.pid"
_SOCK_FILE = ".forge/daemon.sock"
_STATE_FILE = ".forge/daemon.json"
_CRASH_LOG = ".forge/logs/crashes.jsonl"
_MAX_COMPLETED = 20  # keep last N completed sprint summaries


# ── PID helpers ────────────────────────────────────────────────────────


def _read_pid(pid_file: Path) -> int | None:
    """Read PID from pid_file, return None if missing or invalid."""
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def is_daemon_running(forge_root: Path) -> bool:
    """Return True if the daemon process is alive."""
    pid_file = forge_root / _PID_FILE
    pid = _read_pid(pid_file)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        # Stale PID file — clean it up
        try:
            pid_file.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    except PermissionError:
        # Process exists but we don't have permission to signal it
        return True


# ── Atomic state file writers ──────────────────────────────────────────


def _write_daemon_json(forge_root: Path, state_dict: dict) -> None:
    """Atomically write daemon.json via tempfile + os.replace."""
    state_path = forge_root / _STATE_FILE
    state_path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(state_dict, indent=2, default=str)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=state_path.parent,
        delete=False,
        suffix=".tmp",
    ) as tf:
        tf.write(data)
        tmp_path = tf.name
    try:
        os.replace(tmp_path, state_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _append_crash_log(forge_root: Path, crash: dict) -> None:
    """Append a JSON line to crashes.jsonl."""
    crash_path = forge_root / _CRASH_LOG
    crash_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(crash_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(crash, default=str) + "\n")
    except OSError:
        pass


def _daemon_ntfy_notify(config: "ForgeConfig", crash: dict) -> None:
    """Send ntfy notification on daemon crash. Fails silently."""
    if config.notifications.ntfy is None:
        return
    ntfy = config.notifications.ntfy
    slug = crash.get("spec", "unknown")
    phase = crash.get("phase", "UNKNOWN")
    cost = crash.get("cost_at_crash", 0.0)
    title = f"TheForge daemon CRASHED — {slug}"
    body = f"{phase}\nCost at crash: ${cost:.2f}"
    try:
        req = urllib.request.Request(
            ntfy.url,
            data=body.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": ntfy.priority,
                "Content-Type": "text/plain; charset=utf-8",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


# ── State update callback ───────────────────────────────────────────────


def make_state_update_fn(
    forge_root: Path,
    current_state: dict,
    lock: threading.Lock,
) -> Callable[[dict], None]:
    """Return a thread-safe callback that merges updates into current_state.

    Also flushes daemon.json on each call.
    """

    def _update(updates: dict) -> None:
        with lock:
            current_state.update(updates)
            try:
                # Build daemon.json snapshot from current state
                snapshot = dict(current_state)
                _write_daemon_json(forge_root, snapshot)
            except Exception:
                pass

    return _update


# ── DaemonServer ────────────────────────────────────────────────────────


class DaemonServer:
    """Asyncio-based unix socket server that manages sprint queue and execution."""

    def __init__(self, forge_root: Path, config: "ForgeConfig") -> None:
        self.forge_root = forge_root
        self.config = config
        self._queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
        self._running_spec: str | None = None
        self._queued_specs: list[str] = []  # ordered list for FIFO display
        self._completed: list[dict] = []
        self._shutdown_event: asyncio.Event = asyncio.Event()
        self._current_sprint_task: asyncio.Task | None = None
        self._state_lock = threading.Lock()
        self._daemon_started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self._daemon_start_monotonic = time.monotonic()
        self._current_state: dict = {
            "pid": os.getpid(),
            "started_at": self._daemon_started_at,
            "current_sprint": None,
            "queue": [],
            "completed": [],
        }

    def _flush_state(self) -> None:
        """Write current state to daemon.json."""
        try:
            _write_daemon_json(self.forge_root, self._current_state)
        except Exception:
            pass

    def _state_update(self, updates: dict) -> None:
        """Merge updates into current state and flush."""
        with self._state_lock:
            self._current_state.update(updates)
            self._flush_state()

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a single client connection."""
        try:
            data = await asyncio.wait_for(reader.readline(), timeout=10.0)
            if not data:
                return
            try:
                msg = json.loads(data.decode("utf-8"))
            except json.JSONDecodeError:
                writer.write(json.dumps({"ok": False, "error": "invalid JSON"}).encode() + b"\n")
                await writer.drain()
                return

            action = msg.get("action")
            if action == "submit":
                response = await self.handle_submit(msg)
            elif action == "status":
                response = self.handle_status()
            elif action == "stop":
                response = self.handle_stop()
            else:
                response = {"ok": False, "error": f"unknown action: {action}"}

            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def handle_submit(self, msg: dict) -> dict:
        """Enqueue a sprint if not already running/queued."""
        manifest = msg.get("manifest", "")
        args = msg.get("args", {})

        # Extract slug for dedup; use manifest stem as fallback
        slug = args.get("slug") or Path(manifest).stem

        if slug == self._running_spec:
            return {"ok": False, "error": f"spec '{slug}' is already running"}
        if slug in self._queued_specs:
            return {"ok": False, "error": f"spec '{slug}' is already queued"}

        self._queued_specs.append(slug)
        await self._queue.put((manifest, args))

        # Update queue in state
        with self._state_lock:
            self._current_state["queue"] = list(self._queued_specs)
            self._flush_state()

        return {"ok": True, "queued": slug, "position": self._queue.qsize()}

    def handle_status(self) -> dict:
        """Return current daemon state."""
        with self._state_lock:
            return {"ok": True, "state": dict(self._current_state)}

    def handle_stop(self) -> dict:
        """Signal daemon to shut down."""
        self._shutdown_event.set()
        return {"ok": True, "message": "shutdown initiated"}

    async def _run_loop(self) -> None:
        """Main sprint execution loop."""
        while not self._shutdown_event.is_set():
            try:
                # Wait for next item with a timeout to check shutdown
                try:
                    manifest, args = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                slug = args.get("slug") or Path(manifest).stem
                self._running_spec = slug
                try:
                    self._queued_specs.remove(slug)
                except ValueError:
                    pass

                started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
                sprint_state: dict = {
                    "manifest": manifest,
                    "spec": slug,
                    "phase": "STARTING",
                    "iteration": 0,
                    "cost_usd": 0.0,
                    "started_at": started_at,
                }
                self._state_update(
                    {
                        "current_sprint": sprint_state,
                        "queue": list(self._queued_specs),
                    }
                )

                # Make a state_update_fn that merges into sprint_state
                def _make_sprint_update(ss: dict) -> Callable[[dict], None]:
                    def _fn(updates: dict) -> None:
                        with self._state_lock:
                            ss.update(updates)
                            self._current_state["current_sprint"] = dict(ss)
                            self._flush_state()

                    return _fn

                sprint_update_fn = _make_sprint_update(sprint_state)

                _run_start = time.monotonic()
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None,
                        self._execute_sprint,
                        manifest,
                        args,
                        sprint_update_fn,
                    )
                    elapsed = time.monotonic() - _run_start
                    completed_entry = {
                        "spec": slug,
                        "manifest": manifest,
                        "outcome": "done",
                        "cost_usd": sprint_state.get("cost_usd", 0.0),
                        "duration_s": round(elapsed, 1),
                        "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    }
                except Exception as exc:
                    elapsed = time.monotonic() - _run_start
                    crash = {
                        "signal": None,
                        "spec": slug,
                        "phase": sprint_state.get("phase", "UNKNOWN"),
                        "iteration": sprint_state.get("iteration", 0),
                        "cost_at_crash": sprint_state.get("cost_usd", 0.0),
                        "uptime_seconds": round(elapsed, 1),
                        "last_log_event": f"exception: {type(exc).__name__}: {exc}",
                        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    }
                    _append_crash_log(self.forge_root, crash)
                    _daemon_ntfy_notify(self.config, crash)
                    completed_entry = {
                        "spec": slug,
                        "manifest": manifest,
                        "outcome": "crashed",
                        "cost_usd": sprint_state.get("cost_usd", 0.0),
                        "duration_s": round(elapsed, 1),
                        "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "error": str(exc),
                    }

                self._completed.append(completed_entry)
                if len(self._completed) > _MAX_COMPLETED:
                    self._completed = self._completed[-_MAX_COMPLETED:]

                self._running_spec = None
                self._state_update(
                    {
                        "current_sprint": None,
                        "queue": list(self._queued_specs),
                        "completed": list(self._completed),
                    }
                )
                self._queue.task_done()

            except Exception as _loop_exc:
                print(f"[daemon] run_loop error (continuing): {_loop_exc}", flush=True)

    def _execute_sprint(
        self,
        manifest: str,
        args: dict,
        state_update_fn: Callable[[dict], None],
    ) -> None:
        """Execute a sprint in a thread executor. Called via run_in_executor."""
        from .config import load_config
        from .sprint import run_sprint

        # Find config — use forge_root/forge.yaml or config passed in args
        config_path_str = args.get("config")
        if config_path_str:
            config_path = Path(config_path_str)
        else:
            config_path = self.forge_root / "forge.yaml"

        config = load_config(config_path)
        manifest_path = Path(manifest)
        if not manifest_path.is_absolute():
            manifest_path = (self.forge_root / manifest).resolve()

        run_sprint(
            config,
            manifest_path,
            auto_merge=args.get("auto_merge", False),
            interactive=False,  # daemon mode is non-interactive
            notify=args.get("notify", True),
            resume=args.get("resume", False),
            state_update_fn=state_update_fn,
        )

    async def serve(self) -> None:
        """Start the unix socket server and run the sprint loop."""
        sock_path = self.forge_root / _SOCK_FILE
        sock_path.parent.mkdir(parents=True, exist_ok=True)

        # Remove stale socket
        if sock_path.exists():
            try:
                sock_path.unlink()
            except OSError:
                pass

        server = await asyncio.start_unix_server(
            self.handle_client,
            path=str(sock_path),
        )

        self._flush_state()

        loop_task = asyncio.create_task(self._run_loop())
        try:
            async with server:
                await self._shutdown_event.wait()
        finally:
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass
            try:
                sock_path.unlink(missing_ok=True)
            except OSError:
                pass


# ── Daemonization ──────────────────────────────────────────────────────


def daemonize(pid_file: Path, log_file: Path) -> None:
    """Classic UNIX double-fork daemonization.

    Raises RuntimeError if the daemon is already running.
    After this call returns in the grandchild, the process is fully daemonized.
    The parent and intermediate child both exit.
    """
    # First fork
    try:
        pid = os.fork()
    except AttributeError:
        raise RuntimeError("os.fork() not available on this platform")

    if pid > 0:
        # Parent exits — wait for intermediate child to avoid zombie
        os.waitpid(pid, 0)
        sys.exit(0)

    # Intermediate child: become session leader
    os.setsid()

    # Second fork — ensures we're not a session leader and can't acquire a tty
    try:
        pid = os.fork()
    except AttributeError:
        pass
    else:
        if pid > 0:
            # Intermediate child exits
            sys.exit(0)

    # Grandchild: write PID file
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    # Redirect stdio
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as lf:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(lf.fileno(), sys.stdout.fileno())
        os.dup2(lf.fileno(), sys.stderr.fileno())
    with open(os.devnull) as null:
        os.dup2(null.fileno(), sys.stdin.fileno())


# ── Public entry points ────────────────────────────────────────────────


def start_daemon(config: "ForgeConfig", *, no_daemonize: bool = False) -> None:
    """Daemonize and start the server.

    Args:
        config: The forge configuration.
        no_daemonize: When True, skip double-fork and run in foreground.
            Used by launchd (which manages the process lifecycle itself).

    Raises RuntimeError if a daemon is already running.
    """
    forge_root = config.project_root
    pid_file = forge_root / _PID_FILE
    log_file = forge_root / ".forge" / "logs" / "daemon.log"
    sock_path = forge_root / _SOCK_FILE

    if is_daemon_running(forge_root):
        pid = _read_pid(pid_file)
        raise RuntimeError(f"Daemon already running (PID {pid})")

    # Clean up any stale socket from a previous ungraceful crash
    if sock_path.exists():
        try:
            sock_path.unlink()
        except OSError:
            pass

    # Ensure directories exist before forking
    (forge_root / ".forge" / "logs").mkdir(parents=True, exist_ok=True)

    if not no_daemonize:
        daemonize(pid_file, log_file)
    else:
        # Foreground mode (launchd): just write the PID file
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()), encoding="utf-8")

    # ── Daemon process continues here (grandchild or foreground) ──────
    _start_monotonic = time.monotonic()
    print(f"[daemon] Starting (PID {os.getpid()})", flush=True)

    server = DaemonServer(forge_root, config)

    # SIGTERM handler: log crash context and exit cleanly
    def _sigterm_handler(signum: int, frame: object) -> None:
        print("[daemon] Received SIGTERM — shutting down", flush=True)
        uptime = round(time.monotonic() - _start_monotonic, 1)
        current = server._current_state.get("current_sprint")
        if current:
            crash = {
                "signal": f"SIGTERM ({signum})",
                "spec": current.get("spec", "unknown"),
                "phase": current.get("phase", "UNKNOWN"),
                "iteration": current.get("iteration", 0),
                "cost_at_crash": current.get("cost_usd", 0.0),
                "uptime_seconds": uptime,
                "last_log_event": f"SIGTERM received during phase {current.get('phase')}",
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
            _append_crash_log(forge_root, crash)
            _daemon_ntfy_notify(config, crash)
        server._shutdown_event.set()
        try:
            pid_file.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            sock_path.unlink(missing_ok=True)
        except OSError:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT, _sigterm_handler)

    asyncio.run(server.serve())


def stop_daemon(forge_root: Path) -> None:
    """Send SIGTERM to the daemon and wait up to 10s for it to exit."""
    pid_file = forge_root / _PID_FILE
    pid = _read_pid(pid_file)
    if pid is None:
        raise RuntimeError("No daemon PID file found — daemon not running?")

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pid_file.unlink(missing_ok=True)
        raise RuntimeError(f"No process with PID {pid} — daemon already stopped?")

    # Wait up to 10s for the process to exit
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            # Gone
            pid_file.unlink(missing_ok=True)
            return
        except PermissionError:
            # Still running
            pass
        time.sleep(0.3)

    raise RuntimeError(f"Daemon (PID {pid}) did not exit within 10 seconds")


def get_daemon_status(forge_root: Path) -> dict:
    """Read daemon.json or return {'running': False} if missing/stale."""
    state_path = forge_root / _STATE_FILE
    if not is_daemon_running(forge_root):
        # Try to return last known state with running=false
        if state_path.exists():
            try:
                data = json.loads(state_path.read_text(encoding="utf-8"))
                data["running"] = False
                return data
            except (json.JSONDecodeError, OSError):
                pass
        return {"running": False}

    if state_path.exists():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            data["running"] = True
            return data
        except (json.JSONDecodeError, OSError):
            pass

    return {"running": True, "current_sprint": None, "queue": [], "completed": []}


def submit_sprint(forge_root: Path, manifest: str, args: dict) -> dict:
    """Connect to daemon socket and submit a sprint. Returns server response."""
    sock_path = forge_root / _SOCK_FILE
    if not sock_path.exists():
        return {"ok": False, "error": "Daemon socket not found — is the daemon running?"}

    msg = json.dumps({"action": "submit", "manifest": manifest, "args": args}) + "\n"

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(10.0)
            sock.connect(str(sock_path))
            sock.sendall(msg.encode("utf-8"))
            # Read response
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            line = buf.split(b"\n")[0]
            return json.loads(line.decode("utf-8"))
    except (OSError, json.JSONDecodeError, socket.timeout) as exc:
        return {"ok": False, "error": str(exc)}


def _daemon_socket_command(forge_root: Path, msg: dict) -> dict:
    """Send a command to the daemon socket and return the response."""
    sock_path = forge_root / _SOCK_FILE
    if not sock_path.exists():
        return {"ok": False, "error": "Daemon socket not found"}

    raw = json.dumps(msg) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(10.0)
            sock.connect(str(sock_path))
            sock.sendall(raw.encode("utf-8"))
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            line = buf.split(b"\n")[0]
            return json.loads(line.decode("utf-8"))
    except (OSError, json.JSONDecodeError, socket.timeout) as exc:
        return {"ok": False, "error": str(exc)}


# macOS launchd integration lives in daemon_launchd.py; re-exported above.
