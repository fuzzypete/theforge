"""forge sprint subcommand — run multiple stories from a sprint manifest or GitHub query."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

from theforge.cli.overrides import apply_base_branch_override
from theforge.cli.shared import _find_config, load_config_checked
from theforge.config import load_config
from theforge.config.provenance import (
    VALUE_SOURCE_CLI_OVERRIDE,
    VALUE_SOURCE_DERIVED,
    refresh_provenance,
)
from theforge.coordinator.util import set_log_level as coordinator_set_log_level
from theforge.runners import LogLevel
from theforge.runners import set_log_level as runner_set_log_level
from theforge.sprint import SprintRunContext, run_sprint
from theforge.sprint.budget import evaluate_budget
from theforge.sprint.carry import load_sprint_carry_budget_snapshot
from theforge.sprint.launch_guard import acquire_launch_story_locks
from theforge.sprint.live_stories import LivenessResolution
from theforge.sprint.lock import release_story_locks
from theforge.sprint.preflight import reacquire_story_locks_in_daemon
from theforge.sprint.runner import parse_manifest_story_refs

# A run's reported disposition must be derived from how it actually ended, not
# from the absence of a record saying otherwise. ``_BACKSTOP`` carries the
# outcome for the atexit fallback registered on the detached path, covering
# failures that escape before/around the per-mode try/finally blocks below.
_BACKSTOP: dict[str, str | None] = {"outcome": "completed", "cause": None}

# Cause recorded when a sprint process reaches its terminal marker without
# either completing run_sprint or catching an exception (SIGINT, SystemExit,
# BaseException escaping the runner).
_UNKNOWN_END_CAUSE = "sprint process ended before recording a completion"

# A launch that is not a re-exec has no inherited work by construction: nothing
# is live and nothing is unresolved. Distinct from a failed lookup, which yields
# an *unresolved* result (see ``_resolve_story_liveness``).
_NO_LIVENESS = LivenessResolution()


def parse_manifest_slugs(config: object, manifest_path: Path) -> list[str]:
    """CLI seam for pre-launch slug parsing in manifest mode."""
    slugs, _canonical_refs_by_slug = parse_manifest_story_refs(config, manifest_path)
    return slugs


def _exc_cause(exc: BaseException) -> str:
    """Return a short single-line description of a terminating exception."""
    from theforge import detach as _detach_mod

    return _detach_mod.format_exception_cause(exc)


def _record_run_failure(cause: str) -> None:
    """Mark the active sprint process as terminating abnormally."""
    _BACKSTOP["outcome"] = "failed"
    _BACKSTOP["cause"] = cause


def _backstop_run_ended(run_id: str, project_root: Path) -> None:
    """atexit fallback: write whatever terminal outcome this process observed.

    A no-op when the per-mode ``finally`` already wrote the marker
    (``write_run_ended`` does not overwrite an existing file).
    """
    from theforge import detach as _detach_mod

    _detach_mod.write_run_ended(
        run_id,
        project_root,
        _BACKSTOP["outcome"] or "failed",
        cause=_BACKSTOP["cause"],
    )


def _derive_query_sprint_name(
    *,
    name: str | None,
    milestone: str | None,
    label: str | None,
    issues_arg: str | None,
) -> str:
    """Return the canonical query-mode sprint name used across launch and runtime."""
    return name or milestone or label or f"issues-{issues_arg}"


def cmd_sprint(args: object) -> int:
    """Run multiple stories via a sprint manifest or GitHub query.

    Thin wrapper around :func:`_cmd_sprint` that records an abnormal
    termination before the exception leaves the command, so the atexit backstop
    cannot label a crashed sprint as completed.
    """
    try:
        return _cmd_sprint(args)
    except KeyboardInterrupt:
        _BACKSTOP.update({"outcome": "stopped", "cause": "interrupted by operator (SIGINT)"})
        raise
    except BaseException as exc:
        _record_run_failure(_exc_cause(exc))
        raise


def _cmd_sprint(args: object) -> int:
    """Run multiple stories via a sprint manifest or GitHub query."""
    from theforge import daemon as _daemon
    from theforge import detach as _detach
    from theforge.coordinator.util import _generate_run_id

    milestone: str | None = getattr(args, "milestone", None)
    label: str | None = getattr(args, "label", None)
    issues_arg: str | None = getattr(args, "issues", None)
    query_mode = bool(milestone or label or issues_arg)
    manifest_arg: str | None = getattr(args, "manifest", None)

    # ── Validate argument combinations ──────────────────────────────────
    if not query_mode and not manifest_arg:
        print(
            "forge sprint: provide a manifest path or use --milestone/--label/--issues",
            file=sys.stderr,
        )
        return 1

    if query_mode and manifest_arg:
        print(
            "forge sprint: --milestone/--label/--issues and a manifest path "
            "are mutually exclusive",
            file=sys.stderr,
        )
        return 1

    selected_queries = [
        flag
        for flag, value in (
            ("--milestone", milestone),
            ("--label", label),
            ("--issues", issues_arg),
        )
        if value
    ]
    if len(selected_queries) > 1:
        print(
            f"forge sprint: {', '.join(selected_queries)} are mutually exclusive",
            file=sys.stderr,
        )
        return 1

    budget_str: str | None = getattr(args, "budget", None)
    if query_mode and budget_str is None:
        print(
            "forge sprint: --budget <usd> is required when using "
            "--milestone, --label, or --issues",
            file=sys.stderr,
        )
        return 1

    # ── Find and load config ─────────────────────────────────────────────
    config_path: Path | None = None
    if getattr(args, "config", None):
        config_path = Path(args.config).resolve()
    elif query_mode:
        config_path = _find_config(Path.cwd())
    else:
        manifest_path = Path(manifest_arg).resolve()
        config_path = _find_config(manifest_path.parent)

    if config_path is None or not config_path.exists():
        print(
            "forge.yaml not found. Run 'forge init' or pass --config path/to/forge.yaml",
            file=sys.stderr,
        )
        return 1

    config = apply_base_branch_override(
        load_config_checked(config_path, loader=load_config),
        getattr(args, "base_branch", None),
    )

    # ── Detach BEFORE any subprocess/gh work ─────────────────────────────
    # macOS aborts the child of a fork() when Foundation has been initialized
    # in the parent (e.g., by `gh` subprocess calls during shape-gate /
    # intake remediation). The new daemonize_run is a clean Popen re-exec
    # rather than a fork, but we still hoist it above the subprocess work so
    # neither the parent nor the child lands in a tainted-then-fork state.
    # The detached child re-executes the full CLI; this branch detects that
    # re-entry and skips daemonize while still installing the PID/cleanup.
    import os as _os

    fg = bool(getattr(args, "fg", False))
    submit_to_daemon = bool(getattr(args, "detach", False))
    dry_run = bool(getattr(args, "dry_run", False))
    detach_to_background = not fg and not submit_to_daemon and not dry_run

    # Capture the re-exec signal BEFORE the detach section runs. On the real
    # coordinator re-exec path (workspace.pull_base_branch sets FORGE_PREV_RUN_ID
    # then os.execv with the same argv), the re-exec'd process inherits
    # FORGE_DETACHED=1, so it enters the is_detached_child() branch below and
    # calls setup_detached_child(), which pops FORGE_PREV_RUN_ID (detach.py) to
    # write the log-redirect sidecar. If we read _is_reexec() after that pop it
    # always returns False, silently disabling the merged-story reconciliation in
    # run_sprint. Snapshot it here so both manifest and query modes see the true
    # signal regardless of the detach handoff.
    reexec = _is_reexec()

    if detach_to_background:
        from theforge import detach as _detach_mod

        # Compute slug from args alone — no subprocess work in the parent.
        manifest_arg_local: str | None = getattr(args, "manifest", None)
        milestone_local: str | None = getattr(args, "milestone", None)
        label_local: str | None = getattr(args, "label", None)
        issues_arg_local: str | None = getattr(args, "issues", None)
        query_mode_local = bool(milestone_local or label_local or issues_arg_local)
        if query_mode_local:
            launch_slug = _derive_query_sprint_name(
                name=getattr(args, "name", None),
                milestone=milestone_local,
                label=label_local,
                issues_arg=issues_arg_local,
            )
        else:
            launch_slug = Path(manifest_arg_local).stem if manifest_arg_local else "sprint"

        if _detach_mod.is_detached_child():
            launch_run_id = _os.environ.get("FORGE_DETACHED_RUN_ID") or _generate_run_id()
            _detach_mod.setup_detached_child(
                launch_run_id, launch_slug, config.project_root, is_sprint=True
            )
            _detach_mod.install_cleanup_handler(launch_run_id, config.project_root)
            print("[forge] Detached sprint starting", file=sys.stderr, flush=True)
            args.__dict__["_detached_run_id"] = launch_run_id
            args.__dict__["_detached_slug"] = launch_slug

            # Backstop cleanup for early-return paths (all-skipped, no
            # stories, --detach-not-supported guard) that bypass the
            # try/finally in run_sprint blocks. Idempotent — write_run_ended
            # is a no-op if the .ended marker already exists. Writes the
            # outcome this process actually observed, so an exception escaping
            # before the per-mode try/finally is not recorded as "completed".
            import atexit as _atexit

            _atexit.register(
                _backstop_run_ended,
                launch_run_id,
                config.project_root,
            )
            _atexit.register(_detach_mod.remove_pid, launch_run_id, config.project_root)
        else:
            launch_run_id = _generate_run_id()
            _detach_mod.daemonize_run(
                launch_run_id, launch_slug, config.project_root, is_sprint=True
            )
            # daemonize_run never returns in the parent.

    if getattr(args, "verbose", False):
        coordinator_set_log_level(LogLevel.VERBOSE)
        runner_set_log_level(LogLevel.VERBOSE)

    auto_merge = getattr(args, "auto_merge", False)
    interactive = getattr(args, "interactive", False)
    resume = getattr(args, "resume", False)
    no_pull = getattr(args, "no_pull", False)
    dry_run = getattr(args, "dry_run", False)
    max_parallel: int | None = getattr(args, "parallel", None)
    force = getattr(args, "force", False)
    accept_unmeasured_spend = list(getattr(args, "accept_unmeasured_spend", None) or [])
    accept_unmeasured_reason = getattr(args, "accept_unmeasured_reason", None)

    # ── Query mode: fetch issues and build ResolvedSprint ───────────────
    if query_mode:
        return _run_query_mode(
            args=args,
            config=config,
            config_path=config_path,
            milestone=milestone,
            label=label,
            issues_arg=issues_arg,
            budget_str=budget_str,
            dry_run=dry_run,
            max_parallel=max_parallel,
            auto_merge=auto_merge,
            interactive=interactive,
            resume=resume,
            reexec=reexec,
            no_pull=no_pull,
            force=force,
            accept_unmeasured_spend=accept_unmeasured_spend,
            accept_unmeasured_reason=accept_unmeasured_reason,
            _daemon=_daemon,
            _detach=_detach,
            _generate_run_id=_generate_run_id,
        )

    # ── Manifest mode (original behaviour, unchanged) ────────────────────
    manifest_path = Path(manifest_arg).resolve()
    if not manifest_path.exists():
        print(f"Sprint manifest not found: {manifest_path}", file=sys.stderr)
        return 1

    slugs = parse_manifest_slugs(config, manifest_path)
    _parsed_slugs, canonical_refs_by_slug = parse_manifest_story_refs(config, manifest_path)
    # ``reexec`` was captured at the top of cmd_sprint, before setup_detached_child
    # popped FORGE_PREV_RUN_ID — do not recompute it here (the env signal is gone).
    # On the re-exec path, resolve the prior generation's recorded outcomes so the
    # launch guard can distinguish an already-completed worktree from a fresh
    # collision. Mirror the runner's sprint-name resolution (manifest ``name``).
    prior_outcomes: dict[str, dict] | None = None
    if reexec:
        try:
            from theforge.sprint.manifest import load_sprint_manifest  # noqa: PLC0415

            prior_outcomes = _resolve_prior_outcomes(
                config, load_sprint_manifest(manifest_path).name
            )
        except Exception:
            prior_outcomes = None
    # Stories this same process (the pid survives ``os.execv``) still has agents
    # running for — plus the ones whose liveness could not be established, which
    # are this run's own possible live work, not foreign state either.
    liveness = _resolve_story_liveness(config, slugs) if reexec else _NO_LIVENESS
    locked_fds, launch_error, dropped_slugs = _acquire_launch_locks(
        slugs=slugs,
        config=config,
        resume=resume,
        allow_drop=reexec,
        force=force,
        prior_outcomes=prior_outcomes,
        live_slugs=set(liveness.live_slugs),
        unresolved_slugs=set(liveness.unresolved_slugs),
        registered_slugs=set(liveness.registered_slugs),
        canonical_refs_by_slug=canonical_refs_by_slug,
    )
    if launch_error is not None:
        return launch_error

    live_slugs = [s for s in slugs if s not in dropped_slugs]
    # Detach already happened at top of cmd_sprint (Popen re-exec model). If
    # we are in the detached child, the run_id/slug were resolved there and
    # cached on args; otherwise (--fg / --detach daemon submit) generate now.
    cached_run_id = args.__dict__.get("_detached_run_id")
    cached_slug = args.__dict__.get("_detached_slug")
    if cached_run_id is not None:
        run_id = cached_run_id
        slug = cached_slug or manifest_path.stem
        # Locks were acquired in this same process; metadata rewrite is a
        # no-op idempotent call but we keep it for parity with prior behavior.
        locked_fds = reacquire_story_locks_in_daemon(
            live_slugs,
            config.project_root,
            locked_fds,
        )
    else:
        run_id = _generate_run_id()
        slug = manifest_path.stem

    # Publish run context for agent process-group registration. setup_detached_child
    # already exports on the detached path; this covers the --fg path (idempotent).
    _detach.export_run_context(run_id, config.project_root)
    # Reap any agent groups orphaned by an abruptly-killed prior sprint before we
    # launch new work (the guaranteed path for the SIGKILL-parent case).
    from theforge import process_group as _process_group  # noqa: PLC0415

    _process_group.reap_orphan_agents(config.project_root)

    if getattr(args, "detach", False) and _daemon.is_daemon_running(config.project_root):
        release_story_locks(locked_fds)
        sprint_args: dict = {
            "auto_merge": auto_merge,
            "notify": not args.no_notify,
            "resume": resume,
            "config": str(config_path),
            "no_pull": no_pull,
            "force": force,
            "accept_unmeasured_spend": accept_unmeasured_spend,
            "accept_unmeasured_reason": accept_unmeasured_reason,
        }
        response = _daemon.submit_sprint(config.project_root, str(manifest_path), sprint_args)
        if response.get("ok"):
            slug = response.get("queued", manifest_path.stem)
            pos = response.get("position", 1)
            print(f"[daemon] Queued '{slug}' (position {pos})")
            if not getattr(args, "detach", False):
                print("[daemon] Use 'forge status' to monitor progress.")
            return 0
        else:
            err = response.get("error", "unknown error")
            print(f"[daemon] Submit failed: {err}", file=sys.stderr)
            return 1

    # Default to a failed disposition: only a run that returns from run_sprint
    # has been observed to complete. Anything else (exception, SIGINT,
    # BaseException) must not be recorded as a completion.
    outcome = "failed"
    cause: str | None = _UNKNOWN_END_CAUSE
    try:
        result = run_sprint(
            SprintRunContext.for_sprint(
                config,
                manifest_path,
                auto_merge=auto_merge,
                interactive=interactive,
                notify=not args.no_notify,
                resume=resume,
                reexec=reexec,
                no_pull=no_pull,
                run_id=run_id,
                dropped_slugs=dropped_slugs,
                force=force,
                live_story_slugs=set(liveness.live_slugs),
                unresolved_live_slugs=set(liveness.unresolved_slugs),
                registered_live_slugs=set(liveness.registered_slugs),
                accept_unmeasured_spend=accept_unmeasured_spend,
                accept_unmeasured_reason=accept_unmeasured_reason,
            )
        )
    except KeyboardInterrupt:
        # Ctrl-C is a deliberate termination, not a crash — record it as such
        # rather than folding it into the failure bucket.
        outcome, cause = "stopped", "interrupted by operator (SIGINT)"
        raise
    except Exception as exc:
        import traceback

        cause = _exc_cause(exc)
        _record_run_failure(cause)
        print(f"Sprint error: {exc}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        return 1
    else:
        outcome, cause = "completed", None
    finally:
        release_story_locks(locked_fds)
        # Write terminal marker then remove PID — ensures status is accurate even
        # if run_sprint raises. SIGTERM handler may have already written "stopped";
        # write_run_ended is a no-op when the file already exists.
        _detach.write_run_ended(run_id, config.project_root, outcome, cause=cause)
        _detach.remove_pid(run_id, config.project_root)

    return 0 if result.specs_failed == 0 else 1


def _acquire_launch_locks(
    slugs: list[str],
    config: object,
    resume: bool,
    *,
    allow_drop: bool = False,
    force: bool = False,
    prior_outcomes: dict[str, str | dict] | None = None,
    live_slugs: set[str] | None = None,
    unresolved_slugs: set[str] | None = None,
    registered_slugs: set[str] | None = None,
    canonical_refs_by_slug: dict[str, str] | None = None,
) -> tuple[list, int | None, dict[str, str]]:
    return acquire_launch_story_locks(
        slugs=slugs,
        config=config,
        resume=resume,
        allow_drop=allow_drop,
        force=force,
        prior_outcomes=prior_outcomes,
        live_slugs=live_slugs,
        unresolved_slugs=unresolved_slugs,
        registered_slugs=registered_slugs,
        canonical_refs_by_slug=canonical_refs_by_slug,
    )


def _resolve_story_liveness(config: object, slugs: list[str]) -> LivenessResolution:
    """Stories of this same process still executing across a re-exec boundary.

    Thin CLI seam over :mod:`theforge.sprint.live_stories` — the ownership
    decision and the fail-closed semantics both live in the sprint package. A
    resolution failure yields an *unresolved* result, never an empty one: "we
    could not ask" must stay distinguishable from "nothing is running", or the
    launch guard reconciles this run's own live work as a foreign collision
    (#2079).
    """
    from theforge.sprint.live_stories import (  # noqa: PLC0415
        resolve_liveness,
        unresolved_liveness,
    )

    try:
        return resolve_liveness(
            slugs,
            project_root=config.project_root,
            path_pattern=config.workspace.path_pattern,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed, never fail the launch
        return unresolved_liveness(slugs, reason=f"liveness lookup unavailable: {exc}")


def _resolve_prior_outcomes(config: object, sprint_name: str) -> dict[str, dict]:
    """Best-effort map of slug -> prior-generation story record for the guard.

    Resolves the logical sprint id the same way the runner does (from the
    manifest ``name``) and reads the prior generation's accumulated story
    entries from ``.forge/sprints/<id>/state.yaml``. Returns an empty map on any
    failure so a lookup miss degrades to today's collision behavior — this must
    never fail the launch.

    The whole recorded entry is carried forward, not just its ``outcome``: the
    guard's reconciliation decision needs the landing evidence recorded beside
    the outcome (``landing_status`` / ``landing`` / ``merge``), because a
    coordinator ``DONE`` is persisted before the sprint's landing step runs and
    on its own says nothing about whether the story actually landed (#2189).
    The policy that reads those fields lives in
    :mod:`theforge.sprint.prior_landing`; this is only the data hand-off.
    """
    try:
        from theforge.sprint.audit import (  # noqa: PLC0415
            _get_or_create_sprint_id,
            _load_accumulated_stories,
        )
        from theforge.sprint.prior_landing import as_prior_record  # noqa: PLC0415

        sprint_id = _get_or_create_sprint_id(sprint_name, config.project_root)
        records: dict[str, dict] = {}
        for story in _load_accumulated_stories(sprint_id, config.project_root):
            if not isinstance(story, dict):
                continue
            slug = story.get("slug")
            if not slug:
                continue
            records[slug] = as_prior_record(story)
        return records
    except Exception:
        return {}


def _resolve_base_branch_sha(config: object) -> str | None:
    """Best-effort lookup of the configured base branch's HEAD SHA.

    Returned as a column on shape-gate verdict events so downstream queries
    can scope refusal-economics samples to a specific tree state. Returns
    ``None`` when git is unavailable or the branch is not resolvable —
    substrate emission must not fail because of a git lookup miss.
    """
    import subprocess as _sp

    base = getattr(getattr(config, "workspace", None), "base_branch", None)
    if not base:
        return None
    project_root = getattr(config, "project_root", None)
    try:
        proc = _sp.run(
            ["git", "rev-parse", str(base)],
            capture_output=True,
            text=True,
            cwd=str(project_root) if project_root else None,
            timeout=10,
        )
    except (OSError, _sp.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


_INTAKE_REMEDIATED_ENV = "FORGE_INTAKE_REMEDIATED"


def _consume_intake_remediated_env() -> set[int]:
    """Read and clear the carry-across-re-exec remediated-issues env var.

    Set by an earlier process before ``os.execv`` (see ``pull_base_branch``
    in coordinator/workspace.py). Cleared on consumption so it never bleeds
    into a subsequent re-exec or a child subprocess that has no business
    inheriting the sprint's intake state.
    """
    import os

    raw = os.environ.pop(_INTAKE_REMEDIATED_ENV, "")
    if not raw:
        return set()
    numbers: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            numbers.add(int(part))
        except ValueError:
            continue
    return numbers


def _publish_intake_remediated_env(numbers: "set[int] | frozenset[int]") -> None:
    """Stash the just-remediated issue numbers in the environment.

    The sprint runner may re-exec the process if ``git pull`` updates the
    src/theforge tree between intake remediation and dispatch. Env vars
    survive ``os.execv``; in-memory dicts do not. Re-entry reads the value
    via ``_consume_intake_remediated_env`` and threads it into the post-
    re-exec shape gate so the just-remediated issues aren't dropped on a
    stale ``needs-grooming`` label that the async labeler hasn't reconciled
    yet.
    """
    import os

    if not numbers:
        return
    os.environ[_INTAKE_REMEDIATED_ENV] = ",".join(str(n) for n in sorted(numbers))


def _is_reexec() -> bool:
    """Return True when the current process was started by a forge re-exec.

    ``FORGE_PREV_RUN_ID`` is set by coordinator workspace.py just before
    ``os.execv``; it is only popped inside ``daemonize_run``.  Seeing it here
    (before daemonization) is the authoritative signal that we are running a
    re-exec of a mid-sprint forge.
    """
    import os

    return bool(os.environ.get("FORGE_PREV_RUN_ID"))


def _remediate_shape_gate_skips(
    *,
    issues: list,
    skipped_issues: list,
    config: object,
) -> tuple[list, list]:
    """Attempt body-only remediation for shape-gate-skipped issues.

    For each skipped issue whose reason code is body-fixable (e.g.
    ``reopened_stale_contract``), call into the intake remediator. On
    success, move the issue back into the runnable list. On failure,
    leave it in ``skipped_issues`` so the existing audit + warning paths
    keep treating it as dropped.

    Skip reasons not body-fixable are reported as
    ``no remediation attempted`` so the operator can distinguish them from
    failed remediation attempts (per #1385's audit-trail requirement).
    """
    intake_cfg = getattr(config, "intake", None)
    auto_fix_enabled = bool(getattr(intake_cfg, "auto_fix", False))
    auto_fix_mode = str(getattr(intake_cfg, "auto_fix_mode", "comment") or "comment")

    if not auto_fix_enabled or auto_fix_mode != "edit":
        # Surface the no-attempt signal so the operator doesn't conflate
        # this with a failed remediation. Keep the skipped list intact.
        from theforge.intake import BODY_FIXABLE_SKIP_REASONS

        no_attempt_codes: set[str] = set()
        for sk in skipped_issues:
            sk_dict = sk.as_dict() if hasattr(sk, "as_dict") else dict(sk)
            for code in sk_dict.get("reason_codes") or []:
                if code in BODY_FIXABLE_SKIP_REASONS:
                    no_attempt_codes.add(code)
        if no_attempt_codes:
            print(
                "[forge] Entry shape-gate skip remediation: no remediation attempted "
                f"(intake.auto_fix={auto_fix_enabled}, mode={auto_fix_mode!r}); "
                "body-fixable skip reasons present: " + ", ".join(sorted(no_attempt_codes)),
                file=sys.stderr,
            )
        return issues, skipped_issues

    # Lazy import to avoid pulling intake on every CLI invocation.
    from theforge.intake import (
        ShapeGateSkipRemediationKind,
        remediate_shape_gate_skip,
    )

    issue_lookup: dict[int, dict] = {}
    for sk in skipped_issues:
        sk_dict = sk.as_dict() if hasattr(sk, "as_dict") else dict(sk)
        sk_num = sk_dict.get("issue_number")
        if sk_num is None:
            continue
        # Synthesize a runnable-issue entry from the skip record so it can
        # be re-added to ``issues`` on remediation success. ``issues`` is a
        # list of ``{"number": ..., "title": ...}`` dicts at this stage.
        issue_lookup[int(sk_num)] = {
            "number": int(sk_num),
            "title": sk_dict.get("title", "") or "",
        }

    new_runnable = list(issues)
    new_skipped: list = []
    for sk in skipped_issues:
        sk_dict = sk.as_dict() if hasattr(sk, "as_dict") else dict(sk)
        sk_num = sk_dict.get("issue_number")
        if sk_num is None:
            new_skipped.append(sk)
            continue
        reason_codes = tuple(sk_dict.get("reason_codes") or ())
        outcome = remediate_shape_gate_skip(
            issue_number=int(sk_num),
            reason_codes=reason_codes,
            project_root=config.project_root,
            auto_fix_enabled=auto_fix_enabled,
            auto_fix_mode=auto_fix_mode,
        )
        if outcome.kind is ShapeGateSkipRemediationKind.REMEDIATED:
            print(
                f"[forge] Entry shape-gate skip remediation: issue #{sk_num} -> "
                f"remediated ({outcome.detail})",
                file=sys.stderr,
            )
            new_runnable.append(issue_lookup[int(sk_num)])
            continue
        if outcome.kind is ShapeGateSkipRemediationKind.DROPPED_NOT_COVERED:
            print(
                f"[forge] Entry shape-gate skip remediation: issue #{sk_num} -> "
                f"no remediation attempted ({outcome.detail})",
                file=sys.stderr,
            )
        else:
            print(
                f"[forge] Entry shape-gate skip remediation: issue #{sk_num} -> "
                f"{outcome.kind.value} ({outcome.detail})",
                file=sys.stderr,
            )
        new_skipped.append(sk)
    return new_runnable, new_skipped


def _emit_shape_skip_events(
    *,
    config: object,
    run_id: str | None,
    sprint_name: str | None,
    milestone: str | None,
    original_skips: list,
    original_advisories: list,
    intake_outcomes: "dict | None" = None,
) -> None:
    """Record shape-gate skip classification events into the audit substrate.

    Derives the per-issue remediation outcome from ``intake_outcomes`` (an issue
    the entry-intake gate REMEDIATED vs. one it declined) and hands the gate's
    original skip/advisory partition to :func:`emit_shape_skip_events`.
    Best-effort — the helper swallows substrate failures internally.
    """
    from theforge.intake import IntakeOutcomeKind
    from theforge.sprint.skip_report import emit_shape_skip_events

    intake_outcomes = intake_outcomes or {}
    remediated: set[int] = set()
    declined: set[int] = set()
    for num, outcome in intake_outcomes.items():
        kind = getattr(outcome, "kind", None)
        if kind is IntakeOutcomeKind.REMEDIATED:
            remediated.add(int(num))
        elif kind is IntakeOutcomeKind.DROPPED_SHAPE:
            audit = getattr(outcome, "audit", None) or {}
            if isinstance(audit, dict) and audit.get("remediation_source") == "declined":
                declined.add(int(num))
    try:
        emit_shape_skip_events(
            config.project_root,
            run_id=run_id,
            sprint_name=sprint_name,
            milestone=milestone,
            skipped=original_skips,
            advisories=original_advisories,
            remediated_numbers=remediated,
            declined_numbers=declined,
        )
    except Exception as exc:  # noqa: BLE001 — observability, never gating
        print(f"[forge] Warning: shape-skip emission failed: {exc}", file=sys.stderr)


def _emit_all_skipped_audit(
    *,
    config: object,
    sprint_name: str,
    budget_usd: float,
    skipped_issues: list,
    intake_outcomes: "dict | None" = None,
    run_id: str | None = None,
) -> None:
    """Write sprint-audit.yaml and sprint-summary.yaml when every issue was
    gated out. Without this, an all-skipped sprint leaves no machine-readable
    record of which issues were rejected or why.
    """
    import datetime

    from theforge.sprint.audit import _write_sprint_audit, _write_sprint_summary
    from theforge.sprint.manifest import ResolvedSprint, SprintResult
    from theforge.sprint.shape_gate import skipped_issue_state_fields
    from theforge.sprint.story_state import SprintStoryState, StoryOutcome

    # Build a canonical SprintStoryState containing every shape-gate-skipped
    # issue so the all-skipped audit/summary projects from the same SoT
    # structure run_sprint() uses. Counts and the per-story list both flow
    # from this single source.
    intake_outcomes = intake_outcomes or {}
    # Entry-level intake remediation may have spent agent budget before the
    # all-skipped fork. Roll those costs into the sprint total so the
    # operator-visible accounting matches the actual spend even when no
    # issue made it past the shape gate.
    intake_remediation_cost = 0.0
    for outcome in intake_outcomes.values():
        agent = outcome.audit.get("agent") if isinstance(outcome.audit, dict) else None
        if not isinstance(agent, dict):
            continue
        raw = agent.get("cost_usd")
        if raw is None:
            continue
        try:
            intake_remediation_cost += float(raw)
        except (TypeError, ValueError):
            continue
    story_state = SprintStoryState()
    for sk in skipped_issues or []:
        sk_dict = sk.as_dict() if hasattr(sk, "as_dict") else dict(sk)
        sk_num = sk_dict.get("issue_number")
        if sk_num is None:
            continue
        sk_slug = f"issue-{sk_num}"
        # Verdict-preferred reason/detail come from the shared helper so
        # every surface renders the typed verdict identifier consistently;
        # operator-action classification and intake-outcome enrichment are
        # layered on top.
        sk_reason, sk_detail = skipped_issue_state_fields(sk)
        sk_codes = sk_dict.get("reason_codes") or []
        is_operator_action = "operator_action" in sk_codes
        sk_outcome = StoryOutcome.OPERATOR_ACTION if is_operator_action else StoryOutcome.SKIPPED
        sk_detail["final_outcome"] = sk_outcome.name
        if is_operator_action:
            sk_reason = "operator-action — operator deliverable"
            sk_detail["operator_action"] = True
        outcome = intake_outcomes.get(sk_num)
        sk_cost = 0.0
        if outcome is not None:
            sk_detail["intake_kind"] = outcome.kind.value
            sk_detail["intake_detail"] = outcome.detail
            sk_detail["intake_findings"] = [f.as_dict() for f in outcome.findings]
            sk_detail["intake_audit"] = dict(outcome.audit)
            sk_detail["intake_proposed_replacement"] = outcome.proposed_replacement
            agent = outcome.audit.get("agent") if isinstance(outcome.audit, dict) else None
            raw = agent.get("cost_usd") if isinstance(agent, dict) else None
            if raw is not None:
                try:
                    sk_cost = float(raw)
                except (TypeError, ValueError):
                    sk_cost = 0.0
        story_state.register(
            sk_slug,
            f"Issue #{sk_num}",
            outcome=sk_outcome,
            reason=sk_reason,
            canonical_ref=f"issue:{sk_num}",
            detail=sk_detail,
            cost_usd=sk_cost,
        )
    canonical_counts = story_state.counts()
    manifest = ResolvedSprint(
        name=sprint_name,
        budget_usd=budget_usd,
        stories=[],
        max_parallel=1,
    )
    result = SprintResult(
        name=sprint_name,
        specs_total=canonical_counts["total"],
        specs_succeeded=canonical_counts["succeeded"],
        specs_failed=canonical_counts["failed"],
        specs_skipped=canonical_counts["skipped"],
        total_cost_usd=intake_remediation_cost,
        budget_usd=budget_usd,
        results=[],
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        _write_sprint_audit(
            manifest=manifest,
            result=result,
            canonical_refs=[],
            started_at=now,
            finished_at=now,
            duration=0.0,
            project_root=config.project_root,
            skipped_issues=skipped_issues,
            run_id=run_id,
            story_state=story_state,
        )
        log_dir = config.project_root / ".forge" / "logs" / sprint_name
        log_dir.mkdir(parents=True, exist_ok=True)
        _write_sprint_summary(
            manifest=manifest,
            result=result,
            canonical_refs=[],
            started_at=now,
            finished_at=now,
            duration=0.0,
            sprint_log_dir=log_dir,
            skipped_issues=skipped_issues,
            story_state=story_state,
            run_id=run_id,
            project_root=config.project_root,
        )
    except Exception as exc:
        print(
            f"[forge] Warning: failed to write all-skipped sprint audit: {exc}",
            file=sys.stderr,
        )


def _run_query_mode(
    *,
    args: object,
    config: object,
    config_path: Path,
    milestone: str | None,
    label: str | None,
    issues_arg: str | None = None,
    budget_str: str | None,
    dry_run: bool,
    max_parallel: int | None,
    auto_merge: bool,
    interactive: bool,
    resume: bool,
    no_pull: bool,
    force: bool = False,
    reexec: bool = False,
    accept_unmeasured_spend: list[str] | None = None,
    accept_unmeasured_reason: str | None = None,
    _daemon: object,
    _detach: object,
    _generate_run_id: object,
) -> int:
    """Handle --milestone / --label / --issues query mode.

    ``reexec`` is captured by ``cmd_sprint`` before the detach handoff pops
    FORGE_PREV_RUN_ID; it must be passed in, not recomputed here (the env signal
    is already gone by the time this runs on the real re-exec path).
    """
    from theforge.coordinator.audit_substrate import record_shape_verdict_event
    from theforge.eval.semantic_readiness import SEMANTIC_REASON_CODES
    from theforge.intake import IntakeOutcomeKind
    from theforge.sprint.dag import resolve_satisfied_dependencies
    from theforge.sprint.entry_intake import remediate_entry_skipped_issues
    from theforge.sprint.query import (
        assign_dependency_batches_with_satisfied,
        build_resolved_sprint,
        fetch_issues_by_numbers,
        fetch_issues_for_label,
        fetch_issues_for_milestone,
    )
    from theforge.sprint.shape_gate import (
        apply_shape_gate,
        format_advisory_warning,
        format_operator_action_notice,
        format_skipped_warning,
    )

    try:
        budget_usd = float(budget_str)
    except (TypeError, ValueError):
        print(f"forge sprint: --budget must be a number, got {budget_str!r}", file=sys.stderr)
        return 1
    if budget_usd <= 0:
        print(f"forge sprint: --budget must be > 0, got {budget_usd}", file=sys.stderr)
        return 1

    # Query mode is sequential by default unless the caller explicitly opts in.
    effective_max_parallel = 1 if max_parallel is None else max_parallel

    query_desc = (
        f"milestone '{milestone}'"
        if milestone
        else f"label '{label}'"
        if label
        else f"issues '{issues_arg}'"
    )

    # Fetch issue list (lightweight — just numbers and titles)
    try:
        if milestone:
            issues = fetch_issues_for_milestone(milestone, config.project_root)
        elif label:
            issues = fetch_issues_for_label(label, config.project_root)
        else:
            try:
                issue_numbers = [
                    int(part.strip()) for part in issues_arg.split(",") if part.strip()
                ]
            except ValueError as exc:
                raise RuntimeError(
                    "--issues must be a comma-separated list of integer issue numbers"
                ) from exc
            if not issue_numbers:
                raise RuntimeError("No issue numbers provided")
            issues = fetch_issues_by_numbers(issue_numbers, config.project_root)
    except RuntimeError as exc:
        print(f"[forge] GitHub query failed for {query_desc}: {exc}", file=sys.stderr)
        return 1

    if not issues:
        print(
            f"[forge] No open issues found for {query_desc} — nothing to run.",
            file=sys.stderr,
        )
        return 0

    # ── Shape gate: label check + local shape_check re-run ──────────────
    # Runs BEFORE preflight so we never spend money on malformed issues.
    # Dry-run is a pure dependency preview — bypass the gate there so
    # operators can inspect the DAG without making ``gh`` calls per issue.
    skipped_issues: list = []
    # Pre-compute the run_id so the shape gate's substrate verdict events
    # can be correlated with the sprint that produced them. Detach (Popen
    # re-exec) already happened at the top of cmd_sprint — in the detached
    # child the launch run_id was cached on args, so reuse it here; otherwise
    # (--fg) generate one now and reuse it below where the daemon previously
    # generated it.
    gate_run_id = (
        (args.__dict__.get("_detached_run_id") or _generate_run_id()) if not dry_run else None
    )
    if not dry_run and gate_run_id is not None:
        # Publish run context for agent process-group registration (idempotent with
        # setup_detached_child) and reap groups orphaned by an abruptly-killed
        # prior sprint before launching new work.
        _detach.export_run_context(gate_run_id, config.project_root)
        from theforge import process_group as _process_group  # noqa: PLC0415

        _process_group.reap_orphan_agents(config.project_root)
    gate_sprint_name = _derive_query_sprint_name(
        name=getattr(args, "name", None),
        milestone=milestone,
        label=label,
        issues_arg=issues_arg,
    )
    # Carry-across-re-exec: a prior process may have intake-remediated some
    # issues and stashed their numbers in the environment before re-exec.
    # Consume them here so the post-re-exec gate treats the async-lagging
    # ``needs-grooming`` label as stale rather than authoritative.
    carried_remediated_numbers = _consume_intake_remediated_env()
    if not dry_run:
        classifier_mode = getattr(getattr(config, "shape_check", None), "classifier", "heuristic")
        base_branch_sha = _resolve_base_branch_sha(config)

        def _emit_shape_verdict(payload: dict) -> None:
            event = dict(payload)
            event.setdefault("run_id", gate_run_id)
            event.setdefault("sprint_name", gate_sprint_name)
            event.setdefault("milestone", milestone)
            event.setdefault("base_branch_sha", base_branch_sha)
            record_shape_verdict_event(config.project_root, event)

        gate_result = apply_shape_gate(
            issues,
            config.project_root,
            classifier_mode=classifier_mode,
            force=force,
            emit_verdict=_emit_shape_verdict,
            intake_remediated_numbers=carried_remediated_numbers or None,
        )
        # Capture the gate's original skip/advisory partition before the
        # remediation passes below mutate ``skipped_issues`` — the shape-skip
        # substrate emission (issue #1453) classifies every gate skip, including
        # the ones remediation later moves back to runnable.
        original_gate_skips = list(gate_result.skipped)
        original_gate_advisories = list(gate_result.advisories)
        if gate_result.skipped:
            if force:
                # --force overrides shape refusals, not the operator's
                # ratification record (#2785). Report the two partitions
                # separately so the banner does not claim semantically
                # withheld issues are running — apply_shape_gate has already
                # excluded them from ``runnable``.
                semantic_skips = []
                shape_skips = []
                for sk in gate_result.skipped:
                    if any(code in SEMANTIC_REASON_CODES for code in sk.reason_codes):
                        semantic_skips.append(sk)
                    else:
                        shape_skips.append(sk)
                if shape_skips:
                    print(
                        "[forge] --force in effect; running these shape-flagged issues anyway.\n"
                        f"{format_skipped_warning(shape_skips)}",
                        file=sys.stderr,
                    )
                if semantic_skips:
                    print(
                        "[forge] --force does not override semantic readiness; "
                        "these issues remain withheld.\n"
                        f"{format_skipped_warning(semantic_skips)}",
                        file=sys.stderr,
                    )
            else:
                print(format_skipped_warning(gate_result.skipped), file=sys.stderr)
        if gate_result.advisories:
            print(format_advisory_warning(gate_result.advisories), file=sys.stderr)
        if gate_result.operator_action:
            # Operator-facing banner — deliberate non-dispatch, distinct from
            # the malformed-shape skip warning above. The label cannot be
            # bypassed by --force; the banner prints in both modes.
            print(format_operator_action_notice(gate_result.operator_action), file=sys.stderr)
        issues = gate_result.runnable
        # Operator-action issues are persisted alongside shape-gate skips so
        # the audit/summary surfaces them; the runner inspects reason_codes to
        # apply the StoryOutcome.OPERATOR_ACTION classification.
        skipped_issues = list(gate_result.skipped) + list(gate_result.operator_action)

        # Bridge to intake remediation: entry-skipped issues bypass the
        # in-runner remediation pass, so route them through here. Suppress
        # remediation under --force, which is the operator's explicit
        # escape hatch. Operator-action entries are excluded — the label is
        # the operator's deliberate signal, not a defect to remediate.
        # Semantically withheld issues are excluded on the same grounds: their
        # bodies are structurally fine, and the missing thing is an operator
        # ratification no body edit produces. Editing the body would only
        # change the revision the next evaluation has to read (#2785).
        entry_intake_outcomes: dict[int, object] = {}
        remediation_targets = [
            sk
            for sk in gate_result.skipped
            if not any(code in SEMANTIC_REASON_CODES for code in sk.reason_codes)
        ]
        if remediation_targets and not force:
            entry_intake_outcomes = remediate_entry_skipped_issues(
                remediation_targets,
                config=config,
                log=lambda m: print(f"[forge] {m}", file=sys.stderr),
                sprint_id=gate_run_id,
                milestone=milestone,
            )

        # Re-add successfully remediated issues so the sprint continues without
        # requiring the operator to re-invoke forge sprint.
        if entry_intake_outcomes:
            remediated_numbers = {
                num
                for num, outcome in entry_intake_outcomes.items()
                if outcome.kind is IntakeOutcomeKind.REMEDIATED
            }
            if remediated_numbers:
                skip_by_number = {sk.issue_number: sk for sk in skipped_issues}
                for num in sorted(remediated_numbers):
                    sk = skip_by_number[num]
                    issues.append({"number": sk.issue_number, "title": sk.title})
                skipped_issues = [
                    sk for sk in skipped_issues if sk.issue_number not in remediated_numbers
                ]
            # Publish the union of this run's remediations and any that were
            # carried across an earlier re-exec. If run_sprint re-execs after
            # a mid-sprint source pull, the next entry into _run_query_mode
            # must trust the just-remediated bodies over the async-stale
            # ``needs-grooming`` label.
            _publish_intake_remediated_env(remediated_numbers | carried_remediated_numbers)
        elif carried_remediated_numbers:
            _publish_intake_remediated_env(carried_remediated_numbers)

        # Body-only remediation for shape-gate skips. When intake auto_fix
        # is enabled in edit mode, attempt to repair body-fixable skip
        # reasons (e.g. ``reopened_stale_contract``) by editing the issue
        # body in place. Successfully remediated issues move back to the
        # runnable list so the operator no longer needs ``--force``.
        if not force and skipped_issues:
            issues, skipped_issues = _remediate_shape_gate_skips(
                issues=issues,
                skipped_issues=skipped_issues,
                config=config,
            )

        # Shape-gate skip observability (issue #1453): record every gate skip
        # (and advisory) with its taxonomy category into the audit substrate,
        # tagging remediation outcomes so the postmortem can separate
        # remediated-and-proceeded from declined-by-remediation from
        # still-blocked. Runs after remediation settles; correlates by
        # gate_run_id with the sprint's downstream rows. Observability only —
        # failures are swallowed inside the helper.
        _emit_shape_skip_events(
            config=config,
            run_id=gate_run_id,
            sprint_name=gate_sprint_name,
            milestone=milestone,
            original_skips=original_gate_skips,
            original_advisories=original_gate_advisories,
            intake_outcomes=entry_intake_outcomes,
        )

        if not issues:
            print(
                f"[forge] All {len(skipped_issues)} issue(s) skipped by shape gate "
                "— nothing to run.",
                file=sys.stderr,
            )
            _emit_all_skipped_audit(
                config=config,
                sprint_name=_derive_query_sprint_name(
                    name=getattr(args, "name", None),
                    milestone=milestone,
                    label=label,
                    issues_arg=issues_arg,
                ),
                budget_usd=budget_usd,
                skipped_issues=skipped_issues,
                intake_outcomes=entry_intake_outcomes,
                run_id=gate_run_id,
            )
            return 0

    sprint_name = _derive_query_sprint_name(
        name=getattr(args, "name", None),
        milestone=milestone,
        label=label,
        issues_arg=issues_arg,
    )

    # On the re-exec path, resolve the prior generation's recorded outcomes
    # *before* resolution rather than after it. Resolution re-reads every issue
    # from GitHub, and a story this sprint landed moments before the re-exec is
    # already closed by then: without the prior record in hand, resolution
    # classifies it as a pre-existing closed dependency and it is gone from
    # ``slugs`` / ``canonical_refs_by_slug`` before any reconciliation can act on
    # it (#2847). Best-effort — a miss degrades to today's behavior. Resolved
    # once and reused for the launch locks below.
    prior_outcomes = _resolve_prior_outcomes(config, sprint_name) if reexec else None

    # Build full ResolvedSprint (fetches individual issue bodies via gh)
    try:
        resolved = build_resolved_sprint(
            issues=issues,
            name=sprint_name,
            budget_usd=budget_usd,
            max_parallel=effective_max_parallel,
            project_root=config.project_root,
            prior_outcomes=prior_outcomes,
        )
    except RuntimeError as exc:
        print(f"[forge] Failed to resolve sprint from {query_desc}: {exc}", file=sys.stderr)
        return 1

    if not resolved.stories:
        print(
            f"[forge] No stories could be fetched for {query_desc} — nothing to run.",
            file=sys.stderr,
        )
        return 0

    if dry_run:
        tasks = [task for task, _src, _ref in resolved.stories]
        satisfied = resolve_satisfied_dependencies(
            tasks,
            project_root=config.project_root,
            base_branch=config.workspace.base_branch,
            branch_pattern=config.workspace.branch_pattern,
            pre_satisfied=resolved.closed_dependency_slugs,
        )
        batch_plan = assign_dependency_batches_with_satisfied(
            tasks,
            effective_max_parallel,
            satisfied=satisfied,
        )
        print(f"[dry-run] {query_desc}  {len(tasks)} issue(s)  sprint='{sprint_name}'")
        if resolved.budget_usd > 0.0:
            carry_snapshot = load_sprint_carry_budget_snapshot(
                project_root=config.project_root,
                sprint_name=sprint_name,
                resume=resume,
                reexec=reexec,
            )
            headroom = carry_snapshot.remaining_headroom_usd(resolved.budget_usd)
            budget_line = f"  budget=${resolved.budget_usd:.2f}"
            budget_line += f" carried=${carry_snapshot.carried_cost_usd:.2f}"
            if carry_snapshot.accepted_unmeasured_ceiling_usd > 0.0:
                budget_line += (
                    " accepted_unmeasured_ceiling="
                    f"${carry_snapshot.accepted_unmeasured_ceiling_usd:.2f}"
                )
            budget_line += f" usable_headroom=${max(headroom, 0.0):.2f}"
            if carry_snapshot.headroom_is_lower_bound:
                budget_line += " (lower bound; carried unmeasured spend remains)"
            print(budget_line)
            carry_budget_decision = evaluate_budget(
                accumulated_cost=0.0,
                prior_cost=carry_snapshot.carried_cost_usd,
                budget_usd=resolved.budget_usd,
                unmeasured_spend=carry_snapshot.unresolved_unmeasured_sources,
                accepted_unmeasured_ceiling_usd=carry_snapshot.accepted_unmeasured_ceiling_usd,
            )
            if carry_budget_decision is not None:
                print(
                    f"  cannot dispatch under the supplied ceiling: {carry_budget_decision.detail}"
                )
        for task, _src, _ref in resolved.stories:
            deps = ", ".join(task.depends_on) if task.depends_on else "-"
            if task.slug in batch_plan.blocked:
                status = f"blocked=[{', '.join(batch_plan.blocked[task.slug])}]"
            else:
                batch = batch_plan.assignments.get(task.slug)
                status = "stalled" if batch is None else f"batch={batch}"
            print(
                f"  {status}  #{task.github_issue:>5}  {task.slug:<12} deps=[{deps}]  {task.name}"
            )
        return 0

    # ── Lock acquisition using resolved slugs (no manifest path needed) ──
    # ``reexec`` is threaded in from cmd_sprint (captured before the detach
    # handoff popped FORGE_PREV_RUN_ID) — do not recompute it here.
    slugs = [task.slug for task, _src, _ref in resolved.stories]
    canonical_refs_by_slug = {
        task.slug: canonical_ref for task, _src, canonical_ref in resolved.stories
    }
    # ``prior_outcomes`` was resolved above, ahead of ``build_resolved_sprint``,
    # and is reused here so the launch guard can reconcile already-completed
    # worktrees instead of flattening them into fresh collisions.
    # Stories this same process still has live (or unresolved) agents for — see
    # manifest mode above for why unresolved is deferred rather than dropped.
    liveness = _resolve_story_liveness(config, slugs) if reexec else _NO_LIVENESS
    locked_fds, launch_error, dropped_slugs = _acquire_launch_locks(
        slugs=slugs,
        config=config,
        resume=resume,
        allow_drop=reexec,
        force=force,
        prior_outcomes=prior_outcomes,
        live_slugs=set(liveness.live_slugs),
        unresolved_slugs=set(liveness.unresolved_slugs),
        registered_slugs=set(liveness.registered_slugs),
        canonical_refs_by_slug=canonical_refs_by_slug,
    )
    if launch_error is not None:
        return launch_error

    live_slugs = [s for s in slugs if s not in dropped_slugs]

    # ── run_id / slug ─────────────────────────────────────────────────────
    # Detach (Popen re-exec) already happened at top of cmd_sprint. If we
    # are in the detached child, run_id/slug were resolved there and cached
    # on args (and gate_run_id above reused the cached id so the shape
    # gate's substrate verdict events correlate with this sprint's
    # downstream rows). Otherwise (--fg) reuse the run_id pre-generated for
    # the shape gate.
    cached_run_id = args.__dict__.get("_detached_run_id")
    if cached_run_id is not None:
        run_id = cached_run_id
        locked_fds = reacquire_story_locks_in_daemon(
            live_slugs,
            config.project_root,
            locked_fds,
        )
        # Write a bootstrap state file before run_sprint enters its long
        # baseline-gate / intake-remediation / preflight phases so
        # `forge status --watch` renders the issue list, sprint phase, and
        # base-branch / budget / parallel context immediately on attach
        # rather than displaying an empty table for several minutes.
        from theforge.sprint.state_writer import write_bootstrap_state

        try:
            write_bootstrap_state(
                run_id,
                config.project_root,
                sprint_name=sprint_name,
                sprint_phase="starting",
                base_branch=getattr(getattr(config, "workspace", None), "base_branch", None),
                budget_usd=budget_usd,
                max_parallel=effective_max_parallel,
                issues=[
                    {
                        "number": issue.get("number"),
                        "title": issue.get("title", ""),
                    }
                    for issue in issues
                ],
                skipped_issues=skipped_issues,
            )
        except Exception:
            # Bootstrap state is a UX nicety; do not block sprint launch on
            # write failure (the runner will still create the canonical
            # state file once preflight completes).
            pass
    else:
        run_id = gate_run_id or _generate_run_id()

    # Query mode does not support daemon submission (no manifest file to pass)
    if getattr(args, "detach", False) and _daemon.is_daemon_running(config.project_root):
        release_story_locks(locked_fds)
        print(
            "[forge] --detach is not supported in query mode (--milestone/--label/--issues).\n"
            "        Run with --fg or without --detach instead.",
            file=sys.stderr,
        )
        return 1

    # See the manifest-mode comment: absence of a completion is not completion.
    outcome = "failed"
    cause: str | None = _UNKNOWN_END_CAUSE
    runtime_config = refresh_provenance(
        replace(config, sprint=replace(config.sprint, max_parallel=effective_max_parallel)),
        source_updates={
            "sprint.max_parallel": (
                VALUE_SOURCE_CLI_OVERRIDE if max_parallel is not None else VALUE_SOURCE_DERIVED
            )
        },
    )

    try:
        result = run_sprint(
            SprintRunContext.for_sprint(
                runtime_config,
                resolved,
                auto_merge=auto_merge,
                interactive=interactive,
                notify=not args.no_notify,
                resume=resume,
                reexec=reexec,
                no_pull=no_pull,
                run_id=run_id,
                dropped_slugs=dropped_slugs,
                skipped_issues=skipped_issues,
                entry_intake_outcomes=entry_intake_outcomes,
                force=force,
                live_story_slugs=set(liveness.live_slugs),
                unresolved_live_slugs=set(liveness.unresolved_slugs),
                registered_live_slugs=set(liveness.registered_slugs),
                accept_unmeasured_spend=accept_unmeasured_spend or [],
                accept_unmeasured_reason=accept_unmeasured_reason,
            )
        )
    except KeyboardInterrupt:
        # Ctrl-C is a deliberate termination, not a crash — record it as such
        # rather than folding it into the failure bucket.
        outcome, cause = "stopped", "interrupted by operator (SIGINT)"
        raise
    except Exception as exc:
        import traceback

        cause = _exc_cause(exc)
        _record_run_failure(cause)
        print(f"Sprint error: {exc}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        return 1
    else:
        outcome, cause = "completed", None
    finally:
        release_story_locks(locked_fds)
        # Write terminal marker then remove PID — same pattern as manifest mode.
        _detach.write_run_ended(run_id, config.project_root, outcome, cause=cause)
        _detach.remove_pid(run_id, config.project_root)

    return 0 if result.specs_failed == 0 else 1


def register_parser(subparsers: object) -> None:
    """Register the 'sprint' subcommand parser."""
    sprint_parser = subparsers.add_parser(
        "sprint",
        help="Run multiple stories from a sprint manifest or GitHub query",
    )
    # Manifest path is now optional — query mode uses --milestone/--label/--issues instead
    sprint_parser.add_argument(
        "manifest",
        nargs="?",
        default=None,
        help="Path to sprint.yaml manifest (omit when using --milestone, --label, or --issues)",
    )
    sprint_parser.add_argument("--config", help="Path to forge.yaml (default: auto-detect)")
    sprint_parser.add_argument(
        "--base-branch",
        default=None,
        help="Override workspace.base_branch for this sprint without editing forge.yaml",
    )

    # ── GitHub query mode ────────────────────────────────────────────────
    sprint_parser.add_argument(
        "--milestone",
        metavar="NAME",
        help="Run all open issues in a GitHub milestone (requires --budget)",
    )
    sprint_parser.add_argument(
        "--label",
        metavar="LABEL",
        help="Run all open issues with a GitHub label (requires --budget)",
    )
    sprint_parser.add_argument(
        "--issues",
        metavar="N[,N,...]",
        default=None,
        help="Run specific GitHub issues by comma-separated number (requires --budget)",
    )
    sprint_parser.add_argument(
        "--budget",
        metavar="USD",
        help="Budget ceiling in USD (required for --milestone/--label/--issues)",
    )
    sprint_parser.add_argument(
        "--name",
        metavar="NAME",
        help="Override sprint name (default: milestone or label value)",
    )
    sprint_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print the resolved issue list without executing",
    )
    sprint_parser.add_argument(
        "--parallel",
        metavar="N",
        type=int,
        default=None,
        help="Maximum concurrent stories (overrides forge.yaml max_parallel)",
    )

    # ── Common options ───────────────────────────────────────────────────
    sprint_parser.add_argument(
        "--auto-merge",
        action="store_true",
        default=False,
        help="Merge each story's branch after APPROVE",
    )
    sprint_parser.add_argument(
        "--interactive",
        action="store_true",
        default=False,
        help="Pause for human review at each story",
    )
    sprint_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Show tool activity, heartbeats, and raw agent output (verbose mode)",
    )
    sprint_parser.add_argument(
        "--no-notify",
        action="store_true",
        default=False,
        help="Suppress OS notifications",
    )
    sprint_parser.add_argument(
        "--resume",
        action="store_true",
        default=False,
        help="Auto-triage failed stories and pick optimal re-entry point",
    )
    sprint_parser.add_argument(
        "--detach",
        action="store_true",
        default=False,
        help="When daemon is running, submit and return immediately (manifest mode only)",
    )
    sprint_parser.add_argument(
        "--fg",
        action="store_true",
        default=False,
        help="Run in foreground (skip daemonization)",
    )
    sprint_parser.add_argument(
        "--no-pull",
        action="store_true",
        default=False,
        help="Skip git pull --ff-only before creating fresh worktrees (offline/CI use)",
    )
    sprint_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help=(
            "Bypass the sprint-entry shape gate and run every issue regardless "
            "of needs-grooming labels or local shape check results. Emits a "
            "prominent warning listing every skipped issue's reason codes."
        ),
    )
    sprint_parser.add_argument(
        "--accept-unmeasured-spend",
        metavar="SOURCE",
        action="append",
        default=None,
        help=(
            "Accept one unmeasured-spend source by id (repeatable). A story "
            "whose agent call exited without reporting cost makes the sprint "
            "total a lower bound, and the budget guard then refuses to certify "
            "a cap it cannot evaluate. Naming the source here charges its "
            "recorded ceiling to budget verification in place of the unknown, "
            "so the story runs again. The source id is the one printed in the "
            "'budget unverifiable' refusal (e.g. issue-2206 or "
            "carried:issue-2206 — both name the same work). A source with no "
            "derivable ceiling is refused and the guard stays closed. An "
            "acceptance resolves the recorded occurrence, not the story: if "
            "that story goes unmeasured again it is a new unknown and the guard "
            "closes on it again. The resolution is recorded in "
            "sprint-audit.yaml under sprint.accepted_unmeasured_spend; the cost "
            "stays reported as unmeasured."
        ),
    )
    sprint_parser.add_argument(
        "--accept-unmeasured-reason",
        metavar="TEXT",
        default=None,
        help="Reason recorded alongside each --accept-unmeasured-spend acceptance",
    )
