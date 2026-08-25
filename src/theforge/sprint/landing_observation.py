"""Observing landings and turning what was observed into evidence (#2598).

:mod:`theforge.coordinator.landing_evidence` defines *what* an evidence artifact
is and refuses to build a malformed one. This module decides *when* one may be
built, which is the harder half: the rule is that a positive landing assertion
requires a landing that was observed to have happened and can be named, and
everything short of that is an attempt.

Two observers live here, and the split follows the shape of the landing modes
rather than the shape of the code:

**In-sprint** (:func:`observe_landing`) — the sprint's integration step just ran
a landing and knows the reviewed commit, the gated commit, the carrier and the
result. Synchronous modes resolve here.

**Post-exit** (:func:`reconcile_landing_evidence`) — an asynchronous mode
(``merge-pr`` with a queued auto-merge, ``pr``) exits the sprint with the
landing unresolved, and the merge happens minutes or days later with no forge
process watching. Reconciliation is what closes those: it reads the queued
attempt each run left behind, asks git and GitHub whether the landing has since
happened, and publishes a positive assertion when it has. It writes a *new*
artifact and never touches the run record, which is the whole point — the run
did not change, only the world's response to it did.

Reconciliation is idempotent by construction: assertions are write-once, and an
attempt is only appended when it would say something the last one did not.

Evidence is never derived from prose. ``sprint/dag.py`` consults a base commit
whose *message* closes an issue as a last-resort merge signal, which is
appropriate for "should I re-run this story" and inappropriate here — it is the
signal that produced #2374, and a landing assertion that cannot name the commit
that landed is the claim this whole model exists to remove. When this module
cannot name a landed commit, it records ``unknown`` and stops.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from ..config import ForgeConfig
from ..coordinator.landing_evidence import (
    build_landing_assertion,
    build_landing_attempt,
    landing_evidence_dir,
    landing_evidence_read_dirs,
    read_landing_attempts,
    write_landing_assertion,
    write_landing_attempt,
)
from ..log_util import _log_line

# Attempt outcomes a later observation may still upgrade into an assertion.
#
# ``timeout`` belongs here and ``failed`` does not, and the difference is the
# whole reason the attempt outcome is richer than ``landing_status``: a queued
# PR forge stopped waiting for may merge an hour later, while a landing that was
# refused will not land itself. ``closed`` and ``refused`` are terminal for the
# same reason. Re-polling a genuinely failed landing forever would turn the
# reconciliation pass into a poll loop over the whole history.
RECONCILABLE_OUTCOMES = frozenset({"queued", "unknown", "timeout"})

OBSERVER_INTEGRATION = "sprint.integration"
OBSERVER_QUEUED_PR = "sprint.queued-pr"
OBSERVER_RECONCILE = "forge.reconcile"


def _log(msg: str) -> None:
    _log_line("[sprint]", msg)


def _sh(cmd: str, cwd: Path, timeout: int = 60) -> tuple[bool, str]:
    from ..coordinator import util as _cu  # noqa: PLC0415

    return _cu._run_shell(cmd, cwd, timeout=timeout)


def attested_commits(state: Any) -> tuple[str | None, str | None]:
    """``(reviewed_commit, gated_commit)`` for a coordinator state.

    These are the commits the review and gate attestations are keyed to. They
    are recorded on the run's live state and nowhere durable that a later
    process can reach, which is why every attempt artifact carries them
    forward.
    """
    reviewed = None
    metadata = getattr(state, "review_cycle_metadata", None) or []
    for meta in reversed(metadata):
        candidate = getattr(meta, "reviewed_commit", None)
        if candidate:
            reviewed = candidate
            break
    gated = getattr(state, "last_gate_commit", None)
    return reviewed, gated


def _pr_url(merge_info: dict) -> str | None:
    url = merge_info.get("pr_url")
    if isinstance(url, str) and url:
        return url
    guard = merge_info.get("guard_evidence")
    if isinstance(guard, dict):
        candidate = guard.get("pr_url")
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _merged_pr_commit(project_root: Path, branch: str) -> tuple[str, str, str] | None:
    """``(landed_commit, pr_ref, pr_url)`` when GitHub reports a merged PR.

    GitHub's ``mergeCommit`` is the only source that survives
    ``merge_strategy: squash``: the reviewed commit is not an ancestor of the
    target branch at all under a squash, so nothing local can bridge the two.
    Without a merge commit there is no assertion to make, whatever ``mergedAt``
    says — a PR reported merged whose merge commit cannot be named is exactly
    the unnameable claim this refuses to record.
    """
    ok, out = _sh(
        f"gh pr list --head {shlex.quote(branch)} --state merged --limit 5 "
        "--json number,url,mergeCommit,mergedAt",
        project_root,
    )
    if not ok:
        return None
    try:
        entries = json.loads(out.strip() or "[]")
    except ValueError:
        return None
    if not isinstance(entries, list):
        return None
    best: tuple[str, str, str, str] | None = None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        merge_commit = entry.get("mergeCommit")
        oid = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
        merged_at = entry.get("mergedAt")
        number = entry.get("number")
        if not (isinstance(oid, str) and oid and isinstance(merged_at, str) and merged_at):
            continue
        url = entry.get("url") if isinstance(entry.get("url"), str) else ""
        candidate = (merged_at, oid, f"#{number}" if number else url, url)
        if best is None or candidate[0] > best[0]:
            best = candidate
    if best is None:
        return None
    return best[1], best[2], best[3]


def _open_pr_url(project_root: Path, branch: str) -> str | None:
    ok, out = _sh(
        f"gh pr list --head {shlex.quote(branch)} --state open --limit 1 --json url",
        project_root,
    )
    if not ok:
        return None
    try:
        entries = json.loads(out.strip() or "[]")
    except ValueError:
        return None
    if isinstance(entries, list) and entries and isinstance(entries[0], dict):
        url = entries[0].get("url")
        return url if isinstance(url, str) and url else None
    return None


def _merge_commit_from_topology(
    project_root: Path, base_branch: str, source_commit: str
) -> str | None:
    """The merge commit on ``base_branch`` that brought ``source_commit`` in.

    Only answers for a genuine merge: the source commit must be reachable from
    the base branch, and the answer is the first merge along the ancestry path.
    A fast-forward has no merge commit, in which case the base tip that contains
    the commit is named instead — still a commit that provably contains the
    work, which is what the assertion claims.
    """
    if not source_commit:
        return None
    ref = f"origin/{base_branch}"
    ok_ref, _ = _sh(f"git rev-parse --verify {shlex.quote(ref)}", project_root)
    if not ok_ref:
        ref = base_branch
        ok_ref, _ = _sh(f"git rev-parse --verify {shlex.quote(ref)}", project_root)
        if not ok_ref:
            return None
    reachable, _ = _sh(
        f"git merge-base --is-ancestor {shlex.quote(source_commit)} {shlex.quote(ref)}",
        project_root,
    )
    if not reachable:
        return None
    ok_merges, merges = _sh(
        f"git rev-list --ancestry-path --merges {shlex.quote(source_commit)}..{shlex.quote(ref)}",
        project_root,
    )
    if ok_merges and merges.strip():
        # rev-list is newest-first; the oldest is the merge that brought it in.
        return merges.split()[-1]
    ok_tip, tip = _sh(f"git rev-parse {shlex.quote(ref)}", project_root)
    return tip.strip() if ok_tip and tip.strip() else None


def _append_attempt_if_new(project_root: Path, attempt: dict) -> Path | None:
    """Write ``attempt`` unless the last one already said the same thing.

    Reconciliation is expected to run repeatedly against a landing that stays
    unresolved for weeks. Appending an identical ``queued`` artifact each time
    would turn the evidence tree into a poll log.
    """
    previous = read_landing_attempts(project_root, str(attempt["run_id"]))
    if previous:
        last = previous[-1]
        same = all(
            last.get(key) == attempt.get(key)
            for key in ("outcome", "pr_url", "detail", "source_commit")
        )
        if same:
            return None
    return write_landing_attempt(project_root, attempt)


def observe_landing(
    config: ForgeConfig,
    *,
    run_id: str,
    slug: str,
    landing_mode: str,
    landing_status: str | None,
    merge_info: dict | None,
    reviewed_commit: str | None,
    gated_commit: str | None,
    observer: str = OBSERVER_INTEGRATION,
    attempt_outcome: str | None = None,
) -> dict | None:
    """Record what this landing attempt did, and assert only if it landed.

    ``attempt_outcome`` lets a caller that knows *more* than ``landing_status``
    say so. The scheduler collapses every non-landing to ``failed`` because that
    is all it needs to decide whether to release dependents, but a queued PR
    forge stopped waiting for is not the same fact as a landing that was
    refused: the first is still reconcilable and the second is not. The
    distinction is invisible in ``landing_status`` and load-bearing here.

    Returns the assertion when one was made, otherwise ``None``. Never raises:
    a sprint must not fail because it could not describe its own landing, and
    the absence of evidence already reads as unresolved.
    """
    project_root = config.project_root
    base_branch = config.workspace.base_branch
    merge = merge_info if isinstance(merge_info, dict) else {}
    pr_url = _pr_url(merge)
    try:
        # ``landing_status == "landed"`` covers the queued-PR wrap-up, where the
        # scheduler learns the PR merged from polling rather than from the
        # merge_info the landing step returned.
        if merge.get("merged") or landing_status == "landed":
            landed_commit, carrier_kind, carrier_ref = _resolve_landed_carrier(
                config, slug=slug, merge_info=merge, reviewed_commit=reviewed_commit
            )
            if landed_commit and reviewed_commit and gated_commit:
                assertion = build_landing_assertion(
                    run_id=run_id,
                    slug=slug,
                    landing_mode=landing_mode,
                    target_branch=base_branch,
                    reviewed_commit=reviewed_commit,
                    gated_commit=gated_commit,
                    carrier_kind=carrier_kind,
                    carrier_ref=carrier_ref,
                    landed_commit=landed_commit,
                    pr_url=pr_url,
                    observer=observer,
                )
                write_landing_assertion(project_root, assertion)
                return assertion
            # A landing that happened and cannot be named is recorded as
            # unobserved, not as landed. The scheduler still treats it as
            # landed — that is live state — but the corpus does not.
            missing = [
                name
                for name, value in (
                    ("landed_commit", landed_commit),
                    ("reviewed_commit", reviewed_commit),
                    ("gated_commit", gated_commit),
                )
                if not value
            ]
            _append_attempt_if_new(
                project_root,
                build_landing_attempt(
                    run_id=run_id,
                    slug=slug,
                    landing_mode=landing_mode,
                    target_branch=base_branch,
                    outcome="unknown",
                    source_commit=reviewed_commit,
                    gated_commit=gated_commit,
                    pr_url=pr_url,
                    detail=f"landing reported merged but could not be named: missing "
                    f"{', '.join(missing)}",
                    observer=observer,
                ),
            )
            return None

        if attempt_outcome:
            outcome = attempt_outcome
            detail = str(merge.get("error") or attempt_outcome)
        elif merge.get("merge_queued") or landing_status == "pending_integration":
            outcome, detail = "queued", merge.get("landing_path") or "awaiting resolution"
        elif landing_status == "failed":
            outcome, detail = "failed", str(merge.get("error") or "landing failed")
        elif landing_status is None:
            outcome, detail = "queued", "no landing obligation resolved in this run"
        else:
            outcome, detail = "unknown", str(landing_status)
        _append_attempt_if_new(
            project_root,
            build_landing_attempt(
                run_id=run_id,
                slug=slug,
                landing_mode=landing_mode,
                target_branch=base_branch,
                outcome=outcome,
                source_commit=reviewed_commit,
                gated_commit=gated_commit,
                pr_url=pr_url,
                detail=detail,
                observer=observer,
            ),
        )
    except Exception as exc:  # noqa: BLE001 — evidence is never load-bearing for the run
        _log(f"Warning: could not record landing evidence for {slug}: {exc}")
    return None


def _resolve_landed_carrier(
    config: ForgeConfig,
    *,
    slug: str,
    merge_info: dict,
    reviewed_commit: str | None,
) -> tuple[str | None, str, str]:
    """Name the commit that landed and the carrier that delivered it."""
    project_root = config.project_root
    base_branch = config.workspace.base_branch
    branch = config.workspace.branch_pattern.format(slug=slug)

    rollback = merge_info.get("gate_green_rollback")
    if isinstance(rollback, dict) and rollback.get("landed_commit"):
        return str(rollback["landed_commit"]), "merge", base_branch

    if merge_info.get("action") == "merge-pr" or _pr_url(merge_info):
        merged = _merged_pr_commit(project_root, branch)
        if merged is not None:
            landed, pr_ref, _url = merged
            return landed, "pull_request", pr_ref
        url = _pr_url(merge_info)
        if url and reviewed_commit:
            landed = _merge_commit_from_topology(project_root, base_branch, reviewed_commit)
            if landed:
                return landed, "pull_request", url
        return None, "pull_request", url or branch

    ok, tip = _sh(f"git rev-parse {shlex.quote(base_branch)}", project_root)
    if ok and tip.strip():
        return tip.strip(), "merge", base_branch
    return None, "merge", base_branch


# ── Post-exit reconciliation ─────────────────────────────────────────────


def unresolved_landing_run_ids(project_root: Path) -> list[str]:
    """Runs with an open landing attempt and no positive assertion yet.

    Driven by the evidence tree rather than by the run-record tree, which is
    both cheaper (a repository accumulates run records forever; only a handful
    ever have an unresolved landing) and more correct: a run with no attempt
    never had a landing obligation, and manufacturing one for it would invent
    an obligation that never existed.
    """
    with_attempts: set[str] = set()
    asserted: set[str] = set()
    for directory in landing_evidence_read_dirs(project_root):
        if not directory.exists():
            continue
        with_attempts.update(
            path.name.split(".attempt-", 1)[0] for path in directory.glob("*.attempt-*.json")
        )
        asserted.update(
            path.name.split(".landed.json", 1)[0] for path in directory.glob("*.landed.json")
        )
    return sorted(with_attempts - asserted)


def reconcile_landing_evidence(
    config: ForgeConfig,
    *,
    observer: str = OBSERVER_RECONCILE,
    run_ids: set[str] | None = None,
) -> list[dict]:
    """Close out landings that resolved after the sprint that requested them.

    This is the seam asynchronous modes need and did not have. ``merge-pr`` with
    a queued auto-merge and ``pr`` both exit the sprint with the landing owed
    and unresolved; without something that looks again, they would stay
    unresolved forever and the corpus would carry a permanent gap where the
    strongest fact about the run belongs.

    Returns the assertions published by this pass. Safe to run repeatedly and
    safe to run concurrently with a sprint: it writes only into the evidence
    tree, never into a run record and never into the project-root checkout's
    index or branch.
    """
    project_root = config.project_root
    base_branch = config.workspace.base_branch
    published: list[dict] = []
    candidates = [
        run_id
        for run_id in unresolved_landing_run_ids(project_root)
        if run_ids is None or run_id in run_ids
    ]
    if not candidates:
        return []
    _sh(f"git fetch origin {shlex.quote(base_branch)}", project_root, timeout=120)

    for run_id in candidates:
        attempts = read_landing_attempts(project_root, run_id)
        if not attempts:
            continue
        last = attempts[-1]
        if last["outcome"] not in RECONCILABLE_OUTCOMES:
            continue
        summary = {"slug": str(last.get("slug") or "")}

        branch = config.workspace.branch_pattern.format(slug=summary["slug"])
        reviewed = last.get("source_commit")
        gated = last.get("gated_commit")
        landed_commit: str | None = None
        carrier_kind = "pull_request"
        carrier_ref = last.get("pr_url") or branch
        pr_url = last.get("pr_url")

        merged = _merged_pr_commit(project_root, branch)
        if merged is not None:
            landed_commit, carrier_ref, merged_url = merged
            pr_url = merged_url or pr_url
        elif reviewed:
            topology = _merge_commit_from_topology(project_root, base_branch, reviewed)
            if topology:
                landed_commit = topology
                carrier_kind = "merge" if not pr_url else "pull_request"
                carrier_ref = pr_url or base_branch

        if landed_commit and reviewed and gated:
            assertion = build_landing_assertion(
                run_id=run_id,
                slug=summary["slug"] or run_id,
                landing_mode=last["landing_mode"],
                target_branch=base_branch,
                reviewed_commit=reviewed,
                gated_commit=gated,
                carrier_kind=carrier_kind,
                carrier_ref=str(carrier_ref),
                landed_commit=landed_commit,
                pr_url=pr_url,
                observer=observer,
            )
            write_landing_assertion(project_root, assertion)
            published.append(assertion)
            continue

        open_pr = _open_pr_url(project_root, branch)
        _append_attempt_if_new(
            project_root,
            build_landing_attempt(
                run_id=run_id,
                slug=summary["slug"] or run_id,
                landing_mode=last["landing_mode"],
                target_branch=base_branch,
                outcome="queued" if open_pr else "unknown",
                source_commit=reviewed,
                gated_commit=gated,
                pr_url=open_pr or pr_url,
                detail="pull request still open" if open_pr else "no landing observed yet",
                observer=observer,
            ),
        )
    if published:
        _log(
            f"Reconciled {len(published)} landing(s) into positive evidence at "
            f"{landing_evidence_dir(project_root)}."
        )
    return published
