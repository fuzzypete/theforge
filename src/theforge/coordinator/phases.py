"""Backward-compatibility shim — phase handlers now live in dedicated modules.

DEV phase:      theforge.coordinator.dev_phase
VALIDATE phase: theforge.coordinator.validate_phase
REVIEW phase:   theforge.coordinator.review_phase
"""

# ruff: noqa: F401
from .completion import (
    _append_cycle_history,
    _archive_story_to_done,
    _create_pr,
    _finalize_approve,
)
from .dev_phase import (
    _ensure_runners,
    _run_dev_phase,
    build_dev_prompt,
    build_fix_prompt,
    log_agent_result,
    run_agent,
)
from .review_phase import (
    _apply_review_parse_fallback,
    _build_reviewer_verdicts,
    _handle_interactive_review_decision,
    _human_review,
    _log_review_findings,
    _ReviewOutcome,
    _run_escalate_gate,
    _run_review_only_phase,
    _run_review_phase,
)
from .validate_phase import (
    _run_validate_phase,
    _ValidateOutcome,
)
