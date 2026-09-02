"""Durable landing evidence, kept separate from the run record it describes.

A run record says what a run *did*: what it was asked for, what it built, what
the reviewers and the gate concluded. Whether that work then reached a branch is
a fact about the world's response to the run, observed later — sometimes much
later, by a different process, for an asynchronous landing mode. Forge has
historically denormalized that fact back into the record as a ``landing_status``
field and rewritten the record to carry it, which has two costs the spike for
issue #2598 exists to remove:

* The record stops being immutable. A record that is rewritten after approval
  cannot be an attestation of anything, because the bytes reviewers and the gate
  keyed to can change afterwards.
* The field is a *claim* rather than a *consequence*. It has drifted from git
  twice — #2374 read a commit merely mentioning an issue as proof its branch
  merged, and #2111 re-triaged a landed story as stale.

So landing evidence lives here, in its own artifacts, under a simple rule:

    A **positive landing assertion** is created only by an observed successful
    landing, and names what landed where. Everything else is a **landing
    attempt**, which records that a landing was tried and what happened, and
    never asserts that anything landed.

The asymmetry is deliberate. An attempt is cheap and may be wrong about the
future ("queued"); an assertion is expensive and must be right about the past.
Absence of an assertion means *unresolved*, never "failed" — a distinction the
old three-state field could not draw, because ``None`` had to serve both for "no
landing owed" and "nobody has looked".

``landing_status`` is not removed by this module. It remains the sprint
scheduler's in-process answer to "may I dispatch this story's dependents yet",
which is a live scheduling question, not durable truth. What changes is that
durable answers come from here.

Stdlib-only imports, per project convention 4 (pure-data types in low-dependency
modules). The artifact store is ``json`` + ``pathlib`` and nothing else, so this
module is importable from the coordinator, the sprint scheduler and the CLI
without any of them acquiring each other.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Mapping
from pathlib import Path

# Where landing evidence is published. A sibling of ``.forge/audits/runs`` for
# the same reason the runs tree exists: it is project memory, tracked by forge's
# generated ``.gitignore``, and travels to a fresh clone.
LANDING_EVIDENCE_RELPATH = (".forge", "audits", "landing")

# Where project memory waits between being drained out of the project-root
# checkout and being merged into the base branch. Ignored by ``.forge/**``, so
# its presence never dirties the checkout.
#
# It lives here, in the low-dependency module, because it is a *read* location
# as much as a write one: evidence published to the memory branch is no longer
# in ``.forge/audits/landing`` and will not be until the memory pull request
# merges, and a reader that only looked at the canonical tree would conclude the
# landing it just recorded had never been observed. The transport in
# ``sprint/memory_publication`` imports the constant from here rather than the
# other way round, so the reader does not acquire a dependency on the writer.
PROJECT_MEMORY_STAGING_RELPATH = (".forge", "memory-staging")

ASSERTION_KIND = "landing_assertion"
ATTEMPT_KIND = "landing_attempt"

# Bumped when the artifact shape changes in a way a reader must migrate across.
LANDING_EVIDENCE_SCHEMA_VERSION = 1

# Outcomes an attempt may report. Deliberately *not* a landed state: an attempt
# artifact cannot assert a landing whatever value it carries, so there is no
# spelling of "landed" available here to be mistaken for evidence.
#
#   queued   — a carrier exists and the landing has not resolved (auto-merge
#              queue, an open PR). The normal sprint-exit state for async modes.
#   refused  — the landing was declined before it was attempted (dirty root,
#              unmet dependency, missing review).
#   failed   — the landing ran and did not land the work.
#   timeout  — a bounded wait for the landing to resolve expired.
#   closed   — the carrier was resolved without landing (PR closed unmerged).
#   unknown  — the landing was attempted and its outcome could not be observed.
ATTEMPT_OUTCOMES = frozenset({"queued", "refused", "failed", "timeout", "closed", "unknown"})

# The partition of :data:`ATTEMPT_OUTCOMES` into "this landing has not resolved"
# and "this landing resolved without landing". Named here, once, because the
# substrate projection (#2849) has to draw the same line as :func:`landing_state`
# does over the files, and two spellings of it would let a SQL reader and a
# filesystem reader disagree about the same run.
#
# Anything outside ``RESOLVED_NON_LANDING_OUTCOMES`` reads as unresolved. That is
# the fail-open direction the asymmetry demands: only an assertion may say
# "landed", and only an enumerated resolved outcome may say "did not land".
OPEN_ATTEMPT_OUTCOMES = frozenset({"queued", "unknown"})
RESOLVED_NON_LANDING_OUTCOMES = ATTEMPT_OUTCOMES - OPEN_ATTEMPT_OUTCOMES

# Carriers that can deliver a landing. ``pull_request`` names a PR (merged
# through GitHub); ``merge`` names a merge performed directly against the base
# checkout. Both are recorded with the reference that identifies them.
CARRIER_KINDS = frozenset({"pull_request", "merge"})

# Fields an assertion must name for it to be evidence rather than an opinion.
# Every one of these answers a question the spec requires the artifact to
# answer: which run, which work, where it went, and what carried it.
_ASSERTION_REQUIRED = (
    "run_id",
    "slug",
    "landing_mode",
    "target_branch",
    "reviewed_commit",
    "gated_commit",
    "carrier_kind",
    "carrier_ref",
    "landed_commit",
    "observer",
    "observed_at",
)

_ATTEMPT_REQUIRED = (
    "run_id",
    "slug",
    "landing_mode",
    "target_branch",
    "outcome",
    "observer",
    "observed_at",
)


class LandingEvidenceError(ValueError):
    """An evidence artifact was built or read without a load-bearing field.

    A ``ValueError`` because that is what it is — the caller passed something
    that cannot be evidence. Raising rather than emitting a partial artifact is
    the fail-closed direction: a missing ``landed_commit`` would produce an
    assertion that claims a landing it cannot name, which is exactly the class
    of claim this module exists to stop.
    """


def utc_now_iso() -> str:
    """An observation timestamp in the one format every artifact here uses."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _require(payload: Mapping, fields: tuple[str, ...], what: str) -> None:
    missing = [name for name in fields if not str(payload.get(name) or "").strip()]
    if missing:
        raise LandingEvidenceError(f"{what} is missing required field(s): {', '.join(missing)}")


# ── Builders ─────────────────────────────────────────────────────────────


def build_landing_attempt(
    *,
    run_id: str,
    slug: str,
    landing_mode: str,
    target_branch: str,
    outcome: str,
    observer: str,
    source_commit: str | None = None,
    gated_commit: str | None = None,
    pr_url: str | None = None,
    detail: str | None = None,
    observed_at: str | None = None,
) -> dict:
    """Build a landing-attempt artifact.

    ``outcome`` must be one of :data:`ATTEMPT_OUTCOMES`. An unrecognised outcome
    raises rather than being stored verbatim: the whole point of the closed set
    is that a reader can enumerate the non-landed states, and a free-text
    outcome would put an unreadable value where that enumeration is made.

    ``source_commit`` (the reviewed commit) and ``gated_commit`` are optional
    here — an attempt asserts nothing about them — but recording them is what
    lets a *later* observer promote this attempt into an assertion. The
    attestations are keyed to those commits, and only the process that ran the
    story is in a position to know them; a reconciliation running after sprint
    exit has the queued attempt and nothing else.
    """
    if outcome not in ATTEMPT_OUTCOMES:
        raise LandingEvidenceError(
            f"unknown landing attempt outcome {outcome!r}; expected one of "
            f"{', '.join(sorted(ATTEMPT_OUTCOMES))}"
        )
    payload = {
        "kind": ATTEMPT_KIND,
        "schema_version": LANDING_EVIDENCE_SCHEMA_VERSION,
        "run_id": run_id,
        "slug": slug,
        "landing_mode": landing_mode,
        "target_branch": target_branch,
        "outcome": outcome,
        "source_commit": source_commit,
        "gated_commit": gated_commit,
        "pr_url": pr_url,
        "detail": detail,
        "observer": observer,
        "observed_at": observed_at or utc_now_iso(),
    }
    _require(payload, _ATTEMPT_REQUIRED, "landing attempt")
    return payload


def build_landing_assertion(
    *,
    run_id: str,
    slug: str,
    landing_mode: str,
    target_branch: str,
    reviewed_commit: str,
    gated_commit: str,
    carrier_kind: str,
    carrier_ref: str,
    landed_commit: str,
    observer: str,
    pr_url: str | None = None,
    observed_at: str | None = None,
) -> dict:
    """Build a positive landing assertion.

    Only call this having *observed* a successful landing. The signature is the
    enforcement: there is no way to produce one of these without naming the
    commit that landed and the carrier that delivered it, so a caller who only
    knows that a landing was requested cannot express that here.

    ``reviewed_commit`` and ``gated_commit`` are the source commits the review
    and gate attestations were keyed to. They are recorded separately from
    ``landed_commit`` because forge's default ``merge_strategy: squash`` means
    the two are routinely different SHAs — the reviewed commit is not even an
    ancestor of the target branch. Ancestry therefore cannot supply this
    evidence, and the carrier is what bridges the two.
    """
    if carrier_kind not in CARRIER_KINDS:
        raise LandingEvidenceError(
            f"unknown landing carrier kind {carrier_kind!r}; expected one of "
            f"{', '.join(sorted(CARRIER_KINDS))}"
        )
    payload = {
        "kind": ASSERTION_KIND,
        "schema_version": LANDING_EVIDENCE_SCHEMA_VERSION,
        "run_id": run_id,
        "slug": slug,
        "landing_mode": landing_mode,
        "target_branch": target_branch,
        "reviewed_commit": reviewed_commit,
        "gated_commit": gated_commit,
        "carrier_kind": carrier_kind,
        "carrier_ref": carrier_ref,
        "landed_commit": landed_commit,
        "pr_url": pr_url,
        "observer": observer,
        "observed_at": observed_at or utc_now_iso(),
    }
    _require(payload, _ASSERTION_REQUIRED, "landing assertion")
    return payload


# ── Validators ───────────────────────────────────────────────────────────


def is_landing_assertion(payload: object) -> bool:
    """Whether ``payload`` is a well-formed positive landing assertion.

    Read-side counterpart to :func:`build_landing_assertion`. A malformed
    artifact is not an assertion — it does not raise here, because a reader
    asking "did this land?" of a corrupt file must get "unresolved", not an
    exception and not a yes.
    """
    if not isinstance(payload, Mapping):
        return False
    if payload.get("kind") != ASSERTION_KIND:
        return False
    try:
        _require(payload, _ASSERTION_REQUIRED, "landing assertion")
    except LandingEvidenceError:
        return False
    return payload.get("carrier_kind") in CARRIER_KINDS


def is_landing_attempt(payload: object) -> bool:
    """Whether ``payload`` is a well-formed landing attempt."""
    if not isinstance(payload, Mapping):
        return False
    if payload.get("kind") != ATTEMPT_KIND:
        return False
    try:
        _require(payload, _ATTEMPT_REQUIRED, "landing attempt")
    except LandingEvidenceError:
        return False
    return payload.get("outcome") in ATTEMPT_OUTCOMES


# ── Artifact store ───────────────────────────────────────────────────────


def landing_evidence_dir(project_root: Path) -> Path:
    """The canonical evidence tree — where every artifact is *written*."""
    return project_root.joinpath(*LANDING_EVIDENCE_RELPATH)


def landing_evidence_read_dirs(project_root: Path) -> list[Path]:
    """Every place evidence may be found, canonical tree first.

    Reads span the staging area as well as the canonical tree because the
    transport drains the canonical tree to publish it. Between a publish and the
    memory pull request merging, the artifacts exist only in staging, and a
    reader that missed them would report a run it had already observed to have
    landed as unresolved — and reconciliation would go looking for the landing
    all over again.
    """
    return [
        landing_evidence_dir(project_root),
        project_root.joinpath(*PROJECT_MEMORY_STAGING_RELPATH).joinpath(*LANDING_EVIDENCE_RELPATH),
    ]


def _glob_evidence(project_root: Path, pattern: str) -> list[Path]:
    """Paths matching ``pattern`` across every read location, in filename order."""
    matches: list[Path] = []
    for directory in landing_evidence_read_dirs(project_root):
        if directory.exists():
            matches.extend(directory.glob(pattern))
    return sorted(matches, key=lambda path: path.name)


def _assertion_path(project_root: Path, run_id: str) -> Path:
    return landing_evidence_dir(project_root) / f"{run_id}.landed.json"


def _write_json(path: Path, payload: Mapping) -> Path:
    """Write an artifact so a concurrent reader never sees a half-written file.

    Parallel sprints publish from more than one worker, and the publication
    transport reads this tree while stories are still writing into it.

    The temporary file goes *outside* the evidence tree, in the same scratch
    directory the canonical run-record writer uses. The evidence tree is tracked
    project memory; a write-in-progress file inside it would be transient dirt
    in the shared checkout and a publishable artifact, which is exactly what it
    must never be. ``.forge/audits/.tmp`` is denied by ``.forge/**`` and
    re-included by nothing, and it is on the same filesystem, so the replace
    stays atomic.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = path.parent.parent / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / (path.name + ".tmp")
    tmp.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def write_landing_attempt(project_root: Path, attempt: Mapping) -> Path:
    """Append a landing-attempt artifact for this run.

    Attempts accumulate: a story whose landing was refused on sibling dirt and
    then retried made two attempts, and collapsing them would erase the retry
    the operator needs to see. The sequence number is derived from what is
    already on disk so a resumed sprint continues the series rather than
    overwriting it.
    """
    if not is_landing_attempt(attempt):
        raise LandingEvidenceError("refusing to write a malformed landing attempt")
    run_id = str(attempt["run_id"])
    directory = landing_evidence_dir(project_root)
    directory.mkdir(parents=True, exist_ok=True)
    # Counted across every read location so a published-but-unmerged series
    # continues rather than restarting and overwriting itself on republish.
    existing = len(_glob_evidence(project_root, f"{run_id}.attempt-*.json"))
    return _write_json(directory / f"{run_id}.attempt-{existing:03d}.json", attempt)


def write_landing_assertion(project_root: Path, assertion: Mapping) -> Path:
    """Publish the positive landing assertion for a run, once.

    Write-once by design. A landing happens a single time, so a second
    assertion for the same run is either a duplicate observation (harmless, and
    the existing artifact already says the same thing) or a contradiction —
    and silently replacing the first with the second would make the artifact
    exactly the rewritable claim it replaced. The existing path is returned
    either way, so callers can be run repeatedly (reconciliation is expected to
    be).
    """
    if not is_landing_assertion(assertion):
        raise LandingEvidenceError("refusing to write a malformed landing assertion")
    run_id = str(assertion["run_id"])
    existing = _glob_evidence(project_root, f"{run_id}.landed.json")
    if existing:
        return existing[0]
    return _write_json(_assertion_path(project_root, run_id), assertion)


def _read_json(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def read_landing_assertion(project_root: Path, run_id: str) -> dict | None:
    """The positive landing assertion for ``run_id``, or ``None`` if unresolved.

    ``None`` means unresolved — not failed. Every caller that turns this into an
    operator-facing answer must preserve that distinction.
    """
    for path in _glob_evidence(project_root, f"{run_id}.landed.json"):
        payload = _read_json(path)
        if is_landing_assertion(payload):
            return payload
    return None


def read_landing_attempts(project_root: Path, run_id: str) -> list[dict]:
    """Every recorded landing attempt for ``run_id``, oldest first."""
    attempts = []
    for path in _glob_evidence(project_root, f"{run_id}.attempt-*.json"):
        payload = _read_json(path)
        if is_landing_attempt(payload):
            attempts.append(payload)
    return attempts


def landed_run_ids(project_root: Path) -> set[str]:
    """Run ids with a positive landing assertion published in this checkout."""
    landed = set()
    for path in _glob_evidence(project_root, "*.landed.json"):
        payload = _read_json(path)
        if is_landing_assertion(payload):
            landed.add(str(payload["run_id"]))
    return landed


def landing_state(project_root: Path, run_id: str) -> str:
    """The durable answer to "did this run land?" — the read model in one call.

    Three values, and the third is the one the old field could not express:

    * ``"landed"``   — a positive assertion exists.
    * ``"unresolved"`` — no assertion, and either no attempt or an attempt that
      is still open (``queued``, ``unknown``).
    * ``"not_landed"`` — no assertion, and the most recent attempt reports a
      resolved non-landing (``refused``, ``failed``, ``timeout``, ``closed``).

    Note what is *not* here: a run with no evidence at all is ``unresolved``.
    Defaulting it to either landed or failed is how a read model invents facts,
    and the spec forbids both directions.
    """
    if read_landing_assertion(project_root, run_id) is not None:
        return "landed"
    attempts = read_landing_attempts(project_root, run_id)
    if not attempts:
        return "unresolved"
    last = attempts[-1]["outcome"]
    if last in RESOLVED_NON_LANDING_OUTCOMES:
        return "not_landed"
    return "unresolved"
