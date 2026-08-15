"""``python -m theforge.structural_decay_observer`` entry point."""

from __future__ import annotations

from .report import main

if __name__ == "__main__":  # pragma: no cover - manual spike entry point
    raise SystemExit(main())
