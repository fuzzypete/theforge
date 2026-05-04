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
