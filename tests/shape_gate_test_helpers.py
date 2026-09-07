"""Shared helper for tests that drive sprint query mode for other reasons.

Query mode runs every issue through the sprint-entry shape gate, which reads
each issue with ``gh``. A test with no ``gh`` to answer that read leaves the
gate unable to evaluate anything, and since #2910 an unevaluated issue is
refused rather than admitted — so a test that needs its issues to *run* has to
say so instead of inheriting admission from a failed fetch.
"""

from __future__ import annotations

from unittest.mock import patch


def admit_every_issue_at_the_shape_gate():
    """Patch the sprint-entry shape gate to admit every issue it is handed.

    Use in tests whose subject is downstream of admission (locking, pid
    cleanup, parallelism defaults, crash handling). Tests that are about the
    gate itself must not use this — they exercise ``apply_shape_gate``.
    """
    from theforge.sprint.shape_gate import ShapeGateResult

    return patch(
        "theforge.sprint.shape_gate.apply_shape_gate",
        side_effect=lambda issues, *_args, **_kwargs: ShapeGateResult(runnable=list(issues)),
    )
