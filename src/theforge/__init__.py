"""TheForge — Deterministic multi-LLM development orchestrator."""

try:
    from importlib.metadata import version as _version

    __version__ = _version("theforge")
except Exception:
    __version__ = "0.0.0-dev"
