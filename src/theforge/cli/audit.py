"""forge audit subcommand — display human-readable audit summaries."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from theforge.coordinator.util import _fmt_duration


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
    print(f"  Cost:  ${cost.get('total_usd', 0):.4f}")

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
        pf_cost = preflight.get("cost_usd", 0.0) or 0.0
        print()
        print(f"  Preflight: {pf_verdict} (${pf_cost:.4f})")
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
    print(f"    Dev iterations: {iterations.get('dev_iterations', '?')}")
    print(f"    Review cycles:  {iterations.get('review_cycles', '?')}")
    print(f"    Gate decisions: {iterations.get('gate_decisions', [])}")

    # Cost summary
    print()
    print("  Cost")
    print(f"    Total:  ${cost.get('total_usd', 0):.4f}")
    dev_inv = cost.get("dev_invocations", 0)
    rev_inv = cost.get("review_invocations", 0)
    print(f"    Dev:    ${cost.get('dev_usd', 0):.4f}  ({dev_inv} invocation(s))")
    print(f"    Review: ${cost.get('review_usd', 0):.4f}  ({rev_inv} invocation(s))")

    # Per-agent breakdown
    agents = cost.get("agents", [])
    if agents:
        print()
        print(f"  {'Role':<10} {'Profile':<20} {'Cost (USD)':>12}  {'Duration':>10}")
        print(f"  {'-' * 10} {'-' * 20} {'-' * 12}  {'-' * 10}")
        for a in agents:
            role = a.get("role", "?")
            profile = a.get("profile", "?")
            cost_usd = a.get("cost_usd", 0.0) or 0.0
            dur = a.get("duration_seconds")
            dur_str = _fmt_duration(dur) if dur is not None else "—"
            print(f"  {role:<10} {profile:<20} ${cost_usd:>11.4f}  {dur_str:>10}")

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

            findings = r.get("findings", [])
            if findings:
                for finding in findings:
                    sev = finding.get("severity", "?")
                    ffile = finding.get("file", "?")
                    line = finding.get("line")
                    loc = f"{ffile}:{line}" if line else ffile
                    desc = finding.get("description", "")
                    print(f"      [{sev}] {loc} — {desc}")

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
