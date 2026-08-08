"""Tests for src/theforge/convention_types.py."""

from __future__ import annotations

import dataclasses

from theforge.convention_types import ConventionViolation


def test_violations_block_by_default():
    """Blocking is the default; advisory rules opt out explicitly."""
    violation = ConventionViolation(rule="no_circular_imports", file="src/a.py", detail="cycle")
    assert violation.blocking is True


def test_violations_are_replaceable_without_mutation():
    """Callers derive variants (e.g. fail-closed) by copy, not by mutation."""
    original = ConventionViolation("max_module_lines", "src/a.py", "big", blocking=False)
    promoted = dataclasses.replace(original, blocking=True)
    assert promoted.blocking is True
    assert original.blocking is False


def test_conventions_module_re_exports_the_type():
    """The historical import site keeps working for callers and tests."""
    from theforge.conventions import ConventionViolation as ReExported

    assert ReExported is ConventionViolation
