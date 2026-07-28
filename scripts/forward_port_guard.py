#!/usr/bin/env python3
"""Content-scoped protection for the files a forward-port must not fully carry.

`forward-port.yml` merges every `release/*` push into main. Two files always
differ across that boundary for reasons that must NOT propagate:

  - `pyproject.toml` — the release line bumps `[project] version` to an rc /
    release version; main keeps its own dev version.
  - `CHANGELOG.md` — the release line rolls `[Unreleased]` into a versioned
    section; main keeps `[Unreleased]` accumulating.

The workflow used to defend those two facts by reverting *both files whole* to
main's copy on every port. That protected the version by discarding everything
else in the file: #2016 lost `requires-python = ">=3.12"` and
`[tool.ruff] target-version = "py312"` in transit and still reported success.

This helper scopes the guard to the content it exists for:

  protect  — take the merged tree's file, restore only main's `[project]
             version` / main's `[Unreleased]` section, and drop the versioned
             changelog sections the release line rolled. Every other
             release-branch change survives the port.
  resolve  — three-way merge a version-file conflict by first normalising the
             release side's version/roll content back to the merge base, so the
             only conflicts left are genuine ones (which abort the port).

Both refuse to guess. Anything discarded outside that scope — an unclassifiable
changelog section, a missing `[project] version`, a merged file the scoped
rewrite cannot reproduce — exits non-zero with a diagnostic so the port fails
loudly instead of shipping a partial tree.

Usage:
    forward_port_guard.py protect --kind {pyproject,changelog} \\
        --main MAIN_COPY --merged MERGED_FILE [--output PATH]
    forward_port_guard.py resolve --kind {pyproject,changelog} \\
        --base BASE --ours OURS --theirs THEIRS --output PATH

Exit 0 on success (a report goes to stdout), 1 on a guard failure, 2 on usage
errors. Kept pure apart from file IO and the `git merge-file` subprocess in
`resolve`, so it is unit-testable (see tests/test_forward_port_guard.py).
"""

from __future__ import annotations

import argparse
import difflib
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

UNRELEASED = "Unreleased"

# `## [Unreleased]` / `## [0.13.0rc9] — 2026-07-25`: the bracketed token is the
# section key. A `##` heading with no bracket keys on its full text.
_SECTION_RE = re.compile(r"^##\s+(?!#)(.*)$")
_BRACKET_KEY_RE = re.compile(r"^\[([^\]]+)\]")

# A release roll only ever adds version-keyed sections. Anything else that a
# release branch adds is unrelated content and must not be silently dropped.
_VERSION_KEY_RE = re.compile(r"^v?\d+\.\d+")

# `version = "0.13.0rc9"` inside the `[project]` table.
_VERSION_LINE_RE = re.compile(r"^version\s*=\s*(.+?)\s*$")
_TABLE_HEADER_RE = re.compile(r"^\[")


class GuardError(Exception):
    """A drop the guard is not authorised to make — the port must fail."""


# --------------------------------------------------------------------------
# pyproject.toml — protect `[project] version` only
# --------------------------------------------------------------------------


def _project_version_index(lines: list[str]) -> int:
    """Index of the `version = ...` line inside the `[project]` table, or -1."""
    in_project = False
    for index, line in enumerate(lines):
        if _TABLE_HEADER_RE.match(line):
            in_project = line.strip() == "[project]"
            continue
        if in_project and _VERSION_LINE_RE.match(line):
            return index
    return -1


def project_version_line(text: str) -> str:
    """Return the raw `version = ...` line from `[project]`.

    Raises GuardError when the table or the field is absent: without it the
    guard cannot tell a version bump from any other change, and guessing is
    exactly the failure mode this helper exists to remove.
    """
    lines = text.splitlines()
    index = _project_version_index(lines)
    if index < 0:
        raise GuardError("pyproject.toml has no `version` field in its [project] table")
    return lines[index]


def protect_pyproject(main_text: str, merged_text: str) -> tuple[str, list[str]]:
    """Merged pyproject with main's `[project] version` restored.

    Everything else in the merged file — dependencies, `requires-python`, tool
    configuration — is carried through untouched.
    """
    main_line = project_version_line(main_text)
    merged_lines = merged_text.splitlines()
    index = _project_version_index(merged_lines)
    if index < 0:
        raise GuardError("pyproject.toml has no `version` field in its [project] table")

    merged_line = merged_lines[index]
    report = []
    if merged_line == main_line:
        report.append("pyproject.toml: [project] version already matches main; nothing excluded")
    else:
        merged_lines[index] = main_line
        report.append(
            f"pyproject.toml: excluded the release-line version bump "
            f"({merged_line.strip()} -> {main_line.strip()}); main keeps its own version"
        )

    result = _join(merged_lines, merged_text)
    _verify_pyproject(merged_text, result)
    report.extend(_carried_report("pyproject.toml", main_text, result))
    return result, report


def _verify_pyproject(merged_text: str, result_text: str) -> None:
    """Fail unless the result is the merged file with only the version changed.

    A scoped guard that quietly removed anything else would reintroduce #2016
    in a subtler form, so the rewrite is checked against the merge result
    rather than trusted.
    """
    try:
        merged = tomllib.loads(merged_text)
        result = tomllib.loads(result_text)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - malformed input
        raise GuardError(f"pyproject.toml is not valid TOML after the merge: {exc}") from exc

    merged_project = dict(merged.get("project", {}))
    result_project = dict(result.get("project", {}))
    merged_project.pop("version", None)
    result_project.pop("version", None)
    merged.pop("project", None)
    result.pop("project", None)

    if merged_project != result_project or merged != result:
        removed = _keys_lost(merged_project, result_project) + _keys_lost(merged, result)
        detail = f" (missing/changed: {', '.join(removed)})" if removed else ""
        raise GuardError(
            "the version guard changed pyproject.toml content outside "
            f"[project] version{detail}; refusing to port a partial file"
        )


def _keys_lost(before: dict, after: dict) -> list[str]:
    return sorted(key for key, value in before.items() if after.get(key) != value)


# --------------------------------------------------------------------------
# CHANGELOG.md — protect `[Unreleased]` and drop release-roll sections
# --------------------------------------------------------------------------


class Section:
    """One `## ...` changelog section: its heading line plus the body below it."""

    def __init__(self, heading: str, key: str, body: list[str]) -> None:
        self.heading = heading
        self.key = key
        self.body = body

    @property
    def lines(self) -> list[str]:
        return [self.heading, *self.body]

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Section)
            and self.heading == other.heading
            and self.body == other.body
        )


def parse_changelog(text: str) -> tuple[list[str], list[Section]]:
    """Split a changelog into (preamble lines, sections in file order)."""
    preamble: list[str] = []
    sections: list[Section] = []
    for line in text.splitlines():
        match = _SECTION_RE.match(line)
        if match:
            heading_text = match.group(1).strip()
            bracket = _BRACKET_KEY_RE.match(heading_text)
            key = bracket.group(1).strip() if bracket else heading_text
            sections.append(Section(line, key, []))
        elif sections:
            sections[-1].body.append(line)
        else:
            preamble.append(line)
    return preamble, sections


def render_changelog(preamble: list[str], sections: list[Section], template: str) -> str:
    lines = list(preamble)
    for section in sections:
        lines.extend(section.lines)
    return _join(lines, template)


def protect_changelog(main_text: str, merged_text: str) -> tuple[str, list[str]]:
    """Merged changelog with main's `[Unreleased]` restored and rolls dropped.

    Release-roll content is exactly two things: the emptied/rewritten
    `[Unreleased]` section, and the versioned sections the roll created. Any
    other section the release line added is unrelated content — it is carried,
    and if it cannot be classified the port fails rather than dropping it.
    """
    _, main_sections = parse_changelog(main_text)
    merged_preamble, merged_sections = parse_changelog(merged_text)

    main_by_key = {section.key: section for section in main_sections}
    if UNRELEASED not in main_by_key:
        raise GuardError(
            "main's CHANGELOG.md has no `## [Unreleased]` section, so release-roll "
            "content is not mechanically distinguishable; refusing to guess"
        )

    kept: list[Section] = []
    dropped: list[Section] = []
    # Where main's [Unreleased] goes back: exactly where the merged file had it,
    # or at the top when the roll removed it outright.
    unreleased_at: int | None = None
    for position, section in enumerate(merged_sections):
        if section.key == UNRELEASED:
            if unreleased_at is None:
                unreleased_at = len(kept)
            dropped.append(section)
            continue
        if section.key in main_by_key:
            kept.append(section)
        elif _VERSION_KEY_RE.match(section.key):
            dropped.append(section)
        else:
            raise GuardError(
                f"CHANGELOG.md section `{section.heading.strip()}` (position {position + 1}) "
                "is absent from main and is not a version section, so it cannot be "
                "classified as a release roll; refusing to drop it silently"
            )

    kept.insert(0 if unreleased_at is None else unreleased_at, main_by_key[UNRELEASED])

    result = render_changelog(merged_preamble, kept, merged_text)
    _verify_changelog(main_text, merged_text, result)

    report = []
    rolled = [s.key for s in dropped if s.key != UNRELEASED]
    if rolled:
        report.append(
            "CHANGELOG.md: excluded release-roll section(s) "
            + ", ".join(f"[{key}]" for key in rolled)
        )
    merged_unreleased = next((s for s in merged_sections if s.key == UNRELEASED), None)
    if merged_unreleased is None:
        report.append("CHANGELOG.md: restored main's [Unreleased] section (the roll emptied it)")
    elif merged_unreleased != main_by_key[UNRELEASED]:
        report.append("CHANGELOG.md: kept main's [Unreleased] section over the release line's")
    if not report:
        report.append("CHANGELOG.md: no release-roll content to exclude")
    report.extend(_carried_report("CHANGELOG.md", main_text, result))
    return result, report


def _verify_changelog(main_text: str, merged_text: str, result_text: str) -> None:
    """Fail unless the result is the merged file minus exactly the roll scope."""
    _, main_sections = parse_changelog(main_text)
    merged_preamble, merged_sections = parse_changelog(merged_text)
    result_preamble, result_sections = parse_changelog(result_text)

    if result_preamble != merged_preamble:
        raise GuardError(
            "the changelog guard altered content above the first section heading; "
            "refusing to port a partial file"
        )

    main_by_key = {section.key: section for section in main_sections}
    result_by_key = {section.key: section for section in result_sections}

    for section in merged_sections:
        if section.key == UNRELEASED or section.key not in main_by_key:
            continue
        if result_by_key.get(section.key) != section:
            raise GuardError(
                f"the changelog guard dropped or altered section `{section.heading.strip()}`, "
                "which is neither [Unreleased] nor a release-roll section; "
                "refusing to port a partial file"
            )

    if result_by_key.get(UNRELEASED) != main_by_key[UNRELEASED]:
        raise GuardError("the changelog guard failed to restore main's [Unreleased] section")

    merged_keys = {section.key for section in merged_sections} | {UNRELEASED}
    invented = sorted(key for key in result_by_key if key not in merged_keys)
    if invented:
        raise GuardError(
            f"the changelog guard invented section(s) absent from the merge: {invented}"
        )


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------


def _join(lines: list[str], template: str) -> str:
    text = "\n".join(lines)
    if template.endswith("\n") and not text.endswith("\n"):
        text += "\n"
    return text


def _carried_report(name: str, main_text: str, result_text: str) -> list[str]:
    """One line naming how much release-branch change this port actually lands."""
    diff = [
        line
        for line in difflib.unified_diff(
            main_text.splitlines(), result_text.splitlines(), n=0, lineterm=""
        )
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    if not diff:
        return [f"{name}: no release-branch change to port"]
    return [f"{name}: porting {len(diff)} changed line(s) from the release line"]


_PROTECTORS = {"pyproject": protect_pyproject, "changelog": protect_changelog}


def protect(kind: str, main_text: str, merged_text: str) -> tuple[str, list[str]]:
    return _PROTECTORS[kind](main_text, merged_text)


def resolve(kind: str, base_text: str, ours_text: str, theirs_text: str) -> tuple[str, list[str]]:
    """Three-way merge a conflicted version file with the roll normalised away.

    The rc bump and the changelog roll conflict on every port. Rewriting the
    protected scope of *all three* sides to main's copy makes that content
    identical everywhere, so `git merge-file` sees only genuine release-branch
    edits — and does not conflict merely because the rc bump sits on the line
    above one (`requires-python` is adjacent to `version` in practice).
    Anything that still conflicts is real and propagates as a conflict
    (GuardError), never as a silent `--ours`.
    """
    base_normalised, _ = protect(kind, ours_text, base_text)
    theirs_normalised, report = protect(kind, ours_text, theirs_text)
    report = [f"conflict resolution: {line}" for line in report]

    with tempfile.TemporaryDirectory() as tmp:
        paths = {}
        for name, text in (
            ("ours", ours_text),
            ("base", base_normalised),
            ("theirs", theirs_normalised),
        ):
            path = Path(tmp) / name
            path.write_text(text)
            paths[name] = str(path)
        completed = subprocess.run(
            [
                "git",
                "merge-file",
                "-p",
                "-L",
                "main",
                "-L",
                "merge-base (roll normalised)",
                "-L",
                "release (roll normalised)",
                paths["ours"],
                paths["base"],
                paths["theirs"],
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    if completed.returncode < 0:  # pragma: no cover - git-level failure
        raise GuardError(f"git merge-file failed: {completed.stderr.strip()}")
    if completed.returncode > 0:
        raise GuardError(
            f"{completed.returncode} conflict(s) remain after normalising the release-line "
            "version/roll content — this is a genuine conflict, not a version bump"
        )
    return completed.stdout, report


def _read(path: str) -> str:
    return Path(path).read_text()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_protect = sub.add_parser("protect", help="restore main's version/roll scope in place")
    p_protect.add_argument("--kind", required=True, choices=sorted(_PROTECTORS))
    p_protect.add_argument("--main", required=True, help="main's copy of the file")
    p_protect.add_argument("--merged", required=True, help="the merged working-tree file")
    p_protect.add_argument("--output", help="write here instead of over --merged")

    p_resolve = sub.add_parser("resolve", help="three-way merge a conflicted version file")
    p_resolve.add_argument("--kind", required=True, choices=sorted(_PROTECTORS))
    p_resolve.add_argument("--base", required=True, help="merge-base stage (:1:)")
    p_resolve.add_argument("--ours", required=True, help="main stage (:2:)")
    p_resolve.add_argument("--theirs", required=True, help="release stage (:3:)")
    p_resolve.add_argument("--output", required=True)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "protect":
            result, report = protect(args.kind, _read(args.main), _read(args.merged))
            destination = args.output or args.merged
        else:
            result, report = resolve(
                args.kind, _read(args.base), _read(args.ours), _read(args.theirs)
            )
            destination = args.output
    except GuardError as exc:
        print(f"::error::forward-port guard: {exc}", file=sys.stderr)
        print(f"[forward-port] ERROR: {exc}", file=sys.stderr)
        return 1

    Path(destination).write_text(result)
    for line in report:
        print(f"[forward-port] {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
