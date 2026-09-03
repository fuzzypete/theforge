"""Did an agent act on a claim it was given? The receipt instrument (#2866).

The knowledge loop already records what was *injected* into a run
(``context_manifests[*].prior_run_context``) and the negative case — a reviewer
raising a finding that repeats a claim the developer had already been shown
(:mod:`theforge.knowledge_uptake`). Neither records the positive case.

This module records it, and the shape of the instrument is the whole point:

1. **It is a receipt, not an opinion.** There is no field for usefulness,
   satisfaction, confidence, or a counterfactual. An agent names each claim it
   was given, selects one disposition from a closed set, and — only for the
   dispositions that assert the claim influenced the work — points at an
   observable consequence in this run's own artifacts.
2. **Every citation is checked against the record.** A cited claim that was not
   injected into that phase is an *unmatched citation* and is excluded from every
   count of use. An injected claim the debrief never names is *unaddressed*, which
   is a distinct fact from any disposition the agent could have chosen. Neither is
   ever reported as "unused".
3. **Corroboration is existence, not causation.** Resolving a pointer establishes
   that the cited consequence is in the run's artifacts. It does not establish
   that the injected claim caused it, and nothing here may be rendered as
   verified, confirmed, or effective use.
4. **It decides nothing.** No routing, selection, readiness, or landing code
   reads this output. Malformed, missing, or unrecognised debrief data is
   recorded and never branched on.

Every judgment is deterministic pure Python over artifacts the record already
carries, so a figure produced today is reproducible from the record tomorrow.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

#: Version of the receipt-capture shape. Its *presence* on a record is what makes
#: the run comparable at all: a record written before this instrument existed
#: cannot say whether any agent was asked for a debrief, and must read as
#: uncomparable rather than as a run whose agents all declined to answer.
RECEIPT_CAPTURE_VERSION = 1

# ── The closed disposition set ────────────────────────────────────────────────
#
# Closed on purpose. A disposition outside this set is recorded as unrecognised
# and excluded; it is never mapped onto a nearby one, because the nearest
# neighbour of a word an agent invented is a guess, and a guess in a count reads
# as a measurement.

#: The claim changed a decision the agent made.
DISPOSITION_CHANGED_DECISION = "changed_decision"
#: The claim prompted the agent to verify something it would not have checked.
DISPOSITION_PROMPTED_VERIFICATION = "prompted_verification"
#: The claim confirmed an approach the agent had already taken.
DISPOSITION_CONFIRMED_APPROACH = "confirmed_approach"
#: The claim did not bear on this task.
DISPOSITION_IRRELEVANT = "irrelevant"
#: The agent already knew what the claim says.
DISPOSITION_ALREADY_KNOWN = "already_known"
#: The claim no longer describes the code, or was wrong.
DISPOSITION_STALE_OR_WRONG = "stale_or_wrong"

#: Dispositions that assert the claim influenced the work. Only these require an
#: evidence pointer, and only these can become a use claim.
USE_ASSERTING_DISPOSITIONS = frozenset(
    {DISPOSITION_CHANGED_DECISION, DISPOSITION_PROMPTED_VERIFICATION}
)

#: Dispositions that assert the claim did *not* change what the agent did. They
#: are reported under their own names — never merged into an "unused" bucket.
NON_USE_DISPOSITIONS = (
    DISPOSITION_CONFIRMED_APPROACH,
    DISPOSITION_ALREADY_KNOWN,
    DISPOSITION_IRRELEVANT,
    DISPOSITION_STALE_OR_WRONG,
)

CLOSED_DISPOSITIONS = frozenset(USE_ASSERTING_DISPOSITIONS) | frozenset(NON_USE_DISPOSITIONS)

# Per-entry outcomes.
OUTCOME_CORROBORATED_USE = "corroborated_use_claim"
OUTCOME_UNCORROBORATED_USE = "uncorroborated_use_claim"
OUTCOME_NON_USE = "non_use"
OUTCOME_UNRECOGNISED = "unrecognised_disposition"
OUTCOME_UNMATCHED = "unmatched_citation"

# Per-phase statuses. Mutually exclusive; every phase that assembled context has
# exactly one.
PHASE_NOTHING_TO_DEBRIEF = "nothing_to_debrief"
PHASE_DEBRIEFED = "debriefed"
PHASE_UNDEBRIEFED = "undebriefed"

# Run-level statuses.
STATUS_CAPTURED = "captured"
STATUS_UNCOMPARABLE = "uncomparable_pre_capture"

#: The one sentence every rendering of this instrument must carry.
INTERPRETATION_NOTE = (
    "receipt distribution only; corroboration establishes that a cited consequence "
    "exists, not that the injected claim caused it — no effectiveness or ROI "
    "conclusion follows"
)

# Pointer-resolution kinds.
POINTER_FILE = "changed_file"
POINTER_PLAN = "plan"
POINTER_COMMIT = "commit"
POINTER_TEST = "test"
POINTER_UNRESOLVED = "unresolved"

#: When two agents in the same phase debrief the same claim differently, exactly
#: one disposition is counted for that claim. The order below is the tie-break,
#: and it is deliberately conservative: every non-use disposition outranks every
#: use-asserting one, so a disagreement can never resolve *into* a use count.
#: Unrecognised ranks last — anything an agent actually selected from the set
#: says more than a word that is not in it. Raw entries stay visible either way.
_DISPOSITION_PRECEDENCE = (
    DISPOSITION_STALE_OR_WRONG,
    DISPOSITION_IRRELEVANT,
    DISPOSITION_ALREADY_KNOWN,
    DISPOSITION_CONFIRMED_APPROACH,
    DISPOSITION_PROMPTED_VERIFICATION,
    DISPOSITION_CHANGED_DECISION,
)

# A path-like token: either something containing a directory separator, or a
# bare filename with a source-file extension. Matched against the run's own
# changed-file set, never against the filesystem — the record is the ground
# truth, and the working tree may have moved on.
_PATH_TOKEN_RE = re.compile(
    r"(?:[\w.@-]+/)+[\w.@-]+|\b[\w.@-]+\.(?:py|md|yaml|yml|json|toml|txt|ts|tsx|js|sh|cfg|ini)\b"
)
_PLAN_SECTION_RE = re.compile(r"(?:§|section\s+|step\s+|part\s+)(\d+)", re.IGNORECASE)


# ── Debrief normalization ────────────────────────────────────────────────────


def normalize_debrief(payload: Any) -> dict:
    """Read an agent's raw ``knowledge_debrief`` payload into audit-shaped data.

    Returns ``{"entries": [...] | None, "malformed_reason": str | None,
    "malformed_entry_count": int}``. Nothing here raises and nothing here
    branches on content: a debrief that is absent, the wrong type, or full of
    junk is *described*, so the record can say which it was.
    """
    if payload is None:
        return {"entries": None, "malformed_reason": "absent", "malformed_entry_count": 0}
    if not isinstance(payload, (list, tuple)):
        return {
            "entries": None,
            "malformed_reason": f"not_a_list({type(payload).__name__})",
            "malformed_entry_count": 0,
        }

    entries: list[dict] = []
    malformed = 0
    for item in payload:
        if not isinstance(item, Mapping):
            malformed += 1
            continue
        claim_ref = _text(item.get("claim_ref"))
        if not claim_ref:
            malformed += 1
            continue
        entries.append(
            {
                "claim_ref": claim_ref,
                "disposition": _text(item.get("disposition")),
                "did": _text(item.get("did"), limit=500),
                "evidence": _string_list(item.get("evidence")),
            }
        )
    return {"entries": entries, "malformed_reason": None, "malformed_entry_count": malformed}


def extract_knowledge_debrief(text: str) -> Any:
    """Pull the top-level ``knowledge_debrief`` out of a YAML phase output.

    Returns the raw payload, or ``None`` when the output is not YAML, is not a
    mapping, or simply does not carry the key. Deliberately total: this runs on
    the phase-output path, and an agent that produced an unparseable receipt must
    not be able to take the phase down with it.
    """
    import yaml  # noqa: PLC0415

    body = str(text or "")
    # Plan output is bare YAML; review output is fenced. Try both rather than
    # guessing from the phase, so one extractor serves every YAML contract.
    candidates = [body]
    candidates.extend(re.findall(r"```(?:yaml|yml)?\s*\n(.*?)```", body, re.DOTALL))
    for candidate in candidates:
        try:
            data = yaml.safe_load(candidate)
        except Exception:  # noqa: BLE001 - a broken receipt is recorded, never raised
            continue
        if isinstance(data, Mapping) and data.get("knowledge_debrief") is not None:
            return data["knowledge_debrief"]
    return None


def debrief_submission(
    *,
    phase: str,
    agent_role: str,
    phase_iteration: int | None,
    source: str,
    payload: Any,
) -> dict:
    """Build one audit-shaped debrief submission record for coordinator state."""
    normalized = normalize_debrief(payload)
    return {
        "phase": (phase or "").lower(),
        "agent_role": agent_role or "",
        "phase_iteration": phase_iteration,
        "source": source,
        **normalized,
    }


# ── Exposure ─────────────────────────────────────────────────────────────────


def _capture_present(context_manifests: Sequence[Mapping[str, Any]]) -> bool:
    """False when any prior-run manifest predates claim-exposure capture.

    Same rule as the uptake matcher: one uncaptured manifest means the run
    cannot say what that phase's agent was shown, so nothing computed over the
    rest is a complete statement about the run.
    """
    for entry in context_manifests:
        if not isinstance(entry, Mapping):
            continue
        prior = entry.get("prior_run_context")
        if not isinstance(prior, Mapping):
            continue
        if not isinstance(prior.get("claim_exposure"), Mapping):
            return False
    return True


def _exposure_by_phase(
    context_manifests: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, dict]]:
    """``{phase: {claim_ref: claim record}}`` for every claim rendered this run.

    Keyed by phase and *not* by recipient. A review phase can append one manifest
    per reviewer, each carrying the same claims; keying by agent role would let a
    claim exposed to reviewer A and cited by reviewer B count as unaddressed and
    as an unmatched citation at the same time, which is two false statements
    about one claim that was in fact both shown and answered.
    """
    exposure: dict[str, dict[str, dict]] = {}
    for entry in context_manifests:
        if not isinstance(entry, Mapping):
            continue
        phase = _text(entry.get("phase")).lower()
        prior = entry.get("prior_run_context")
        if not isinstance(prior, Mapping):
            continue
        bucket = exposure.setdefault(phase, {})
        for included in prior.get("included") or []:
            if not isinstance(included, Mapping):
                continue
            for claim in included.get("claims") or []:
                if not isinstance(claim, Mapping):
                    continue
                ref = _text(claim.get("claim_ref"))
                text = _text(claim.get("claim"))
                if not ref or not text:
                    continue
                bucket.setdefault(
                    ref,
                    {
                        "claim_ref": ref,
                        "claim": text,
                        "run_id": claim.get("run_id"),
                        "phase": phase,
                    },
                )
    return exposure


# ── Evidence-pointer resolution ──────────────────────────────────────────────


def resolve_pointer(pointer: Any, artifacts: Mapping[str, Any]) -> dict:
    """Resolve one evidence pointer against this run's recorded artifacts.

    The resolver is deliberately literal and deterministic. It answers only
    "does the thing this pointer names exist in what this run produced?" — it
    never reads the agent's free-text ``did`` field, because that text
    contributes to no count and letting it decide corroboration would make it
    contribute to one.

    Recognised pointer forms, tried in order:

    * a path-like token (``src/theforge/foo.py``, ``foo.py``) — resolves iff the
      run's changed-file set contains a path that matches it exactly or by path
      suffix. A pointer naming a path the run did not change is *unresolved*,
      not unrecognised: it named something checkable and the check failed.
    * ``plan``, optionally with a section (``plan §3``, ``plan step 3``) —
      resolves iff a plan was recorded, and, when a section is named, iff that
      section or step id is present in it.
    * ``commit`` — resolves iff the run recorded at least one commit.
    * ``test`` — resolves iff the run changed at least one test file.

    Anything else is ``unrecognised_pointer_form``, which yields an
    *uncorroborated* use claim. A pointer nobody can check is not a pointer.
    """
    text = _text(pointer)
    if not text:
        return _pointer(text, resolved=False, kind=POINTER_UNRESOLVED, reason="empty_pointer")

    changed_files = _string_list(artifacts.get("changed_files"))
    tokens = _PATH_TOKEN_RE.findall(text)
    if tokens:
        for token in tokens:
            match = _match_changed_file(token, changed_files)
            if match is not None:
                return _pointer(text, resolved=True, kind=POINTER_FILE, target=match)
        return _pointer(
            text,
            resolved=False,
            kind=POINTER_FILE,
            reason="named_path_not_in_changed_files",
        )

    lowered = text.lower()
    if "plan" in lowered:
        return _resolve_plan_pointer(text, artifacts)
    if "commit" in lowered:
        commits = _string_list(artifacts.get("commits"))
        if commits:
            return _pointer(text, resolved=True, kind=POINTER_COMMIT, target=commits[0])
        return _pointer(text, resolved=False, kind=POINTER_COMMIT, reason="no_commit_recorded")
    if "test" in lowered:
        test_files = [path for path in changed_files if _looks_like_test(path)]
        if test_files:
            return _pointer(text, resolved=True, kind=POINTER_TEST, target=test_files[0])
        return _pointer(text, resolved=False, kind=POINTER_TEST, reason="no_test_file_changed")

    return _pointer(
        text, resolved=False, kind=POINTER_UNRESOLVED, reason="unrecognised_pointer_form"
    )


def _resolve_plan_pointer(text: str, artifacts: Mapping[str, Any]) -> dict:
    plan_text = _text(artifacts.get("plan_text"), limit=None)
    if not plan_text:
        return _pointer(text, resolved=False, kind=POINTER_PLAN, reason="no_plan_recorded")
    match = _PLAN_SECTION_RE.search(text)
    if match is None:
        return _pointer(text, resolved=True, kind=POINTER_PLAN, target="plan")
    section = match.group(1)
    step_ids = {str(item) for item in artifacts.get("plan_step_ids") or []}
    if section in step_ids:
        return _pointer(text, resolved=True, kind=POINTER_PLAN, target=f"plan step {section}")
    if re.search(rf"(?:§|#+\s*|\bstep\s+|\bsection\s+){re.escape(section)}\b", plan_text, re.I):
        return _pointer(text, resolved=True, kind=POINTER_PLAN, target=f"plan section {section}")
    return _pointer(text, resolved=False, kind=POINTER_PLAN, reason="plan_section_not_found")


def _match_changed_file(token: str, changed_files: Iterable[str]) -> str | None:
    """Match a pointer's path token against the run's changed files.

    Suffix matching, so ``foo.py`` matches ``src/pkg/foo.py`` but ``ofoo.py``
    does not: the comparison is on whole path segments, never on raw substrings.
    """
    normalized = str(Path(token.strip().strip("`'\"()[],.")))
    if normalized in {"", "."}:
        return None
    wanted = normalized.split("/")
    for path in changed_files:
        parts = str(Path(path)).split("/")
        if len(wanted) <= len(parts) and parts[-len(wanted) :] == wanted:
            return path
    return None


def _looks_like_test(path: str) -> bool:
    parts = str(Path(path)).split("/")
    name = parts[-1] if parts else ""
    return "tests" in parts[:-1] or name.startswith("test_") or name.endswith("_test.py")


def _pointer(
    pointer: str,
    *,
    resolved: bool,
    kind: str,
    target: str | None = None,
    reason: str | None = None,
) -> dict:
    return {
        "pointer": pointer,
        "resolved": resolved,
        "kind": kind,
        "target": target,
        "unresolved_reason": reason,
    }


# ── Verification ─────────────────────────────────────────────────────────────


def build_receipt_report(
    *,
    context_manifests: Sequence[Mapping[str, Any]] | None,
    debriefs: Sequence[Mapping[str, Any]] | None,
    artifacts: Mapping[str, Any] | None = None,
) -> dict:
    """Verify this run's debriefs against what it recorded having injected.

    Returns the audit block stored on the run record. It is pure: same manifests,
    same debriefs, same artifacts — same block, on any machine, at any later time.
    """
    manifests = [m for m in (context_manifests or []) if isinstance(m, Mapping)]
    submissions = [d for d in (debriefs or []) if isinstance(d, Mapping)]
    run_artifacts = dict(artifacts or {})

    base = {
        "capture_version": RECEIPT_CAPTURE_VERSION,
        "interpretation": INTERPRETATION_NOTE,
    }

    if not _capture_present(manifests):
        return {
            **base,
            "status": STATUS_UNCOMPARABLE,
            "note": (
                "this run predates claim-exposure capture; what each agent was shown "
                "is not recorded, so no debrief can be matched against it"
            ),
            "phases": [],
            "entries": [],
            "unaddressed": [],
            "counts": None,
        }

    exposure = _exposure_by_phase(manifests)
    entries = _classify_entries(submissions, exposure=exposure, artifacts=run_artifacts)
    counted = _collapse(entries)
    phases = _phase_blocks(exposure, submissions, counted)
    unaddressed = _unaddressed(exposure, counted)

    return {
        **base,
        "status": STATUS_CAPTURED,
        "note": "",
        "phases": phases,
        "entries": entries,
        "unaddressed": unaddressed,
        "counts": _counts(phases, exposure, counted, unaddressed),
    }


def _classify_entries(
    submissions: Sequence[Mapping[str, Any]],
    *,
    exposure: Mapping[str, Mapping[str, dict]],
    artifacts: Mapping[str, Any],
) -> list[dict]:
    """Decide every debrief entry, in submission order. Raw entries all survive."""
    classified: list[dict] = []
    for order, submission in enumerate(submissions):
        phase = _text(submission.get("phase")).lower()
        phase_exposure = exposure.get(phase) or {}
        for index, entry in enumerate(submission.get("entries") or []):
            if not isinstance(entry, Mapping):
                continue
            claim_ref = _text(entry.get("claim_ref"))
            disposition = _text(entry.get("disposition"))
            record = {
                "phase": phase,
                "agent_role": submission.get("agent_role") or "",
                "phase_iteration": submission.get("phase_iteration"),
                "source": submission.get("source"),
                "claim_ref": claim_ref,
                "disposition": disposition,
                "did": _text(entry.get("did"), limit=500),
                "evidence": [],
                "submission_order": order,
                "entry_index": index,
            }
            if claim_ref not in phase_exposure:
                # Named a claim this phase was never shown. Excluded from every
                # count of use — the citation is about the record, not the work.
                classified.append({**record, "outcome": OUTCOME_UNMATCHED})
                continue
            if disposition not in CLOSED_DISPOSITIONS:
                classified.append({**record, "outcome": OUTCOME_UNRECOGNISED})
                continue
            if disposition not in USE_ASSERTING_DISPOSITIONS:
                classified.append({**record, "outcome": OUTCOME_NON_USE})
                continue
            pointers = [resolve_pointer(p, artifacts) for p in entry.get("evidence") or []]
            corroborated = bool(pointers) and all(p["resolved"] for p in pointers)
            classified.append(
                {
                    **record,
                    "evidence": pointers,
                    "outcome": (
                        OUTCOME_CORROBORATED_USE if corroborated else OUTCOME_UNCORROBORATED_USE
                    ),
                }
            )
    return classified


def _rank(entry: Mapping[str, Any]) -> tuple:
    disposition = entry.get("disposition")
    try:
        primary = _DISPOSITION_PRECEDENCE.index(disposition)
    except ValueError:
        primary = len(_DISPOSITION_PRECEDENCE)
    return (
        primary,
        str(entry.get("agent_role") or ""),
        entry.get("submission_order", 0),
        entry.get("entry_index", 0),
    )


def _collapse(entries: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str], dict]:
    """One counted entry per ``(phase, claim_ref)``.

    A run can expose the same claim to several agents in one phase — every
    reviewer in a pool, every dev iteration — and each of them debriefs it. Left
    uncollapsed, three reviewers answering three claims would report nine
    dispositions against three injected claims, and the distribution would say
    more happened than did. The raw entries stay in the record; only the counting
    collapses.
    """
    best: dict[tuple[str, str], dict] = {}
    for entry in entries:
        key = (str(entry.get("phase") or ""), str(entry.get("claim_ref") or ""))
        current = best.get(key)
        if current is None or _rank(entry) < _rank(current):
            best[key] = dict(entry)
    return best


def _phase_blocks(
    exposure: Mapping[str, Mapping[str, dict]],
    submissions: Sequence[Mapping[str, Any]],
    counted: Mapping[tuple[str, str], dict],
) -> list[dict]:
    phases = sorted(set(exposure) | {_text(s.get("phase")).lower() for s in submissions})
    blocks: list[dict] = []
    for phase in phases:
        if not phase:
            continue
        claims = exposure.get(phase) or {}
        phase_submissions = [s for s in submissions if _text(s.get("phase")).lower() == phase]
        readable = [s for s in phase_submissions if isinstance(s.get("entries"), list)]
        if not claims:
            status = PHASE_NOTHING_TO_DEBRIEF
        elif readable:
            status = PHASE_DEBRIEFED
        else:
            status = PHASE_UNDEBRIEFED
        blocks.append(
            {
                "phase": phase,
                "status": status,
                "claims_injected": len(claims),
                "debrief_submissions": len(phase_submissions),
                "readable_submissions": len(readable),
                "malformed_submissions": [
                    {
                        "agent_role": s.get("agent_role") or "",
                        "phase_iteration": s.get("phase_iteration"),
                        "reason": s.get("malformed_reason"),
                        "malformed_entry_count": s.get("malformed_entry_count", 0),
                    }
                    for s in phase_submissions
                    if s.get("malformed_reason") or s.get("malformed_entry_count")
                ],
                "counted_entries": sum(1 for key in counted if key[0] == phase),
            }
        )
    return blocks


def _unaddressed(
    exposure: Mapping[str, Mapping[str, dict]],
    counted: Mapping[tuple[str, str], dict],
) -> list[dict]:
    """Injected claims no debrief in that phase named at all.

    Distinct from every disposition, including the ones that say the claim did
    not matter: "the agent said it was irrelevant" and "the agent never mentioned
    it" are different observations, and collapsing them would invent an answer.
    """
    missing: list[dict] = []
    for phase in sorted(exposure):
        for ref in sorted(exposure[phase]):
            if (phase, ref) in counted:
                continue
            missing.append({**exposure[phase][ref], "phase": phase})
    return missing


def _counts(
    phases: Sequence[Mapping[str, Any]],
    exposure: Mapping[str, Mapping[str, dict]],
    counted: Mapping[tuple[str, str], dict],
    unaddressed: Sequence[Mapping[str, Any]],
) -> dict:
    outcomes = [entry.get("outcome") for entry in counted.values()]
    dispositions = [
        entry.get("disposition")
        for entry in counted.values()
        if entry.get("outcome") == OUTCOME_NON_USE
    ]
    counts = {
        "phases_with_injected_knowledge": sum(
            1 for p in phases if p.get("status") != PHASE_NOTHING_TO_DEBRIEF
        ),
        "phases_debriefed": sum(1 for p in phases if p.get("status") == PHASE_DEBRIEFED),
        "phases_undebriefed": sum(1 for p in phases if p.get("status") == PHASE_UNDEBRIEFED),
        "phases_nothing_to_debrief": sum(
            1 for p in phases if p.get("status") == PHASE_NOTHING_TO_DEBRIEF
        ),
        "claims_injected": sum(len(claims) for claims in exposure.values()),
        OUTCOME_CORROBORATED_USE: outcomes.count(OUTCOME_CORROBORATED_USE),
        OUTCOME_UNCORROBORATED_USE: outcomes.count(OUTCOME_UNCORROBORATED_USE),
        "unmatched_citations": outcomes.count(OUTCOME_UNMATCHED),
        "unrecognised_dispositions": outcomes.count(OUTCOME_UNRECOGNISED),
        "unaddressed_claims": len(unaddressed),
    }
    for disposition in NON_USE_DISPOSITIONS:
        counts[disposition] = dispositions.count(disposition)
    return counts


# ── Artifact extraction ──────────────────────────────────────────────────────


def artifacts_from_record(record: Mapping[str, Any]) -> dict:
    """Collect the run's own artifacts a pointer may be resolved against.

    Read from the audit record rather than from the working tree: the record is
    what a later reader has, and a resolution that depended on the checkout would
    stop reproducing the moment the branch moved.
    """
    changed: list[str] = []
    changed_block = record.get("changed_files")
    if isinstance(changed_block, Mapping):
        for item in changed_block.get("files") or []:
            if isinstance(item, Mapping):
                path = _text(item.get("path"))
                if path:
                    changed.append(path)

    commits: list[str] = []
    for handoff in record.get("dev_handoffs") or []:
        if not isinstance(handoff, Mapping):
            continue
        payload = handoff.get("handoff")
        if not isinstance(payload, Mapping):
            continue
        for commit in payload.get("commits") or []:
            if isinstance(commit, Mapping):
                parts = (_text(commit.get("sha")), _text(commit.get("message")))
                label = " ".join(part for part in parts if part)
            else:
                label = _text(commit)
            if label:
                commits.append(label)

    phases = record.get("phases")
    plan_block = phases.get("plan") if isinstance(phases, Mapping) else None
    plan_text = ""
    plan_step_ids: list[str] = []
    if isinstance(plan_block, Mapping):
        structured = plan_block.get("plan_structured")
        if isinstance(structured, Mapping):
            parts = [_text(structured.get("approach"), limit=None)]
            for step in structured.get("steps") or []:
                if not isinstance(step, Mapping):
                    continue
                if step.get("id") is not None:
                    plan_step_ids.append(str(step["id"]))
                    parts.append(f"step {step['id']}")
                parts.append(_text(step.get("description"), limit=None))
                parts.append(_text(step.get("details"), limit=None))
            plan_text = "\n".join(part for part in parts if part)

    return {
        "changed_files": changed,
        "commits": commits,
        "plan_text": plan_text,
        "plan_step_ids": plan_step_ids,
    }


# ── Small helpers ────────────────────────────────────────────────────────────


def _text(value: Any, *, limit: int | None = 200) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text[:limit] if limit is not None else text


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in (_text(raw, limit=500) for raw in value) if item]
