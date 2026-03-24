"""Tests for finding_classifier.py — fingerprinting, classification, exit criteria."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from theforge.coord_state import CoordinatorState, FindingRecord
from theforge.finding_classifier import (
    _fingerprint,
    _jaccard,
    _matches_prior,
    _normalize_tokens,
    has_blocking_p1,
    net_new_p1s,
    update_finding_registry,
)
from theforge.review import ReviewFinding, ReviewResult


def _make_finding(
    description: str,
    file: str = "src/foo.py",
    severity: str = "P1",
    line: int | None = 10,
    suggestion: str = "fix it",
) -> ReviewFinding:
    return ReviewFinding(
        severity=severity,
        file=file,
        line=line,
        description=description,
        suggestion=suggestion,
    )


def _make_review(findings: list[ReviewFinding], verdict: str = "REQUEST_CHANGES") -> ReviewResult:
    return ReviewResult(
        verdict=verdict,
        summary="test review",
        findings=findings,
        story_matches=True,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=[],
        raw_yaml={},
    )


def _make_state() -> CoordinatorState:
    return CoordinatorState()


class TestNormalizeTokens:
    def test_lowercases_and_strips_punctuation(self):
        tokens = _normalize_tokens("Missing null-check in Foo.Bar!")
        assert "missing" in tokens
        assert "null" in tokens  # hyphen split
        assert "check" in tokens
        # Short tokens filtered out
        assert "in" not in tokens

    def test_empty_string(self):
        assert _normalize_tokens("") == frozenset()

    def test_filters_short_tokens(self):
        tokens = _normalize_tokens("a is ok go")
        # "a", "is", "ok", "go" are all ≤ 2 chars, all filtered
        assert len(tokens) == 0


class TestJaccard:
    def test_identical_sets(self):
        s = frozenset(["a", "b", "c"])
        assert _jaccard(s, s) == 1.0

    def test_disjoint_sets(self):
        a = frozenset(["foo", "bar"])
        b = frozenset(["baz", "qux"])
        assert _jaccard(a, b) == 0.0

    def test_partial_overlap(self):
        a = frozenset(["foo", "bar", "baz"])
        b = frozenset(["foo", "bar", "qux"])
        # intersection=2, union=4
        assert _jaccard(a, b) == pytest.approx(2 / 4)

    def test_both_empty(self):
        assert _jaccard(frozenset(), frozenset()) == 1.0

    def test_one_empty(self):
        assert _jaccard(frozenset(["foo"]), frozenset()) == 0.0


class TestFingerprint:
    def test_same_finding_same_fingerprint(self):
        fp1 = _fingerprint("P1", "src/foo.py", "Missing null check in handler")
        fp2 = _fingerprint("P1", "src/foo.py", "Missing null check in handler")
        assert fp1 == fp2

    def test_different_severity_different_fingerprint(self):
        fp1 = _fingerprint("P1", "src/foo.py", "Missing null check in handler")
        fp2 = _fingerprint("P2", "src/foo.py", "Missing null check in handler")
        assert fp1 != fp2

    def test_different_file_different_fingerprint(self):
        fp1 = _fingerprint("P1", "src/foo.py", "Missing null check in handler")
        fp2 = _fingerprint("P1", "src/bar.py", "Missing null check in handler")
        assert fp1 != fp2

    def test_paraphrased_may_match_at_threshold(self):
        # Same core tokens — should still produce same fingerprint
        fp1 = _fingerprint("P1", "src/foo.py", "Missing null check in handler method")
        fp2 = _fingerprint("P1", "src/foo.py", "Missing null check in handler method")
        assert fp1 == fp2


class TestMatchesPrior:
    def _make_record(
        self, description: str, file: str = "src/foo.py", severity: str = "P1"
    ) -> FindingRecord:
        fp = _fingerprint(severity, file, description)
        return FindingRecord(
            finding_id=fp,
            cycle_first_seen=1,
            cycle_last_seen=1,
            file=file,
            line=10,
            severity=severity,
            description=description,
            reporter="reviewer-a",
            disposition="net_new",
        )

    def test_identical_matches(self):
        finding = _make_finding("Missing null check in handler")
        record = self._make_record("Missing null check in handler")
        assert _matches_prior(finding, record)

    def test_different_file_no_match(self):
        finding = _make_finding("Missing null check in handler", file="src/bar.py")
        record = self._make_record("Missing null check in handler", file="src/foo.py")
        assert not _matches_prior(finding, record)

    def test_different_severity_no_match(self):
        finding = _make_finding("Missing null check in handler", severity="P2")
        record = self._make_record("Missing null check in handler", severity="P1")
        assert not _matches_prior(finding, record)

    def test_low_overlap_no_match(self):
        finding = _make_finding("Completely different description about something else")
        record = self._make_record("Missing null check in handler method call")
        assert not _matches_prior(finding, record)


class TestUpdateFindingRegistryCycle1:
    """Cycle 1: all findings should be net_new."""

    def test_single_reviewer_p1_is_net_new(self, tmp_path):
        state = _make_state()
        finding = _make_finding("Missing null check in handler")
        review = _make_review([finding])
        cycle_results = [("reviewer-a", review)]

        with patch("theforge.finding_classifier._get_changed_files", return_value=frozenset()):
            classified = update_finding_registry(state, cycle_results, tmp_path, cycle_num=1)

        assert len(classified) == 1
        assert classified[0].disposition == "net_new"
        assert classified[0].severity == "P1"
        assert classified[0].cycle_first_seen == 1
        assert len(state.finding_registry) == 1

    def test_p2_finding_classified_net_new(self, tmp_path):
        state = _make_state()
        finding = _make_finding("Style issue", severity="P2")
        review = _make_review([finding], verdict="APPROVE")
        cycle_results = [("reviewer-a", review)]

        with patch("theforge.finding_classifier._get_changed_files", return_value=frozenset()):
            classified = update_finding_registry(state, cycle_results, tmp_path, cycle_num=1)

        assert any(r.disposition == "net_new" for r in classified)

    def test_multiple_reviewers_same_finding_corroborated(self, tmp_path):
        state = _make_state()
        finding_a = _make_finding("Missing null check in handler")
        finding_b = _make_finding("Missing null check in handler")
        review_a = _make_review([finding_a])
        review_b = _make_review([finding_b])
        cycle_results = [("reviewer-a", review_a), ("reviewer-b", review_b)]

        with patch("theforge.finding_classifier._get_changed_files", return_value=frozenset()):
            classified = update_finding_registry(state, cycle_results, tmp_path, cycle_num=1)

        # 2+ reviewers same finding → corroborated_new
        p1s = [r for r in classified if r.severity == "P1"]
        assert len(p1s) == 1
        assert p1s[0].disposition == "corroborated_new"

    def test_multiple_reviewers_nearby_lines_different_wording_corroborated(self, tmp_path):
        state = _make_state()
        finding_a = _make_finding("Missing null check before dereferencing request.user", line=10)
        finding_b = _make_finding("Handler can crash when account lookup returns None", line=12)
        cycle_results = [
            ("reviewer-a", _make_review([finding_a])),
            ("reviewer-b", _make_review([finding_b])),
        ]

        with patch("theforge.finding_classifier._get_changed_files", return_value=frozenset()):
            classified = update_finding_registry(state, cycle_results, tmp_path, cycle_num=1)

        p1s = [r for r in classified if r.severity == "P1"]
        assert len(p1s) == 1
        assert p1s[0].disposition == "corroborated_new"

    def test_multiple_reviewers_far_apart_lines_not_corroborated(self, tmp_path):
        state = _make_state()
        finding_a = _make_finding("Missing null check before dereferencing request.user", line=10)
        finding_b = _make_finding("Handler can crash when account lookup returns None", line=14)
        cycle_results = [
            ("reviewer-a", _make_review([finding_a])),
            ("reviewer-b", _make_review([finding_b])),
        ]

        with patch("theforge.finding_classifier._get_changed_files", return_value=frozenset()):
            classified = update_finding_registry(state, cycle_results, tmp_path, cycle_num=1)

        p1s = [r for r in classified if r.severity == "P1"]
        assert len(p1s) == 2
        assert all(r.disposition == "net_new" for r in p1s)


class TestUpdateFindingRegistryCycle2:
    """Cycle 2+: matching against prior registry."""

    def _populate_cycle1(self, state, description, file="src/foo.py", disposition="net_new"):
        fp = _fingerprint("P1", file, description)
        record = FindingRecord(
            finding_id=fp,
            cycle_first_seen=1,
            cycle_last_seen=1,
            file=file,
            line=10,
            severity="P1",
            description=description,
            reporter="reviewer-a",
            disposition=disposition,
        )
        state.finding_registry.append(record)
        return record

    def test_unresolved_prior_finding_becomes_unresolved(self, tmp_path):
        state = _make_state()
        self._populate_cycle1(state, "Missing null check in handler")

        finding = _make_finding("Missing null check in handler")
        review = _make_review([finding])
        cycle_results = [("reviewer-a", review)]

        with patch("theforge.finding_classifier._get_changed_files", return_value=frozenset()):
            classified = update_finding_registry(state, cycle_results, tmp_path, cycle_num=2)

        assert classified[0].disposition == "unresolved"
        assert classified[0].cycle_last_seen == 2

    def test_fixed_finding_not_in_cycle2_marked_fixed(self, tmp_path):
        state = _make_state()
        record = self._populate_cycle1(state, "Missing null check in handler")
        assert record.disposition == "net_new"

        # Cycle 2: finding does NOT appear → should be marked fixed
        review = _make_review([])  # no findings
        cycle_results = [("reviewer-a", review)]

        with patch("theforge.finding_classifier._get_changed_files", return_value=frozenset()):
            update_finding_registry(state, cycle_results, tmp_path, cycle_num=2)

        assert record.disposition == "fixed"

    def test_new_finding_in_changed_file_is_regression(self, tmp_path):
        state = _make_state()
        # No prior registry entries for this finding

        finding = _make_finding("Index out of bounds in new loop", file="src/changed.py")
        review = _make_review([finding])
        cycle_results = [("reviewer-a", review)]

        with patch(
            "theforge.finding_classifier._get_changed_files",
            return_value=frozenset(["src/changed.py"]),
        ):
            classified = update_finding_registry(state, cycle_results, tmp_path, cycle_num=2)

        p1s = [r for r in classified if r.severity == "P1"]
        assert p1s[0].disposition == "regression"

    def test_new_finding_not_in_changed_file_single_reviewer_is_net_new(self, tmp_path):
        state = _make_state()

        finding = _make_finding("Latent issue in untouched file", file="src/untouched.py")
        review = _make_review([finding])
        cycle_results = [("reviewer-a", review)]

        with patch(
            "theforge.finding_classifier._get_changed_files",
            return_value=frozenset(["src/other.py"]),
        ):
            classified = update_finding_registry(state, cycle_results, tmp_path, cycle_num=2)

        p1s = [r for r in classified if r.severity == "P1"]
        assert p1s[0].disposition == "net_new"

    def test_new_finding_2_reviewers_not_in_changed_files_is_corroborated(self, tmp_path):
        state = _make_state()

        finding_a = _make_finding("Security flaw in untouched code", file="src/auth.py")
        finding_b = _make_finding("Security flaw in untouched code", file="src/auth.py")
        cycle_results = [
            ("reviewer-a", _make_review([finding_a])),
            ("reviewer-b", _make_review([finding_b])),
        ]

        with patch(
            "theforge.finding_classifier._get_changed_files",
            return_value=frozenset(["src/other.py"]),
        ):
            classified = update_finding_registry(state, cycle_results, tmp_path, cycle_num=2)

        p1s = [r for r in classified if r.severity == "P1"]
        assert len(p1s) == 1
        assert p1s[0].disposition == "corroborated_new"


class TestHasBlockingP1:
    def _make_record(self, disposition: str, severity: str = "P1") -> FindingRecord:
        return FindingRecord(
            finding_id="abc123",
            cycle_first_seen=1,
            cycle_last_seen=1,
            file="src/foo.py",
            line=10,
            severity=severity,
            description="some finding",
            reporter="reviewer-a",
            disposition=disposition,  # type: ignore[arg-type]
        )

    def test_unresolved_blocks(self):
        assert has_blocking_p1([self._make_record("unresolved")])

    def test_regression_blocks(self):
        assert has_blocking_p1([self._make_record("regression")])

    def test_corroborated_new_blocks(self):
        assert has_blocking_p1([self._make_record("corroborated_new")])

    def test_net_new_does_not_block(self):
        assert not has_blocking_p1([self._make_record("net_new")])

    def test_fixed_does_not_block(self):
        assert not has_blocking_p1([self._make_record("fixed")])

    def test_p2_unresolved_does_not_block(self):
        assert not has_blocking_p1([self._make_record("unresolved", severity="P2")])

    def test_empty_list_does_not_block(self):
        assert not has_blocking_p1([])

    def test_mixed_blocking_and_net_new(self):
        records = [
            self._make_record("net_new"),
            self._make_record("regression"),
        ]
        assert has_blocking_p1(records)


class TestNetNewP1s:
    def _make_record(self, disposition: str, severity: str = "P1") -> FindingRecord:
        return FindingRecord(
            finding_id="abc123",
            cycle_first_seen=1,
            cycle_last_seen=1,
            file="src/foo.py",
            line=None,
            severity=severity,
            description="test",
            reporter="reviewer-a",
            disposition=disposition,  # type: ignore[arg-type]
        )

    def test_returns_only_net_new_p1s(self):
        records = [
            self._make_record("net_new"),
            self._make_record("unresolved"),
            self._make_record("regression"),
            self._make_record("net_new", severity="P2"),  # P2 excluded
        ]
        result = net_new_p1s(records)
        assert len(result) == 1
        assert result[0].disposition == "net_new"
        assert result[0].severity == "P1"


class TestGetChangedFiles:
    def test_returns_empty_on_no_prev_commit(self, tmp_path):
        from theforge.finding_classifier import _get_changed_files

        result = _get_changed_files(tmp_path, prev_commit=None)
        assert result == frozenset()

    def test_parses_git_output(self, tmp_path):
        from theforge.finding_classifier import _get_changed_files

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = b"src/foo.py\nsrc/bar.py\n"
            result = _get_changed_files(tmp_path, prev_commit="abc123")

        assert result == frozenset(["src/foo.py", "src/bar.py"])

    def test_returns_empty_on_git_failure(self, tmp_path):
        from theforge.finding_classifier import _get_changed_files

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = b""
            result = _get_changed_files(tmp_path, prev_commit="abc123")

        assert result == frozenset()
