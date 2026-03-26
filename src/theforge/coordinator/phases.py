"""Phase handler re-export shim.

The three phase implementations live in their own modules:
  - phase_dev.py      — DEV phase
  - phase_validate.py — VALIDATE phase
  - phase_review.py   — REVIEW phase (approve finalization, PR creation, cycle history)

This module re-exports everything so that existing import paths and test
patch targets (e.g. ``theforge.coordinator.phases.subprocess.run``,
``theforge.coordinator.phases._run_escalate_gate``) continue to resolve.
"""

from __future__ import annotations

# subprocess must be importable at this namespace for test patches.
import subprocess  # noqa: F401

from .phase_dev import _run_dev_phase  # noqa: F401
from .phase_review import (  # noqa: F401
    _append_cycle_history,
    _archive_story_to_done,
    _build_reviewer_verdicts,
    _create_pr,
    _finalize_approve,
    _ReviewOutcome,
    _run_escalate_gate,
    _run_review_phase,
)
from .phase_validate import _run_validate_phase, _ValidateOutcome  # noqa: F401
