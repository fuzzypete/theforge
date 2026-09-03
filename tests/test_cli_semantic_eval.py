from __future__ import annotations

import json
import sys
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from theforge.cli import eval_cmd
from theforge.config.types import ModelProfile
from theforge.eval.semantic_runner import ReviewSemanticResult
from theforge.eval.semantic_storage import (
    FrozenSemanticBaseline,
    SemanticEvaluationRecord,
)
from theforge.eval.semantic_types import OUTCOME_FINDINGS, STATUS_FINDINGS, SemanticFinding


def _profile(name: str = "preflight", *, model: str = "sonnet") -> ModelProfile:
    return ModelProfile(
        name=name,
        cli="claude",
        model=model,
        budget_usd=1.0,
        timeout_seconds=60,
        allowed_tools=("Read",),
        phase="preflight",
    )


def _review_result() -> ReviewSemanticResult:
    finding = SemanticFinding(
        summary="Semantic defect",
        rationale="The body leaves the decision ambiguous.",
        severity="high",
    )
    record = SemanticEvaluationRecord(
        issue_ref="issue-2681",
        canonical_type="enhancement",
        input_digest="digest-123",
        model_id="anthropic/sonnet/cli",
        prompt_contract_version="semantic-review.v1",
        status=STATUS_FINDINGS,
        cache_hit=False,
        duration_seconds=1.2,
        cost_usd=0.2,
        cost_provenance="estimated",
        started_at="2026-09-03T00:00:00+00:00",
        completed_at="2026-09-03T00:00:01+00:00",
        configured_profile_name="preflight",
        configured_model_name="sonnet",
        resolved_model_id="anthropic/sonnet/cli",
        outcome=OUTCOME_FINDINGS,
        findings=(finding,),
    )
    return ReviewSemanticResult(
        issue=SimpleNamespace(issue_ref="issue-2681"),
        evaluation_input=SimpleNamespace(
            input_digest="digest-123",
            canonical_type="enhancement",
        ),
        baseline=FrozenSemanticBaseline(
            issue_ref="issue-2681",
            input_digest="digest-123",
            canonical_type="enhancement",
            defect_ids=("d1",),
            frozen_at="2026-09-03T00:00:00+00:00",
        ),
        baseline_created=True,
        record=record,
    )


def test_cmd_review_semantic_prints_findings_and_uses_default_profile(capsys) -> None:
    config = SimpleNamespace(
        preflight_profile=_profile(),
        project_root=Path("/tmp/project"),
        secrets={},
    )
    args = SimpleNamespace(
        issue_number="2681",
        profile=None,
        prompt_contract_version=None,
        baseline_defect_ids=["d1"],
        freeze_empty_baseline=False,
        no_cache=False,
        config=None,
    )

    with (
        patch("theforge.cli.eval_cmd._load_checked_config", return_value=config),
        patch(
            "theforge.eval.semantic_runner.review_issue_semantically",
            return_value=_review_result(),
        ) as mock_review,
    ):
        rc = eval_cmd.cmd_review_semantic(args)

    out = capsys.readouterr().out
    assert rc == 0
    assert "baseline_frozen_at=2026-09-03T00:00:00+00:00" in out
    assert "status=FINDINGS" in out
    assert "input_digest=digest-123" in out
    assert "model_id=anthropic/sonnet/cli" in out
    assert "prompt_contract_version=semantic-review.v1" in out
    assert "Semantic defect" in out
    assert mock_review.call_args.kwargs["profile"] == config.preflight_profile


def test_cmd_semantic_report_renders_json(capsys) -> None:
    config = SimpleNamespace(project_root=Path("/tmp/project"))
    args = SimpleNamespace(corpus="corpus.yaml", output_format="json", config=None)

    with (
        patch("theforge.cli.eval_cmd._load_checked_config", return_value=config),
        patch("theforge.eval.semantic_report.load_semantic_corpus", return_value=object()),
        patch(
            "theforge.eval.semantic_report.build_semantic_corpus_report",
            return_value=SimpleNamespace(to_dict=lambda: {"precision": 0.5}),
        ),
        patch(
            "theforge.eval.semantic_report.render_semantic_corpus_report_json",
            return_value=json.dumps({"precision": 0.5}),
        ),
    ):
        rc = eval_cmd.cmd_semantic_report(args)

    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out) == {"precision": 0.5}


def test_main_dispatches_review_semantic_command() -> None:
    main_module = import_module("theforge.cli.main")
    with (
        patch.object(sys, "argv", ["forge", "review-semantic", "42"]),
        patch("theforge.cli.main.eval_cmd.cmd_review_semantic", return_value=0) as mock_cmd,
        pytest.raises(SystemExit) as excinfo,
    ):
        main_module.main()

    assert excinfo.value.code == 0
    mock_cmd.assert_called_once()
