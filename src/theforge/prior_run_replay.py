"""Historical replay harness for prior-run selection.

This module replays completed summary-backed stories through the existing
``select_prior_runs`` path against a disposable, historically-filtered corpus.
The replay is measurement-only: source corpora are cloned into temporary
repositories, rewound to each story's recorded ``changed_files.base_ref``, and
then populated with only the persisted summaries and authoritative run records
that existed before the replayed story started.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from theforge.coordinator.context_scope import plan_file_list
from theforge.knowledge_index import rebuild_knowledge_index
from theforge.task.prior_run_selector import (
    PriorRunSelection,
    _dev_grounded_claims,
    _evidenced_claims,
    _finding_descriptions,
    _touched_paths,
    select_prior_runs,
)

_DEFAULT_PHASE_LIMIT = 3
_HIGH_LIMIT = 999
_JUDGMENT_EFFECTS = frozenset({"plan", "implementation", "verification", "none"})
_PHASES = ("plan", "dev", "review")
_FENCE_PROBE_RUN_IDS = ("1a6b6e18d232", "73d7de156730")


class PriorRunReplayError(RuntimeError):
    """Raised when replay inputs are invalid or incomplete."""


@dataclass(frozen=True)
class CorpusSpec:
    name: str
    root: Path


@dataclass(frozen=True)
class Judgment:
    corpus: str
    replay_run_id: str
    phase: str
    prior_run_id: str
    claim_hash: str
    claim: str
    effect: str
    rationale: str


@dataclass(frozen=True)
class JudgmentConfig:
    replay_run_ids_by_corpus: dict[str, frozenset[str]]
    judgments: dict[tuple[str, str, str, str, str], Judgment]


@dataclass(frozen=True)
class SummaryFixture:
    run_id: str
    summary_path: Path
    run_record_path: Path
    summary: dict[str, Any]
    run_record: dict[str, Any]
    generated_at: dt.datetime
    started_at: dt.datetime
    base_ref: str
    story_text: str
    story_name: str
    story_slug: str


def run_prior_run_replay(
    corpora: list[CorpusSpec],
    *,
    judgments_path: Path,
) -> dict[str, Any]:
    """Run replay across named corpora and return a structured payload."""
    judgment_config = _load_judgments(judgments_path)
    corpus_reports = [_replay_corpus(spec, judgment_config) for spec in corpora]
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "judgments_path": str(judgments_path),
        "corpora": corpus_reports,
        "aggregate": _aggregate_report(corpus_reports),
    }


def _replay_corpus(spec: CorpusSpec, judgment_config: JudgmentConfig) -> dict:
    fixtures = _discover_fixtures(spec.root)
    replay_ids = judgment_config.replay_run_ids_by_corpus.get(spec.name)
    replay_fixtures = (
        [fixture for fixture in fixtures if fixture.run_id in replay_ids]
        if replay_ids
        else fixtures
    )
    story_reports = [
        _replay_story(spec, fixture, fixtures, judgment_config.judgments)
        for fixture in replay_fixtures
    ]
    return {
        "name": spec.name,
        "root": str(spec.root),
        "story_count": len(replay_fixtures),
        "available_story_count": len(fixtures),
        "stories": story_reports,
        "fence_probes": _run_fence_probes(spec, fixtures),
        "metrics": _aggregate_corpus_metrics(story_reports),
    }


def _discover_fixtures(corpus_root: Path) -> list[SummaryFixture]:
    fixtures: list[SummaryFixture] = []
    summaries_root = corpus_root / ".forge" / "knowledge" / "summaries"
    for summary_path in sorted(summaries_root.glob("*.yaml")):
        summary = _load_yaml(summary_path)
        if not isinstance(summary, dict):
            continue
        run_id = _text(summary.get("run_id")) or summary_path.stem
        run_record_rel = _text(summary.get("authoritative_run_record"))
        run_record_path = (
            corpus_root / run_record_rel
            if run_record_rel
            else (corpus_root / ".forge" / "audits" / "runs" / f"{run_id}.json")
        )
        run_record = _load_json(run_record_path)
        if not isinstance(run_record, dict):
            continue
        outcome = run_record.get("outcome")
        if not isinstance(outcome, dict) or outcome.get("success") is not True:
            continue
        generated_at = _parse_timestamp(summary.get("generated_at"))
        started_at = _parse_timestamp((run_record.get("timing") or {}).get("started_at"))
        changed_files = run_record.get("changed_files")
        base_ref = _text(changed_files.get("base_ref")) if isinstance(changed_files, dict) else ""
        task = run_record.get("task")
        if (
            generated_at is None
            or started_at is None
            or not base_ref
            or not isinstance(task, dict)
            or not _text(task.get("story_text"))
        ):
            continue
        fixtures.append(
            SummaryFixture(
                run_id=run_id,
                summary_path=summary_path,
                run_record_path=run_record_path,
                summary=summary,
                run_record=run_record,
                generated_at=generated_at,
                started_at=started_at,
                base_ref=base_ref,
                story_text=_text(task.get("story_text")),
                story_name=_text((summary.get("story") or {}).get("name"))
                or _text(task.get("name")),
                story_slug=_text((summary.get("story") or {}).get("slug"))
                or _text(task.get("slug")),
            )
        )
    fixtures.sort(key=lambda item: (item.started_at, item.run_id))
    return fixtures


def _replay_story(
    spec: CorpusSpec,
    fixture: SummaryFixture,
    fixtures: list[SummaryFixture],
    judgments: dict[tuple[str, str, str, str, str], Judgment],
) -> dict[str, Any]:
    historical_fixtures = [
        candidate
        for candidate in fixtures
        if candidate.generated_at < fixture.started_at and candidate.run_id != fixture.run_id
    ]
    phases = _phase_inputs(fixture.run_record)
    phase_reports: list[dict[str, Any]] = []
    with _materialized_replay_corpus(
        spec.root, fixture.base_ref, historical_fixtures
    ) as replay_root:
        rebuild_knowledge_index(replay_root)
        for phase in _PHASES:
            phase_reports.append(
                _replay_phase(
                    spec.name,
                    replay_root,
                    fixture,
                    phase=phase,
                    file_list=phases[phase]["file_list"],
                    recoverable=phases[phase]["recoverable"],
                    recovery_note=phases[phase]["note"],
                    judgments=judgments,
                )
            )
    return {
        "run_id": fixture.run_id,
        "story_slug": fixture.story_slug,
        "story_name": fixture.story_name,
        "started_at": fixture.started_at.isoformat(),
        "generated_at": fixture.generated_at.isoformat(),
        "base_ref": fixture.base_ref,
        "eligible_prior_summary_count": len(historical_fixtures),
        "phase_replays": phase_reports,
    }


def _phase_inputs(run_record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    note = (
        "audit records do not persist preflight_likely_files; "
        "replay runs without file overlap input"
    )
    plan_data = {
        "file_list": None,
        "recoverable": False,
        "note": note,
    }
    structured = run_record.get("plan_structured")
    if isinstance(structured, dict):
        replay_files = plan_file_list(structured) or None
        data = {"file_list": replay_files, "recoverable": True, "note": ""}
    else:
        data = {
            "file_list": None,
            "recoverable": False,
            "note": (
                "audit record does not persist plan_structured; "
                "replay runs without file overlap input"
            ),
        }
    return {"plan": plan_data, "dev": data, "review": copy.deepcopy(data)}


def _replay_phase(
    corpus_name: str,
    replay_root: Path,
    fixture: SummaryFixture,
    *,
    phase: str,
    file_list: list[str] | None,
    recoverable: bool,
    recovery_note: str,
    judgments: dict[tuple[str, str, str, str, str], Judgment],
) -> dict[str, Any]:
    selection = select_prior_runs(
        replay_root,
        phase=phase,
        story_text=fixture.story_text,
        file_list=file_list,
        limit=_DEFAULT_PHASE_LIMIT,
    )
    high_limit = select_prior_runs(
        replay_root,
        phase=phase,
        story_text=fixture.story_text,
        file_list=file_list,
        limit=_HIGH_LIMIT,
    )
    return _phase_report_from_selection(
        corpus_name,
        replay_root,
        fixture.run_id,
        phase=phase,
        file_list=file_list,
        recoverable=recoverable,
        recovery_note=recovery_note,
        selection=selection,
        high_limit=high_limit,
        judgments=judgments,
    )


def _phase_report_from_selection(
    corpus_name: str,
    replay_root: Path,
    replay_run_id: str,
    *,
    phase: str,
    file_list: list[str] | None,
    recoverable: bool,
    recovery_note: str,
    selection: PriorRunSelection,
    high_limit: PriorRunSelection,
    judgments: dict[tuple[str, str, str, str, str], Judgment],
) -> dict[str, Any]:
    touched_files, touched_dirs = _touched_paths(file_list)
    candidate_reports = []
    useful_rendered_claims = 0
    useful_omitted_claims = 0
    for candidate in high_limit.candidates:
        summary = _load_yaml(replay_root / candidate.summary_path)
        claims = _claims_for_phase(
            phase,
            summary if isinstance(summary, dict) else {},
            touched_files=touched_files,
            touched_dirs=touched_dirs,
        )
        rendered_claims = claims[: _render_cap_for_phase(phase)]
        overflowed = candidate.run_id not in {item.run_id for item in selection.candidates}
        claim_reports = []
        for index, claim in enumerate(claims):
            key = _judgment_key(
                corpus_name,
                replay_run_id,
                phase,
                candidate.run_id,
                claim,
            )
            judgment = judgments.get(key)
            if judgment is None:
                raise PriorRunReplayError(
                    "missing judgment for "
                    f"{corpus_name}/{replay_run_id}/{phase}/{candidate.run_id}/{_claim_hash(claim)}"
                )
            rendered = index < len(rendered_claims) and claim == rendered_claims[index]
            useful = judgment.effect != "none"
            if rendered and useful and not overflowed:
                useful_rendered_claims += 1
            if (not rendered or overflowed) and useful:
                useful_omitted_claims += 1
            claim_reports.append(
                {
                    "claim": claim,
                    "claim_hash": _claim_hash(claim),
                    "rendered": rendered and not overflowed,
                    "effect": judgment.effect,
                    "rationale": judgment.rationale,
                }
            )
        candidate_reports.append(
            {
                "run_id": candidate.run_id,
                "score": candidate.score,
                "reason": candidate.reason,
                "qualifying_signal": _first_reason(candidate.reason),
                "content": candidate.content if not overflowed else "",
                "summary_path": candidate.summary_path,
                "overflowed_selection_cap": overflowed,
                "claims": claim_reports,
            }
        )

    return {
        "phase": phase,
        "status": "replayed" if recoverable else "replayed_missing_file_list",
        "recovery_note": recovery_note,
        "file_list": file_list,
        "entry_count": selection.entry_count,
        "index_state": selection.index_state,
        "phase_eligible": selection.phase_eligible,
        "rendering_mode": selection.rendering_mode,
        "candidates": candidate_reports,
        "excluded": [
            {
                "run_id": item.run_id,
                "reason": item.reason,
                "admissibility_excluded": item.admissibility_excluded,
            }
            for item in selection.excluded
        ],
        "diagnostic": {
            "high_limit_candidate_count": len(high_limit.candidates),
            "candidate_cap_pressure": max(
                0, len(high_limit.candidates) - len(selection.candidates)
            ),
        },
        "metrics": {
            "useful_rendered_claims": useful_rendered_claims,
            "useful_omitted_claims": useful_omitted_claims,
            "useful_candidate_cap_truncation": any(
                candidate["overflowed_selection_cap"]
                and any(claim["effect"] != "none" for claim in candidate["claims"])
                for candidate in candidate_reports
            ),
            "useful_claim_cap_truncation": any(
                (not candidate["overflowed_selection_cap"])
                and any(
                    (not claim["rendered"]) and claim["effect"] != "none"
                    for claim in candidate["claims"]
                )
                for candidate in candidate_reports
            ),
        },
    }


def _claims_for_phase(
    phase: str,
    summary: dict[str, Any],
    *,
    touched_files: set[str],
    touched_dirs: set[str],
) -> list[str]:
    if phase == "plan":
        return _evidenced_claims(summary)
    if phase == "dev":
        return _dev_grounded_claims(
            summary,
            touched_files=touched_files,
            touched_dirs=touched_dirs,
        )
    if phase == "review":
        insights = summary.get("review_insights")
        if not isinstance(insights, dict):
            return []
        claims: list[str] = []
        claims.extend(_finding_descriptions(insights.get("recurring_findings")))
        claims.extend(_finding_descriptions(insights.get("resolved_findings")))
        claims.extend(_text(item) for item in insights.get("observations", []) if _text(item))
        return claims
    return []


def _render_cap_for_phase(phase: str) -> int:
    return 5


def _aggregate_corpus_metrics(story_reports: list[dict[str, Any]]) -> dict[str, Any]:
    qualifier_counts: Counter[str] = Counter()
    offered_story_count = 0
    replayed_phase_count = 0
    candidate_cap_useful = 0
    claim_cap_useful = 0
    seen_story_run: set[tuple[str, str]] = set()
    for story in story_reports:
        story_offered = False
        for phase in story["phase_replays"]:
            replayed_phase_count += 1
            if phase["candidates"]:
                story_offered = True
            if phase["metrics"]["useful_candidate_cap_truncation"]:
                candidate_cap_useful += 1
            if phase["metrics"]["useful_claim_cap_truncation"]:
                claim_cap_useful += 1
            for candidate in phase["candidates"]:
                if candidate["overflowed_selection_cap"]:
                    continue
                key = (story["run_id"], candidate["run_id"])
                if key in seen_story_run:
                    continue
                seen_story_run.add(key)
                qualifier_counts[candidate["qualifying_signal"]] += 1
        if story_offered:
            offered_story_count += 1
    return {
        "replayed_phase_count": replayed_phase_count,
        "stories_with_candidates": offered_story_count,
        "qualifying_signal_counts": dict(sorted(qualifier_counts.items())),
        "candidate_cap_useful_phase_count": candidate_cap_useful,
        "claim_cap_useful_phase_count": claim_cap_useful,
    }


def _aggregate_report(corpora: list[dict[str, Any]]) -> dict[str, Any]:
    total_stories = sum(corpus["story_count"] for corpus in corpora)
    total_offered = sum(corpus["metrics"]["stories_with_candidates"] for corpus in corpora)
    qualifier_counts: Counter[str] = Counter()
    candidate_cap_useful = 0
    claim_cap_useful = 0
    for corpus in corpora:
        qualifier_counts.update(corpus["metrics"]["qualifying_signal_counts"])
        candidate_cap_useful += corpus["metrics"]["candidate_cap_useful_phase_count"]
        claim_cap_useful += corpus["metrics"]["claim_cap_useful_phase_count"]
    return {
        "story_count": total_stories,
        "stories_with_candidates": total_offered,
        "qualifying_signal_counts": dict(sorted(qualifier_counts.items())),
        "candidate_cap_useful_phase_count": candidate_cap_useful,
        "claim_cap_useful_phase_count": claim_cap_useful,
    }


def _run_fence_probes(spec: CorpusSpec, fixtures: list[SummaryFixture]) -> list[dict[str, Any]]:
    relevant = {
        fixture.run_id: fixture for fixture in fixtures if fixture.run_id in _FENCE_PROBE_RUN_IDS
    }
    if len(relevant) != len(_FENCE_PROBE_RUN_IDS):
        return []
    all_fixtures = sorted(fixtures, key=lambda item: (item.generated_at, item.run_id))
    with _materialized_replay_corpus(
        spec.root,
        max(relevant.values(), key=lambda item: item.generated_at).base_ref,
        all_fixtures,
    ) as replay_root:
        rebuild_knowledge_index(replay_root)
        probes = []
        for target in relevant.values():
            summary_paths = _summary_paths_for_probe(target)
            selection = select_prior_runs(
                replay_root,
                phase="dev",
                story_text="",
                file_list=summary_paths,
                limit=_HIGH_LIMIT,
            )
            offered = {candidate.run_id: candidate for candidate in selection.candidates}
            probes.append(
                {
                    "probe_run_id": target.run_id,
                    "file_list": summary_paths,
                    "matched": {
                        run_id: {
                            "offered": run_id in offered,
                            "reason": offered[run_id].reason if run_id in offered else "",
                        }
                        for run_id in _FENCE_PROBE_RUN_IDS
                    },
                    "co_surfaced": all(run_id in offered for run_id in _FENCE_PROBE_RUN_IDS),
                }
            )
        return probes


def _summary_paths_for_probe(fixture: SummaryFixture) -> list[str]:
    changed_files = fixture.summary.get("changed_files")
    if isinstance(changed_files, list) and changed_files:
        return [str(changed_files[0])]
    changed = fixture.run_record.get("changed_files")
    files = changed.get("files") if isinstance(changed, dict) else None
    if isinstance(files, list) and files:
        first = files[0]
        if isinstance(first, dict) and _text(first.get("path")):
            return [_text(first.get("path"))]
    return []


@dataclass
class _ReplayCorpusMaterialization:
    path: Path

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def _materialized_replay_corpus(
    source_root: Path,
    base_ref: str,
    eligible_fixtures: list[SummaryFixture],
) -> _ReplayCorpusMaterialization:
    temp_root = Path(tempfile.mkdtemp(prefix="forge-prior-run-replay-"))
    _git(source_root.parent, "clone", "--shared", str(source_root), str(temp_root))
    _git(temp_root, "checkout", "--detach", base_ref)

    summaries_root = temp_root / ".forge" / "knowledge" / "summaries"
    runs_root = temp_root / ".forge" / "audits" / "runs"
    if summaries_root.exists():
        shutil.rmtree(summaries_root)
    if runs_root.exists():
        shutil.rmtree(runs_root)
    summaries_root.mkdir(parents=True, exist_ok=True)
    runs_root.mkdir(parents=True, exist_ok=True)

    for fixture in eligible_fixtures:
        target_summary = temp_root / fixture.summary_path.relative_to(source_root)
        target_summary.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fixture.summary_path, target_summary)
        target_run = temp_root / fixture.run_record_path.relative_to(source_root)
        target_run.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fixture.run_record_path, target_run)
    return _ReplayCorpusMaterialization(temp_root)


def _git(cwd: Path, *args: str) -> None:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        raise PriorRunReplayError(f"git {' '.join(args)} failed: {stderr}")


def _load_judgments(path: Path) -> JudgmentConfig:
    raw = _load_yaml(path)
    if not isinstance(raw, dict):
        raise PriorRunReplayError(f"judgments file must be a mapping: {path}")
    corpora = raw.get("corpora")
    if not isinstance(corpora, dict):
        raise PriorRunReplayError("judgments file must contain a top-level 'corpora' mapping")

    replay_run_ids_by_corpus: dict[str, frozenset[str]] = {}
    loaded: dict[tuple[str, str, str, str, str], Judgment] = {}
    for corpus_name, payload in corpora.items():
        if not isinstance(payload, dict):
            raise PriorRunReplayError(f"judgments corpus {corpus_name!r} must be a mapping")
        replay_run_ids = payload.get("replay_run_ids")
        if replay_run_ids is not None:
            if not isinstance(replay_run_ids, list) or any(
                not _text(item) for item in replay_run_ids
            ):
                raise PriorRunReplayError(
                    f"judgments corpus {corpus_name!r} replay_run_ids must be a list of strings"
                )
            replay_run_ids_by_corpus[str(corpus_name)] = frozenset(
                _text(item) for item in replay_run_ids
            )
        claims = payload.get("claims")
        if not isinstance(claims, list):
            raise PriorRunReplayError(
                f"judgments corpus {corpus_name!r} must contain a claims list"
            )
        for item in claims:
            if not isinstance(item, dict):
                raise PriorRunReplayError(f"judgment entries for {corpus_name!r} must be mappings")
            claim = _text(item.get("claim"))
            claim_hash = _text(item.get("claim_hash"))
            if not claim or not claim_hash or claim_hash != _claim_hash(claim):
                raise PriorRunReplayError(
                    f"judgment claim hash mismatch for corpus {corpus_name!r}: {claim!r}"
                )
            effect = _text(item.get("effect"))
            if effect not in _JUDGMENT_EFFECTS:
                raise PriorRunReplayError(
                    f"judgment effect must be one of {sorted(_JUDGMENT_EFFECTS)}"
                )
            judgment = Judgment(
                corpus=str(corpus_name),
                replay_run_id=_text(item.get("replay_run_id")),
                phase=_text(item.get("phase")),
                prior_run_id=_text(item.get("prior_run_id")),
                claim_hash=claim_hash,
                claim=claim,
                effect=effect,
                rationale=_text(item.get("rationale")),
            )
            if not all(
                [
                    judgment.replay_run_id,
                    judgment.phase,
                    judgment.prior_run_id,
                    judgment.rationale,
                ]
            ):
                raise PriorRunReplayError(f"incomplete judgment entry for corpus {corpus_name!r}")
            key = _judgment_key(
                judgment.corpus,
                judgment.replay_run_id,
                judgment.phase,
                judgment.prior_run_id,
                judgment.claim,
            )
            if key in loaded:
                raise PriorRunReplayError(
                    "duplicate judgment for "
                    f"{judgment.corpus}/{judgment.replay_run_id}/{judgment.phase}/"
                    f"{judgment.prior_run_id}/{judgment.claim_hash}"
                )
            loaded[key] = judgment
    return JudgmentConfig(replay_run_ids_by_corpus=replay_run_ids_by_corpus, judgments=loaded)


def _judgment_key(
    corpus: str,
    replay_run_id: str,
    phase: str,
    prior_run_id: str,
    claim: str,
) -> tuple[str, str, str, str, str]:
    return (corpus, replay_run_id, phase, prior_run_id, _claim_hash(claim))


def _claim_hash(claim: str) -> str:
    return hashlib.sha256(claim.strip().encode("utf-8")).hexdigest()[:12]


def _first_reason(reason: str) -> str:
    return reason.split(", ", 1)[0] if reason else ""


def _load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _parse_timestamp(value: Any) -> dt.datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""
