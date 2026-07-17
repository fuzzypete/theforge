"""Regression: a DONE row must never surface a stale blocking review verdict.

Story #1217/#1425 (issue #1749): when the recorded ``reviews`` list omits the
terminal APPROVE cycle — leaving an intermediate REQUEST_CHANGES entry as the
last item — the completed-status reader rendered that blocking verdict as the
DETAIL for a DONE row, contradicting the row's own approved outcome. The
reader already guards the mirror-image case (a failed row must not surface a
stale APPROVE); this guards the success side.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from theforge.sprint.status_reader import read_completed_status


def _write_summary(tmp_path: Path, stories: list[dict]) -> Path:
    summary_dir = tmp_path / ".forge" / "logs" / "review-verdict-sprint"
    summary_dir.mkdir(parents=True, exist_ok=True)
    summary_path = summary_dir / "sprint-summary.yaml"
    summary_path.write_text(
        yaml.dump(
            {
                "sprint": {"name": "review-verdict-sprint", "run_id": "run-rv"},
                "stories": stories,
            }
        ),
        encoding="utf-8",
    )
    return summary_path


def _write_audit(tmp_path: Path, slug: str, audit_data: dict) -> None:
    audit_dir = tmp_path / ".forge" / "logs" / "review-verdict-sprint" / slug
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "audit.yaml").write_text(yaml.dump(audit_data), encoding="utf-8")


def test_done_row_does_not_surface_stale_request_changes_verdict(tmp_path: Path) -> None:
    """Reproduces #1217: reviews list ends in REQUEST_CHANGES (the terminal
    APPROVE cycle was never appended) but outcome.success is True. The DETAIL
    must not assert REQUEST_CHANGES / blocked approval on this DONE row."""
    summary_path = _write_summary(
        tmp_path,
        [
            {
                "slug": "issue-1217",
                "path": "Issue #1217",
                "outcome": "DONE",
                "cost_usd": 1.0,
            }
        ],
    )
    _write_audit(
        tmp_path,
        "issue-1217",
        {
            "outcome": {
                "success": True,
                "final_phase": "DONE",
                "message": "Review approved after 2 cycle(s), 2 dev iteration(s).",
            },
            "verdict": None,
            "reviews": [
                {
                    "verdict": "REQUEST_CHANGES",
                    "summary": (
                        "Hard convention violations detected after gate PASS; "
                        "approval is blocked until they are fixed."
                    ),
                }
            ],
        },
    )

    entries = read_completed_status(summary_path)
    assert len(entries) == 1
    detail = entries[0].detail
    assert "REQUEST_CHANGES" not in detail
    assert "blocked" not in detail.lower()
    assert detail == "Review approved after 2 cycle(s), 2 dev iteration(s)."


def test_done_row_with_trailing_approve_review_still_renders_approve(
    tmp_path: Path,
) -> None:
    """When the reviews list correctly ends in APPROVE, that verdict is still
    the rendered detail — the guard only suppresses blocking verdicts on
    success rows, it does not disable the review-detail path entirely."""
    summary_path = _write_summary(
        tmp_path,
        [
            {
                "slug": "issue-2000",
                "path": "Issue #2000",
                "outcome": "DONE",
                "cost_usd": 1.0,
            }
        ],
    )
    _write_audit(
        tmp_path,
        "issue-2000",
        {
            "outcome": {"success": True, "final_phase": "DONE", "message": "done"},
            "verdict": None,
            "reviews": [{"verdict": "APPROVE", "summary": "Looks good."}],
        },
    )

    entries = read_completed_status(summary_path)
    assert len(entries) == 1
    assert "APPROVE" in entries[0].detail


def test_failed_row_still_appends_stale_request_changes_verdict(tmp_path: Path) -> None:
    """The pre-existing failure-outcome guard is unaffected: a FAILED row may
    still annotate its message with the last review verdict."""
    summary_path = _write_summary(
        tmp_path,
        [
            {
                "slug": "issue-3000",
                "path": "Issue #3000",
                "outcome": "FAILED",
                "cost_usd": 1.0,
            }
        ],
    )
    _write_audit(
        tmp_path,
        "issue-3000",
        {
            "outcome": {"success": False, "final_phase": "ESCALATE", "message": "gave up"},
            "verdict": None,
            "reviews": [{"verdict": "REQUEST_CHANGES", "summary": "still broken"}],
        },
    )

    entries = read_completed_status(summary_path)
    assert len(entries) == 1
    assert entries[0].detail == "gave up (review verdict: REQUEST_CHANGES)"
