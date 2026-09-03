"""The spike closure guard: a spike closes on a recorded outcome or not at all.

See :mod:`theforge.spike_guard.outcome` for the rule and
:mod:`theforge.spike_guard.guard` for the ``gh`` boundary that feeds it.
"""

from .guard import check_spike_closure
from .outcome import (
    NOT_PIPELINE_LABELS,
    OUTCOME_MARKER,
    REMEDIATION,
    SPIKE_LABEL,
    TRIGGER_CONDITION_FIELDS,
    TRIGGER_CONDITION_HEADING,
    ClosureDecision,
    IssueFacts,
    SpikeOutcome,
    SpikeOutcomeKind,
    evaluate_spike_closure,
    find_spike_outcome,
    missing_trigger_condition_fields,
    parse_spike_outcomes,
    required_follow_up,
    validate_follow_up,
)

__all__ = [
    "ClosureDecision",
    "IssueFacts",
    "NOT_PIPELINE_LABELS",
    "OUTCOME_MARKER",
    "REMEDIATION",
    "SPIKE_LABEL",
    "SpikeOutcome",
    "SpikeOutcomeKind",
    "TRIGGER_CONDITION_FIELDS",
    "TRIGGER_CONDITION_HEADING",
    "check_spike_closure",
    "evaluate_spike_closure",
    "find_spike_outcome",
    "missing_trigger_condition_fields",
    "parse_spike_outcomes",
    "required_follow_up",
    "validate_follow_up",
]
