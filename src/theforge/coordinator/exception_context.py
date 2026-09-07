"""Audit-grade exception and command context for coordinator subprocess steps.

When a coordinator step shells out and the call raises, the useful record is not
the exception message: it is the command that was run, with bulky free-text
arguments redacted, plus enough of the offending input to see *why* the call was
rejected. :func:`_build_exception_context` produces that record, and every
coordinator step that reports ``error_context`` produces it the same way.

It lives in its own module — stdlib-only, importing nothing from the coordinator
— so a step module can build the context without depending on the phase module
that happens to have defined it first. ``completion.py`` re-exports these names
because that is where its callers and tests already reach for them (#2818).
"""

from __future__ import annotations

import shlex
import traceback


def _render_command_for_audit(cmd: list[str]) -> str:
    """Render a subprocess command line while redacting bulky free-text args."""
    rendered: list[str] = []
    skip_next = False
    for index, arg in enumerate(cmd):
        if skip_next:
            skip_next = False
            continue
        if arg == "--body" and index + 1 < len(cmd):
            rendered.append(shlex.quote(arg))
            rendered.append(shlex.quote(f"<redacted body len={len(cmd[index + 1])}>"))
            skip_next = True
            continue
        rendered.append(shlex.quote(arg))
    return " ".join(rendered)


def _escape_control_preview(text: str) -> str:
    """Render a preview string with control characters made explicit."""
    out: list[str] = []
    for char in text:
        codepoint = ord(char)
        if char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "\t":
            out.append("\\t")
        elif codepoint < 32 or codepoint == 127:
            out.append(f"\\x{codepoint:02x}")
        else:
            out.append(char)
    return "".join(out)


def _preview_window(text: str, position: int, *, radius: int = 24) -> str:
    """Return an escaped preview around a string position."""
    start = max(0, position - radius)
    end = min(len(text), position + radius + 1)
    return _escape_control_preview(text[start:end])


def _hex_window(text: str, position: int, *, radius: int = 8) -> str:
    """Return a compact hex dump window around a string position."""
    start = max(0, position - radius)
    end = min(len(text), position + radius + 1)
    return text[start:end].encode("utf-8", errors="replace").hex(" ")


def _identify_embedded_null_source(
    cmd: list[str],
    input_sources: list[dict[str, str]] | None,
) -> dict[str, object] | None:
    """Locate the first embedded NUL in the command and source fields."""
    for arg_index, arg in enumerate(cmd):
        nul_position = arg.find("\x00")
        if nul_position < 0:
            continue

        detail: dict[str, object] = {
            "argument_index": arg_index,
            "argument_flag": (
                cmd[arg_index - 1]
                if arg_index > 0 and cmd[arg_index - 1].startswith("--")
                else None
            ),
            "position": nul_position,
            "preview": _preview_window(arg, nul_position),
            "hex_window": _hex_window(arg, nul_position),
        }
        for source in input_sources or []:
            source_text = source.get("text", "")
            source_nul_position = source_text.find("\x00")
            if source_nul_position >= 0:
                detail["source"] = source.get("source")
                detail["source_position"] = source_nul_position
                detail["source_preview"] = _preview_window(source_text, source_nul_position)
                detail["source_hex_window"] = _hex_window(source_text, source_nul_position)
                break
        return detail
    return None


def _build_exception_context(
    exc: Exception,
    *,
    cmd: list[str] | None = None,
    input_sources: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    """Serialize rich exception context for audit/debugging."""
    context: dict[str, object] = {
        "exception_class": type(exc).__name__,
        "exception_args": [repr(arg) for arg in exc.args],
        "traceback": traceback.format_exc(),
    }
    if cmd is not None:
        context["command"] = _render_command_for_audit(cmd)
    if (
        isinstance(exc, ValueError)
        and "embedded null byte" in str(exc).lower()
        and cmd is not None
    ):
        offending_input = _identify_embedded_null_source(cmd, input_sources)
        if offending_input is not None:
            context["offending_input"] = offending_input
    return context
