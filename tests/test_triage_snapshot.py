"""Tests for canonical triage finding snapshots."""

from __future__ import annotations

from theforge.triage_snapshot import canonicalize_finding_snapshot


def test_canonicalize_finding_snapshot_normalizes_cross_producer_differences() -> None:
    proposal_time = {
        "issue_ref": "#1312",
        "issue_number": "1312",
        "title": "  audit count is off by one  ",
        "body": "  audit count is off by one  ",
        "labels": ["forge-finding", "bug"],
        "pool_state": " Hygiene ",
        "verification_status": " stale_evidence ",
        "evidence": [
            {
                "evidence_id": "path-churn",
                "kind": "churn",
                "summary": " cited file changed 3 time(s) since filing ",
                "detail": "",
                "checkable": True,
            },
            {
                "id": "symbol-absent",
                "kind": "staleness",
                "summary": " cited symbol absent from current tree ",
                "checkable": True,
                "detail": " rg audit_count returns no match ",
            },
        ],
    }
    live = {
        "issue_ref": "#1312",
        "issue_number": 1312,
        "title": "audit count is off by one",
        "body": "audit count is off by one",
        "labels": ("bug", "forge-finding"),
        "pool_state": "Hygiene",
        "verification_status": "stale_evidence",
        "evidence": [
            {
                "id": "symbol-absent",
                "kind": "staleness",
                "summary": "cited symbol absent from current tree",
                "checkable": True,
                "detail": "rg audit_count returns no match",
                "artifact": "ignored-by-stale-check",
                "observed_status": "stale_evidence",
            },
            {
                "id": "path-churn",
                "kind": "churn",
                "summary": "cited file changed 3 time(s) since filing",
                "checkable": True,
                "detail": "",
                "observed_status": "stale_evidence",
            },
        ],
    }

    assert canonicalize_finding_snapshot(proposal_time) == canonicalize_finding_snapshot(live)


def test_canonicalize_finding_snapshot_emits_stable_empty_defaults() -> None:
    assert canonicalize_finding_snapshot({}) == {
        "issue_ref": "",
        "issue_number": None,
        "title": "",
        "body": "",
        "labels": [],
        "pool_state": "",
        "verification_status": "",
        "evidence": [],
    }
