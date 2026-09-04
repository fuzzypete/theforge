from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from theforge.agent_types import AgentResult
from theforge.config.types import ModelProfile, TransportFallbackConfig
from theforge.eval.semantic_input import build_semantic_evaluation_input
from theforge.eval.semantic_report import (
    SemanticCorpus,
    SemanticCorpusEntry,
    SemanticFindingJudgment,
    build_semantic_corpus_report,
    render_semantic_corpus_report,
)
from theforge.eval.semantic_runner import (
    SemanticBaselineRequiredError,
    build_audit_only_profile,
    load_semantic_issue,
    review_issue_semantically,
    semantic_model_id,
)
from theforge.eval.semantic_storage import (
    FrozenSemanticBaseline,
    SemanticEvaluationRecord,
    SemanticReviewStore,
    utc_now_iso,
)
from theforge.eval.semantic_types import (
    OUTCOME_FINDINGS,
    STATUS_EVALUATION_FAILED,
    STATUS_FINDINGS,
    STATUS_NO_FINDINGS,
    SemanticFinding,
)


def _profile(
    *,
    provider: str | None = None,
    cli: str | None = "claude",
    model: str = "sonnet",
    allowed_tools: tuple[str, ...] = ("Read", "Edit", "Write", "Bash", "Glob", "Grep"),
) -> ModelProfile:
    return ModelProfile(
        name="semantic-source",
        provider=provider,
        cli=cli,
        model=model,
        budget_usd=1.0,
        timeout_seconds=60,
        allowed_tools=allowed_tools,
        fallback_models=("fallback-model",),
        api_fallback=TransportFallbackConfig(provider="anthropic", model="haiku"),
        phase="review",
    )


def _issue_view(
    *,
    title: str = "Audit issue",
    body: str = "## Acceptance criteria\n- keep it tight",
    labels: list[object] | None = None,
):
    payload = {
        "title": title,
        "body": body,
        "labels": labels if labels is not None else [{"name": "enhancement"}],
    }
    return lambda _number, _root: subprocess.CompletedProcess(
        args=["gh", "issue", "view", "1"],
        returncode=0,
        stdout=json.dumps(payload),
        stderr="",
    )


def _agent_result(
    output: str,
    *,
    success: bool = True,
    cost_usd: float | None = 0.25,
) -> AgentResult:
    return AgentResult(
        success=success,
        output=output,
        session_id=None,
        cost_usd=cost_usd,
        exit_code=0 if success else 1,
        raw={},
        profile_name="semantic-source",
        model_used="sonnet",
        transport_used="cli",
        cost_provenance="estimated",
    )


class TestSemanticInput:
    def test_digest_is_deterministic_and_serializes_null_canonical_type(self) -> None:
        first = build_semantic_evaluation_input(
            title="Title",
            body="Body",
            labels=("untyped",),
        )
        second = build_semantic_evaluation_input(
            title="Title",
            body="Body",
            labels=("untyped",),
        )

        assert first.serialized_json == second.serialized_json
        assert first.input_digest == second.input_digest
        assert first.canonical_type is None
        assert first.serialized_json == '{"body":"Body","canonical_type":null,"title":"Title"}'


class TestAuditOnlyProfile:
    def test_build_audit_only_profile_strips_mutation_and_fallbacks(self) -> None:
        profile = _profile()

        derived = build_audit_only_profile(profile)

        assert derived.allowed_tools == ("Read", "Glob", "Grep")
        assert derived.fallback_models == ()
        assert derived.api_fallback is None
        assert derived.phase == "preflight"
        assert derived.sandbox_mode == "read-only"
        assert derived.max_iterations == 1

    def test_api_profile_tools_are_canonicalized_to_read_only_names(self) -> None:
        profile = _profile(
            provider="openai",
            cli=None,
            model="gpt-5.6",
            allowed_tools=(),
        )

        derived = build_audit_only_profile(profile)

        assert derived.allowed_tools == ("read_file", "glob", "grep")


class TestSemanticIssueLoad:
    def test_load_semantic_issue_uses_only_issue_view_command(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        seen: list[list[str]] = []

        def fake_run(*args, **kwargs):
            seen.append(list(args[0]))
            return subprocess.CompletedProcess(
                args=args[0],
                returncode=0,
                stdout=json.dumps(
                    {
                        "title": "Title",
                        "body": "Body",
                        "labels": [{"name": "enhancement"}],
                    }
                ),
                stderr="",
            )

        monkeypatch.setattr("theforge.eval.semantic_runner.subprocess.run", fake_run)
        issue = load_semantic_issue(issue_number=7, project_root=tmp_path)

        assert issue.issue_ref == "issue-7"
        assert seen == [["gh", "issue", "view", "7", "--json", "title,body,labels"]]


class TestSemanticRunner:
    def test_review_requires_frozen_baseline_before_runner_invocation(
        self, tmp_path: Path
    ) -> None:
        called = False

        def agent_runner(**kwargs):
            nonlocal called
            called = True
            return _agent_result('{"outcome":"NO_FINDINGS"}')

        with pytest.raises(SemanticBaselineRequiredError):
            review_issue_semantically(
                issue_number=1,
                project_root=tmp_path,
                secrets={},
                profile=_profile(),
                gh_issue_view=_issue_view(),
                agent_runner=agent_runner,
            )

        assert called is False

    def test_review_records_success_and_stores_identity_components(self, tmp_path: Path) -> None:
        seen_profile: ModelProfile | None = None

        def agent_runner(**kwargs):
            nonlocal seen_profile
            seen_profile = kwargs["profile"]
            return _agent_result(
                json.dumps(
                    {
                        "outcome": "FINDINGS",
                        "findings": [
                            {
                                "summary": "Default action is semantically loose",
                                "rationale": "It permits the opposite of the contract.",
                                "severity": "high",
                            }
                        ],
                    }
                )
            )

        result = review_issue_semantically(
            issue_number=1,
            project_root=tmp_path,
            secrets={"API_KEY": "ignored"},
            profile=_profile(),
            gh_issue_view=_issue_view(),
            agent_runner=agent_runner,
            baseline_defect_ids=("baseline-defect",),
        )
        store = SemanticReviewStore(tmp_path)
        records = store.iter_records()
        baselines = store.iter_baselines()

        assert seen_profile is not None
        assert seen_profile.allowed_tools == ("Read", "Glob", "Grep")
        assert seen_profile.fallback_models == ()
        assert seen_profile.api_fallback is None
        assert seen_profile.phase == "preflight"
        assert len(records) == 1
        assert len(baselines) == 1
        assert records[0].status == STATUS_FINDINGS
        assert records[0].outcome == OUTCOME_FINDINGS
        assert records[0].input_digest == result.evaluation_input.input_digest
        assert records[0].model_id == semantic_model_id(seen_profile)
        assert records[0].prompt_contract_version == "semantic-review.v1"
        assert records[0].resolved_model_id == records[0].model_id
        assert baselines[0].input_digest == result.evaluation_input.input_digest
        assert baselines[0].defect_ids == ("baseline-defect",)

    def test_exact_identity_reuses_cache_and_identity_changes_rerun(self, tmp_path: Path) -> None:
        bodies = iter(
            [
                "First body",
                "First body",
                "First body",
                "Second body",
            ]
        )
        calls: list[str] = []

        def gh_issue_view(_number: int, _root: Path):
            body = next(bodies)
            return subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(
                    {
                        "title": "Issue",
                        "body": body,
                        "labels": [{"name": "enhancement"}],
                    }
                ),
                stderr="",
            )

        def agent_runner(**kwargs):
            calls.append(kwargs["profile"].model)
            return _agent_result('{"outcome":"NO_FINDINGS"}')

        base_profile = _profile()
        first = review_issue_semantically(
            issue_number=1,
            project_root=tmp_path,
            secrets={},
            profile=base_profile,
            gh_issue_view=gh_issue_view,
            agent_runner=agent_runner,
            baseline_defect_ids=(),
        )
        second = review_issue_semantically(
            issue_number=1,
            project_root=tmp_path,
            secrets={},
            profile=base_profile,
            gh_issue_view=gh_issue_view,
            agent_runner=agent_runner,
        )
        third = review_issue_semantically(
            issue_number=1,
            project_root=tmp_path,
            secrets={},
            profile=base_profile,
            gh_issue_view=gh_issue_view,
            agent_runner=agent_runner,
            prompt_contract_version="semantic-review.v2",
        )
        fourth = review_issue_semantically(
            issue_number=1,
            project_root=tmp_path,
            secrets={},
            profile=_profile(model="opus"),
            gh_issue_view=gh_issue_view,
            agent_runner=agent_runner,
            baseline_defect_ids=(),
        )

        assert first.record.cache_hit is False
        assert second.record.cache_hit is True
        assert third.record.cache_hit is False
        assert fourth.record.cache_hit is False
        assert calls == ["sonnet", "sonnet", "opus"]

    def test_runner_launch_and_parse_failures_record_evaluation_failed(
        self, tmp_path: Path
    ) -> None:
        def raising_runner(**kwargs):
            raise RuntimeError("launch boom")

        launch_failed = review_issue_semantically(
            issue_number=1,
            project_root=tmp_path,
            secrets={},
            profile=_profile(),
            gh_issue_view=_issue_view(),
            agent_runner=raising_runner,
            baseline_defect_ids=(),
        )

        parse_failed = review_issue_semantically(
            issue_number=1,
            project_root=tmp_path,
            secrets={},
            profile=_profile(),
            gh_issue_view=_issue_view(body="Different body"),
            agent_runner=lambda **kwargs: _agent_result("plain prose, not structured"),
            baseline_defect_ids=(),
        )

        assert launch_failed.record.status == STATUS_EVALUATION_FAILED
        assert launch_failed.record.outcome is None
        assert "launch boom" in (launch_failed.record.failure_detail or "")
        assert launch_failed.record.raw_output is None
        assert parse_failed.record.status == STATUS_EVALUATION_FAILED
        assert parse_failed.record.outcome is None
        assert parse_failed.record.findings == ()
        assert "parse failed" in (parse_failed.record.failure_detail or "")
        assert parse_failed.record.raw_output == "plain prose, not structured"

        records = SemanticReviewStore(tmp_path).iter_records()
        assert records[0].raw_output is None
        assert records[1].raw_output == "plain prose, not structured"

    def test_failed_identity_record_does_not_poison_repeat_run(self, tmp_path: Path) -> None:
        calls = 0

        def agent_runner(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return _agent_result("plain prose, not structured")
            return _agent_result('{"outcome":"NO_FINDINGS"}')

        first = review_issue_semantically(
            issue_number=1,
            project_root=tmp_path,
            secrets={},
            profile=_profile(),
            gh_issue_view=_issue_view(),
            agent_runner=agent_runner,
            baseline_defect_ids=(),
        )
        second = review_issue_semantically(
            issue_number=1,
            project_root=tmp_path,
            secrets={},
            profile=_profile(),
            gh_issue_view=_issue_view(),
            agent_runner=agent_runner,
        )

        assert first.record.status == STATUS_EVALUATION_FAILED
        assert first.record.cache_hit is False
        assert second.record.status == STATUS_NO_FINDINGS
        assert second.record.cache_hit is False
        assert calls == 2

    def test_later_failed_record_does_not_shadow_cached_success(self, tmp_path: Path) -> None:
        calls = 0

        def agent_runner(**kwargs):
            nonlocal calls
            calls += 1
            return _agent_result('{"outcome":"NO_FINDINGS"}')

        first = review_issue_semantically(
            issue_number=1,
            project_root=tmp_path,
            secrets={},
            profile=_profile(),
            gh_issue_view=_issue_view(),
            agent_runner=agent_runner,
            baseline_defect_ids=(),
        )
        store = SemanticReviewStore(tmp_path)
        store.append_record(
            SemanticEvaluationRecord(
                issue_ref=first.record.issue_ref,
                canonical_type=first.record.canonical_type,
                input_digest=first.record.input_digest,
                model_id=first.record.model_id,
                prompt_contract_version=first.record.prompt_contract_version,
                status=STATUS_EVALUATION_FAILED,
                cache_hit=False,
                duration_seconds=0.2,
                cost_usd=None,
                cost_provenance="unknown",
                started_at=utc_now_iso(),
                completed_at=utc_now_iso(),
                configured_profile_name="semantic-source",
                configured_model_name="sonnet",
                resolved_model_id=None,
                failure_detail="transient parse failure",
            )
        )

        second = review_issue_semantically(
            issue_number=1,
            project_root=tmp_path,
            secrets={},
            profile=_profile(),
            gh_issue_view=_issue_view(),
            agent_runner=agent_runner,
        )

        assert first.record.status == STATUS_NO_FINDINGS
        assert second.record.status == STATUS_NO_FINDINGS
        assert second.record.cache_hit is True
        assert second.record.failure_detail is None
        assert calls == 1

    def test_existing_frozen_baseline_rejects_mismatched_defect_ids(self, tmp_path: Path) -> None:
        review_issue_semantically(
            issue_number=1,
            project_root=tmp_path,
            secrets={},
            profile=_profile(),
            gh_issue_view=_issue_view(),
            agent_runner=lambda **kwargs: _agent_result('{"outcome":"NO_FINDINGS"}'),
            baseline_defect_ids=("d1",),
        )

        with pytest.raises(ValueError, match="already frozen and cannot be changed"):
            review_issue_semantically(
                issue_number=1,
                project_root=tmp_path,
                secrets={},
                profile=_profile(),
                gh_issue_view=_issue_view(),
                agent_runner=lambda **kwargs: _agent_result('{"outcome":"NO_FINDINGS"}'),
                baseline_defect_ids=("d2",),
            )


class TestSemanticReporting:
    def test_corpus_report_calculates_precision_rejection_cost_and_novel_metrics(
        self, tmp_path: Path
    ) -> None:
        store = SemanticReviewStore(tmp_path)
        digest_one = "digest-one"
        digest_two = "digest-two"
        digest_three = "digest-three"

        finding_known = SemanticFinding(
            summary="Known defect",
            rationale="Baseline defect recovered",
            severity="high",
        )
        finding_rejected = SemanticFinding(
            summary="Rejected defect",
            rationale="Human said no",
            severity="low",
        )
        finding_novel = SemanticFinding(
            summary="Novel defect",
            rationale="Human confirmed a new issue",
            severity="medium",
        )

        store.append_baseline(
            FrozenSemanticBaseline(
                issue_ref="issue-1",
                input_digest=digest_one,
                canonical_type="enhancement",
                defect_ids=("d1", "d2"),
                frozen_at=utc_now_iso(),
            )
        )
        store.append_baseline(
            FrozenSemanticBaseline(
                issue_ref="issue-3",
                input_digest=digest_three,
                canonical_type="enhancement",
                defect_ids=("d3",),
                frozen_at=utc_now_iso(),
            )
        )

        store.append_record(
            SemanticEvaluationRecord(
                issue_ref="issue-1",
                canonical_type="enhancement",
                input_digest=digest_one,
                model_id="anthropic/sonnet/cli",
                prompt_contract_version="semantic-review.v1",
                status=STATUS_FINDINGS,
                cache_hit=False,
                duration_seconds=1.0,
                cost_usd=0.6,
                cost_provenance="estimated",
                started_at=utc_now_iso(),
                completed_at=utc_now_iso(),
                configured_profile_name="preflight",
                configured_model_name="sonnet",
                resolved_model_id="anthropic/sonnet/cli",
                outcome=OUTCOME_FINDINGS,
                findings=(finding_known, finding_rejected, finding_novel),
            )
        )
        store.append_record(
            SemanticEvaluationRecord(
                issue_ref="issue-1",
                canonical_type="enhancement",
                input_digest=digest_one,
                model_id="anthropic/sonnet/cli",
                prompt_contract_version="semantic-review.v1",
                status=STATUS_FINDINGS,
                cache_hit=False,
                duration_seconds=1.0,
                cost_usd=0.4,
                cost_provenance="estimated",
                started_at=utc_now_iso(),
                completed_at=utc_now_iso(),
                configured_profile_name="preflight",
                configured_model_name="sonnet",
                resolved_model_id="anthropic/sonnet/cli",
                outcome=OUTCOME_FINDINGS,
                findings=(finding_known, finding_rejected, finding_novel),
            )
        )
        store.append_record(
            SemanticEvaluationRecord(
                issue_ref="issue-1",
                canonical_type="enhancement",
                input_digest=digest_one,
                model_id="anthropic/sonnet/cli",
                prompt_contract_version="semantic-review.v1",
                status=STATUS_FINDINGS,
                cache_hit=True,
                duration_seconds=0.01,
                cost_usd=0.0,
                cost_provenance="cache_zero",
                started_at=utc_now_iso(),
                completed_at=utc_now_iso(),
                configured_profile_name="preflight",
                configured_model_name="sonnet",
                resolved_model_id="anthropic/sonnet/cli",
                outcome=OUTCOME_FINDINGS,
                findings=(finding_known, finding_rejected, finding_novel),
            )
        )
        store.append_record(
            SemanticEvaluationRecord(
                issue_ref="issue-3",
                canonical_type="enhancement",
                input_digest=digest_three,
                model_id="anthropic/sonnet/cli",
                prompt_contract_version="semantic-review.v1",
                status=STATUS_EVALUATION_FAILED,
                cache_hit=False,
                duration_seconds=0.5,
                cost_usd=None,
                cost_provenance="unknown",
                started_at=utc_now_iso(),
                completed_at=utc_now_iso(),
                configured_profile_name="preflight",
                configured_model_name="sonnet",
                resolved_model_id="anthropic/sonnet/cli",
                failure_detail="parse failed",
            )
        )

        corpus = SemanticCorpus(
            name="semantic-audit",
            entries=(
                SemanticCorpusEntry(
                    issue_ref="issue-1",
                    input_digest=digest_one,
                    frozen_baseline_defect_ids=("d1", "d2"),
                    judgments=(
                        SemanticFindingJudgment(
                            finding_digest=finding_known.finding_digest,
                            judgment="confirmed",
                            defect_id="d1",
                        ),
                        SemanticFindingJudgment(
                            finding_digest=finding_rejected.finding_digest,
                            judgment="rejected",
                        ),
                        SemanticFindingJudgment(
                            finding_digest=finding_novel.finding_digest,
                            judgment="confirmed",
                            defect_id="novel-1",
                        ),
                    ),
                ),
                SemanticCorpusEntry(
                    issue_ref="issue-2",
                    input_digest=digest_two,
                    frozen_baseline_defect_ids=("d4",),
                    judgments=(),
                ),
                SemanticCorpusEntry(
                    issue_ref="issue-3",
                    input_digest=digest_three,
                    frozen_baseline_defect_ids=("d3",),
                    judgments=(),
                ),
            ),
        )

        report = build_semantic_corpus_report(corpus, store=store)

        assert report.total_documents == 3
        assert report.documents_with_records == 2
        assert report.absent_documents == 1
        assert report.failed_documents == 1
        assert report.known_defects_recovered == 1
        assert report.baseline_defects_total == 4
        assert report.confirmed_findings == 2
        assert report.rejected_findings == 1
        assert report.confirmed_novel_defects == 1
        assert report.precision == pytest.approx(2 / 3)
        assert report.rejection_rate == pytest.approx(1 / 3)
        assert report.cost_per_confirmed_finding == pytest.approx(0.5)
        assert report.cost_unknown_records_excluded == 1
        assert report.repeated_identity_groups == 1
        assert report.stable_repeated_identity_groups == 1
        assert report.repeated_identity_stability == pytest.approx(1.0)
        assert report.independent_repeat_groups == 1
        assert report.stable_independent_repeat_groups == 1
        assert report.independent_repeat_stability == pytest.approx(1.0)
        assert report.cache_derived_repeat_groups == 0
        assert report.stable_cache_derived_repeat_groups == 0
        assert report.cache_derived_repeat_stability is None

    def test_corpus_report_marks_cache_only_independent_stability_unmeasured(
        self, tmp_path: Path
    ) -> None:
        store = SemanticReviewStore(tmp_path)
        digest = "digest-one"
        finding = SemanticFinding(
            summary="Known defect",
            rationale="Baseline defect recovered",
            severity="high",
        )

        store.append_baseline(
            FrozenSemanticBaseline(
                issue_ref="issue-1",
                input_digest=digest,
                canonical_type="enhancement",
                defect_ids=("d1",),
                frozen_at=utc_now_iso(),
            )
        )
        store.append_record(
            SemanticEvaluationRecord(
                issue_ref="issue-1",
                canonical_type="enhancement",
                input_digest=digest,
                model_id="anthropic/sonnet/cli",
                prompt_contract_version="semantic-review.v1",
                status=STATUS_FINDINGS,
                cache_hit=False,
                duration_seconds=1.0,
                cost_usd=0.6,
                cost_provenance="estimated",
                started_at=utc_now_iso(),
                completed_at=utc_now_iso(),
                configured_profile_name="preflight",
                configured_model_name="sonnet",
                resolved_model_id="anthropic/sonnet/cli",
                outcome=OUTCOME_FINDINGS,
                findings=(finding,),
            )
        )
        store.append_record(
            SemanticEvaluationRecord(
                issue_ref="issue-1",
                canonical_type="enhancement",
                input_digest=digest,
                model_id="anthropic/sonnet/cli",
                prompt_contract_version="semantic-review.v1",
                status=STATUS_FINDINGS,
                cache_hit=True,
                duration_seconds=0.01,
                cost_usd=0.0,
                cost_provenance="cache_zero",
                started_at=utc_now_iso(),
                completed_at=utc_now_iso(),
                configured_profile_name="preflight",
                configured_model_name="sonnet",
                resolved_model_id="anthropic/sonnet/cli",
                outcome=OUTCOME_FINDINGS,
                findings=(finding,),
            )
        )

        corpus = SemanticCorpus(
            name="semantic-audit",
            entries=(
                SemanticCorpusEntry(
                    issue_ref="issue-1",
                    input_digest=digest,
                    frozen_baseline_defect_ids=("d1",),
                    judgments=(
                        SemanticFindingJudgment(
                            finding_digest=finding.finding_digest,
                            judgment="confirmed",
                            defect_id="d1",
                        ),
                    ),
                ),
            ),
        )

        report = build_semantic_corpus_report(corpus, store=store)
        rendered = render_semantic_corpus_report(report)

        assert report.repeated_identity_groups == 1
        assert report.stable_repeated_identity_groups == 1
        assert report.repeated_identity_stability == pytest.approx(1.0)
        assert report.independent_repeat_groups == 0
        assert report.stable_independent_repeat_groups == 0
        assert report.independent_repeat_stability is None
        assert report.cache_derived_repeat_groups == 1
        assert report.stable_cache_derived_repeat_groups == 1
        assert report.cache_derived_repeat_stability == pytest.approx(1.0)
        assert "repeat_stability=repeated=1/1 independent=unmeasured cache_derived=1/1" in rendered
