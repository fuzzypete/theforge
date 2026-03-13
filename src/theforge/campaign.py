"""Campaign mode: run multiple specs sequentially through the full pipeline.

A campaign is defined by a YAML manifest listing spec paths to run in order,
with an aggregate budget ceiling (Claude costs only).
"""

from __future__ import annotations

import datetime
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import ForgeConfig
from .coordinator import CoordinatorResult, _fmt_dur, generate_audit_log, run_task
from .task import TaskSpec


@dataclass
class CampaignManifest:
    """Parsed campaign.yaml manifest."""

    name: str
    budget_usd: float
    specs: list[str]  # relative paths to spec files


@dataclass
class CampaignResult:
    """Aggregate result from running a campaign."""

    name: str
    specs_total: int
    specs_succeeded: int
    specs_failed: int
    specs_skipped: int  # ALREADY_DONE or budget-stopped
    total_cost_usd: float
    budget_usd: float
    results: list[tuple[str, CoordinatorResult]] = field(default_factory=list)
    stopped_reason: str | None = None  # why campaign stopped early, if it did


def load_manifest(manifest_path: Path) -> CampaignManifest:
    """Load and validate a campaign.yaml manifest.

    Raises ValueError if the manifest is invalid.
    """
    if not manifest_path.exists():
        raise ValueError(f"Campaign manifest not found: {manifest_path}")

    with open(manifest_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    if not isinstance(raw, dict):
        raise ValueError(f"Campaign manifest must be a YAML mapping: {manifest_path}")

    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise ValueError("Campaign manifest must have a non-empty 'name' field")

    budget_usd = raw.get("budget_usd")
    if budget_usd is None:
        raise ValueError("Campaign manifest must have a 'budget_usd' field")
    try:
        budget_usd = float(budget_usd)
    except (TypeError, ValueError):
        raise ValueError(f"Campaign 'budget_usd' must be a number, got {budget_usd!r}")
    if budget_usd <= 0:
        raise ValueError(f"Campaign 'budget_usd' must be > 0, got {budget_usd}")

    specs = raw.get("specs")
    if not specs or not isinstance(specs, list):
        raise ValueError("Campaign manifest must have a non-empty 'specs' list")
    if not all(isinstance(s, str) for s in specs):
        raise ValueError("All entries in 'specs' must be strings (file paths)")

    return CampaignManifest(name=name, budget_usd=budget_usd, specs=specs)


def _validate_spec_paths(manifest: CampaignManifest, project_root: Path) -> list[Path]:
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
            f"Campaign manifest references {len(missing)} missing spec(s):\n"
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
    return TaskSpec(
        name=name,
        spec_path=spec_path,
        slug=slug,
        file_scope=fm.get("file_scope", []),
        pytest_target=fm.get("pytest_target"),
    )


def _log(msg: str) -> None:
    print(f"[campaign] {msg}", file=sys.stderr, flush=True)


def _spec_header(idx: int, total: int, slug: str) -> str:
    """Format a spec header line: [N/total] slug ─────... (fills to 60 chars)."""
    prefix = f"[{idx}/{total}] {slug} "
    dashes = "─" * max(0, 60 - len(prefix))
    return prefix + dashes


def run_campaign(
    config: ForgeConfig,
    manifest_path: Path,
    *,
    auto_merge: bool = False,
    interactive: bool = False,
) -> CampaignResult:
    """Run all specs in a campaign manifest sequentially.

    Budget enforcement tracks Claude costs only; Codex/Gemini invocations
    report $0.00 and do not count toward the ceiling.

    Args:
        config: Loaded ForgeConfig for the project.
        manifest_path: Path to the campaign.yaml manifest.
        auto_merge: If True, merge each spec's branch after APPROVE.
        interactive: If True, pause for human review at each spec.

    Returns:
        CampaignResult with per-spec outcomes and aggregate stats.
    """
    manifest = load_manifest(manifest_path)
    spec_paths = _validate_spec_paths(manifest, config.project_root)

    total = len(spec_paths)
    plural = "s" if total != 1 else ""
    print(
        f'[campaign] "{manifest.name}"  {total} spec{plural}  budget=${manifest.budget_usd:.2f}',
        file=sys.stderr,
        flush=True,
    )
    _log("⚠ Budget tracks Claude costs only (Codex/Gemini report $0.00)")

    started_at = datetime.datetime.now(datetime.timezone.utc)
    accumulated_cost = 0.0
    results: list[tuple[str, CoordinatorResult]] = []
    specs_succeeded = 0
    specs_failed = 0
    specs_skipped = 0
    stopped_reason: str | None = None

    for idx, spec_path in enumerate(spec_paths, start=1):
        spec_str = manifest.specs[idx - 1]
        task = _build_task_from_spec(spec_path)

        # Budget check before starting
        if accumulated_cost >= manifest.budget_usd:
            acc = accumulated_cost
            bud = manifest.budget_usd
            _log(f"[{idx}/{total}] SKIPPED (budget exhausted: ${acc:.2f} >= ${bud:.2f})")
            specs_skipped += 1
            stopped_reason = f"Budget exhausted (${acc:.2f} >= ${bud:.2f})"
            # Mark remaining specs as skipped too
            for remaining_idx in range(idx + 1, total + 1):
                _log(f"[{remaining_idx}/{total}] SKIPPED (budget exhausted)")
                specs_skipped += 1
            break

        # Emit spec header banner
        print(_spec_header(idx, total, task.slug), file=sys.stderr, flush=True)

        _spec_start = datetime.datetime.now(datetime.timezone.utc)
        result = run_task(config, task, interactive=interactive, auto_merge=auto_merge)
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

        # Emit spec completion summary
        icon = "✓" if result.success else "✗"
        _log(f"[{idx}/{total}] {icon} {task.slug}   ${spec_cost:.2f}  {_fmt_dur(_spec_elapsed)}")

        # Stop campaign if budget exceeded after this run
        if accumulated_cost >= manifest.budget_usd and idx < total:
            acc = accumulated_cost
            bud = manifest.budget_usd
            stopped_reason = f"Budget exceeded after spec {idx} (${acc:.2f} >= ${bud:.2f})"
            for remaining_idx in range(idx + 1, total + 1):
                _log(f"[{remaining_idx}/{total}] SKIPPED (budget exceeded)")
                specs_skipped += 1
            _log(f"Stopping campaign: {stopped_reason}")
            break

    finished_at = datetime.datetime.now(datetime.timezone.utc)
    duration = (finished_at - started_at).total_seconds()

    campaign_result = CampaignResult(
        name=manifest.name,
        specs_total=total,
        specs_succeeded=specs_succeeded,
        specs_failed=specs_failed,
        specs_skipped=specs_skipped,
        total_cost_usd=accumulated_cost,
        budget_usd=manifest.budget_usd,
        results=results,
        stopped_reason=stopped_reason,
    )

    _campaign_elapsed = (datetime.datetime.now(datetime.timezone.utc) - started_at).total_seconds()
    _log(
        f"Campaign complete: {specs_succeeded} succeeded, {specs_failed} failed, "
        f"{specs_skipped} skipped. Total: ${accumulated_cost:.2f}  {_fmt_dur(_campaign_elapsed)}"
    )

    # Write campaign-audit.yaml
    _write_campaign_audit(
        manifest=manifest,
        result=campaign_result,
        spec_paths=spec_paths,
        started_at=started_at,
        finished_at=finished_at,
        duration=duration,
        project_root=config.project_root,
    )

    return campaign_result


def _write_campaign_audit(
    manifest: CampaignManifest,
    result: CampaignResult,
    spec_paths: list[Path],
    started_at: datetime.datetime,
    finished_at: datetime.datetime,
    duration: float,
    project_root: Path,
) -> None:
    """Write campaign-audit.yaml to the project root."""
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
        "campaign": {
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

    audit_path = project_root / "campaign-audit.yaml"
    with open(audit_path, "w", encoding="utf-8") as f:
        yaml.dump(audit, f, default_flow_style=False, sort_keys=False)
    _log(f"Audit written: {audit_path}")
