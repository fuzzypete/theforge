"""TheForge — Deterministic multi-LLM development orchestrator."""

# Force multiprocessing to use the spawn start method process-wide. Defense
# in depth against macOS Objective-C fork-safety crashes (the Obj-C runtime
# aborts when a child of fork() finds Foundation initialization in flight on
# another thread). subprocess.Popen has its own posix_spawn fast path; this
# guard covers any future code that introduces multiprocessing.Pool/Process.
try:
    import multiprocessing as _multiprocessing

    _multiprocessing.set_start_method("spawn", force=True)
except (RuntimeError, ValueError):
    pass

try:
    from importlib.metadata import version as _version

    __version__ = _version("theforge")
except Exception:
    __version__ = "0.0.0-dev"

__all__ = ["__version__"]
