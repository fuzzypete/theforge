"""Tests for scripts/derive_changelog.py and the promote-rc.sh CHANGELOG seam.

`derive_changelog.py` is the testable helper promote-rc.sh calls to build the
release section from milestone state + tag-range PR merges, replacing the old
blind `[Unreleased]` rename. Covers:
  - label-grouped rendering (`- Title (#N)` under Added/Fixed/Documentation/Changed)
  - cross-reference passes when milestone issues ⇔ `(#N)` merges match
  - cross-reference aborts (non-zero + discrepancy lines) when a milestone issue
    has no merge, or a merged PR has no milestone issue
  - chore/merge subjects with no `(#N)` are ignored (do not trip the reverse check)

Plus a seam test that runs `promote-rc.sh --dry-run` with the helper stubbed and
the surrounding git/gh calls mocked, asserting the derived section is printed and
CHANGELOG.md is left unmodified.

The helper's `gh` and `git` reads are PATH-mocked with fake executables; the
seam test uses exported bash functions so the mocks survive promote-rc.sh's
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


def _write_fake_bins(bin_dir: Path, *, issues: list[dict], git_subjects: list[str]) -> None:
    """Install fake `gh` and `git` on PATH for the derivation helper.

    `gh issue list ... --json number,title,labels` -> the issues fixture.
    `git log --pretty=%s <range>` -> the merge subjects fixture.
    """
    issues_file = bin_dir / "issues.json"
    issues_file.write_text(json.dumps(issues), encoding="utf-8")
    log_file = bin_dir / "gitlog.txt"
    log_file.write_text("".join(f"{s}\n" for s in git_subjects), encoding="utf-8")

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
    subjects = [
        "Add a shiny thing (#101)",
        "Fix a broken thing (#102)",
        "Document the thing (#103)",
        "Refactor internals (#104)",
        "Unlabeled change (#105)",
    ]
    _write_fake_bins(bin_dir, issues=issues, git_subjects=subjects)

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
    assert "- Add a shiny thing (#101)" in out
    assert "- Fix a broken thing (#102)" in out
    assert "- Document the thing (#103)" in out
    # chore + unlabeled both fall through to Changed.
    changed_block = out[out.index("### Changed") : out.index("### Fixed")]
    assert "- Refactor internals (#104)" in changed_block
    assert "- Unlabeled change (#105)" in changed_block


def test_cross_reference_passes_when_matched(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    issues = [_issue(201, "Thing one", ["bug"]), _issue(202, "Thing two", ["enhancement"])]
    subjects = ["Thing one (#201)", "Thing two (#202)"]
    _write_fake_bins(bin_dir, issues=issues, git_subjects=subjects)

    result = _run_derive(bin_dir)

    assert result.returncode == 0, result.stderr
    assert "2 closed issues in milestone v0.11.0" in result.stderr
    assert "2 PR merges in v0.10.0..HEAD" in result.stderr
    assert "all milestone issues have corresponding merges" in result.stderr
    assert "all merges in tag range have corresponding milestone issues" in result.stderr


def test_aborts_when_milestone_issue_has_no_merge(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    issues = [_issue(301, "Merged thing", ["bug"]), _issue(302, "Never merged", ["bug"])]
    subjects = ["Merged thing (#301)"]
    _write_fake_bins(bin_dir, issues=issues, git_subjects=subjects)

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
    subjects = ["Milestoned thing (#401)", "Rogue merge (#999)"]
    _write_fake_bins(bin_dir, issues=issues, git_subjects=subjects)

    result = _run_derive(bin_dir)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "PR #999 merged in tag range with no v0.11.0 milestone issue" in result.stderr


def test_ignores_subjects_without_pr_number(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    issues = [_issue(501, "Real change", ["enhancement"])]
    subjects = [
        "Real change (#501)",
        "chore: begin v0.11.0.dev0 development [skip ci]",
        "Merge pull request #77 from fuzzypete/some-branch",
        "chore: release v0.10.0",
    ]
    _write_fake_bins(bin_dir, issues=issues, git_subjects=subjects)

    result = _run_derive(bin_dir)

    # The no-(#N) chore/merge subjects must NOT be treated as merged PRs, so the
    # reverse cross-reference stays clean and the render succeeds.
    assert result.returncode == 0, result.stderr
    assert "1 PR merges in v0.10.0..HEAD" in result.stderr
    assert "- Real change (#501)" in result.stdout


def test_promote_dry_run_prints_derived_section_without_writing(tmp_path):
    """Seam test: promote-rc.sh --dry-run derives + prints, leaves CHANGELOG alone.

    The derivation helper is stubbed (its own behavior is unit-tested above); this
    exercises promote-rc.sh's wiring — that it calls the helper, echoes the
    rendered section in dry-run, and does not touch CHANGELOG.md.
    """
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    shutil.copy(PROMOTE, scripts_dir / "promote-rc.sh")

    sentinel = "## [0.11.0] — 2026-07-19\n\n### Added\n\n- Derived thing (#123)\n"
    stub = scripts_dir / "derive_changelog.py"
    stub.write_text(
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

    env = os.environ.copy()
    # Exported bash functions beat PATH, so they survive the script's
    # `/opt/homebrew/bin` prepend where fake executables would be shadowed.
    env["BASH_FUNC_git%%"] = """() {
  case "$1 $2" in
    "rev-parse --abbrev-ref") echo "release/v0.11" ;;
    "status --porcelain") return 0 ;;
    "show-ref --verify") return 1 ;;
    "rev-parse --verify") return 1 ;;
    "ls-remote --tags") return 0 ;;
    "describe --tags") echo "v0.10.0" ;;
    *) echo "+ git $*" ;;
  esac
}"""
    env["BASH_FUNC_gh%%"] = """() {
  if [[ "$1 $2" == "issue list" ]]; then
    echo 0
  fi
}"""

    result = subprocess.run(
        ["bash", str(scripts_dir / "promote-rc.sh"), "--dry-run", "0.11.0"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "- Derived thing (#123)" in result.stdout
    assert "[dry-run] derived CHANGELOG section for v0.11.0" in result.stdout
    # CHANGELOG.md must be untouched in dry-run.
    assert changelog.read_text(encoding="utf-8") == original_changelog
