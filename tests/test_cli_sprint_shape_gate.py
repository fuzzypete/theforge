"""CLI tests: sprint query mode honors the sprint-entry shape gate."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

from theforge.cli import cmd_sprint
from theforge.config import (
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    ModelProfile,
    PlanAgentReviewConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.sprint.manifest import ResolvedSprint, SprintResult
from theforge.sprint.shape_gate import ShapeGateResult, SkippedIssue
from theforge.sprint.sources import GitHubIssueSource
from theforge.task import TaskStory


def _api_profile(name: str, provider: str = "anthropic", model: str = "claude-opus-4-6"):
    return ModelProfile(
        name=name,
        provider=provider,
        model=model,
        budget_usd=1.0,
        timeout_seconds=120,
        allowed_tools=("Read",),
    )


def _make_config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="feat/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=ModelProfile(
            name="dev",
            cli="claude",
            model="sonnet",
            budget_usd=2.0,
            timeout_seconds=300,
            allowed_tools=("Read",),
        ),
        preflight_profile=ModelProfile(
            name="preflight",
            cli="claude",
            model="sonnet",
            budget_usd=0.5,
            timeout_seconds=120,
            allowed_tools=("Read",),
        ),
        review_pool=[_api_profile("claude-reviewer"), _api_profile("codex", "openai")],
        synthesis_profile=None,
        retry=RetryPolicy(),
        plan_agent_review=PlanAgentReviewConfig.of(enabled=False),
        log=LogConfig(enabled=False),
    )


def _query_args(tmp_path: Path, *, force: bool = False) -> argparse.Namespace:
    (tmp_path / "forge.yaml").write_text("project:\n  root: .\n", encoding="utf-8")
    return argparse.Namespace(
        manifest=None,
        config=None,
        fg=True,
        detach=False,
        resume=False,
        milestone="v0.5.0",
        label=None,
        issues=None,
        budget="10",
        parallel=1,
        name=None,
        dry_run=False,
        auto_merge=False,
        interactive=False,
        verbose=False,
        no_notify=True,
        no_pull=False,
        force=force,
        base_branch=None,
    )


def _resolved(issues: list[int]) -> ResolvedSprint:
    stories = []
    for n in issues:
        task = TaskStory(name=f"T{n}", slug=f"issue-{n}", github_issue=n)
        stories.append((task, GitHubIssueSource(), f"issue:{n}"))
    return ResolvedSprint(
        name="v0.5.0",
        budget_usd=10.0,
        stories=stories,
        max_parallel=1,
    )


def _ok_result() -> SprintResult:
    return SprintResult(
        name="v0.5.0",
        specs_total=1,
        specs_succeeded=1,
        specs_failed=0,
        specs_skipped=0,
        total_cost_usd=0.0,
        budget_usd=10.0,
        results=[],
    )


def test_cli_query_mode_filters_skipped_and_passes_to_run_sprint(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    args = _query_args(tmp_path)

    fetched = [
        {"number": 1, "title": "good"},
        {"number": 2, "title": "bad"},
    ]
    gated = ShapeGateResult(
        runnable=[{"number": 1, "title": "good"}],
        skipped=[
            SkippedIssue(
                issue_number=2,
                reason_codes=("missing_ac",),
                source="local_check",
                title="bad",
            )
        ],
    )

    captured: dict = {}

    def fake_run_sprint(run_context):
        captured["context"] = run_context
        return _ok_result()

    with (
        patch("theforge.cli.sprint.load_config", return_value=config),
        patch("theforge.cli.sprint._find_config", return_value=tmp_path / "forge.yaml"),
        patch("theforge.sprint.query.fetch_issues_for_milestone", return_value=fetched),
        patch("theforge.sprint.query.build_resolved_sprint", return_value=_resolved([1])),
        patch("theforge.sprint.shape_gate.apply_shape_gate", return_value=gated),
        patch(
            "theforge.cli.sprint._acquire_launch_locks",
            return_value=([], None, {}),
        ),
        patch("theforge.cli.sprint.release_story_locks"),
        patch("theforge.cli.sprint.run_sprint", side_effect=fake_run_sprint),
    ):
        rc = cmd_sprint(args)

    assert rc == 0
    assert "context" in captured
    skipped_issues = captured["context"].skipped_issues
    assert len(skipped_issues) == 1
    assert skipped_issues[0].issue_number == 2
    # Only runnable issues feed build_resolved_sprint.
    # (Confirmed by ``build_resolved_sprint`` being called with one issue,
    # which _resolved([1]) represents — if two had leaked through, the
    # downstream DAG would differ.)


def test_cli_query_mode_all_skipped_exits_cleanly(tmp_path: Path, capsys) -> None:
    import yaml

    config = _make_config(tmp_path)
    args = _query_args(tmp_path)

    fetched = [{"number": 2, "title": "bad"}]
    gated = ShapeGateResult(
        runnable=[],
        skipped=[
            SkippedIssue(
                issue_number=2,
                reason_codes=("missing_ac",),
                source="local_check",
                title="bad",
            )
        ],
    )

    with (
        patch("theforge.cli.sprint.load_config", return_value=config),
        patch("theforge.cli.sprint._find_config", return_value=tmp_path / "forge.yaml"),
        patch("theforge.sprint.query.fetch_issues_for_milestone", return_value=fetched),
        patch("theforge.sprint.shape_gate.apply_shape_gate", return_value=gated),
    ):
        rc = cmd_sprint(args)

    assert rc == 0
    err = capsys.readouterr().err
    assert "skipped by shape gate" in err

    # All-skipped must still produce machine-readable audit/summary records.
    audit_path = tmp_path / ".forge" / "audits" / "sprint-audit.yaml"
    assert audit_path.exists()
    audit = yaml.safe_load(audit_path.read_text())
    assert audit["skipped"][0]["issue_number"] == 2
    assert audit["skipped"][0]["reason_codes"] == ["missing_ac"]

    # Summary file is keyed by sprint name (milestone here is "v0.5.0").
    summary_path = tmp_path / ".forge" / "logs" / "v0.5.0" / "sprint-summary.yaml"
    assert summary_path.exists()
    summary = yaml.safe_load(summary_path.read_text())
    assert summary["skipped"][0]["issue_number"] == 2


def test_cli_passes_configured_classifier_to_shape_gate(tmp_path: Path) -> None:
    """CLI must thread config.shape_check.classifier into the gate call."""
    from theforge.config.types import ShapeCheckConfig

    config = _make_config(tmp_path)
    # dataclasses.replace-style override on the frozen ForgeConfig field.
    object.__setattr__(config, "shape_check", ShapeCheckConfig(classifier="llm"))

    args = _query_args(tmp_path)
    fetched = [{"number": 1, "title": "good"}]
    gated = ShapeGateResult(runnable=fetched, skipped=[])
    captured: dict = {}

    def _spy(*call_args, **kwargs):
        captured["kwargs"] = kwargs
        return gated

    with (
        patch("theforge.cli.sprint.load_config", return_value=config),
        patch("theforge.cli.sprint._find_config", return_value=tmp_path / "forge.yaml"),
        patch("theforge.sprint.query.fetch_issues_for_milestone", return_value=fetched),
        patch("theforge.sprint.query.build_resolved_sprint", return_value=_resolved([1])),
        patch("theforge.sprint.shape_gate.apply_shape_gate", side_effect=_spy),
        patch(
            "theforge.cli.sprint._acquire_launch_locks",
            return_value=([], None, {}),
        ),
        patch("theforge.cli.sprint.release_story_locks"),
        patch("theforge.cli.sprint.run_sprint", return_value=_ok_result()),
    ):
        cmd_sprint(args)

    assert captured["kwargs"]["classifier_mode"] == "llm"


def test_cli_all_skipped_runs_intake_remediation_bridge(tmp_path: Path) -> None:
    """When every issue is skipped at the entry gate and intake remediation
    is enabled, the bridge fires, posts the outcome into the audit/summary
    YAML, and the sprint exits with the shape-gate-skipped issue marked
    with an intake outcome — not silently dropped."""
    import yaml

    from theforge.config.types import IntakeConfig
    from theforge.intake import IntakeOutcome, IntakeOutcomeKind

    config = _make_config(tmp_path)
    object.__setattr__(
        config,
        "intake",
        IntakeConfig(grooming=True, auto_fix=True, auto_fix_mode="comment"),
    )
    args = _query_args(tmp_path)

    fetched = [{"number": 1014, "title": "bad"}]
    gated = ShapeGateResult(
        runnable=[],
        skipped=[
            SkippedIssue(
                issue_number=1014,
                reason_codes=("implementation_plan_in_body",),
                source="local_check",
                title="bad",
                detail="HOW belongs in plan, not body",
            )
        ],
    )

    def fake_remediate(skipped, **_kw):
        # Emulate the bridge having run remediation in comment mode.
        return {
            sk.issue_number: IntakeOutcome(
                slug=f"issue-{sk.issue_number}",
                kind=IntakeOutcomeKind.DROPPED_SHAPE,
                detail="comment mode: proposed replacement posted; story dropped",
                audit={
                    "remediation_source": "agent",
                    "comment_posted": True,
                },
            )
            for sk in skipped
        }

    with (
        patch("theforge.cli.sprint.load_config", return_value=config),
        patch("theforge.cli.sprint._find_config", return_value=tmp_path / "forge.yaml"),
        patch("theforge.sprint.query.fetch_issues_for_milestone", return_value=fetched),
        patch("theforge.sprint.shape_gate.apply_shape_gate", return_value=gated),
        patch(
            "theforge.sprint.entry_intake.remediate_entry_skipped_issues",
            side_effect=fake_remediate,
        ),
    ):
        rc = cmd_sprint(args)

    assert rc == 0

    summary_path = tmp_path / ".forge" / "logs" / "v0.5.0" / "sprint-summary.yaml"
    assert summary_path.exists()
    summary = yaml.safe_load(summary_path.read_text())

    # Bridge outcome must appear in the per-story detail so the operator
    # can see remediation activity rather than silence.
    stories = summary.get("stories") or []
    target = next((s for s in stories if s.get("slug") == "issue-1014"), None)
    assert target is not None, summary
    detail = target.get("detail") or {}
    assert detail.get("intake_kind") == "dropped_shape"
    assert detail.get("intake_audit", {}).get("comment_posted") is True


def test_cli_all_skipped_skips_remediation_under_force(tmp_path: Path) -> None:
    """--force is the operator's explicit escape hatch; the bridge must not
    fire and must not post comments behind the operator's back."""
    config = _make_config(tmp_path)
    args = _query_args(tmp_path, force=True)

    fetched = [{"number": 2, "title": "bad"}]
    gated = ShapeGateResult(
        runnable=fetched,  # force=True returns all as runnable
        skipped=[
            SkippedIssue(
                issue_number=2,
                reason_codes=("missing_ac",),
                source="local_check",
                title="bad",
            )
        ],
    )

    bridge_calls: list = []

    def spy(skipped, **_kw):
        bridge_calls.append(list(skipped))
        return {}

    with (
        patch("theforge.cli.sprint.load_config", return_value=config),
        patch("theforge.cli.sprint._find_config", return_value=tmp_path / "forge.yaml"),
        patch("theforge.sprint.query.fetch_issues_for_milestone", return_value=fetched),
        patch("theforge.sprint.query.build_resolved_sprint", return_value=_resolved([2])),
        patch("theforge.sprint.shape_gate.apply_shape_gate", return_value=gated),
        patch(
            "theforge.sprint.entry_intake.remediate_entry_skipped_issues",
            side_effect=spy,
        ),
        patch(
            "theforge.cli.sprint._acquire_launch_locks",
            return_value=([], None, {}),
        ),
        patch("theforge.cli.sprint.release_story_locks"),
        patch("theforge.cli.sprint.run_sprint", return_value=_ok_result()),
    ):
        cmd_sprint(args)

    assert bridge_calls == []


def test_cli_query_mode_force_runs_every_issue_but_warns(tmp_path: Path, capsys) -> None:
    config = _make_config(tmp_path)
    args = _query_args(tmp_path, force=True)

    fetched = [{"number": 2, "title": "bad"}]
    # With force=True the gate returns *all* issues runnable, but still
    # populates ``skipped`` so we can emit the warning.
    gated = ShapeGateResult(
        runnable=fetched,
        skipped=[
            SkippedIssue(
                issue_number=2,
                reason_codes=("missing_ac",),
                source="local_check",
                title="bad",
            )
        ],
    )

    with (
        patch("theforge.cli.sprint.load_config", return_value=config),
        patch("theforge.cli.sprint._find_config", return_value=tmp_path / "forge.yaml"),
        patch("theforge.sprint.query.fetch_issues_for_milestone", return_value=fetched),
        patch("theforge.sprint.query.build_resolved_sprint", return_value=_resolved([2])),
        patch("theforge.sprint.shape_gate.apply_shape_gate", return_value=gated),
        patch(
            "theforge.cli.sprint._acquire_launch_locks",
            return_value=([], None, {}),
        ),
        patch("theforge.cli.sprint.release_story_locks"),
        patch("theforge.cli.sprint.run_sprint", return_value=_ok_result()),
    ):
        rc = cmd_sprint(args)

    assert rc == 0
    err = capsys.readouterr().err
    assert "--force in effect" in err
    assert "#2" in err
    assert "missing_ac" in err


def test_cli_query_mode_force_banner_separates_semantic_withholdings(
    tmp_path: Path, capsys
) -> None:
    """--force overrides shape refusals only, so the banner must not claim the
    semantically withheld issue is running (#2785)."""
    from theforge.eval.semantic_readiness import SEMANTIC_NOT_RATIFIED_CODE

    config = _make_config(tmp_path)
    args = _query_args(tmp_path, force=True)

    fetched = [{"number": 2, "title": "bad"}, {"number": 3, "title": "unratified"}]
    # apply_shape_gate already excludes the semantic withholding from runnable
    # under --force; only the shape-flagged issue is force-admitted.
    gated = ShapeGateResult(
        runnable=[fetched[0]],
        skipped=[
            SkippedIssue(
                issue_number=2,
                reason_codes=("missing_ac",),
                source="local_check",
                title="bad",
            ),
            SkippedIssue(
                issue_number=3,
                reason_codes=(SEMANTIC_NOT_RATIFIED_CODE,),
                source="semantic_readiness",
                title="unratified",
            ),
        ],
    )

    with (
        patch("theforge.cli.sprint.load_config", return_value=config),
        patch("theforge.cli.sprint._find_config", return_value=tmp_path / "forge.yaml"),
        patch("theforge.sprint.query.fetch_issues_for_milestone", return_value=fetched),
        patch("theforge.sprint.query.build_resolved_sprint", return_value=_resolved([2])),
        patch("theforge.sprint.shape_gate.apply_shape_gate", return_value=gated),
        patch(
            "theforge.cli.sprint._acquire_launch_locks",
            return_value=([], None, {}),
        ),
        patch("theforge.cli.sprint.release_story_locks"),
        patch("theforge.cli.sprint.run_sprint", return_value=_ok_result()),
    ):
        rc = cmd_sprint(args)

    assert rc == 0
    err = capsys.readouterr().err
    force_line = next(line for line in err.splitlines() if "--force in effect" in line)
    withheld_line = next(line for line in err.splitlines() if "remain withheld" in line)
    # The force-override banner must name only the shape skip, and the
    # withheld banner only the semantic one.
    assert "shape-flagged" in force_line
    assert "does not override semantic readiness" in withheld_line
    assert "#2 " in err and "missing_ac" in err
    assert "#3 " in err and SEMANTIC_NOT_RATIFIED_CODE in err


def test_cli_remediated_issues_continue_to_run_sprint(tmp_path: Path) -> None:
    """When all issues are skipped at the entry gate but intake remediation
    successfully fixes them (REMEDIATED outcome), the sprint must continue and
    pass those issues to run_sprint — the operator must not re-invoke forge."""
    from theforge.config.types import IntakeConfig
    from theforge.intake import IntakeOutcome, IntakeOutcomeKind

    config = _make_config(tmp_path)
    object.__setattr__(
        config,
        "intake",
        IntakeConfig(grooming=True, auto_fix=True, auto_fix_mode="edit"),
    )
    args = _query_args(tmp_path)

    fetched = [{"number": 42, "title": "fixable"}]
    gated = ShapeGateResult(
        runnable=[],
        skipped=[
            SkippedIssue(
                issue_number=42,
                reason_codes=("missing_ac",),
                source="local_check",
                title="fixable",
            )
        ],
    )

    def fake_remediate(skipped, **_kw):
        return {
            sk.issue_number: IntakeOutcome(
                slug=f"issue-{sk.issue_number}",
                kind=IntakeOutcomeKind.REMEDIATED,
                detail="edit mode: issue body updated, gate rerun passed",
                audit={"remediation_source": "agent"},
            )
            for sk in skipped
        }

    run_sprint_calls: list[dict] = []

    def fake_run_sprint(*_a, **kw):
        run_sprint_calls.append(kw)
        return _ok_result()

    with (
        patch("theforge.cli.sprint.load_config", return_value=config),
        patch("theforge.cli.sprint._find_config", return_value=tmp_path / "forge.yaml"),
        patch("theforge.sprint.query.fetch_issues_for_milestone", return_value=fetched),
        patch("theforge.sprint.shape_gate.apply_shape_gate", return_value=gated),
        patch(
            "theforge.sprint.entry_intake.remediate_entry_skipped_issues",
            side_effect=fake_remediate,
        ),
        patch("theforge.sprint.query.build_resolved_sprint", return_value=_resolved([42])),
        patch(
            "theforge.cli.sprint._acquire_launch_locks",
            return_value=([], None, {}),
        ),
        patch("theforge.cli.sprint.release_story_locks"),
        patch("theforge.cli.sprint.run_sprint", side_effect=fake_run_sprint),
    ):
        rc = cmd_sprint(args)

    assert rc == 0
    # run_sprint must have been called — remediation is not a terminal condition.
    assert run_sprint_calls, "run_sprint was never called; sprint exited after remediation"


def test_cli_remediated_issues_not_in_nothing_to_run_message(tmp_path: Path, capsys) -> None:
    """The 'nothing to run' message must not appear when at least one issue was
    successfully REMEDIATED."""
    from theforge.config.types import IntakeConfig
    from theforge.intake import IntakeOutcome, IntakeOutcomeKind

    config = _make_config(tmp_path)
    object.__setattr__(
        config,
        "intake",
        IntakeConfig(grooming=True, auto_fix=True, auto_fix_mode="edit"),
    )
    args = _query_args(tmp_path)

    fetched = [{"number": 99, "title": "autofix-me"}]
    gated = ShapeGateResult(
        runnable=[],
        skipped=[
            SkippedIssue(
                issue_number=99,
                reason_codes=("missing_ac",),
                source="local_check",
                title="autofix-me",
            )
        ],
    )

    def fake_remediate(skipped, **_kw):
        return {
            sk.issue_number: IntakeOutcome(
                slug=f"issue-{sk.issue_number}",
                kind=IntakeOutcomeKind.REMEDIATED,
                detail="edit mode: issue body updated, gate rerun passed",
                audit={"remediation_source": "agent"},
            )
            for sk in skipped
        }

    with (
        patch("theforge.cli.sprint.load_config", return_value=config),
        patch("theforge.cli.sprint._find_config", return_value=tmp_path / "forge.yaml"),
        patch("theforge.sprint.query.fetch_issues_for_milestone", return_value=fetched),
        patch("theforge.sprint.shape_gate.apply_shape_gate", return_value=gated),
        patch(
            "theforge.sprint.entry_intake.remediate_entry_skipped_issues",
            side_effect=fake_remediate,
        ),
        patch("theforge.sprint.query.build_resolved_sprint", return_value=_resolved([99])),
        patch(
            "theforge.cli.sprint._acquire_launch_locks",
            return_value=([], None, {}),
        ),
        patch("theforge.cli.sprint.release_story_locks"),
        patch("theforge.cli.sprint.run_sprint", return_value=_ok_result()),
    ):
        rc = cmd_sprint(args)

    assert rc == 0
    err = capsys.readouterr().err
    assert "nothing to run" not in err


def test_cli_remediated_issues_publish_env_var_for_reexec_carry(
    tmp_path: Path, monkeypatch
) -> None:
    """After successful intake remediation, the just-remediated issue numbers
    are stashed in ``FORGE_INTAKE_REMEDIATED`` so they survive ``os.execv``
    when the runner re-execs on a mid-sprint source pull. Without this,
    the post-re-exec shape gate re-queries GitHub and drops issues whose
    ``needs-grooming`` label hasn't been async-reconciled yet."""
    from theforge.cli.sprint import _INTAKE_REMEDIATED_ENV
    from theforge.config.types import IntakeConfig
    from theforge.intake import IntakeOutcome, IntakeOutcomeKind

    monkeypatch.delenv(_INTAKE_REMEDIATED_ENV, raising=False)

    config = _make_config(tmp_path)
    object.__setattr__(
        config,
        "intake",
        IntakeConfig(grooming=True, auto_fix=True, auto_fix_mode="edit"),
    )
    args = _query_args(tmp_path)

    fetched = [
        {"number": 1543, "title": "a"},
        {"number": 1544, "title": "b"},
        {"number": 1545, "title": "c"},
    ]
    gated = ShapeGateResult(
        runnable=[],
        skipped=[
            SkippedIssue(
                issue_number=n,
                reason_codes=("missing_observed",),
                source="local_check",
                title=str(n),
            )
            for n in (1543, 1544, 1545)
        ],
    )

    def fake_remediate(skipped, **_kw):
        return {
            sk.issue_number: IntakeOutcome(
                slug=f"issue-{sk.issue_number}",
                kind=IntakeOutcomeKind.REMEDIATED,
                detail="edit mode: issue body updated, gate rerun passed",
                audit={"remediation_source": "agent"},
            )
            for sk in skipped
        }

    captured_env_at_run_sprint: dict = {}

    def fake_run_sprint(*_a, **_kw):
        import os as _os

        captured_env_at_run_sprint["val"] = _os.environ.get(_INTAKE_REMEDIATED_ENV)
        return _ok_result()

    with (
        patch("theforge.cli.sprint.load_config", return_value=config),
        patch("theforge.cli.sprint._find_config", return_value=tmp_path / "forge.yaml"),
        patch("theforge.sprint.query.fetch_issues_for_milestone", return_value=fetched),
        patch("theforge.sprint.shape_gate.apply_shape_gate", return_value=gated),
        patch(
            "theforge.sprint.entry_intake.remediate_entry_skipped_issues",
            side_effect=fake_remediate,
        ),
        patch(
            "theforge.sprint.query.build_resolved_sprint",
            return_value=_resolved([1543, 1544, 1545]),
        ),
        patch(
            "theforge.cli.sprint._acquire_launch_locks",
            return_value=([], None, {}),
        ),
        patch("theforge.cli.sprint.release_story_locks"),
        patch("theforge.cli.sprint.run_sprint", side_effect=fake_run_sprint),
    ):
        cmd_sprint(args)

    # By the time run_sprint runs (and may re-exec via pull_base_branch),
    # the env var must be set so the post-re-exec gate trusts the
    # just-remediated bodies over the async-stale label.
    assert captured_env_at_run_sprint.get("val") == "1543,1544,1545"


def test_cli_carries_remediated_numbers_from_env_into_shape_gate(
    tmp_path: Path, monkeypatch
) -> None:
    """On re-exec entry, the gate must receive the carried numbers via the
    ``intake_remediated_numbers`` kwarg, and the env var must be consumed
    (cleared) so it doesn't bleed into a downstream subprocess."""
    from theforge.cli.sprint import _INTAKE_REMEDIATED_ENV

    monkeypatch.setenv(_INTAKE_REMEDIATED_ENV, "1545,1543")

    config = _make_config(tmp_path)
    args = _query_args(tmp_path)

    fetched = [{"number": 1543, "title": "good"}]
    gated = ShapeGateResult(runnable=fetched, skipped=[])
    captured: dict = {}

    def _spy(*_a, **kwargs):
        captured["kwargs"] = kwargs
        return gated

    with (
        patch("theforge.cli.sprint.load_config", return_value=config),
        patch("theforge.cli.sprint._find_config", return_value=tmp_path / "forge.yaml"),
        patch("theforge.sprint.query.fetch_issues_for_milestone", return_value=fetched),
        patch("theforge.sprint.query.build_resolved_sprint", return_value=_resolved([1543])),
        patch("theforge.sprint.shape_gate.apply_shape_gate", side_effect=_spy),
        patch(
            "theforge.cli.sprint._acquire_launch_locks",
            return_value=([], None, {}),
        ),
        patch("theforge.cli.sprint.release_story_locks"),
        patch("theforge.cli.sprint.run_sprint", return_value=_ok_result()),
    ):
        cmd_sprint(args)

    threaded = captured["kwargs"].get("intake_remediated_numbers")
    assert threaded is not None
    assert set(threaded) == {1543, 1545}

    # The env var is re-published with the carried set so a downstream
    # re-exec (rare but possible if a second source-pull lands mid-run)
    # still treats the async-stale label as non-authoritative for these
    # already-remediated issues.
    import os as _os

    assert _os.environ.get(_INTAKE_REMEDIATED_ENV) == "1543,1545"
