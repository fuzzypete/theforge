"""Conformance: every issue-body producer validates before it mutates.

Two guards, because either alone is escapable. The first enumerates the known
producers and proves each one refuses at its own mutation seam. The second
scans the repository for issue-body mutation seams and fails when one appears
that no registered producer accounts for — so a newly added producer that skips
validation fails this suite rather than being discovered in the corpus.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

import pytest

from theforge.shape_check.producer import PRODUCERS

REPO_ROOT = Path(__file__).resolve().parents[1]

# ── Seam discovery ────────────────────────────────────────────────────
# The unit is one `gh issue create|edit` invocation carrying a body flag, not
# one file. A file-level scan cannot answer the question the acceptance
# criterion actually asks — it reports a file as covered the moment any
# registered producer id appears anywhere in it, so a *second* seam added
# beside an existing one is absorbed silently.
#
# Python argv is read through the AST rather than as text, because the tokens
# are separate string constants there. That distinguishes a real invocation
# from prose: a docstring or an error message reading "gh issue edit
# --body-file failed" is a single string constant, never the four separate
# constants a real argv list holds.

_GH_TOKENS = frozenset({"gh", "issue"})
_VERBS = frozenset({"create", "edit"})
_BODY_FLAGS = frozenset({"--body", "--body-file"})
#: Backslash-continued shell lines, joined so one command reads as one line.
_SHELL_CONTINUATION_RE = re.compile(r"\\+\n\s*")
_SHELL_INVOCATION_RE = re.compile(r"\bgh\s+issue\s+(?:create|edit)\b[^\n]*")
_SHELL_BODY_FLAG_RE = re.compile(r"--body(?:-file)?\b")


def _string_constants(node: ast.AST) -> set[str]:
    return {
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def _python_seams(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(line, enclosing function)`` for each Python argv body seam.

    Each function is walked once and the results indexed, rather than re-walked
    per candidate list. The straightforward nesting is O(functions x tree) and
    made this scan the slowest thing in the suite — 2.7M ``ast.walk`` steps,
    which pushed the parametrized cases below past the five-second per-test
    convention once every xdist worker had to re-pay it.
    """
    seams: list[tuple[int, str]] = []
    functions = [
        n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for fn in functions:
        nodes = list(ast.walk(fn))
        lists = [n for n in nodes if isinstance(n, ast.List)]
        if not lists:
            continue
        # `command = ["gh", "issue", "create", ...]` followed by
        # `command += ["--body-file", path]` is one seam split over two lists.
        extended = {
            node.target.id
            for node in nodes
            if isinstance(node, ast.AugAssign)
            and isinstance(node.target, ast.Name)
            and _string_constants(node.value) & _BODY_FLAGS
        }
        #: Names each list literal is assigned to, so the `extended` lookup
        #: below is a dict hit instead of another walk of the whole function.
        assigned_names: dict[int, set[str]] = {}
        for node in nodes:
            if isinstance(node, ast.Assign):
                assigned_names.setdefault(id(node.value), set()).update(
                    target.id for target in node.targets if isinstance(target, ast.Name)
                )
        for lst in lists:
            literals = {e.value for e in lst.elts if isinstance(e, ast.Constant)}
            if not (_GH_TOKENS <= literals and literals & _VERBS):
                continue
            if literals & _BODY_FLAGS:
                seams.append((lst.lineno, fn.name))
                continue
            if assigned_names.get(id(lst), frozenset()) & extended:
                seams.append((lst.lineno, fn.name))
    return sorted(set(seams))


def _shell_seams(text: str) -> list[int]:
    """Return the line of each shell `gh issue create|edit` carrying a body."""
    joined = _SHELL_CONTINUATION_RE.sub(" ", text)
    return sorted(
        {
            joined.count("\n", 0, m.start()) + 1
            for m in _SHELL_INVOCATION_RE.finditer(joined)
            if _SHELL_BODY_FLAG_RE.search(m.group(0))
        }
    )


def _embedded_shell(tree: ast.AST) -> list[str]:
    """Shell scripts embedded in Python as string constants — hook templates."""
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and n.value.lstrip().startswith("#!")
        and "bash" in n.value[:60]
    ]


#: Pseudo-function names for seams that are not inside a Python function.
SCRIPT = "<script>"
EMBEDDED_HOOK = "<embedded-hook>"


def discover_seams(path: Path) -> list[tuple[int, str]]:
    """Every issue-body mutation seam in one file, as ``(line, function)``."""
    text = path.read_text(encoding="utf-8")
    if path.suffix != ".py":
        return [(line, SCRIPT) for line in _shell_seams(text)]
    # Neither seam kind can exist without the bare token `issue` somewhere in
    # the source: a Python argv seam needs it as a string constant, and an
    # embedded hook needs it inside `gh issue create|edit`. Skipping the parse
    # for files that lack it is exact, and it drops more than half the tree.
    if "issue" not in text:
        return []
    tree = ast.parse(text)
    seams = _python_seams(tree)
    if "#!" in text:
        for template in _embedded_shell(tree):
            seams += [(line, EMBEDDED_HOOK) for line in _shell_seams(template)]
    return sorted(seams)


def _scan_sources() -> list[Path]:
    return sorted(
        [
            *(REPO_ROOT / "src" / "theforge").rglob("*.py"),
            *(REPO_ROOT / ".forge" / "hooks").glob("*.sh"),
        ]
    )


@lru_cache(maxsize=1)
def _all_seams() -> Mapping[tuple[str, str], int]:
    """Seam counts across the repository, keyed by ``(path, function)``.

    Cached because the scan AST-parses every module under ``src/theforge`` —
    around two seconds — and two of the tests below are parametrized over the
    whole registry. Recomputing it per case burned roughly a minute of CPU
    across the suite, which is real contention for the process and timing
    tests sharing the same ``-n auto`` workers.
    """
    counts: dict[tuple[str, str], int] = {}
    for path in _scan_sources():
        for _line, function in discover_seams(path):
            key = (str(path.relative_to(REPO_ROOT)), function)
            counts[key] = counts.get(key, 0) + 1
    return MappingProxyType(counts)


# ── Seam registry ─────────────────────────────────────────────────────
#: Every issue-body mutation seam, keyed by the file and the function holding
#: it, mapped to ``{producer id: the function that validates for it}``. The
#: guard is named separately because it is not always the function holding the
#: seam: `cmd_shape` validates before calling `_apply_to_github`, and
#: `_land_artifact` validates before calling `_gh_edit_body`. Naming it makes
#: that indirection reviewable instead of implicit.
SEAM_GUARDS: dict[tuple[str, str], dict[str, str]] = {
    (".forge/hooks/post_run.sh", SCRIPT): {"post-run-hook-finding": "forge_validate_body"},
    (".forge/hooks/post-run-gh-issues.sh", SCRIPT): {
        "post-run-hook-finding": "forge_validate_body"
    },
    ("src/theforge/cli/hooks.py", EMBEDDED_HOOK): {"post-run-hook-finding": "forge_validate_body"},
    ("src/theforge/advisory_conventions.py", "_maybe_file_issue"): {
        "forge-advisory-finding": "_maybe_file_issue"
    },
    ("src/theforge/cli/author.py", "_create_issue"): {"forge-author-create": "_create_issue"},
    ("src/theforge/cli/author.py", "_edit_issue"): {"forge-author-edit": "_edit_issue"},
    ("src/theforge/cli/report.py", "_create_issue"): {"forge-report-create": "cmd_report"},
    ("src/theforge/cli/report.py", "_update_body"): {"forge-report-update": "cmd_report"},
    ("src/theforge/cli/shape.py", "_apply_to_github"): {"forge-shape": "cmd_shape"},
    ("src/theforge/cli/todo.py", "_create_todo"): {"forge-todo-create": "_create_todo"},
    ("src/theforge/cli/todo.py", "_triage_todo"): {"forge-todo-triage": "_triage_todo"},
    ("src/theforge/coordinator/diagnose_flow.py", "_gh_edit_body"): {
        "forge-diagnose": "_land_artifact"
    },
    ("src/theforge/intake/groom_flow.py", "_default_edit"): {"forge-groom": "run_groom"},
    ("src/theforge/intake/remediation.py", "_default_edit_body"): {
        "forge-intake-autofix": "_remediate_one",
        "forge-intake-reopen-context": "remediate_shape_gate_skip",
    },
}

#: How many seams each registered site is expected to hold. Every entry here is
#: one, and a second seam appearing in the same function must fail rather than
#: hide behind its neighbour — that is the hole a file-level scan leaves open.
EXPECTED_SEAM_COUNT = 1


@lru_cache(maxsize=None)
def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def _function_source(path: str, name: str) -> str | None:
    """Source of one function, read once per file rather than once per case."""
    text = _read(path)
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(text, node)
    return None


class TestSeamRegistry:
    def test_scan_is_not_vacuous(self):
        """A broken matcher must fail loudly, not report zero seams."""
        found = _all_seams()
        assert len(found) >= len(SEAM_GUARDS), (
            "the seam scan found fewer sites than the registry — the matcher is "
            f"broken. found: {sorted(found)}"
        )

    def test_discovered_seams_match_the_registry_exactly(self):
        """Seam-level, not file-level: a new seam beside an existing one fails.

        Keying on ``(file, function)`` and comparing counts is what closes the
        hole a per-file scan leaves: adding a second body write to a file that
        already holds one no longer inherits its neighbour's coverage.
        """
        found = _all_seams()
        unregistered = sorted(set(found) - set(SEAM_GUARDS))
        stale = sorted(set(SEAM_GUARDS) - set(found))
        assert not unregistered, (
            "these sites create or edit an issue body but no registered producer "
            f"accounts for them: {unregistered}. Register a producer in "
            "shape_check.producer.PRODUCERS, validate before the mutation, and add "
            "the seam to SEAM_GUARDS."
        )
        assert not stale, f"SEAM_GUARDS lists sites that no longer hold a seam: {stale}"

    @pytest.mark.parametrize("key", sorted(SEAM_GUARDS))
    def test_each_registered_site_holds_exactly_one_seam(self, key):
        path, function = key
        count = _all_seams().get(key, 0)
        assert count == EXPECTED_SEAM_COUNT, (
            f"{path}::{function} holds {count} issue-body mutation seams, expected "
            f"{EXPECTED_SEAM_COUNT}. A second body write in the same function needs "
            "its own producer and its own validation — it does not inherit the "
            "coverage of the one already there."
        )

    @pytest.mark.parametrize("key", sorted(SEAM_GUARDS))
    def test_each_seam_is_guarded_by_the_function_that_declares_its_producer(self, key):
        path, function = key
        text = _read(path)
        for producer, guard in SEAM_GUARDS[key].items():
            if function in (SCRIPT, EMBEDDED_HOOK):
                # Shell: the guard must be defined, must name the producer, and
                # must be invoked — the hook calls it in the `if` that decides
                # whether the create runs at all.
                assert f"{guard}()" in text, f"{path}: no {guard}() definition"
                assert producer in text, f"{path}: never names producer {producer!r}"
                assert text.count(guard) >= 2, (
                    f"{path}: {guard} is defined but never invoked before the seam"
                )
                continue
            source = _function_source(path, guard)
            assert source is not None, (
                f"{path}: seam in {function} names guard {guard}, which does not exist"
            )
            assert f'producer="{producer}"' in source, (
                f"{path}: {guard} guards the seam in {function} but never declares "
                f"producer={producer!r} — the seam is not routed through the boundary."
            )

    def test_every_registered_producer_owns_a_seam(self):
        registered = set(PRODUCERS)
        accounted = {p for guards in SEAM_GUARDS.values() for p in guards}
        assert registered == accounted, (
            "PRODUCERS and SEAM_GUARDS disagree; "
            f"unmapped: {sorted(registered - accounted)}, "
            f"unregistered: {sorted(accounted - registered)}"
        )


class TestSeamScannerDetectsNewSeams:
    """The guard's own guard.

    The registry is only as good as the scanner behind it, so these prove the
    scanner sees a seam added where the previous file-level scan would have
    absorbed it: beside an existing one, inside an already approved file.
    """

    def test_a_second_seam_in_an_already_guarded_function_is_detected(self):
        original = _read("src/theforge/cli/todo.py")
        before = _python_seams(ast.parse(original))
        # Derived from the registry rather than hard-coded, so this test cannot
        # drift out of agreement with the thing it is checking.
        registered = sum(
            count
            for (path, _fn), count in _all_seams().items()
            if path == "src/theforge/cli/todo.py"
        )
        assert len(before) == registered
        assert "_create_todo" in [fn for _, fn in before]

        # A second body write appended inside the function that already holds one.
        injected = original.replace(
            "    proc = _run_gh(command, project_root)",
            "    _run_gh(\n"
            '        ["gh", "issue", "edit", "1", "--body", "sneaked in"], project_root\n'
            "    )\n"
            "    proc = _run_gh(command, project_root)",
            1,
        )
        assert injected != original, "injection anchor no longer present"
        after = _python_seams(ast.parse(injected))
        create_seams = [fn for _, fn in after].count("_create_todo")
        assert create_seams == 2, (
            "the scanner missed a second seam added inside a function that already "
            "holds one — this is exactly the case a file-level scan absorbs"
        )

    def test_a_seam_in_a_new_function_of_an_approved_file_is_detected(self):
        original = _read("src/theforge/cli/todo.py")
        injected = original + (
            "\n\ndef _quietly_file_something(project_root):\n"
            "    return _run_gh(\n"
            '        ["gh", "issue", "create", "--title", "t", "--body", "b"], project_root\n'
            "    )\n"
        )
        functions = [fn for _, fn in _python_seams(ast.parse(injected))]
        assert "_quietly_file_something" in functions
        assert ("src/theforge/cli/todo.py", "_quietly_file_something") not in SEAM_GUARDS, (
            "the injected seam must be unregistered, or this test proves nothing"
        )

    def test_prose_naming_the_command_is_not_mistaken_for_a_seam(self):
        """A docstring or error string is one constant, never an argv list."""
        prose = (
            "def _unrelated():\n"
            '    """Replace the body of an issue via ``gh issue edit --body-file``."""\n'
            '    raise RuntimeError("gh issue edit --body-file failed")\n'
        )
        assert _python_seams(ast.parse(prose)) == []

    def test_a_shell_seam_added_to_a_hook_is_detected(self):
        hook = _read(".forge/hooks/post_run.sh")
        assert len(_shell_seams(hook)) == 1
        injected = hook + '\ngh issue create --title "t" --body "b"\n'
        assert len(_shell_seams(injected)) == 2


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
        from theforge.diagnose_types import ClaimVerification, DiagnosisArtifact, Hypothesis

        return DiagnosisArtifact(
            issue_number=44,
            observed_symptom="the export drops rows",
            reproduction_or_evidence="run log at `src/c.py:9`",
            hypotheses=(
                Hypothesis(
                    "the loader drops rows before serialization",
                    "confirmed",
                    "confirmed by reading the export path",
                    claim_verification=ClaimVerification(
                        "source", "Checked against the target repository source."
                    ),
                ),
            ),
            confirmed_cause="the loader in `src/a.py:42` drops it",
            affected_code_path="src/b.py:17",
            fix_success_criterion="no rows are dropped",
            confirmed_cause_verification=ClaimVerification(
                "source", "Checked against the target repository source."
            ),
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

    def test_a_declaration_it_cannot_keep_is_reported_not_absorbed(self, tmp_path):
        """Diagnose declares runnable; an untyped issue does not become runnable.

        The refusal here is not caused by anything diagnose introduced — the
        issue was already missing a type label. That is still a body whose
        evaluated state differs from the state diagnose declared, so it is
        reported rather than written. A declaration a pre-existing refusal can
        absorb would not be a declaration.
        """
        from theforge.coordinator import diagnose_flow
        from theforge.diagnose_types import DiagnoseState

        state = DiagnoseState(
            issue_number=44,
            issue_title="export drops rows",
            issue_body="## Observed\n\nrows vanish\n\n## Expected\n\nrows survive\n",
        )
        with patch.object(diagnose_flow, "_gh_edit_body") as mock_edit:
            with pytest.raises(Exception, match="forge-diagnose") as excinfo:
                diagnose_flow._land_artifact(
                    state, self._artifact(), "body_section", tmp_path, issue_labels=[]
                )
        assert not mock_edit.called
        message = str(excinfo.value)
        assert "declared : runnable" in message
        assert "evaluated: needs_type" in message
        # And it names the thing the operator has to fix, which diagnose cannot.
        assert "missing_type" in message


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


class TestAuthorValidatesBeforeWriting:
    """``forge author`` submits only a runnable body, so it declares RUNNABLE.

    The declaration is not a restatement of the flow's own verdict: the flow
    gates ``AuthorResult.body`` while both writers persist
    ``body_for_storage()``, so the string that lands is not the string that was
    checked. These cover the seam that closes the gap.
    """

    def _result(self, body, *, labels=("task",), title="Add the widget"):
        from theforge.intake.author_flow import AuthoringStatus, AuthorResult
        from theforge.shape_check.issue_spec import spec_for_label

        return AuthorResult(
            title=title,
            labels=tuple(labels),
            body=body,
            type_spec=spec_for_label("task"),
            status=AuthoringStatus.RUNNABLE,
            reasons=(),
            missing_parts=(),
        )

    def test_a_conforming_body_is_created(self, tmp_path):
        from theforge.cli import author as author_cli

        with patch.object(author_cli, "_gh") as mock_gh:
            mock_gh.return_value = type(
                "P", (), {"returncode": 0, "stdout": "url", "stderr": ""}
            )()
            assert author_cli._create_issue(self._result(_RUNNABLE_TASK_BODY), tmp_path)
        assert mock_gh.called

    def test_a_body_that_is_not_runnable_is_not_created(self, tmp_path, capsys):
        from theforge.cli import author as author_cli

        with patch.object(author_cli, "_gh") as mock_gh:
            created = author_cli._create_issue(self._result("Prose only.\n"), tmp_path)
        assert not created
        assert not mock_gh.called
        err = capsys.readouterr().err
        assert "forge-author-create" in err
        assert "declared : runnable" in err

    def test_an_edit_that_would_degrade_the_issue_is_not_written(self, tmp_path, capsys):
        from theforge.cli import author as author_cli

        degraded = _RUNNABLE_TASK_BODY + (
            "\n## Implementation plan\n\n"
            "1. Patch `src/a.py:42`\n2. Patch `src/b.py:17`\n3. Patch `src/c.py:9`\n"
        )
        loaded = author_cli._LoadedDraft(
            title="Add the widget",
            body=_RUNNABLE_TASK_BODY,
            labels=("task",),
            issue_number=12,
        )
        with patch.object(author_cli, "_gh") as mock_gh:
            ok = author_cli._edit_issue(self._result(degraded), loaded, tmp_path)
        assert not ok
        assert not mock_gh.called, "no gh call at all — not even the label edits"
        err = capsys.readouterr().err
        assert "forge-author-edit" in err
        assert "implementation_plan_in_body" in err
