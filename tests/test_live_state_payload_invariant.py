"""Cross-phase live-state payload field invariant (issue #1921).

Each coordinator phase builds its own ``state_update_fn`` live-state payload
independently. Nothing previously asserted that these payloads agree on which
*story-descriptive* fields they carry, so a field added for one phase (e.g. the
numeric ``complexity_score`` in #1917) silently stayed absent from the siblings
and surfaced only as an operator-visible display inconsistency — never a test
failure.

This module is that missing mechanical backstop. It discovers, by static AST
inspection of the phase modules, the top-level key set every ``state_update_fn``
call site emits, then asserts that the *story-descriptive* fields are carried
consistently across sibling phases. Because discovery reads the real call sites,
a newly added top-level field automatically enters the required common set and
fails the test in every phase that omits it — until that phase carries it too,
or the field is explicitly declared phase-specific below.

The declarations (``PHASE_SPECIFIC_TOP_LEVEL``, ``NESTED_DETAIL_PHASE_SPECIFIC``,
``BOOTSTRAP_MARKER_PHASES``) are the *intent* layer: legitimately phase-local
fields are named here rather than the invariant being weakened to tolerate them.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import theforge

# ── Coordinator phase modules that build live-state payloads ──────────────────
#
# reviewer_progress.py is intentionally excluded: it routes through a distinct
# ``self._state_update_fn`` channel object (not the bare ``state_update_fn``
# closure the phase functions receive) and is already covered, via the same
# ``live_complexity_fields`` helper, by test_live_complexity_score_propagation.py.
_COORD_DIR = Path(theforge.__file__).resolve().parent / "coordinator"
PHASE_MODULES = [
    "preflight_flow.py",
    "plan_flow.py",
    "review_phase.py",
    "validate_phase.py",
    "engine.py",
]

# ── Declared exceptions (the intent layer) ────────────────────────────────────

# ``**helper(...)`` dictionary expansions the discovery understands, mapped to the
# top-level keys the helper contributes. An expansion whose callee is not listed
# here fails discovery loudly so a new payload helper must be registered rather
# than silently shrinking the discovered field set.
HELPER_EXPANSIONS: dict[str, frozenset[str]] = {
    "live_complexity_fields": frozenset({"complexity", "complexity_score"}),
}

# Top-level fields that are legitimately phase-specific: carried by some phase
# payloads but not required to be uniform across siblings. ``current_model`` names
# the model running *this* phase; ``detail`` is a per-phase STAGE/DETAIL blob whose
# contents differ by phase (see NESTED_DETAIL_PHASE_SPECIFIC).
PHASE_SPECIFIC_TOP_LEVEL = frozenset({"current_model", "detail"})

# Structural fields present in every payload; not story-descriptive.
STRUCTURAL_TOP_LEVEL = frozenset({"phase", "iteration", "cost_usd"})

# Nested ``detail`` sub-fields that are legitimately phase-specific. ``detail`` is
# already declared phase-specific at the top level (its contents are compared per
# phase by the renderer, not across phases), so these are documented here to
# record intent; the invariant verifies each is actually observed so the
# declaration cannot go stale.
NESTED_DETAIL_PHASE_SPECIFIC = frozenset(
    {
        "preflight_verdict",
        "preflight_sufficiency",
        "plan_attempt",
        "review_cycle",
        "gate_status",
    }
)

# Phases permitted to emit a *bare* transition-marker payload — one that carries
# no story-descriptive fields. PREFLIGHT fires an entry ping before its own agent
# has computed complexity; WORKSPACE is pure infrastructure setup. Every other
# phase that emits a payload must carry the common story-descriptive fields.
BOOTSTRAP_MARKER_PHASES = frozenset({"PREFLIGHT", "WORKSPACE"})


# ── Discovery ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DiscoveredPayload:
    module: str
    phase: str
    top_level: frozenset[str]
    detail_keys: frozenset[str]


def _expansion_callee(value: ast.expr) -> str | None:
    """Return the callee name of a ``**call(...)`` expansion, or None."""
    if isinstance(value, ast.Call):
        func = value.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
    return None


def _discover_in_module(filename: str) -> list[DiscoveredPayload]:
    source = (_COORD_DIR / filename).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=filename)
    found: list[DiscoveredPayload] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "state_update_fn"
        ):
            continue
        assert node.args, f"{filename}: state_update_fn() called with no argument"
        payload = node.args[0]
        assert isinstance(payload, ast.Dict), (
            f"{filename}:{node.lineno}: state_update_fn() payload is not a dict "
            f"literal; the invariant can only inspect literal payloads"
        )
        top_level: set[str] = set()
        detail_keys: set[str] = set()
        phase: str | None = None
        for key, val in zip(payload.keys, payload.values):
            if key is None:
                # ``**expansion`` — resolve via the registered helper table.
                callee = _expansion_callee(val)
                assert callee in HELPER_EXPANSIONS, (
                    f"{filename}:{node.lineno}: unknown '**' payload expansion "
                    f"{callee!r}; register it in HELPER_EXPANSIONS so its fields "
                    f"are part of the invariant"
                )
                top_level |= HELPER_EXPANSIONS[callee]
                continue
            assert isinstance(key, ast.Constant) and isinstance(key.value, str), (
                f"{filename}:{node.lineno}: non-string literal payload key"
            )
            name = key.value
            top_level.add(name)
            if name == "phase":
                assert isinstance(val, ast.Constant) and isinstance(val.value, str), (
                    f"{filename}:{node.lineno}: 'phase' value is not a string literal"
                )
                phase = val.value
            if name == "detail" and isinstance(val, ast.Dict):
                for dkey in val.keys:
                    if isinstance(dkey, ast.Constant) and isinstance(dkey.value, str):
                        detail_keys.add(dkey.value)
        assert phase is not None, (
            f"{filename}:{node.lineno}: state_update_fn() payload has no 'phase' key"
        )
        found.append(
            DiscoveredPayload(
                module=filename,
                phase=phase,
                top_level=frozenset(top_level),
                detail_keys=frozenset(detail_keys),
            )
        )
    return found


def _discover_all() -> list[DiscoveredPayload]:
    payloads: list[DiscoveredPayload] = []
    for filename in PHASE_MODULES:
        payloads.extend(_discover_in_module(filename))
    return payloads


def _story_descriptive(payload: DiscoveredPayload) -> frozenset[str]:
    """Top-level fields that describe the *story*, not the phase or structure."""
    return payload.top_level - STRUCTURAL_TOP_LEVEL - PHASE_SPECIFIC_TOP_LEVEL


def _is_story_update(payload: DiscoveredPayload) -> bool:
    """A payload reports story progress if it carries any story-descriptive field
    or a per-phase ``detail`` block; a payload with neither is a bare transition
    marker."""
    return bool(_story_descriptive(payload)) or "detail" in payload.top_level


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestDiscovery:
    def test_call_sites_are_discovered(self) -> None:
        """Discovery must actually find the phase call sites; a refactor that
        renamed or moved them would otherwise make the invariant vacuous."""
        payloads = _discover_all()
        assert len(payloads) >= 10, (
            f"only {len(payloads)} live-state payloads discovered across "
            f"{PHASE_MODULES}; expected the full set of phase call sites"
        )
        phases = {p.phase for p in payloads}
        # Every major phase that writes live state must be represented.
        for expected in ("PREFLIGHT", "PLAN", "REVIEW", "VALIDATE", "DEV"):
            assert expected in phases, f"no live-state payload discovered for {expected}"


class TestCrossPhaseFieldConsistency:
    def test_story_descriptive_fields_are_consistent_across_phases(self) -> None:
        """Every story-update payload carries the story-descriptive fields its
        siblings provide. The required set is derived from the union of all
        discovered story-descriptive fields (minus declared phase-specific ones),
        so a field added to one phase automatically becomes required everywhere."""
        payloads = _discover_all()
        story_updates = [p for p in payloads if _is_story_update(p)]

        expected_common: frozenset[str] = frozenset().union(
            *(_story_descriptive(p) for p in story_updates)
        )
        # Sanity: the union is non-trivial (complexity is the field family #1917
        # and this story exist to keep consistent).
        assert "complexity" in expected_common

        for p in story_updates:
            missing = expected_common - _story_descriptive(p)
            assert not missing, (
                f"{p.module} {p.phase} live-state payload omits story-descriptive "
                f"field(s) {sorted(missing)} that sibling phase payloads carry; "
                f"add them to this payload (via the shared helper) or declare them "
                f"phase-specific in PHASE_SPECIFIC_TOP_LEVEL"
            )

    def test_bare_markers_only_in_declared_bootstrap_phases(self) -> None:
        """A payload carrying no story-descriptive fields and no detail block is a
        bare transition marker, permitted only in declared bootstrap phases. This
        is what catches an early-phase payload (e.g. the VALIDATE entry ping) that
        silently drops the complexity fields its post-gate sibling carries."""
        payloads = _discover_all()
        for p in payloads:
            if _is_story_update(p):
                continue
            assert p.phase in BOOTSTRAP_MARKER_PHASES, (
                f"{p.module} emits a bare {p.phase} live-state payload "
                f"({sorted(p.top_level)}) that carries no story-descriptive fields; "
                f"carry the shared complexity fields or, if this phase legitimately "
                f"predates complexity, add {p.phase} to BOOTSTRAP_MARKER_PHASES"
            )


class TestDeclarationsExpressIntent:
    """The declared exceptions must describe reality, so the invariant expresses
    intent rather than accumulating stale allowances."""

    def test_phase_specific_top_level_fields_are_actually_phase_specific(self) -> None:
        payloads = _discover_all()
        total = len(payloads)
        for field in PHASE_SPECIFIC_TOP_LEVEL:
            present = sum(1 for p in payloads if field in p.top_level)
            assert present > 0, (
                f"declared phase-specific field {field!r} appears in no payload; "
                f"the declaration is stale"
            )
            assert present < total, (
                f"declared phase-specific field {field!r} appears in every payload; "
                f"it is a common field, not phase-specific — remove it from "
                f"PHASE_SPECIFIC_TOP_LEVEL so the invariant requires it everywhere"
            )

    def test_declared_nested_detail_fields_are_observed(self) -> None:
        payloads = _discover_all()
        observed_detail_keys: frozenset[str] = frozenset().union(
            *(p.detail_keys for p in payloads)
        )
        for field in NESTED_DETAIL_PHASE_SPECIFIC:
            assert field in observed_detail_keys, (
                f"declared nested detail field {field!r} appears in no discovered "
                f"detail block; the declaration is stale"
            )
