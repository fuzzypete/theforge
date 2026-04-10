"""Subprocess entry point for project-code evaluations that run in the worktree.

Called via:
    PYTHONPATH=/worktree/src python /path/to/_subprocess_eval.py <command>

with JSON piped to stdin; JSON result printed to stdout.

By setting PYTHONPATH to the worktree's src/ directory, the subprocess imports
theforge.* modules from the worktree rather than the coordinator's own copies.
This is the correct isolation boundary for self-hosting: the coordinator never
imports worktree-version code into its own process; it only evaluates it here.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# When Python runs this file directly (not via -m), it inserts the script's
# directory into sys.path[0].  The coordinator directory contains logging.py
# which shadows the standard library logging module.  Remove it before any
# lazy imports inside the command handlers below.
_this_dir = os.path.dirname(os.path.abspath(__file__))
if sys.path and os.path.abspath(sys.path[0]) == _this_dir:
    sys.path.pop(0)


def _cmd_check_conventions(data: dict) -> dict:
    """Run check_hard_conventions or new_hard_convention_violations_since_ref."""
    from theforge.config.types import HardConventionsConfig
    from theforge.conventions import (
        check_hard_conventions,
        new_hard_convention_violations_since_ref,
    )

    config = HardConventionsConfig(**data["config"])
    project_root = Path(data["project_root"])

    def _v_to_dict(v: object) -> dict:
        return {
            "rule": v.rule,  # type: ignore[attr-defined]
            "file": v.file,  # type: ignore[attr-defined]
            "detail": v.detail,  # type: ignore[attr-defined]
            "blocking": v.blocking,  # type: ignore[attr-defined]
        }

    if "baseline_ref" in data:
        all_v, net_new_v = new_hard_convention_violations_since_ref(
            config, project_root, data["baseline_ref"]
        )
        return {
            "all_violations": [_v_to_dict(v) for v in all_v],
            "violations": [_v_to_dict(v) for v in net_new_v],
        }
    else:
        violations = check_hard_conventions(config, project_root)
        return {"violations": [_v_to_dict(v) for v in violations]}


def _cmd_check_handoff_consistency(data: dict) -> dict:
    """Run check_handoff_story_consistency."""
    from theforge.devhandoff import DevHandoff, check_handoff_story_consistency

    handoff = DevHandoff(
        summary="",
        commits=[],
        acceptance_criteria=data["acceptance_criteria"],
        story_deviations=[],
        deferred_items=[],
        gate_result=None,
        parse_errors=[],
        raw={},
    )
    errors = check_handoff_story_consistency(handoff, data["story_content"])
    return {"errors": errors}


def _cmd_update_finding_registry(data: dict) -> dict:
    """Run finding_classifier.update_finding_registry.

    Returns the updated finding_registry (full list of records) and the indices
    of records classified this cycle, so the coordinator can reconstruct shared
    references between state.finding_registry and _classified.
    """
    from theforge import finding_classifier as _fc
    from theforge.review import ReviewFinding, ReviewResult

    # Build a minimal state object: only finding_registry is accessed
    class _State:
        def __init__(self, registry: list) -> None:
            self.finding_registry = registry

    from theforge.coordinator.state import FindingRecord

    state = _State(
        registry=[FindingRecord(**r) for r in data["finding_registry"]],
    )

    # Reconstruct cycle_results
    cycle_results = []
    for reviewer_name, rr_data in data["cycle_results"]:
        findings = [ReviewFinding(**f) for f in rr_data["findings"]]
        rr = ReviewResult(
            verdict=rr_data["verdict"],
            summary=rr_data["summary"],
            findings=findings,
            story_matches=rr_data["story_matches"],
            story_mismatches=rr_data["story_mismatches"],
            test_adequate=rr_data["test_adequate"],
            test_gaps=rr_data["test_gaps"],
            parse_errors=rr_data["parse_errors"],
            raw_yaml=rr_data["raw_yaml"],
        )
        cycle_results.append((reviewer_name, rr))

    workspace_path = Path(data["workspace_path"])
    cycle_num = data["cycle_num"]
    prev_commit = data.get("prev_commit")

    classified = _fc.update_finding_registry(
        state=state,
        cycle_results=cycle_results,
        workspace_path=workspace_path,
        cycle_num=cycle_num,
        prev_commit=prev_commit,
    )

    def _record_to_dict(r: object) -> dict:
        return {
            "finding_id": r.finding_id,  # type: ignore[attr-defined]
            "cycle_first_seen": r.cycle_first_seen,  # type: ignore[attr-defined]
            "cycle_last_seen": r.cycle_last_seen,  # type: ignore[attr-defined]
            "file": r.file,  # type: ignore[attr-defined]
            "line": r.line,  # type: ignore[attr-defined]
            "severity": r.severity,  # type: ignore[attr-defined]
            "description": r.description,  # type: ignore[attr-defined]
            "reporter": r.reporter,  # type: ignore[attr-defined]
            "disposition": r.disposition,  # type: ignore[attr-defined]
        }

    registry_dicts = [_record_to_dict(r) for r in state.finding_registry]

    # Map classified records back to their indices in the registry
    registry_ids = {id(r): i for i, r in enumerate(state.finding_registry)}
    classified_indices = [registry_ids[id(r)] for r in classified]

    return {
        "finding_registry": registry_dicts,
        "classified_indices": classified_indices,
    }


def _cmd_classify_families(data: dict) -> dict:
    """Run review_finding_classifier.classify_families."""
    from theforge.review import ReviewFinding
    from theforge.review_finding_classifier import classify_families

    def _make_rf(f: dict) -> ReviewFinding:
        return ReviewFinding(
            severity=f.get("severity", "P1"),
            file=f.get("file", ""),
            line=f.get("line"),
            description=f.get("description", ""),
            suggestion=f.get("suggestion"),
        )

    current_findings = [_make_rf(f) for f in data["current_findings"]]
    current_cycle = data["current_cycle"]
    trajectory_store = data["trajectory_store"]
    prior_cycle_findings = [
        (cycle_num, [_make_rf(f) for f in findings])
        for cycle_num, findings in data["prior_cycle_findings"]
    ]

    updated_store, surviving = classify_families(
        current_findings=current_findings,
        current_cycle=current_cycle,
        trajectory_store=trajectory_store,
        prior_cycle_findings=prior_cycle_findings,
    )
    return {"trajectory_store": updated_store, "surviving_families": surviving}


_COMMANDS = {
    "check_conventions": _cmd_check_conventions,
    "check_handoff_consistency": _cmd_check_handoff_consistency,
    "update_finding_registry": _cmd_update_finding_registry,
    "classify_families": _cmd_classify_families,
}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in _COMMANDS:
        print(json.dumps({"error": f"unknown command: {cmd!r}"}), file=sys.stderr)
        sys.exit(1)

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"JSON decode error: {e}"}), file=sys.stderr)
        sys.exit(1)

    try:
        result = _COMMANDS[cmd](payload)
        print(json.dumps(result))
    except Exception as e:
        import traceback

        print(
            json.dumps({"error": str(e), "traceback": traceback.format_exc()}),
            file=sys.stderr,
        )
        sys.exit(1)
