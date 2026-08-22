"""Shared helpers for the forge CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import yaml

from theforge.artifacts import (
    AUDIT_PATH,
    ESCALATED_MARKER_PATH,
    ensure_parent_dir,
)
from theforge.config import (
    ForgeConfig,
    _validate_plan_provider,
    load_config,
)
from theforge.config.auth import check_agent_auth
from theforge.config.profiles import iter_config_profiles
from theforge.config.provenance import VALUE_SOURCE_CLI_OVERRIDE, refresh_provenance
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.audit_substrate import CURRENT_RECORD_SCHEMA_VERSION
from theforge.coordinator.knowledge_summary_flow import maybe_generate_run_summary
from theforge.coordinator.redact import redact
from theforge.coordinator.review_context import hard_convention_review_kwargs
from theforge.coordinator.state import CoordinatorResult
from theforge.coordinator.workspace import _base_branch_lands_locally
from theforge.knowledge_index import rebuild_knowledge_index
from theforge.knowledge_summary import summary_path
from theforge.log_util import _log_line
from theforge.sprint.audit_publish import (
    _STORY_RUN_AUDIT_PUBLISH_STATE_PATH,
    AUDIT_PUBLISH_BRANCH_MISMATCH,
    AUDIT_PUBLISH_COMMIT_FAILED,
    AUDIT_PUBLISH_VERIFY_FAILED,
    StoryRunAuditPublishError,
    publish_story_run_artifacts_for_config,
)
from theforge.task import (
    TaskStory,
    build_dev_prompt,
    build_review_prompt,
    frontmatter_allows_forge_yaml_mutation,
    load_story,
    parse_story_frontmatter,
)
from theforge.validation_profiles import PHASE_ADVISORY, PHASE_MERGE, select_validation

_SECRETS_FILE = ".forge/.env"
_UNPUBLISHED_STORY_RUN_ARTIFACTS_DIR = Path(".forge") / "unpublished-story-run-artifacts"

# ── forge run precondition guards ──────────────────────────────────────────
# Some machine-local state under .forge/ is catastrophic to track in git.
# Committing worktree bookkeeping, lock files, daemon state, or pending-run
# state assumes single-machine locality: parallel machines get false-positive
# lock errors, nested git repos break checkout, and committed secrets leak.
# `forge run` fails fast on these before doing any work. Noise paths (logs,
# derived views, the SQLite index) surface a warning but do not block.
#
# Each entry is (display_path, is_dir). Directory entries end in "/" and match
# any tracked file beneath them; file entries match an exact tracked path.
_RUN_BLOCKER_PATHS: list[tuple[str, bool]] = [
    (".forge/worktrees/", True),
    (".forge/locks/", True),
    (".forge/merge.lock", False),
    (".forge/daemon.json", False),
    (".forge/pending/", True),
    (".forge/runs/", True),
    (".forge/.env", False),
    (".forge/secrets.yaml", False),
    ("handoff.yaml", False),
]

_RUN_WARNING_PATHS: list[tuple[str, bool]] = [
    (".forge/logs/", True),
    # history.jsonl becomes a derived-local file after the Phase C migration;
    # warning on it now is harmless (a warning never blocks a run) and matches
    # the AC. If it is still legitimately tracked pre-migration the operator can
    # ignore the notice.
    (".forge/audits/history.jsonl", False),
    (".forge/audits/index.sqlite", False),
    (".forge/assignment_history.yaml", False),
]


def _rm_cached_command(display: str, is_dir: bool) -> str:
    """Build the `git rm --cached` command that stops tracking a path."""
    flag = "-r " if is_dir else ""
    return f"git rm --cached {flag}{display}"


def _log(msg: str) -> None:
    """Emit a single forge-tagged stderr log line."""
    _log_line("[forge]", msg)


def _tracked_files(project_root: Path) -> list[str] | None:
    """Return the list of git-tracked paths under project_root.

    Returns None when the directory is not a git repository or git is
    unavailable — in that case there is nothing to guard against.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
        )
    except (OSError, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line]


def _path_is_tracked(tracked: list[str], display: str, is_dir: bool) -> bool:
    """Report whether any tracked path matches the guarded path."""
    if is_dir:
        prefix = display if display.endswith("/") else display + "/"
        return any(f.startswith(prefix) for f in tracked)
    return display in tracked


def check_run_preconditions(project_root: Path) -> tuple[list[str], list[str]]:
    """Return (blocker_messages, warning_messages) for tracked catastrophic paths.

    Blockers must fail the run fast; warnings surface but allow it to proceed.
    Each message names the exact `git rm --cached` command to resolve it.
    """
    tracked = _tracked_files(project_root)
    if tracked is None:
        return [], []

    blockers: list[str] = []
    for display, is_dir in _RUN_BLOCKER_PATHS:
        if _path_is_tracked(tracked, display, is_dir):
            blockers.append(
                f"{display} is tracked in git. "
                f"Run `{_rm_cached_command(display, is_dir)}` and commit, then retry."
            )

    warnings: list[str] = []
    for display, is_dir in _RUN_WARNING_PATHS:
        if _path_is_tracked(tracked, display, is_dir):
            warnings.append(
                f"{display} is tracked in git. "
                f"Run `{_rm_cached_command(display, is_dir)}` and commit to stop tracking it."
            )

    return blockers, warnings


def _find_config(start: Path | None = None) -> Path | None:
    """Walk up from start (or cwd) looking for forge.yaml."""
    current = start or Path.cwd()
    for parent in [current, *current.parents]:
        candidate = parent / "forge.yaml"
        if candidate.exists():
            return candidate
    return None


def _print_startup_auth_warnings(config: ForgeConfig) -> None:
    """Print a stderr warning for every configured profile missing credentials.

    Runs ``check_agent_auth`` on each profile after ``load_config`` succeeds so
    the operator learns about a missing API key or CLI binary before the state
    machine spends money. These are warnings only — they never block the run.
    Sandbox readiness is intentionally excluded; this surface is about config
    credentials, not the host environment.

    Computing the warnings is itself best-effort: a profile that
    ``check_agent_auth`` cannot classify is skipped rather than aborting the
    run, since a genuinely malformed profile is already rejected by
    ``load_config`` before this point. "Warnings don't block" applies to the
    computation as much as the result.
    """
    try:
        profiles = list(iter_config_profiles(config))
    except Exception:
        return

    seen: set[tuple[str, str]] = set()
    for label, profile in profiles:
        try:
            ready, reason = check_agent_auth(
                profile, config.secrets, include_sandbox_readiness=False
            )
        except Exception:
            continue
        if ready:
            continue
        dedup_key = (profile.name, reason)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        print(
            f"⚠ config: {label} profile {profile.name!r} — {reason}",
            file=sys.stderr,
        )


def _print_unpublished_story_run_artifact_warnings(project_root: Path) -> None:
    """Warn when preserved single-story audit artifacts still exist off-tree."""
    preserved_root = project_root / _UNPUBLISHED_STORY_RUN_ARTIFACTS_DIR
    if not preserved_root.exists():
        return

    preserved_runs: list[str] = []
    try:
        children = sorted(preserved_root.iterdir())
    except OSError:
        return

    for child in children:
        if not child.is_dir():
            continue
        if any(path.is_file() for path in child.rglob("*")):
            preserved_runs.append(child.name)

    if not preserved_runs:
        return

    print(
        "⚠ preserved unpublished story-run artifacts remain under "
        f"{preserved_root}: {', '.join(preserved_runs)}. These records were kept "
        "after an earlier canonical publish failure and are not picked up from "
        "the canonical audit tree.",
        file=sys.stderr,
    )


def print_config_load_error(
    config_path: Path,
    exc: ValueError,
    *,
    prefix: str = "✗ forge.yaml is invalid",
) -> None:
    """Print a structural config error plus runtime/checkout provenance, if any."""
    print(f"{prefix}: {exc}", file=sys.stderr)
    try:
        from theforge.cli.substrate import format_config_validation_provenance_lines

        for line in format_config_validation_provenance_lines(config_path=config_path):
            print(line, file=sys.stderr)
    except Exception:
        pass


def load_config_checked(
    config_path: Path,
    *,
    loader: Callable[[Path], ForgeConfig] | None = None,
    emit_startup_auth_warnings: bool = True,
    emit_startup_artifact_warnings: bool = True,
) -> ForgeConfig:
    """Load config for a run/sprint entrypoint, enforcing startup contracts.

    - A structural config error — a ``ValueError`` raised while ``load_config``
      parses/validates ``forge.yaml`` — exits with code 2 instead of bubbling
      up as a generic exit-1 crash.
    - After a successful load, every configured profile is auth-checked and any
      missing-credential / missing-binary warnings print to stderr before the
      caller enters the coordinator state machine. Warnings never block.

    ``loader`` lets callers pass their own module-level ``load_config`` reference
    so it stays patchable at the call-site module (e.g. tests that mock
    ``theforge.cli.run.load_config``). Defaults to this module's ``load_config``.
    """
    load = loader or load_config
    try:
        config = load(config_path)
    except ValueError as exc:
        print_config_load_error(config_path, exc)
        raise SystemExit(2) from exc
    if emit_startup_auth_warnings:
        _print_startup_auth_warnings(config)
    if emit_startup_artifact_warnings:
        project_root = getattr(config, "project_root", None)
        if isinstance(project_root, Path):
            _print_unpublished_story_run_artifact_warnings(project_root)
    return config


def _parse_story_frontmatter(story_path: Path) -> dict:
    """Backward-compatible wrapper around the shared story frontmatter parser."""
    return parse_story_frontmatter(story_path)


def _build_task(story_path: Path, slug: str | None = None) -> TaskStory:
    """Build a TaskStory from a story file, using frontmatter if available."""
    fm = _parse_story_frontmatter(story_path)

    # Slug: CLI arg > frontmatter > filename stem
    resolved_slug = slug or fm.get("slug") or story_path.stem

    raw_issue = fm.get("github_issue")
    try:
        github_issue = int(raw_issue) if raw_issue is not None else None
    except (ValueError, TypeError):
        github_issue = None
    return TaskStory(
        name=fm.get("name", story_path.stem.replace("_", " ").replace("-", " ").title()),
        story_path=story_path.resolve(),
        slug=resolved_slug,
        test_target=fm.get("test_target"),
        gate_override=fm.get("gate"),
        github_issue=github_issue,
        allow_mutate_forge_yaml=frontmatter_allows_forge_yaml_mutation(fm),
    )


def _write_audit(
    result: CoordinatorResult,
    config: ForgeConfig,
    task: TaskStory,
    *,
    auto_merge: bool = False,
) -> Path:
    """Write the canonical audit log and preserve minimal worktree state on ESCALATE."""
    runtime_config = getattr(result, "runtime_config", None)
    audit_config = runtime_config if isinstance(runtime_config, ForgeConfig) else config
    audit = generate_audit_log(audit_config, task, result)
    audits_dir = audit_config.project_root / ".forge" / "audits"
    audits_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audits_dir / "forge_audit.yaml"

    def _write_yaml_copy(path: Path) -> None:
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(audit, f, default_flow_style=False, sort_keys=False)

    _write_yaml_copy(audit_path)
    final_phase = result.phase.name
    if (
        final_phase == "ESCALATE"
        and result.state.workspace_path
        and result.state.workspace_path.exists()
    ):
        worktree_audit_path = result.state.workspace_path / AUDIT_PATH
        ensure_parent_dir(worktree_audit_path)
        _write_yaml_copy(worktree_audit_path)
        marker_path = result.state.workspace_path / ESCALATED_MARKER_PATH
        ensure_parent_dir(marker_path)
        timestamp = audit.get("ended_at") or audit.get("started_at") or ""
        marker_path.write_text(
            f"slug: {task.slug}\nfinal_phase: {final_phase}\ntimestamp: {timestamp}\n",
            encoding="utf-8",
        )
    # Post-DONE knowledge summary (#1859). Runs after the authoritative record
    # exists and never raises — the audit write path above is what this run's
    # outcome depends on, not this.
    maybe_generate_run_summary(audit_config, result, audit)
    _write_yaml_copy(audit_path)
    if (
        final_phase == "ESCALATE"
        and result.state.workspace_path
        and result.state.workspace_path.exists()
    ):
        worktree_audit_path = result.state.workspace_path / AUDIT_PATH
        ensure_parent_dir(worktree_audit_path)
        _write_yaml_copy(worktree_audit_path)
    # Copy to durable per-story log dir (survives worktree cleanup)
    if result.state.log_dir is not None:
        try:
            log_audit_path = result.state.log_dir / "audit.yaml"
            log_audit_path.parent.mkdir(parents=True, exist_ok=True)
            _write_yaml_copy(log_audit_path)
        except Exception:
            pass  # best-effort
    # Write per-run JSON record (Phase A dual-write).
    _write_per_run_record(result, audit_config, audit, audits_dir)
    unpublished_artifact_copy = _preserve_unpublished_story_run_artifacts_on_failure(
        audit_config, result, audit
    )
    try:
        publish_story_run_artifacts_for_config(
            audit_config,
            lands_locally=_base_branch_lands_locally(audit_config, auto_merge=auto_merge),
        )
    except StoryRunAuditPublishError as exc:
        preserved_path = unpublished_artifact_copy(exc)
        state_suffix = (
            f" [state={exc.state}; recorded in {_STORY_RUN_AUDIT_PUBLISH_STATE_PATH}]"
            if exc.state
            else ""
        )
        preserved_suffix = (
            f"; unpublished artifacts preserved at {preserved_path}"
            if preserved_path is not None
            else ""
        )
        _log(
            f"warning: canonical story run audit publish failed: {exc}"
            f"{state_suffix}{preserved_suffix}"
        )
    return audit_path


def _preserve_unpublished_story_run_artifacts_on_failure(
    config: ForgeConfig,
    result: CoordinatorResult,
    audit: dict,
) -> Callable[[StoryRunAuditPublishError], Path | None]:
    """Return a failure handler that preserves pending run artifacts off-tree.

    Single-story runs write tracked artifacts directly into the project-root
    checkout. If publish fails *before* they are committed, those files would
    stand as uncommitted dirt and the next merge-path invocation would refuse at
    the landing precondition. Preserve this run's pending artifacts under an
    ignored .forge/ path, then remove them from the tracked tree so the next
    run sees the same clean checkout the publish step would have produced.
    """

    artifact_paths = _story_run_artifact_paths_for_run(config, result, audit)

    def _preserve(exc: StoryRunAuditPublishError) -> Path | None:
        if exc.state not in {
            AUDIT_PUBLISH_BRANCH_MISMATCH,
            AUDIT_PUBLISH_COMMIT_FAILED,
            AUDIT_PUBLISH_VERIFY_FAILED,
        }:
            return None
        return _move_dirty_story_run_artifacts_off_tree(
            config.project_root, artifact_paths, run_id=result.state.run_id
        )

    return _preserve


def _story_run_artifact_paths_for_run(
    config: ForgeConfig,
    result: CoordinatorResult,
    audit: dict,
) -> list[Path]:
    """Paths this single-story run may have written into tracked artifact trees."""
    run_id = result.state.run_id
    if not run_id:
        return []

    paths = [config.project_root / ".forge" / "audits" / "runs" / f"{run_id}.json"]
    summary_info = audit.get("knowledge_summary")
    if isinstance(summary_info, dict) and summary_info.get("written") is True:
        paths.append(summary_path(config.project_root, run_id))
    return paths


def _move_dirty_story_run_artifacts_off_tree(
    project_root: Path,
    artifact_paths: list[Path],
    *,
    run_id: str | None,
) -> Path | None:
    """Copy dirty per-run artifacts into ignored local storage and clear them."""
    if not artifact_paths:
        return None

    summaries_rel = Path(".forge") / "knowledge" / "summaries"
    preserved_root = (
        project_root / _UNPUBLISHED_STORY_RUN_ARTIFACTS_DIR / (run_id or "unknown-run")
    )
    preserved_any = False
    preserved_summary = False
    preserved_relpaths: dict[Path, Path] = {}

    for path in artifact_paths:
        try:
            rel = path.relative_to(project_root)
        except ValueError:
            continue
        if not _path_has_pending_git_changes(project_root, rel):
            continue
        if path.exists():
            dest = preserved_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            preserved_relpaths[rel] = dest.relative_to(project_root)
            _repoint_preserved_story_run_artifact_in_substrate(project_root, rel, dest)
            try:
                rel.relative_to(summaries_rel)
                preserved_summary = True
            except ValueError:
                pass
            preserved_any = True
        _clear_pending_git_path(project_root, rel)

    _rewrite_preserved_summary_authoritative_records(project_root, preserved_relpaths)
    if preserved_summary:
        _refresh_knowledge_index_after_preserving_summary(project_root)

    if preserved_any:
        return preserved_root
    return None


def _refresh_knowledge_index_after_preserving_summary(project_root: Path) -> None:
    """Refresh derived summary lookup data after preserving summaries off-tree."""
    try:
        rebuild_knowledge_index(project_root)
    except Exception as exc:  # noqa: BLE001 - publish failure handling stays best-effort
        _log(
            "warning: failed to refresh knowledge index after preserving "
            f"unpublished story run artifacts: {exc}"
        )


def _rewrite_preserved_summary_authoritative_records(
    project_root: Path,
    preserved_relpaths: dict[Path, Path],
) -> None:
    """Point preserved summaries at preserved run records when both were moved."""
    runs_rel = Path(".forge") / "audits" / "runs"
    summaries_rel = Path(".forge") / "knowledge" / "summaries"

    for source_relpath, preserved_relpath in preserved_relpaths.items():
        try:
            source_relpath.relative_to(summaries_rel)
        except ValueError:
            continue

        run_id = source_relpath.stem
        preserved_run_relpath = preserved_relpaths.get(runs_rel / f"{run_id}.json")
        if preserved_run_relpath is None:
            continue

        preserved_summary_path = project_root / preserved_relpath
        try:
            payload = yaml.safe_load(preserved_summary_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(payload, dict):
            continue

        payload["authoritative_run_record"] = str(preserved_run_relpath)
        try:
            preserved_summary_path.write_text(
                yaml.safe_dump(payload, sort_keys=False, default_flow_style=False),
                encoding="utf-8",
            )
        except OSError:
            continue


def _repoint_preserved_story_run_artifact_in_substrate(
    project_root: Path,
    source_relpath: Path,
    preserved_path: Path,
) -> None:
    """Keep the substrate row readable after a run record is preserved off-tree."""
    try:
        runs_rel = Path(".forge") / "audits" / "runs"
        source_relpath.relative_to(runs_rel)
    except ValueError:
        return

    try:
        from theforge.coordinator import audit_substrate

        with open(preserved_path, encoding="utf-8") as fh:
            record = json.load(fh)
        if not isinstance(record, dict) or not record.get("run_id"):
            return
        stat = preserved_path.stat()
        conn = audit_substrate.create_or_open(project_root)
        try:
            audit_substrate.upsert_run_record(
                conn,
                record,
                provenance="native",
                source_path=str(preserved_path.relative_to(project_root)),
                source_mtime=stat.st_mtime,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        return


def _path_has_pending_git_changes(project_root: Path, relpath: Path) -> bool:
    """Whether git currently sees relpath as dirty or untracked."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--", str(relpath)],
            cwd=str(project_root),
            capture_output=True,
            text=True,
        )
    except (OSError, FileNotFoundError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def _clear_pending_git_path(project_root: Path, relpath: Path) -> None:
    """Best-effort cleanup of one generated artifact from the tracked tree."""
    try:
        subprocess.run(
            ["git", "reset", "-q", "HEAD", "--", str(relpath)],
            cwd=str(project_root),
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "restore", "--staged", "--worktree", "--source=HEAD", "--", str(relpath)],
            cwd=str(project_root),
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "clean", "-fd", "--", str(relpath)],
            cwd=str(project_root),
            capture_output=True,
            text=True,
        )
    except (OSError, FileNotFoundError):
        pass


def _write_per_run_record(
    result: CoordinatorResult,
    config: ForgeConfig,
    audit: dict,
    audits_dir: Path,
) -> None:
    """Write a per-run JSON record to .forge/audits/runs/{run_id}.json.

    The record is written exactly once at run termination, carries schema_version,
    run_id, and parent_run_id (null for Phase A — resume lineage is not yet tracked),
    and is scrubbed by a best-effort redaction pass before hitting disk.

    Missing run_id (e.g. very old coordinator path) silently skips the write so
    existing behaviour is unchanged.
    """
    run_id = result.state.run_id
    if not run_id:
        return

    try:
        runs_dir = audits_dir / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        run_file = runs_dir / f"{run_id}.json"
        # Don't overwrite an already-written record (immutability contract).
        if run_file.exists():
            return

        record: dict = {
            "schema_version": CURRENT_RECORD_SCHEMA_VERSION,
            "run_id": run_id,
            "parent_run_id": None,
            "forge_version": audit.get("forge_version"),
        }
        record.update(audit)
        # Ensure the envelope fields stay at the top (dict insertion order is preserved).
        # Re-insert them so they shadow any same-named keys from audit.
        record["schema_version"] = CURRENT_RECORD_SCHEMA_VERSION
        record["run_id"] = run_id
        record["parent_run_id"] = None
        record["forge_version"] = audit.get("forge_version")

        env_file = config.project_root / _SECRETS_FILE
        redacted = redact(record, env_file if env_file.exists() else None)

        with open(run_file, "w", encoding="utf-8") as f:
            json.dump(redacted, f, default=str, indent=2)
    except Exception:
        pass  # best-effort — never block a run on audit write failure
        return

    # Mirror the per-run record into the SQLite audit substrate. The
    # per-run JSON is canonical; substrate write failure is a logged
    # warning, not a hard fail — `forge audits rebuild` recovers.
    try:
        from theforge.coordinator import audit_substrate

        conn = audit_substrate.create_or_open(config.project_root)
        try:
            stat = run_file.stat()
            audit_substrate.upsert_run_record(
                conn,
                redacted,
                provenance="native",
                source_path=str(run_file.relative_to(config.project_root)),
                source_mtime=stat.st_mtime,
                env_file=env_file if env_file.exists() else None,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        import sys as _sys

        print(f"[forge] warning: failed to update audit substrate: {exc}", file=_sys.stderr)


def _cmd_dry_run(config: ForgeConfig, task: TaskStory, story_path: Path) -> int:
    """Print what would happen without invoking any agents."""
    story_content = load_story(story_path)
    workspace_path = config.project_root / config.workspace.path_pattern.format(slug=task.slug)
    branch_name = config.workspace.branch_pattern.format(slug=task.slug)

    # The dry run shows the same selection the real run would make: the
    # merge-authority profile for the gate, the advisory one for the dev loop
    # (#2358). Reading the raw gate_command field here would print a command a
    # profiles-declaring project never runs.
    gate_selection = select_validation(config.validation, phase=PHASE_MERGE, task=task)
    advisory_selection = select_validation(config.validation, phase=PHASE_ADVISORY, task=task)
    dev_prompt = build_dev_prompt(
        task,
        workspace_path=workspace_path,
        branch_name=branch_name,
        allowed_tools=config.dev_profile.allowed_tools,
        story_content=story_content,
        gate_command=gate_selection.command,
        test_command=advisory_selection.command,
        conventions=config.conventions_soft,
        p2_policy=config.dev.p2_policy,
        **(
            {
                "test_profile": advisory_selection.profile,
                "test_authority": advisory_selection.authority,
                "gate_profile": gate_selection.profile,
            }
            if config.validation.profiles
            else {}
        ),
    )
    review_prompt = build_review_prompt(
        task,
        story_content=story_content,
        commit_log="(dry run — no commits available)",
        workspace_path=str(workspace_path),
        branch=branch_name,
        handoff_content="(dry run — no handoff available)",
        conventions=config.conventions_soft,
        p2_policy=config.dev.p2_policy,
        **hard_convention_review_kwargs(config),
    )

    sep = "=" * 60
    print(f"{sep}")
    print("DRY RUN — no agents will be invoked")
    print(f"{sep}\n")

    workspace_command = config.workspace.create_command.format(
        slug=task.slug,
        base_branch=config.workspace.base_branch,
    )
    print(f"Workspace command: {workspace_command}")
    print(f"Workspace path:    {workspace_path}")
    print(f"Branch:            {branch_name}")
    print(f"Gate command:      {gate_selection.command}")
    if config.validation.profiles:
        print(f"Validation:        {gate_selection.describe()}")
    print()

    print(f"{sep}")
    print(f"DEV PROMPT ({len(dev_prompt)} chars)")
    print(f"  CLI:     {config.dev_profile.cli}")
    print(f"  Model:   {config.dev_profile.model}")
    print(f"  Budget:  ${config.dev_profile.budget_usd:.2f}")
    print(f"  Timeout: {config.dev_profile.timeout_seconds}s")
    print(f"  Tools:   {', '.join(config.dev_profile.allowed_tools)}")
    print(f"{sep}")
    print(dev_prompt)

    print(f"\n{sep}")
    print(f"REVIEW PROMPT ({len(review_prompt)} chars)")
    print(f"  CLI:     {config.review_profile.cli}")
    print(f"  Model:   {config.review_profile.model}")
    print(f"  Budget:  ${config.review_profile.budget_usd:.2f}")
    print(f"  Timeout: {config.review_profile.timeout_seconds}s")
    print(f"  Tools:   {', '.join(config.review_profile.allowed_tools)}")
    print(f"{sep}")
    print(review_prompt)

    return 0


def _apply_dev_model_override(config: "ForgeConfig", spec: str) -> "ForgeConfig":
    """Override the dev profile with a --dev-model spec.

    Format: provider/model@base_url
    Examples:
        ollama/qwen2.5-coder:14b@http://localhost:11434/v1
        openai/qwen2.5-coder:7b@http://localhost:11434/v1
        anthropic/claude-opus-4-6

    The "ollama" provider alias is normalised to "openai" because Ollama exposes
    an OpenAI-compatible API.  Pass the Ollama base URL via the @url suffix.
    """
    from dataclasses import replace

    base_url = None
    if "@" in spec:
        spec, base_url = spec.rsplit("@", 1)

    if "/" in spec:
        provider, model = spec.split("/", 1)
    else:
        provider = "openai"
        model = spec

    # Ollama exposes an OpenAI-compatible API — normalise so it routes through
    # the existing OpenAI runner (which already passes base_url to the client).
    if provider == "ollama":
        provider = "openai"

    new_dev = replace(
        config.dev_profile,
        cli=None,
        provider=provider,
        model=model,
        base_url=base_url,
        budget_usd=config.dev_profile.budget_usd,
        # Clear transport so ModelProfile.__post_init__ re-infers it from the
        # new cli/provider pair. Without this, the prior transport (e.g.
        # claude CLI) would persist and dispatch would still go CLI.
        transport=None,
    )
    updated = replace(config, dev_profile=new_dev)
    return refresh_provenance(
        updated,
        source_updates={
            "dev_profile.cli": VALUE_SOURCE_CLI_OVERRIDE,
            "dev_profile.provider": VALUE_SOURCE_CLI_OVERRIDE,
            "dev_profile.model": VALUE_SOURCE_CLI_OVERRIDE,
            "dev_profile.base_url": VALUE_SOURCE_CLI_OVERRIDE,
            "dev_profile.transport.kind": VALUE_SOURCE_CLI_OVERRIDE,
            "dev_profile.transport.runner": VALUE_SOURCE_CLI_OVERRIDE,
            "dev_profile.transport.executable": VALUE_SOURCE_CLI_OVERRIDE,
        },
    )


def _apply_plan_model_override(config: "ForgeConfig", spec: str) -> "ForgeConfig":
    """Override the plan profile with a --plan-model spec.

    Format: provider/model  (sets API transport, clears CLI)
            bare-model-name (updates model identifier only, preserves transport)
    Examples:
        opus
        anthropic/claude-opus-4-6
    """
    from dataclasses import replace

    if "/" in spec:
        provider, model = spec.split("/", 1)
        new_plan = replace(
            config.plan,
            ref=replace(config.plan.ref, provider=provider, model=model, cli=None),
        )
        if new_plan.enabled:
            _validate_plan_provider(new_plan, config.secrets)
    else:
        new_plan = replace(config.plan, ref=replace(config.plan.ref, model=spec))

    updated = replace(config, plan=new_plan, plan_model_is_default=False)
    return refresh_provenance(
        updated,
        source_updates={
            "plan.ref.cli": VALUE_SOURCE_CLI_OVERRIDE,
            "plan.ref.provider": VALUE_SOURCE_CLI_OVERRIDE,
            "plan.ref.model": VALUE_SOURCE_CLI_OVERRIDE,
            "plan.ref.transport.kind": VALUE_SOURCE_CLI_OVERRIDE,
            "plan.ref.transport.runner": VALUE_SOURCE_CLI_OVERRIDE,
            "plan.ref.transport.executable": VALUE_SOURCE_CLI_OVERRIDE,
            "plan_model_is_default": VALUE_SOURCE_CLI_OVERRIDE,
        },
    )
