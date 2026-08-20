from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

from theforge.prior_run_replay import (
    CorpusSpec,
    Judgment,
    PriorRunReplayError,
    _aggregate_corpus_metrics,
    _claim_hash,
    _discover_fixtures,
    _judgment_key,
    _load_judgments,
    _phase_inputs,
    _replay_story,
    run_prior_run_replay,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def _init_repo(root: Path) -> str:
    _git(root, "init")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "user.email", "test@example.com")
    _write(root / "forge.yaml", "project: demo\n")
    _write(root / "src/demo.py", "print('base')\n")
    _git(root, "add", "forge.yaml", "src/demo.py")
    _git(root, "commit", "-m", "initial")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _summary_payload(
    run_id: str,
    *,
    story_slug: str,
    story_name: str,
    generated_at: str,
    claims: list[str],
    changed_files: list[str] | None = None,
    review_insights: dict | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "authoritative_run_record": f".forge/audits/runs/{run_id}.json",
        "generated_at": generated_at,
        "story": {
            "slug": story_slug,
            "name": story_name,
            "github_issue": 1,
        },
        "story_shape": {
            "work_type": "bug",
            "complexity": "small",
            "complexity_score": 3,
            "contract_change": False,
        },
        "domains": ["parser"],
        "changed_files": changed_files or ["src/demo.py"],
        "what_changed": {
            "description": "Adjusted parser behavior",
            "approach": "Changed the parser implementation",
            "files_modified": ["src/demo.py"],
        },
        "what_was_learned": [
            {
                "claim": claim,
                "evidence": [{"type": "file", "path": "src/demo.py"}],
            }
            for claim in claims
        ],
        "learned_patterns": ["parser-safety"],
        "review_insights": review_insights
        or {
            "recurring_findings": [],
            "resolved_findings": [],
            "observations": [],
        },
        "complexity_signal": {
            "actual_iterations": 1,
            "review_cycles": 1,
            "plan_regenerations": 0,
            "cost_usd": 1.0,
            "dominant_difficulty": "none",
        },
    }


def _run_record_payload(
    run_id: str,
    *,
    story_slug: str,
    story_name: str,
    story_text: str,
    started_at: str,
    finished_at: str,
    base_ref: str,
    changed_files: list[str] | None = None,
    plan_structured: dict | None = None,
) -> dict:
    return {
        "schema_version": 31,
        "run_id": run_id,
        "task": {
            "name": story_name,
            "slug": story_slug,
            "story_path": None,
            "story_text": story_text,
            "github_issue": 1,
            "fix_ready": True,
            "readiness_warnings": [],
        },
        "outcome": {
            "success": True,
            "final_phase": "DONE",
            "message": "done",
            "error_type": None,
            "start_phase": None,
            "stop_phase": None,
        },
        "timing": {
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": 60.0,
        },
        "changed_files": {
            "base_ref": base_ref,
            "head_ref": base_ref,
            "files": [
                {"path": path, "insertions": 1, "deletions": 0, "binary": False}
                for path in (changed_files or ["src/demo.py"])
            ],
        },
        "phases": {"plan": {"plan_structured": plan_structured} if plan_structured else {}},
        "trust_status": "trusted",
        "context_manifests": [],
    }


def _write_fixture(
    root: Path,
    run_id: str,
    *,
    story_slug: str,
    story_name: str,
    story_text: str,
    started_at: str,
    finished_at: str,
    generated_at: str,
    base_ref: str,
    claims: list[str],
    changed_files: list[str] | None = None,
    review_insights: dict | None = None,
    plan_structured: dict | None = None,
) -> None:
    _write(
        root / ".forge" / "knowledge" / "summaries" / f"{run_id}.yaml",
        yaml.safe_dump(
            _summary_payload(
                run_id,
                story_slug=story_slug,
                story_name=story_name,
                generated_at=generated_at,
                claims=claims,
                changed_files=changed_files,
                review_insights=review_insights,
            ),
            sort_keys=False,
        ),
    )
    _write(
        root / ".forge" / "audits" / "runs" / f"{run_id}.json",
        json.dumps(
            _run_record_payload(
                run_id,
                story_slug=story_slug,
                story_name=story_name,
                story_text=story_text,
                started_at=started_at,
                finished_at=finished_at,
                base_ref=base_ref,
                changed_files=changed_files,
                plan_structured=plan_structured,
            ),
            indent=2,
        )
        + "\n",
    )


def _judgment(
    corpus: str,
    replay_run_id: str,
    phase: str,
    prior_run_id: str,
    claim: str,
    effect: str,
) -> Judgment:
    return Judgment(
        corpus=corpus,
        replay_run_id=replay_run_id,
        phase=phase,
        prior_run_id=prior_run_id,
        claim_hash=_claim_hash(claim),
        claim=claim,
        effect=effect,
        rationale=f"{effect} rationale",
    )


def test_replay_filters_future_summaries_and_leaves_source_corpus_clean(tmp_path: Path) -> None:
    base_ref = _init_repo(tmp_path)
    _write_fixture(
        tmp_path,
        "aaa111",
        story_slug="issue-a",
        story_name="Earlier parser fix",
        story_text="Parser bug in demo flow",
        started_at="2026-08-01T00:00:00+00:00",
        finished_at="2026-08-01T00:10:00+00:00",
        generated_at="2026-08-01T00:10:00+00:00",
        base_ref=base_ref,
        claims=["Earlier lesson"],
    )
    _write_fixture(
        tmp_path,
        "bbb222",
        story_slug="issue-b",
        story_name="Replay target",
        story_text="Parser bug in replay target",
        started_at="2026-08-02T00:00:00+00:00",
        finished_at="2026-08-02T00:10:00+00:00",
        generated_at="2026-08-02T00:10:00+00:00",
        base_ref=base_ref,
        claims=[],
    )
    _write_fixture(
        tmp_path,
        "ccc333",
        story_slug="issue-c",
        story_name="Future parser fix",
        story_text="Parser bug in future story",
        started_at="2026-08-03T00:00:00+00:00",
        finished_at="2026-08-03T00:10:00+00:00",
        generated_at="2026-08-03T00:10:00+00:00",
        base_ref=base_ref,
        claims=["Future lesson"],
    )

    fixtures = _discover_fixtures(tmp_path)
    replay_fixture = next(item for item in fixtures if item.run_id == "bbb222")
    judgments = {
        _judgment_key("demo", "bbb222", "plan", "aaa111", "Earlier lesson"): _judgment(
            "demo", "bbb222", "plan", "aaa111", "Earlier lesson", "plan"
        ),
        _judgment_key("demo", "bbb222", "dev", "aaa111", "Earlier lesson"): _judgment(
            "demo", "bbb222", "dev", "aaa111", "Earlier lesson", "implementation"
        ),
    }

    report = _replay_story(CorpusSpec("demo", tmp_path), replay_fixture, fixtures, judgments)

    plan = next(item for item in report["phase_replays"] if item["phase"] == "plan")
    assert plan["status"] == "replayed"
    assert plan["file_list"] == ["src/demo.py"]
    assert [candidate["run_id"] for candidate in plan["candidates"]] == ["aaa111"]
    assert "ccc333" not in {candidate["run_id"] for candidate in plan["candidates"]}
    assert not (tmp_path / ".forge" / "knowledge" / "index.yaml").exists()


def test_replay_recovers_plan_and_later_phase_file_lists_from_persisted_artifacts(
    tmp_path: Path,
) -> None:
    base_ref = _init_repo(tmp_path)
    _write_fixture(
        tmp_path,
        "aaa111",
        story_slug="issue-a",
        story_name="Earlier parser fix",
        story_text="Parser overlap in src/api.py",
        started_at="2026-08-01T00:00:00+00:00",
        finished_at="2026-08-01T00:10:00+00:00",
        generated_at="2026-08-01T00:10:00+00:00",
        base_ref=base_ref,
        claims=["Earlier lesson"],
        changed_files=["src/api.py", "tests/test_api.py"],
    )
    _write_fixture(
        tmp_path,
        "bbb222",
        story_slug="issue-b",
        story_name="Replay target",
        story_text="Parser overlap in src/api.py",
        started_at="2026-08-02T00:00:00+00:00",
        finished_at="2026-08-02T00:10:00+00:00",
        generated_at="2026-08-02T00:10:00+00:00",
        base_ref=base_ref,
        claims=[],
        changed_files=["src/final_impl.py", "tests/test_final_impl.py"],
        plan_structured={
            "steps": [
                {
                    "id": "step-1",
                    "title": "Touch the api module",
                    "files": ["src/api.py", "tests/test_api.py"],
                }
            ]
        },
    )

    fixtures = _discover_fixtures(tmp_path)
    replay_fixture = next(item for item in fixtures if item.run_id == "bbb222")
    judgments = {
        _judgment_key("demo", "bbb222", "plan", "aaa111", "Earlier lesson"): _judgment(
            "demo", "bbb222", "plan", "aaa111", "Earlier lesson", "plan"
        ),
        _judgment_key("demo", "bbb222", "dev", "aaa111", "Earlier lesson"): _judgment(
            "demo", "bbb222", "dev", "aaa111", "Earlier lesson", "implementation"
        ),
    }

    report = _replay_story(CorpusSpec("demo", tmp_path), replay_fixture, fixtures, judgments)

    phase_reports = {phase["phase"]: phase for phase in report["phase_replays"]}
    assert phase_reports["plan"]["status"] == "replayed"
    assert phase_reports["plan"]["file_list"] == ["src/api.py", "tests/test_api.py"]
    assert phase_reports["plan"]["recovery_note"] == "recovered from phases.plan.plan_structured"
    assert phase_reports["dev"]["status"] == "replayed"
    assert phase_reports["dev"]["file_list"] == ["src/api.py", "tests/test_api.py"]
    assert phase_reports["dev"]["recovery_note"] == "recovered from phases.plan.plan_structured"
    assert phase_reports["review"]["file_list"] == ["src/api.py", "tests/test_api.py"]
    assert phase_reports["review"]["recovery_note"] == "recovered from phases.plan.plan_structured"


def test_replay_dev_and_review_do_not_fallback_to_persisted_changed_files(tmp_path: Path) -> None:
    base_ref = _init_repo(tmp_path)
    _write_fixture(
        tmp_path,
        "aaa111",
        story_slug="issue-a",
        story_name="Earlier parser fix",
        story_text="Parser overlap in src/api.py",
        started_at="2026-08-01T00:00:00+00:00",
        finished_at="2026-08-01T00:10:00+00:00",
        generated_at="2026-08-01T00:10:00+00:00",
        base_ref=base_ref,
        claims=["Earlier lesson"],
        changed_files=["src/api.py"],
    )
    _write_fixture(
        tmp_path,
        "bbb222",
        story_slug="issue-b",
        story_name="Replay target",
        story_text="Parser overlap in src/api.py",
        started_at="2026-08-02T00:00:00+00:00",
        finished_at="2026-08-02T00:10:00+00:00",
        generated_at="2026-08-02T00:10:00+00:00",
        base_ref=base_ref,
        claims=[],
        changed_files=["src/final_impl.py"],
        plan_structured=None,
    )

    fixtures = _discover_fixtures(tmp_path)
    replay_fixture = next(item for item in fixtures if item.run_id == "bbb222")
    judgments = {
        _judgment_key("demo", "bbb222", "plan", "aaa111", "Earlier lesson"): _judgment(
            "demo", "bbb222", "plan", "aaa111", "Earlier lesson", "plan"
        ),
        _judgment_key("demo", "bbb222", "dev", "aaa111", "Earlier lesson"): _judgment(
            "demo", "bbb222", "dev", "aaa111", "Earlier lesson", "implementation"
        ),
    }

    report = _replay_story(CorpusSpec("demo", tmp_path), replay_fixture, fixtures, judgments)

    phase_reports = {phase["phase"]: phase for phase in report["phase_replays"]}
    assert phase_reports["plan"]["status"] == "replayed"
    assert phase_reports["plan"]["file_list"] == ["src/final_impl.py"]
    assert (
        phase_reports["plan"]["recovery_note"]
        == "plan_structured missing; fell back to persisted changed_files"
    )
    assert phase_reports["dev"]["status"] == "replayed_missing_file_list"
    assert phase_reports["dev"]["file_list"] is None
    assert (
        phase_reports["dev"]["recovery_note"]
        == "audit record persists no phases.plan.plan_structured; "
        "dev/review replay runs without file overlap input"
    )
    assert phase_reports["review"]["status"] == "replayed_missing_file_list"
    assert phase_reports["review"]["file_list"] is None


def test_replay_later_phase_inputs_are_independent_mappings(tmp_path: Path) -> None:
    base_ref = _init_repo(tmp_path)
    _write_fixture(
        tmp_path,
        "bbb222",
        story_slug="issue-b",
        story_name="Replay target",
        story_text="Parser overlap in src/api.py",
        started_at="2026-08-02T00:00:00+00:00",
        finished_at="2026-08-02T00:10:00+00:00",
        generated_at="2026-08-02T00:10:00+00:00",
        base_ref=base_ref,
        claims=[],
        changed_files=["src/final_impl.py", "tests/test_final_impl.py"],
        plan_structured={
            "steps": [
                {
                    "id": "step-1",
                    "title": "Touch the api module",
                    "files": ["src/api.py", "tests/test_api.py"],
                }
            ]
        },
    )

    fixture = _discover_fixtures(tmp_path)[0]

    phase_inputs = _phase_inputs(fixture.run_record)
    assert phase_inputs["dev"] is not phase_inputs["review"]
    assert phase_inputs["dev"]["file_list"] is not phase_inputs["review"]["file_list"]

    phase_inputs["dev"]["file_list"].append("src/extra.py")
    assert phase_inputs["review"]["file_list"] == ["src/api.py", "tests/test_api.py"]


def test_run_prior_run_replay_omits_fence_probes_without_judgment_configuration(
    tmp_path: Path,
) -> None:
    corpus_root = tmp_path / "corpus"
    corpus_root.mkdir()
    base_ref = _init_repo(corpus_root)
    _write_fixture(
        corpus_root,
        "aaa111",
        story_slug="issue-a",
        story_name="Earlier parser fix",
        story_text="Parser bug in demo flow",
        started_at="2026-08-01T00:00:00+00:00",
        finished_at="2026-08-01T00:10:00+00:00",
        generated_at="2026-08-01T00:10:00+00:00",
        base_ref=base_ref,
        claims=[],
    )
    _write_fixture(
        corpus_root,
        "bbb222",
        story_slug="issue-b",
        story_name="Replay target",
        story_text="Parser bug in replay target",
        started_at="2026-08-02T00:00:00+00:00",
        finished_at="2026-08-02T00:10:00+00:00",
        generated_at="2026-08-02T00:10:00+00:00",
        base_ref=base_ref,
        claims=[],
    )
    judgments_path = tmp_path / "judgments.yaml"
    judgments_path.write_text(
        yaml.safe_dump(
            {
                "corpora": {
                    "demo": {
                        "claims": [],
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    report = run_prior_run_replay([CorpusSpec("demo", corpus_root)], judgments_path=judgments_path)

    assert report["corpora"][0]["fence_probes"] == []


def test_run_prior_run_replay_fence_probe_reports_primary_and_expanded_probe_results(
    tmp_path: Path,
) -> None:
    corpus_root = tmp_path / "corpus"
    corpus_root.mkdir()
    base_ref = _init_repo(corpus_root)
    for run_id, stamp in [
        ("aaa111", "2026-08-01T00:10:00+00:00"),
        ("bbb222", "2026-08-01T01:10:00+00:00"),
        ("ccc333", "2026-08-01T02:10:00+00:00"),
        ("ddd444", "2026-08-01T03:10:00+00:00"),
    ]:
        _write_fixture(
            corpus_root,
            run_id,
            story_slug=f"issue-{run_id}",
            story_name=f"Earlier parser fix {run_id}",
            story_text="Parser bug in demo flow",
            started_at=stamp.replace("10:00+00:00", "00:00+00:00"),
            finished_at=stamp,
            generated_at=stamp,
            base_ref=base_ref,
            claims=[],
            changed_files=["src/parser.py"],
        )
    judgments_path = tmp_path / "judgments.yaml"
    judgments_path.write_text(
        yaml.safe_dump(
            {
                "corpora": {
                    "demo": {
                        "fence_probes": [{"run_ids": ["aaa111", "ddd444"]}],
                        "claims": [],
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    report = run_prior_run_replay([CorpusSpec("demo", corpus_root)], judgments_path=judgments_path)

    probe = report["corpora"][0]["fence_probes"][0]
    assert probe["co_surfaced"] is False
    assert probe["co_surfaced_in_expanded_probe"] is True
    assert any(
        (not detail["offered"]) and detail["offered_in_expanded_probe"]
        for detail in probe["matched"].values()
    )
    assert probe["diagnostic"]["selection_limit"] == 3
    assert probe["diagnostic"]["candidate_cap_pressure"] == 1


def test_replay_review_rendering_matches_selector_section_caps(tmp_path: Path) -> None:
    base_ref = _init_repo(tmp_path)
    recurring = [
        {"summary": f"Recurring finding {index}", "finding_key": f"r{index}"}
        for index in range(1, 3)
    ]
    resolved = [
        {"summary": f"Resolved finding {index}", "finding_key": f"f{index}"}
        for index in range(1, 6)
    ]
    observations = ["Observation 1", "Observation 2"]
    _write_fixture(
        tmp_path,
        "aaa111",
        story_slug="issue-a",
        story_name="Earlier review-heavy fix",
        story_text="Review-heavy parser bug",
        started_at="2026-08-01T00:00:00+00:00",
        finished_at="2026-08-01T00:10:00+00:00",
        generated_at="2026-08-01T00:10:00+00:00",
        base_ref=base_ref,
        claims=[],
        review_insights={
            "recurring_findings": recurring,
            "resolved_findings": resolved,
            "observations": observations,
        },
    )
    _write_fixture(
        tmp_path,
        "bbb222",
        story_slug="issue-b",
        story_name="Replay target",
        story_text="Review-heavy parser bug",
        started_at="2026-08-02T00:00:00+00:00",
        finished_at="2026-08-02T00:10:00+00:00",
        generated_at="2026-08-02T00:10:00+00:00",
        base_ref=base_ref,
        claims=[],
    )

    fixtures = _discover_fixtures(tmp_path)
    replay_fixture = next(item for item in fixtures if item.run_id == "bbb222")
    judgments = {}
    for claim in [
        "Recurring finding 1",
        "Recurring finding 2",
        "Resolved finding 1",
        "Resolved finding 2",
        "Resolved finding 3",
        "Resolved finding 4",
        "Resolved finding 5",
        "Observation 1",
        "Observation 2",
    ]:
        effect = "verification" if claim == "Observation 2" else "none"
        judgments[_judgment_key("demo", "bbb222", "review", "aaa111", claim)] = _judgment(
            "demo", "bbb222", "review", "aaa111", claim, effect
        )

    report = _replay_story(CorpusSpec("demo", tmp_path), replay_fixture, fixtures, judgments)

    review = next(item for item in report["phase_replays"] if item["phase"] == "review")
    candidate = review["candidates"][0]
    rendered_claims = {item["claim"] for item in candidate["claims"] if item["rendered"]}
    assert "Observation 2" in rendered_claims
    assert review["metrics"]["useful_claim_cap_truncation"] is False


def test_run_prior_run_replay_rejects_subset_replay_run_ids(tmp_path: Path) -> None:
    corpus_root = tmp_path / "corpus"
    corpus_root.mkdir()
    base_ref = _init_repo(corpus_root)
    _write_fixture(
        corpus_root,
        "aaa111",
        story_slug="issue-a",
        story_name="Earlier parser fix",
        story_text="Parser bug in demo flow",
        started_at="2026-08-01T00:00:00+00:00",
        finished_at="2026-08-01T00:10:00+00:00",
        generated_at="2026-08-01T00:10:00+00:00",
        base_ref=base_ref,
        claims=[],
    )
    _write_fixture(
        corpus_root,
        "bbb222",
        story_slug="issue-b",
        story_name="Replay target",
        story_text="Parser bug in replay target",
        started_at="2026-08-02T00:00:00+00:00",
        finished_at="2026-08-02T00:10:00+00:00",
        generated_at="2026-08-02T00:10:00+00:00",
        base_ref=base_ref,
        claims=[],
    )
    judgments_path = tmp_path / "judgments.yaml"
    judgments_path.write_text(
        yaml.safe_dump(
            {
                "corpora": {
                    "demo": {
                        "replay_run_ids": ["bbb222"],
                        "claims": [],
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    try:
        run_prior_run_replay([CorpusSpec("demo", corpus_root)], judgments_path=judgments_path)
    except PriorRunReplayError as exc:
        assert "must cover every available completed story" in str(exc)
    else:
        raise AssertionError("expected subset replay_run_ids to fail")


def test_replay_reports_candidate_and_claim_cap_pressure(tmp_path: Path) -> None:
    base_ref = _init_repo(tmp_path)
    _write_fixture(
        tmp_path,
        "aaa111",
        story_slug="issue-a",
        story_name="Earlier parser fix A",
        story_text="Parser bug A",
        started_at="2026-08-01T00:00:00+00:00",
        finished_at="2026-08-01T00:10:00+00:00",
        generated_at="2026-08-01T00:10:00+00:00",
        base_ref=base_ref,
        claims=["Overflow useful claim"],
    )
    for run_id, stamp in [
        ("bbb222", "2026-08-01T01:10:00+00:00"),
        ("ccc333", "2026-08-01T02:10:00+00:00"),
    ]:
        _write_fixture(
            tmp_path,
            run_id,
            story_slug=f"issue-{run_id}",
            story_name=f"Earlier parser fix {run_id}",
            story_text="Parser bug peer",
            started_at=stamp.replace("10:00+00:00", "00:00+00:00"),
            finished_at=stamp,
            generated_at=stamp,
            base_ref=base_ref,
            claims=(
                [f"B claim {index}" for index in range(1, 7)]
                if run_id == "bbb222"
                else [f"{run_id} claim"]
            ),
        )
    _write_fixture(
        tmp_path,
        "ddd444",
        story_slug="issue-d",
        story_name="Earlier parser fix D",
        story_text="Parser bug peer",
        started_at="2026-08-01T03:00:00+00:00",
        finished_at="2026-08-01T03:10:00+00:00",
        generated_at="2026-08-01T03:10:00+00:00",
        base_ref=base_ref,
        claims=["ddd444 claim"],
    )
    _write_fixture(
        tmp_path,
        "zzz999",
        story_slug="issue-z",
        story_name="Replay target",
        story_text="Parser bug in replay target",
        started_at="2026-08-02T00:00:00+00:00",
        finished_at="2026-08-02T00:10:00+00:00",
        generated_at="2026-08-02T00:10:00+00:00",
        base_ref=base_ref,
        claims=[],
    )

    fixtures = _discover_fixtures(tmp_path)
    replay_fixture = next(item for item in fixtures if item.run_id == "zzz999")
    judgments = {}
    judgments[_judgment_key("demo", "zzz999", "plan", "aaa111", "Overflow useful claim")] = (
        _judgment(
            "demo",
            "zzz999",
            "plan",
            "aaa111",
            "Overflow useful claim",
            "plan",
        )
    )
    for claim in [f"B claim {index}" for index in range(1, 7)]:
        effect = "plan" if claim == "B claim 6" else "none"
        key = _judgment_key("demo", "zzz999", "plan", "bbb222", claim)
        judgments[key] = _judgment("demo", "zzz999", "plan", "bbb222", claim, effect)
        judgments[_judgment_key("demo", "zzz999", "dev", "bbb222", claim)] = _judgment(
            "demo", "zzz999", "dev", "bbb222", claim, "none"
        )
    for run_id, claim, effect in [
        ("ccc333", "ccc333 claim", "none"),
        ("ddd444", "ddd444 claim", "none"),
    ]:
        key = _judgment_key("demo", "zzz999", "plan", run_id, claim)
        judgments[key] = _judgment("demo", "zzz999", "plan", run_id, claim, effect)
        judgments[_judgment_key("demo", "zzz999", "dev", run_id, claim)] = _judgment(
            "demo", "zzz999", "dev", run_id, claim, "none"
        )
    judgments[_judgment_key("demo", "zzz999", "dev", "aaa111", "Overflow useful claim")] = (
        _judgment(
            "demo",
            "zzz999",
            "dev",
            "aaa111",
            "Overflow useful claim",
            "none",
        )
    )

    report = _replay_story(CorpusSpec("demo", tmp_path), replay_fixture, fixtures, judgments)

    plan = next(item for item in report["phase_replays"] if item["phase"] == "plan")
    assert plan["diagnostic"]["high_limit_candidate_count"] == 4
    assert plan["diagnostic"]["candidate_cap_pressure"] == 1
    assert plan["metrics"]["useful_candidate_cap_truncation"] is True
    assert plan["metrics"]["useful_claim_cap_truncation"] is True
    assert any(item["reason"] == "below_selection_cap(3)" for item in plan["excluded"])
    overflow = next(item for item in plan["candidates"] if item["run_id"] == "aaa111")
    assert overflow["overflowed_selection_cap"] is True


def test_aggregate_metrics_keep_qualifying_signals_per_phase() -> None:
    metrics = _aggregate_corpus_metrics(
        [
            {
                "run_id": "story-1",
                "phase_replays": [
                    {
                        "phase": "plan",
                        "candidates": [
                            {
                                "run_id": "prior-1",
                                "qualifying_signal": "file_overlap(src/api.py)",
                                "overflowed_selection_cap": False,
                            }
                        ],
                        "metrics": {
                            "useful_candidate_cap_truncation": False,
                            "useful_claim_cap_truncation": False,
                        },
                    },
                    {
                        "phase": "dev",
                        "candidates": [
                            {
                                "run_id": "prior-1",
                                "qualifying_signal": "story_match",
                                "overflowed_selection_cap": False,
                            }
                        ],
                        "metrics": {
                            "useful_candidate_cap_truncation": False,
                            "useful_claim_cap_truncation": False,
                        },
                    },
                    {
                        "phase": "review",
                        "candidates": [
                            {
                                "run_id": "prior-1",
                                "qualifying_signal": "story_match",
                                "overflowed_selection_cap": False,
                            }
                        ],
                        "metrics": {
                            "useful_candidate_cap_truncation": False,
                            "useful_claim_cap_truncation": False,
                        },
                    },
                ],
            }
        ]
    )

    assert metrics["qualifying_signal_counts_by_phase"] == {
        "plan": {"file_overlap(src/api.py)": 1},
        "dev": {"story_match": 1},
        "review": {"story_match": 1},
    }
    assert metrics["qualifying_signal_counts"] == {
        "file_overlap(src/api.py)": 1,
        "story_match": 2,
    }


def test_replay_fails_when_claim_judgments_are_missing(tmp_path: Path) -> None:
    base_ref = _init_repo(tmp_path)
    _write_fixture(
        tmp_path,
        "aaa111",
        story_slug="issue-a",
        story_name="Earlier parser fix",
        story_text="Parser bug in demo flow",
        started_at="2026-08-01T00:00:00+00:00",
        finished_at="2026-08-01T00:10:00+00:00",
        generated_at="2026-08-01T00:10:00+00:00",
        base_ref=base_ref,
        claims=["Earlier lesson"],
    )
    _write_fixture(
        tmp_path,
        "bbb222",
        story_slug="issue-b",
        story_name="Replay target",
        story_text="Parser bug in replay target",
        started_at="2026-08-02T00:00:00+00:00",
        finished_at="2026-08-02T00:10:00+00:00",
        generated_at="2026-08-02T00:10:00+00:00",
        base_ref=base_ref,
        claims=[],
    )

    fixtures = _discover_fixtures(tmp_path)
    replay_fixture = next(item for item in fixtures if item.run_id == "bbb222")

    try:
        _replay_story(CorpusSpec("demo", tmp_path), replay_fixture, fixtures, {})
    except PriorRunReplayError as exc:
        assert "missing judgment" in str(exc)
    else:
        raise AssertionError("expected replay to fail on missing judgments")


def test_repo_judgments_replay_against_current_theforge_corpus() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    judgment_config = _load_judgments(repo_root / "docs" / "prior-run-replay-judgments.yaml")
    available_run_ids = {fixture.run_id for fixture in _discover_fixtures(repo_root)}
    judged_run_ids = judgment_config.replay_run_ids_by_corpus["theforge"]

    if not judged_run_ids.issubset(available_run_ids):
        missing = sorted(judged_run_ids - available_run_ids)
        raise AssertionError(f"judgment file references missing replay run ids: {missing}")

    if judged_run_ids != available_run_ids:
        return

    report = run_prior_run_replay(
        [CorpusSpec("theforge", repo_root)],
        judgments_path=repo_root / "docs" / "prior-run-replay-judgments.yaml",
    )

    corpus = report["corpora"][0]
    assert corpus["name"] == "theforge"
    assert corpus["story_count"] == corpus["available_story_count"]
