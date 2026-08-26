"""Conformance: every issue-body producer validates before it mutates.

Two guards, because either alone is escapable. The first enumerates the known
producers and proves each one refuses at its own mutation seam. The second
scans the repository for issue-body mutation seams and fails when one appears
that no registered producer accounts for — so a newly added producer that skips
validation fails this suite rather than being discovered in the corpus.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from theforge.shape_check.producer import PRODUCERS

REPO_ROOT = Path(__file__).resolve().parents[1]

# ── Static scan ───────────────────────────────────────────────────────
# Tolerant of both spellings a mutation seam is written in: a shell command
# (`gh issue create --body ...`) and a Python argv list, where the same tokens
# are quoted, comma-separated and split across lines. Quotes, commas and
# brackets are erased before matching so one pattern reads both.
_MUTATION_RE = re.compile(
    r"gh\s+issue\s+(?:create|edit)(?:(?!gh\s+issue).){0,400}?--body(?:-file)?\b", re.S
)
_STRIP_RE = re.compile(r"[\"'\\,\[\]]")

#: Every file holding an issue-body mutation seam, mapped to the producer ids
#: that account for it. Adding a seam without adding it here fails the scan.
APPROVED_SEAMS: dict[str, tuple[str, ...]] = {
    "src/theforge/cli/shape.py": ("forge-shape",),
    "src/theforge/cli/todo.py": ("forge-todo-create", "forge-todo-triage"),
    "src/theforge/cli/report.py": ("forge-report-create", "forge-report-update"),
    "src/theforge/cli/hooks.py": ("post-run-hook-finding",),
    "src/theforge/intake/groom_flow.py": ("forge-groom",),
    "src/theforge/intake/remediation.py": (
        "forge-intake-autofix",
        "forge-intake-reopen-context",
    ),
    "src/theforge/advisory_conventions.py": ("forge-advisory-finding",),
    "src/theforge/coordinator/diagnose_flow.py": ("forge-diagnose",),
    ".forge/hooks/post_run.sh": ("post-run-hook-finding",),
    ".forge/hooks/post-run-gh-issues.sh": ("post-run-hook-finding",),
}


def _scan_sources() -> list[Path]:
    return sorted(
        [
            *(REPO_ROOT / "src" / "theforge").rglob("*.py"),
            *(REPO_ROOT / ".forge" / "hooks").glob("*.sh"),
        ]
    )


def _has_mutation_seam(text: str) -> bool:
    return bool(_MUTATION_RE.search(re.sub(r"\s+", " ", _STRIP_RE.sub(" ", text))))


class TestStaticSeamScan:
    def test_scan_is_not_vacuous(self):
        """A broken matcher must fail loudly, not report zero producers."""
        found = [p for p in _scan_sources() if _has_mutation_seam(p.read_text(encoding="utf-8"))]
        assert len(found) >= len(APPROVED_SEAMS), (
            "the mutation-seam scan matched fewer files than the approved set — "
            f"the matcher is broken. matched: {[str(p) for p in found]}"
        )

    def test_every_mutation_seam_is_an_approved_producer(self):
        found = {
            str(p.relative_to(REPO_ROOT))
            for p in _scan_sources()
            if _has_mutation_seam(p.read_text(encoding="utf-8"))
        }
        unaccounted = found - set(APPROVED_SEAMS)
        assert not unaccounted, (
            "these files create or edit an issue body but no registered producer "
            f"accounts for them: {sorted(unaccounted)}. Register a producer in "
            "shape_check.producer.PRODUCERS, validate before the mutation, and add "
            "the file to APPROVED_SEAMS."
        )

    def test_every_approved_seam_still_exists(self):
        found = {
            str(p.relative_to(REPO_ROOT))
            for p in _scan_sources()
            if _has_mutation_seam(p.read_text(encoding="utf-8"))
        }
        assert set(APPROVED_SEAMS) - found == set()

    @pytest.mark.parametrize(("path", "producers"), sorted(APPROVED_SEAMS.items()))
    def test_each_seam_names_its_producer(self, path, producers):
        text = (REPO_ROOT / path).read_text(encoding="utf-8")
        for producer in producers:
            assert producer in text, (
                f"{path} holds an issue-body mutation seam but never names producer "
                f"{producer!r} — it cannot be validating through the shared boundary."
            )

    def test_every_registered_producer_owns_a_seam(self):
        registered = set(PRODUCERS)
        accounted = {p for producers in APPROVED_SEAMS.values() for p in producers}
        assert registered == accounted, (
            "PRODUCERS and APPROVED_SEAMS disagree; "
            f"unmapped: {sorted(registered - accounted)}, "
            f"unregistered: {sorted(accounted - registered)}"
        )


# ── Per-producer refusal at the mutation seam ─────────────────────────

_RUNNABLE_TASK_BODY = (
    "## Why\n\nOperators cannot see it.\n\n"
    "## Acceptance criteria\n\n- The command prints the resolved path.\n\n"
    "## Example\n\n```\n$ forge thing\n/tmp/thing\n```\n"
)


class TestShapeRefusesBeforeApplying:
    def test_unvalidatable_classification_never_reaches_the_mutation(self, tmp_path, capsys):
        from theforge.cli import shape as shape_cli
        from theforge.intake.shape_classify import Classification, Confidence, ShapeProposal

        (tmp_path / "forge.yaml").write_text("project: {}\n", encoding="utf-8")
        args = argparse.Namespace(
            config=str(tmp_path / "forge.yaml"),
            issue=7,
            from_brief=None,
            from_stdin=False,
            apply=True,
            next=False,
        )
        proposal = ShapeProposal(classification=Classification.EPIC, confidence=Confidence.HIGH)
        with (
            patch.object(shape_cli, "_load_issue", return_value=("t", "prose", ["epic"])),
            patch.object(shape_cli, "classify", return_value=proposal),
            patch.object(shape_cli, "restructure_body", return_value="a different body"),
            patch.object(shape_cli, "_apply_to_github") as mock_apply,
        ):
            rc = shape_cli.cmd_shape(args)

        assert rc == 1
        assert not mock_apply.called
        assert "no declared lifecycle state" in capsys.readouterr().err


class TestGroomRefusesBeforeEditing:
    def test_a_repair_that_introduces_a_refusal_never_reaches_edit(self, tmp_path):
        from theforge.intake import groom_flow

        original = _RUNNABLE_TASK_BODY
        degraded = original + (
            "\n## Implementation plan\n\n"
            "1. Patch `src/a.py:42`\n2. Patch `src/b.py:17`\n3. Patch `src/c.py:9`\n"
        )

        def fetch(number, project_root):
            return {"title": "add export", "body": original, "labels": ["task"]}

        edits: list[tuple] = []

        def edit(number, body, project_root):
            edits.append((number, body))
            return True

        with patch.object(groom_flow, "_restructure_feature_body", return_value=degraded):
            with pytest.raises(groom_flow.GroomError, match="forge-groom"):
                groom_flow.run_groom(
                    "12",
                    apply_changes=True,
                    project_root=tmp_path,
                    fetch_issue=fetch,
                    edit_issue_body=edit,
                )
        assert edits == []

    def test_a_conforming_repair_still_edits(self, tmp_path):
        from theforge.intake import groom_flow

        original = "## Why\n\nOperators cannot see it.\n"

        def fetch(number, project_root):
            return {"title": "add export", "body": original, "labels": ["task"]}

        edits: list[tuple] = []

        def edit(number, body, project_root):
            edits.append((number, body))
            return True

        result = groom_flow.run_groom(
            "12",
            apply_changes=True,
            project_root=tmp_path,
            fetch_issue=fetch,
            edit_issue_body=edit,
        )
        assert result.applied
        assert edits


class TestDiagnoseRefusesBeforeEditing:
    def _artifact(self):
        from theforge.diagnose_types import DiagnosisArtifact

        return DiagnosisArtifact(
            issue_number=44,
            observed_symptom="the export drops rows",
            reproduction_or_evidence="run log at `src/c.py:9`",
            hypotheses=(),
            confirmed_cause="the loader in `src/a.py:42` drops it",
            affected_code_path="src/b.py:17",
            fix_success_criterion="no rows are dropped",
        )

    def test_body_section_citations_on_a_non_bug_issue_report_rather_than_write(self, tmp_path):
        from theforge.coordinator import diagnose_flow
        from theforge.diagnose_types import DiagnoseState

        state = DiagnoseState(
            issue_number=44, issue_title="add export", issue_body=_RUNNABLE_TASK_BODY
        )
        with patch.object(diagnose_flow, "_gh_edit_body") as mock_edit:
            with pytest.raises(Exception, match="forge-diagnose"):
                diagnose_flow._land_artifact(
                    state,
                    self._artifact(),
                    "body_section",
                    tmp_path,
                    issue_labels=["enhancement"],
                )
        assert not mock_edit.called

    def test_the_same_landing_on_a_bug_issue_writes(self, tmp_path):
        from theforge.coordinator import diagnose_flow
        from theforge.diagnose_types import DiagnoseState

        state = DiagnoseState(
            issue_number=44,
            issue_title="export drops rows",
            issue_body="## Observed\n\nrows vanish\n\n## Expected\n\nrows survive\n",
        )
        with patch.object(diagnose_flow, "_gh_edit_body") as mock_edit:
            diagnose_flow._land_artifact(
                state, self._artifact(), "body_section", tmp_path, issue_labels=["bug"]
            )
        assert mock_edit.called


class TestReportRefusesBeforeCreating:
    """Seam-level refusal lives in ``test_cli_report.py``, where the observing-
    project fixtures are; these cover the declaration helper's own edges."""

    def test_a_default_cause_report_declares_the_investigation_ready_state(self):
        from theforge.cli.report import _declared_verdict
        from theforge.reporting.render import Diagnosis
        from theforge.shape_check.types import ShapeVerdict

        assert (
            _declared_verdict(Diagnosis(symptom="it broke"))
            is ShapeVerdict.DIAGNOSIS_CAUSE_UNKNOWN
        )
        assert (
            _declared_verdict(Diagnosis(symptom="it broke", cause="the loader drops it"))
            is ShapeVerdict.RUNNABLE
        )

    def test_an_unnameable_target_verdict_refuses_rather_than_filing(self, capsys):
        from theforge.cli import report as report_cli
        from theforge.reporting.target_gate import TargetGateVerdict

        unnameable = TargetGateVerdict(
            repo="o/r", ref="main", sha="abc123def456", verdict="who_knows", shape=None, reasons=()
        )
        assert (
            report_cli._check_declaration(
                producer="forge-report-create",
                declared=report_cli.ShapeVerdict.RUNNABLE,
                verdict=unnameable,
            )
            is None
        )
        assert "not a state this producer can declare" in capsys.readouterr().err


class TestTodoRefusesBeforeMutating:
    def _args(self, tmp_path, **kwargs):
        (tmp_path / "forge.yaml").write_text("project: {}\n", encoding="utf-8")
        base = {
            "config": str(tmp_path / "forge.yaml"),
            "from_sprint": None,
            "issue": None,
            "run_id": None,
            "todo_action": None,
            "todo_args": [],
            "number": None,
            "text": None,
        }
        base.update(kwargs)
        return argparse.Namespace(**base)

    def test_create_validates_before_gh_runs(self, tmp_path):
        from theforge.cli import todo as todo_cli

        with patch.object(todo_cli, "_run_gh") as mock_gh:
            mock_gh.return_value = type(
                "P", (), {"returncode": 0, "stdout": "url", "stderr": ""}
            )()
            rc = todo_cli._create_todo(self._args(tmp_path, text="capture this"))
        assert rc == 0
        assert mock_gh.called

    def test_create_refuses_when_the_draft_state_changes(self, tmp_path, capsys):
        from theforge.cli import todo as todo_cli

        with (
            patch.object(todo_cli, "TODO_DRAFT_VERDICT", todo_cli.ShapeVerdict.RUNNABLE),
            patch.object(todo_cli, "_run_gh") as mock_gh,
        ):
            rc = todo_cli._create_todo(self._args(tmp_path, text="capture this"))
        assert rc == 1
        assert not mock_gh.called
        assert "forge-todo-create" in capsys.readouterr().err

    def test_triage_body_edit_refuses_and_leaves_the_body_unchanged(
        self, tmp_path, monkeypatch, capsys
    ):
        from theforge.cli import todo as todo_cli

        viewed = json.dumps(
            {"title": "add export", "body": _RUNNABLE_TASK_BODY, "labels": [{"name": "task"}]}
        )
        degraded = _RUNNABLE_TASK_BODY + (
            "\n## Implementation plan\n\n"
            "1. Patch `src/a.py:42`\n2. Patch `src/b.py:17`\n3. Patch `src/c.py:9`\n"
        )
        calls: list[list[str]] = []

        def fake_gh(command, project_root):
            calls.append(command)
            stdout = viewed if command[2] == "view" else ""
            return type("P", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

        def fake_editor(command, cwd=None):
            Path(command[-1]).write_text(degraded, encoding="utf-8")
            return type("P", (), {"returncode": 0})()

        monkeypatch.setattr(todo_cli, "_run_gh", fake_gh)
        monkeypatch.setattr(todo_cli.subprocess, "run", fake_editor)
        responses = iter(["", "", "y", "n"])
        monkeypatch.setattr("builtins.input", lambda _p: next(responses))

        rc = todo_cli._triage_todo(self._args(tmp_path, todo_action="triage", number=12))

        assert rc == 1
        # The editor path really ran — the refusal is at the write, not earlier.
        assert any(call[2] == "view" for call in calls)
        assert not any("--body-file" in call for call in calls)
        err = capsys.readouterr().err
        assert "forge-todo-triage" in err
        assert "left unchanged" in err


class TestAdvisoryFilerRefusesBeforeCreating:
    def test_a_non_runnable_body_is_not_filed(self, tmp_path, capsys):
        from theforge import advisory_conventions

        entry = {
            "rule": "module_lines",
            "file": "src/theforge/big.py",
            "detail": "too long",
            "line_count": 900,
            "limit": 600,
            "gap": 300,
            "last_seen": "2026-08-26T00:00:00Z",
        }
        config = type(
            "C",
            (),
            {
                "conventions_advisory": type(
                    "A",
                    (),
                    {
                        "issue_filing": type(
                            "F",
                            (),
                            {
                                "enabled": True,
                                "threshold_percent": 0,
                                "label": "tech-debt",
                                "milestone": None,
                            },
                        )()
                    },
                )(),
                "project_root": tmp_path,
            },
        )()

        with (
            patch.object(advisory_conventions, "_render_issue_body", return_value="prose only"),
            patch.object(advisory_conventions.subprocess, "run") as mock_run,
        ):
            result = advisory_conventions._maybe_file_issue(config, entry)

        assert result is None
        assert not mock_run.called
        assert "forge-advisory-finding" in capsys.readouterr().err


class TestIntakeRemediationRefusesBeforeEditing:
    def test_reopen_context_fold_in_that_degrades_the_body_is_dropped(self, tmp_path):
        from theforge.intake import remediation

        edits: list[tuple] = []

        def edit_body(number, body, project_root):
            edits.append((number, body))
            return True

        def fetch_detail(number, project_root):
            return {
                "title": "add export",
                "body": _RUNNABLE_TASK_BODY,
                "labels": [{"name": "task"}],
                "timeline": [],
            }

        degraded = _RUNNABLE_TASK_BODY + (
            "\n## Implementation plan\n\n"
            "1. Patch `src/a.py:42`\n2. Patch `src/b.py:17`\n3. Patch `src/c.py:9`\n"
        )
        with (
            patch(
                "theforge.sprint.reopen_context.analyze_reopen_contract",
                return_value=type("S", (), {"has_reopen_context": True})(),
            ),
            patch("theforge.sprint.reopen_context.append_reopen_context", return_value=degraded),
        ):
            outcome = remediation.remediate_shape_gate_skip(
                issue_number=12,
                reason_codes=("reopened_stale_contract",),
                project_root=tmp_path,
                auto_fix_enabled=True,
                auto_fix_mode="edit",
                fetch_detail=fetch_detail,
                edit_body=edit_body,
            )

        assert outcome.kind is remediation.ShapeGateSkipRemediationKind.DROPPED_AFTER_FIX
        assert "forge-intake-reopen-context" in (outcome.detail or "")
        assert edits == []


class TestIntakeAutoFixValidatesBeforeEditing:
    """Auto-fix's own rerun gate already drops a rewrite that still has blocking
    findings. The declared-state check sits behind it as the last thing between
    a candidate body and ``gh issue edit``, so a rewrite that clears the rerun
    gate but lands outside the state auto-fix declared is still not written."""

    def _remediate(self, tmp_path, *, replacement, edits):
        from theforge.intake import remediation

        def edit_body(number, body, project_root):
            edits.append((number, body))
            return True

        def agent_caller(body, findings, comments):
            from theforge.intake.agent_rewrite import AgentRewriteResult

            return AgentRewriteResult(replacement=replacement, detail="", cost_usd=0.0)

        return remediation._remediate_one(
            slug="issue-12",
            issue_number=12,
            title="add export",
            body="Prose only, no acceptance criteria.\n",
            labels=["task"],
            comments=[],
            grooming_enabled=False,
            auto_fix_enabled=True,
            auto_fix_mode="edit",
            agent_caller=agent_caller,
            missing_agent_detail="no agent",
            post_comment=lambda *a: True,
            edit_body=edit_body,
            project_root=tmp_path,
        )

    def test_a_conforming_rewrite_is_written(self, tmp_path):
        from theforge.intake import remediation

        edits: list[tuple] = []
        outcome = self._remediate(tmp_path, replacement=_RUNNABLE_TASK_BODY, edits=edits)

        assert outcome.kind is remediation.IntakeOutcomeKind.REMEDIATED
        assert edits and edits[0][1] == _RUNNABLE_TASK_BODY

    def test_the_declared_state_check_gates_the_edit_seam(self, tmp_path):
        """The validator runs before ``edit_body``, not after it."""
        from theforge.intake import remediation
        from theforge.shape_check.producer import compare_declaration
        from theforge.shape_check.types import ShapeVerdict

        refusal = compare_declaration(
            producer="forge-intake-autofix",
            declared=ShapeVerdict.RUNNABLE,
            actual=ShapeVerdict.NEEDS_GROOMING_MISSING_AC,
            strict=True,
        )
        assert not refusal.conforms

        edits: list[tuple] = []
        with patch.object(remediation, "validate_issue_body", return_value=refusal) as mock_check:
            outcome = self._remediate(tmp_path, replacement=_RUNNABLE_TASK_BODY, edits=edits)

        assert mock_check.called
        assert edits == []
        assert outcome.kind is remediation.IntakeOutcomeKind.DROPPED_AFTER_FIX
        assert "forge-intake-autofix" in (outcome.detail or "")
        assert outcome.proposed_replacement == _RUNNABLE_TASK_BODY


class TestGeneratedHookFailsClosed:
    """The hook ships into repositories whose environment forge does not control."""

    def _run_hook(self, tmp_path, hook: Path, extra_env: dict[str, str]):
        import os
        import shutil
        import subprocess

        if shutil.which("jq") is None:
            pytest.skip("real jq required to execute the finding hook")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        gh_log = tmp_path / "gh.log"
        gh = bin_dir / "gh"
        gh.write_text(
            "#!/usr/bin/env bash\n"
            'for a in "$@"; do printf "%s\\n" "$a" >> "$GH_LOG"; done\n'
            "exit 0\n",
            encoding="utf-8",
        )
        gh.chmod(0o755)
        payload = {
            "verdict": "APPROVE",
            "slug": "issue-test",
            "branch": "fix/test",
            "summary": "approved with one finding",
            "findings": [
                {
                    "severity": "P1",
                    "file": "src/example.py",
                    "line": 12,
                    "description": "the loader drops the dep",
                    "observed": "the loader silently drops the dep",
                    "expected": "the loader surfaces the malformed file",
                    "evidence": "src/example.py near line 12",
                    "suggestion": "",
                }
            ],
        }
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GH_LOG": str(gh_log),
            **extra_env,
        }
        env.pop("FORGE_GH_PR_REVIEWS", None)
        proc = subprocess.run(
            ["bash", str(hook)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
            cwd=REPO_ROOT,
            check=False,
        )
        return proc, gh_log.read_text(encoding="utf-8") if gh_log.exists() else ""

    @pytest.mark.parametrize(
        "hook_name", [".forge/hooks/post_run.sh", ".forge/hooks/post-run-gh-issues.sh"]
    )
    def test_an_unavailable_validator_skips_filing_rather_than_reverting_to_unvalidated(
        self, tmp_path, hook_name
    ):
        hook = REPO_ROOT / hook_name
        if not hook.exists():
            pytest.skip(f"{hook_name} missing on this checkout")
        proc, log = self._run_hook(
            tmp_path, hook, {"FORGE_PYTHON": str(tmp_path / "no-such-python")}
        )
        assert proc.returncode == 0, proc.stderr
        assert "issue\ncreate" not in log, "a finding was filed without a usable validator"
        assert "not filing finding" in proc.stderr
        assert "post-run-hook-finding" in proc.stderr

    @pytest.mark.parametrize(
        "hook_name", [".forge/hooks/post_run.sh", ".forge/hooks/post-run-gh-issues.sh"]
    )
    def test_a_conforming_finding_is_filed_when_the_validator_runs(self, tmp_path, hook_name):
        import sys as _sys

        hook = REPO_ROOT / hook_name
        if not hook.exists():
            pytest.skip(f"{hook_name} missing on this checkout")
        proc, log = self._run_hook(tmp_path, hook, {"FORGE_PYTHON": _sys.executable})
        assert proc.returncode == 0, proc.stderr
        assert "issue\ncreate" in log
        assert "bug" in log.splitlines()
