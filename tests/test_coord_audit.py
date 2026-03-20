"""Tests for coord_audit helper functions."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

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

    def test_stale_approve_branch_ahead(self, tmp_path: Path) -> None:
        """Returns False when APPROVE exists but branch has unmerged commits (abandoned run)."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        record = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        (audits / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"3\n", stderr=b"")
        with patch("theforge.coord_audit.subprocess.run", return_value=mock_result):
            assert has_review_approve(tmp_path, "my-spec", "main") is False

    def test_valid_approve_branch_merged(self, tmp_path: Path) -> None:
        """Returns True when APPROVE exists and branch is merged (0 commits ahead)."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        record = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        (audits / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"0\n", stderr=b"")
        with patch("theforge.coord_audit.subprocess.run", return_value=mock_result):
            assert has_review_approve(tmp_path, "my-spec", "main") is True

    def test_valid_approve_branch_absent(self, tmp_path: Path) -> None:
        """Returns True when APPROVE exists and branch is absent (non-zero git exit)."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        record = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        (audits / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=128, stdout=b"", stderr=b"unknown revision"
        )
        with patch("theforge.coord_audit.subprocess.run", return_value=mock_result):
            assert has_review_approve(tmp_path, "my-spec", "main") is True

    def test_no_approve_record_with_base_branch(self, tmp_path: Path) -> None:
        """Returns False when no APPROVE record exists (baseline for new signature)."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        record = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "REQUEST_CHANGES"}],
        }
        (audits / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        assert has_review_approve(tmp_path, "my-spec", "main") is False

    def test_stale_approve_subprocess_timeout(self, tmp_path: Path) -> None:
        """Returns True (treat as valid) when git subprocess times out."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        record = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        (audits / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        with patch(
            "theforge.coord_audit.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
        ):
            # Timeout → helper returns False → APPROVE is not stale → True
            assert has_review_approve(tmp_path, "my-spec", "main") is True

    def test_stale_approve_non_integer_output(self, tmp_path: Path) -> None:
        """Returns True (treat as valid) when git outputs non-integer."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        record = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        (audits / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"not-a-number\n", stderr=b""
        )
        with patch("theforge.coord_audit.subprocess.run", return_value=mock_result):
            # ValueError from int() → helper returns False → APPROVE is not stale → True
            assert has_review_approve(tmp_path, "my-spec", "main") is True

    def test_custom_branch_pattern_passed_to_helper(self, tmp_path: Path) -> None:
        """Branch name is forwarded to git — verifies non-default branch patterns work."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        record = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        (audits / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"0\n", stderr=b"")
        with patch("theforge.coord_audit.subprocess.run", return_value=mock_result) as mock_run:
            result = has_review_approve(tmp_path, "my-spec", "main", branch="forge/my-spec")
        assert result is True
        # Verify the custom branch pattern was used in the git command
        call_args = mock_run.call_args[0][0]
        assert any("forge/my-spec" in arg for arg in call_args)
