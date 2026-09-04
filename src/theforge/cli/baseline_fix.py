"""Route a reproduced sprint baseline failure into the normal run pipeline."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from theforge import detach as _detach
from theforge.baseline_repair import (
    BaselineRepairError,
    load_baseline_repair_evidence,
    render_issue_body,
    render_issue_title,
)
from theforge.cli.overrides import apply_base_branch_override
from theforge.cli.shared import (
    _find_config,
    _write_audit,
    check_run_preconditions,
    load_config_checked,
)
from theforge.config import load_config
from theforge.coordinator.engine import run_task
from theforge.coordinator.state import Phase
from theforge.coordinator.util import _generate_run_id
from theforge.coordinator.util import set_log_level as coordinator_set_log_level
from theforge.runners import LogLevel
from theforge.runners import set_log_level as runner_set_log_level
from theforge.shape_check.producer import validate_issue_body
from theforge.shape_check.types import ShapeVerdict
from theforge.sprint.sources import GitHubIssueSource

GH_TIMEOUT_SECONDS = 60
_ISSUE_URL_RE = re.compile(r"/issues/(?P<number>\d+)\s*$")


def cmd_baseline_fix(args: argparse.Namespace) -> int:
    """Create and execute a baseline-repair issue from a sprint audit."""
    config_path = Path(args.config).resolve() if getattr(args, "config", None) else _find_config()
    if config_path is None or not config_path.exists():
        print(
            "forge.yaml not found. Run 'forge init' to create one, "
            "or pass --config path/to/forge.yaml",
            file=sys.stderr,
        )
        return 1

    blockers, warnings = check_run_preconditions(config_path.parent)
    for warning in warnings:
        print(f"⚠ {warning}", file=sys.stderr)
    if blockers:
        print(
            "✗ forge baseline-fix aborted — catastrophic paths are tracked in git:",
            file=sys.stderr,
        )
        for blocker in blockers:
            print(f"  ✗ {blocker}", file=sys.stderr)
        return 1

    config = apply_base_branch_override(
        load_config_checked(config_path, loader=load_config),
        getattr(args, "base_branch", None),
    )

    if getattr(args, "verbose", False):
        coordinator_set_log_level(LogLevel.VERBOSE)
        runner_set_log_level(LogLevel.VERBOSE)

    audit_path = _resolve_sprint_audit_path(args, config.project_root)
    if audit_path is None:
        return 1

    try:
        baseline = load_baseline_repair_evidence(audit_path)
    except BaselineRepairError as exc:
        print(f"forge baseline-fix: {exc}", file=sys.stderr)
        return 1

    requested_run_id = getattr(args, "run", None)
    if requested_run_id:
        if baseline.sprint_run_id is None:
            print(
                "forge baseline-fix: --run only matches run-keyed sprint audits "
                f"(got {audit_path.name!r})",
                file=sys.stderr,
            )
            return 1
        if baseline.sprint_run_id != requested_run_id:
            print(
                "forge baseline-fix: selected sprint audit does not match "
                f"--run {requested_run_id} (audit is for {baseline.sprint_run_id})",
                file=sys.stderr,
            )
            return 1

    auto_merge = bool(getattr(args, "auto_merge", False))
    if not auto_merge and config.workspace.on_approve not in ("merge", "merge-pr"):
        print(
            "forge baseline-fix requires a landing workflow. Pass --auto-merge, "
            "or configure workspace.on_approve as 'merge' or 'merge-pr'.",
            file=sys.stderr,
        )
        return 1

    issue_title = render_issue_title(baseline)
    issue_body = render_issue_body(baseline)
    if not _validated_issue_body(issue_title, issue_body):
        return 1
    created = _create_issue(config.project_root, title=issue_title, body=issue_body)
    if created is None:
        return 1
    issue_number, issue_url = created
    print(f"Created baseline repair issue #{issue_number}: {issue_url}", file=sys.stderr)

    source = GitHubIssueSource()
    try:
        task = source.fetch(str(issue_number), config.project_root)
    except Exception as exc:  # noqa: BLE001
        print(
            "forge baseline-fix: created issue "
            f"#{issue_number} but could not load it as a story: {exc}",
            file=sys.stderr,
        )
        return 1

    run_id = _generate_run_id()
    _detach.export_run_context(run_id, config.project_root)
    _detach.write_pid(run_id, task.slug, config.project_root)

    outcome = "completed"
    cause: str | None = None
    try:
        result = run_task(
            config,
            task,
            interactive=bool(getattr(args, "interactive", False)),
            auto_merge=auto_merge,
            notify=not bool(getattr(args, "no_notify", False)),
            run_id=run_id,
            no_pull=bool(getattr(args, "no_pull", False)),
        )
        audit_record_path = _write_audit(result, config, task, auto_merge=auto_merge)

        if result.success:
            try:
                source.on_complete(task, result, config)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[forge] WARN on_complete callback failed for {task.slug}: {exc}",
                    file=sys.stderr,
                )
        elif result.phase == Phase.ESCALATE and not getattr(
            result, "infrastructure_failure", False
        ):
            try:
                source.on_escalate(task, result.state, config)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[forge] WARN on_escalate callback failed for {task.slug}: {exc}",
                    file=sys.stderr,
                )

        _print_summary(
            result=result,
            audit_path=audit_record_path,
            issue_number=issue_number,
            issue_url=issue_url,
            baseline=baseline,
        )
        return 0 if result.success else 1
    except BaseException as exc:
        outcome = "failed"
        cause = _detach.format_exception_cause(exc)
        raise
    finally:
        _detach.write_run_ended(run_id, config.project_root, outcome, cause=cause)
        _detach.remove_pid(run_id, config.project_root)


def register_parser(subparsers: object) -> None:
    """Register the ``baseline-fix`` subcommand."""
    parser = subparsers.add_parser(
        "baseline-fix",
        help="Create a bug from a reproduced broken baseline and run it through forge",
    )
    parser.add_argument(
        "--sprint-audit",
        metavar="PATH",
        default=None,
        help="Path to the sprint audit to repair (default: latest only when unambiguous)",
    )
    parser.add_argument(
        "--run",
        metavar="RUN_ID",
        default=None,
        help="Sprint run id whose run-<id>-sprint-audit.yaml should be repaired",
    )
    parser.add_argument("--config", help="Path to forge.yaml (default: auto-detect)")
    parser.add_argument(
        "--base-branch",
        default=None,
        help="Override workspace.base_branch for this run without editing forge.yaml",
    )
    parser.add_argument(
        "--auto-merge",
        action="store_true",
        default=False,
        help="Force local merge after APPROVE when config would otherwise not land the fix",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        default=False,
        help="Pause at APPROVE for human confirmation before merging",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Show tool activity, heartbeats, and raw agent output (verbose mode)",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        default=False,
        help="Suppress OS notifications",
    )
    parser.add_argument(
        "--no-pull",
        action="store_true",
        default=False,
        help="Skip git pull --ff-only before creating a fresh workspace",
    )


def _resolve_sprint_audit_path(args: argparse.Namespace, project_root: Path) -> Path | None:
    explicit = getattr(args, "sprint_audit", None)
    if explicit:
        path = Path(explicit).resolve()
        if not path.is_file():
            print(f"forge baseline-fix: sprint audit not found: {path}", file=sys.stderr)
            return None
        return path

    run_id = getattr(args, "run", None)
    audits_dir = project_root / ".forge" / "audits"
    if run_id:
        path = audits_dir / f"run-{run_id}-sprint-audit.yaml"
        if not path.is_file():
            print(
                f"forge baseline-fix: no per-run sprint audit found for {run_id} at {path}",
                file=sys.stderr,
            )
            return None
        return path

    candidates = _per_run_sprint_audits(audits_dir)
    if not candidates:
        legacy = audits_dir / "sprint-audit.yaml"
        if legacy.is_file():
            return legacy
        print(
            "forge baseline-fix: no sprint audit found. Pass --sprint-audit or --run.",
            file=sys.stderr,
        )
        return None

    if len(candidates) > 1:
        preview = ", ".join(path.name for path in candidates[:3])
        if len(candidates) > 3:
            preview += ", ..."
        print(
            "forge baseline-fix: latest sprint audit is ambiguous "
            f"({preview}); pass --sprint-audit or --run.",
            file=sys.stderr,
        )
        return None
    return candidates[0]


def _per_run_sprint_audits(audits_dir: Path) -> list[Path]:
    if not audits_dir.is_dir():
        return []
    candidates: list[tuple[int, Path]] = []
    for path in audits_dir.glob("run-*-sprint-audit.yaml"):
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            continue
        candidates.append((stamp, path))
    return [path for _stamp, path in sorted(candidates, reverse=True)]


def _create_issue(project_root: Path, *, title: str, body: str) -> tuple[int, str] | None:
    path = _write_temp(body)
    try:
        proc = subprocess.run(
            [
                "gh",
                "issue",
                "create",
                "--title",
                title,
                "--body-file",
                str(path),
                "--label",
                "bug",
            ],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=GH_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"gh issue create failed: {exc}", file=sys.stderr)
        return None
    finally:
        path.unlink(missing_ok=True)
    if proc.returncode != 0:
        print(_gh_error(proc, "gh issue create failed"), file=sys.stderr)
        return None
    issue_url = _extract_issue_url(proc.stdout)
    if issue_url is None:
        print("gh issue create produced no issue URL", file=sys.stderr)
        return None
    match = _ISSUE_URL_RE.search(issue_url)
    if match is None:
        print(f"gh issue create returned an unparseable issue URL: {issue_url}", file=sys.stderr)
        return None
    return int(match.group("number")), issue_url


def _extract_issue_url(stdout: str) -> str | None:
    for line in reversed(stdout.strip().splitlines()):
        stripped = line.strip()
        if "/issues/" in stripped:
            return stripped
    return None


def _gh_error(proc: subprocess.CompletedProcess[str], fallback: str) -> str:
    return proc.stderr.strip() or proc.stdout.strip() or fallback


def _validated_issue_body(title: str, body: str) -> bool:
    validation = validate_issue_body(
        producer="forge-baseline-fix-create",
        title=title,
        body=body,
        labels=["bug"],
        declared=ShapeVerdict.DIAGNOSIS_CAUSE_UNKNOWN,
    )
    if validation.conforms:
        return True
    print(validation.report(), file=sys.stderr)
    return False


def _print_summary(
    *,
    result: object,
    audit_path: Path,
    issue_number: int,
    issue_url: str,
    baseline: object,
) -> None:
    print(file=sys.stderr)
    print(f"{'=' * 60}", file=sys.stderr)
    icon = "✓" if getattr(result, "success", False) else "✗"
    print(f"  {icon} {getattr(result, 'message', '')}", file=sys.stderr)
    print(f"  Issue:      #{issue_number} ({issue_url})", file=sys.stderr)
    print(f"  Audit log:  {audit_path}", file=sys.stderr)
    total_cost = getattr(getattr(result, "state", None), "total_cost", 0.0) or 0.0
    print(f"  Total cost: ${float(total_cost):.3f}", file=sys.stderr)
    if not getattr(result, "success", False):
        evidence_path = getattr(baseline, "evidence_path", None)
        evidence_unavailable = getattr(baseline, "evidence_unavailable", None)
        worktree = getattr(baseline, "worktree", None)
        if evidence_path is not None:
            print(f"  Baseline evidence: {evidence_path}", file=sys.stderr)
        elif evidence_unavailable:
            print(f"  Baseline evidence: {evidence_unavailable}", file=sys.stderr)
        if worktree is not None:
            print(f"  Baseline worktree: {worktree}", file=sys.stderr)
    print(f"{'=' * 60}", file=sys.stderr)


def _write_temp(text: str) -> Path:
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".md", delete=False) as handle:
        handle.write(text)
        handle.flush()
        return Path(handle.name)
