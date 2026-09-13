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
