"""CLI-only config override helpers."""

from __future__ import annotations

import dataclasses


def apply_base_branch_override(config: object, base_branch: str | None) -> object:
    """Return config with workspace.base_branch overridden for this invocation."""
    if not base_branch:
        return config
    return dataclasses.replace(
        config,
        workspace=dataclasses.replace(config.workspace, base_branch=base_branch),
    )
