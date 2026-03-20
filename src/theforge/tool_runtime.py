"""Tool runtime for API-mode agents.

Defines the canonical tool registry, tool schemas, per-provider schema translation,
and tool handlers. This is the single source of truth for all tool definitions.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

MAX_TOOL_OUTPUT_BYTES = 51200  # 50KB default

# Maps forge.yaml user-facing capitalized names → canonical internal names.
# CLI profiles keep capitalized names; API profiles are normalized at parse time.
TOOL_NAME_MAP: dict[str, str] = {
    "Read": "read_file",
    "Bash": "bash",
    "Grep": "grep",
    "Glob": "glob",
    "Write": "write_file",
    "Edit": "edit_file",
}


@dataclass
class ToolDef:
    """A tool available to API-mode agents."""

    name: str
    description: str
    parameters: dict  # JSON Schema for arguments
    handler: Callable[..., str]  # accepts **args + working_dir kwarg; returns str

    def to_openai_function(self) -> dict:
        """Translate to OpenAI Chat Completions function tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_openai_responses_function(self) -> dict:
        """Translate to OpenAI Responses API function tool schema.

        The Responses API (used by Codex models) uses a flat format with
        'type', 'name', 'description', 'parameters' at the top level,
        unlike Chat Completions which nests under 'function'.
        """
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    def to_anthropic_tool(self) -> dict:
        """Translate to Anthropic tool schema."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

    def to_google_declaration(self) -> dict:
        """Translate to Google function declaration schema.

        Google's API does not support additionalProperties, anyOf, or other
        advanced JSON Schema features. These are stripped recursively.
        """
        from .runner_api import _sanitize_schema_for_google

        return {
            "name": self.name,
            "description": self.description,
            "parameters": _sanitize_schema_for_google(self.parameters),
        }


# ── Tool output helpers ────────────────────────────────────────────────


def truncate_output(text: str, max_bytes: int = MAX_TOOL_OUTPUT_BYTES) -> str:
    """Truncate tool output to max_bytes, appending a notice if truncated."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes].decode("utf-8", errors="replace")
    original_size = len(encoded)
    kb = max_bytes // 1024
    return f"{truncated}\n[output truncated — {original_size} bytes, showing first {kb}KB]"


def _resolve_safe(working_dir: Path, path: str) -> Path | None:
    """Resolve path relative to working_dir; return None if it escapes."""
    resolved = (working_dir / path).resolve()
    try:
        resolved.relative_to(working_dir.resolve())
        return resolved
    except ValueError:
        return None


# ── Tool handlers ─────────────────────────────────────────────────────


def _handle_read_file(
    *,
    path: str,
    working_dir: Path,
    start_line: int | None = None,
    end_line: int | None = None,
    max_bytes: int = MAX_TOOL_OUTPUT_BYTES,
) -> str:
    """Read file contents, optionally constrained to a line range."""
    resolved = _resolve_safe(working_dir, path)
    if resolved is None:
        return f"Error: path traversal rejected — '{path}' resolves outside working directory"
    if not resolved.exists():
        return f"Error: FileNotFoundError — {path} does not exist"
    if resolved.is_dir():
        return f"Error: {path} is a directory, not a file"
    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
        if start_line is not None or end_line is not None:
            lines = content.splitlines()
            s = (start_line - 1) if start_line is not None else 0
            e = end_line if end_line is not None else len(lines)
            s = max(0, s)
            e = min(len(lines), e)
            selected = lines[s:e]
            content = "\n".join(f"{s + i + 1}: {line}" for i, line in enumerate(selected))
        return truncate_output(content, max_bytes)
    except Exception as exc:
        return f"Error: {type(exc).__name__} — {exc}"


def _handle_bash(
    *,
    command: str,
    working_dir: Path,
    max_bytes: int = MAX_TOOL_OUTPUT_BYTES,
) -> str:
    """Run a shell command in working_dir with a 30s timeout."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )
        combined = result.stdout + result.stderr
        output = f"Exit code: {result.returncode}\n{combined}"
        return truncate_output(output, max_bytes)
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 30s"
    except Exception as exc:
        return f"Error: {type(exc).__name__} — {exc}"


def _handle_grep(
    *,
    pattern: str,
    working_dir: Path,
    path: str | None = None,
    glob: str | None = None,
    max_bytes: int = MAX_TOOL_OUTPUT_BYTES,
) -> str:
    """Search for a regex pattern within working_dir (or a sub-path)."""
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"Error: invalid regex pattern — {exc}"

    search_root = working_dir
    if path:
        resolved = _resolve_safe(working_dir, path)
        if resolved is None:
            return f"Error: path traversal rejected — '{path}' resolves outside working directory"
        search_root = resolved

    results: list[str] = []
    # When search_root is a file, search it directly; otherwise walk the tree.
    candidates = [search_root] if search_root.is_file() else sorted(search_root.rglob("*"))
    for file_path in candidates:
        if not file_path.is_file():
            continue
        if glob and not fnmatch.fnmatch(file_path.name, glob):
            continue
        try:
            for line_no, line in enumerate(
                file_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if regex.search(line):
                    rel = file_path.relative_to(working_dir)
                    results.append(f"{rel}:{line_no}: {line}")
        except Exception:
            continue

    if not results:
        return "(no matches)"
    return truncate_output("\n".join(results), max_bytes)


def _handle_glob(
    *,
    pattern: str,
    working_dir: Path,
    max_bytes: int = MAX_TOOL_OUTPUT_BYTES,
) -> str:
    """Find files matching a glob pattern within working_dir."""
    try:
        matches = sorted(working_dir.glob(pattern))
        if not matches:
            return "(no matches)"
        paths = [str(p.relative_to(working_dir)) for p in matches]
        return truncate_output("\n".join(paths), max_bytes)
    except Exception as exc:
        return f"Error: {type(exc).__name__} — {exc}"


def _handle_write_file(
    *,
    path: str,
    content: str,
    working_dir: Path,
    max_bytes: int = MAX_TOOL_OUTPUT_BYTES,
) -> str:
    """Create or overwrite a file with the given content."""
    resolved = _resolve_safe(working_dir, path)
    if resolved is None:
        return f"Error: path traversal rejected — '{path}' resolves outside working directory"
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return truncate_output(
            f"Written {len(content.encode('utf-8'))} bytes to {path}", max_bytes
        )
    except Exception as exc:
        return f"Error: {type(exc).__name__} — {exc}"


def _handle_edit_file(
    *,
    path: str,
    old_string: str,
    new_string: str,
    working_dir: Path,
    max_bytes: int = MAX_TOOL_OUTPUT_BYTES,
) -> str:
    """Replace an exact string in a file.

    Fails if old_string appears zero times (not found) or more than once
    (ambiguous — provide more surrounding context to make it unique).
    """
    resolved = _resolve_safe(working_dir, path)
    if resolved is None:
        return f"Error: path traversal rejected — '{path}' resolves outside working directory"
    if not resolved.exists():
        return f"Error: FileNotFoundError — {path} does not exist"
    if resolved.is_dir():
        return f"Error: {path} is a directory, not a file"
    try:
        content = resolved.read_text(encoding="utf-8")
        count = content.count(old_string)
        if count == 0:
            return f"Error: old_string not found in {path} — no changes made"
        if count > 1:
            return (
                f"Error: old_string appears {count} times in {path} — "
                "provide more surrounding context to make it unique"
            )
        new_content = content.replace(old_string, new_string, 1)
        resolved.write_text(new_content, encoding="utf-8")
        return truncate_output(f"Replaced 1 occurrence in {path}", max_bytes)
    except Exception as exc:
        return f"Error: {type(exc).__name__} — {exc}"


# ── Tool registry ─────────────────────────────────────────────────────

TOOL_REGISTRY: dict[str, ToolDef] = {
    "read_file": ToolDef(
        name="read_file",
        description=(
            "Read file contents from the working directory. "
            "Optionally restrict to a line range using start_line and end_line "
            "(1-based, inclusive). Returns the file text (or selected lines with line numbers)."
        ),
        parameters={
            "type": "object",
            "additionalProperties": False,
            "required": ["path"],
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file, relative to the working directory.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "First line to return (1-based, inclusive). Optional.",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Last line to return (1-based, inclusive). Optional.",
                },
            },
        },
        handler=_handle_read_file,
    ),
    "bash": ToolDef(
        name="bash",
        description=(
            "Run a shell command in the working directory. "
            "Timeout: 30s. Returns exit code, stdout, and stderr. "
            "Prefer read_file/grep/glob for inspection tasks. "
            "Use for test discovery (pytest --co -q), git log, etc."
        ),
        parameters={
            "type": "object",
            "additionalProperties": False,
            "required": ["command"],
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to run.",
                },
            },
        },
        handler=_handle_bash,
    ),
    "grep": ToolDef(
        name="grep",
        description=(
            "Search for a regex pattern in files under working_dir. "
            "Returns matching lines with file:line prefix. "
            "Use glob to filter by filename pattern."
        ),
        parameters={
            "type": "object",
            "additionalProperties": False,
            "required": ["pattern"],
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regular expression to search for.",
                },
                "path": {
                    "type": "string",
                    "description": "Sub-directory or file to search. Defaults to working dir.",
                },
                "glob": {
                    "type": "string",
                    "description": "Glob pattern to filter filenames (e.g. '*.py'). Optional.",
                },
            },
        },
        handler=_handle_grep,
    ),
    "glob": ToolDef(
        name="glob",
        description=(
            "Find files matching a glob pattern within the working directory. "
            "Returns newline-separated relative paths."
        ),
        parameters={
            "type": "object",
            "additionalProperties": False,
            "required": ["pattern"],
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern (e.g. '**/*.py', 'src/**/*.ts').",
                },
            },
        },
        handler=_handle_glob,
    ),
    "write_file": ToolDef(
        name="write_file",
        description=(
            "Create or overwrite a file with the given content. "
            "Parent directories are created automatically. "
            "Use for new files or complete rewrites; use edit_file for targeted changes."
        ),
        parameters={
            "type": "object",
            "additionalProperties": False,
            "required": ["path", "content"],
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file, relative to the working directory.",
                },
                "content": {
                    "type": "string",
                    "description": "Full content to write to the file.",
                },
            },
        },
        handler=_handle_write_file,
    ),
    "edit_file": ToolDef(
        name="edit_file",
        description=(
            "Replace an exact string in a file. "
            "Fails if old_string appears zero times (not found) or more than once "
            "(ambiguous — provide more surrounding context to make it unique). "
            "Use write_file to create new files or fully rewrite existing ones."
        ),
        parameters={
            "type": "object",
            "additionalProperties": False,
            "required": ["path", "old_string", "new_string"],
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file, relative to the working directory.",
                },
                "old_string": {
                    "type": "string",
                    "description": "Exact string to find and replace. Must appear exactly once.",
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement string.",
                },
            },
        },
        handler=_handle_edit_file,
    ),
}
