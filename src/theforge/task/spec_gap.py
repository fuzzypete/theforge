"""Parser for the ``<forge_spec_gap>`` dev→operator backchannel (#2122).

A dev agent that reaches an acceptance criterion which does not define the case
in front of it has, today, exactly two moves: guess, or fail. The guess becomes
the spec, and review discovers it several cycles and tens of dollars later.

This module owns the one structured signal that gives it a third move: name the
criterion, name the undefined case, state the assumption it would proceed under,
and stop. The coordinator turns that into an operator question (see
:mod:`theforge.coordinator.spec_gap_flow`); this module only parses.

Deliberately strict, per project convention 2. A malformed or duplicated block
is reported, never salvaged: a half-read gap signal would silently drop the
criterion the operator needs to answer about, which is the failure this whole
channel exists to prevent.

Stdlib + PyYAML only (project convention 4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml

OPEN_TAG = "<forge_spec_gap>"
CLOSE_TAG = "</forge_spec_gap>"

#: Required keys. ``assumption`` is required rather than optional because the
#: bounded-allowance and no-answer paths both proceed under it — a gap raised
#: without one would leave the coordinator with nothing to record and nothing
#: to tell the next dev iteration (see the ``no_answer`` /
#: ``allowance_exhausted`` resolutions).
_REQUIRED_KEYS = ("criterion", "undefined_case", "assumption")

_FENCED_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)

#: How a raised gap was resolved. Every gap gets exactly one of these — the
#: channel never discards a pause without a record of how it ended.
RESOLUTION_OPERATOR = "operator"
RESOLUTION_NO_ANSWER = "no_answer"
RESOLUTION_ALLOWANCE_EXHAUSTED = "allowance_exhausted"

RESOLUTION_SOURCES = (
    RESOLUTION_OPERATOR,
    RESOLUTION_NO_ANSWER,
    RESOLUTION_ALLOWANCE_EXHAUSTED,
)


class SpecGapParseError(Exception):
    """Raised when a ``<forge_spec_gap>`` block is present but malformed."""


@dataclass(frozen=True)
class SpecGapSignal:
    """One specification gap a dev agent raised."""

    criterion: str
    undefined_case: str
    assumption: str
    options_considered: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        """JSON/YAML-safe payload, for pending files, audit, and resume state."""
        return {
            "criterion": self.criterion,
            "undefined_case": self.undefined_case,
            "assumption": self.assumption,
            "options_considered": list(self.options_considered),
        }

    @property
    def key(self) -> tuple[str, str]:
        """Identity of the gap: which criterion, and which case within it.

        Used to deduplicate resolutions across iterations and runs, so an
        operator answer given once is not asked for again.
        """
        return (self.criterion.strip(), self.undefined_case.strip())


def _clean_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise SpecGapParseError(
            f"{OPEN_TAG} field {field_name!r} must be a string, got {type(value).__name__}"
        )
    cleaned = value.strip()
    if not cleaned:
        raise SpecGapParseError(f"{OPEN_TAG} field {field_name!r} must not be empty")
    return cleaned


def extract_spec_gap(text: str) -> SpecGapSignal | None:
    """Extract and validate the ``<forge_spec_gap>`` block from agent output.

    Fenced code blocks are stripped first, so an agent quoting the block spec
    back in its prose (the prompt shows the shape) does not read as raising one.

    Returns None when no block is present — absence is the ordinary case and is
    never an error. Raises :class:`SpecGapParseError` when a block is present
    but unusable.
    """
    stripped = _FENCED_BLOCK_RE.sub("", text or "")

    open_positions = [m.start() for m in re.finditer(re.escape(OPEN_TAG), stripped)]
    close_positions = [m.start() for m in re.finditer(re.escape(CLOSE_TAG), stripped)]

    if not open_positions and not close_positions:
        return None

    if len(open_positions) > 1 or len(close_positions) > 1:
        # One question per pause. Two blocks would mean answering one and
        # silently dropping the other.
        raise SpecGapParseError(
            f"Found {len(open_positions)} {OPEN_TAG} opening tag(s) and "
            f"{len(close_positions)} closing tag(s); expected exactly one block"
        )

    if not open_positions:
        raise SpecGapParseError(f"Found {CLOSE_TAG} closing tag without opening {OPEN_TAG}")
    if not close_positions:
        raise SpecGapParseError(f"Found {OPEN_TAG} opening tag without closing {CLOSE_TAG}")

    open_pos = open_positions[0]
    close_pos = close_positions[0]
    if close_pos < open_pos + len(OPEN_TAG):
        raise SpecGapParseError(f"{CLOSE_TAG} appears before or inside {OPEN_TAG}")

    body = stripped[open_pos + len(OPEN_TAG) : close_pos].strip()
    if not body:
        raise SpecGapParseError(f"{OPEN_TAG} block is empty")

    try:
        data = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        raise SpecGapParseError(f"YAML parse error in {OPEN_TAG} block: {exc}") from exc

    if not isinstance(data, dict):
        raise SpecGapParseError(
            f"{OPEN_TAG} content must be a YAML mapping, got {type(data).__name__}"
        )

    missing = [key for key in _REQUIRED_KEYS if key not in data]
    if missing:
        raise SpecGapParseError(f"{OPEN_TAG} missing required keys: {sorted(missing)}")

    raw_options = data.get("options_considered")
    options: tuple[str, ...] = ()
    if raw_options is not None:
        if not isinstance(raw_options, list):
            raise SpecGapParseError(
                f"{OPEN_TAG} field 'options_considered' must be a list, "
                f"got {type(raw_options).__name__}"
            )
        options = tuple(str(option).strip() for option in raw_options if str(option).strip())

    return SpecGapSignal(
        criterion=_clean_text(data["criterion"], "criterion"),
        undefined_case=_clean_text(data["undefined_case"], "undefined_case"),
        assumption=_clean_text(data["assumption"], "assumption"),
        options_considered=options,
    )


def resolution_key(resolution: Any) -> tuple[str, str] | None:
    """Identity of a persisted resolution record, or None when unreadable."""
    if not isinstance(resolution, dict):
        return None
    criterion = str(resolution.get("criterion") or "").strip()
    undefined_case = str(resolution.get("undefined_case") or "").strip()
    if not criterion and not undefined_case:
        return None
    return (criterion, undefined_case)


def dedupe_resolutions(resolutions: Any) -> list[dict[str, Any]]:
    """Return resolution records unique by :func:`resolution_key`, later wins.

    Order is stable on first appearance so the dev prompt lists gaps in the
    order they were raised, while a re-answered gap carries its newest answer.
    """
    if not isinstance(resolutions, list):
        return []
    order: list[tuple[str, str]] = []
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in resolutions:
        key = resolution_key(entry)
        if key is None:
            continue
        if key not in by_key:
            order.append(key)
        by_key[key] = dict(entry)
    return [by_key[key] for key in order]
