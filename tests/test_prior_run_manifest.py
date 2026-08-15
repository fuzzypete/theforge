from __future__ import annotations

from pathlib import Path

import yaml

from theforge.task.prior_run_manifest import build_manifest
from theforge.task.prior_run_selector import select_prior_runs

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
        },
        "what_was_learned": [{"claim": "retries need a jitter cap", "evidence": []}],
        "learned_patterns": ["retry-decorator"],
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
    assert [item["run_id"] for item in manifest["included"]] == ["4f2a91c"]
    dropped = {item["run_id"]: item["reason"] for item in manifest["dropped"]}
    assert dropped["71bd334"] == "inadmissible(cited_source_deleted)"
    assert dropped["0ae5f92"] == "inadmissible(no_verdict)"
    assert "2 summaries matched but were excluded on admissibility" in manifest["note"]
    assert manifest["included"][0]["verdict"]["status"] == "admissible"


def test_manifest_note_reports_absence_when_nothing_is_indexed(tmp_path: Path) -> None:
    selection = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)
    manifest = build_manifest(selection, included_run_ids=set(), phase="dev")

    assert manifest["included"] == []
    assert manifest["dropped"] == []
    assert "no relevant prior knowledge exists" in manifest["note"]


def test_manifest_note_reports_phase_ineligibility(tmp_path: Path) -> None:
    _corpus(tmp_path, [_entry("4f2a91c")])
    selection = select_prior_runs(tmp_path, phase="preflight", story_text=_STORY, file_list=_FILES)
    manifest = build_manifest(selection, included_run_ids=set(), phase="preflight")

    assert "not injected in the preflight phase" in manifest["note"]


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
