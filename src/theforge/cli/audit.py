"""forge audit subcommand — display human-readable audit summaries."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from theforge.coordinator.util import _fmt_duration

_UNKNOWN_COST = "unknown"


def _cost_str(value: object, *, width: int = 0) -> str:
    """Render an audit cost field, keeping an unmeasured ``None`` unmeasured.

    The audit stores ``None`` for spend a transport could not measure. Printing
    it as ``$0.0000`` would present unpriced work as free, so it renders as
    ``unknown`` instead (#1992).
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"${float(value):>{width}.4f}" if width else f"${float(value):.4f}"
    return f"{_UNKNOWN_COST:>{width + 1}}" if width else _UNKNOWN_COST


def _cmd_audit_ideate(audit: dict) -> int:
    """Print a human-readable summary of an ideation audit record."""
    sep = "=" * 60
    icon = "✓" if audit.get("success") else "✗"
    brief_preview = (audit.get("brief", "") or "")[:80].replace("\n", " ")
    spec_path = audit.get("story_path") or audit.get("spec_path") or "(none)"
    print(sep)
    print(f"{icon} IDEATE  →  {spec_path}")
    print(sep)
    print(f"  Brief:   {brief_preview!r}")

    pool = audit.get("model_pool", [])
    synth = audit.get("synthesis_profile")
    print(f"  Pool:    {'+'.join(pool) or '?'}  synthesis={synth or '—'}")

    if audit.get("human_decision_required"):
        print("  ⚠ Human decisions required")
        for item in audit.get("residual_divergence", []):
            print(f"    - {item}")

    timing = audit.get("timing", {})
    duration = timing.get("duration_seconds")
    started = timing.get("started_at")
    if started or duration is not None:
        print()
        print("  Timing")
        if started:
            print(f"    Started:  {started}")
        if duration is not None:
            mins, secs = divmod(int(duration), 60)
            print(f"    Duration: {mins}m {secs}s ({duration:.1f}s)")

    cost = audit.get("cost", {})
    print()
    print(f"  Cost:  {_cost_str(cost.get('total_usd'))}")

    rounds = audit.get("rounds", [])
    if rounds:
        print()
        print("  Rounds")
        for r in rounds:
            rn = r.get("round_number", "?")
            conv = r.get("converged_count", 0)
            div = r.get("divergent_count", 0)
            print(f"    Round {rn}:  converged={conv}  divergent={div}")
            for item in r.get("divergent_items", []):
                print(f"      ✗ {item}")

    print(sep)
    return 0


def cmd_audit(args: object) -> int:
    """Print a human-readable summary of an audit file."""
    audit_path = Path(args.file).resolve()
    if not audit_path.exists():
        print(f"Audit file not found: {audit_path}", file=sys.stderr)
        return 1

    with open(audit_path, encoding="utf-8") as f:
        audit = yaml.safe_load(f) or {}

    # Dispatch to ideation display for ideation audit records.
    if audit.get("type") == "ideate":
        return _cmd_audit_ideate(audit)

    task = audit.get("task", {})
    outcome = audit.get("outcome", {})
    iterations = audit.get("iterations", {})
    cost = audit.get("cost", {})
    timing = audit.get("timing", {})
    workspace = audit.get("workspace", {})
    reviews = audit.get("reviews", [])
    preflight = audit.get("preflight")

    sep = "=" * 60
    icon = "✓" if outcome.get("success") else "✗"
    print(f"{sep}")
    print(f"{icon} {task.get('name', '?')}  [{outcome.get('final_phase', '?')}]")
    print(f"{sep}")
    print(f"  Message:  {outcome.get('message', '?')}")

    # Workspace
    if workspace.get("path") or workspace.get("branch"):
        print(f"  Workspace: {workspace.get('path', '?')}")
        print(f"  Branch:    {workspace.get('branch', '?')}")

    # Preflight
    if preflight:
        pf_verdict = preflight.get("verdict", "?")
        pf_reason = preflight.get("reason", "")
        pf_cost = preflight.get("cost_usd")
        print()
        print(f"  Preflight: {pf_verdict} ({_cost_str(pf_cost)})")
        if pf_reason:
            print(f"    Reason: {pf_reason}")

    # Timing
    started = timing.get("started_at")
    finished = timing.get("finished_at")
    duration = timing.get("duration_seconds")
    if started or finished or duration is not None:
        print()
        print("  Timing")
        if started:
            print(f"    Started:  {started}")
        if finished:
            print(f"    Finished: {finished}")
        if duration is not None:
            mins, secs = divmod(int(duration), 60)
            print(f"    Duration: {mins}m {secs}s ({duration:.1f}s)")

    # Iterations
    print()
    print("  Iterations")
    dev_productive = iterations.get(
        "dev_iterations_productive", iterations.get("dev_iterations", "?")
    )
    dev_total = iterations.get("dev_attempts_total", "?")
    review_cycles = iterations.get("review_cycles_total", iterations.get("review_cycles", "?"))
    print(f"    Dev iterations (productive): {dev_productive}")
    print(f"    Dev attempts (total):        {dev_total}")
    print(f"    Review cycles:               {review_cycles}")
    print(f"    Gate runs (executed):        {iterations.get('gate_runs', '?')}")
    print(f"    Gate decisions: {iterations.get('gate_decisions', [])}")
    # What each validation run was worth, not just that it happened (#2358).
    # Absent for runs recorded before profiles existed; nothing is printed then,
    # so an older audit renders exactly as it did before.
    for entry in iterations.get("validation_runs") or []:
        if not isinstance(entry, dict):
            continue
        _skipped = " (skipped)" if entry.get("skipped") else ""
        _widened = " [widened]" if entry.get("widened") else ""
        print(
            f"    Validation: {entry.get('profile', '?')} "
            f"({entry.get('authority', '?')} authority) → "
            f"{entry.get('result', '?')}{_skipped}{_widened}"
        )
        if entry.get("command"):
            print(f"                command: {entry['command']}")

    # Cost summary
    print()
    print("  Cost")
    print(f"    Total:  {_cost_str(cost.get('total_usd'))}")
    dev_inv = cost.get("dev_invocations", 0)
    rev_inv = cost.get("review_invocations", 0)
    print(f"    Dev:    {_cost_str(cost.get('dev_usd'))}  ({dev_inv} invocation(s))")
    print(f"    Review: {_cost_str(cost.get('review_usd'))}  ({rev_inv} invocation(s))")

    # Per-agent breakdown
    agents = cost.get("agents", [])
    if agents:
        print()
        print(f"  {'Role':<10} {'Profile':<20} {'Cost (USD)':>12}  {'Duration':>10}")
        print(f"  {'-' * 10} {'-' * 20} {'-' * 12}  {'-' * 10}")
        for a in agents:
            role = a.get("role", "?")
            profile = a.get("profile", "?")
            cost_usd = a.get("cost_usd")
            dur = a.get("duration_seconds")
            dur_str = _fmt_duration(dur) if dur is not None else "—"
            print(f"  {role:<10} {profile:<20} {_cost_str(cost_usd, width=11)}  {dur_str:>10}")

    knowledge_summary = audit.get("knowledge_summary")
    if isinstance(knowledge_summary, dict):
        status = knowledge_summary.get("status", "?")
        attempted = "yes" if knowledge_summary.get("attempted") else "no"
        written = "yes" if knowledge_summary.get("written") else "no"
        print()
        print("  Knowledge Summary")
        print(f"    Status:    {status}")
        print(f"    Attempted: {attempted}")
        print(f"    Written:   {written}")
        reason = knowledge_summary.get("reason")
        if reason:
            print(f"    Reason:    {reason}")
        path = knowledge_summary.get("path")
        if path:
            print(f"    Path:      {path}")

    # Reviews
    if reviews:
        print()
        print("  Reviews")
        for r in reviews:
            cycle = r.get("cycle", "?")
            verdict = r.get("verdict", "?")
            p1 = r.get("p1_count", 0)
            p2 = r.get("p2_count", 0)
            summary = r.get("summary", "")
            print(f"    Cycle {cycle}: {verdict} ({p1} P1, {p2} P2) — {summary}")

            # What the verdict was a verdict about. A cycle rendered without
            # its commit and that commit's gate state reads as current even
            # when later commits already superseded it (#2052).
            commit = r.get("commit")
            verification = r.get("verification")
            vstate = verification.get("state") if isinstance(verification, dict) else None
            gate_decision = (
                verification.get("gate_decision") if isinstance(verification, dict) else None
            )
            if commit or vstate:
                commit_str = commit[:12] if isinstance(commit, str) and commit else "unknown"
                verification_str = vstate or "unknown"
                if gate_decision:
                    verification_str = f"{verification_str} (gate {gate_decision})"
                print(f"      Reviewed commit: {commit_str} — verification: {verification_str}")

            findings = r.get("findings", [])
            if findings:
                for finding in findings:
                    sev = finding.get("severity", "?")
                    ffile = finding.get("file", "?")
                    line = finding.get("line")
                    loc = f"{ffile}:{line}" if line else ffile
                    desc = finding.get("description", "")
                    print(f"      [{sev}] {loc} — {desc}")

    # Coordinator-raised blocking findings (gate / hard convention). These are
    # not reviewer verdicts, so they are not in Reviews above — but one of them
    # can spend a review cycle or end the story, so the operator has to see it.
    validate_blocks = audit.get("validate_blocks") or []
    if validate_blocks:
        print()
        print("  Coordinator blocks (VALIDATE)")
        for block in validate_blocks:
            kind = block.get("kind", "?")
            outcome = block.get("outcome", "?")
            reason = block.get("reason", "?")
            cycle = block.get("review_cycle", "?")
            spent = block.get("dev_iterations_spent", "?")
            print(f"    [{kind}] cycle {cycle}, {spent} dev iteration(s) → {outcome} ({reason})")
            for violation in block.get("convention_violations") or []:
                rule = violation.get("rule", "?")
                vfile = violation.get("file", "?")
                print(f"      [{rule}] {vfile}: {violation.get('detail', '')}")
            detail = (block.get("detail") or "").strip()
            if detail:
                first_line = detail.splitlines()[0]
                print(f"      {first_line}")

    if audit.get("error"):
        print()
        error_type = audit.get("error_type")
        if error_type:
            print(f"  Error: {error_type}: {audit['error']}")
        else:
            print(f"  Error: {audit['error']}")

    print(f"{sep}")
    return 0


def register_parser(subparsers: object) -> None:
    """Register the 'audit' subcommand parser."""
    audit_parser = subparsers.add_parser("audit", help="Print audit log summary")
    audit_parser.add_argument(
        "file", help="Path to audit file (e.g. .forge/audits/forge_audit.yaml)"
    )
