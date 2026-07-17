"""Tests for diagnose starting-evidence auto-injection.

Covers:
- reference detection (run/sprint ids, branches, PR/issue numbers, audit paths)
- bounded excerpting (line/char/count caps)
- best-effort no-op when nothing is recognizable
- fixture comparison: an issue citing a run_id pre-loads the run-log excerpt
  the agent would otherwise have had to discover, vs. an issue with no
  references (which gets no injected section at all)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from theforge.coordinator.diagnose_evidence import (
    _MAX_LOG_TAIL_LINES,
    _MAX_TOTAL_EVIDENCE_CHARS,
    build_starting_evidence,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestNoReferences:
    def test_empty_body_yields_empty_evidence(self, tmp_path):
        ev = build_starting_evidence(issue_body="", project_root=tmp_path)
        assert ev.is_empty
        assert ev.text == ""
        assert ev.reference_labels == []

    def test_body_with_no_recognizable_refs_is_noop(self, tmp_path):
        ev = build_starting_evidence(
            issue_body="Something is broken when I run the thing twice.",
            project_root=tmp_path,
        )
        assert ev.is_empty

    def test_reference_that_resolves_to_nothing_is_noop(self, tmp_path):
        # A well-formed run id that has no matching log on disk → best-effort
        # produces no section (behaves the same as today).
        ev = build_starting_evidence(
            issue_body="Sprint run 7cf3f238d8d8 failed to land.",
            project_root=tmp_path,
        )
        assert ev.is_empty


class TestRunLogEvidence:
    def test_run_id_preloads_log_tail(self, tmp_path):
        run_id = "7cf3f238d8d8"
        lines = "\n".join(f"[forge] line {i}" for i in range(100))
        _write(tmp_path / ".forge" / "logs" / "issues-1135,1409" / f"run-{run_id}.log", lines)

        ev = build_starting_evidence(
            issue_body=f"Sprint run {run_id} (issues-1135,1409): issue-1135 reached APPROVE",
            project_root=tmp_path,
        )
        assert not ev.is_empty
        assert "== STARTING EVIDENCE" in ev.text
        assert f"run-{run_id}.log" in ev.text
        assert "line 99" in ev.text  # tail present
        assert "line 0" not in ev.text  # head dropped by the tail bound
        assert any("run log" in lbl for lbl in ev.reference_labels)

    def test_run_log_tail_is_bounded_to_max_lines(self, tmp_path):
        run_id = "abcabcabcabc"
        lines = "\n".join(f"row{i}" for i in range(500))
        _write(tmp_path / ".forge" / "logs" / "sprintX" / f"run-{run_id}.log", lines)

        ev = build_starting_evidence(issue_body=f"run {run_id}", project_root=tmp_path)
        # Only the last _MAX_LOG_TAIL_LINES rows appear.
        assert f"row{500 - _MAX_LOG_TAIL_LINES}" in ev.text
        assert f"row{500 - _MAX_LOG_TAIL_LINES - 1}" not in ev.text


class TestSprintStateEvidence:
    def test_sprint_id_preloads_state(self, tmp_path):
        sprint_id = "00f8b889f953"
        _write(
            tmp_path / ".forge" / "sprints" / sprint_id / "state.yaml",
            "sprint_id: 00f8b889f953\nissues: [1, 2, 3]\nstatus: running\n",
        )
        ev = build_starting_evidence(
            issue_body=f"See sprint {sprint_id} state.", project_root=tmp_path
        )
        assert not ev.is_empty
        assert "state.yaml" in ev.text
        assert "status: running" in ev.text


class TestHistoryEvidence:
    def test_history_lines_mentioning_run_id_are_loaded(self, tmp_path):
        run_id = "deadbeef0000"
        history = (
            '{"run": "other", "outcome": "DONE"}\n'
            f'{{"run": "{run_id}", "landing_status": "landed"}}\n'
            '{"run": "unrelated"}\n'
        )
        _write(tmp_path / ".forge" / "audits" / "history.jsonl", history)
        ev = build_starting_evidence(
            issue_body=f"history.jsonl audit for {run_id} shows landing_status",
            project_root=tmp_path,
        )
        assert "history.jsonl" in ev.text
        assert "landing_status" in ev.text


class TestForgePathEvidence:
    def test_explicit_forge_path_is_loaded(self, tmp_path):
        _write(
            tmp_path / ".forge" / "audits" / "sprint-audit.yaml",
            "kind: sprint\nsucceeded: 2\nfailed: 0\n",
        )
        ev = build_starting_evidence(
            issue_body="The .forge/audits/sprint-audit.yaml shows the wrong count.",
            project_root=tmp_path,
        )
        assert "sprint-audit.yaml" in ev.text
        assert "succeeded: 2" in ev.text


class TestGhBackedEvidence:
    def test_branch_reference_loads_pr_history(self, tmp_path):
        pr_json = '[{"number": 1143, "state": "MERGED", "title": "x", "mergedAt": "2026-04-30"}]'

        def fake_run_gh(args, project_root):
            assert args[:2] == ["pr", "list"]
            assert "feat/issue-1135" in args
            return pr_json

        with patch("theforge.coordinator.diagnose_evidence._run_gh", side_effect=fake_run_gh):
            ev = build_starting_evidence(
                issue_body="Branch feat/issue-1135 never shipped.",
                project_root=tmp_path,
            )
        assert "feat/issue-1135" in ev.text
        assert "1143" in ev.text

    def test_pr_number_loads_view_and_skips_self_issue(self, tmp_path):
        calls = []

        def fake_run_gh(args, project_root):
            calls.append(args)
            if args[:2] == ["pr", "view"] and args[2] == "1143":
                return '{"number": 1143, "state": "MERGED"}'
            return None

        with patch("theforge.coordinator.diagnose_evidence._run_gh", side_effect=fake_run_gh):
            ev = build_starting_evidence(
                issue_body="Related to #1143 but this is issue #1420.",
                project_root=tmp_path,
                self_issue_number=1420,
            )
        assert "1143" in ev.text
        # The self-issue (#1420) must not be queried.
        assert not any(a[:2] == ["pr", "view"] and a[2] == "1420" for a in calls)

    def test_gh_failure_fails_open(self, tmp_path):
        with patch("theforge.coordinator.diagnose_evidence._run_gh", return_value=None):
            ev = build_starting_evidence(
                issue_body="Branch feat/issue-9 and PR #99.",
                project_root=tmp_path,
            )
        assert ev.is_empty

    def test_many_unresolved_refs_are_gh_call_bounded(self, tmp_path):
        # A body citing far more branches and #NNNN than the attempt cap must
        # not fire an unbounded number of (up-to-30s) gh subprocesses before the
        # agent starts. All unresolved → gh returns None every time.
        from theforge.coordinator.diagnose_evidence import _MAX_GH_ATTEMPTS_PER_KIND

        branches = " ".join(f"feat/issue-{i}" for i in range(40))
        prs = " ".join(f"#{2000 + i}" for i in range(40))
        calls = []

        def fake_run_gh(args, project_root):
            calls.append(args)
            return None  # every reference unresolved

        with patch("theforge.coordinator.diagnose_evidence._run_gh", side_effect=fake_run_gh):
            ev = build_starting_evidence(
                issue_body=f"{branches}\n{prs}",
                project_root=tmp_path,
            )
        assert ev.is_empty
        branch_calls = [a for a in calls if a[:2] == ["pr", "list"]]
        # A #NNNN attempt is a PR view then (on failure) an issue view: 2 gh calls.
        pr_calls = [a for a in calls if a[:2] == ["pr", "view"]]
        issue_calls = [a for a in calls if a[:2] == ["issue", "view"]]
        assert len(branch_calls) == _MAX_GH_ATTEMPTS_PER_KIND
        assert len(pr_calls) == _MAX_GH_ATTEMPTS_PER_KIND
        assert len(issue_calls) == _MAX_GH_ATTEMPTS_PER_KIND


class TestBounding:
    def test_total_block_is_capped(self, tmp_path):
        # Many run logs, each large, must not produce an unbounded block.
        body_refs = []
        for i in range(8):
            rid = format(0xA0000000000 + i, "012x")  # distinct 12-char hex ids
            body_refs.append(rid)
            _write(
                tmp_path / ".forge" / "logs" / f"s{i}" / f"run-{rid}.log",
                "\n".join("X" * 200 for _ in range(50)),
            )
        ev = build_starting_evidence(issue_body=" ".join(body_refs), project_root=tmp_path)
        assert len(ev.text) <= _MAX_TOTAL_EVIDENCE_CHARS + 500  # header + marker slack


class TestFixtureComparison:
    """AC: a diagnose run whose body cites a run_id reaches confirmed-cause with
    less discovery work than one with no auto-injection. Operationalized as a
    fixture comparison of the built prompt: with the reference, the run-log
    excerpt is present in the prompt (zero tool calls needed to see it); with no
    reference, the prompt carries no STARTING EVIDENCE section at all."""

    def test_cited_run_id_preloads_evidence_absent_without_citation(self, tmp_path):
        from theforge.task.diagnose_prompts import build_diagnose_prompt

        run_id = "7cf3f238d8d8"
        marker = "issue-1135 reached APPROVE 0 P1 0 P2"
        _write(
            tmp_path / ".forge" / "logs" / "issues-1135" / f"run-{run_id}.log",
            f"[forge] earlier\n[forge] {marker}\n[sprint] Sprint complete\n",
        )

        with_ref = build_starting_evidence(
            issue_body=f"Sprint run {run_id}: landing_status wrong",
            project_root=tmp_path,
        )
        without_ref = build_starting_evidence(
            issue_body="Landing status looks wrong, unclear where.",
            project_root=tmp_path,
        )

        prompt_with = build_diagnose_prompt(
            issue_number=1420,
            title="landing bug",
            body=f"Sprint run {run_id}: landing_status wrong",
            starting_evidence=with_ref.text,
        )
        prompt_without = build_diagnose_prompt(
            issue_number=1420,
            title="landing bug",
            body="Landing status looks wrong, unclear where.",
            starting_evidence=without_ref.text,
        )

        # With the citation: the log excerpt the agent would otherwise have to
        # fetch is already in the prompt.
        assert "== STARTING EVIDENCE" in prompt_with
        assert marker in prompt_with
        # Without it: no injected section, unchanged from pre-feature behavior.
        assert "== STARTING EVIDENCE" not in prompt_without
        assert marker not in prompt_without
