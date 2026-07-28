"""Tests for scripts/forward_port_guard.py and forward-port.yml's use of it.

The guard exists because forward-port.yml used to revert pyproject.toml and
CHANGELOG.md wholesale to main's copy on every port (#2016): protecting
`[project] version` that way also discarded `requires-python` and the ruff
`target-version`, silently, while the port still reported success.

Covered here:
  - the release/v0.13 scenario from #2016: main's version survives, the release
    line's requires-python / ruff target-version port through
  - changelog rolls are excluded by section, not by reverting the file
  - anything the guard cannot classify, or would drop outside that scope, is an
    error rather than a silent drop
  - conflicted version files are three-way merged, not resolved to --ours
  - forward-port.yml actually calls the guard and no longer reverts the files
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "forward_port_guard.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "forward-port.yml"


def _load_guard():
    spec = importlib.util.spec_from_file_location("forward_port_guard", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


MAIN_PYPROJECT = """\
[project]
name = "theforge"
version = "0.12.0.dev0"
requires-python = ">=3.11"
dependencies = ["pyyaml>=6.0"]

[tool.ruff]
target-version = "py311"
line-length = 99
"""

# release/v0.13 at the time of PR #2001: the rc bump plus the two changes #2012
# made for real reasons, which the old whole-file revert threw away.
RELEASE_PYPROJECT = """\
[project]
name = "theforge"
version = "0.13.0rc9"
requires-python = ">=3.12"
dependencies = ["pyyaml>=6.0"]

[tool.ruff]
target-version = "py312"
line-length = 99
"""

MAIN_CHANGELOG = """\
# Changelog

## [Unreleased]

### Fixed

- main-only fix (#999)

## [0.12.0] — 2026-07-01

- shipped earlier
"""

RELEASE_CHANGELOG = """\
# Changelog

## [Unreleased]

## [0.13.0rc9] — 2026-07-25

### Fixed

- release-line fix (#1000)

## [0.12.0] — 2026-07-01

- shipped earlier
"""


# --------------------------------------------------------------------------
# pyproject.toml
# --------------------------------------------------------------------------


def test_release_v013_port_keeps_main_version_and_carries_the_rest():
    """The #2016 regression, end to end on the real inputs.

    main keeps 0.12.0.dev0; the release line's narrowed floor and ruff target
    must arrive on main, or the port ships a tree whose CI matrix and declared
    floor disagree (exactly what test_requires_python_floor_matches_ci_matrix
    caught on PR #2001).
    """
    result, report = guard.protect_pyproject(MAIN_PYPROJECT, RELEASE_PYPROJECT)

    assert 'version = "0.12.0.dev0"' in result
    assert '"0.13.0rc9"' not in result
    assert 'requires-python = ">=3.12"' in result
    assert 'target-version = "py312"' in result
    assert any("excluded the release-line version bump" in line for line in report)


def test_pyproject_with_only_a_version_bump_reports_nothing_else_ported():
    result, report = guard.protect_pyproject(MAIN_PYPROJECT, MAIN_PYPROJECT)

    assert result == MAIN_PYPROJECT
    assert any("already matches main" in line for line in report)
    assert any("no release-branch change to port" in line for line in report)


def test_pyproject_dependency_added_on_the_release_line_ports_through():
    release = RELEASE_PYPROJECT.replace(
        'dependencies = ["pyyaml>=6.0"]',
        'dependencies = ["pyyaml>=6.0", "python-dotenv>=1.0"]',
    )

    result, _ = guard.protect_pyproject(MAIN_PYPROJECT, release)

    assert "python-dotenv>=1.0" in result
    assert 'version = "0.12.0.dev0"' in result


def test_missing_project_version_is_an_error_not_a_guess():
    with pytest.raises(guard.GuardError, match="no `version` field"):
        guard.protect_pyproject('[project]\nname = "x"\n', RELEASE_PYPROJECT)


def test_version_outside_the_project_table_is_not_mistaken_for_it():
    """A `version` key in another table must not be rewritten."""
    merged = RELEASE_PYPROJECT + '\n[tool.other]\nversion = "9.9.9"\n'

    result, _ = guard.protect_pyproject(MAIN_PYPROJECT, merged)

    assert 'version = "9.9.9"' in result
    assert 'version = "0.12.0.dev0"' in result


def test_pyproject_verification_rejects_content_dropped_outside_the_version():
    """The self-check is what makes 'no silent drops' mechanical.

    If a future edit to the scoped rewrite lost a table, the port must fail
    rather than open a green PR over a partial file.
    """
    doctored = MAIN_PYPROJECT.replace('\n[tool.ruff]\ntarget-version = "py311"\n', "\n")

    with pytest.raises(guard.GuardError, match="outside \\[project\\] version"):
        guard._verify_pyproject(RELEASE_PYPROJECT, doctored)


# --------------------------------------------------------------------------
# CHANGELOG.md
# --------------------------------------------------------------------------


def test_changelog_roll_is_excluded_by_section_not_by_reverting_the_file():
    result, report = guard.protect_changelog(MAIN_CHANGELOG, RELEASE_CHANGELOG)

    assert "main-only fix (#999)" in result, "main's [Unreleased] must survive"
    assert "0.13.0rc9" not in result, "the release roll must not land on main"
    assert "## [0.12.0] — 2026-07-01" in result
    assert any("[0.13.0rc9]" in line for line in report)


def test_changelog_edits_outside_the_roll_port_through():
    """An unrelated release-branch changelog edit is release content, not a roll."""
    release = RELEASE_CHANGELOG.replace("- shipped earlier", "- shipped earlier (corrected)")

    result, _ = guard.protect_changelog(MAIN_CHANGELOG, release)

    assert "- shipped earlier (corrected)" in result
    assert "main-only fix (#999)" in result


def test_changelog_preamble_changes_port_through():
    release = RELEASE_CHANGELOG.replace("# Changelog", "# Changelog\n\nNow with a preamble.")

    result, _ = guard.protect_changelog(MAIN_CHANGELOG, release)

    assert "Now with a preamble." in result


def test_unreleased_is_restored_at_the_top_when_the_roll_removed_it():
    release = RELEASE_CHANGELOG.replace("## [Unreleased]\n\n", "", 1)

    result, report = guard.protect_changelog(MAIN_CHANGELOG, release)

    sections = [line for line in result.splitlines() if line.startswith("## ")]
    assert sections[0] == "## [Unreleased]"
    assert "main-only fix (#999)" in result
    assert any("restored main's [Unreleased]" in line for line in report)


def test_unclassifiable_new_section_is_reported_not_dropped():
    """A non-version section main lacks cannot be proven to be a roll.

    Dropping it would be exactly the silent loss #2016 is about, so the guard
    fails the port instead.
    """
    release = RELEASE_CHANGELOG.replace(
        "## [0.12.0] — 2026-07-01",
        "## Migration notes\n\n- read this\n\n## [0.12.0] — 2026-07-01",
    )

    with pytest.raises(guard.GuardError, match="cannot be classified"):
        guard.protect_changelog(MAIN_CHANGELOG, release)


def test_main_without_unreleased_refuses_to_guess():
    main = MAIN_CHANGELOG.replace("## [Unreleased]\n\n### Fixed\n\n- main-only fix (#999)\n\n", "")

    with pytest.raises(guard.GuardError, match="refusing to guess"):
        guard.protect_changelog(main, RELEASE_CHANGELOG)


def test_changelog_verification_rejects_a_dropped_non_roll_section():
    """A deletion outside the roll scope is an error, not an accepted port."""
    doctored = MAIN_CHANGELOG  # missing the release line's [0.12.0] edit entirely
    merged = RELEASE_CHANGELOG.replace("- shipped earlier", "- shipped earlier (corrected)")

    with pytest.raises(guard.GuardError, match="dropped or altered section"):
        guard._verify_changelog(MAIN_CHANGELOG, merged, doctored)


# --------------------------------------------------------------------------
# conflict resolution
# --------------------------------------------------------------------------


def test_resolve_merges_a_conflicted_pyproject_instead_of_taking_ours():
    """The rc bump conflicts on every port; --ours would drop the real change.

    base -> ours: main bumped its dev version.
    base -> theirs: the release line cut an rc AND narrowed requires-python.
    The resolution must keep main's version and carry the narrowing.
    """
    base = MAIN_PYPROJECT.replace('version = "0.12.0.dev0"', 'version = "0.12.0a0"')
    ours = MAIN_PYPROJECT
    theirs = RELEASE_PYPROJECT

    result, report = guard.resolve("pyproject", base, ours, theirs)

    assert 'version = "0.12.0.dev0"' in result
    assert 'requires-python = ">=3.12"' in result
    assert 'target-version = "py312"' in result
    assert "<<<<<<<" not in result
    assert any(line.startswith("conflict resolution:") for line in report)


def test_resolve_merges_a_conflicted_changelog_keeping_mains_unreleased():
    base = MAIN_CHANGELOG.replace("### Fixed\n\n- main-only fix (#999)\n\n", "")
    ours = MAIN_CHANGELOG
    theirs = RELEASE_CHANGELOG.replace("- shipped earlier", "- shipped earlier (corrected)")

    result, _ = guard.resolve("changelog", base, ours, theirs)

    assert "main-only fix (#999)" in result
    assert "- shipped earlier (corrected)" in result
    assert "0.13.0rc9" not in result
    assert "<<<<<<<" not in result


def test_resolve_reports_a_genuine_conflict_rather_than_picking_a_side():
    base = MAIN_PYPROJECT
    ours = MAIN_PYPROJECT.replace("line-length = 99", "line-length = 88")
    theirs = RELEASE_PYPROJECT.replace("line-length = 99", "line-length = 120")

    with pytest.raises(guard.GuardError, match="genuine conflict"):
        guard.resolve("pyproject", base, ours, theirs)


# --------------------------------------------------------------------------
# CLI + workflow wiring
# --------------------------------------------------------------------------


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_protect_rewrites_the_merged_file_in_place(tmp_path):
    main = tmp_path / "main.toml"
    merged = tmp_path / "pyproject.toml"
    main.write_text(MAIN_PYPROJECT)
    merged.write_text(RELEASE_PYPROJECT)

    result = _run_cli(
        "protect", "--kind", "pyproject", "--main", str(main), "--merged", str(merged)
    )

    assert result.returncode == 0, result.stderr
    assert 'version = "0.12.0.dev0"' in merged.read_text()
    assert 'requires-python = ">=3.12"' in merged.read_text()
    assert "[forward-port]" in result.stdout


def test_cli_exits_nonzero_and_annotates_on_a_guard_failure(tmp_path):
    main = tmp_path / "main.md"
    merged = tmp_path / "CHANGELOG.md"
    main.write_text("# Changelog\n\n## [0.12.0]\n\n- x\n")
    merged.write_text(RELEASE_CHANGELOG)

    result = _run_cli(
        "protect", "--kind", "changelog", "--main", str(main), "--merged", str(merged)
    )

    assert result.returncode == 1
    assert "::error::" in result.stderr
    # The merged file must be left untouched when the guard refuses.
    assert merged.read_text() == RELEASE_CHANGELOG


def test_workflow_no_longer_reverts_the_version_files_wholesale():
    workflow = WORKFLOW.read_text()

    assert "git checkout ORIG_HEAD -- pyproject.toml CHANGELOG.md" not in workflow, (
        "the whole-file revert is the #2016 defect; the guard replaces it"
    )
    assert "git checkout --ours -- pyproject.toml CHANGELOG.md" not in workflow, (
        "--ours on a version-file conflict drops release-line changes just as silently"
    )


def test_workflow_invokes_the_guard_for_both_protected_files():
    workflow = WORKFLOW.read_text()

    assert "scripts/forward_port_guard.py protect --kind pyproject" in workflow
    assert "scripts/forward_port_guard.py protect --kind changelog" in workflow
    assert "scripts/forward_port_guard.py resolve --kind" in workflow
    # A guard failure must fail the step (and so reach the issue/Slack steps)
    # rather than being swallowed by a pipe into the run summary.
    assert "set -o pipefail" in workflow
