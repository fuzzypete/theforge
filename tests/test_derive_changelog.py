"""Tests for scripts/derive_changelog.py and the promote-rc.sh CHANGELOG seam.

`derive_changelog.py` is the testable helper promote-rc.sh calls to build the
release section from milestone state + tag-range PR merges, replacing the old
blind `[Unreleased]` rename. Covers:
  - label-grouped rendering (`- Title (#N)` under Added/Fixed/Documentation/Changed)
  - cross-reference passes when milestone issues ⇔ their closing merges match
  - a milestone issue closed by a *different-numbered* PR still matches (the key
    invariant: the milestone tracks issue numbers, the squash subject carries the
    PR number, and they routinely differ — issue #1087 closed by PR #1804)
  - cross-reference aborts (non-zero + discrepancy lines) when a milestone issue
    has no closing merge, or a merge closes no milestone issue
  - chore/merge subjects with neither a trailing `(#N)` nor a `Closes #N` are ignored

Plus seam tests that run `promote-rc.sh` with the helper stubbed and the
surrounding git/gh calls mocked: one `--dry-run` (prints, does not write) and one
real write (splices the derived section while preserving the `[Unreleased]`
scratchpad).

The helper's `gh` and `git` reads are PATH-mocked with fake executables; the
seam tests use exported bash functions so the mocks survive promote-rc.sh's
`/opt/homebrew/bin` PATH prepend (functions beat PATH lookup).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "derive_changelog.py"
PROMOTE = REPO_ROOT / "scripts" / "promote-rc.sh"

# Must mirror derive_changelog.py's git-log record format (subject US body RS).
_US = "\x1f"
_RS = "\x1e"


def _write_fake_bins(bin_dir: Path, *, issues: list[dict], commits: list[tuple[str, str]]) -> None:
    """Install fake `gh` and `git` on PATH for the derivation helper.

    `gh issue list ... --json number,title,labels` -> the issues fixture.
    `git log --pretty=format:%s%x1f%b%x1e <range>` -> the commits fixture,
    rendered in the exact record format the helper parses (subject, body).
    """
    issues_file = bin_dir / "issues.json"
    issues_file.write_text(json.dumps(issues), encoding="utf-8")
    log_file = bin_dir / "gitlog.txt"
    log_file.write_text(
        "".join(f"{subject}{_US}{body}{_RS}" for subject, body in commits),
        encoding="utf-8",
    )

    gh = bin_dir / "gh"
    gh.write_text(f'#!/usr/bin/env bash\ncat "{issues_file}"\n')
    gh.chmod(0o755)

    git = bin_dir / "git"
    git.write_text(
        f'#!/usr/bin/env bash\nif [[ "$1" == "log" ]]; then\n    cat "{log_file}"\nfi\n'
    )
    git.chmod(0o755)


def _run_derive(bin_dir: Path, *extra: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--version",
            "0.11.0",
            "--prev-tag",
            "v0.10.0",
            "--date",
            "2026-07-19",
            "--repo",
            "fuzzypete/theforge",
            *extra,
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _issue(number: int, title: str, labels: list[str]) -> dict:
    return {"number": number, "title": title, "labels": [{"name": n} for n in labels]}


def _commit(pr: int, title: str, closes: int) -> tuple[str, str]:
    """A forge squash merge: subject ends with the PR number, body Closes the issue."""
    return (f"{title} (#{pr})", f"## Summary\n\nwork\n\nCloses #{closes}\n")


def test_renders_label_grouped_section(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    issues = [
        _issue(101, "Add a shiny thing", ["enhancement"]),
        _issue(102, "Fix a broken thing", ["bug"]),
        _issue(103, "Document the thing", ["documentation"]),
        _issue(104, "Refactor internals", ["chore"]),
        _issue(105, "Unlabeled change", []),
    ]
    # PR numbers deliberately differ from issue numbers.
    commits = [
        _commit(901, "Add a shiny thing", 101),
        _commit(902, "Fix a broken thing", 102),
        _commit(903, "Document the thing", 103),
        _commit(904, "Refactor internals", 104),
        _commit(905, "Unlabeled change", 105),
    ]
    _write_fake_bins(bin_dir, issues=issues, commits=commits)

    result = _run_derive(bin_dir)

    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert out.startswith("## [0.11.0] — 2026-07-19")
    # Keep-a-Changelog section order: Added, Changed, Fixed, Documentation.
    assert (
        out.index("### Added")
        < out.index("### Changed")
        < out.index("### Fixed")
        < out.index("### Documentation")
    )
    # Entries carry the ISSUE number (from the milestone), not the PR number.
    assert "- Add a shiny thing (#101)" in out
    assert "- Fix a broken thing (#102)" in out
    assert "- Document the thing (#103)" in out
    assert "#901" not in out  # PR numbers never leak into the rendered section
    # chore + unlabeled both fall through to Changed.
    changed_block = out[out.index("### Changed") : out.index("### Fixed")]
    assert "- Refactor internals (#104)" in changed_block
    assert "- Unlabeled change (#105)" in changed_block


def test_cross_reference_passes_when_matched(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    issues = [_issue(201, "Thing one", ["bug"]), _issue(202, "Thing two", ["enhancement"])]
    commits = [_commit(801, "Thing one", 201), _commit(802, "Thing two", 202)]
    _write_fake_bins(bin_dir, issues=issues, commits=commits)

    result = _run_derive(bin_dir)

    assert result.returncode == 0, result.stderr
    assert "2 closed issues in milestone v0.11.0" in result.stderr
    assert "2 PR merges in v0.10.0..HEAD" in result.stderr
    assert "all milestone issues have corresponding merges" in result.stderr
    assert "all merges in tag range have corresponding milestone issues" in result.stderr


def test_milestone_issue_closed_by_different_pr_number(tmp_path):
    """The regression the review caught: issue #1087 closed by PR #1804.

    The milestone lists issue #1087; the squash subject ends `(#1804)` and the
    body says `Closes #1087`. Cross-reference must match them via the closing
    reference (not the trailing PR number) and the render must cite the issue.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    issues = [_issue(1087, "Surface reviewer time-nudge warnings", ["enhancement"])]
    commits = [_commit(1804, "Surface reviewer time-nudge warnings", 1087)]
    _write_fake_bins(bin_dir, issues=issues, commits=commits)

    result = _run_derive(bin_dir)

    assert result.returncode == 0, result.stderr
    assert "- Surface reviewer time-nudge warnings (#1087)" in result.stdout
    assert "all milestone issues have corresponding merges" in result.stderr
    assert "all merges in tag range have corresponding milestone issues" in result.stderr


def test_aborts_when_milestone_issue_has_no_merge(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    issues = [_issue(301, "Merged thing", ["bug"]), _issue(302, "Never merged", ["bug"])]
    commits = [_commit(701, "Merged thing", 301)]
    _write_fake_bins(bin_dir, issues=issues, commits=commits)

    result = _run_derive(bin_dir)

    assert result.returncode != 0
    assert result.stdout == ""
    assert (
        "milestone v0.11.0 has issue #302 (closed) with no PR merge in v0.10.0..HEAD"
        in result.stderr
    )


def test_aborts_when_merge_has_no_milestone_issue(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    issues = [_issue(401, "Milestoned thing", ["bug"])]
    # PR #888 closes issue #999, which is not in the milestone.
    commits = [_commit(801, "Milestoned thing", 401), _commit(888, "Rogue merge", 999)]
    _write_fake_bins(bin_dir, issues=issues, commits=commits)

    result = _run_derive(bin_dir)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "PR #888 merged in tag range with no v0.11.0 milestone issue" in result.stderr


def test_ignores_subjects_without_pr_number(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    issues = [_issue(501, "Real change", ["enhancement"])]
    commits = [
        _commit(801, "Real change", 501),
        ("chore: begin v0.11.0.dev0 development [skip ci]", ""),
        ("Merge pull request #77 from fuzzypete/some-branch", ""),
        ("chore: release v0.10.0", ""),
    ]
    _write_fake_bins(bin_dir, issues=issues, commits=commits)

    result = _run_derive(bin_dir)

    # The no-(#N)/no-Closes chore/merge subjects must NOT be treated as merged
    # PRs, so the reverse cross-reference stays clean and the render succeeds.
    assert result.returncode == 0, result.stderr
    assert "1 PR merges in v0.10.0..HEAD" in result.stderr
    assert "- Real change (#501)" in result.stdout


def _seam_env(dry_run: bool) -> dict:
    """Environment with git/gh/make/sed mocked for a promote-rc.sh seam run.

    Exported bash functions beat PATH, so they survive the script's
    `/opt/homebrew/bin` prepend where fake executables would be shadowed. `sed`
    is shimmed to `-i.bak` form so the script's BSD `sed -i ''` works on Linux CI
    too; `command sed` reaches the real binary for non-in-place uses.
    """
    env = os.environ.copy()
    env["BASH_FUNC_git%%"] = """() {
  case "$1 $2" in
    "rev-parse --abbrev-ref") echo "release/v0.11" ;;
    "status --porcelain") return 0 ;;
    "show-ref --verify") return 1 ;;
    "rev-parse --verify") return 1 ;;
    "ls-remote --tags") return 0 ;;
    "tag --list") echo "v0.10.0" ;;
    *) echo "+ git $*" ;;
  esac
}"""
    env["BASH_FUNC_gh%%"] = """() {
  if [[ "$1 $2" == "issue list" ]]; then
    echo 0
  fi
}"""
    env["BASH_FUNC_make%%"] = "() {\n  return 0\n}"
    env["BASH_FUNC_sed%%"] = """() {
  if [[ "$1" == "-i" ]]; then
    shift
    if [[ -z "$1" ]]; then shift; fi
    local script="$1"; shift
    command sed -i.forgebak -e "$script" "$@"
    for f in "$@"; do rm -f "$f.forgebak"; done
  else
    command sed "$@"
  fi
}"""
    return env


def _seam_setup(tmp_path: Path) -> tuple[Path, Path, str]:
    """Lay out a tmp repo with promote-rc.sh + a stubbed deriver. Returns
    (scripts_dir, changelog_path, original_changelog)."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    shutil.copy(PROMOTE, scripts_dir / "promote-rc.sh")

    sentinel = "## [0.11.0] — 2026-07-19\n\n### Added\n\n- Derived thing (#123)\n"
    (scripts_dir / "derive_changelog.py").write_text(
        "import sys\n"
        "sys.stderr.write('[forge]   1 closed issues in milestone v0.11.0\\n')\n"
        f"sys.stdout.write({sentinel!r})\n",
        encoding="utf-8",
    )

    (tmp_path / "pyproject.toml").write_text('version = "0.11.0rc1"\n', encoding="utf-8")
    original_changelog = (
        "# Changelog\n\n## [Unreleased]\n\n- operator scratch note\n\n"
        "## [0.10.0] — 2026-06-01\n\n- Old release note.\n"
    )
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(original_changelog, encoding="utf-8")
    return scripts_dir, changelog, original_changelog


def test_promote_dry_run_prints_derived_section_without_writing(tmp_path):
    """Seam: promote-rc.sh --dry-run derives + prints, leaves CHANGELOG alone."""
    scripts_dir, changelog, original = _seam_setup(tmp_path)

    result = subprocess.run(
        ["bash", str(scripts_dir / "promote-rc.sh"), "--dry-run", "0.11.0"],
        cwd=tmp_path,
        env=_seam_env(dry_run=True),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "- Derived thing (#123)" in result.stdout
    assert "[dry-run] derived CHANGELOG section for v0.11.0" in result.stdout
    # CHANGELOG.md must be untouched in dry-run.
    assert changelog.read_text(encoding="utf-8") == original


def test_promote_writes_derived_section_preserving_unreleased(tmp_path):
    """Seam: a real (non-dry-run) promote splices the derived section in above the
    prior release while leaving the [Unreleased] scratchpad and its notes intact."""
    scripts_dir, changelog, _ = _seam_setup(tmp_path)

    result = subprocess.run(
        ["bash", str(scripts_dir / "promote-rc.sh"), "0.11.0"],
        cwd=tmp_path,
        env=_seam_env(dry_run=False),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    written = changelog.read_text(encoding="utf-8")
    # [Unreleased] and its scratch note are preserved verbatim.
    assert "## [Unreleased]\n\n- operator scratch note" in written
    # Derived section spliced in above the prior release, not into [Unreleased].
    assert "## [0.11.0] — 2026-07-19" in written
    assert "- Derived thing (#123)" in written
    assert written.index("## [Unreleased]") < written.index("## [0.11.0]")
    assert written.index("## [0.11.0]") < written.index("## [0.10.0]")
    # The scratch note stays under [Unreleased], above the derived section.
    assert written.index("- operator scratch note") < written.index("## [0.11.0]")
