"""Unit tests for structured landing-record derivation (issue #1424).

The sprint summary historically recorded each story's merge outcome as a single
boolean that read ``True`` both when a fresh PR shipped code and when the
already-merged guard short-circuited and discarded the worktree's commits.
``build_landing_record`` turns the raw ``land_story`` merge_info into a
structured record an operator can triage without checking GitHub.
"""

from __future__ import annotations

from theforge.coordinator.landing_record import build_landing_record


def test_fresh_merge_marks_fresh_pr_created() -> None:
    merge = {
        "action": "merge-pr",
        "pr_url": "https://github.com/x/y/pull/1414",
        "merged": True,
        "merge_queued": False,
        "success": True,
        "landing_path": "fresh-merge",
        "guard_evidence": {
            "branch": "feat/issue-1414",
            "pr_url": "https://github.com/x/y/pull/1414",
        },
    }
    record = build_landing_record(merge)
    assert record is not None
    assert record["outcome"] == "merged"
    assert record["fresh_pr_created"] is True
    assert record["merged"] is True
    assert record["pr_url"] == "https://github.com/x/y/pull/1414"


def test_already_merged_short_circuit_is_distinguishable() -> None:
    merge = {
        "action": "merge-pr",
        "pr_url": "https://github.com/x/y/pull/1143",
        "merged": True,
        "success": True,
        "landing_path": "already-merged",
        "guard_evidence": {
            "branch": "feat/issue-1135",
            "pr_number": 1143,
            "pr_url": "https://github.com/x/y/pull/1143",
            "merged_at": "2026-04-30T14:21:12Z",
        },
    }
    record = build_landing_record(merge)
    assert record is not None
    assert record["outcome"] == "already-merged-short-circuit"
    # The defining signal: no fresh PR was created even though merged is True.
    assert record["fresh_pr_created"] is False
    assert record["pr_url"] == "https://github.com/x/y/pull/1143"
    assert record["pr_merged_at"] == "2026-04-30T14:21:12Z"


def test_fresh_merge_and_already_merged_disagree_on_fresh_pr_created() -> None:
    """A real merge and a guard short-circuit both have merged=True but differ."""
    fresh = build_landing_record({"merged": True, "landing_path": "fresh-merge", "pr_url": "u"})
    guard = build_landing_record({"merged": True, "landing_path": "already-merged", "pr_url": "u"})
    assert fresh is not None and guard is not None
    assert fresh["merged"] == guard["merged"] is True
    assert fresh["fresh_pr_created"] != guard["fresh_pr_created"]


def test_queued_auto_merge_is_not_reported_as_shipped() -> None:
    merge = {
        "merged": False,
        "merge_queued": True,
        "landing_path": "fresh-merge",
        "pr_url": "https://github.com/x/y/pull/9",
        "guard_evidence": {"branch": "feat/x", "merge_queued": True},
    }
    record = build_landing_record(merge)
    assert record is not None
    assert record["outcome"] == "merge-queued"
    assert record["fresh_pr_created"] is True
    assert record["merged"] is False
    assert record["merge_queued"] is True


def test_zero_delta_short_circuit() -> None:
    merge = {
        "merged": False,
        "skipped": True,
        "landing_path": "zero-delta",
        "pr_url": None,
        "guard_evidence": {"branch": "feat/x", "base_branch": "main", "ahead_commit_count": 0},
    }
    record = build_landing_record(merge)
    assert record is not None
    assert record["outcome"] == "zero-delta-short-circuit"
    assert record["fresh_pr_created"] is False
    assert record["merged"] is False


def test_missing_review_short_circuit() -> None:
    record = build_landing_record(
        {"merged": False, "landing_path": "missing-review", "guard_evidence": {"branch": "feat/x"}}
    )
    assert record is not None
    assert record["outcome"] == "missing-review"
    assert record["fresh_pr_created"] is False


def test_none_and_non_landing_dicts_return_none() -> None:
    assert build_landing_record(None) is None
    assert build_landing_record("not a dict") is None
    # pending placeholder set by _finalize_approve before land_story runs
    assert build_landing_record({"action": "merge", "pending": True}) is None
    # on_approve == "none"
    assert build_landing_record({"action": "none", "success": True, "error": None}) is None
    # early merge failure returned before a landing_path was recorded
    assert build_landing_record({"action": "merge-pr", "merged": False, "success": False}) is None


def test_unknown_landing_path_falls_through_to_raw() -> None:
    record = build_landing_record({"merged": True, "landing_path": "some-future-path"})
    assert record is not None
    assert record["outcome"] == "some-future-path"
    assert record["fresh_pr_created"] is False
