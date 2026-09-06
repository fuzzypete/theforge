"""Shared setup logic for resume entry points (run_from_review, run_from_dev).

Provides _setup_resume_entry which initialises coordinator state, structured
logging, and session restoration for entry points that reuse an existing
worktree instead of creating one from scratch.
"""

from __future__ import annotations

import datetime
import logging
import subprocess
import time
from pathlib import Path

import yaml

from theforge.config import ForgeConfig
from theforge.sessions import load_sessions
from theforge.task import TaskStory, load_story

from . import util as _cu
from .config_snapshot import sync_forge_yaml_into_worktree
from .git_lock import FETCH_LOCK
from .logging import StructuredLogger
from .notify import _escalate_notify
from .resume_persistence import recover_phase_state
from .state import CoordinatorResult, CoordinatorState, MergeStepState, Phase

_logger = logging.getLogger(__name__)


def _yaml_safe(value: object) -> object:
    """Recursively coerce tuples to lists so yaml.safe_load can round-trip."""
    if isinstance(value, tuple):
        return [_yaml_safe(v) for v in value]
    if isinstance(value, list):
        return [_yaml_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _yaml_safe(v) for k, v in value.items()}
    return value


def _serialize_review_result(rr: object) -> dict | None:
    """Convert a ReviewResult dataclass into a yaml-safe dict for the resume sidecar.

    Imported lazily so this module stays free of theforge.review at import time.
    """
    if rr is None:
        return None
    import dataclasses  # noqa: PLC0415

    return _yaml_safe(dataclasses.asdict(rr))  # type: ignore[arg-type,return-value]


def _deserialize_parse_errors(raw: object) -> list:
    """Reconstruct ParseError instances from sidecar data.

    Backward-compat: bare strings in older sidecars are rehydrated with the
    SCHEMA_VALIDATION stage so they remain typed without losing information.
    """
    from theforge.schemas import SCHEMA_VALIDATION, ParseError  # noqa: PLC0415

    if not raw:
        return []
    out: list = []
    for entry in raw:
        if isinstance(entry, ParseError):
            out.append(entry)
        elif isinstance(entry, dict):
            stage = entry.get("stage") or SCHEMA_VALIDATION
            message = entry.get("message") or ""
            out.append(ParseError(stage=stage, message=message))
        else:
            out.append(ParseError(stage=SCHEMA_VALIDATION, message=str(entry)))
    return out


def _deserialize_review_result(data: object) -> object | None:
    """Reconstruct a ReviewResult/ReviewFinding/ACVerification graph from sidecar data."""
    if not isinstance(data, dict):
        return None
    from theforge.review import ACVerification, ReviewFinding, ReviewResult  # noqa: PLC0415

    findings = []
    for f in data.get("findings") or []:
        if not isinstance(f, dict):
            continue
        findings.append(
            ReviewFinding(
                severity=f.get("severity", "P1"),
                file=f.get("file", ""),
                line=f.get("line"),
                observed=f.get("observed", ""),
                expected=f.get("expected", ""),
                evidence=f.get("evidence", ""),
                suggestion=f.get("suggestion"),
                reviewers=tuple(f.get("reviewers") or ()),
                reporter=f.get("reporter", ""),
            )
        )
    ac_verification = []
    for ac in data.get("ac_verification") or []:
        if not isinstance(ac, dict):
            continue
        ac_verification.append(
            ACVerification(
                criterion=ac.get("criterion", ""),
                status=ac.get("status", ""),
                evidence=ac.get("evidence", ""),
            )
        )
    return ReviewResult(
        verdict=data.get("verdict", "REQUEST_CHANGES"),
        summary=data.get("summary", ""),
        findings=findings,
        story_matches=bool(data.get("story_matches", False)),
        story_mismatches=list(data.get("story_mismatches") or []),
        test_adequate=bool(data.get("test_adequate", False)),
        test_gaps=list(data.get("test_gaps") or []),
        parse_errors=_deserialize_parse_errors(data.get("parse_errors")),
        raw_yaml=dict(data.get("raw_yaml") or {}),
        ac_verification=tuple(ac_verification),
        criteria_enumerable=bool(data.get("criteria_enumerable", True)),
        criteria_enumerable_rationale=data.get("criteria_enumerable_rationale") or "",
        sanitization_audit=dict(data.get("sanitization_audit") or {}),
    )


def save_trajectory_state(workspace_path: Path, state: CoordinatorState) -> None:
    """Persist trajectory fields to <workspace_path>/.forge/trajectory.yaml.

    Called after each review cycle classification, and after each gate
    execution in VALIDATE, so the trajectory survives a ``forge run --resume``.

    Design note: trajectory data is stored in a dedicated sidecar file rather
    than in ``.forge/sessions.json`` (via ``save_sessions()``).  ``save_sessions()``
    rewrites ``.forge/sessions.json`` from scratch on every call, and multiple
    callers (dev_phase.py, review_pool.py, plan_flow.py, engine.py) invoke it
    between review cycles.  Adding trajectory keys to sessions.json would require
    every caller to pass those keys through so later writes don't erase them.
    A sidecar avoids that coupling: trajectory writes are independent of session
    writes, and neither can silently clobber the other.
    """
    sidecar = workspace_path / ".forge" / "trajectory.yaml"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "trajectory_cycle": state.trajectory_cycle,
        "finding_trajectory": state.finding_trajectory,
        "review_cycle_findings": [
            [cycle_num, findings] for cycle_num, findings in state.review_cycle_findings
        ],
        "surviving_families": state.surviving_families,
        # Topology-walk detection (#2372): both the evidence and the fact that it
        # has already been routed to the gate, so a --resume neither loses the
        # signal nor re-escalates a pattern the operator already decided on.
        "review_topology_signal": state.review_topology_signal,
        "review_topology_escalated": state.review_topology_escalated,
        "review_topology_triggered": state.review_topology_triggered,
        "escalate_kind": state.escalate_kind,
        # Gate-execution count, so the "N gate run(s)" an escalation reports
        # covers the story's whole history rather than restarting at the most
        # recent --resume (#1984).
        "gate_runs": state.gate_runs,
        # Which commit the gate last judged, and what it decided. Without these
        # a resumed run reports every review cycle as ungated even though a gate
        # ran before the resume (#2052).
        "last_gate_commit": state.last_gate_commit,
        "last_gate_decision": state.last_gate_decision,
        # Which validation profile produced each result, and what authority it
        # carried (#2358). Persisted with the decision it explains: a resumed
        # run that kept the verdict but lost the profile behind it could no
        # longer say what the verdict was worth.
        "validation_runs": state.validation_runs,
        "hygiene_escalation_dev_commit_sha": state.hygiene_escalation_dev_commit_sha,
        "hygiene_escalation_prior_approve_count": state.hygiene_escalation_prior_approve_count,
        "hygiene_escalation_total_count": state.hygiene_escalation_total_count,
        "hygiene_escalation_prior_review": _serialize_review_result(
            state.hygiene_escalation_prior_review
        ),
        # Gate-green salvage (#2028). The checkpoint is what a resumed run would
        # otherwise have to re-derive from a review cycle it no longer holds, and
        # the pending salvage event is what a landing after a resume reads to
        # know it must reset the branch before merging. Losing either turns a
        # recoverable story back into a discarded one.
        "gate_green_checkpoint": (
            {
                **state.gate_green_checkpoint.to_audit_dict(),
                "review_result": _serialize_review_result(
                    state.gate_green_checkpoint.review_result
                ),
            }
            if state.gate_green_checkpoint is not None
            else None
        ),
        "gate_green_salvage": state.gate_green_salvage,
        "gate_green_salvage_declined": state.gate_green_salvage_declined,
    }
    sidecar.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")


def load_trajectory_state(workspace_path: Path, state: CoordinatorState) -> None:
    """Restore trajectory fields from <workspace_path>/.forge/trajectory.yaml.

    Called in ``_setup_resume_entry`` so post-resume trajectory numbering
    continues from the correct baseline.  Missing or corrupt sidecar files are
    handled gracefully — state fields remain at their defaults.
    """
    sidecar = workspace_path / ".forge" / "trajectory.yaml"
    if not sidecar.exists():
        return

    try:
        raw = sidecar.read_text(encoding="utf-8")
        if not raw.strip():
            return
        data = yaml.safe_load(raw)
        if not isinstance(data, dict):
            _logger.warning("trajectory.yaml: expected a dict, got %s — ignoring", type(data))
            return
    except Exception as exc:  # noqa: BLE001
        _logger.warning("trajectory.yaml: failed to parse (%s) — ignoring", exc)
        return

    if "trajectory_cycle" in data:
        state.trajectory_cycle = int(data["trajectory_cycle"])
    if "finding_trajectory" in data and isinstance(data["finding_trajectory"], list):
        state.finding_trajectory = data["finding_trajectory"]
    if "review_cycle_findings" in data and isinstance(data["review_cycle_findings"], list):
        state.review_cycle_findings = [
            (int(entry[0]), list(entry[1]))
            for entry in data["review_cycle_findings"]
            if isinstance(entry, (list, tuple)) and len(entry) == 2
        ]
    if "surviving_families" in data and isinstance(data["surviving_families"], list):
        state.surviving_families = data["surviving_families"]
    if isinstance(data.get("review_topology_signal"), dict):
        state.review_topology_signal = data["review_topology_signal"]
    if isinstance(data.get("review_topology_escalated"), bool):
        state.review_topology_escalated = data["review_topology_escalated"]
    if isinstance(data.get("review_topology_triggered"), bool):
        state.review_topology_triggered = data["review_topology_triggered"]
    if isinstance(data.get("gate_runs"), int) and not isinstance(data.get("gate_runs"), bool):
        state.gate_runs = max(0, int(data["gate_runs"]))
    if isinstance(data.get("last_gate_commit"), str) and data["last_gate_commit"]:
        state.last_gate_commit = data["last_gate_commit"]
    if isinstance(data.get("last_gate_decision"), str) and data["last_gate_decision"]:
        state.last_gate_decision = data["last_gate_decision"]
    # Absent in sidecars written before profiles existed. Absence restores an
    # empty history, which the readers treat as legacy (complete/merge) rather
    # than as an untrusted run, so an older resume keeps its previous meaning.
    if isinstance(data.get("validation_runs"), list):
        state.validation_runs = [
            entry for entry in data["validation_runs"] if isinstance(entry, dict)
        ]
    if data.get("escalate_kind") in ("hygiene", "content", "decompose"):
        state.escalate_kind = data["escalate_kind"]
    if isinstance(data.get("hygiene_escalation_dev_commit_sha"), str):
        state.hygiene_escalation_dev_commit_sha = data["hygiene_escalation_dev_commit_sha"]
    if isinstance(data.get("hygiene_escalation_prior_approve_count"), int):
        state.hygiene_escalation_prior_approve_count = data[
            "hygiene_escalation_prior_approve_count"
        ]
    if isinstance(data.get("hygiene_escalation_total_count"), int):
        state.hygiene_escalation_total_count = data["hygiene_escalation_total_count"]
    _prior_rr = _deserialize_review_result(data.get("hygiene_escalation_prior_review"))
    if _prior_rr is not None:
        state.hygiene_escalation_prior_review = _prior_rr  # type: ignore[assignment]
    _restore_gate_green_salvage(data, state)


def _restore_gate_green_salvage(data: dict, state: CoordinatorState) -> None:
    """Restore the gate-green checkpoint and salvage event from the sidecar (#2028).

    Absent in sidecars written before the feature existed; absence restores the
    defaults, which is a story with nothing to salvage — the pre-existing
    behaviour.
    """
    from .state import GateGreenCheckpoint  # noqa: PLC0415

    raw = data.get("gate_green_checkpoint")
    if isinstance(raw, dict) and isinstance(raw.get("commit"), str) and raw["commit"]:
        state.gate_green_checkpoint = GateGreenCheckpoint(
            commit=raw["commit"],
            review_cycle=int(raw.get("review_cycle") or 0),
            dev_iterations_spent=int(raw.get("dev_iterations_spent") or 0),
            review_verdict=str(raw.get("review_verdict") or ""),
            carried_p2_count=int(raw.get("carried_p2_count") or 0),
            branch_name=raw.get("branch_name"),
            review_result=_deserialize_review_result(raw.get("review_result")),
        )
    if isinstance(data.get("gate_green_salvage"), dict):
        state.gate_green_salvage = data["gate_green_salvage"]
    if isinstance(data.get("gate_green_salvage_declined"), dict):
        state.gate_green_salvage_declined = data["gate_green_salvage_declined"]


def load_plan_state(workspace_path: Path, state: CoordinatorState) -> None:
    """Restore plan_output / plan_structured from the worktree's ``.forge/plan.md``.

    Called in ``_setup_resume_entry`` so a resumed run stands in for the
    PLAN_VALIDATION → DEV/REVIEW handoff: without this, ``_setup_resume_entry``
    allocates a fresh ``CoordinatorState`` whose ``plan_structured`` stays at its
    ``None`` default, and every plan-derived signal consumed by coordinator
    policy (stuck-detection scaling, routing, complexity adjustment, audit
    telemetry) silently degrades to a zero/empty default even though the plan
    that ran for the story demonstrably declares files (issue #1135).

    Best-effort: a missing plan file leaves state at its defaults (a fresh run
    that never produced a plan). A present-but-unparseable plan (freeform
    markdown fallback) restores the raw text into ``plan_output`` and leaves
    ``plan_structured`` at ``None``, matching how the non-resume PLAN path
    behaves for freeform plans.
    """
    from theforge.artifacts import PLAN_PATH  # noqa: PLC0415
    from theforge.task.plan_parser import parse_plan_output  # noqa: PLC0415

    plan_file = workspace_path / PLAN_PATH
    if not plan_file.exists():
        return
    try:
        text = plan_file.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        _logger.warning("plan.md: failed to read (%s) — ignoring", exc)
        return
    if not text.strip():
        return
    state.plan_output = text
    try:
        parsed = parse_plan_output(text)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("plan.md: failed to parse (%s) — restoring raw text only", exc)
        return
    if parsed is not None:
        state.plan_structured = parsed


def save_merge_state(workspace_path: Path, merge_state: MergeStepState) -> None:
    """Persist merge step state to <workspace_path>/.forge/merge_state.yaml.

    Called after each step in _merge_pr so a crash can be resumed from the last
    committed step.  Follows the same sidecar pattern as save_trajectory_state.
    """
    sidecar = workspace_path / ".forge" / "merge_state.yaml"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "completed_steps": list(merge_state.completed_steps),
        "pr_url": merge_state.pr_url,
        "merge_queued": merge_state.merge_queued,
        "auto_merge_queued": merge_state.auto_merge_queued,
        "error": merge_state.error,
        "last_updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    sidecar.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")


def load_merge_state(workspace_path: Path) -> MergeStepState:
    """Restore merge step state from <workspace_path>/.forge/merge_state.yaml.

    Returns a fresh MergeStepState if the sidecar is missing or corrupt.
    """
    sidecar = workspace_path / ".forge" / "merge_state.yaml"
    if not sidecar.exists():
        return MergeStepState()
    try:
        raw = sidecar.read_text(encoding="utf-8")
        if not raw.strip():
            return MergeStepState()
        data = yaml.safe_load(raw)
        if not isinstance(data, dict):
            _logger.warning("merge_state.yaml: expected a dict, got %s — ignoring", type(data))
            return MergeStepState()
        completed = data.get("completed_steps")
        return MergeStepState(
            completed_steps=list(completed) if isinstance(completed, list) else [],
            pr_url=data.get("pr_url"),
            merge_queued=bool(data.get("merge_queued", False)),
            auto_merge_queued=bool(data.get("auto_merge_queued", False)),
            error=data.get("error"),
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning("merge_state.yaml: failed to parse (%s) — ignoring", exc)
        return MergeStepState()


def delete_merge_state(workspace_path: Path) -> None:
    """Remove the merge step state sidecar on clean completion."""
    sidecar = workspace_path / ".forge" / "merge_state.yaml"
    try:
        sidecar.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("merge_state.yaml: failed to delete (%s) — ignoring", exc)


def _detach_skip_worktree_forge_yaml(worktree_path: Path) -> str | None:
    """Temporarily detach a skip-worktree'd forge.yaml so a rebase can update it.

    _setup_resume_entry syncs the operator's live forge.yaml into the worktree and
    marks it skip-worktree (issue #1627) so its content never lands in a story
    commit. skip-worktree hides the working-tree modification from status/diff, but
    it does NOT protect the path from an incoming rebase/checkout: if the base
    branch changed forge.yaml, git tries to update the path, detects the divergent
    working-tree content, and aborts the whole rebase ("local changes ... would be
    overwritten by checkout") — a conflict forge manufactured against a file the
    operator never edited in that worktree (issue #1772).

    Preserve the synced content, clear the skip-worktree bit, and reset the working
    copy to the tracked version so the tree is clean and the rebase can replay
    freely. Returns the preserved content for _reattach_skip_worktree_forge_yaml,
    or None when forge.yaml is not skip-worktree here (e.g. the reused-worktree
    path in workspace.py that never syncs operator config).
    """
    ls_ok, ls_out = _cu._run_shell("git ls-files -v forge.yaml", worktree_path)
    if not ls_ok or not ls_out.strip().startswith("S "):
        return None
    forge_yaml = worktree_path / "forge.yaml"
    try:
        content = forge_yaml.read_text()
    except OSError:
        # Nothing to preserve/restore; leave the bit as-is rather than risk
        # re-syncing empty content over operator config.
        return None
    _cu._run_shell("git update-index --no-skip-worktree forge.yaml", worktree_path)
    _cu._run_shell("git checkout -- forge.yaml", worktree_path)
    return content


def _reattach_skip_worktree_forge_yaml(worktree_path: Path, content: str | None) -> None:
    """Re-apply operator forge.yaml content and re-set skip-worktree after rebase.

    Restores the steady state established by _setup_resume_entry (issue #1627): the
    run executes with operator config in the working tree, but the file stays
    hidden from status/diff/staging so it never lands in a story commit. Called
    unconditionally after the rebase (success or failure) to undo the detach.
    """
    if content is None:
        return
    forge_yaml = worktree_path / "forge.yaml"
    try:
        forge_yaml.write_text(content)
    except OSError:
        pass
    _cu._run_shell("git update-index --skip-worktree forge.yaml", worktree_path)


def _rebase_onto_main(worktree_path: str, base_branch: str, logger) -> tuple[bool, str]:
    """Fetch and rebase the resumed worktree onto origin/base_branch."""
    git_dir = Path(worktree_path) / ".git"
    if not git_dir.exists():
        return True, ""

    wt = Path(worktree_path)
    # A synced, skip-worktree'd forge.yaml would abort the rebase if the base
    # branch diverged on that file; detach it around the rebase and restore it
    # afterward (issue #1772).
    _synced_forge_yaml = _detach_skip_worktree_forge_yaml(wt)
    try:
        with FETCH_LOCK:
            fetch_proc = subprocess.run(
                ["git", "fetch", "origin", base_branch],
                capture_output=True,
                text=True,
                cwd=worktree_path,
            )
        if fetch_proc.returncode != 0:
            err = fetch_proc.stderr.strip() or fetch_proc.stdout.strip()
            return False, err

        # If HEAD already contains origin/base_branch, the branch is fully
        # integrated — this is exactly the state after an operator brought a
        # stale branch current with `git merge main` and resolved conflicts in
        # a merge commit (also any already-up-to-date branch). A linear rebase
        # here would replay the branch's original pre-merge commits and drop the
        # merge commit, discarding the resolution and re-firing the settled
        # conflict (issue #1794). Skip the rebase; the finally-block still
        # reattaches the skip-worktree'd forge.yaml. Only rc==0 means "already an
        # ancestor" — any other code (git returns 1 for not-ancestor) falls
        # through to the rebase so we fail toward attempting it, not skipping it.
        ancestor_proc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", f"origin/{base_branch}", "HEAD"],
            capture_output=True,
            text=True,
            cwd=worktree_path,
        )
        if ancestor_proc.returncode == 0:
            return True, ""

        rebase_proc = subprocess.run(
            ["git", "rebase", f"origin/{base_branch}"],
            capture_output=True,
            text=True,
            cwd=worktree_path,
        )
        if rebase_proc.returncode != 0:
            err = rebase_proc.stderr.strip() or rebase_proc.stdout.strip()
            subprocess.run(
                ["git", "rebase", "--abort"],
                capture_output=True,
                text=True,
                cwd=worktree_path,
            )
            return False, err
    except Exception as exc:  # noqa: BLE001
        if logger is not None:
            logger.warning("resume rebase step failed: %s", exc)
        return False, str(exc)
    finally:
        _reattach_skip_worktree_forge_yaml(wt, _synced_forge_yaml)

    return True, ""


#: Which re-entry path is running.  Both reach this module through the same
#: ``_setup_resume_entry``, and they do different things with the same recovered
#: state, so the disclosure below has to know which one it is speaking for
#: (#2239).
#:
#: * ``review``          — ``forge review``: enters at REVIEW and runs the cycle.
#: * ``pipeline_resume`` — ``forge run --resume`` / ``forge sprint --resume``:
#:   re-enters the pipeline and continues from whatever the recovered state says,
#:   which for a recorded escalate-gate decision means landing, not REVIEW.
REENTRY_MODE_REVIEW = "review"
REENTRY_MODE_PIPELINE_RESUME = "pipeline_resume"


def _report_reentry_impact(recovery: dict, *, reentry_mode: str) -> None:
    """State, before the loop spends anything, a recovery that changes what runs.

    The generic "recovered phase record: escalation" line above names *what* was
    lifted off disk but not what acting on it means for the outstanding review
    cycle — and that differs by path.  ``forge review`` enters at REVIEW and runs
    the cycle; a pipeline resume continues from the recovered decision, so a
    cycle that never ran never will.  Reporting one path's behaviour from the
    other would be worse than saying nothing: the operator would be told review
    is being skipped by the very command about to run it.  Split across short
    lines to match the surrounding RESUME output (#2239).
    """
    impact = recovery.get("reentry_impact")
    if not impact:
        return

    decision = impact.get("escalation_decision")
    action = impact.get("escalation_selected_action")
    decision_str = f"{decision} / {action}" if action else str(decision)
    source = recovery.get("source_run_id") or "an earlier attempt"
    _cu._log(f"  ↺ RESUME   recovered escalation decision {decision_str} (from {source})")

    cycle = impact.get("outstanding_review_cycle")
    cycle_str = f"REVIEW cycle {cycle}" if cycle else "the next REVIEW cycle"
    verdict = impact.get("latest_review_verdict")
    gate = impact.get("last_gate_decision")
    context = ", ".join(
        part
        for part in (
            f"last verdict {verdict}" if verdict else "",
            f"gate {gate}" if gate else "",
        )
        if part
    )
    outstanding = f"  ↺ RESUME   outstanding: {cycle_str} has not run"
    if context:
        outstanding = f"{outstanding} ({context})"
    _cu._log(outstanding)

    if reentry_mode == REENTRY_MODE_REVIEW:
        _cu._log(
            f"  ↺ RESUME   `forge review` runs {cycle_str} now — "
            f"`forge sprint --resume` would continue from that decision and skip it"
        )
    else:
        _cu._log(
            f"  ↺ RESUME   this resume continues from that decision and will NOT run REVIEW — "
            f"`forge review` runs {cycle_str} instead"
        )


def _setup_resume_entry(
    config: ForgeConfig,
    task: TaskStory,
    workspace_path: Path,
    *,
    initial_phase: Phase,
    notify: bool,
    run_id: str | None,
    reentry_mode: str = REENTRY_MODE_PIPELINE_RESUME,
) -> tuple[CoordinatorState, StructuredLogger, str, str, float] | CoordinatorResult:
    """Shared setup for run_from_review / run_from_dev.

    Returns (state, logger, branch_name, story_content, task_start) on success,
    or a CoordinatorResult on failure (worktree missing).

    ``reentry_mode`` names which operator-facing command is re-entering, because
    the two do different things with the same recovered state.  It defaults to
    the pipeline resume: the entry function alone cannot distinguish them (a
    ``--resume`` triage can pick ``run_from_review`` too), so ``forge review``
    declares itself and everything else is a resume.
    """
    # No preflight verdict is seeded here. A resumed attempt does not run
    # preflight, but "this process did not run it" is not the same claim as
    # "the phase was skipped": a prior attempt of the same story may have run it
    # and the coordinator may have routed on its output. Seeding "SKIPPED" made
    # the audit assert a bypass that never happened (#2155). The verdict is left
    # unset and filled below from the durable phase record when one exists;
    # unrecovered, it stays null — absent, which is distinguishable from the
    # deliberate SKIPPED the ``--from <phase>`` bypass still writes.
    state = CoordinatorState(
        phase=initial_phase,
        dev_iteration=0,
        review_cycle=0,
    )
    state.started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _task_start = time.monotonic()

    _run_id = run_id or _cu._generate_run_id()
    state.run_id = _run_id
    logger = StructuredLogger(
        run_id=_run_id,
        project=config.project,
        task=task.slug,
        log_file=config.log.log_file,
        enabled=config.log.enabled,
        project_root=config.project_root,
    )
    logger._safe_emit(
        "run_start",
        specs=[str(task.story_path)],
        budget_usd=config.dev_profile.budget_usd,
        resume=True,
        p2_policy=config.dev.p2_policy,
    )
    _cu._log(f"  Dev P2 policy: {config.dev.p2_policy}")

    if not workspace_path.exists():
        state.phase = Phase.ESCALATE
        state.error = f"Worktree not found at {workspace_path}. Run `forge run` first."
        _cu._log(f"✗ ESCALATE   {state.error}")
        logger._safe_emit("escalate", reason=state.error, phase="INIT")
        logger._safe_emit("run_end", outcome="escalate", total_cost_usd=0.0, total_duration_s=0.0)
        _escalate_notify(task, state, notify, config)
        return CoordinatorResult(
            success=False,
            phase=state.phase,
            state=state,
            message=state.error,
        )

    state.workspace_path = workspace_path

    # Restore trajectory data from sidecar (survives --resume)
    load_trajectory_state(workspace_path, state)

    # Restore the parsed plan from the worktree's .forge/plan.md. A resumed run
    # allocates a fresh CoordinatorState that stands in for the PLAN_VALIDATION →
    # DEV/REVIEW handoff; without this, state.plan_structured stays None and every
    # plan-derived policy signal degrades to a zero/empty default (issue #1135).
    load_plan_state(workspace_path, state)

    # Give the resumed run its operative forge.yaml: the sprint's pinned
    # snapshot when one is active, the project root otherwise (#1980, #1627).
    sync_forge_yaml_into_worktree(config.project_root, workspace_path, label="RESUME")

    # Restore session IDs from prior run if available
    _sessions = load_sessions(workspace_path)
    if _sessions.get("dev_session_id"):
        state.dev_session_id = _sessions["dev_session_id"]
    if _sessions.get("reviewer_session_ids"):
        state.reviewer_session_ids = _sessions["reviewer_session_ids"]
    if _sessions.get("plan_review_session_ids"):
        state.plan_review_session_ids = _sessions["plan_review_session_ids"]

    # Resolve branch name from actual worktree HEAD
    _ok_branch, _branch_out = _cu._run_shell("git rev-parse --abbrev-ref HEAD", workspace_path)
    if _ok_branch and _branch_out.strip() and _branch_out.strip() != "HEAD":
        branch_name = _branch_out.strip()
    else:
        branch_name = config.workspace.branch_pattern.format(slug=task.slug)
    state.branch_name = branch_name

    story_content = task.story_text if task.story_text is not None else load_story(task.story_path)
    state.story_content = story_content

    # Restore the phase outputs a fresh CoordinatorState would otherwise lose:
    # preflight's judgement, the routing decision derived from it, the
    # plan-review outcome, and any escalate-gate/timeout escalation an earlier
    # attempt of this story produced (#2155). Applied after story_content is
    # resolved because the record is keyed to the story text.
    #
    # Precedence: this only fills fields that are still unset, so a live
    # cached_preflight_state — applied later by the engine and validated against
    # git state — still wins outright, and a phase this attempt runs itself
    # always beats a recorded one. The routing *record* (restore_routing_decision)
    # remains the seating authority for the review pool and is applied after
    # this, re-deriving the panel from the same complexity signal.
    _recovery = recover_phase_state(
        config.project_root,
        state,
        slug=task.slug,
        story_content=story_content,
    )
    # Stamp the path onto the impact before it reaches the audit: which command
    # re-entered decides what the recovered decision does to REVIEW, so an audit
    # reader tracing the disclosure needs the same fact the disclosure used.
    if isinstance(_recovery.get("reentry_impact"), dict):
        _recovery["reentry_impact"]["reentry_mode"] = reentry_mode
    state.phase_recovery = _recovery
    logger._safe_emit("phase_recovery", phase="RESUME", **_recovery)
    if _recovery["status"] == "recovered":
        _cu._log(
            f"  ↺ RESUME   recovered phase record: {', '.join(_recovery['recovered_phases'])}"
        )
        _report_reentry_impact(_recovery, reentry_mode=reentry_mode)
    elif _recovery["status"] == "rejected":
        _cu._log(
            f"  ⚠ RESUME   persisted phase record rejected ({_recovery['reason']}) — "
            f"phases run by an earlier attempt will be reported as unrecorded"
        )
    elif _recovery["status"] == "unavailable":
        _cu._log(
            "  ⚠ RESUME   no persisted phase record — phases run by an earlier "
            "attempt (if any) will be reported as unrecorded, not skipped"
        )

    return state, logger, branch_name, story_content, _task_start
