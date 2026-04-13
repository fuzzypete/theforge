"""Tests for finding_classifier.py — fingerprinting, classification, exit criteria."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from theforge.coordinator.state import CoordinatorState, FindingRecord
from theforge.finding_classifier import (
    _fingerprint,
    _jaccard,
    _matches_prior,
    _matches_prior_agnostic,
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


class TestMatchesPriorAgnostic:
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

    def test_same_severity_matches(self):
        finding = _make_finding("Missing null check in handler")
        record = self._make_record("Missing null check in handler")
        assert _matches_prior_agnostic(finding, record)

    def test_different_severity_still_matches(self):
        finding = _make_finding("Missing null check in handler", severity="P2")
        record = self._make_record("Missing null check in handler", severity="P1")
        assert _matches_prior_agnostic(finding, record)

    def test_different_file_no_match(self):
        finding = _make_finding("Missing null check in handler", file="src/bar.py")
        record = self._make_record("Missing null check in handler", file="src/foo.py")
        assert not _matches_prior_agnostic(finding, record)

    def test_low_overlap_no_match(self):
        finding = _make_finding("Completely different description about something else")
        record = self._make_record("Missing null check in handler method call")
        assert not _matches_prior_agnostic(finding, record)


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

    def test_multiple_reviewers_nearby_lines_different_severities_do_not_merge(self, tmp_path):
        state = _make_state()
        finding_a = _make_finding(
            "Missing null check before dereferencing request.user",
            severity="P1",
            line=10,
        )
        finding_b = _make_finding(
            "Formatting issue in nearby branch",
            severity="P2",
            line=12,
        )
        cycle_results = [
            ("reviewer-a", _make_review([finding_a])),
            ("reviewer-b", _make_review([finding_b], verdict="APPROVE")),
        ]

        with patch("theforge.finding_classifier._get_changed_files", return_value=frozenset()):
            classified = update_finding_registry(state, cycle_results, tmp_path, cycle_num=1)

        assert len(classified) == 2
        by_severity = {record.severity: record for record in classified}
        assert by_severity["P1"].disposition == "net_new"
        assert by_severity["P2"].disposition == "net_new"

    def test_same_reviewer_distant_duplicate_does_not_block_nearby_corroboration(self, tmp_path):
        """Reviewer A reports the same description at lines 10 AND 100 (same fingerprint bucket).
        Reviewer B reports different wording at line 11.
        The pair (line 10, line 11) satisfies ±3, so the buckets should merge → corroborated_new.
        The distant report at line 100 must not prevent corroboration.
        """
        state = _make_state()
        finding_a_near = _make_finding("Missing null check before dereference", line=10)
        finding_a_far = _make_finding("Missing null check before dereference", line=100)
        finding_b = _make_finding("Potential None dereference in handler", line=11)
        cycle_results = [
            ("reviewer-a", _make_review([finding_a_near, finding_a_far])),
            ("reviewer-b", _make_review([finding_b])),
        ]

        with patch("theforge.finding_classifier._get_changed_files", return_value=frozenset()):
            classified = update_finding_registry(state, cycle_results, tmp_path, cycle_num=1)

        p1s = [r for r in classified if r.severity == "P1"]
        # reviewer-a and reviewer-b should corroborate via the near pair (lines 10 & 11)
        corroborated = [r for r in p1s if r.disposition == "corroborated_new"]
        assert len(corroborated) >= 1, (
            f"Expected at least one corroborated_new P1; got dispositions: "
            f"{[r.disposition for r in p1s]}"
        )

    def test_nearby_line_corroboration_does_not_chain_transitively(self, tmp_path):
        state = _make_state()
        cycle_results = [
            ("reviewer-a", _make_review([_make_finding("Null access in branch A", line=10)])),
            (
                "reviewer-b",
                _make_review([_make_finding("Unchecked response in branch B", line=13)]),
            ),
            ("reviewer-c", _make_review([_make_finding("Missing guard in branch C", line=16)])),
        ]

        with patch("theforge.finding_classifier._get_changed_files", return_value=frozenset()):
            classified = update_finding_registry(state, cycle_results, tmp_path, cycle_num=1)

        p1s = [r for r in classified if r.severity == "P1"]
        assert len(p1s) == 2
        corroborated = [record for record in p1s if record.disposition == "corroborated_new"]
        net_new = [record for record in p1s if record.disposition == "net_new"]
        assert len(corroborated) == 1
        assert len(net_new) == 1


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

    def test_p1_downgraded_to_p2_gets_downgraded_disposition(self, tmp_path):
        """A P1 in cycle 1 reported as P2 in cycle 2 should get 'downgraded', not 'fixed'."""
        state = _make_state()
        self._populate_cycle1(state, "Missing null check in handler")

        # Same description, but P2 this time
        finding = _make_finding("Missing null check in handler", severity="P2")
        review = _make_review([finding], verdict="APPROVE")
        cycle_results = [("reviewer-a", review)]

        with patch("theforge.finding_classifier._get_changed_files", return_value=frozenset()):
            update_finding_registry(state, cycle_results, tmp_path, cycle_num=2)

        # The existing P1 record should be updated to 'downgraded'
        assert len(state.finding_registry) == 1
        record = state.finding_registry[0]
        assert record.disposition == "downgraded"
        assert record.cycle_last_seen == 2
        # Severity stays P1 (it's the same record — the downgrade is noted in disposition)
        assert record.severity == "P1"

    def test_p1_downgraded_to_p2_is_not_marked_fixed(self, tmp_path):
        """A downgraded finding's cycle_last_seen is updated, so it must not be marked fixed."""
        state = _make_state()
        self._populate_cycle1(state, "Missing null check in handler")

        finding = _make_finding("Missing null check in handler", severity="P2")
        review = _make_review([finding], verdict="APPROVE")
        cycle_results = [("reviewer-a", review)]

        with patch("theforge.finding_classifier._get_changed_files", return_value=frozenset()):
            update_finding_registry(state, cycle_results, tmp_path, cycle_num=2)

        record = state.finding_registry[0]
        assert record.disposition != "fixed"

    def test_downgraded_p1_does_not_block(self, tmp_path):
        """A downgraded P1 should not block — it was intentionally softened by reviewers."""
        state = _make_state()
        self._populate_cycle1(state, "Missing null check in handler")

        finding = _make_finding("Missing null check in handler", severity="P2")
        review = _make_review([finding], verdict="APPROVE")
        cycle_results = [("reviewer-a", review)]

        with patch("theforge.finding_classifier._get_changed_files", return_value=frozenset()):
            classified = update_finding_registry(state, cycle_results, tmp_path, cycle_num=2)

        assert not has_blocking_p1(classified)

    def test_p2_upgraded_to_p1_creates_new_record(self, tmp_path):
        """P2 in cycle 1 reported as P1 in cycle 2 → new finding (regression or net_new)."""
        state = _make_state()
        fp = _fingerprint("P2", "src/foo.py", "Style issue in handler")
        record = FindingRecord(
            finding_id=fp,
            cycle_first_seen=1,
            cycle_last_seen=1,
            file="src/foo.py",
            line=10,
            severity="P2",
            description="Style issue in handler",
            reporter="reviewer-a",
            disposition="net_new",
        )
        state.finding_registry.append(record)

        # Same description but P1 now — upgrade case
        finding = _make_finding("Style issue in handler", severity="P1", file="src/foo.py")
        review = _make_review([finding])
        cycle_results = [("reviewer-a", review)]

        with patch("theforge.finding_classifier._get_changed_files", return_value=frozenset()):
            update_finding_registry(state, cycle_results, tmp_path, cycle_num=2)

        # P2→P1 is NOT treated as downgrade; new P1 record created
        p1_records = [r for r in state.finding_registry if r.severity == "P1"]
        assert len(p1_records) == 1
        assert p1_records[0].disposition in ("net_new", "regression")


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
