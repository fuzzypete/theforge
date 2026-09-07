"""Automatic scheduling of the semantic evaluator at admission time.

``semantic_readiness`` derives what is *on record* for a revision; this module
answers the question that derivation cannot: when policy requires a review and
no record exists, who runs the evaluator? Before #2907 the answer was "an
operator, one ``forge review-semantic`` at a time", so the gate withheld work on
the absence of a step nothing had been scheduled to perform.

What this module changes is *whether an evaluation happens*, never *what a
result means*:

* It invokes only for a document whose structural admission already says
  implementation-ready and whose policy requirement is ``required``.
* It invokes only when the current revision has no attempt on record — a
  successful evaluation, an accepted/awaiting ratification state, or a prior
  failure all count as an attempt, so a recurring transition against an
  unchanged revision reuses what is already recorded rather than spending
  again.
* It never writes a :class:`~theforge.eval.semantic_storage.SemanticRatificationRecord`.
  A raised finding still withholds admission until an operator ratifies it,
  exactly as when the evaluator is invoked by hand.
* It is *total*: every failure mode — an unlaunchable agent, a store write
  error, a profile that resolves to no model identity — is recorded as
  ``evaluation_failed`` and reported as such. Nothing here can leave a document
  reading as evaluated-clean on the strength of a failure.

The revision that is evaluated is the revision that occasioned the withholding:
callers hand in the title/body/labels they already fetched, and those are fed to
the evaluator through its ``gh_issue_view`` seam rather than re-fetched, so a
concurrent edit cannot land an evaluation against a revision no gate saw.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from theforge.agent_types import AgentResult
from theforge.config.types import ModelProfile
from theforge.eval.semantic_input import build_semantic_evaluation_input
from theforge.eval.semantic_prompt import PROMPT_CONTRACT_VERSION
from theforge.eval.semantic_readiness import (
    SEMANTIC_REVIEW_REQUIRED_STATE,
    STATE_EVALUATION_FAILED,
    STATE_UNEVALUATED,
    SemanticReadiness,
    derive_semantic_readiness_for_revision,
    semantic_requirement,
)
from theforge.eval.semantic_storage import (
    BASELINE_PROVENANCE_AUTOMATIC,
    SemanticEvaluationRecord,
    SemanticReviewStore,
    utc_now_iso,
)
from theforge.eval.semantic_types import STATUS_EVALUATION_FAILED

_log = logging.getLogger(__name__)

SEMANTIC_LOCK_DIR = Path(".forge") / "locks" / "semantic"


def _issue_view_for_revision(
    title: str,
    body: str,
    labels: tuple[str, ...] | list[str],
) -> Callable[[int, Path], subprocess.CompletedProcess[str]]:
    """Return a ``gh issue view`` stand-in that replays one already-fetched revision.

    ``review_issue_semantically`` always loads the issue through this seam. On
    the automatic path the caller has already fetched the revision that made the
    gate withhold admission, and a second live fetch could race an edit and
    record an evaluation of text no admission decision was made against. Feeding
    the fetched revision back through the seam binds the evaluation to it.
    """
    payload = json.dumps(
        {
            "title": title,
            "body": body,
            "labels": [{"name": name} for name in labels],
        }
    )

    def _view(_number: int, _project_root: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=payload, stderr="")

    return _view


@contextmanager
def _revision_scheduling_lock(project_root: Path, key: str):
    """Yield ``True`` when this process may schedule the evaluation of *key*.

    The check-then-invoke sequence over an unlocked JSONL store is otherwise
    racy: a query-mode gate and a manifest resolution can both observe "no
    attempt" and both spend. The lock is non-blocking on purpose — a peer
    already evaluating this exact revision *is* the at-most-once guarantee being
    honoured, so the loser skips invocation and re-derives readiness rather than
    waiting out an agent call inside a gate.
    """
    handle = None
    try:
        lock_dir = project_root / SEMANTIC_LOCK_DIR
        lock_dir.mkdir(parents=True, exist_ok=True)
        handle = (lock_dir / f"{key}.lock").open("a+")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if handle is not None:
            handle.close()
        yield False
        return
    except OSError as exc:
        # No lock, no scheduling. Proceeding unserialized would let two
        # concurrent admissions evaluate the same revision under the same
        # prompt contract, which is the bound AC5 states; deferring costs a
        # withheld document that the next transition evaluates once the lock
        # directory is usable again, and spends nothing meanwhile.
        _log.warning(
            "semantic scheduling lock unavailable for %s (%s); deferring the evaluation "
            "rather than running it unserialized",
            key,
            exc,
        )
        if handle is not None:
            handle.close()
        yield False
        return

    try:
        yield True
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


def _lock_key(issue_ref: str, input_digest: str, prompt_contract_version: str) -> str:
    raw = f"{issue_ref}|{input_digest}|{prompt_contract_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _failed_readiness(
    *,
    issue_ref: str,
    input_digest: str,
    canonical_type: str | None,
    lifecycle_state: str,
    detail: str,
) -> SemanticReadiness:
    """Build an ``evaluation_failed`` readiness without consulting the store.

    Used where the store itself is the thing that failed. The requirement axis
    is still policy applied to the document's own type, so a `not_required`
    document is not withheld by an audit-storage problem that has no bearing on
    its admission.
    """
    return SemanticReadiness(
        issue_ref=issue_ref,
        input_digest=input_digest,
        canonical_type=canonical_type,
        requirement=semantic_requirement(
            canonical_type=canonical_type,
            lifecycle_state=lifecycle_state,
        ),
        state=STATE_EVALUATION_FAILED,
        detail=detail,
    )


def _record_cannot_attempt(
    *,
    store: SemanticReviewStore,
    readiness: SemanticReadiness,
    model_id: str,
    prompt_contract_version: str,
    profile_name: str,
    model_name: str,
    failure_detail: str,
) -> bool:
    """Persist an ``evaluation_failed`` record for an invocation that never ran.

    AC4 names "cannot be attempted at all" alongside failure, timeout and
    refusal. Without this the document would report ``unevaluated`` — accurate
    about the record but silent about the attempt — and the next transition
    would spend again on a call that cannot succeed.

    Returns whether the record reached the store. It may not: the store is
    exactly what fails in some of these cases, which is why the caller reports
    the failure from the return value rather than from a re-derivation that
    would read back as merely unevaluated.
    """
    try:
        store.append_record(
            SemanticEvaluationRecord(
                issue_ref=readiness.issue_ref,
                canonical_type=readiness.canonical_type,
                input_digest=readiness.input_digest,
                model_id=model_id,
                prompt_contract_version=prompt_contract_version,
                status=STATUS_EVALUATION_FAILED,
                cache_hit=False,
                duration_seconds=0.0,
                cost_usd=0.0,
                started_at=utc_now_iso(),
                completed_at=utc_now_iso(),
                configured_profile_name=profile_name,
                configured_model_name=model_name,
                failure_detail=failure_detail,
            )
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "could not record semantic evaluation failure for %s: %s", readiness.issue_ref, exc
        )
        return False
    return True


def ensure_semantic_evaluation(
    *,
    issue_number: int,
    title: str,
    body: str,
    labels: tuple[str, ...] | list[str],
    project_root: Path,
    secrets: dict[str, str] | None,
    profile: ModelProfile,
    prompt_contract_version: str = PROMPT_CONTRACT_VERSION,
    lifecycle_state: str = SEMANTIC_REVIEW_REQUIRED_STATE,
    store: SemanticReviewStore | None = None,
    agent_runner: Callable[..., AgentResult] | None = None,
) -> SemanticReadiness:
    """Derive semantic readiness, scheduling the missing evaluation if policy needs one.

    Returns the readiness admission consumes. When policy does not require a
    review, or the current revision already carries an attempt, this is exactly
    what :func:`derive_semantic_readiness` returns and nothing is spent.
    """
    from theforge.eval.semantic_runner import (  # noqa: PLC0415
        build_audit_only_profile as _audit_profile,
    )
    from theforge.eval.semantic_runner import (  # noqa: PLC0415
        normalize_issue_ref,
        review_issue_semantically,
        semantic_model_id,
    )

    semantic_store = store or SemanticReviewStore(project_root)
    issue_ref = normalize_issue_ref(issue_number)
    evaluation_input = build_semantic_evaluation_input(title=title, body=body, labels=labels)

    def _store_failure(exc: Exception) -> SemanticReadiness:
        return _failed_readiness(
            issue_ref=issue_ref,
            input_digest=evaluation_input.input_digest,
            canonical_type=evaluation_input.canonical_type,
            lifecycle_state=lifecycle_state,
            detail=f"the semantic audit record for the current revision could not be read: {exc}",
        )

    def _derive() -> SemanticReadiness:
        """Derive from the store, reporting a store failure as one.

        Unreadable audit state is not evidence of readiness. Letting the
        exception escape would land in each caller's fail-open handler and admit
        a policy-required document with no semantic state at all — the outcome
        AC4 forbids — so it is answered here, fail-closed.
        """
        try:
            return derive_semantic_readiness_for_revision(
                issue_ref=issue_ref,
                input_digest=evaluation_input.input_digest,
                canonical_type=evaluation_input.canonical_type,
                store=semantic_store,
                lifecycle_state=lifecycle_state,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("semantic audit records unreadable for %s: %s", issue_ref, exc)
            return _store_failure(exc)

    readiness = _derive()
    if not readiness.required:
        # Policy exempts this document. Nothing is evaluated for it, and the
        # presence or absence of a record does not change its admission.
        return readiness
    if readiness.state != STATE_UNEVALUATED:
        # Something is already on record for this exact revision — a successful
        # evaluation (rated or not) or a prior failure. Either way the revision
        # has had its attempt and re-running would spend on evidence that
        # already exists.
        return readiness

    def _attempted() -> bool | None:
        """True/False when the store can answer, ``None`` when it cannot."""
        try:
            return (
                semantic_store.latest_attempt_for_revision(
                    issue_ref=issue_ref,
                    input_digest=readiness.input_digest,
                    prompt_contract_version=prompt_contract_version,
                )
                is not None
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("semantic audit records unreadable for %s: %s", issue_ref, exc)
            return None

    def _cannot_attempt(model_id: str, detail: str) -> SemanticReadiness:
        """Record and report an evaluation that could not be attempted at all."""
        _record_cannot_attempt(
            store=semantic_store,
            readiness=readiness,
            model_id=model_id,
            prompt_contract_version=prompt_contract_version,
            profile_name=profile_name,
            model_name=model_name,
            failure_detail=detail,
        )
        # Reported from here rather than re-derived: when the store is what
        # failed, the record is not there to read back, and the revision would
        # report as merely unevaluated instead of as the failure it is.
        return _failed_readiness(
            issue_ref=issue_ref,
            input_digest=evaluation_input.input_digest,
            canonical_type=evaluation_input.canonical_type,
            lifecycle_state=lifecycle_state,
            detail=detail,
        )

    profile_name = getattr(profile, "name", "") or ""
    model_name = getattr(profile, "model", "") or ""

    attempted = _attempted()
    if attempted is None:
        return _store_failure(RuntimeError("semantic audit records could not be read"))
    if attempted:
        return readiness

    try:
        model_id = semantic_model_id(_audit_profile(profile))
    except Exception as exc:  # noqa: BLE001
        # The configured profile does not resolve to a model identity, so the
        # evaluation cannot be attempted at all.
        return _cannot_attempt("", f"semantic evaluation could not be attempted: {exc}")

    key = _lock_key(issue_ref, readiness.input_digest, prompt_contract_version)
    with _revision_scheduling_lock(project_root, key) as may_schedule:
        if not may_schedule:
            # Either a peer holds this revision's lock or the lock itself is
            # unavailable. Both are deferrals, not results: nothing is spent,
            # nothing is recorded, and the document keeps whatever state it
            # already has — which for an unevaluated required revision is
            # withheld.
            _log.info(
                "semantic evaluation of %s (%s) deferred; not scheduling it here",
                issue_ref,
                readiness.input_digest,
            )
            return _derive()

        # Re-check under the lock: a peer may have completed between the read
        # above and the lock acquisition.
        rechecked = _attempted()
        if rechecked is None:
            return _store_failure(RuntimeError("semantic audit records could not be read"))
        if rechecked:
            return _derive()

        # A frozen baseline is a human calibration claim about a revision, and
        # the automatic path has no human to make one. Where one already exists
        # it is preserved untouched (passing defect ids would trip the
        # already-frozen guard); where none does, an empty baseline is frozen
        # with automatic provenance, which a later human freeze supersedes.
        baseline_defect_ids: tuple[str, ...] | None = None
        try:
            if semantic_store.frozen_baseline(readiness.input_digest) is None:
                baseline_defect_ids = ()
        except Exception:  # noqa: BLE001
            baseline_defect_ids = None

        try:
            review_issue_semantically(
                issue_number=issue_number,
                project_root=project_root,
                secrets=secrets,
                profile=profile,
                prompt_contract_version=prompt_contract_version,
                baseline_defect_ids=baseline_defect_ids,
                baseline_provenance=BASELINE_PROVENANCE_AUTOMATIC,
                store=semantic_store,
                gh_issue_view=_issue_view_for_revision(title, body, labels),
                agent_runner=agent_runner,
            )
        except Exception as exc:  # noqa: BLE001
            # review_issue_semantically records its own failures for everything
            # that happens once invocation starts; reaching here means the call
            # could not be attempted (baseline conflict, store failure, an
            # agent-launch wrapper error that escaped). Report it as the failure
            # it is rather than letting the document read as unevaluated.
            _log.warning("automatic semantic evaluation of %s failed: %s", issue_ref, exc)
            return _cannot_attempt(model_id, f"semantic evaluation could not be attempted: {exc}")

    return _derive()


def semantic_dispatch_withholding(
    *,
    issue_number: int,
    revision_digest: str,
    revision_type: str | None,
    project_root: Path,
    store: SemanticReviewStore | None = None,
    lifecycle_state: str = SEMANTIC_REVIEW_REQUIRED_STATE,
) -> SemanticReadiness | None:
    """Return the readiness withholding the revision about to be dispatched, if any.

    Admission reads a revision, evaluates it and decides; the sprint then
    fetches the issue again to build the story it dispatches. Between those two
    reads the document can change, and the fetched revision would otherwise
    inherit an admission decision made about text that no longer exists — the
    one way a document could reach a dev agent without a ratified review of
    *the revision the agent is handed*.

    This closes that handoff. The caller passes the revision identity of the
    payload it actually built (``TaskStory.source_revision_digest`` /
    ``source_revision_type``, computed by ``GitHubIssueSource.fetch`` from the
    same title/body/labels the story carries), and readiness is re-derived
    against it. Read-only and free: no ``gh`` call, no evaluation, no spend. A
    revision that changed under the sprint is simply withheld, and the next
    entry evaluates it in its own right.

    Returns ``None`` when the dispatched revision is admissible — including
    when policy requires no review of it — and the withholding readiness
    otherwise.
    """
    from theforge.eval.semantic_runner import normalize_issue_ref  # noqa: PLC0415

    issue_ref = normalize_issue_ref(issue_number)
    semantic_store = store or SemanticReviewStore(project_root)
    try:
        readiness = derive_semantic_readiness_for_revision(
            issue_ref=issue_ref,
            input_digest=revision_digest,
            canonical_type=revision_type,
            store=semantic_store,
            lifecycle_state=lifecycle_state,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("semantic audit records unreadable for %s: %s", issue_ref, exc)
        readiness = _failed_readiness(
            issue_ref=issue_ref,
            input_digest=revision_digest,
            canonical_type=revision_type,
            lifecycle_state=lifecycle_state,
            detail=(
                f"the semantic audit record for the dispatched revision could not be read: {exc}"
            ),
        )
    return readiness if readiness.withholds_admission else None
