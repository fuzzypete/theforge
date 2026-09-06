"""`forge explain` for a story that has not finished (#2923).

Seam-level: the records are produced by the real writers — the sprint's
in-flight audit flush, the ``forge stop`` finalizer, and the resume-record save
— and then read back through ``cmd_explain``. That is the boundary the bug was
on: the decision was written by one side and unreachable from the other, so a
test that hand-rolls the file it expects would not have caught it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tests.test_cli_explain import _routing_block
from tests.test_sprint_resume import _make_config
from theforge.cli import explain, explain_live
from theforge.coordinator import audit_substrate as sub
from theforge.coordinator import resume_persistence
from theforge.coordinator.state import CoordinatorState, Phase
from theforge.sprint.audit import finalize_interrupted_story_audit, write_live_story_audit
from theforge.task import TaskStory

SPRINT_NAME = "issues-2908"
SLUG = "issue-2908"
RUN_ID = "0210029b48a1"


class _Args:
    file = None
    story = None
    run = None
    config = None
    config_key = None

    def __init__(self, **kwargs: object) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


def _project(tmp_path: Path) -> Path:
    (tmp_path / "forge.yaml").write_text("project: test\n", encoding="utf-8")
    return tmp_path


def _live_state(tmp_path: Path, *, routing: dict | None) -> CoordinatorState:
    state = CoordinatorState(run_id=RUN_ID)
    state.phase = Phase.DEV
    state.started_at = "2026-09-05T00:00:00+00:00"
    state.sprint_name = SPRINT_NAME
    state.workspace_path = tmp_path / SLUG
    state.log_dir = tmp_path / ".forge" / "logs" / SPRINT_NAME / SLUG
    state.routing_decision = routing
    return state


#: Sentinel so `routing=None` means "the router had not decided yet".
_DEFAULT_ROUTING = object()


def _flush_live_audit(tmp_path: Path, *, routing: object = _DEFAULT_ROUTING) -> Path:
    """Write the story's in-flight audit exactly as a running sprint does."""
    config = _make_config(tmp_path)
    task = TaskStory(name="Issue #2908", slug=SLUG, story_text="do the thing", github_issue=2908)
    resolved = _routing_block() if routing is _DEFAULT_ROUTING else routing
    state = _live_state(tmp_path, routing=resolved)
    path = write_live_story_audit(config, task, state, sprint_name=SPRINT_NAME)
    assert path is not None and path.exists()
    return path


def _save_resume_record(tmp_path: Path, *, routing: object = _DEFAULT_ROUTING) -> Path:
    resolved = _routing_block() if routing is _DEFAULT_ROUTING else routing
    state = _live_state(tmp_path, routing=resolved)
    path = resume_persistence.save_resume_record(
        tmp_path, state, slug=SLUG, story_content="do the thing", run_id=RUN_ID
    )
    assert path is not None and path.exists()
    return path


# ── The record a running or stopped story already wrote is reachable ──────


def test_running_story_is_explained_from_its_in_flight_audit(tmp_path: Path, capsys) -> None:
    _project(tmp_path)
    audit_path = _flush_live_audit(tmp_path)

    assert explain.cmd_explain(_Args(story=SLUG, config=str(tmp_path / "forge.yaml"))) == 0
    captured = capsys.readouterr()
    assert "Routing decision (origin: preflight)" in captured.out
    assert "DEV" in captured.out
    # The operator is told which store answered and that it may still change.
    assert "in-flight story audit" in captured.err
    assert str(audit_path) in captured.err


def test_stopped_story_is_explained_after_stop_finalizes_its_audit(tmp_path: Path, capsys) -> None:
    """The `forge stop` path: same record, stamped terminal by another process."""
    _project(tmp_path)
    _flush_live_audit(tmp_path)
    assert finalize_interrupted_story_audit(tmp_path, SPRINT_NAME, SLUG) is not None

    assert explain.cmd_explain(_Args(story=SLUG, config=str(tmp_path / "forge.yaml"))) == 0
    captured = capsys.readouterr()
    assert "Routing decision (origin: preflight)" in captured.out
    assert "interrupted story audit" in captured.err


def test_run_id_lookup_reaches_the_same_unfinished_record(tmp_path: Path, capsys) -> None:
    _project(tmp_path)
    _flush_live_audit(tmp_path)

    assert explain.cmd_explain(_Args(run=RUN_ID, config=str(tmp_path / "forge.yaml"))) == 0
    assert "Routing decision (origin: preflight)" in capsys.readouterr().out


def test_issue_number_lookup_reaches_the_unfinished_record(tmp_path: Path, capsys) -> None:
    _project(tmp_path)
    _flush_live_audit(tmp_path)

    assert explain.cmd_explain(_Args(story="2908", config=str(tmp_path / "forge.yaml"))) == 0
    assert "Routing decision (origin: preflight)" in capsys.readouterr().out


def test_resume_record_answers_when_no_story_audit_exists(tmp_path: Path, capsys) -> None:
    """The second store: a story whose audit flush never happened."""
    _project(tmp_path)
    resume_path = _save_resume_record(tmp_path)

    assert explain.cmd_explain(_Args(story=SLUG, config=str(tmp_path / "forge.yaml"))) == 0
    captured = capsys.readouterr()
    assert "Routing decision (origin: preflight)" in captured.out
    assert "resume record" in captured.err
    assert str(resume_path) in captured.err
    # A resume record carries no configuration provenance; saying it "predates"
    # provenance would assert something about the run that is not true.
    assert "not carried by this store" in captured.out
    assert "predates configuration provenance" not in captured.out


def test_in_flight_audit_outranks_a_resume_record_without_routing(tmp_path: Path, capsys) -> None:
    """Freshness never beats actually holding the decision that was asked about."""
    _project(tmp_path)
    _flush_live_audit(tmp_path)
    # Saved after the audit, so it is the newer file — but it carries no routing.
    state = _live_state(tmp_path, routing=None)
    state.complexity_routing_audit = {"complexity_source": "preflight"}
    assert (
        resume_persistence.save_resume_record(
            tmp_path, state, slug=SLUG, story_content="x", run_id=RUN_ID
        )
        is not None
    )

    assert explain.cmd_explain(_Args(story=SLUG, config=str(tmp_path / "forge.yaml"))) == 0
    captured = capsys.readouterr()
    assert "Routing decision (origin: preflight)" in captured.out
    assert "in-flight story audit" in captured.err


# ── A finished record still wins, and nothing is written ──────────────────


def test_published_substrate_record_still_takes_precedence(tmp_path: Path, capsys) -> None:
    _project(tmp_path)
    runs = sub.runs_dir(tmp_path)
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{RUN_ID}.json").write_text(
        json.dumps(
            {
                "schema_version": sub.CURRENT_RECORD_SCHEMA_VERSION,
                "forge_version": "0.16.0",
                "run_id": RUN_ID,
                "task": {"slug": SLUG, "name": SLUG, "github_issue": 2908},
                "outcome": {"success": True, "final_phase": "DONE"},
                "routing_decision": _routing_block(),
            }
        ),
        encoding="utf-8",
    )
    sub.rebuild_from_runs(tmp_path)
    _flush_live_audit(tmp_path)

    assert explain.cmd_explain(_Args(story=SLUG, config=str(tmp_path / "forge.yaml"))) == 0
    captured = capsys.readouterr()
    assert "Routing decision (origin: preflight)" in captured.out
    assert "in-flight story audit" not in captured.err


def test_reading_an_unfinished_record_writes_nothing(tmp_path: Path) -> None:
    """`forge explain` stays read-only on the fallback path too."""
    _project(tmp_path)
    audit_path = _flush_live_audit(tmp_path)
    before = audit_path.stat().st_mtime_ns

    assert explain.cmd_explain(_Args(story=SLUG, config=str(tmp_path / "forge.yaml"))) == 0
    assert audit_path.stat().st_mtime_ns == before
    assert not sub.substrate_path(tmp_path).exists()
    assert not (tmp_path / ".forge" / "audits").exists()


# ── "Not recorded" and "could not be read" are different answers ──────────


def test_absent_record_names_the_stores_it_searched(tmp_path: Path, capsys) -> None:
    _project(tmp_path)
    runs = sub.runs_dir(tmp_path)
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "other.json").write_text(
        json.dumps(
            {
                "schema_version": sub.CURRENT_RECORD_SCHEMA_VERSION,
                "run_id": "other",
                "task": {"slug": "issue-1", "name": "issue-1"},
                "routing_decision": _routing_block(),
            }
        ),
        encoding="utf-8",
    )
    sub.rebuild_from_runs(tmp_path)

    assert explain.cmd_explain(_Args(story=SLUG, config=str(tmp_path / "forge.yaml"))) == 1
    err = capsys.readouterr().err
    assert "no audit record found for story issue-2908" in err
    assert "resume_state" in err
    assert "audit.yaml" in err
    assert "Nothing has recorded a routing decision" in err


def test_unreadable_record_is_not_reported_as_an_absent_one(tmp_path: Path, capsys) -> None:
    """The distinction the operator could not make during the incident."""
    _project(tmp_path)
    audit_path = _flush_live_audit(tmp_path)
    audit_path.write_text("{{ not: [valid\n", encoding="utf-8")

    assert explain.cmd_explain(_Args(story=SLUG, config=str(tmp_path / "forge.yaml"))) == 1
    err = capsys.readouterr().err
    assert "could not be read" in err
    assert "this is not the same as no record having been written" in err
    assert "Nothing has recorded a routing decision" not in err


def _corrupt_audit_for_another_story(tmp_path: Path) -> Path:
    """A malformed live audit belonging to a story nobody asked about."""
    other = tmp_path / ".forge" / "logs" / SPRINT_NAME / "issue-4242"
    other.mkdir(parents=True)
    path = other / "audit.yaml"
    path.write_text("{{ not: [valid\n", encoding="utf-8")
    return path


def test_run_lookup_does_not_claim_another_storys_corrupt_audit(tmp_path: Path, capsys) -> None:
    """A --run lookup has no path signal, so it may not attribute a corrupt file.

    Nothing in `.forge/logs/<sprint>/<slug>/audit.yaml` names a run, so an
    unparseable audit for another story says nothing about the run asked for.
    Reporting it as "this run's record could not be read" would answer a
    question the search never actually resolved.
    """
    _project(tmp_path)
    corrupt = _corrupt_audit_for_another_story(tmp_path)

    assert explain.cmd_explain(_Args(run="missing-run", config=str(tmp_path / "forge.yaml"))) == 1
    err = capsys.readouterr().err
    assert "a routing record for run missing-run exists on disk" not in err
    assert "this is not the same as no record having been written" not in err
    # The skipped file is still disclosed, and the closing claim is weakened to
    # match what the search can support.
    assert f"skipped {corrupt}" in err
    assert "may exist among them" in err
    assert "Nothing has recorded a routing decision" not in err


def test_issue_lookup_does_not_claim_another_storys_corrupt_audit(tmp_path: Path, capsys) -> None:
    """Same guard for a bare issue number: only the conventional slug dir counts."""
    _project(tmp_path)
    _corrupt_audit_for_another_story(tmp_path)

    assert explain.cmd_explain(_Args(story="2908", config=str(tmp_path / "forge.yaml"))) == 1
    err = capsys.readouterr().err
    assert "exists on disk but could not be read" not in err
    assert "skipped" in err


def test_issue_lookup_does_claim_its_own_corrupt_audit(tmp_path: Path, capsys) -> None:
    """The path names the story, so the record's existence is a supported claim."""
    _project(tmp_path)
    audit_path = _flush_live_audit(tmp_path)
    audit_path.write_text("{{ not: [valid\n", encoding="utf-8")

    assert explain.cmd_explain(_Args(story="2908", config=str(tmp_path / "forge.yaml"))) == 1
    err = capsys.readouterr().err
    assert "exists on disk but could not be read" in err


def test_run_lookup_does_not_claim_another_storys_corrupt_resume_record(
    tmp_path: Path, capsys
) -> None:
    """The same pattern in the second store, which is scanned whole for a run."""
    _project(tmp_path)
    resume_dir = tmp_path / ".forge" / "resume_state"
    resume_dir.mkdir(parents=True)
    (resume_dir / "issue-4242.json").write_text("{not json", encoding="utf-8")

    assert explain.cmd_explain(_Args(run="missing-run", config=str(tmp_path / "forge.yaml"))) == 1
    err = capsys.readouterr().err
    assert "exists on disk but could not be read" not in err
    assert "skipped" in err


def test_unfinished_record_without_routing_says_why(tmp_path: Path, capsys) -> None:
    """A story stopped before the router ran: absent, but not for the legacy reason."""
    _project(tmp_path)
    _flush_live_audit(tmp_path, routing=None)

    assert explain.cmd_explain(_Args(story=SLUG, config=str(tmp_path / "forge.yaml"))) == 1
    err = capsys.readouterr().err
    assert "carries no routing_decision block yet" in err
    assert "#1391" not in err


# ── The finder itself ─────────────────────────────────────────────────────


def test_finder_reads_a_single_run_layout(tmp_path: Path) -> None:
    """`forge run` writes the same audit one directory shallower."""
    story_dir = tmp_path / ".forge" / "logs" / SLUG
    story_dir.mkdir(parents=True)
    (story_dir / "audit.yaml").write_text(
        yaml.dump({"run_id": RUN_ID, "in_flight": True, "routing_decision": _routing_block()}),
        encoding="utf-8",
    )

    found = explain_live.find_live_record(tmp_path, slug=SLUG).found
    assert found is not None
    assert found.store == explain_live.STORE_IN_FLIGHT_AUDIT
    assert found.has_routing_decision


@pytest.mark.parametrize(
    ("payload", "expected_store"),
    [
        ({"run_id": RUN_ID, "in_flight": True}, explain_live.STORE_IN_FLIGHT_AUDIT),
        (
            {"run_id": RUN_ID, "in_flight": False, "interrupted_by": "stopped"},
            explain_live.STORE_INTERRUPTED_AUDIT,
        ),
        ({"run_id": RUN_ID}, explain_live.STORE_UNPUBLISHED_AUDIT),
    ],
)
def test_finder_names_the_state_the_record_is_in(
    tmp_path: Path, payload: dict, expected_store: str
) -> None:
    story_dir = tmp_path / ".forge" / "logs" / SPRINT_NAME / SLUG
    story_dir.mkdir(parents=True)
    (story_dir / "audit.yaml").write_text(yaml.dump(payload), encoding="utf-8")

    found = explain_live.find_live_record(tmp_path, run_id=RUN_ID).found
    assert found is not None and found.store == expected_store


def test_finder_reports_nothing_for_an_empty_project(tmp_path: Path) -> None:
    lookup = explain_live.find_live_record(tmp_path, slug=SLUG)
    assert lookup.found is None
    assert lookup.unreadable == ()
    assert lookup.unattributed == ()
    assert any("resume_state" in entry for entry in lookup.searched)


def test_finder_splits_unreadable_by_whether_the_path_names_the_target(
    tmp_path: Path,
) -> None:
    for name in (SLUG, "issue-4242"):
        story_dir = tmp_path / ".forge" / "logs" / SPRINT_NAME / name
        story_dir.mkdir(parents=True)
        (story_dir / "audit.yaml").write_text("{{ not: [valid\n", encoding="utf-8")

    by_slug = explain_live.find_live_record(tmp_path, slug=SLUG)
    assert [path.parent.name for path, _ in by_slug.unreadable] == [SLUG]
    assert [path.parent.name for path, _ in by_slug.unattributed] == ["issue-4242"]

    by_run = explain_live.find_live_record(tmp_path, run_id=RUN_ID)
    assert by_run.unreadable == ()
    assert len(by_run.unattributed) == 2
