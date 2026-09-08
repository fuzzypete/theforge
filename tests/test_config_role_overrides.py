"""Explicit role overrides, and the layering that keeps them shareable (#2950).

Two components must reach the same answer about which roles config pins: the
router, which uses it to decide what bypasses candidate selection, and the
pre-dispatch availability gate, which uses it to decide what a phase may draw
from. The derivation therefore lives in ``config`` rather than in either of
them — the first iteration of this slice put it in ``coordinator.preflight`` and
had the sprint gate import it back, which is a circular import between two
packages that must not depend on each other.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from coord_test_helpers import _make_config  # noqa: E402

from theforge.config import ModelProfile  # noqa: E402
from theforge.config.role_overrides import explicit_role_overrides  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "src" / "theforge"


def _reviewer(name: str, model: str) -> ModelProfile:
    return ModelProfile(
        name=name,
        provider="anthropic",
        model=model,
        budget_usd=2.0,
        timeout_seconds=600,
        allowed_tools=(),
    )


# ── The derivation ─────────────────────────────────────────────────────


def test_a_default_config_pins_nothing(tmp_path):
    overrides = explicit_role_overrides(_make_config(tmp_path))
    assert "dev" not in overrides.profiles
    assert "preflight" not in overrides.profiles


def test_a_pinned_review_pool_is_carried_whole(tmp_path):
    """The pool's head locks the role; the rest still has to be visible."""
    pool = [_reviewer("r1", "sonnet"), _reviewer("r2", "opus")]
    config = replace(_make_config(tmp_path), review_pool=pool, review_pool_is_default=False)
    overrides = explicit_role_overrides(config)
    assert "review_pool" in overrides.roles
    assert overrides.profiles["code_review"].model == "sonnet"
    assert [p.model for p in overrides.review_pool] == ["sonnet", "opus"]


def test_an_empty_pool_pins_nothing_rather_than_raising(tmp_path):
    """Emptiness and contents are one question asked once.

    ``plan_agent_review.profiles`` is a computed property; asking "is it
    non-empty?" and "what is in it?" separately let a config answer yes to the
    first and produce nothing for the second, which indexed off the end.
    """
    config = replace(_make_config(tmp_path), review_pool=[], review_pool_is_default=False)
    overrides = explicit_role_overrides(config)
    assert overrides.review_pool == ()
    assert "code_review" not in overrides.profiles


# ── The layering ───────────────────────────────────────────────────────


def _imported_modules(path: Path) -> set[str]:
    """Every theforge module imported by *path*, including inside functions."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            # Resolve the relative form used inside the package.
            prefix = "theforge." if node.level and "theforge" not in node.module else ""
            found.add(f"{prefix}{node.module}")
    return found


@pytest.mark.parametrize(
    ("module", "forbidden"),
    [
        ("coordinator/preflight.py", "availability_gate"),
        ("coordinator/preflight_flow.py", "availability_gate"),
        ("sprint/availability_gate.py", "coordinator"),
        ("model_availability.py", "coordinator"),
        ("model_availability.py", "sprint"),
        ("config/role_overrides.py", "coordinator"),
        ("config/role_overrides.py", "sprint"),
    ],
)
def test_the_shared_derivation_creates_no_package_cycle(module, forbidden):
    """Neither package may import the other to reach the shared derivation.

    The coordinator and the sprint packages both consume
    ``phase_candidate_profiles`` and ``explicit_role_overrides``. Those live in
    ``model_availability`` and ``config.role_overrides`` precisely so that
    consuming them never forms an edge between the two packages.
    """
    imported = _imported_modules(SRC / module)
    offenders = sorted(name for name in imported if forbidden in name)
    assert not offenders, f"{module} must not import {forbidden}: {offenders}"
