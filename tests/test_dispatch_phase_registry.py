from __future__ import annotations

import ast
from pathlib import Path

import pytest

from theforge.config import model_catalog
from theforge.config.model_catalog import parse_definition
from theforge.config.model_identity import DispatchPhase, build_dispatch_phase_registry


def test_registry_derives_known_default_and_closed_sets_from_phase_metadata() -> None:
    registry = build_dispatch_phase_registry(
        (
            DispatchPhase("synthetic_open"),
            DispatchPhase(
                "synthetic_closed",
                operator_constrainable=False,
                default_eligible=False,
            ),
        )
    )

    assert registry.known_phases == frozenset({"synthetic_open", "synthetic_closed"})
    assert registry.default_phase_eligibility == frozenset({"synthetic_open"})
    assert registry.closed_operator_phases == frozenset({"synthetic_closed"})


def test_parser_uses_registry_derived_vocabulary_without_a_second_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = build_dispatch_phase_registry(
        (
            DispatchPhase("synthetic_open"),
            DispatchPhase(
                "synthetic_closed",
                operator_constrainable=False,
                default_eligible=False,
            ),
        )
    )
    monkeypatch.setattr(model_catalog, "KNOWN_PHASES", registry.known_phases)
    monkeypatch.setattr(
        model_catalog,
        "is_known_dispatch_phase",
        lambda phase: phase in registry.known_phases,
    )
    monkeypatch.setattr(
        model_catalog,
        "is_operator_constrainable_phase",
        lambda phase: phase not in registry.closed_operator_phases,
    )

    opened = parse_definition(
        {
            "provider": "openai",
            "model": "m",
            "transport": {"kind": "api"},
            "routing": {
                "tier": "cheap",
                "capability": 7,
                "cost_rank": 1,
                "phase_eligibility": ["synthetic_open"],
            },
        },
        where="x",
    )
    assert opened.routing["phase_eligibility"] == frozenset({"synthetic_open"})

    with pytest.raises(ValueError, match="deliberately closed"):
        parse_definition(
            {
                "provider": "openai",
                "model": "m",
                "transport": {"kind": "api"},
                "routing": {
                    "tier": "cheap",
                    "capability": 7,
                    "cost_rank": 1,
                    "phase_eligibility": ["synthetic_closed"],
                },
            },
            where="x",
        )


def test_model_profile_phase_sites_do_not_introduce_new_string_literals() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    violations: list[str] = []

    for path in sorted((repo_root / "src/theforge").rglob("*.py")):
        relative_path = path.relative_to(repo_root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            phase_kw = next((kw for kw in node.keywords if kw.arg == "phase"), None)
            if phase_kw is None or not isinstance(phase_kw.value, ast.Constant):
                continue
            if not isinstance(phase_kw.value.value, str):
                continue
            func = node.func
            func_name = None
            if isinstance(func, ast.Name):
                func_name = func.id
            elif isinstance(func, ast.Attribute):
                func_name = func.attr
            if func_name not in {"ModelProfile", "model_ref_to_profile", "replace"}:
                continue
            if phase_kw.value.value.upper() == phase_kw.value.value:
                continue
            violations.append(f"{relative_path}:{node.lineno}:{phase_kw.value.value}")

    assert not violations, f"phase literals must flow through shared constants: {violations}"
