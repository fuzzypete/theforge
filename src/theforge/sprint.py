"""Sprint mode: run multiple specs sequentially through the full pipeline.

A sprint is defined by a YAML manifest listing spec paths to run in order,
with an aggregate budget ceiling (Claude costs only).
"""

from __future__ import annotations

import datetime
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import ForgeConfig
from .coord_audit import has_review_approve
from .coordinator import (
    CoordinatorResult,
    StructuredLogger,
    _fmt_duration,
    _generate_run_id,
    _is_remote_mode,
    _notify,
    _ntfy_publish,
    _run_gate,
    generate_audit_log,
    run_from_dev,
    run_from_review,
    run_task,
)
from .task import TaskSpec


@dataclass
class SprintManifest:
    """Parsed sprint.yaml manifest."""

    name: str
    budget_usd: float
    specs: list[str]  # relative paths to spec files


@dataclass
class SprintResult:
    """Aggregate result from running a sprint."""

    name: str
    specs_total: int
    specs_succeeded: int
    specs_failed: int
    specs_skipped: int  # ALREADY_DONE or budget-stopped
    total_cost_usd: float
    budget_usd: float
    results: list[tuple[str, CoordinatorResult]] = field(default_factory=list)
    stopped_reason: str | None = None  # why sprint stopped early, if it did


def load_sprint_manifest(manifest_path: Path) -> SprintManifest:
    """Load and validate a sprint.yaml manifest.

    Raises ValueError if the manifest is invalid.
    """
    if not manifest_path.exists():
        raise ValueError(f"Sprint manifest not found: {manifest_path}")

    with open(manifest_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    if not isinstance(raw, dict):
        raise ValueError(f"Sprint manifest must be a YAML mapping: {manifest_path}")

    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise ValueError("Sprint manifest must have a non-empty 'name' field")

    budget_usd = raw.get("budget_usd")
    if budget_usd is None:
        raise ValueError("Sprint manifest must have a 'budget_usd' field")
    try:
        budget_usd = float(budget_usd)
    except (TypeError, ValueError):
        raise ValueError(f"Sprint 'budget_usd' must be a number, got {budget_usd!r}")
    if budget_usd <= 0:
        raise ValueError(f"Sprint 'budget_usd' must be > 0, got {budget_usd}")

    specs = raw.get("specs")
    if not specs or not isinstance(specs, list):
        raise ValueError("Sprint manifest must have a non-empty 'specs' list")
    if not all(isinstance(s, str) for s in specs):
        raise ValueError("All entries in 'specs' must be strings (file paths)")

    return SprintManifest(name=name, budget_usd=budget_usd, specs=specs)


def _validate_spec_paths(manifest: SprintManifest, project_root: Path) -> list[Path]:
    """Resolve and validate all spec paths. Raises ValueError if any are missing."""
    resolved: list[Path] = []
    missing: list[str] = []
    for spec_str in manifest.specs:
        path = (project_root / spec_str).resolve()
        if not path.exists():
            missing.append(spec_str)
        else:
            resolved.append(path)
    if missing:
        raise ValueError(
            f"Sprint manifest references {len(missing)} missing spec(s):\n"
            + "\n".join(f"  {s}" for s in missing)
        )
    return resolved


def _build_task_from_spec(spec_path: Path) -> TaskSpec:
    """Build a TaskSpec from a spec file using frontmatter if available."""
    # Import here to avoid circular imports; cli._build_task is essentially the same logic
    text = spec_path.read_text(encoding="utf-8")
    fm: dict = {}
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            try:
                parsed = yaml.safe_load(text[3:end].strip()) or {}
                if isinstance(parsed, dict):
                    fm = parsed
            except yaml.YAMLError:
                pass

    slug = fm.get("slug") or spec_path.stem
    name = fm.get("name", spec_path.stem.replace("_", " ").replace("-", " ").title())
    raw_deps = fm.get("depends_on", [])
    if isinstance(raw_deps, str):
        depends_on = [raw_deps]
    elif isinstance(raw_deps, list):
        depends_on = [str(d) for d in raw_deps]
    else:
        depends_on = []
    return TaskSpec(
        name=name,
        spec_path=spec_path,
        slug=slug,
        pytest_target=fm.get("pytest_target"),
        gate_override=fm.get("gate"),
        depends_on=depends_on,
    )


def _log(msg: str) -> None:
    print(f"[sprint] {msg}", file=sys.stderr, flush=True)


def _spec_header(idx: int, total: int, slug: str) -> str:
    """Format a spec header line: [N/total] slug ─────... (fills to 60 chars)."""
    prefix = f"[{idx}/{total}] {slug} "
    dashes = "─" * max(0, 60 - len(prefix))
    return prefix + dashes


@dataclass
class SpecTriage:
    """Result of triaging a spec for sprint resume."""

    spec_path: str
    action: str  # "skip_merged", "skip", "review", "dev", "full"
    reason: str
    worktree_path: Path | None = None
    slug: str = ""


def _triage_spec(
    spec_path: str,
    config: ForgeConfig,
    project_root: Path,
) -> SpecTriage:
    """Determine the optimal re-entry point for a spec.

    Decision tree:
      merged to base?           → skip_merged
      no worktree?              → full
      0 commits ahead?          → full (stale; run_task will clean up)
      gate passes?              → review
      gate fails?               → dev
    """
    full_path = (project_root / spec_path).resolve()
    task = _build_task_from_spec(full_path)
    slug = task.slug

    branch = config.workspace.branch_pattern.format(slug=slug)
    base_branch = config.workspace.base_branch
    worktree_path = project_root / config.workspace.path_pattern.format(slug=slug)

    # 1. Check if already merged to base branch
    # --is-ancestor alone is not enough: a branch created at main HEAD with
    # zero new commits also passes --is-ancestor.  We must also verify the
    # branch has commits that diverge from main (i.e. it's not just pointing
    # at the same commit or an older one).
    try:
        merge_result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", branch, base_branch],
            cwd=str(project_root),
            capture_output=True,
            timeout=30,
        )
        if merge_result.returncode == 0:
            # Verify branch actually has unique commits (not just at base HEAD)
            ahead_result = subprocess.run(
                ["git", "rev-list", f"{base_branch}..{branch}", "--count"],
                cwd=str(project_root),
                capture_output=True,
                timeout=30,
            )
            ahead_count = int(ahead_result.stdout.decode("utf-8", errors="replace").strip() or "0")
            if ahead_count > 0:
                return SpecTriage(
                    spec_path=spec_path,
                    action="skip_merged",
                    reason=f"already merged to {base_branch}",
                    worktree_path=None,
                    slug=slug,
                )
            # ahead_count == 0: branch exists at base HEAD, not truly merged —
            # fall through to worktree checks below
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass  # Branch may not exist yet — treat as not merged

    # 2. Check if worktree exists
    if not worktree_path.exists():
        return SpecTriage(
            spec_path=spec_path,
            action="full",
            reason="no worktree found",
            worktree_path=None,
            slug=slug,
        )

    # 3. Check commits ahead of base branch
    try:
        log_result = subprocess.run(
            ["git", "log", f"{base_branch}..{branch}", "--oneline"],
            cwd=str(project_root),
            capture_output=True,
            timeout=30,
        )
        commits_ahead = [
            ln
            for ln in log_result.stdout.decode("utf-8", errors="replace").strip().splitlines()
            if ln.strip()
        ]
    except (subprocess.TimeoutExpired, OSError):
        commits_ahead = []

    if not commits_ahead:
        # Stale worktree: 0 commits ahead of base. run_task WORKSPACE phase will
        # recreate it from scratch. Pass worktree_path=None so callers don't reuse it.
        return SpecTriage(
            spec_path=spec_path,
            action="full",
            reason=f"worktree exists but 0 commits ahead of {base_branch} (stale)",
            worktree_path=None,
            slug=slug,
        )

    # 4. Check audit trail for a prior review APPROVE
    if has_review_approve(project_root, slug, base_branch, branch):
        return SpecTriage(
            spec_path=spec_path,
            action="skip",
            reason=f"prior APPROVE in audit trail ({len(commits_ahead)} commits ahead)",
            worktree_path=worktree_path,
            slug=slug,
        )

    # 5. Gate pre-check to decide REVIEW vs DEV entry
    gate_decision, gate_err, _gate_output = _run_gate(config, worktree_path, task=task)

    if gate_err is None and gate_decision == "PASS":
        return SpecTriage(
            spec_path=spec_path,
            action="review",
            reason=f"worktree exists, gate passes ({len(commits_ahead)} commits ahead)",
            worktree_path=worktree_path,
            slug=slug,
        )

    reason_detail = gate_err or f"gate returned {gate_decision}"
    return SpecTriage(
        spec_path=spec_path,
        action="dev",
        reason=f"worktree exists, gate fails ({reason_detail})",
        worktree_path=worktree_path,
        slug=slug,
    )


def _read_prior_sprint_cost(project_root: Path) -> float:
    """Read total_cost_usd from the prior sprint-audit.yaml, if it exists."""
    audit_path = project_root / ".forge" / "audits" / "sprint-audit.yaml"
    if not audit_path.exists():
        return 0.0
    try:
        with open(audit_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return float(data.get("sprint", {}).get("total_cost_usd", 0.0))
    except (OSError, ValueError, TypeError):
        return 0.0


def run_sprint(
    config: ForgeConfig,
    manifest_path: Path,
    *,
    auto_merge: bool = False,
    interactive: bool = False,
    notify: bool = False,
    resume: bool = False,
) -> SprintResult:
    """Run all specs in a sprint manifest sequentially.

    Budget enforcement tracks Claude costs only; Codex/Gemini invocations
    report $0.00 and do not count toward the ceiling.

    Args:
        config: Loaded ForgeConfig for the project.
        manifest_path: Path to the sprint.yaml manifest.
        auto_merge: If True, merge each spec's branch after APPROVE.
        interactive: If True, pause for human review at each spec.
        resume: If True, triage each spec to find the optimal re-entry point
            (skip_merged / review / dev / full) and carry forward prior costs.

    Returns:
        SprintResult with per-spec outcomes and aggregate stats.
    """
    manifest = load_sprint_manifest(manifest_path)
    spec_paths = _validate_spec_paths(manifest, config.project_root)

    total = len(spec_paths)
    plural = "s" if total != 1 else ""
    print(
        f'[sprint] "{manifest.name}"  {total} spec{plural}  budget=${manifest.budget_usd:.2f}',
        file=sys.stderr,
        flush=True,
    )
    _log("⚠ Budget tracks Claude costs only (Codex/Gemini report $0.00)")

    # Sprint-level structured logger
    _sprint_run_id = _generate_run_id()
    _sprint_logger = StructuredLogger(
        run_id=_sprint_run_id,
        project=config.project,
        task=manifest.name,
        log_file=config.log.log_file,
        enabled=config.log.enabled,
        project_root=config.project_root,
    )
    _sprint_logger.emit(
        "run_start",
        specs=manifest.specs,
        budget_usd=manifest.budget_usd,
        resume=resume,
    )

    # Create sprint-level log directory
    _sprint_log_dir = config.project_root / ".forge" / "logs" / manifest.name
    try:
        _sprint_log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        _sprint_log_dir = None  # type: ignore[assignment]

    started_at = datetime.datetime.now(datetime.timezone.utc)
    accumulated_cost = 0.0
    prior_cost = 0.0
    results: list[tuple[str, CoordinatorResult]] = []
    specs_succeeded = 0
    specs_failed = 0
    specs_skipped = 0
    stopped_reason: str | None = None
    merged_slugs: set[str] = set()

    # Pre-scan: parse all TaskSpecs once and build dependent_slugs.
    # Caching avoids re-reading spec files in the main loop.
    # We collect all dependents regardless of manifest ordering — merging earlier
    # is safe and simpler than strict look-ahead filtering.
    _parsed_tasks: dict[Path, object] = {_sp: _build_task_from_spec(_sp) for _sp in spec_paths}
    dependent_slugs: set[str] = set()
    for _t in _parsed_tasks.values():
        dependent_slugs.update(_t.depends_on)  # type: ignore[union-attr]

    # Resume mode: triage all specs and carry forward prior costs
    triages: dict[str, SpecTriage] = {}
    if resume:
        prior_cost = _read_prior_sprint_cost(config.project_root)
        if prior_cost > 0.0:
            _log(f"Resuming with prior cost: ${prior_cost:.2f}")
        _log("Triaging specs...")
        for spec_str in manifest.specs:
            triage = _triage_spec(spec_str, config, config.project_root)
            triages[spec_str] = triage
            action_label = triage.action.upper().replace("_", " ")
            _log(f"  {triage.slug:<20} {action_label} ({triage.reason})")

    for idx, spec_path in enumerate(spec_paths, start=1):
        spec_str = manifest.specs[idx - 1]
        task = _parsed_tasks[spec_path]  # type: ignore[assignment]

        # Resume mode: skip already-merged or already-approved specs before budget check.
        # These represent completed work; not subject to budget enforcement.
        if resume:
            triage = triages.get(spec_str)
            if triage and triage.action == "skip_merged":
                _log(f"[{idx}/{total}] SKIP {task.slug} ({triage.reason})")
                specs_succeeded += 1
                merged_slugs.add(task.slug)
                continue
            if triage and triage.action == "skip":
                _log(f"[{idx}/{total}] SKIP {task.slug} — already approved ({triage.reason})")
                specs_succeeded += 1
                # A prior APPROVE means the work is done and satisfies downstream deps,
                # even if it was never merged to main (e.g. auto_merge=False previously).
                merged_slugs.add(task.slug)
                continue

        # Budget check before starting (cumulative: prior + current run)
        cumulative_cost = prior_cost + accumulated_cost
        if cumulative_cost >= manifest.budget_usd:
            acc = cumulative_cost
            bud = manifest.budget_usd
            _log(f"[{idx}/{total}] SKIPPED (budget exhausted: ${acc:.2f} >= ${bud:.2f})")
            specs_skipped += 1
            stopped_reason = f"Budget exhausted (${acc:.2f} >= ${bud:.2f})"
            # Mark remaining specs as skipped too
            for remaining_idx in range(idx + 1, total + 1):
                _log(f"[{remaining_idx}/{total}] SKIPPED (budget exhausted)")
                specs_skipped += 1
            break

        # Dependency check: all depends_on slugs must have merged.
        # Only this spec is skipped — independent specs continue.
        missing_deps = [dep for dep in task.depends_on if dep not in merged_slugs]
        if missing_deps:
            dep_list = ", ".join(missing_deps)
            _log(f"[{idx}/{total}] SKIPPED {task.slug} (dependency failed: {dep_list})")
            specs_skipped += 1
            continue

        # Emit spec header banner
        print(_spec_header(idx, total, task.slug), file=sys.stderr, flush=True)

        _spec_start = datetime.datetime.now(datetime.timezone.utc)

        # Eager merge: if any later spec declares this slug as a dependency, force
        # auto_merge so the merged code is on main before the dependent starts.
        effective_auto_merge = auto_merge or (task.slug in dependent_slugs)

        # Choose entry point based on triage
        if resume:
            triage = triages.get(spec_str)
            if triage and triage.action == "review" and triage.worktree_path is not None:
                result = run_from_review(
                    config,
                    task,
                    triage.worktree_path,
                    interactive=interactive,
                    auto_merge=effective_auto_merge,
                    notify=notify,
                    run_id=_sprint_run_id,
                    sprint_name=manifest.name,
                )
            elif triage and triage.action == "dev" and triage.worktree_path is not None:
                result = run_from_dev(
                    config,
                    task,
                    triage.worktree_path,
                    interactive=interactive,
                    auto_merge=effective_auto_merge,
                    notify=notify,
                    run_id=_sprint_run_id,
                    sprint_name=manifest.name,
                )
            else:
                result = run_task(
                    config,
                    task,
                    interactive=interactive,
                    auto_merge=effective_auto_merge,
                    notify=notify,
                    run_id=_sprint_run_id,
                    sprint_name=manifest.name,
                )
        else:
            result = run_task(
                config,
                task,
                interactive=interactive,
                auto_merge=effective_auto_merge,
                notify=notify,
                run_id=_sprint_run_id,
                sprint_name=manifest.name,
            )
        _spec_elapsed = (
            datetime.datetime.now(datetime.timezone.utc) - _spec_start
        ).total_seconds()
        results.append((spec_str, result))

        # Write per-spec audit to worktree for diagnostics
        workspace_path = config.project_root / config.workspace.path_pattern.format(slug=task.slug)
        if workspace_path.exists():
            audit = generate_audit_log(config, task, result)
            audit_path = workspace_path / "forge_audit.yaml"
            with open(audit_path, "w", encoding="utf-8") as f:
                yaml.dump(audit, f, default_flow_style=False, sort_keys=False)
            _log(f"[{idx}/{total}] Per-spec audit written: {audit_path}")
        # Copy audit to durable per-story log dir
        if result.state.log_dir is not None:
            try:
                audit = generate_audit_log(config, task, result)
                _story_audit_path = result.state.log_dir / "audit.yaml"
                _story_audit_path.parent.mkdir(parents=True, exist_ok=True)
                with open(_story_audit_path, "w", encoding="utf-8") as f:
                    yaml.dump(audit, f, default_flow_style=False, sort_keys=False)
            except Exception:
                pass  # best-effort

        spec_cost = result.state.total_cost
        accumulated_cost += spec_cost

        # Classify outcome
        preflight_verdict = result.state.preflight_verdict
        if preflight_verdict == "ALREADY_DONE":
            specs_skipped += 1
        elif result.success:
            specs_succeeded += 1
        else:
            specs_failed += 1

        # Track merged slugs for dependency checking.
        # ALREADY_DONE means the spec's changes are already on main, so it satisfies deps.
        if preflight_verdict == "ALREADY_DONE" or (
            result.merge is not None and result.merge.get("merged", False)
        ):
            merged_slugs.add(task.slug)

        # Emit spec completion summary
        icon = "✓" if result.success else "✗"
        dur = _fmt_duration(_spec_elapsed)
        _log(f"[{idx}/{total}] {icon} {task.slug}   ${spec_cost:.2f}  {dur}")

        # Stop sprint if budget exceeded after this run (cumulative)
        if (prior_cost + accumulated_cost) >= manifest.budget_usd and idx < total:
            acc = prior_cost + accumulated_cost
            bud = manifest.budget_usd
            stopped_reason = f"Budget exceeded after spec {idx} (${acc:.2f} >= ${bud:.2f})"
            for remaining_idx in range(idx + 1, total + 1):
                _log(f"[{remaining_idx}/{total}] SKIPPED (budget exceeded)")
                specs_skipped += 1
            _log(f"Stopping sprint: {stopped_reason}")
            break

    finished_at = datetime.datetime.now(datetime.timezone.utc)
    duration = (finished_at - started_at).total_seconds()

    final_cost = accumulated_cost + prior_cost
    sprint_result = SprintResult(
        name=manifest.name,
        specs_total=total,
        specs_succeeded=specs_succeeded,
        specs_failed=specs_failed,
        specs_skipped=specs_skipped,
        total_cost_usd=final_cost,
        budget_usd=manifest.budget_usd,
        results=results,
        stopped_reason=stopped_reason,
    )

    _sprint_elapsed = (datetime.datetime.now(datetime.timezone.utc) - started_at).total_seconds()
    _sprint_dur = _fmt_duration(_sprint_elapsed)
    _log(
        f"Sprint complete: {specs_succeeded} succeeded, {specs_failed} failed, "
        f"{specs_skipped} skipped. Total: ${final_cost:.2f}  {_sprint_dur}"
    )
    _sprint_outcome = "done" if specs_failed == 0 and stopped_reason is None else "partial"
    _sprint_logger.emit(
        "run_end",
        outcome=_sprint_outcome,
        total_cost_usd=round(final_cost, 6),
        total_duration_s=round(_sprint_elapsed, 2),
    )
    if notify:
        _notify(
            f"TheForge: {manifest.name}",
            f"✓ {specs_succeeded} passed, ✗ {specs_failed} failed"
            f" — ${final_cost:.2f}  {_fmt_duration(_sprint_elapsed)}",
        )
        # R10: ntfy summary notification when remote mode is active
        if _is_remote_mode(notify, config):
            assert config.notifications.ntfy is not None
            _ntfy_title = f'TheForge sprint complete \u2014 "{manifest.name}"'
            _ntfy_body_lines = [
                f"{total} specs: {specs_succeeded} succeeded \u00b7 {specs_failed} failed",
                f"Total cost: ${final_cost:.2f}   Duration: {_fmt_duration(_sprint_elapsed)}",
            ]
            if stopped_reason:
                _ntfy_body_lines.append(f"Stopped: {stopped_reason}")
            _ntfy_publish(
                config.notifications.ntfy.url,
                _ntfy_title,
                "\n".join(_ntfy_body_lines),
                priority=config.notifications.ntfy.priority,
            )

    # Write sprint-audit.yaml (existing format; kept for backward compatibility)
    _write_sprint_audit(
        manifest=manifest,
        result=sprint_result,
        spec_paths=spec_paths,
        started_at=started_at,
        finished_at=finished_at,
        duration=duration,
        project_root=config.project_root,
    )

    # Write sprint-summary.yaml to .forge/logs/<sprint-name>/
    if _sprint_log_dir is not None:
        _write_sprint_summary(
            manifest=manifest,
            result=sprint_result,
            spec_paths=spec_paths,
            started_at=started_at,
            finished_at=finished_at,
            duration=duration,
            sprint_log_dir=_sprint_log_dir,
        )

    # ── POST_SPRINT hook ──────────────────────────────────────────────
    if config.hooks and config.hooks.post_sprint:
        from .coord_hooks import build_post_sprint_payload
        from .coord_hooks import run_hook as _run_hook

        _stories = []
        for spec_str, res in results:
            # Derive slug: use workspace_path leaf (set during WORKSPACE phase) or spec stem
            _ws = res.state.workspace_path
            if _ws is not None:
                _slug = _ws.name
            else:
                _slug = Path(spec_str).stem
            _verdict = ""
            if res.state.review_results:
                _verdict = res.state.review_results[-1].verdict
            elif res.success:
                _verdict = "APPROVE"
            _stories.append(
                {
                    "slug": _slug,
                    "outcome": "done" if res.success else "escalate",
                    "verdict": _verdict,
                    "merged": res.merge is not None and res.merge.get("merged", False),
                }
            )
        _ps_payload = build_post_sprint_payload(
            sprint_name=manifest.name,
            stories=_stories,
            run_id=_sprint_run_id,
            config=config,
            total_cost_usd=final_cost,
            duration_seconds=_sprint_elapsed,
        )
        _run_hook(
            config.hooks.post_sprint,
            _ps_payload,
            config.hooks.timeout_seconds,
            "post_sprint",
            _sprint_logger,
        )

    return sprint_result


def _write_sprint_audit(
    manifest: SprintManifest,
    result: SprintResult,
    spec_paths: list[Path],
    started_at: datetime.datetime,
    finished_at: datetime.datetime,
    duration: float,
    project_root: Path,
) -> None:
    """Write sprint-audit.yaml to the project root."""
    # Build per-spec entries
    spec_entries = []
    results_by_spec = {spec_str: res for spec_str, res in result.results}

    for spec_str, spec_path in zip(manifest.specs, spec_paths):
        if spec_str in results_by_spec:
            res = results_by_spec[spec_str]
            preflight = res.state.preflight_verdict or "PROCEED"
            outcome = "ALREADY_DONE" if preflight == "ALREADY_DONE" else res.phase.name

            # Build reviews summary for this spec
            reviews_summary = []
            for i, meta in enumerate(res.state.review_cycle_metadata):
                cycle_entry: dict = {
                    "cycle": i + 1,
                    "pool": meta.pool_models,
                    "successful": meta.successful,
                    "failed": meta.failed,
                    "synthesized": meta.synthesized,
                    "parse_retries": meta.parse_retries,
                }
                if i < len(res.state.review_results):
                    r = res.state.review_results[i]
                    cycle_entry["verdict"] = r.verdict
                    cycle_entry["p1_count"] = sum(1 for f in r.findings if f.severity == "P1")
                    cycle_entry["p2_count"] = sum(1 for f in r.findings if f.severity == "P2")
                reviews_summary.append(cycle_entry)

            entry = {
                "path": spec_str,
                "outcome": outcome,
                "cost_usd": round(res.state.total_cost, 4),
                "preflight": preflight,
                "merge": res.merge is not None and res.merge.get("merged", False),
                "reviews": reviews_summary,
            }
        else:
            # Skipped due to budget
            entry = {
                "path": spec_str,
                "outcome": "SKIPPED",
                "cost_usd": 0.0,
                "preflight": None,
                "merge": False,
                "reviews": [],
            }
        spec_entries.append(entry)

    audit = {
        "sprint": {
            "name": manifest.name,
            "budget_usd": manifest.budget_usd,
            "total_cost_usd": round(result.total_cost_usd, 4),
            "budget_note": "Costs reflect Claude invocations only; Codex/Gemini report $0.00",
            "started_at": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished_at": finished_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_seconds": round(duration, 1),
            "specs_total": result.specs_total,
            "specs_succeeded": result.specs_succeeded,
            "specs_failed": result.specs_failed,
            "specs_skipped": result.specs_skipped,
            "stopped_reason": result.stopped_reason,
        },
        "specs": spec_entries,
    }

    audits_dir = project_root / ".forge" / "audits"
    audits_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audits_dir / "sprint-audit.yaml"
    with open(audit_path, "w", encoding="utf-8") as f:
        yaml.dump(audit, f, default_flow_style=False, sort_keys=False)
    # Append to history log (JSONL, never overwritten).
    history_path = audits_dir / "history.jsonl"
    try:
        import json

        with open(history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(audit, default=str) + "\n")
    except OSError:
        pass
    _log(f"Audit written: {audit_path}")


def _write_sprint_summary(
    manifest: SprintManifest,
    result: SprintResult,
    spec_paths: list[Path],
    started_at: datetime.datetime,
    finished_at: datetime.datetime,
    duration: float,
    sprint_log_dir: Path,
) -> None:
    """Write sprint-summary.yaml to <project_root>/.forge/logs/<sprint-name>/."""
    spec_entries = []
    results_by_spec = {spec_str: res for spec_str, res in result.results}

    for spec_str, spec_path in zip(manifest.specs, spec_paths):
        if spec_str in results_by_spec:
            res = results_by_spec[spec_str]
            preflight = res.state.preflight_verdict or "PROCEED"
            outcome = "ALREADY_DONE" if preflight == "ALREADY_DONE" else res.phase.name
            last_verdict = ""
            if res.state.review_results:
                last_verdict = res.state.review_results[-1].verdict
            elif res.success:
                last_verdict = "APPROVE"
            entry = {
                "path": spec_str,
                "slug": spec_path.stem,
                "outcome": outcome,
                "verdict": last_verdict or None,
                "cost_usd": round(res.state.total_cost, 4),
                "preflight": preflight,
                "merge": res.merge is not None and res.merge.get("merged", False),
            }
        else:
            entry = {
                "path": spec_str,
                "slug": Path(spec_str).stem,
                "outcome": "SKIPPED",
                "verdict": None,
                "cost_usd": 0.0,
                "preflight": None,
                "merge": False,
            }
        spec_entries.append(entry)

    summary = {
        "sprint": {
            "name": manifest.name,
            "budget_usd": manifest.budget_usd,
            "total_cost_usd": round(result.total_cost_usd, 4),
            "started_at": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished_at": finished_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_seconds": round(duration, 1),
            "specs_total": result.specs_total,
            "specs_succeeded": result.specs_succeeded,
            "specs_failed": result.specs_failed,
            "specs_skipped": result.specs_skipped,
            "stopped_reason": result.stopped_reason,
        },
        "stories": spec_entries,
    }

    try:
        sprint_log_dir.mkdir(parents=True, exist_ok=True)
        summary_path = sprint_log_dir / "sprint-summary.yaml"
        with open(summary_path, "w", encoding="utf-8") as f:
            yaml.dump(summary, f, default_flow_style=False, sort_keys=False)
        _log(f"Sprint summary written: {summary_path}")
    except Exception as exc:
        _log(f"Warning: sprint summary write failed: {exc}")
