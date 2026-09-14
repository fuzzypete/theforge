"""The canonical per-run record is rewritten, not overwritten (#2519).

``_write_story_audit`` calls ``_write_native_story_record`` more than once for
the same finished story — before and after the post-DONE knowledge summary, and
again across pending-integration, landing and wrap-up. Each of those is a later
*statement* about one run, not a later run. A statement that happens to know
less is not authoritative over the record already on disk merely by arriving
second.
"""

from __future__ import annotations

import json
from pathlib import Path

from theforge.sprint.audit import _write_native_story_record


def _record(project_root: Path, run_id: str = "run-1") -> dict:
    path = project_root / ".forge" / "audits" / "runs" / f"{run_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _full_audit() -> dict:
    return {
        "run_id": "run-1",
        "task": {"slug": "issue-42", "name": "Demo"},
        "outcome": {"success": True, "final_phase": "DONE", "cost_usd": 4.25},
        "reviews": [{"cycle": 1, "verdict": "APPROVE"}],
        "changed_files": {"files": [{"path": "src/client.py"}]},
        "finding_registry": [{"finding_id": "f-001"}],
        "last_model": "claude-opus-5",
        "sprint_name": "Parallel Sprint",
    }


def test_a_thinner_later_write_does_not_blank_the_existing_record(tmp_path: Path) -> None:
    _write_native_story_record(tmp_path, _full_audit())

    thinner = {
        "run_id": "run-1",
        "task": {"slug": "issue-42", "name": "Demo"},
        # Everything below says less than what is already recorded.
        "reviews": [],
        "changed_files": {},
        "finding_registry": None,
        "last_model": "",
        "outcome": {"success": True, "final_phase": "DONE", "cost_usd": 4.25},
    }
    _write_native_story_record(tmp_path, thinner, force_replace=True)

    record = _record(tmp_path)
    assert record["reviews"] == [{"cycle": 1, "verdict": "APPROVE"}]
    assert record["changed_files"] == {"files": [{"path": "src/client.py"}]}
    assert record["finding_registry"] == [{"finding_id": "f-001"}]
    assert record["last_model"] == "claude-opus-5"
    assert record["sprint_name"] == "Parallel Sprint"


def test_a_later_write_still_folds_in_what_it_does_know(tmp_path: Path) -> None:
    """The knowledge summary is folded in by exactly this second write."""
    _write_native_story_record(tmp_path, _full_audit())

    later = dict(_full_audit())
    later["reviews"] = []
    later["knowledge_summary"] = {"status": "written", "attempted": True, "written": True}
    _write_native_story_record(tmp_path, later, force_replace=True)

    record = _record(tmp_path)
    assert record["knowledge_summary"] == {
        "status": "written",
        "attempted": True,
        "written": True,
    }
    # ...without losing the reviews the emptied incoming payload dropped.
    assert record["reviews"] == [{"cycle": 1, "verdict": "APPROVE"}]


def test_a_concrete_later_value_still_wins(tmp_path: Path) -> None:
    """Preserving non-empty existing values is not freezing the record."""
    _write_native_story_record(tmp_path, _full_audit())

    later = dict(_full_audit())
    later["last_model"] = "claude-sonnet-5"
    _write_native_story_record(tmp_path, later, force_replace=True)

    assert _record(tmp_path)["last_model"] == "claude-sonnet-5"


def test_a_partial_nested_block_updates_its_keys_without_dropping_the_rest(
    tmp_path: Path,
) -> None:
    """A later payload naming some of `outcome`'s keys must not shed the others."""
    _write_native_story_record(tmp_path, _full_audit())

    later = {
        "run_id": "run-1",
        # Non-empty, but only part of what the stored block carries.
        "outcome": {"final_phase": "DONE", "landing_note": "queued"},
        "changed_files": {"head_ref": "bbb222"},
    }
    _write_native_story_record(tmp_path, later, force_replace=True)

    record = _record(tmp_path)
    assert record["outcome"] == {
        "success": True,
        "final_phase": "DONE",
        "cost_usd": 4.25,
        "landing_note": "queued",
    }
    assert record["changed_files"] == {
        "files": [{"path": "src/client.py"}],
        "head_ref": "bbb222",
    }


def test_nested_merging_goes_all_the_way_down(tmp_path: Path) -> None:
    audit = dict(_full_audit())
    audit["phases"] = {"dev": {"cost_usd": 3.0, "duration_s": 90.0, "outcome": "success"}}
    _write_native_story_record(tmp_path, audit)

    later = dict(_full_audit())
    later["phases"] = {"dev": {"cost_usd": 3.5}}
    _write_native_story_record(tmp_path, later, force_replace=True)

    assert _record(tmp_path)["phases"]["dev"] == {
        "cost_usd": 3.5,
        "duration_s": 90.0,
        "outcome": "success",
    }


def test_a_shorter_non_empty_list_does_not_truncate_the_stored_one(tmp_path: Path) -> None:
    """Within one run these collections only grow, so a shorter later list is thinner."""
    audit = dict(_full_audit())
    audit["reviews"] = [
        {"cycle": 1, "verdict": "REQUEST_CHANGES"},
        {"cycle": 2, "verdict": "APPROVE"},
    ]
    _write_native_story_record(tmp_path, audit)

    later = dict(_full_audit())
    later["reviews"] = [{"cycle": 1, "verdict": "REQUEST_CHANGES"}]
    _write_native_story_record(tmp_path, later, force_replace=True)

    assert _record(tmp_path)["reviews"] == [
        {"cycle": 1, "verdict": "REQUEST_CHANGES"},
        {"cycle": 2, "verdict": "APPROVE"},
    ]


def test_a_longer_list_is_authoritative(tmp_path: Path) -> None:
    _write_native_story_record(tmp_path, _full_audit())

    later = dict(_full_audit())
    later["reviews"] = [
        {"cycle": 1, "verdict": "APPROVE"},
        {"cycle": 2, "verdict": "APPROVE"},
    ]
    _write_native_story_record(tmp_path, later, force_replace=True)

    assert len(_record(tmp_path)["reviews"]) == 2


def test_the_landing_fields_remain_the_one_clearable_exception(tmp_path: Path) -> None:
    """`_MERGE_CLEARABLE_FIELDS` is where a deliberate reset is allowed to live."""
    from theforge.sprint.audit import _LANDING_CLAIM_FIELDS, _MERGE_CLEARABLE_FIELDS

    assert _MERGE_CLEARABLE_FIELDS == _LANDING_CLAIM_FIELDS

    audit = dict(_full_audit())
    audit["landing_status"] = "landed"
    audit["landing"] = {"outcome": "merged"}
    _write_native_story_record(tmp_path, audit)

    later = dict(_full_audit())
    later["landing_status"] = "landed"
    later["landing"] = None
    _write_native_story_record(tmp_path, later, force_replace=True)

    # Cleared, not preserved — the exemption is doing its job.
    assert _record(tmp_path)["landing"] is None


def test_a_later_rejected_summary_does_not_keep_the_written_one_s_artifact_metadata(
    tmp_path: Path,
) -> None:
    """`knowledge_summary` is one outcome, not a pile of keys from several."""
    audit = dict(_full_audit())
    audit["knowledge_summary"] = {
        "status": "written",
        "attempted": True,
        "written": True,
        "path": ".forge/knowledge/summaries/run-1.yaml",
        "index_rebuild": {"status": "rebuilt"},
    }
    _write_native_story_record(tmp_path, audit)

    later = dict(_full_audit())
    later["knowledge_summary"] = {
        "status": "rejected",
        "attempted": True,
        "written": False,
        "reason": "evidence did not resolve",
    }
    _write_native_story_record(tmp_path, later, force_replace=True)

    assert _record(tmp_path)["knowledge_summary"] == {
        "status": "rejected",
        "attempted": True,
        "written": False,
        "reason": "evidence did not resolve",
    }


def test_an_absent_summary_block_still_leaves_the_recorded_one_standing(
    tmp_path: Path,
) -> None:
    """Atomic does not mean clearable — saying nothing is not saying otherwise."""
    audit = dict(_full_audit())
    audit["knowledge_summary"] = {"status": "written", "attempted": True, "written": True}
    _write_native_story_record(tmp_path, audit)

    _write_native_story_record(tmp_path, _full_audit(), force_replace=True)

    assert _record(tmp_path)["knowledge_summary"]["written"] is True


def test_an_equal_length_list_with_thinner_elements_keeps_the_element_fields(
    tmp_path: Path,
) -> None:
    """The loss just happens one level further down than a missing key."""
    audit = dict(_full_audit())
    audit["reviews"] = [
        {"cycle": 1, "verdict": "REQUEST_CHANGES", "summary": "missing read timeout"},
        {"cycle": 2, "verdict": "APPROVE", "summary": "resolved"},
    ]
    _write_native_story_record(tmp_path, audit)

    later = dict(_full_audit())
    later["reviews"] = [
        {"cycle": 1, "verdict": "REQUEST_CHANGES"},
        {"cycle": 2, "verdict": "APPROVE"},
    ]
    _write_native_story_record(tmp_path, later, force_replace=True)

    assert _record(tmp_path)["reviews"] == [
        {"cycle": 1, "verdict": "REQUEST_CHANGES", "summary": "missing read timeout"},
        {"cycle": 2, "verdict": "APPROVE", "summary": "resolved"},
    ]


def test_list_elements_are_paired_by_their_own_identity_not_by_position(
    tmp_path: Path,
) -> None:
    audit = dict(_full_audit())
    audit["finding_registry"] = [
        {"finding_id": "f-001", "severity": "P1", "file": "src/client.py"},
        {"finding_id": "f-002", "severity": "P2", "file": "src/retry.py"},
    ]
    _write_native_story_record(tmp_path, audit)

    later = dict(_full_audit())
    # Reordered and thinner; identity, not position, decides what merges.
    later["finding_registry"] = [
        {"finding_id": "f-002", "disposition": "resolved"},
        {"finding_id": "f-001", "disposition": "resolved"},
    ]
    _write_native_story_record(tmp_path, later, force_replace=True)

    by_id = {entry["finding_id"]: entry for entry in _record(tmp_path)["finding_registry"]}
    assert by_id["f-001"] == {
        "finding_id": "f-001",
        "severity": "P1",
        "file": "src/client.py",
        "disposition": "resolved",
    }
    assert by_id["f-002"]["severity"] == "P2"


def test_an_element_only_the_stored_list_has_survives(tmp_path: Path) -> None:
    audit = dict(_full_audit())
    audit["reviews"] = [
        {"cycle": 1, "verdict": "REQUEST_CHANGES"},
        {"cycle": 2, "verdict": "APPROVE"},
    ]
    _write_native_story_record(tmp_path, audit)

    later = dict(_full_audit())
    later["reviews"] = [{"cycle": 1, "verdict": "REQUEST_CHANGES"}]
    _write_native_story_record(tmp_path, later, force_replace=True)

    assert [entry["cycle"] for entry in _record(tmp_path)["reviews"]] == [1, 2]
