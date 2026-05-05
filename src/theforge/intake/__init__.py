"""Sprint intake remediation: shared findings, grooming check, auto-fix.

Provides the ``IntakeFinding`` shared type emitted by both the shape gate
and the sprint-time grooming check, and a one-pass agent remediation
orchestrator that runs between ``normalize_dependency_plan`` and
``run_batch_preflight`` in the sprint runner.

Stdlib-only at the type layer; the orchestrator delegates GitHub I/O via
injectable callables so the module remains testable without network.
"""

from __future__ import annotations

from .agent_rewrite import (
    AgentRewriteResult,
    build_agent_rewrite_prompt,
    parse_agent_rewrite_output,
)
from .findings import (
    FixType,
    IntakeFinding,
    IntakeSeverity,
    findings_from_shape_result,
)
from .groom import groom_check
from .mechanical import apply_mechanical_fixes
from .remediation import (
    BODY_FIXABLE_SKIP_REASONS,
    IntakeOutcome,
    IntakeOutcomeKind,
    ShapeGateSkipRemediation,
    ShapeGateSkipRemediationKind,
    remediate_shape_gate_skip,
    run_intake_remediation,
)

__all__ = [
    "BODY_FIXABLE_SKIP_REASONS",
    "FixType",
    "AgentRewriteResult",
    "IntakeFinding",
    "IntakeOutcome",
    "IntakeOutcomeKind",
    "IntakeSeverity",
    "ShapeGateSkipRemediation",
    "ShapeGateSkipRemediationKind",
    "apply_mechanical_fixes",
    "build_agent_rewrite_prompt",
    "findings_from_shape_result",
    "groom_check",
    "parse_agent_rewrite_output",
    "remediate_shape_gate_skip",
    "run_intake_remediation",
]
