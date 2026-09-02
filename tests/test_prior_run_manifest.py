from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from theforge.task.prior_run_manifest import build_manifest
from theforge.task.prior_run_selector import (
    _RENDERED_SIZE_KIND,
    _RENDERED_SIZE_METHOD,
    _RENDERED_SIZE_UNIT,
    _measure_rendered_summary,
    select_prior_runs,
)

_STORY = "Refactor the sprint runner retry loop"
_FILES = ["src/theforge/sprint/runner.py"]


def _verdict(status: str = "admissible", rank: str = "full", reasons: list[str] | None = None):
    verdict: dict = {"status": status, "rank": rank}
    if reasons:
        verdict["reasons"] = reasons
    return verdict


def _entry(
    run_id: str,
    *,
    changed_files: list[str] | None = None,
    domains: list[str] | None = None,
    patterns: list[str] | None = None,
    verdict: dict | None = _verdict(),
    generated_at: str = "2026-08-01T00:00:00",
    story_name: str = "Sprint runner retry",
) -> dict:
    entry: dict = {
        "run_id": run_id,
        "generated_at": generated_at,
        "story": {"slug": run_id, "name": story_name, "github_issue": 1},
        "story_shape": {"work_type": "refactor", "complexity": "medium"},
        "domains": domains if domains is not None else ["sprint"],
        "changed_files": changed_files
        if changed_files is not None
        else ["src/theforge/sprint/runner.py"],
        "learned_patterns": patterns or [],
        "summary_path": f".forge/knowledge/summaries/{run_id}.yaml",
    }
    if verdict is not None:
        entry["admissibility_verdict"] = verdict
    return entry


def _write_index(root: Path, entries: list[dict], *, schema_version: int = 2) -> None:
    path = root / ".forge" / "knowledge" / "index.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": schema_version,
        "source_count": len(entries),
        "indexed_count": len(entries),
        "skipped_count": 0,
        "entries": entries,
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_summary(root: Path, run_id: str, **overrides) -> None:
    path = root / ".forge" / "knowledge" / "summaries" / f"{run_id}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    artifact = {
        "schema_version": 1,
        "run_id": run_id,
        "what_changed": {
            "description": f"run {run_id} reworked the retry loop",
            "approach": "extracted a helper",
            "files_modified": ["src/theforge/sprint/runner.py"],
        },
        "what_was_learned": [
            {
                "claim": "retries need a jitter cap",
                "evidence": [{"type": "file", "path": "src/theforge/sprint/runner.py"}],
            }
        ],
        "learned_patterns": ["retry-decorator"],
        "review_insights": {
            "recurring_findings": [
                {
                    "finding_id": "f-007",
                    "description": "missing timeout",
                    "cycles_seen": 2,
                }
            ],
            "resolved_findings": [
                {
                    "finding_id": "f-003",
                    "description": "race condition",
                    "resolution": "guarded helper",
                }
            ],
            "observations": ["verify the timeout path"],
        },
        "complexity_signal": {
            "actual_iterations": 2,
            "review_cycles": 2,
            "plan_regenerations": 1,
            "cost_usd": 4.25,
            "dominant_difficulty": "edge case coverage",
        },
        "story_shape": {
            "work_type": "refactor",
            "complexity": "medium",
            "complexity_score": 6,
            "contract_change": False,
        },
    }
    artifact.update(overrides)
    path.write_text(yaml.safe_dump(artifact, sort_keys=False), encoding="utf-8")


def _corpus(root: Path, entries: list[dict]) -> None:
    _write_index(root, entries)
    for entry in entries:
        _write_summary(root, entry["run_id"])


def test_manifest_note_separates_admissibility_from_absence(tmp_path: Path) -> None:
    _corpus(
        tmp_path,
        [
            _entry("4f2a91c"),
            _entry(
                "71bd334",
                verdict=_verdict(
                    status="inadmissible", rank="excluded", reasons=["cited_source_deleted"]
                ),
            ),
            _entry("0ae5f92", verdict=None),
        ],
    )

    selection = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)
    manifest = build_manifest(selection, included_run_ids={"4f2a91c"}, phase="dev")

    assert manifest["enabled"] is True
    assert manifest["phase"] == "dev"
    assert manifest["rendering_mode"] == "phase_summary"
    assert [item["run_id"] for item in manifest["included"]] == ["4f2a91c"]
    assert manifest["included"][0]["phase"] == "dev"
    assert manifest["included"][0]["rendering_mode"] == "phase_summary"
    assert manifest["included"][0]["rendered_size"]["method"] == _RENDERED_SIZE_METHOD
    assert manifest["included"][0]["rendered_size"]["unit"] == _RENDERED_SIZE_UNIT
    assert manifest["included"][0]["rendered_size"]["kind"] == _RENDERED_SIZE_KIND
    dropped = {item["run_id"]: item["reason"] for item in manifest["dropped"]}
    assert dropped["71bd334"] == "inadmissible(cited_source_deleted)"
    assert dropped["0ae5f92"] == "inadmissible(no_verdict)"
    assert "2 summaries matched but were excluded on admissibility" in manifest["note"]
    assert manifest["included"][0]["verdict"]["status"] == "admissible"


def test_manifest_note_reports_absence_when_nothing_is_indexed(tmp_path: Path) -> None:
    selection = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)
    manifest = build_manifest(selection, included_run_ids=set(), phase="dev")

    assert manifest["index_state"] == "ready"
    assert manifest["included"] == []
    assert manifest["dropped"] == []
    assert manifest["note"] == "no relevant prior knowledge exists (no indexed summaries)"


def test_manifest_note_repairs_unreadable_index_with_rebuild(tmp_path: Path) -> None:
    path = tmp_path / ".forge" / "knowledge" / "index.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(": not: yaml:", encoding="utf-8")

    selection = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)
    manifest = build_manifest(selection, included_run_ids=set(), phase="dev")

    assert manifest["index_state"] == "ready"
    assert manifest["note"] == "no relevant prior knowledge exists (no indexed summaries)"


def test_manifest_note_repairs_malformed_entries_with_rebuild(tmp_path: Path) -> None:
    _write_index(tmp_path, [])
    path = tmp_path / ".forge" / "knowledge" / "index.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["entries"] = ["not-a-mapping"]
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    selection = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)
    manifest = build_manifest(selection, included_run_ids=set(), phase="dev")

    assert manifest["index_state"] == "ready"
    assert manifest["note"] == "no relevant prior knowledge exists (no indexed summaries)"


def test_manifest_note_repairs_stale_schema_index_with_rebuild(tmp_path: Path) -> None:
    _write_index(tmp_path, [], schema_version=1)

    selection = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)
    manifest = build_manifest(selection, included_run_ids=set(), phase="dev")

    assert manifest["index_state"] == "ready"
    assert manifest["note"] == "no relevant prior knowledge exists (no indexed summaries)"


def test_manifest_note_preserves_existing_empty_index_message(tmp_path: Path) -> None:
    _write_index(tmp_path, [])

    selection = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)
    manifest = build_manifest(selection, included_run_ids=set(), phase="dev")

    assert manifest["index_state"] == "ready"
    assert manifest["note"] == "no relevant prior knowledge exists (no indexed summaries)"


@pytest.mark.parametrize(
    ("initial_state", "expected_state", "expected_note"),
    [
        pytest.param(
            "missing",
            "missing",
            "prior-run knowledge index is missing or was never built; "
            "run `forge index` to build .forge/knowledge/index.yaml",
            id="missing",
        ),
        pytest.param(
            "unreadable",
            "unreadable",
            "prior-run knowledge index is unreadable; "
            "run `forge index` to rebuild .forge/knowledge/index.yaml",
            id="unreadable",
        ),
        pytest.param(
            "stale",
            "stale_schema",
            "prior-run knowledge index uses an unsupported schema version; "
            "run `forge index` to rebuild .forge/knowledge/index.yaml",
            id="stale",
        ),
    ],
)
def test_manifest_note_preserves_failed_closed_index_states_when_repair_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial_state: str,
    expected_state: str,
    expected_note: str,
) -> None:
    monkeypatch.setattr(
        "theforge.task.prior_run_selector.rebuild_knowledge_index",
        lambda _project_root: (_ for _ in ()).throw(RuntimeError("rebuild failed")),
    )

    path = tmp_path / ".forge" / "knowledge" / "index.yaml"
    if initial_state == "stale":
        _write_index(tmp_path, [], schema_version=1)
    elif initial_state == "unreadable":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(": not: yaml:", encoding="utf-8")

    selection = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)
    manifest = build_manifest(selection, included_run_ids=set(), phase="dev")

    assert manifest["index_state"] == expected_state
    assert manifest["note"] == expected_note


def test_manifest_note_reports_unsupported_phase_ineligibility(tmp_path: Path) -> None:
    _corpus(tmp_path, [_entry("4f2a91c")])
    selection = select_prior_runs(tmp_path, phase="validate", story_text=_STORY, file_list=_FILES)
    manifest = build_manifest(selection, included_run_ids=set(), phase="validate")

    assert "not injected in the validate phase" in manifest["note"]
    assert "supported phases: dev, plan, preflight, review" in manifest["note"]


def test_manifest_surfaces_signal_only_preflight_rendering(tmp_path: Path) -> None:
    _corpus(tmp_path, [_entry("4f2a91c")])

    selection = select_prior_runs(tmp_path, phase="preflight", story_text=_STORY, file_list=_FILES)
    manifest = build_manifest(selection, included_run_ids={"4f2a91c"}, phase="preflight")

    assert manifest["phase"] == "preflight"
    assert manifest["rendering_mode"] == "signal_only"
    assert manifest["included"][0]["phase"] == "preflight"
    assert manifest["included"][0]["rendering_mode"] == "signal_only"
    assert manifest["included"][0]["reason"].startswith("file_overlap(")


def test_manifest_omits_rendered_size_when_summary_was_not_included(tmp_path: Path) -> None:
    _corpus(tmp_path, [_entry("4f2a91c")])

    selection = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)
    manifest = build_manifest(selection, included_run_ids=set(), phase="dev")

    assert manifest["included"] == []
    assert "rendered_size" not in manifest["dropped"][0]


def test_manifest_marks_unmeasured_rendered_size_when_counting_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _corpus(tmp_path, [_entry("4f2a91c")])

    def _broken(*_args, **_kwargs) -> list[str]:
        raise RuntimeError("boom")

    monkeypatch.setattr("theforge.task.prior_run_selector._estimated_token_count", _broken)

    selection = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)
    manifest = build_manifest(selection, included_run_ids={"4f2a91c"}, phase="dev")

    assert manifest["included"][0]["rendered_size"] == {
        "value": None,
        "unit": _RENDERED_SIZE_UNIT,
        "method": _RENDERED_SIZE_METHOD,
        "kind": _RENDERED_SIZE_KIND,
        "unavailable_reason": "measurement_failed",
    }


def test_manifest_records_measured_rendered_size_from_selected_summary_text(
    tmp_path: Path,
) -> None:
    _corpus(tmp_path, [_entry("4f2a91c")])

    selection = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)
    manifest = build_manifest(selection, included_run_ids={"4f2a91c"}, phase="dev")

    candidate = selection.candidates[0]
    assert manifest["included"][0]["rendered_size"] == {
        "value": _measure_rendered_summary(candidate.content).value,
        "unit": _RENDERED_SIZE_UNIT,
        "method": _RENDERED_SIZE_METHOD,
        "kind": _RENDERED_SIZE_KIND,
    }


def test_note_does_not_claim_unrelated_inadmissible_summaries_as_withheld(
    tmp_path: Path,
) -> None:
    """The index holds only inadmissible entries about unrelated code.

    Nothing this story could have used was withheld, so the note must read as
    absence — not as "1 summary matched but was excluded on admissibility".
    """
    _corpus(
        tmp_path,
        [
            _entry(
                "71bd334",
                changed_files=["docs/vision.md"],
                domains=["docs"],
                story_name="Rewrite onboarding docs",
                verdict=_verdict(
                    status="inadmissible", rank="excluded", reasons=["cited_source_deleted"]
                ),
            ),
            _entry(
                "0ae5f92",
                changed_files=["docs/vision.md"],
                domains=["docs"],
                story_name="Rewrite onboarding docs",
                verdict=None,
            ),
        ],
    )

    selection = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)
    manifest = build_manifest(selection, included_run_ids=set(), phase="dev")

    assert manifest["included"] == []
    assert manifest["note"] == "no relevant prior knowledge exists"
    assert "admissibility" not in manifest["note"]
    # The entries are still reported individually, just not as withheld knowledge.
    assert {item["reason"] for item in manifest["dropped"]} == {"not_relevant"}


def test_note_reports_matches_that_lost_to_better_ranked_matches(tmp_path: Path) -> None:
    _corpus(tmp_path, [_entry(f"run{index}") for index in range(5)])

    selection = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)
    manifest = build_manifest(
        selection,
        included_run_ids={item.run_id for item in selection.candidates},
        phase="dev",
    )

    assert len(manifest["included"]) == 3
    assert {item["reason"] for item in manifest["dropped"]} == {"below_selection_cap(3)"}
    assert "2 lower-ranked matches not offered" in manifest["note"]
    assert "admissibility" not in manifest["note"]


# ── Claim-exposure capture (#2684) ───────────────────────────────────────────


def _selection_for(tmp_path: Path, phase: str):
    _corpus(tmp_path, [_entry("run0")])
    return select_prior_runs(tmp_path, phase=phase, story_text=_STORY, file_list=_FILES)


def test_recorded_claims_are_exactly_the_claims_the_prompt_contained(tmp_path: Path) -> None:
    """The record must not drift from the prose an agent actually read.

    Claim text is truncated inside the renderer; a record derived from the
    summary artifact instead of from the render would list claims nobody saw,
    and the indicator would then blame an author for ignoring them.
    """
    selection = _selection_for(tmp_path, "dev")
    candidate = selection.candidates[0]
    manifest = build_manifest(
        selection,
        included_run_ids={candidate.run_id},
        phase="dev",
        agent_role="dev",
        phase_iteration=1,
        rendered_at="2026-08-01T10:00:00+00:00",
    )

    recorded = [claim["claim"] for claim in manifest["included"][0]["claims"]]
    assert recorded == list(candidate.claims)
    for claim in recorded:
        assert claim in candidate.content


def test_each_recorded_claim_names_its_phase_role_iteration_and_render_time(
    tmp_path: Path,
) -> None:
    selection = _selection_for(tmp_path, "dev")
    manifest = build_manifest(
        selection,
        included_run_ids={selection.candidates[0].run_id},
        phase="dev",
        agent_role="dev",
        phase_iteration=2,
        rendered_at="2026-08-01T10:00:00+00:00",
    )

    claim = manifest["included"][0]["claims"][0]
    assert claim["phase"] == "dev"
    assert claim["agent_role"] == "dev"
    assert claim["phase_iteration"] == 2
    assert claim["rendered_at"] == "2026-08-01T10:00:00+00:00"
    assert claim["run_id"] == "run0"
    assert claim["claim_ref"].startswith("run0:")
    assert claim["index"] == 1


def test_claim_reference_is_stable_across_renderings_of_the_same_claim(
    tmp_path: Path,
) -> None:
    dev = build_manifest(
        _selection_for(tmp_path, "dev"),
        included_run_ids={"run0"},
        phase="dev",
        agent_role="dev",
        phase_iteration=1,
        rendered_at="2026-08-01T10:00:00+00:00",
    )
    again = build_manifest(
        _selection_for(tmp_path, "dev"),
        included_run_ids={"run0"},
        phase="dev",
        agent_role="dev",
        phase_iteration=5,
        rendered_at="2026-08-01T18:00:00+00:00",
    )

    assert [c["claim_ref"] for c in dev["included"][0]["claims"]] == [
        c["claim_ref"] for c in again["included"][0]["claims"]
    ]


def test_dropped_candidates_carry_no_claims(tmp_path: Path) -> None:
    """A summary that lost to the budget rendered nothing into any prompt."""
    selection = _selection_for(tmp_path, "dev")
    manifest = build_manifest(
        selection,
        included_run_ids=set(),
        phase="dev",
        agent_role="dev",
        phase_iteration=1,
        rendered_at="2026-08-01T10:00:00+00:00",
    )

    assert manifest["included"] == []
    assert all("claims" not in item for item in manifest["dropped"])


def test_preflight_signal_rendering_records_zero_claims(tmp_path: Path) -> None:
    """ADR-0002 clause 5: preflight receives signals, never claim prose."""
    selection = _selection_for(tmp_path, "preflight")
    manifest = build_manifest(
        selection,
        included_run_ids={c.run_id for c in selection.candidates},
        phase="preflight",
        agent_role="preflight",
        phase_iteration=1,
        rendered_at="2026-08-01T10:00:00+00:00",
    )

    assert manifest["included"], "preflight still receives a signal rendering"
    assert all(item["claims"] == [] for item in manifest["included"])


def test_disabled_manifest_still_records_that_exposure_was_captured() -> None:
    from theforge.task.prior_run_manifest import disabled_manifest

    manifest = disabled_manifest(
        agent_role="dev", phase_iteration=1, rendered_at="2026-08-01T10:00:00+00:00"
    )

    # Captured-and-empty, not uncaptured: the run knows nothing was rendered.
    assert manifest["claim_exposure"]["capture_version"] == 1
    assert manifest["claim_exposure"]["agent_role"] == "dev"
