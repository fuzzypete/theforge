"""CLI-only config override helpers."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from theforge.config import ForgeConfig


def apply_base_branch_override(
    config: ForgeConfig | Any, base_branch: str | None
) -> ForgeConfig | Any:
    """Return config with workspace.base_branch overridden for this invocation."""
    if not base_branch:
        return config
    return dataclasses.replace(
        config,
        workspace=dataclasses.replace(config.workspace, base_branch=base_branch),
    )
