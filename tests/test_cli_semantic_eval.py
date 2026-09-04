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
    assert "use_cache" not in mock_review.call_args.kwargs


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


def _ratify_config(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(project_root=tmp_path)


def _seed_evaluation(tmp_path: Path, findings: tuple[SemanticFinding, ...]):
    """Record one successful evaluation of issue #2785's current revision."""
    from theforge.eval.semantic_input import build_semantic_evaluation_input
    from theforge.eval.semantic_storage import SemanticReviewStore
    from theforge.eval.semantic_types import (
        OUTCOME_NO_FINDINGS,
        STATUS_NO_FINDINGS,
    )

    evaluation_input = build_semantic_evaluation_input(
        title="Add a force flag", body="## What\n\nbody", labels=("enhancement",)
    )
    record = SemanticEvaluationRecord(
        issue_ref="issue-2785",
        canonical_type="enhancement",
        input_digest=evaluation_input.input_digest,
        model_id="anthropic/sonnet/cli",
        prompt_contract_version="semantic-review.v1",
        status=STATUS_FINDINGS if findings else STATUS_NO_FINDINGS,
        cache_hit=False,
        duration_seconds=1.0,
        cost_usd=0.1,
        outcome=OUTCOME_FINDINGS if findings else OUTCOME_NO_FINDINGS,
        findings=findings,
    )
    store = SemanticReviewStore(tmp_path)
    store.append_record(record)
    return store, evaluation_input


def _semantic_issue():
    return SimpleNamespace(
        number=2785,
        issue_ref="issue-2785",
        title="Add a force flag",
        body="## What\n\nbody",
        labels=("enhancement",),
    )


def _ratify_args(**overrides) -> SimpleNamespace:
    args = SimpleNamespace(
        issue_number="2785",
        accept=[],
        reject=[],
        reject_all=False,
        input_digest=None,
        config=None,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_cmd_ratify_semantic_records_decisions_and_reports_readiness(
    tmp_path: Path, capsys
) -> None:
    accepted = SemanticFinding(summary="real defect", rationale="r", severity="high")
    rejected = SemanticFinding(summary="not a defect", rationale="r", severity="low")
    store, _ = _seed_evaluation(tmp_path, (accepted, rejected))

    with (
        patch(
            "theforge.cli.eval_cmd._load_checked_config",
            return_value=_ratify_config(tmp_path),
        ),
        patch(
            "theforge.eval.semantic_runner.load_semantic_issue",
            return_value=_semantic_issue(),
        ),
    ):
        rc = eval_cmd.cmd_ratify_semantic(
            _ratify_args(
                accept=[accepted.finding_digest],
                reject=[rejected.finding_digest],
            )
        )

    out = capsys.readouterr().out
    assert rc == 0
    assert "semantic_state=accepted_concerns" in out
    assert "semantic_requirement=required" in out
    stored = store.iter_ratifications()
    assert len(stored) == 1
    assert stored[0].accepted_digests() == (accepted.finding_digest,)


def test_cmd_ratify_semantic_reject_all_yields_reviewed_ready(tmp_path: Path, capsys) -> None:
    finding = SemanticFinding(summary="not a defect", rationale="r", severity="low")
    _seed_evaluation(tmp_path, (finding,))

    with (
        patch(
            "theforge.cli.eval_cmd._load_checked_config",
            return_value=_ratify_config(tmp_path),
        ),
        patch(
            "theforge.eval.semantic_runner.load_semantic_issue",
            return_value=_semantic_issue(),
        ),
    ):
        rc = eval_cmd.cmd_ratify_semantic(_ratify_args(reject_all=True))

    assert rc == 0
    assert "semantic_state=reviewed_ready" in capsys.readouterr().out


def test_cmd_ratify_semantic_refuses_an_undecided_concern(tmp_path: Path, capsys) -> None:
    one = SemanticFinding(summary="one", rationale="r", severity="low")
    two = SemanticFinding(summary="two", rationale="r", severity="low")
    store, _ = _seed_evaluation(tmp_path, (one, two))

    with (
        patch(
            "theforge.cli.eval_cmd._load_checked_config",
            return_value=_ratify_config(tmp_path),
        ),
        patch(
            "theforge.eval.semantic_runner.load_semantic_issue",
            return_value=_semantic_issue(),
        ),
    ):
        rc = eval_cmd.cmd_ratify_semantic(_ratify_args(reject=[one.finding_digest]))

    assert rc == 1
    assert "every raised concern must be accepted or rejected" in capsys.readouterr().err
    assert store.iter_ratifications() == []


def test_cmd_ratify_semantic_refuses_an_unknown_finding_digest(tmp_path: Path, capsys) -> None:
    _seed_evaluation(tmp_path, ())

    with (
        patch(
            "theforge.cli.eval_cmd._load_checked_config",
            return_value=_ratify_config(tmp_path),
        ),
        patch(
            "theforge.eval.semantic_runner.load_semantic_issue",
            return_value=_semantic_issue(),
        ),
    ):
        rc = eval_cmd.cmd_ratify_semantic(_ratify_args(accept=["deadbeef"]))

    assert rc == 1
    assert "not raised by this evaluation" in capsys.readouterr().err


def test_cmd_ratify_semantic_refuses_without_an_evaluation(tmp_path: Path, capsys) -> None:
    with (
        patch(
            "theforge.cli.eval_cmd._load_checked_config",
            return_value=_ratify_config(tmp_path),
        ),
        patch(
            "theforge.eval.semantic_runner.load_semantic_issue",
            return_value=_semantic_issue(),
        ),
    ):
        rc = eval_cmd.cmd_ratify_semantic(_ratify_args())

    assert rc == 1
    assert "no successful semantic evaluation is recorded" in capsys.readouterr().err


def test_main_dispatches_ratify_semantic_command() -> None:
    main_module = import_module("theforge.cli.main")
    with (
        patch.object(sys, "argv", ["forge", "ratify-semantic", "2785", "--reject-all"]),
        patch("theforge.cli.main.eval_cmd.cmd_ratify_semantic", return_value=0) as mock_cmd,
        pytest.raises(SystemExit) as excinfo,
    ):
        main_module.main()

    assert excinfo.value.code == 0
    mock_cmd.assert_called_once()


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


def test_review_semantic_parser_rejects_no_cache_flag() -> None:
    main_module = import_module("theforge.cli.main")
    parser = main_module.build_parser()

    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["review-semantic", "42", "--no-cache"])

    assert excinfo.value.code == 2
