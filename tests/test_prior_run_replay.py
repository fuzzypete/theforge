from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

from theforge.prior_run_replay import (
    CorpusSpec,
    Judgment,
    PriorRunReplayError,
    _claim_hash,
    _discover_fixtures,
    _judgment_key,
    _replay_story,
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
        "changed_files": ["src/demo.py"],
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
        "review_insights": {
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
            "files": [{"path": "src/demo.py", "insertions": 1, "deletions": 0, "binary": False}],
        },
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
        )
    }

    report = _replay_story(CorpusSpec("demo", tmp_path), replay_fixture, fixtures, judgments)

    plan = next(item for item in report["phase_replays"] if item["phase"] == "plan")
    assert plan["status"] == "replayed_missing_file_list"
    assert [candidate["run_id"] for candidate in plan["candidates"]] == ["aaa111"]
    assert "ccc333" not in {candidate["run_id"] for candidate in plan["candidates"]}
    assert not (tmp_path / ".forge" / "knowledge" / "index.yaml").exists()


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
    for run_id, claim, effect in [
        ("ccc333", "ccc333 claim", "none"),
        ("ddd444", "ddd444 claim", "none"),
    ]:
        key = _judgment_key("demo", "zzz999", "plan", run_id, claim)
        judgments[key] = _judgment("demo", "zzz999", "plan", run_id, claim, effect)

    report = _replay_story(CorpusSpec("demo", tmp_path), replay_fixture, fixtures, judgments)

    plan = next(item for item in report["phase_replays"] if item["phase"] == "plan")
    assert plan["diagnostic"]["high_limit_candidate_count"] == 4
    assert plan["diagnostic"]["candidate_cap_pressure"] == 1
    assert plan["metrics"]["useful_candidate_cap_truncation"] is True
    assert plan["metrics"]["useful_claim_cap_truncation"] is True
    assert any(item["reason"] == "below_selection_cap(3)" for item in plan["excluded"])
    overflow = next(item for item in plan["candidates"] if item["run_id"] == "aaa111")
    assert overflow["overflowed_selection_cap"] is True


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
