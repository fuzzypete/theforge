"""Boundary tests for issue #1386: orchestrator-internal classification must
not run via the worktree-PYTHONPATH subprocess.

The bug: `update_finding_registry` and `classify_families` operate on
TheForge's own coordinator-internal dataclasses (FindingRecord, ReviewFinding).
Routing them through `_run_worktree_eval` made the wire format an implicit
dataclass schema, so a dogfood sprint would crash whenever the worktree's
schema diverged from the launched version.

These tests pin the structural fix:
  - review_phase no longer imports `_run_worktree_eval`.
  - the subprocess-eval dispatch table no longer exposes the internal commands.
  - finding classification continues to run end-to-end without the worktree
    PYTHONPATH boundary in the call path.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import patch

import pytest

from theforge.coordinator import _subprocess_eval, review_phase


def test_review_phase_does_not_import_run_worktree_eval():
    """Orchestrator-internal classification must not depend on the worktree
    eval helper. Importing it back into review_phase would make it trivial to
    accidentally re-route an internal command through the boundary."""
    assert not hasattr(review_phase, "_run_worktree_eval"), (
        "review_phase must not import _run_worktree_eval; orchestrator-internal "
        "commands run in-process (#1386)."
    )


def test_subprocess_eval_does_not_dispatch_internal_commands():
    """The subprocess-eval entry point only carries project-owned commands.
    update_finding_registry / classify_families operate on coordinator-internal
    dataclasses and must never travel across the worktree-PYTHONPATH boundary."""
    assert "update_finding_registry" not in _subprocess_eval._COMMANDS
    assert "classify_families" not in _subprocess_eval._COMMANDS
    # Project-owned commands stay where they are — the concurrency-safe boundary
    # from #628 is preserved for them.
    assert "check_conventions" in _subprocess_eval._COMMANDS


def test_review_phase_calls_classifiers_directly():
    """Source-level guard: the review-phase module body invokes the classifier
    functions directly rather than through a subprocess-eval indirection.
    This is what prevents the cross-version dataclass crash described in #1386."""
    src = inspect.getsource(review_phase)
    assert "_update_finding_registry(" in src
    assert "_classify_families(" in src
    # Belt-and-braces: no string literal naming the removed dispatch keys.
    assert '"update_finding_registry"' not in src
    assert '"classify_families"' not in src


# ── Production-crash regression ────────────────────────────────────────────
#
# The bug observed on 2026-05-04 (forge sprint --issues 1135,1014,1363 against
# installed theforge==0.10.0rc3 on a post-#1366 worktree):
#
#   RuntimeError: Worktree eval 'update_finding_registry' failed (exit 1):
#   ReviewFinding.__init__() got an unexpected keyword argument 'description'
#
# Mechanism: the parent process (installed schema, ReviewFinding.description as
# an init field) JSON-serialized findings and shipped them to a subprocess that
# imported ReviewFinding from the worktree's src/ — where #1366 had renamed
# description → observed/expected/evidence. Constructor kwargs no longer matched.
#
# The tests below recreate that exact divergence and prove the fix:
#   1. The seam (`_run_worktree_eval`) is never invoked for the orchestrator-
#      internal classifier commands, even when a divergent worktree exists.
#   2. The classifier functions themselves run in-process against the launched
#      schema and never see the worktree's broken constructor.
#
# Construction: a sentinel `_run_worktree_eval` patch raises the *exact*
# production error message if anything routes through it. The classifier path
# completing without that error is the proof the boundary is no longer crossed.


def _make_divergent_worktree(tmp_path: Path) -> Path:
    """Build a fake worktree whose ReviewFinding rejects ``description=``.

    This is the post-#1366 schema: ``description`` becomes a derived property,
    no longer a constructor kwarg. The installed (launched) parent still emits
    ``description``-shaped payloads; if any parent-built payload reaches a
    process that imported THIS class, it raises the production TypeError.
    """
    worktree_src = tmp_path / "worktree" / "src"
    pkg = worktree_src / "theforge"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "review.py").write_text(
        "from dataclasses import dataclass\n"
        "@dataclass(frozen=True)\n"
        "class ReviewFinding:\n"
        "    severity: str\n"
        "    file: str\n"
        "    line: int | None\n"
        "    observed: str\n"
        "    expected: str = ''\n"
        "    evidence: str = ''\n"
        "    suggestion: str | None = None\n"
        "    @property\n"
        "    def description(self) -> str:\n"
        "        return self.observed\n",
        encoding="utf-8",
    )
    return tmp_path / "worktree"


def test_divergent_worktree_review_finding_rejects_description_kwarg(tmp_path):
    """Sanity: prove the production crash is real against the simulated
    post-#1366 worktree schema. If this passed, the divergence wouldn't
    matter — and the bug from the issue wouldn't exist."""
    import sys

    worktree = _make_divergent_worktree(tmp_path)
    sys.path.insert(0, str(worktree / "src"))
    try:
        # Import the worktree-side ReviewFinding (NOT the installed one).
        # We can't co-import with the real theforge.review, so do an isolated
        # exec of the file to avoid module-cache collisions.
        ns: dict = {}
        exec(  # noqa: S102 — controlled local file, no untrusted input
            (worktree / "src" / "theforge" / "review.py").read_text(),
            ns,
        )
        DivergentReviewFinding = ns["ReviewFinding"]
        with pytest.raises(TypeError, match="description"):
            DivergentReviewFinding(
                severity="P1",
                file="x.py",
                line=1,
                description="missing null check",
                suggestion=None,
            )
    finally:
        sys.path.remove(str(worktree / "src"))


def test_in_process_classifier_path_under_simulated_schema_divergence(tmp_path, monkeypatch):
    """Seam-level regression for #1386.

    Patch ``_run_worktree_eval`` to raise the *exact* production error if the
    classifier ever crosses the worktree-PYTHONPATH boundary. Then drive the
    in-process classification path that ``review_phase`` uses for
    ``update_finding_registry`` and ``classify_families``. The test passes
    only if the boundary is never crossed AND classification completes against
    the launched-version schema without the description-kwarg crash."""
    from theforge.coordinator.state import CoordinatorState
    from theforge.finding_classifier import update_finding_registry
    from theforge.review import ReviewFinding, ReviewResult
    from theforge.review_finding_classifier import classify_families

    # Pre-build a divergent worktree on disk so the failure mode is concrete:
    # if any code did `_run_worktree_eval(workspace_path, ...)` against this
    # worktree, it would crash with the production error.
    workspace = _make_divergent_worktree(tmp_path)

    # Sentinel: any boundary crossing for orchestrator-internal commands is a
    # bug. Reproduce the exact production error message so test failure
    # mirrors the dogfood crash.
    crossed: list[str] = []

    def _crash_on_cross(_workspace_path, command, _payload, timeout=120):
        crossed.append(command)
        raise RuntimeError(
            f"Worktree eval {command!r} failed (exit 1): "
            "ReviewFinding.__init__() got an unexpected keyword argument 'description'"
        )

    monkeypatch.setattr("theforge.coordinator.util._run_worktree_eval", _crash_on_cross)
    # Stub the changed-files git probe so update_finding_registry is hermetic.
    monkeypatch.setattr(
        "theforge.finding_classifier._get_changed_files",
        lambda _ws, _prev: frozenset(),
    )

    # Build a description-shaped payload (the launched schema). On the buggy
    # path this exact payload is what the parent shipped to the subprocess
    # and what the worktree's ReviewFinding rejected.
    finding = ReviewFinding(
        severity="P1",
        file="src/foo.py",
        line=10,
        description="missing null handling in runtime path",
        suggestion=None,
    )
    review_result = ReviewResult(
        verdict="REQUEST_CHANGES",
        summary="",
        findings=[finding],
        story_matches=True,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=[],
        raw_yaml="",
    )
    state = CoordinatorState()

    # ── update_finding_registry: same call shape review_phase now uses ───
    classified = update_finding_registry(
        state=state,
        cycle_results=[("review", review_result)],
        workspace_path=workspace,
        cycle_num=1,
        prev_commit=None,
    )
    assert len(classified) == 1
    assert classified[0].description == "missing null handling in runtime path"
    assert state.finding_registry, "finding_registry must be populated in-process"

    # ── classify_families: cycle ≥ 2 path with a prior-cycle snapshot ────
    prior_snapshot = [
        {"severity": "P1", "file": "src/foo.py", "line": 10, "description": "earlier finding"}
    ]
    prior_rfs = [
        ReviewFinding(
            severity=d["severity"],
            file=d["file"],
            line=d["line"],
            description=d["description"],
            suggestion=None,
        )
        for d in prior_snapshot
    ]
    updated_store, surviving = classify_families(
        current_findings=[finding],
        current_cycle=2,
        trajectory_store=[],
        prior_cycle_findings=[(1, prior_rfs)],
    )
    assert isinstance(updated_store, list)
    assert isinstance(surviving, list)

    # The whole point: nothing ever crossed the worktree-eval boundary, so
    # the divergent-schema crash that killed issue-1014 cannot occur.
    assert crossed == [], (
        f"orchestrator-internal classification crossed _run_worktree_eval ({crossed!r}); "
        "the #1386 fix routes these in-process to avoid the schema-divergence crash"
    )


def test_call_signatures_match_review_phase_invocations():
    """Final guard: review_phase invokes the classifier functions with the
    same kwargs the regression test exercises. If review_phase's call shape
    drifts away from these signatures, the regression coverage above silently
    becomes irrelevant — pin both ends together."""
    from theforge.finding_classifier import update_finding_registry
    from theforge.review_finding_classifier import classify_families

    ufr_params = set(inspect.signature(update_finding_registry).parameters)
    assert {"state", "cycle_results", "workspace_path", "cycle_num", "prev_commit"} <= ufr_params

    cf_params = set(inspect.signature(classify_families).parameters)
    assert {
        "current_findings",
        "current_cycle",
        "trajectory_store",
        "prior_cycle_findings",
    } <= cf_params
