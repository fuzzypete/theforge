#!/usr/bin/env python3
"""Derive a CHANGELOG release section from a GitHub milestone + git tag range.

`promote-rc.sh` calls this at promote time instead of trusting a hand-curated
`[Unreleased]` section. The milestone is canonical for "what's in this release";
the PR merge commits in the tag range are canonical for "what actually changed".
This script cross-references the two and refuses to render if they disagree, so
an operator who forgot to milestone an issue (or milestoned one that never
merged) gets a loud abort rather than silently-incomplete release notes.

Usage:
    derive_changelog.py --version X.Y.Z --prev-tag vA.B.C \\
        --date YYYY-MM-DD [--repo fuzzypete/theforge]

On success: prints the rendered `## [X.Y.Z] — DATE` section to stdout and
progress/count lines to stderr, exits 0.
On milestone/merge mismatch: prints each discrepancy to stderr, exits 1.

The rendered section groups entries by the first matching issue label:
    bug            -> Fixed
    enhancement    -> Added
    documentation  -> Documentation
    (anything else / no label) -> Changed

Kept pure and side-effect-free apart from the `gh`/`git` subprocess reads and
the stdout/stderr writes, so it is unit-testable under a PATH-mocked `gh` and
`git` (see tests/test_derive_changelog.py).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

# Trailing "(#1234)" on a squash-merge commit subject — the PR number GitHub
# appends on squash merge (e.g. "...surface reviewer time-nudge warnings (#1804)").
# This is the PR number, NOT the issue number; the milestone tracks ISSUES.
_PR_NUM_RE = re.compile(r"\(#(\d+)\)\s*$")

# GitHub closing keywords in a commit body link the merge to the milestone
# issue(s) it resolves — forge emits "Closes #1087". The issue number here is
# what must cross-reference against the milestone; it routinely differs from the
# trailing PR number (issue #1087 closed by PR #1804).
_CLOSES_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b",
    re.IGNORECASE,
)

# git log record format: subject <US> body <RS>. The RS delimits commits so a
# multi-line body (which carries the Closes trailers) stays attached to its
# subject.
_LOG_FORMAT = "%s%x1f%b%x1e"

# Label -> Keep-a-Changelog section. First matching label (in this order) wins.
_LABEL_SECTIONS = (
    ("bug", "Fixed"),
    ("enhancement", "Added"),
    ("documentation", "Documentation"),
)

# Keep-a-Changelog render order for the sections we emit.
_SECTION_ORDER = ("Added", "Changed", "Fixed", "Documentation")

_DEFAULT_SECTION = "Changed"


def _run(cmd: list[str]) -> str:
    """Run a subprocess and return its stdout, raising on non-zero exit."""
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(cmd)}\n{result.stderr}"
        )
    return result.stdout


def fetch_milestone_issues(repo: str, version: str) -> list[dict]:
    """Return closed issues in milestone vX.Y.Z as [{number, title, labels}]."""
    out = _run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--milestone",
            f"v{version}",
            "--state",
            "closed",
            "--limit",
            "500",
            "--json",
            "number,title,labels",
        ]
    )
    raw = json.loads(out or "[]")
    issues = []
    for item in raw:
        issues.append(
            {
                "number": item["number"],
                "title": item["title"],
                "labels": [lbl["name"] for lbl in item.get("labels", [])],
            }
        )
    return issues


def fetch_merges(prev_tag: str) -> list[dict]:
    """Return the PR merges in prev_tag..HEAD as [{pr, issues}].

    Each entry carries the trailing `(#PR)` number (for reverse-check messaging)
    and the set of milestone issue numbers the commit closes (its `Closes #N`
    trailers). Commits with neither a trailing `(#N)` nor any closing trailer
    (version-bump chores, raw `Merge pull request ...` subjects) are dropped so
    they don't trip the reverse cross-reference.
    """
    out = _run(["git", "log", f"--pretty=format:{_LOG_FORMAT}", f"{prev_tag}..HEAD"])
    merges: list[dict] = []
    for record in out.split("\x1e"):
        if not record.strip():
            continue
        subject, _, body = record.partition("\x1f")
        pr_match = _PR_NUM_RE.search(subject.strip())
        pr_num = int(pr_match.group(1)) if pr_match else None
        closed = {int(n) for n in _CLOSES_RE.findall(body)}
        if pr_num is None and not closed:
            continue
        merges.append({"pr": pr_num, "issues": closed})
    return merges


def cross_reference(
    issues: list[dict], merges: list[dict], version: str, prev_tag: str
) -> list[str]:
    """Return a list of discrepancy lines; empty means milestone ⇔ merges match.

    Cross-references milestone ISSUE numbers against the issues each merge
    *closes* (not the trailing PR number, which differs). Forward: every
    milestone issue must be closed by some merge in the range. Reverse: every
    merge in the range must close at least one milestone issue.
    """
    milestone_numbers = {issue["number"] for issue in issues}
    closed_by_merges: set[int] = set()
    for merge in merges:
        closed_by_merges |= merge["issues"]

    discrepancies: list[str] = []
    for number in sorted(milestone_numbers - closed_by_merges):
        discrepancies.append(
            f"milestone v{version} has issue #{number} (closed) "
            f"with no PR merge in {prev_tag}..HEAD"
        )
    for merge in merges:
        if merge["issues"] & milestone_numbers:
            continue
        if merge["pr"] is not None:
            label = f"PR #{merge['pr']}"
        else:
            label = f"issue #{min(merge['issues'])}"
        discrepancies.append(f"{label} merged in tag range with no v{version} milestone issue")
    return discrepancies


def section_for(labels: list[str]) -> str:
    """Map an issue's labels to a Keep-a-Changelog section (first match wins)."""
    for label, section in _LABEL_SECTIONS:
        if label in labels:
            return section
    return _DEFAULT_SECTION


def render_section(issues: list[dict], version: str, date: str) -> str:
    """Render the `## [X.Y.Z] — DATE` section grouped by label-derived section."""
    grouped: dict[str, list[dict]] = {section: [] for section in _SECTION_ORDER}
    for issue in issues:
        grouped[section_for(issue["labels"])].append(issue)

    lines = [f"## [{version}] — {date}", ""]
    for section in _SECTION_ORDER:
        entries = grouped[section]
        if not entries:
            continue
        lines.append(f"### {section}")
        lines.append("")
        for issue in sorted(entries, key=lambda i: i["number"]):
            lines.append(f"- {issue['title']} (#{issue['number']})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Release version, e.g. 0.11.0")
    parser.add_argument("--prev-tag", required=True, help="Previous release tag, e.g. v0.10.0")
    parser.add_argument("--date", required=True, help="Release date YYYY-MM-DD")
    parser.add_argument("--repo", default="fuzzypete/theforge", help="owner/name for gh queries")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    issues = fetch_milestone_issues(args.repo, args.version)
    merges = fetch_merges(args.prev_tag)

    print(
        f"[forge]   {len(issues)} closed issues in milestone v{args.version}",
        file=sys.stderr,
    )
    print(
        f"[forge]   {len(merges)} PR merges in {args.prev_tag}..HEAD",
        file=sys.stderr,
    )

    discrepancies = cross_reference(issues, merges, args.version, args.prev_tag)
    if discrepancies:
        for line in discrepancies:
            print(f"[forge] ERROR: {line}", file=sys.stderr)
        print(
            "[forge] aborting promote — fix milestone/PR linkage and retry",
            file=sys.stderr,
        )
        return 1

    print(
        "[forge]   ✓ all milestone issues have corresponding merges",
        file=sys.stderr,
    )
    print(
        "[forge]   ✓ all merges in tag range have corresponding milestone issues",
        file=sys.stderr,
    )

    sys.stdout.write(render_section(issues, args.version, args.date))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
