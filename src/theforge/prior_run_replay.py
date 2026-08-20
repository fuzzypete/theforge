"""Historical replay harness for prior-run selection.

This module replays completed summary-backed stories through the existing
``select_prior_runs`` path against a disposable, historically-filtered corpus.
The replay is measurement-only: source corpora are cloned into temporary
repositories, rewound to each story's recorded ``changed_files.base_ref``, and
then populated with only the persisted summaries and authoritative run records
that existed before the replayed story started.
"""

from __future__ import annotations

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
    fence_probes_by_corpus: dict[str, tuple[tuple[str, ...], ...]]


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
    available_run_ids = {fixture.run_id for fixture in fixtures}
    if replay_ids is not None and replay_ids != available_run_ids:
        raise PriorRunReplayError(
            "judgments replay_run_ids must cover every available completed story for "
            f"{spec.name}: expected {len(available_run_ids)} run(s), "
            f"got {len(replay_ids)}"
        )
    replay_fixtures = fixtures
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
        "fence_probes": _run_fence_probes(
            spec,
            fixtures,
            probe_groups=judgment_config.fence_probes_by_corpus.get(spec.name, ()),
        ),
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
    changed_files = _persisted_changed_files(run_record)
    phase_plan = ((run_record.get("phases") or {}).get("plan")) or {}
    structured = phase_plan.get("plan_structured") if isinstance(phase_plan, dict) else None
    plan_files = plan_file_list(structured) if isinstance(structured, dict) else None

    plan_scope = _recovered_phase_input(
        primary=plan_files,
        primary_note="recovered from phases.plan.plan_structured",
        fallback=changed_files,
        fallback_note="plan_structured missing; fell back to persisted changed_files",
        missing_note=(
            "audit record persists neither phases.plan.plan_structured nor changed_files; "
            "replay runs without file overlap input"
        ),
    )
    later_phase_scope = _recovered_phase_input(
        primary=plan_files,
        primary_note="recovered from phases.plan.plan_structured",
        fallback=None,
        fallback_note="",
        missing_note=(
            "audit record persists no phases.plan.plan_structured; "
            "dev/review replay runs without file overlap input"
        ),
    )
    return {
        "plan": plan_scope,
        "dev": later_phase_scope,
        "review": _recovered_phase_input(
            primary=plan_files,
            primary_note="recovered from phases.plan.plan_structured",
            fallback=None,
            fallback_note="",
            missing_note=(
                "audit record persists no phases.plan.plan_structured; "
                "dev/review replay runs without file overlap input"
            ),
        ),
    }


def _recovered_phase_input(
    *,
    primary: list[str] | None,
    primary_note: str,
    fallback: list[str] | None,
    fallback_note: str,
    missing_note: str,
) -> dict[str, Any]:
    if primary:
        return {
            "file_list": list(primary),
            "recoverable": True,
            "note": primary_note,
        }
    if fallback:
        return {
            "file_list": list(fallback),
            "recoverable": True,
            "note": fallback_note,
        }
    return {"file_list": None, "recoverable": False, "note": missing_note}


def _persisted_changed_files(run_record: dict[str, Any]) -> list[str] | None:
    changed = run_record.get("changed_files")
    files = changed.get("files") if isinstance(changed, dict) else None
    paths = [
        _text(item.get("path"))
        for item in files or []
        if isinstance(item, dict) and _text(item.get("path"))
    ]
    return paths or None


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
        summary_data = summary if isinstance(summary, dict) else {}
        claims = _claims_for_phase(
            phase,
            summary_data,
            touched_files=touched_files,
            touched_dirs=touched_dirs,
        )
        rendered_claims = _rendered_claims_for_phase(
            phase,
            summary_data,
            touched_files=touched_files,
            touched_dirs=touched_dirs,
        )
        overflowed = candidate.run_id not in {item.run_id for item in selection.candidates}
        claim_reports = []
        rendered_counts = Counter(rendered_claims)
        for claim in claims:
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
            rendered = rendered_counts[claim] > 0
            if rendered:
                rendered_counts[claim] -= 1
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
        rendered: list[str] = []
        for section in _review_claim_sections(summary):
            rendered.extend(section)
        return rendered
    return []


def _rendered_claims_for_phase(
    phase: str,
    summary: dict[str, Any],
    *,
    touched_files: set[str],
    touched_dirs: set[str],
) -> list[str]:
    if phase == "plan":
        return _evidenced_claims(summary)[:5]
    if phase == "dev":
        return _dev_grounded_claims(
            summary,
            touched_files=touched_files,
            touched_dirs=touched_dirs,
        )[:5]
    if phase == "review":
        rendered: list[str] = []
        for section in _review_claim_sections(summary):
            rendered.extend(section[:5])
        return rendered
    return []


def _review_claim_sections(summary: dict[str, Any]) -> list[list[str]]:
    insights = summary.get("review_insights")
    if not isinstance(insights, dict):
        return []
    return [
        _finding_descriptions(insights.get("recurring_findings")),
        _finding_descriptions(insights.get("resolved_findings")),
        [_text(item) for item in insights.get("observations", []) if _text(item)],
    ]


def _aggregate_corpus_metrics(story_reports: list[dict[str, Any]]) -> dict[str, Any]:
    qualifier_counts: Counter[str] = Counter()
    qualifier_counts_by_phase: dict[str, Counter[str]] = {phase: Counter() for phase in _PHASES}
    offered_story_count = 0
    replayed_phase_count = 0
    candidate_cap_useful = 0
    claim_cap_useful = 0
    for story in story_reports:
        story_offered = False
        for phase in story["phase_replays"]:
            phase_name = _text(phase.get("phase"))
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
                qualifier_counts[candidate["qualifying_signal"]] += 1
                if phase_name in qualifier_counts_by_phase:
                    qualifier_counts_by_phase[phase_name][candidate["qualifying_signal"]] += 1
        if story_offered:
            offered_story_count += 1
    return {
        "replayed_phase_count": replayed_phase_count,
        "stories_with_candidates": offered_story_count,
        "qualifying_signal_counts": dict(sorted(qualifier_counts.items())),
        "qualifying_signal_counts_by_phase": {
            phase: dict(sorted(counts.items()))
            for phase, counts in qualifier_counts_by_phase.items()
        },
        "candidate_cap_useful_phase_count": candidate_cap_useful,
        "claim_cap_useful_phase_count": claim_cap_useful,
    }


def _aggregate_report(corpora: list[dict[str, Any]]) -> dict[str, Any]:
    total_stories = sum(corpus["story_count"] for corpus in corpora)
    total_offered = sum(corpus["metrics"]["stories_with_candidates"] for corpus in corpora)
    qualifier_counts: Counter[str] = Counter()
    qualifier_counts_by_phase: dict[str, Counter[str]] = {phase: Counter() for phase in _PHASES}
    candidate_cap_useful = 0
    claim_cap_useful = 0
    for corpus in corpora:
        qualifier_counts.update(corpus["metrics"]["qualifying_signal_counts"])
        for phase in _PHASES:
            qualifier_counts_by_phase[phase].update(
                corpus["metrics"]["qualifying_signal_counts_by_phase"].get(phase, {})
            )
        candidate_cap_useful += corpus["metrics"]["candidate_cap_useful_phase_count"]
        claim_cap_useful += corpus["metrics"]["claim_cap_useful_phase_count"]
    return {
        "story_count": total_stories,
        "stories_with_candidates": total_offered,
        "qualifying_signal_counts": dict(sorted(qualifier_counts.items())),
        "qualifying_signal_counts_by_phase": {
            phase: dict(sorted(counts.items()))
            for phase, counts in qualifier_counts_by_phase.items()
        },
        "candidate_cap_useful_phase_count": candidate_cap_useful,
        "claim_cap_useful_phase_count": claim_cap_useful,
    }


def _run_fence_probes(
    spec: CorpusSpec,
    fixtures: list[SummaryFixture],
    *,
    probe_groups: tuple[tuple[str, ...], ...],
) -> list[dict[str, Any]]:
    probes: list[dict[str, Any]] = []
    fixture_by_run = {fixture.run_id: fixture for fixture in fixtures}
    all_fixtures = sorted(fixtures, key=lambda item: (item.generated_at, item.run_id))
    for probe_run_ids in probe_groups:
        if not probe_run_ids:
            continue
        resolved = {run_id: fixture_by_run.get(run_id) for run_id in probe_run_ids}
        if any(fixture is None for fixture in resolved.values()):
            continue
        selected = {run_id: fixture for run_id, fixture in resolved.items() if fixture is not None}
        with _materialized_replay_corpus(
            spec.root,
            max(selected.values(), key=lambda item: item.generated_at).base_ref,
            all_fixtures,
        ) as replay_root:
            rebuild_knowledge_index(replay_root)
            for target in selected.values():
                summary_paths = _summary_paths_for_probe(target)
                selection = select_prior_runs(
                    replay_root,
                    phase="dev",
                    story_text="",
                    file_list=summary_paths,
                    limit=_DEFAULT_PHASE_LIMIT,
                )
                high_limit = select_prior_runs(
                    replay_root,
                    phase="dev",
                    story_text="",
                    file_list=summary_paths,
                    limit=_HIGH_LIMIT,
                )
                offered = {candidate.run_id: candidate for candidate in selection.candidates}
                expanded_offered = {
                    candidate.run_id: candidate for candidate in high_limit.candidates
                }
                probes.append(
                    {
                        "probe_run_id": target.run_id,
                        "co_surface_run_ids": list(probe_run_ids),
                        "file_list": summary_paths,
                        "matched": {
                            run_id: {
                                "offered": run_id in offered,
                                "reason": offered[run_id].reason if run_id in offered else "",
                                "offered_in_expanded_probe": run_id in expanded_offered,
                                "expanded_reason": (
                                    expanded_offered[run_id].reason
                                    if run_id in expanded_offered
                                    else ""
                                ),
                            }
                            for run_id in probe_run_ids
                        },
                        "co_surfaced": all(run_id in offered for run_id in probe_run_ids),
                        "co_surfaced_in_expanded_probe": all(
                            run_id in expanded_offered for run_id in probe_run_ids
                        ),
                        "diagnostic": {
                            "selection_limit": _DEFAULT_PHASE_LIMIT,
                            "high_limit_candidate_count": len(high_limit.candidates),
                            "candidate_cap_pressure": max(
                                0, len(high_limit.candidates) - len(selection.candidates)
                            ),
                        },
                    }
                )
    return probes


def _summary_paths_for_probe(fixture: SummaryFixture) -> list[str]:
    changed_files = fixture.summary.get("changed_files")
    if isinstance(changed_files, list):
        paths = [str(path).strip() for path in changed_files if str(path).strip()]
        if paths:
            return paths
    return _persisted_changed_files(fixture.run_record) or []


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
    fence_probes_by_corpus: dict[str, tuple[tuple[str, ...], ...]] = {}
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
        raw_probes = payload.get("fence_probes")
        if raw_probes is not None:
            if not isinstance(raw_probes, list):
                raise PriorRunReplayError(
                    f"judgments corpus {corpus_name!r} fence_probes must be a list"
                )
            parsed_groups: list[tuple[str, ...]] = []
            for group in raw_probes:
                if not isinstance(group, dict):
                    raise PriorRunReplayError(
                        f"judgments corpus {corpus_name!r} fence_probes entries must be mappings"
                    )
                run_ids = group.get("run_ids")
                if (
                    not isinstance(run_ids, list)
                    or len(run_ids) < 2
                    or any(not _text(item) for item in run_ids)
                ):
                    raise PriorRunReplayError(
                        f"judgments corpus {corpus_name!r} fence probe run_ids "
                        "must be a list of at least two strings"
                    )
                parsed_groups.append(tuple(_text(item) for item in run_ids))
            fence_probes_by_corpus[str(corpus_name)] = tuple(parsed_groups)
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
    return JudgmentConfig(
        replay_run_ids_by_corpus=replay_run_ids_by_corpus,
        judgments=loaded,
        fence_probes_by_corpus=fence_probes_by_corpus,
    )


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
