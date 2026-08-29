"""Placeholder content must not satisfy a check it does not resolve (#2129).

The shape gate tests structure as a proxy for substance. These tests pin the
boundary: text merely *shaped* like an example or a criterion — a fenced TODO,
a placeholder bullet — keeps its finding standing.
"""

from __future__ import annotations

import pytest

from theforge.shape_check.heuristics import (
    check_bug_missing_observed,
    check_missing_acceptance_criteria,
    check_missing_example,
)
from theforge.shape_check.placeholders import (
    PLACEHOLDER_MARKER,
    is_placeholder_line,
    is_placeholder_only,
    strip_placeholder_content,
)

# The body `forge groom 1108` actually proposed — verbatim from the story.
_ISSUE_1108_STUB = """## Example

```
TODO: replace with a concrete example — sample output, target sketch,
or before/after snippet that lets a reviewer eyeball the behavior.
```
"""

_REAL_EXAMPLE = """## Example

```
$ forge groom 1108
Post-groom verdict: needs_grooming_missing_example
```
"""


# ── strip / detect primitives ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "line",
    [
        f"{PLACEHOLDER_MARKER}: replace with a concrete example",
        "TODO: fill this in",
        "- TODO(forge-groom): replace with criteria",
        "  * TBD",
        "1. FIXME: needs content",
        "- [ ] TODO: decide",
        "> XXX placeholder",
        "<insert observation here>",
        "<fill in>",
    ],
)
def test_placeholder_lines_are_recognized(line):
    assert is_placeholder_line(line)


@pytest.mark.parametrize(
    "line",
    [
        "- Export emits a downloadable file",
        "the todo list renders newest-first",
        "```",
        "",
    ],
)
def test_content_lines_are_not_placeholders(line):
    assert not is_placeholder_line(line)


def test_strip_drops_fenced_block_left_empty():
    stripped = strip_placeholder_content(_ISSUE_1108_STUB)
    assert "```" not in stripped
    assert "TODO" not in stripped


def test_strip_keeps_fenced_block_with_real_content():
    stripped = strip_placeholder_content(_REAL_EXAMPLE)
    assert "forge groom 1108" in stripped
    assert stripped.count("```") == 2


def test_strip_keeps_real_lines_alongside_placeholders():
    section = "- Export emits a file\n- TODO: decide the format\n"
    stripped = strip_placeholder_content(section)
    assert "Export emits a file" in stripped
    assert "TODO" not in stripped


def test_is_placeholder_only_distinguishes_mixed_content():
    assert is_placeholder_only(_ISSUE_1108_STUB.split("\n", 2)[2])
    assert not is_placeholder_only(_REAL_EXAMPLE)
    assert not is_placeholder_only("")


def test_strip_keeps_unclosed_fence_with_content():
    section = "```\nreal sample output\n"
    assert "real sample output" in strip_placeholder_content(section)


def test_placeholder_only_observed_section_is_refused():
    body = (
        "## Observed\n\n"
        "<insert observation here>\n\n"
        "## Expected\n\n"
        "The command exits 0.\n\n"
        "## Diagnosis\n\n"
        "- **Observed symptom.** The command exits 1.\n"
        "- **Evidence.** Run `abc123`.\n"
        "- **Ruled out.** Shell alias drift.\n"
        "- **Confirmed cause.** Exit code is inverted.\n"
        "- **Affected code path.** `cli.main`.\n"
        "- **Fix-success criterion.** Success exits 0.\n"
    )
    reason = check_bug_missing_observed("Exit code", body, ["bug"])
    assert reason is not None
    assert reason.code == "missing_observed"
    assert "placeholder" in reason.detail.lower()


# ── check_missing_example ─────────────────────────────────────────────────


def test_groom_1108_placeholder_body_still_reports_missing_example():
    """The exact regression: a fenced TODO passed all three structural tests."""
    reason = check_missing_example("Add export", _ISSUE_1108_STUB, ["enhancement"])
    assert reason is not None
    assert reason.code == "missing_example"
    assert "placeholder" in reason.detail.lower()


def test_marked_placeholder_example_still_reports_missing_example():
    body = f"## Example\n\n```\n{PLACEHOLDER_MARKER}: replace with a concrete example.\n```\n"
    reason = check_missing_example("Add export", body, ["enhancement"])
    assert reason is not None
    assert reason.code == "missing_example"


def test_real_example_still_passes():
    assert check_missing_example("Add export", _REAL_EXAMPLE, ["enhancement"]) is None


def test_example_with_placeholder_plus_real_content_passes():
    body = (
        "## Example\n\n"
        "```\n"
        "TODO: add the error case too\n"
        "$ forge groom 1108 --apply\n"
        "Applied body restructure to #1108.\n"
        "```\n"
    )
    assert check_missing_example("Add export", body, ["enhancement"]) is None


# ── check_missing_acceptance_criteria ─────────────────────────────────────


def test_placeholder_only_acceptance_criteria_still_reports_finding():
    body = (
        "## Acceptance criteria\n\n"
        f"- {PLACEHOLDER_MARKER}: replace with verifiable observable-behavior bullets "
        "(returns/emits/writes/...)\n"
    )
    reason = check_missing_acceptance_criteria("Add export", body, ["enhancement"])
    assert reason is not None
    assert reason.code == "missing_acceptance_criteria"
    assert "placeholder" in reason.detail.lower()


def test_real_acceptance_criteria_still_passes():
    body = "## Acceptance criteria\n\n- Export writes a CSV to the download directory\n"
    assert check_missing_acceptance_criteria("Add export", body, ["enhancement"]) is None


def test_mixed_acceptance_criteria_passes_on_the_real_bullet():
    body = (
        "## Acceptance criteria\n\n"
        "- TODO: confirm the filename convention\n"
        "- Export writes a CSV to the download directory\n"
    )
    assert check_missing_acceptance_criteria("Add export", body, ["enhancement"]) is None


def test_missing_section_detail_unchanged():
    body = "## What\n\nStuff.\n"
    reason = check_missing_acceptance_criteria("Add export", body, ["enhancement"])
    assert reason is not None
    assert "No acceptance criteria section" in reason.detail
