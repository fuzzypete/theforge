from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from theforge.task.prior_run_selector import (
    _RENDERED_SIZE_KIND,
    _RENDERED_SIZE_METHOD,
    _RENDERED_SIZE_UNIT,
    INDEX_STATE_MISSING,
    INDEX_STATE_READY,
    INDEX_STATE_STALE_SCHEMA,
    INDEX_STATE_UNREADABLE,
    select_prior_runs,
)

_STORY = "Refactor the sprint runner retry loop"
_FILES = ["src/theforge/sprint/runner.py"]
_PREFLIGHT_FORBIDDEN = [
    "SENTINEL what changed description",
    "SENTINEL dominant difficulty prose",
    "SENTINEL recurring finding prose",
    "SENTINEL resolved finding prose",
    "SENTINEL review observation prose",
    "SENTINEL learned pattern tag",
    "SENTINEL plan-step lesson",
]


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
            "description": "SENTINEL what changed description",
            "approach": "Use a bounded helper before touching retry call sites",
            "files_modified": ["src/theforge/sprint/runner.py"],
        },
        "what_was_learned": [
            {
                "claim": "Prefer a helper seam around the retry loop",
                "evidence": [{"type": "file", "path": "src/theforge/sprint/runner.py"}],
            },
            {
                "claim": "SENTINEL plan-step lesson",
                "evidence": [{"type": "plan_step", "step_id": "s-2"}],
            },
        ],
        "learned_patterns": ["SENTINEL learned pattern tag"],
        "review_insights": {
            "recurring_findings": [
                {
                    "finding_id": "f-007",
                    "description": "SENTINEL recurring finding prose",
                    "cycles_seen": 3,
                }
            ],
            "resolved_findings": [
                {
                    "finding_id": "f-003",
                    "description": "SENTINEL resolved finding prose",
                    "resolution": "SENTINEL resolution prose",
                }
            ],
            "observations": ["SENTINEL review observation prose"],
        },
        "complexity_signal": {
            "actual_iterations": 2,
            "review_cycles": 3,
            "plan_regenerations": 1,
            "cost_usd": 4.25,
            "dominant_difficulty": "SENTINEL dominant difficulty prose",
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


def test_relevant_summary_is_selected_with_deterministic_reasons(tmp_path: Path) -> None:
    _corpus(tmp_path, [_entry("4f2a91c")])

    selection = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)

    assert [c.run_id for c in selection.candidates] == ["4f2a91c"]
    candidate = selection.candidates[0]
    assert "file_overlap(src/theforge/sprint/runner.py)" in candidate.reason
    assert "domain_match(sprint)" in candidate.reason
    assert candidate.score > 0
    assert "Related changed files: src/theforge/sprint/runner.py" in candidate.content
    assert "Evidence-backed implementation patterns:" in candidate.content
    assert candidate.rendered_size.method == _RENDERED_SIZE_METHOD
    assert candidate.rendered_size.unit == _RENDERED_SIZE_UNIT
    assert candidate.rendered_size.kind == _RENDERED_SIZE_KIND
    assert not selection.excluded


def test_rendered_size_is_recorded_as_unavailable_when_tokenizer_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _corpus(tmp_path, [_entry("4f2a91c")])

    def _missing(_name: str) -> object:
        raise ImportError("missing")

    monkeypatch.setattr("theforge.task.prior_run_selector.import_module", _missing)

    selection = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)

    rendered_size = selection.candidates[0].rendered_size
    assert rendered_size.value is None
    assert rendered_size.method == _RENDERED_SIZE_METHOD
    assert rendered_size.unit == _RENDERED_SIZE_UNIT
    assert rendered_size.kind == _RENDERED_SIZE_KIND
    assert rendered_size.unavailable_reason == "tiktoken_not_installed"


def test_irrelevant_summary_is_excluded_as_not_relevant(tmp_path: Path) -> None:
    _corpus(
        tmp_path,
        [
            _entry(
                "9c11e0a",
                changed_files=["docs/vision.md"],
                domains=["docs"],
                story_name="Rewrite onboarding docs",
            )
        ],
    )

    selection = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)

    assert selection.candidates == ()
    assert [(e.run_id, e.reason) for e in selection.excluded] == [("9c11e0a", "not_relevant")]


def test_inadmissible_verdict_is_never_selected(tmp_path: Path) -> None:
    _corpus(
        tmp_path,
        [
            _entry(
                "71bd334",
                verdict=_verdict(
                    status="inadmissible", rank="excluded", reasons=["cited_source_deleted"]
                ),
            )
        ],
    )

    selection = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)

    assert selection.candidates == ()
    exclusion = selection.excluded[0]
    assert exclusion.reason == "inadmissible(cited_source_deleted)"
    assert exclusion.admissibility_excluded is True


def test_missing_verdict_is_never_selected(tmp_path: Path) -> None:
    _corpus(tmp_path, [_entry("0ae5f92", verdict=None)])

    selection = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)

    assert selection.candidates == ()
    assert selection.excluded[0].reason == "inadmissible(no_verdict)"
    assert selection.excluded[0].admissibility_excluded is True


def test_malformed_verdict_block_fails_closed(tmp_path: Path) -> None:
    _corpus(tmp_path, [_entry("bad0001", verdict={"status": "admissible"})])

    selection = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)

    assert selection.candidates == ()
    assert selection.excluded[0].reason == "inadmissible(no_verdict)"


def test_preflight_phase_gets_signal_only_advisory_context(tmp_path: Path) -> None:
    _corpus(tmp_path, [_entry("4f2a91c")])

    selection = select_prior_runs(tmp_path, phase="preflight", story_text=_STORY, file_list=_FILES)

    assert selection.phase_eligible is True
    assert selection.rendering_mode == "signal_only"
    assert [c.run_id for c in selection.candidates] == ["4f2a91c"]
    content = selection.candidates[0].content
    assert "Preflight note" in content
    assert "Story shape: work_type=refactor, complexity=medium, complexity_score=6" in content
    assert (
        "Run signals: actual_iterations=2, review_cycles=3, plan_regenerations=1, cost_usd=4.25"
    ) in content
    assert "Recurring findings: count=1; id=f-007, cycles_seen=3" in content
    assert "Resolved findings: count=1; id=f-003" in content
    for sentinel in _PREFLIGHT_FORBIDDEN:
        assert sentinel not in content


def test_preflight_finding_counts_use_full_totals_not_render_cap(tmp_path: Path) -> None:
    _write_index(tmp_path, [_entry("4f2a91c")])
    _write_summary(
        tmp_path,
        "4f2a91c",
        review_insights={
            "recurring_findings": [
                {
                    "finding_id": f"f-{index:03d}",
                    "description": f"detail {index}",
                    "cycles_seen": 2,
                }
                for index in range(1, 8)
            ],
            "resolved_findings": [
                {"finding_id": f"r-{index:03d}", "description": f"resolved {index}"}
                for index in range(1, 7)
            ],
            "observations": ["SENTINEL review observation prose"],
        },
    )

    selection = select_prior_runs(tmp_path, phase="preflight", story_text=_STORY, file_list=_FILES)

    content = selection.candidates[0].content
    assert "Recurring findings: count=7;" in content
    assert "Resolved findings: count=6;" in content
    assert "id=f-006" not in content
    assert "id=r-006" not in content


def test_phase_specific_rendering_changes_by_phase(tmp_path: Path) -> None:
    _corpus(tmp_path, [_entry("4f2a91c")])

    plan = select_prior_runs(tmp_path, phase="plan", story_text=_STORY, file_list=_FILES)
    dev = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)
    review = select_prior_runs(tmp_path, phase="review", story_text=_STORY, file_list=_FILES)

    plan_content = plan.candidates[0].content
    dev_content = dev.candidates[0].content
    review_content = review.candidates[0].content

    assert "Prior approach: Use a bounded helper before touching retry call sites" in plan_content
    assert "Lessons with resolved evidence:" in plan_content
    assert "SENTINEL plan-step lesson" in plan_content

    assert "Related changed files: src/theforge/sprint/runner.py" in dev_content
    assert "Evidence-backed implementation patterns:" in dev_content
    assert "Prefer a helper seam around the retry loop" in dev_content
    assert "SENTINEL learned pattern tag" not in dev_content

    assert "Recurring findings to re-check:" in review_content
    assert "f-007: SENTINEL recurring finding prose (cycles_seen=3)" in review_content
    assert "Resolved findings worth verifying stayed fixed:" in review_content
    assert "SENTINEL review observation prose" in review_content
    assert (
        "Review-cycle signals: review_cycles=3, actual_iterations=2, plan_regenerations=1"
    ) in review_content


def test_missing_index_repairs_to_a_readable_empty_corpus(tmp_path: Path) -> None:
    selection = select_prior_runs(tmp_path, phase="plan", story_text=_STORY, file_list=_FILES)

    assert selection.candidates == ()
    assert selection.excluded == ()
    assert selection.entry_count == 0
    assert selection.index_state == INDEX_STATE_READY
    assert selection.phase == "plan"
    assert selection.rendering_mode == "phase_summary"
    assert (tmp_path / ".forge" / "knowledge" / "index.yaml").exists()


@pytest.mark.parametrize(
    "initial_state",
    [
        pytest.param("missing", id="missing"),
        pytest.param("stale", id="stale"),
        pytest.param("unreadable", id="unreadable"),
    ],
)
def test_selector_repairs_index_states_before_scoring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial_state: str,
) -> None:
    calls = 0

    def _rebuild(_project_root: Path) -> object:
        nonlocal calls
        calls += 1
        _write_index(tmp_path, [_entry("4f2a91c")])
        _write_summary(tmp_path, "4f2a91c")
        return object()

    monkeypatch.setattr("theforge.task.prior_run_selector.rebuild_knowledge_index", _rebuild)

    path = tmp_path / ".forge" / "knowledge" / "index.yaml"
    if initial_state == "stale":
        _write_index(tmp_path, [], schema_version=1)
    elif initial_state == "unreadable":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(": not: yaml:", encoding="utf-8")

    selection = select_prior_runs(tmp_path, phase="plan", story_text=_STORY, file_list=_FILES)

    assert calls == 1
    assert selection.index_state == INDEX_STATE_READY
    assert [candidate.run_id for candidate in selection.candidates] == ["4f2a91c"]
    assert selection.entry_count == 1


@pytest.mark.parametrize(
    ("initial_state", "expected_state"),
    [
        pytest.param("missing", INDEX_STATE_MISSING, id="missing"),
        pytest.param("stale", INDEX_STATE_STALE_SCHEMA, id="stale"),
        pytest.param("unreadable", INDEX_STATE_UNREADABLE, id="unreadable"),
    ],
)
def test_selector_preserves_failed_closed_index_state_when_repair_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial_state: str,
    expected_state: str,
) -> None:
    calls = 0

    def _boom(_project_root: Path) -> object:
        nonlocal calls
        calls += 1
        raise RuntimeError("rebuild failed")

    monkeypatch.setattr("theforge.task.prior_run_selector.rebuild_knowledge_index", _boom)

    path = tmp_path / ".forge" / "knowledge" / "index.yaml"
    if initial_state == "stale":
        _write_index(tmp_path, [], schema_version=1)
    elif initial_state == "unreadable":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(": not: yaml:", encoding="utf-8")

    selection = select_prior_runs(tmp_path, phase="plan", story_text=_STORY, file_list=_FILES)

    assert calls == 1
    assert selection.candidates == ()
    assert selection.excluded == ()
    assert selection.entry_count == 0
    assert selection.index_state == expected_state


def test_valid_empty_index_still_reports_ready_state(tmp_path: Path) -> None:
    _write_index(tmp_path, [])

    selection = select_prior_runs(tmp_path, phase="plan", story_text=_STORY, file_list=_FILES)

    assert selection.candidates == ()
    assert selection.excluded == ()
    assert selection.entry_count == 0
    assert selection.index_state == INDEX_STATE_READY


def test_missing_summary_artifact_excludes_the_entry(tmp_path: Path) -> None:
    _write_index(tmp_path, [_entry("4f2a91c")])

    selection = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)

    assert selection.candidates == ()
    assert selection.excluded[0].reason == "summary_unreadable"


def test_reduced_rank_is_penalised_relative_to_full_rank(tmp_path: Path) -> None:
    _corpus(
        tmp_path,
        [
            _entry("aaa1111"),
            _entry(
                "bbb2222",
                verdict=_verdict(
                    status="admissible_with_reduced_rank",
                    rank="reduced",
                    reasons=["sources_changed"],
                ),
            ),
        ],
    )

    selection = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)

    by_id = {c.run_id: c for c in selection.candidates}
    assert by_id["aaa1111"].score > by_id["bbb2222"].score
    assert "reduced_rank" in by_id["bbb2222"].reason


def test_weak_reduced_rank_entry_is_excluded_as_stale(tmp_path: Path) -> None:
    _corpus(
        tmp_path,
        [
            _entry(
                "ccc3333",
                changed_files=["src/theforge/sprint/other.py"],
                domains=["unrelated"],
                story_name="unrelated work",
                verdict=_verdict(
                    status="admissible_with_reduced_rank",
                    rank="reduced",
                    reasons=["sources_changed"],
                ),
            )
        ],
    )

    selection = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)

    assert selection.candidates == ()
    assert selection.excluded[0].reason == "stale(sources_changed)"


def test_selection_is_capped_and_deterministic(tmp_path: Path) -> None:
    _corpus(tmp_path, [_entry(f"run{index}") for index in range(6)])

    first = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)
    second = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)

    assert len(first.candidates) == 3
    assert [c.run_id for c in first.candidates] == [c.run_id for c in second.candidates]
    # They matched; they lost to better matches. That is not "not relevant".
    assert all(e.reason == "below_selection_cap(3)" for e in first.excluded)
    assert all(e.admissibility_excluded is False for e in first.excluded)


def test_summary_prose_cannot_create_relevance(tmp_path: Path) -> None:
    """Deterministic fields say 'unrelated'; prose screams the story's keywords."""
    _write_index(
        tmp_path,
        [
            _entry(
                "ddd4444",
                changed_files=["docs/vision.md"],
                domains=["docs"],
                story_name="unrelated",
            )
        ],
    )
    _write_summary(
        tmp_path,
        "ddd4444",
        what_changed={
            "description": "sprint runner retry loop refactor sprint runner retry",
            "approach": "sprint runner retry loop",
        },
    )

    selection = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)

    assert selection.candidates == ()
    assert selection.excluded[0].reason == "not_relevant"


def test_unrelated_inadmissible_entry_is_excluded_on_relevance_not_admissibility(
    tmp_path: Path,
) -> None:
    """An inadmissible summary about unrelated code is not knowledge this story lost.

    Relevance is settled first, so the entry reports as not_relevant and never
    inflates the manifest's "matched but excluded on admissibility" count.
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
            )
        ],
    )

    selection = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)

    assert selection.candidates == ()
    assert selection.excluded[0].reason == "not_relevant"
    assert selection.excluded[0].admissibility_excluded is False


def test_unrelated_missing_verdict_entry_is_excluded_on_relevance(tmp_path: Path) -> None:
    _corpus(
        tmp_path,
        [
            _entry(
                "0ae5f92",
                changed_files=["docs/vision.md"],
                domains=["docs"],
                story_name="Rewrite onboarding docs",
                verdict=None,
            )
        ],
    )

    selection = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)

    assert selection.candidates == ()
    assert selection.excluded[0].reason == "not_relevant"
    assert selection.excluded[0].admissibility_excluded is False


def test_relevance_first_ordering_still_bars_relevant_inadmissible_summaries(
    tmp_path: Path,
) -> None:
    """Deciding relevance first must not soften admissibility as a bar on inclusion."""
    _corpus(
        tmp_path,
        [
            _entry(
                "71bd334",
                verdict=_verdict(
                    status="inadmissible", rank="excluded", reasons=["source_run_tainted"]
                ),
            ),
            _entry("0ae5f92", verdict=None),
        ],
    )

    selection = select_prior_runs(tmp_path, phase="dev", story_text=_STORY, file_list=_FILES)

    assert selection.candidates == ()
    reasons = {e.run_id: e.reason for e in selection.excluded}
    assert reasons["71bd334"] == "inadmissible(source_run_tainted)"
    assert reasons["0ae5f92"] == "inadmissible(no_verdict)"
    assert all(e.admissibility_excluded for e in selection.excluded)
