"""Parser for <forge_handoff> structured blocks in agent output.

The agent emits a single XML-tagged YAML block at the end of its final message.
This module extracts and validates that block, returning a dict on success.
Absence of the block is not an error — the caller decides the fallback policy.
"""

from __future__ import annotations

import re

import yaml

OPEN_TAG = "<forge_handoff>"
CLOSE_TAG = "</forge_handoff>"

_REQUIRED_KEYS = frozenset(
    {"summary", "commits", "acceptance_criteria", "story_deviations", "deferred_items"}
)

_FENCED_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)


class ParseError(Exception):
    """Raised when a <forge_handoff> block is present but malformed."""


def extract_dev_handoff(text: str) -> dict | None:
    """Extract and validate the <forge_handoff> block from agent output text.

    Strips fenced code blocks first so agents quoting the block spec in their
    prose don't cause false matches.

    Returns None when no block is found (absence handled by caller).
    Raises ParseError when the block is present but malformed.
    """
    stripped = _FENCED_BLOCK_RE.sub("", text)

    open_positions = [m.start() for m in re.finditer(re.escape(OPEN_TAG), stripped)]
    close_positions = [m.start() for m in re.finditer(re.escape(CLOSE_TAG), stripped)]

    if not open_positions and not close_positions:
        return None

    if len(open_positions) > 1 or len(close_positions) > 1:
        raise ParseError(
            f"Found {len(open_positions)} <forge_handoff> opening tag(s) and "
            f"{len(close_positions)} closing tag(s); expected exactly one block"
        )

    if not open_positions:
        raise ParseError("Found </forge_handoff> closing tag without opening <forge_handoff>")

    if not close_positions:
        raise ParseError("Found <forge_handoff> opening tag without closing </forge_handoff>")

    open_pos = open_positions[0]
    close_pos = close_positions[0]

    if close_pos < open_pos + len(OPEN_TAG):
        raise ParseError("</forge_handoff> appears before or inside <forge_handoff>")

    body = stripped[open_pos + len(OPEN_TAG) : close_pos].strip()

    if not body:
        raise ParseError("<forge_handoff> block is empty")

    try:
        data = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        raise ParseError(f"YAML parse error in <forge_handoff> block: {exc}") from exc

    if not isinstance(data, dict):
        raise ParseError(
            f"<forge_handoff> content must be a YAML mapping, got {type(data).__name__}"
        )

    missing = _REQUIRED_KEYS - set(data.keys())
    if missing:
        raise ParseError(f"<forge_handoff> missing required keys: {sorted(missing)}")

    if not isinstance(data["summary"], str):
        raise ParseError(f"summary must be a string, got {type(data['summary']).__name__}")
    if not isinstance(data["commits"], list):
        raise ParseError(f"commits must be a list, got {type(data['commits']).__name__}")
    if not isinstance(data["acceptance_criteria"], list):
        raise ParseError(
            f"acceptance_criteria must be a list, got {type(data['acceptance_criteria']).__name__}"
        )

    return data
