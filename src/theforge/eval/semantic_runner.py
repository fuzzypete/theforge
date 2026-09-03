"""Manual runner for audit-only semantic evaluation."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from theforge.agent_types import COST_UNKNOWN, AgentResult
from theforge.config import PREFLIGHT_READ_ONLY_TOOLS, resolve_preflight_tools
from theforge.config.model_identity import PHASE_PREFLIGHT, canonical_model_id
from theforge.config.types import ModelProfile
from theforge.eval.semantic_input import (
    SemanticEvaluationInput,
    build_semantic_evaluation_input,
)
from theforge.eval.semantic_parser import SemanticOutputParseError, parse_semantic_review_output
from theforge.eval.semantic_prompt import PROMPT_CONTRACT_VERSION, build_semantic_review_prompt
from theforge.eval.semantic_storage import (
    COST_CACHE_HIT,
    FrozenSemanticBaseline,
    SemanticEvaluationRecord,
    SemanticReviewStore,
    utc_now_iso,
)
from theforge.eval.semantic_types import (
    STATUS_EVALUATION_FAILED,
    status_for_outcome,
)

run_agent = None


def _ensure_runner() -> None:
    global run_agent
    if run_agent is None:
        from theforge.runners import run_agent as _run_agent  # noqa: PLC0415

        run_agent = _run_agent


@dataclass(frozen=True)
class SemanticIssue:
    """Issue payload loaded for semantic evaluation."""

    number: int
    issue_ref: str
    title: str
    body: str
    labels: tuple[str, ...]


@dataclass(frozen=True)
class ReviewSemanticResult:
    """Completed semantic-review invocation, cached or live."""

    issue: SemanticIssue
    evaluation_input: SemanticEvaluationInput
    baseline: FrozenSemanticBaseline
    baseline_created: bool
    record: SemanticEvaluationRecord


class SemanticBaselineRequiredError(RuntimeError):
    """Raised when no frozen human baseline exists for a document digest."""

    def __init__(self, *, issue_ref: str, input_digest: str) -> None:
        super().__init__(
            f"semantic baseline required before revealing evaluator output for "
            f"{issue_ref} ({input_digest})"
        )
        self.issue_ref = issue_ref
        self.input_digest = input_digest


def normalize_issue_ref(value: int | str) -> str:
    text = str(value).strip()
    return text if text.startswith("issue-") else f"issue-{int(text)}"


def _gh_issue_view(number: int, project_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "gh",
            "issue",
            "view",
            str(number),
            "--json",
            "title,body,labels",
        ],
        capture_output=True,
        text=True,
        cwd=str(project_root),
        timeout=30,
    )


def load_semantic_issue(
    *,
    issue_number: int,
    project_root: Path,
    gh_issue_view: Callable[[int, Path], subprocess.CompletedProcess[str]] = _gh_issue_view,
) -> SemanticIssue:
    try:
        proc = gh_issue_view(issue_number, project_root)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Failed to fetch GitHub issue #{issue_number}: {exc}") from exc
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or "gh issue view failed"
        raise RuntimeError(f"gh issue view #{issue_number} failed: {err}")
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"gh issue view #{issue_number} returned malformed JSON: {exc}"
        ) from exc

    labels_raw = data.get("labels") or []
    labels: list[str] = []
    for entry in labels_raw:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            labels.append(entry["name"])
        elif isinstance(entry, str):
            labels.append(entry)

    return SemanticIssue(
        number=issue_number,
        issue_ref=normalize_issue_ref(issue_number),
        title=str(data.get("title") or ""),
        body=str(data.get("body") or ""),
        labels=tuple(labels),
    )


def _read_only_tools_for_profile(profile: ModelProfile) -> tuple[str, ...]:
    tools = resolve_preflight_tools(profile.allowed_tools)
    if profile.mode != "api":
        return tools
    from theforge.runners.tool_runtime import TOOL_NAME_MAP  # noqa: PLC0415

    return tuple(TOOL_NAME_MAP.get(tool, tool) for tool in tools) or tuple(
        TOOL_NAME_MAP.get(tool, tool) for tool in PREFLIGHT_READ_ONLY_TOOLS
    )


def build_audit_only_profile(profile: ModelProfile) -> ModelProfile:
    return replace(
        profile,
        allowed_tools=_read_only_tools_for_profile(profile),
        fallback_models=(),
        api_fallback=None,
        phase=PHASE_PREFLIGHT,
        sandbox_mode="read-only",
        max_iterations=1,
    )


def semantic_model_id(profile: ModelProfile) -> str:
    provider = profile.provider_family
    if provider is None:
        raise ValueError(f"profile {profile.name!r} does not resolve to a provider family")
    return canonical_model_id(provider, profile.model, profile.transport_kind)


def _resolved_model_id(result: AgentResult, configured_profile: ModelProfile) -> str | None:
    provider = configured_profile.provider_family
    if provider is None or not result.model_used or not result.transport_used:
        return None
    return canonical_model_id(provider, result.model_used, result.transport_used)


def _failure_record(
    *,
    issue: SemanticIssue,
    evaluation_input: SemanticEvaluationInput,
    model_id: str,
    prompt_contract_version: str,
    profile: ModelProfile,
    duration_seconds: float,
    started_at: str,
    completed_at: str,
    failure_detail: str,
    cost_usd: float | None = None,
    cost_provenance: str = COST_UNKNOWN,
    resolved_model_id: str | None = None,
    cache_hit: bool = False,
) -> SemanticEvaluationRecord:
    return SemanticEvaluationRecord(
        issue_ref=issue.issue_ref,
        canonical_type=evaluation_input.canonical_type,
        input_digest=evaluation_input.input_digest,
        model_id=model_id,
        prompt_contract_version=prompt_contract_version,
        status=STATUS_EVALUATION_FAILED,
        cache_hit=cache_hit,
        duration_seconds=duration_seconds,
        cost_usd=cost_usd,
        cost_provenance=cost_provenance,
        started_at=started_at,
        completed_at=completed_at,
        configured_profile_name=profile.name,
        configured_model_name=profile.model,
        resolved_model_id=resolved_model_id,
        failure_detail=failure_detail,
    )


def _cache_record(
    *,
    cached: SemanticEvaluationRecord,
    profile: ModelProfile,
    started_at: str,
    completed_at: str,
    duration_seconds: float,
) -> SemanticEvaluationRecord:
    return SemanticEvaluationRecord(
        issue_ref=cached.issue_ref,
        canonical_type=cached.canonical_type,
        input_digest=cached.input_digest,
        model_id=cached.model_id,
        prompt_contract_version=cached.prompt_contract_version,
        status=cached.status,
        cache_hit=True,
        duration_seconds=duration_seconds,
        cost_usd=0.0,
        cost_provenance=COST_CACHE_HIT,
        started_at=started_at,
        completed_at=completed_at,
        configured_profile_name=profile.name,
        configured_model_name=profile.model,
        resolved_model_id=cached.resolved_model_id,
        outcome=cached.outcome,
        findings=cached.findings,
        failure_detail=cached.failure_detail,
    )


def review_issue_semantically(
    *,
    issue_number: int,
    project_root: Path,
    secrets: dict[str, str] | None,
    profile: ModelProfile,
    prompt_contract_version: str = PROMPT_CONTRACT_VERSION,
    baseline_defect_ids: tuple[str, ...] | None = None,
    store: SemanticReviewStore | None = None,
    gh_issue_view: Callable[[int, Path], subprocess.CompletedProcess[str]] = _gh_issue_view,
    agent_runner: Callable[..., AgentResult] | None = None,
) -> ReviewSemanticResult:
    issue = load_semantic_issue(
        issue_number=issue_number,
        project_root=project_root,
        gh_issue_view=gh_issue_view,
    )
    evaluation_input = build_semantic_evaluation_input(
        title=issue.title,
        body=issue.body,
        labels=issue.labels,
    )

    semantic_store = store or SemanticReviewStore(project_root)
    baseline = semantic_store.frozen_baseline(evaluation_input.input_digest)
    baseline_created = False
    if baseline is None:
        if baseline_defect_ids is None:
            raise SemanticBaselineRequiredError(
                issue_ref=issue.issue_ref,
                input_digest=evaluation_input.input_digest,
            )
        baseline, baseline_created = semantic_store.freeze_baseline(
            issue_ref=issue.issue_ref,
            input_digest=evaluation_input.input_digest,
            canonical_type=evaluation_input.canonical_type,
            defect_ids=baseline_defect_ids,
        )
    elif baseline_defect_ids is not None:
        baseline, baseline_created = semantic_store.freeze_baseline(
            issue_ref=issue.issue_ref,
            input_digest=evaluation_input.input_digest,
            canonical_type=evaluation_input.canonical_type,
            defect_ids=baseline_defect_ids,
        )

    evaluation_profile = build_audit_only_profile(profile)
    model_id = semantic_model_id(evaluation_profile)

    started_at = utc_now_iso()
    start = time.monotonic()

    cached = semantic_store.latest_record_for_identity(
        input_digest=evaluation_input.input_digest,
        model_id=model_id,
        prompt_contract_version=prompt_contract_version,
    )
    if cached is not None:
        completed_at = utc_now_iso()
        record = _cache_record(
            cached=cached,
            profile=evaluation_profile,
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=time.monotonic() - start,
        )
        semantic_store.append_record(record)
        return ReviewSemanticResult(
            issue=issue,
            evaluation_input=evaluation_input,
            baseline=baseline,
            baseline_created=baseline_created,
            record=record,
        )

    prompt = build_semantic_review_prompt(
        evaluation_input,
        prompt_contract_version=prompt_contract_version,
    )

    _ensure_runner()
    runner = agent_runner or run_agent
    try:
        result = runner(
            prompt=prompt,
            profile=evaluation_profile,
            working_dir=project_root,
            secrets=secrets,
            plain_text=True,
        )
    except Exception as exc:
        completed_at = utc_now_iso()
        record = _failure_record(
            issue=issue,
            evaluation_input=evaluation_input,
            model_id=model_id,
            prompt_contract_version=prompt_contract_version,
            profile=evaluation_profile,
            duration_seconds=time.monotonic() - start,
            started_at=started_at,
            completed_at=completed_at,
            failure_detail=f"agent launch failed: {exc}",
        )
        semantic_store.append_record(record)
        return ReviewSemanticResult(
            issue=issue,
            evaluation_input=evaluation_input,
            baseline=baseline,
            baseline_created=baseline_created,
            record=record,
        )

    completed_at = utc_now_iso()
    duration_seconds = time.monotonic() - start
    resolved_model_id = _resolved_model_id(result, evaluation_profile)
    failure_detail = (result.output or "").strip() or result.failure_code or ""
    if not result.success or not (result.output or "").strip():
        record = _failure_record(
            issue=issue,
            evaluation_input=evaluation_input,
            model_id=model_id,
            prompt_contract_version=prompt_contract_version,
            profile=evaluation_profile,
            duration_seconds=duration_seconds,
            started_at=started_at,
            completed_at=completed_at,
            failure_detail=failure_detail or f"agent failed with exit code {result.exit_code}",
            cost_usd=result.cost_usd,
            cost_provenance=result.cost_provenance,
            resolved_model_id=resolved_model_id,
        )
        semantic_store.append_record(record)
        return ReviewSemanticResult(
            issue=issue,
            evaluation_input=evaluation_input,
            baseline=baseline,
            baseline_created=baseline_created,
            record=record,
        )

    try:
        parsed = parse_semantic_review_output(result.output)
    except SemanticOutputParseError as exc:
        record = _failure_record(
            issue=issue,
            evaluation_input=evaluation_input,
            model_id=model_id,
            prompt_contract_version=prompt_contract_version,
            profile=evaluation_profile,
            duration_seconds=duration_seconds,
            started_at=started_at,
            completed_at=completed_at,
            failure_detail=f"output parse failed: {exc}",
            cost_usd=result.cost_usd,
            cost_provenance=result.cost_provenance,
            resolved_model_id=resolved_model_id,
        )
        semantic_store.append_record(record)
        return ReviewSemanticResult(
            issue=issue,
            evaluation_input=evaluation_input,
            baseline=baseline,
            baseline_created=baseline_created,
            record=record,
        )

    record = SemanticEvaluationRecord(
        issue_ref=issue.issue_ref,
        canonical_type=evaluation_input.canonical_type,
        input_digest=evaluation_input.input_digest,
        model_id=model_id,
        prompt_contract_version=prompt_contract_version,
        status=status_for_outcome(parsed.outcome),
        cache_hit=False,
        duration_seconds=duration_seconds,
        cost_usd=result.cost_usd,
        cost_provenance=result.cost_provenance,
        started_at=started_at,
        completed_at=completed_at,
        configured_profile_name=evaluation_profile.name,
        configured_model_name=evaluation_profile.model,
        resolved_model_id=resolved_model_id,
        outcome=parsed.outcome,
        findings=parsed.findings,
    )
    semantic_store.append_record(record)
    return ReviewSemanticResult(
        issue=issue,
        evaluation_input=evaluation_input,
        baseline=baseline,
        baseline_created=baseline_created,
        record=record,
    )
