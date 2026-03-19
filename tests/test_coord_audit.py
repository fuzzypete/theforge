"""Tests for coord_audit helper functions."""

from __future__ import annotations

import json
from pathlib import Path

from theforge.coord_audit import has_review_approve


class TestHasReviewApprove:
    def test_no_history_file(self, tmp_path: Path) -> None:
        """Missing history.jsonl returns False (safe default)."""
        assert has_review_approve(tmp_path, "my-spec") is False

    def test_empty_history_file(self, tmp_path: Path) -> None:
        """Empty history.jsonl returns False."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        (audits / "history.jsonl").write_text("", encoding="utf-8")
        assert has_review_approve(tmp_path, "my-spec") is False

    def test_approve_present(self, tmp_path: Path) -> None:
        """Returns True when a matching slug has an APPROVE review."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        record = {
            "task": {"slug": "my-spec", "name": "My Spec"},
            "reviews": [{"cycle": 1, "verdict": "APPROVE", "summary": "Good"}],
        }
        (audits / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        assert has_review_approve(tmp_path, "my-spec") is True

    def test_no_approve_request_changes(self, tmp_path: Path) -> None:
        """Returns False when reviews exist but none are APPROVE."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        record = {
            "task": {"slug": "my-spec", "name": "My Spec"},
            "reviews": [{"cycle": 1, "verdict": "REQUEST_CHANGES", "summary": "Fix this"}],
        }
        (audits / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        assert has_review_approve(tmp_path, "my-spec") is False

    def test_approve_for_different_slug(self, tmp_path: Path) -> None:
        """Returns False when APPROVE exists but for a different slug."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        record = {
            "task": {"slug": "other-spec", "name": "Other Spec"},
            "reviews": [{"cycle": 1, "verdict": "APPROVE", "summary": "Good"}],
        }
        (audits / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        assert has_review_approve(tmp_path, "my-spec") is False

    def test_approve_in_second_record(self, tmp_path: Path) -> None:
        """Returns True when APPROVE is in second record, first has REQUEST_CHANGES."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        rec1 = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "REQUEST_CHANGES"}],
        }
        rec2 = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        content = json.dumps(rec1) + "\n" + json.dumps(rec2) + "\n"
        (audits / "history.jsonl").write_text(content, encoding="utf-8")
        assert has_review_approve(tmp_path, "my-spec") is True

    def test_no_reviews_key(self, tmp_path: Path) -> None:
        """Returns False when record has no 'reviews' key (e.g. ALREADY_DONE run)."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        record = {"task": {"slug": "my-spec"}, "outcome": {"final_phase": "DONE"}}
        (audits / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        assert has_review_approve(tmp_path, "my-spec") is False

    def test_malformed_json_line_skipped(self, tmp_path: Path) -> None:
        """Malformed JSON lines are skipped; valid lines still checked."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        record = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        content = "not-valid-json\n" + json.dumps(record) + "\n"
        (audits / "history.jsonl").write_text(content, encoding="utf-8")
        assert has_review_approve(tmp_path, "my-spec") is True

    def test_empty_reviews_list(self, tmp_path: Path) -> None:
        """Returns False when reviews list is empty."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        record = {"task": {"slug": "my-spec"}, "reviews": []}
        (audits / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        assert has_review_approve(tmp_path, "my-spec") is False
