"""Seam test: reviewer identity + story context surface in finding log lines.

Covers the flow from ReviewFinding.reporter (populated during parsing) through
_log_review_findings, the review-phase call site that emits the operator-facing
`[P1]`/`[P2]` lines. Asserts both the reviewer tag and the story tag appear, and
that the reporter tag is present even for the single-reviewer (no-pool) case.
"""

from theforge.coordinator import review_phase as rp
from theforge.review import ReviewFinding, ReviewResult
from theforge.task import TaskStory


def _finding(reporter: str) -> ReviewFinding:
    return ReviewFinding(
        severity="P1",
        file="src/theforge/coordinator/dev_phase.py",
        line=112,
        observed="The review retry path treats X as Y",
        suggestion=None,
        reporter=reporter,
    )


def _review(findings: list[ReviewFinding]) -> ReviewResult:
    return ReviewResult(
        verdict="REQUEST_CHANGES",
        summary="something to fix",
        findings=findings,
        story_matches=False,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=[],
        raw_yaml={},
    )


def _capture_findings(monkeypatch, review: ReviewResult, task: TaskStory) -> list[str]:
    captured: list[str] = []
    monkeypatch.setattr(rp, "_log", lambda msg: captured.append(msg))
    rp._log_review_findings(review, 1, 0, 0.0, None, task)
    return [m for m in captured if m.strip().startswith("[P")]


def test_finding_line_includes_reviewer_and_story(monkeypatch):
    task = TaskStory(name="Add retry backoff", slug="retry-backoff", github_issue=42)
    lines = _capture_findings(monkeypatch, _review([_finding("reviewer-2")]), task)
    assert len(lines) == 1
    line = lines[0]
    assert "[reviewer: reviewer-2]" in line
    assert "[story: Add retry backoff (#42)]" in line
    assert "[src/theforge/coordinator/dev_phase.py:112]" in line


def test_story_tag_omits_issue_number_when_absent(monkeypatch):
    task = TaskStory(name="Local story", slug="local-story", github_issue=None)
    lines = _capture_findings(monkeypatch, _review([_finding("reviewer-1")]), task)
    assert "[story: Local story]" in lines[0]
    assert "(#" not in lines[0]


def test_single_reviewer_reporter_tag_present(monkeypatch):
    """AC: the reporter tag is not conditionally hidden for the no-pool case."""
    task = TaskStory(name="Solo review", slug="solo", github_issue=7)
    lines = _capture_findings(monkeypatch, _review([_finding("solo-reviewer")]), task)
    assert "[reviewer: solo-reviewer]" in lines[0]


def test_unattributed_finding_renders_placeholder(monkeypatch):
    """An empty reporter still emits a tag (never a hidden/missing segment)."""
    task = TaskStory(name="Solo review", slug="solo", github_issue=7)
    lines = _capture_findings(monkeypatch, _review([_finding("")]), task)
    assert "[reviewer: ?]" in lines[0]
