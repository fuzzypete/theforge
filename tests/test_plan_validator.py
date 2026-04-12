"""Tests for plan_validator.py — mechanical plan structure checks."""

from __future__ import annotations

from pathlib import Path

from theforge.plan_validator import extract_spec_criteria, validate_plan

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_plan(
    steps: list[dict] | None = None,
    criteria_mapping: list[dict] | None = None,
) -> dict:
    return {
        "approach": "test approach",
        "steps": steps or [],
        "criteria_mapping": criteria_mapping or [],
    }


def _step(
    id: int = 1,
    description: str = "Do something",
    files: list[str] | None = None,
    action: str = "modify",
    details: str = "details",
    depends_on: list[int] | None = None,
) -> dict:
    s: dict = {
        "id": id,
        "description": description,
        "files": files if files is not None else ["src/foo.py"],
        "action": action,
        "details": details,
    }
    if depends_on is not None:
        s["depends_on"] = depends_on
    return s


# ── AC coverage ───────────────────────────────────────────────────────────────


def test_ac_coverage_warns_on_missing_criterion(tmp_path: Path) -> None:
    plan = _make_plan(
        criteria_mapping=[{"criterion": "AC 1", "steps": [1]}],
    )
    findings = validate_plan(plan, ["AC 1", "AC 2 unmapped"], tmp_path)
    checks = [f["check"] for f in findings]
    messages = [f["message"] for f in findings]
    assert "ac_coverage" in checks
    assert any("AC 2 unmapped" in m for m in messages)


def test_ac_coverage_passes_when_all_mapped(tmp_path: Path) -> None:
    plan = _make_plan(
        criteria_mapping=[
            {"criterion": "AC 1", "steps": [1]},
            {"criterion": "AC 2", "steps": [1]},
        ],
    )
    findings = validate_plan(plan, ["AC 1", "AC 2"], tmp_path)
    ac_findings = [f for f in findings if f["check"] == "ac_coverage"]
    assert ac_findings == []


# ── Step completeness ─────────────────────────────────────────────────────────


def test_step_completeness_warns_on_empty_steps_list(tmp_path: Path) -> None:
    plan = _make_plan(steps=[])
    findings = validate_plan(plan, [], tmp_path)
    assert any(f["check"] == "step_completeness" and "no steps" in f["message"] for f in findings)


def test_step_completeness_warns_on_missing_description(tmp_path: Path) -> None:
    step = _step(description="")
    plan = _make_plan(steps=[step])
    findings = validate_plan(plan, [], tmp_path)
    assert any(
        f["check"] == "step_completeness" and "description" in f["message"] for f in findings
    )


def test_step_completeness_warns_on_empty_files(tmp_path: Path) -> None:
    step = _step(files=[])
    plan = _make_plan(steps=[step])
    findings = validate_plan(plan, [], tmp_path)
    assert any(f["check"] == "step_completeness" and "files" in f["message"] for f in findings)


def test_step_completeness_warns_on_missing_action(tmp_path: Path) -> None:
    step = _step(action="")
    plan = _make_plan(steps=[step])
    findings = validate_plan(plan, [], tmp_path)
    assert any(f["check"] == "step_completeness" and "action" in f["message"] for f in findings)


# ── File validity ─────────────────────────────────────────────────────────────


def test_file_validity_warns_on_nonexistent_modify_target(tmp_path: Path) -> None:
    step = _step(files=["nonexistent/file.py"], action="modify")
    plan = _make_plan(steps=[step])
    findings = validate_plan(plan, [], tmp_path)
    assert any(f["check"] == "file_validity" for f in findings)


def test_file_validity_passes_for_existing_modify_target(tmp_path: Path) -> None:
    real_file = tmp_path / "src" / "foo.py"
    real_file.parent.mkdir(parents=True)
    real_file.write_text("# exists")
    step = _step(files=["src/foo.py"], action="modify")
    plan = _make_plan(steps=[step])
    findings = validate_plan(plan, [], tmp_path)
    fv_findings = [f for f in findings if f["check"] == "file_validity"]
    assert fv_findings == []


def test_file_validity_skips_create_action(tmp_path: Path) -> None:
    step = _step(files=["new/file.py"], action="create")
    plan = _make_plan(steps=[step])
    findings = validate_plan(plan, [], tmp_path)
    fv_findings = [f for f in findings if f["check"] == "file_validity"]
    assert fv_findings == []


def test_file_validity_warns_on_nonexistent_delete_target(tmp_path: Path) -> None:
    step = _step(files=["gone/file.py"], action="delete")
    plan = _make_plan(steps=[step])
    findings = validate_plan(plan, [], tmp_path)
    assert any(f["check"] == "file_validity" for f in findings)


# ── Dependency ordering ───────────────────────────────────────────────────────


def test_dependency_warns_on_invalid_ref(tmp_path: Path) -> None:
    step = _step(id=1, depends_on=[99])  # 99 doesn't exist
    plan = _make_plan(steps=[step])
    findings = validate_plan(plan, [], tmp_path)
    assert any(f["check"] == "dependency_ordering" and "99" in f["message"] for f in findings)


def test_dependency_warns_on_cycle(tmp_path: Path) -> None:
    step_a = _step(id=1, depends_on=[2])
    step_b = _step(id=2, depends_on=[1])
    plan = _make_plan(steps=[step_a, step_b])
    findings = validate_plan(plan, [], tmp_path)
    assert any(
        f["check"] == "dependency_ordering" and "circular" in f["message"].lower()
        for f in findings
    )


def test_dependency_passes_for_valid_linear_chain(tmp_path: Path) -> None:
    step_a = _step(id=1, depends_on=[])
    step_b = _step(id=2, depends_on=[1])
    plan = _make_plan(steps=[step_a, step_b])
    findings = validate_plan(plan, [], tmp_path)
    dep_findings = [f for f in findings if f["check"] == "dependency_ordering"]
    assert dep_findings == []


# ── Clean plan ────────────────────────────────────────────────────────────────


def test_returns_empty_for_clean_plan(tmp_path: Path) -> None:
    real_file = tmp_path / "src" / "existing.py"
    real_file.parent.mkdir(parents=True)
    real_file.write_text("# exists")

    plan = _make_plan(
        steps=[_step(id=1, files=["src/existing.py"], action="modify")],
        criteria_mapping=[{"criterion": "AC 1", "steps": [1]}],
    )
    findings = validate_plan(plan, ["AC 1"], tmp_path)
    assert findings == []


# ── extract_spec_criteria ────────────────────────────────────────────────────


def test_extract_spec_criteria_basic() -> None:
    spec = """
## Acceptance Criteria

- [ ] First criterion
- [ ] Second criterion
- [x] Already done

## Other Section

- [ ] Not a criterion
"""
    criteria = extract_spec_criteria(spec)
    assert criteria == ["First criterion", "Second criterion", "Already done"]


def test_extract_spec_criteria_no_section() -> None:
    spec = "# Just a heading\n\nSome text.\n"
    criteria = extract_spec_criteria(spec)
    assert criteria == []


def test_extract_spec_criteria_stops_at_next_heading() -> None:
    spec = """
## Acceptance Criteria

- [ ] AC one

## Implementation Notes

- [ ] Not an AC
"""
    criteria = extract_spec_criteria(spec)
    assert criteria == ["AC one"]
