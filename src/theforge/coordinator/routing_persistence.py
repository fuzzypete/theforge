"""Durable per-story record of the routing decision preflight resolved.

Adaptive routing decides, once per story, how many reviewers the code-review
panel seats and which models fill it.  That decision lives only on the
in-memory ``CoordinatorState`` produced by preflight, and the sprint scheduler
hands it to a resumed story as ``cached_preflight_state``.  When that in-memory
cache is lost — a mid-sprint ``os.execv`` re-exec drops the scheduler's
``preflight_states`` map for stories already in flight — a resumed REVIEW phase
has no routed decision to execute and falls back to the static ``forge.yaml``
roster.  Seating the roster instead of the routed panel mis-sizes every
quantity derived from the seat count (per-reviewer budget share, quorum
threshold), which is how a story can escalate on a quorum that was unreachable
before any reviewer ran (#2154).

This module writes the routing decision next to the run so it survives the
process, and reads it back on resume.  It deliberately records only what
routing consumed and produced:

* the complexity signal preflight measured (the *input* to routing), and
* the models routing selected (the *output*), so a re-derivation that diverges
  is detectable rather than silent.

Validity is keyed on the story text alone.  Routing is a function of the
story's complexity, not of the worktree's git state — a resumed story has
commits its preflight never saw, so reusing ``preflight_cache``'s git-state
fingerprint here would reject every record at exactly the moment it is needed.

Stdlib-only imports (project convention 4: pure-data types live in
low-dependency modules).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .state import CoordinatorState

RECORD_VERSION = 1

_SAFE_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_slug(slug: str) -> str:
    """Filesystem-safe filename stem for a story slug."""
    cleaned = _SAFE_SLUG_RE.sub("-", (slug or "").strip()).strip("-.")
    return cleaned or "story"


def routing_records_dir(project_root: Path) -> Path:
    return Path(project_root) / ".forge" / "routing"


def routing_record_path(project_root: Path, slug: str) -> Path:
    return routing_records_dir(project_root) / f"{_safe_slug(slug)}.json"


def story_content_hash(story_content: str) -> str:
    return hashlib.sha256((story_content or "").encode("utf-8")).hexdigest()


def build_routing_record(
    *,
    slug: str,
    state: "CoordinatorState",
    review_pool_models: list[str],
    dev_model: str | None,
    plan_model: str | None,
    story_content: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Build the persisted routing record for a story.

    ``review_pool_models`` is the *seated* panel — the models REVIEW will run —
    and ``code_reviewer_count`` is stated explicitly so a reader never has to
    infer the panel size from a list that some later stage may have rewritten.
    """
    return {
        "version": RECORD_VERSION,
        "slug": slug,
        "run_id": run_id,
        "story_content_hash": story_content_hash(story_content) if story_content else None,
        "complexity": state.preflight_complexity,
        "complexity_score": state.preflight_complexity_score,
        "implementation_complexity_score": state.preflight_implementation_complexity_score,
        "validation_complexity_score": state.preflight_validation_complexity_score,
        "complexity_projection": state.preflight_complexity_projection,
        "work_type": state.preflight_work_type,
        "sufficiency": state.preflight_sufficiency,
        "domains": list(state.preflight_domains or []),
        "contract_change": bool(state.preflight_contract_change),
        "dev_model": dev_model,
        "plan_model": plan_model,
        "code_reviewers": list(review_pool_models),
        "code_reviewer_count": len(review_pool_models),
    }


def save_routing_record(
    project_root: Path,
    record: dict[str, Any],
) -> Path | None:
    """Persist ``record``; return the path written, or None if it could not be.

    Best-effort by design: a routing record is a recovery aid, so failing to
    write one — unwritable path, or a config value that will not serialize —
    must never take down the run that produced it.
    """
    slug = str(record.get("slug") or "")
    try:
        path = routing_record_path(project_root, slug)
        payload = json.dumps(record, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return None
    return path


def load_routing_record(project_root: Path, slug: str) -> dict[str, Any] | None:
    """Read a story's persisted routing record, or None when unusable."""
    try:
        raw = routing_record_path(project_root, slug).read_text(encoding="utf-8")
    except (OSError, TypeError, ValueError):
        return None
    try:
        record = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(record, dict):
        return None
    if record.get("version") != RECORD_VERSION:
        return None
    return record


def validate_routing_record(
    record: dict[str, Any],
    *,
    story_content: str | None,
) -> tuple[bool, str]:
    """Return (usable, reason) for a loaded routing record.

    A record with no recorded story hash is accepted as ``unverified_story``:
    it still carries a real routing decision, and rejecting it would put the
    run back on the static roster this module exists to prevent.
    """
    if not record.get("code_reviewers"):
        return False, "no_routed_review_pool"
    recorded_hash = record.get("story_content_hash")
    if not recorded_hash:
        return True, "unverified_story"
    if story_content is None:
        return True, "unverified_story"
    if recorded_hash != story_content_hash(story_content):
        return False, "story_content_changed"
    return True, "story_content_match"


def apply_routing_record_to_state(state: "CoordinatorState", record: dict[str, Any]) -> None:
    """Restore the routing *inputs* from ``record`` onto a live state.

    Only the complexity signal is restored — never the verdict.  A recovered
    routing decision says how to size the panel; it says nothing about whether
    the story should run, and synthesizing a PROCEED verdict from it would
    fabricate a judgement no preflight made.
    """
    state.preflight_complexity = record.get("complexity") or state.preflight_complexity
    if record.get("complexity_score") is not None:
        state.preflight_complexity_score = record["complexity_score"]
    if record.get("implementation_complexity_score") is not None:
        state.preflight_implementation_complexity_score = record["implementation_complexity_score"]
    if record.get("validation_complexity_score") is not None:
        state.preflight_validation_complexity_score = record["validation_complexity_score"]
    if record.get("complexity_projection"):
        state.preflight_complexity_projection = record["complexity_projection"]
    if record.get("work_type"):
        state.preflight_work_type = record["work_type"]
    if record.get("sufficiency"):
        state.preflight_sufficiency = record["sufficiency"]
    if record.get("domains") and not state.preflight_domains:
        state.preflight_domains = list(record["domains"])
    if record.get("contract_change"):
        state.preflight_contract_change = True
