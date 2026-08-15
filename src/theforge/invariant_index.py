"""Deterministic index over project invariants marked in authoritative docs (#1875).

The spike's central bet is that a project's "do not break this" rules are already
written down somewhere authoritative — an ADR, ``CONVENTIONS.md``, a policy doc —
and the only thing missing is a way to *find the relevant ones* at prompt-build
time. So this module builds a derived, rebuildable, disposable view over marked
regions of the project's own Markdown. It is not a second source of truth:

- The index stores **provenance and applicability metadata only** — id, source
  path, source anchor, line span, scope, enforcement, digest. It deliberately
  does not copy the invariant prose, so an index that drifts from its source is
  visibly stale (digest mismatch) rather than quietly authoritative.
- Consumers re-read the source document to render text. See
  :mod:`theforge.task.invariant_selector`.
- Nothing here calls a model, and nothing here decides coordinator control flow.

The annotation convention is portable — a target project marks its own rules::

    <!-- forge-invariant id="summaries-advisory"
         scope="area:audit phase:plan,dev,review files:src/knowledge_*.py"
         enforcement="review" -->
    LLM-generated summaries advise agents; they never drive coordinator control flow.
    <!-- /forge-invariant -->

Every malformed marker becomes a diagnostic, never an exception: a project with
one broken annotation still gets an index of the rest.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

INVARIANT_INDEX_PATH = Path(".forge") / "knowledge" / "invariants" / "index.yaml"
INVARIANT_INDEX_SCHEMA_VERSION = 1

#: Enforcement levels a project may declare. ``advisory`` is the default so an
#: unmarked level never silently claims gate authority.
ENFORCEMENT_LEVELS = ("advisory", "review", "gate")
DEFAULT_ENFORCEMENT = "advisory"

#: Scope keys the extractor understands. An unknown key is recorded verbatim in
#: ``scope_raw`` and reported as a diagnostic — it never narrows applicability,
#: because a scope nobody parsed cannot be evidence that a rule does not apply.
SCOPE_KEYS = ("area", "phase", "files")

#: How completely a project narrowed a rule's applicability. Consumers read this
#: to decide between a narrow capsule and conservative broad inclusion.
COMPLETENESS_FULL = "full"
COMPLETENESS_PARTIAL = "partial"
COMPLETENESS_NONE = "none"

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_OPEN_MARKER = re.compile(r"<!--\s*forge-invariant\b(?P<attrs>.*?)-->", re.DOTALL)
_CLOSE_MARKER = re.compile(r"<!--\s*/\s*forge-invariant\s*-->")
_ATTR_PATTERN = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*=\s*\"(?P<value>[^\"]*)\"")
_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(?P<title>.+?)\s*$")
_FENCE_PATTERN = re.compile(r"^(?P<fence>`{3,}|~{3,})(?P<info>.*)$")

#: Vendored and build directories never scanned for invariant sources, whatever
#: the configured globs say. Kept to names that mean the same thing in any
#: ecosystem — a project's *own* layout is never assumed here, because the
#: default glob is the whole repository.
_EXCLUDED_DIRS = frozenset(
    {
        "venv",
        "node_modules",
        "__pycache__",
        "vendor",
        "target",
        "build",
        "dist",
        "site-packages",
    }
)


@dataclass(frozen=True)
class InvariantIndexDiagnostic:
    """One annotation the extractor could not use, and why."""

    path: str
    line: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "line": self.line, "reason": self.reason}


@dataclass(frozen=True)
class InvariantIndexBuildResult:
    path: Path
    payload: dict[str, Any]
    diagnostics: tuple[InvariantIndexDiagnostic, ...]

    @property
    def entries(self) -> list[dict[str, Any]]:
        invariants = self.payload.get("invariants")
        return invariants if isinstance(invariants, list) else []


# ── Source discovery ─────────────────────────────────────────────────────────


def discover_sources(project_root: Path, globs: tuple[str, ...] | list[str]) -> list[Path]:
    """Repo-relative Markdown paths matching ``globs``, deduped and sorted.

    Ordering is total and content-independent so two rebuilds of an unchanged
    tree produce byte-identical indexes.

    Hidden directories are skipped along with the vendored/build names above.
    That single rule keeps tooling state — caches, VCS metadata, ``.forge``'s own
    derived index — out of the corpus without naming any project's layout, which
    matters because the default glob is every Markdown file in the repository.
    """
    found: set[Path] = set()
    for pattern in globs:
        if not pattern or not pattern.strip():
            continue
        try:
            matches = project_root.glob(pattern.strip())
        except (ValueError, NotImplementedError):
            continue
        for match in matches:
            if not match.is_file():
                continue
            try:
                rel = match.relative_to(project_root)
            except ValueError:  # pragma: no cover - glob results are always under root
                continue
            if any(part in _EXCLUDED_DIRS or part.startswith(".") for part in rel.parts[:-1]):
                continue
            found.add(rel)
    return sorted(found)


# ── Extraction ───────────────────────────────────────────────────────────────


def extract_from_text(
    text: str, source_path: str
) -> tuple[
    list[dict[str, Any]],
    list[InvariantIndexDiagnostic],
]:
    """Parse every ``forge-invariant`` region in one document.

    Returns metadata entries and diagnostics. The invariant prose is hashed for
    staleness detection and then discarded — the source document keeps it.

    Markers inside fenced code blocks are ignored. A project that documents this
    convention writes example markers in its own docs, and an example is a marker
    *about* the convention rather than an application of it; without this,
    documenting the feature would file its own illustrations as live invariants.
    """
    entries: list[dict[str, Any]] = []
    diagnostics: list[InvariantIndexDiagnostic] = []
    lines = text.splitlines()
    fenced = _fenced_lines(lines)

    for match in _OPEN_MARKER.finditer(text):
        open_line = _line_number(text, match.start())
        if open_line in fenced:
            continue
        attrs, attr_errors = _parse_attributes(match.group("attrs"))
        for error in attr_errors:
            diagnostics.append(InvariantIndexDiagnostic(source_path, open_line, error))

        close = _CLOSE_MARKER.search(text, match.end())
        if close is None:
            diagnostics.append(
                InvariantIndexDiagnostic(
                    source_path, open_line, "unterminated forge-invariant block"
                )
            )
            continue

        invariant_id = attrs.get("id", "").strip()
        if not invariant_id:
            diagnostics.append(
                InvariantIndexDiagnostic(source_path, open_line, "missing required attribute 'id'")
            )
            continue
        if not _ID_PATTERN.match(invariant_id):
            diagnostics.append(
                InvariantIndexDiagnostic(
                    source_path,
                    open_line,
                    f"invalid id {invariant_id!r} (expected [a-z0-9][a-z0-9._-]*)",
                )
            )
            continue

        enforcement = (attrs.get("enforcement") or DEFAULT_ENFORCEMENT).strip().lower()
        if enforcement not in ENFORCEMENT_LEVELS:
            diagnostics.append(
                InvariantIndexDiagnostic(
                    source_path,
                    open_line,
                    f"unknown enforcement {enforcement!r}; using {DEFAULT_ENFORCEMENT!r}",
                )
            )
            enforcement = DEFAULT_ENFORCEMENT

        scope_raw = (attrs.get("scope") or "").strip()
        scope, scope_errors = _parse_scope(scope_raw)
        for error in scope_errors:
            diagnostics.append(InvariantIndexDiagnostic(source_path, open_line, error))

        body = text[match.end() : close.start()]
        close_line = _line_number(text, close.start())
        body_start = open_line + 1
        body_end = max(body_start, close_line - 1)
        if not body.strip():
            diagnostics.append(
                InvariantIndexDiagnostic(
                    source_path, open_line, f"empty invariant body for id {invariant_id!r}"
                )
            )
            continue

        entries.append(
            {
                "id": invariant_id,
                "source_path": source_path,
                "source_anchor": _nearest_heading(lines, open_line),
                "start_line": open_line,
                "end_line": close_line,
                "body_start_line": body_start,
                "body_end_line": body_end,
                "body_lines": len([line for line in body.strip().splitlines()]),
                "enforcement": enforcement,
                "scope_raw": scope_raw,
                "scope": scope,
                "applicability": {
                    "phases": list(scope["phases"]),
                    "areas": list(scope["areas"]),
                    "file_globs": list(scope["files"]),
                    "scope_completeness": _completeness(scope, bool(scope_errors)),
                    "unparsed_scope_keys": list(scope["unknown_keys"]),
                },
                "source_digest": _digest(body),
            }
        )

    return entries, diagnostics


def _fenced_lines(lines: list[str]) -> frozenset[int]:
    """1-based line numbers that sit inside a fenced code block.

    Fence delimiters themselves count as inside, so a marker sharing a line with
    one cannot slip through. An unterminated fence swallows the rest of the file,
    which is the conservative reading: text a Markdown renderer would show as
    code is not a rule the project is asserting.
    """
    inside: set[int] = set()
    fence: str | None = None
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        match = _FENCE_PATTERN.match(stripped)
        if fence is None:
            if match:
                fence = match.group("fence")[:3]
                inside.add(index)
            continue
        inside.add(index)
        if match and match.group("fence").startswith(fence) and not match.group("info").strip():
            fence = None
    return frozenset(inside)


def _parse_attributes(raw: str) -> tuple[dict[str, str], list[str]]:
    attrs: dict[str, str] = {}
    errors: list[str] = []
    consumed = 0
    for match in _ATTR_PATTERN.finditer(raw):
        key = match.group("key").lower()
        if key in attrs:
            errors.append(f"duplicate attribute {key!r}; keeping first value")
            consumed += len(match.group(0))
            continue
        attrs[key] = match.group("value")
        consumed += len(match.group(0))
    leftover = re.sub(r"\s+", "", raw)
    if consumed == 0 and leftover:
        errors.append('could not parse any key="value" attributes')
    for key in attrs:
        if key not in {"id", "scope", "enforcement"}:
            errors.append(f"unknown attribute {key!r}; ignored")
    return attrs, errors


def _parse_scope(raw: str) -> tuple[dict[str, list[str]], list[str]]:
    scope: dict[str, list[str]] = {"areas": [], "phases": [], "files": [], "unknown_keys": []}
    errors: list[str] = []
    for token in raw.split():
        if ":" not in token:
            errors.append(f"scope token {token!r} is not key:value; ignored")
            scope["unknown_keys"].append(token)
            continue
        key, _, values = token.partition(":")
        key = key.strip().lower()
        parsed = [value.strip() for value in values.split(",") if value.strip()]
        if key == "area":
            scope["areas"].extend(parsed)
        elif key == "phase":
            scope["phases"].extend(value.lower() for value in parsed)
        elif key == "files":
            scope["files"].extend(parsed)
        else:
            errors.append(f"unknown scope key {key!r} (known: {', '.join(SCOPE_KEYS)})")
            scope["unknown_keys"].append(key)
    for key in ("areas", "phases", "files", "unknown_keys"):
        scope[key] = sorted(dict.fromkeys(scope[key]))
    return scope, errors


def _completeness(scope: dict[str, list[str]], had_errors: bool) -> str:
    """How confidently this rule's applicability can be narrowed.

    An annotation with unparsed scope tokens is never reported as ``full``: a
    scope nobody understood must widen inclusion, not narrow it.
    """
    if scope["unknown_keys"] or had_errors:
        return COMPLETENESS_PARTIAL if (scope["areas"] or scope["files"]) else COMPLETENESS_NONE
    if scope["files"]:
        return COMPLETENESS_FULL
    if scope["areas"]:
        return COMPLETENESS_PARTIAL
    return COMPLETENESS_NONE


def _nearest_heading(lines: list[str], line_number: int) -> str:
    """The Markdown heading a marker sits under — the human-readable anchor."""
    for index in range(min(line_number, len(lines)) - 1, -1, -1):
        match = _HEADING_PATTERN.match(lines[index])
        if match:
            return match.group("title").strip()
    return ""


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _digest(body: str) -> str:
    return "sha256:" + hashlib.sha256(body.strip().encode("utf-8")).hexdigest()


# ── Build / load ─────────────────────────────────────────────────────────────


def build_invariant_index(
    project_root: Path, source_globs: tuple[str, ...] | list[str]
) -> InvariantIndexBuildResult:
    """Scan the configured sources and return the derived payload (no write)."""
    entries: list[dict[str, Any]] = []
    diagnostics: list[InvariantIndexDiagnostic] = []
    seen_ids: dict[str, str] = {}

    for rel_path in discover_sources(project_root, source_globs):
        source_path = rel_path.as_posix()
        try:
            text = (project_root / rel_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            diagnostics.append(InvariantIndexDiagnostic(source_path, 0, f"unreadable: {exc}"))
            continue
        if "forge-invariant" not in text:
            continue
        found, problems = extract_from_text(text, source_path)
        diagnostics.extend(problems)
        for entry in found:
            previous = seen_ids.get(entry["id"])
            if previous is not None:
                diagnostics.append(
                    InvariantIndexDiagnostic(
                        source_path,
                        entry["start_line"],
                        f"duplicate invariant id {entry['id']!r} (first defined in {previous})",
                    )
                )
                continue
            seen_ids[entry["id"]] = source_path
            entries.append(entry)

    entries.sort(key=lambda entry: (entry["source_path"], entry["start_line"], entry["id"]))
    payload: dict[str, Any] = {
        "schema_version": INVARIANT_INDEX_SCHEMA_VERSION,
        "source_globs": [glob for glob in source_globs],
        "invariant_count": len(entries),
        "diagnostics": [diagnostic.to_dict() for diagnostic in sorted_diagnostics(diagnostics)],
        "invariants": entries,
    }
    return InvariantIndexBuildResult(
        path=project_root / INVARIANT_INDEX_PATH,
        payload=payload,
        diagnostics=tuple(sorted_diagnostics(diagnostics)),
    )


def sorted_diagnostics(
    diagnostics: list[InvariantIndexDiagnostic],
) -> list[InvariantIndexDiagnostic]:
    return sorted(diagnostics, key=lambda item: (item.path, item.line, item.reason))


def rebuild_invariant_index(
    project_root: Path, source_globs: tuple[str, ...] | list[str]
) -> InvariantIndexBuildResult:
    """Build the index and write it to ``.forge/knowledge/invariants/index.yaml``."""
    result = build_invariant_index(project_root, source_globs)
    result.path.parent.mkdir(parents=True, exist_ok=True)
    result.path.write_text(
        yaml.safe_dump(result.payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return result


def load_invariant_index(project_root: Path) -> list[dict[str, Any]]:
    """Read the derived index, degrading to ``[]`` on any problem.

    A missing, unparseable, or wrong-schema index means "no invariants known",
    never an exception: a run with nothing to be reminded of must proceed.
    """
    path = project_root / INVARIANT_INDEX_PATH
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(raw, dict):
        return []
    if raw.get("schema_version") != INVARIANT_INDEX_SCHEMA_VERSION:
        return []
    entries = raw.get("invariants")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict) and entry.get("id")]
