"""CLI-only config override helpers."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from theforge.config.provenance import VALUE_SOURCE_CLI_OVERRIDE, refresh_provenance

if TYPE_CHECKING:
    from theforge.config import ForgeConfig


def apply_base_branch_override(
    config: ForgeConfig | Any, base_branch: str | None
) -> ForgeConfig | Any:
    """Return config with workspace.base_branch overridden for this invocation."""
    if not base_branch:
        return config
    updated = dataclasses.replace(
        config,
        workspace=dataclasses.replace(config.workspace, base_branch=base_branch),
    )
    return refresh_provenance(
        updated,
        source_updates={"workspace.base_branch": VALUE_SOURCE_CLI_OVERRIDE},
    )
