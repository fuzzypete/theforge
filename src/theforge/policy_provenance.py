"""Provenance classification for the policy assertions intake cites when refusing work.

ADR-0006 clause 6 closed one direction of the prose-to-decision loop: LLM-generated
prose may propose, but it never routes. This module is the counterpart on the intake
side — generated prose must not *block*.

The problem it solves (#2137): a run-authored rationale ("reasoning effort is
intentionally NOT score-controlled") sitting in a documentation source of truth is
stored as exactly the same undifferentiated prose as an operator-ratified decision.
An intake check that cites it as a "standing decision" gives it veto power over
chartered work with nothing recording where the authority came from.

The mechanism is deliberately small and deterministic:

* A **citation** is what an intake agent names when it refuses — the assertion text,
  optionally an id, and where it read it. Agents may *claim* a provenance class; that
  claim is advisory evidence and never decides anything.
* A **registry** (``.forge/policy-assertions.yaml``, repo-local and durable) records
  which assertions an operator has ratified, with the ADR clause or recorded operator
  decision that ratifies them.
* **Resolution** is pure Python: a citation resolves to ``ratified`` only when it
  matches a non-retracted ratified registry entry. Everything else — a generated
  entry, a retracted entry, or an assertion the registry has never heard of — resolves
  to ``generated``. *Unmarked is generated*: absence of a record is never promoted.
* Only ``ratified`` carries blocking authority. Conflicts with generated or unmarked
  assertions become **retraction candidates**; assertions the registry does not know
  become **ratification candidates**, so a real operator decision that was merely
  unmarked is surfaced rather than silently demoted.

Stdlib + yaml only, and free of coordinator/config imports, so intake, the coordinator,
and the escalation advisor can all depend on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

# ── Provenance classes ────────────────────────────────────────────────────────

PROVENANCE_RATIFIED = "ratified"
PROVENANCE_GENERATED = "generated"

#: Every provenance class an assertion can resolve to. There is deliberately no
#: third "unknown" class: an assertion with no recorded provenance resolves to
#: ``generated``, because a class that means "we do not know" would sooner or
#: later be treated as good enough to block.
PROVENANCE_CLASSES: tuple[str, ...] = (PROVENANCE_RATIFIED, PROVENANCE_GENERATED)

#: How a citation was tied to a registry entry, recorded so an operator reading an
#: audit record can tell an id match from a fuzzy text match.
MATCH_ID = "id"
MATCH_TEXT_EXACT = "normalized_text"
MATCH_TEXT_SIMILAR = "normalized_text_similarity"
MATCH_UNMATCHED = "unmatched"

#: Repo-local durable registry filename, under ``.forge/`` alongside the other
#: durable records (capabilities, routing decisions).
POLICY_ASSERTIONS_FILENAME = "policy-assertions.yaml"


def policy_assertions_path(project_root: Path | str) -> Path:
    """Return the durable policy-assertion registry path for a project.

    Always resolved against the project root the caller supplies (in the
    coordinator, ``ForgeConfig.project_root``) rather than the process working
    directory, so a command run from a subdirectory reads the same registry.
    """
    return Path(project_root) / ".forge" / POLICY_ASSERTIONS_FILENAME


# ── Text normalization and matching ───────────────────────────────────────────

_NON_WORD_RE = re.compile(r"[^a-z0-9]+")

# Function words that carry no assertion content. "not"/"never"/"no" are
# deliberately NOT stopwords: an assertion and its negation are different
# assertions, and dropping the negation would let "X is score-controlled" match a
# ratified "X is not score-controlled".
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "then",
        "there",
        "these",
        "this",
        "to",
        "was",
        "were",
        "with",
    }
)

#: Jaccard overlap over content tokens above which a reworded citation is treated
#: as naming the same assertion. Set high on purpose: a false positive here grants
#: blocking authority to prose that was never ratified, which is the exact failure
#: this module exists to prevent. A near-miss below the threshold is not dropped —
#: it becomes a ratification candidate, so the operator sees it.
_SIMILARITY_THRESHOLD = 0.7


def normalize_assertion_text(text: str) -> str:
    """Return ``text`` reduced to lowercase words separated by single spaces."""
    return _NON_WORD_RE.sub(" ", (text or "").lower()).strip()


def _content_tokens(text: str) -> frozenset[str]:
    return frozenset(
        tok for tok in normalize_assertion_text(text).split() if tok and tok not in _STOPWORDS
    )


def _similarity(left: str, right: str) -> float:
    a = _content_tokens(left)
    b = _content_tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ── Registry entries ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PolicyAssertion:
    """One recorded policy assertion and the provenance the operator gave it."""

    assertion_id: str
    text: str
    provenance: str
    reference: str = ""
    run_id: str = ""
    ratified_at: str = ""
    retracted: bool = False
    retracted_reason: str = ""

    @property
    def carries_blocking_authority(self) -> bool:
        """Only a live ratified assertion may stop chartered work."""
        return self.provenance == PROVENANCE_RATIFIED and not self.retracted

    def to_dict(self) -> dict:
        return {
            "id": self.assertion_id,
            "text": self.text,
            "provenance": self.provenance,
            "reference": self.reference,
            "run_id": self.run_id,
            "ratified_at": self.ratified_at,
            "retracted": self.retracted,
            "retracted_reason": self.retracted_reason,
        }


def _normalize_provenance(raw: object) -> str:
    """Map a recorded provenance value onto a class; anything unknown is generated."""
    value = str(raw or "").strip().lower()
    return PROVENANCE_RATIFIED if value == PROVENANCE_RATIFIED else PROVENANCE_GENERATED


# ── Citations ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PolicyAssertionCitation:
    """A policy assertion an intake check named when refusing or escalating.

    ``claimed_provenance`` / ``claimed_reference`` are what the *agent* said about
    the assertion. They are evidence for the operator, never an input to whether the
    assertion may block — that comes from the registry alone.
    """

    text: str
    assertion_id: str = ""
    source: str = ""
    claimed_provenance: str = ""
    claimed_reference: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.assertion_id,
            "text": self.text,
            "source": self.source,
            "claimed_provenance": self.claimed_provenance,
            "claimed_reference": self.claimed_reference,
        }

    @classmethod
    def from_mapping(cls, raw: object) -> "PolicyAssertionCitation | None":
        """Build a citation from agent output, or None when it names no assertion."""
        if not isinstance(raw, dict):
            return None
        text = str(raw.get("text") or raw.get("assertion") or "").strip()
        assertion_id = str(raw.get("id") or raw.get("assertion_id") or "").strip()
        if not text and not assertion_id:
            return None
        return cls(
            text=text,
            assertion_id=assertion_id,
            source=str(raw.get("source") or "").strip(),
            claimed_provenance=str(raw.get("claimed_provenance") or raw.get("provenance") or "")
            .strip()
            .lower(),
            claimed_reference=str(
                raw.get("claimed_reference") or raw.get("reference") or ""
            ).strip(),
        )


def parse_citations(raw: object) -> list[PolicyAssertionCitation]:
    """Parse a ``policy_assertions_cited``-shaped value into citations.

    Tolerant of the shapes agents actually emit: a list of mappings, a list of bare
    strings, or a single mapping. Anything unreadable yields ``[]`` — an absent
    citation list means the refusal named no assertion, which is a fact the caller
    acts on rather than an error.
    """
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    citations: list[PolicyAssertionCitation] = []
    for entry in raw:
        if isinstance(entry, str):
            text = entry.strip()
            if text:
                citations.append(PolicyAssertionCitation(text=text))
            continue
        citation = PolicyAssertionCitation.from_mapping(entry)
        if citation is not None:
            citations.append(citation)
    return citations


# ── Resolution ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResolvedAssertion:
    """A citation with its adjudicated provenance class.

    ``provenance`` is the decided class — the only field consumers may gate on.
    ``match_basis`` records how the decision was reached so the operator can tell an
    id match from a reworded-text match from "the registry has never heard of this".
    """

    citation: PolicyAssertionCitation
    provenance: str
    match_basis: str
    reference: str = ""
    matched_id: str = ""
    run_id: str = ""
    retracted: bool = False

    @property
    def carries_blocking_authority(self) -> bool:
        return self.provenance == PROVENANCE_RATIFIED and not self.retracted

    @property
    def is_unmarked(self) -> bool:
        """True when no registry entry recorded this assertion at all."""
        return self.match_basis == MATCH_UNMATCHED

    def label(self) -> str:
        """Short human phrase naming the assertion and the authority behind it."""
        name = self.citation.text or self.citation.assertion_id or "(unnamed assertion)"
        if self.provenance == PROVENANCE_RATIFIED:
            where = self.reference or self.matched_id or "reference not recorded"
            return f'"{name}" — ratified ({where})'
        if self.is_unmarked:
            return f'"{name}" — generated (no ratification recorded)'
        if self.retracted:
            return f'"{name}" — generated (retracted: {self.retracted_detail()})'
        origin = f"authored by run {self.run_id}" if self.run_id else "no ratification recorded"
        return f'"{name}" — generated ({origin})'

    def retracted_detail(self) -> str:
        return self.reference or "no reason recorded"

    def to_dict(self) -> dict:
        return {
            **self.citation.to_dict(),
            "provenance": self.provenance,
            "match_basis": self.match_basis,
            "matched_id": self.matched_id,
            "reference": self.reference,
            "run_id": self.run_id,
            "retracted": self.retracted,
            "carries_blocking_authority": self.carries_blocking_authority,
            "label": self.label(),
        }


@dataclass(frozen=True)
class PolicyAssertionRegistry:
    """The repo-local record of which policy assertions an operator has ratified.

    An empty registry is the normal starting state and is not an error: with nothing
    ratified, every cited assertion resolves to ``generated`` and intake refuses to
    let prose block chartered work. ``errors`` carries any load problem so a
    malformed registry is visible in the audit trail rather than silently behaving
    like an empty one.
    """

    assertions: tuple[PolicyAssertion, ...] = ()
    path: str = ""
    loaded: bool = False
    errors: tuple[str, ...] = ()

    def by_id(self, assertion_id: str) -> PolicyAssertion | None:
        key = (assertion_id or "").strip().lower()
        if not key:
            return None
        for entry in self.assertions:
            if entry.assertion_id.strip().lower() == key:
                return entry
        return None

    def _match(self, citation: PolicyAssertionCitation) -> tuple[PolicyAssertion | None, str]:
        by_id = self.by_id(citation.assertion_id)
        if by_id is not None:
            return by_id, MATCH_ID

        cited = normalize_assertion_text(citation.text)
        if not cited:
            return None, MATCH_UNMATCHED
        for entry in self.assertions:
            if normalize_assertion_text(entry.text) == cited:
                return entry, MATCH_TEXT_EXACT

        # Reworded citation: an agent quoting an assertion rarely reproduces it
        # verbatim. Take the best overlap above the threshold, and prefer the
        # highest-scoring entry so a near-duplicate pair resolves deterministically.
        best: PolicyAssertion | None = None
        best_score = 0.0
        for entry in self.assertions:
            score = _similarity(citation.text, entry.text)
            if score > best_score:
                best, best_score = entry, score
        if best is not None and best_score >= _SIMILARITY_THRESHOLD:
            return best, MATCH_TEXT_SIMILAR
        return None, MATCH_UNMATCHED

    def resolve(self, citation: PolicyAssertionCitation) -> ResolvedAssertion:
        """Classify one citation. Unmatched and unmarked both resolve to generated."""
        entry, basis = self._match(citation)
        if entry is None:
            return ResolvedAssertion(
                citation=citation,
                provenance=PROVENANCE_GENERATED,
                match_basis=MATCH_UNMATCHED,
            )
        return ResolvedAssertion(
            citation=citation,
            provenance=entry.provenance,
            match_basis=basis,
            reference=entry.reference or entry.retracted_reason,
            matched_id=entry.assertion_id,
            run_id=entry.run_id,
            retracted=entry.retracted,
        )

    def resolve_all(self, citations: "list[PolicyAssertionCitation]") -> list[ResolvedAssertion]:
        return [self.resolve(c) for c in citations]


def _parse_registry_entry(raw: object, index: int, errors: list[str]) -> PolicyAssertion | None:
    if not isinstance(raw, dict):
        errors.append(f"assertions[{index}] is not a mapping")
        return None
    text = str(raw.get("text") or "").strip()
    assertion_id = str(raw.get("id") or raw.get("assertion_id") or "").strip()
    if not text and not assertion_id:
        errors.append(f"assertions[{index}] has neither id nor text")
        return None
    provenance = _normalize_provenance(raw.get("provenance"))
    reference = str(raw.get("reference") or "").strip()
    if provenance == PROVENANCE_RATIFIED and not reference:
        # Ratification is a claim about a durable operator decision. Without the
        # reference there is nothing for an operator to read, so the record cannot
        # be honoured as ratified — the same rule as an unmarked assertion.
        errors.append(
            f"assertions[{index}] ({assertion_id or text[:40]!r}) is marked ratified but "
            "records no reference — treated as generated"
        )
        provenance = PROVENANCE_GENERATED
    return PolicyAssertion(
        assertion_id=assertion_id,
        text=text,
        provenance=provenance,
        reference=reference,
        run_id=str(raw.get("run_id") or "").strip(),
        ratified_at=str(raw.get("ratified_at") or "").strip(),
        retracted=bool(raw.get("retracted", False)),
        retracted_reason=str(raw.get("retracted_reason") or "").strip(),
    )


def load_policy_assertions(project_root: Path | str) -> PolicyAssertionRegistry:
    """Load the repo-local registry, or return an empty one when absent/malformed.

    A missing file is the normal case and produces an empty registry with no errors.
    A malformed file produces an empty registry *with* errors: the load failure is
    recorded so it reaches the audit trail instead of masquerading as "nothing has
    been ratified yet".
    """
    path = policy_assertions_path(project_root)
    if not path.exists():
        return PolicyAssertionRegistry(path=str(path), loaded=False)

    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return PolicyAssertionRegistry(
            path=str(path),
            loaded=False,
            errors=(f"failed to read policy assertion registry: {exc}",),
        )

    errors: list[str] = []
    if parsed is None:
        return PolicyAssertionRegistry(path=str(path), loaded=True)
    if not isinstance(parsed, dict):
        return PolicyAssertionRegistry(
            path=str(path),
            loaded=False,
            errors=(f"policy assertion registry must be a mapping, got {type(parsed).__name__}",),
        )

    raw_assertions = parsed.get("assertions")
    if raw_assertions is None:
        raw_assertions = []
    if not isinstance(raw_assertions, list):
        return PolicyAssertionRegistry(
            path=str(path),
            loaded=False,
            errors=("policy assertion registry 'assertions' must be a list",),
        )

    entries: list[PolicyAssertion] = []
    for index, raw in enumerate(raw_assertions):
        entry = _parse_registry_entry(raw, index, errors)
        if entry is not None:
            entries.append(entry)

    return PolicyAssertionRegistry(
        assertions=tuple(entries),
        path=str(path),
        loaded=True,
        errors=tuple(errors),
    )


# ── Blocking adjudication ─────────────────────────────────────────────────────

#: Blocking bases an intake check may declare. Only ``policy_assertion`` is subject
#: to provenance adjudication — a missing credential, a direct specification
#: contradiction, or an absent external dependency is a hard blocker with no policy
#: claim in it, and downgrading those would break refusals that work today.
BLOCKING_BASES: tuple[str, ...] = (
    "none",
    "policy_assertion",
    "missing_credentials",
    "contradiction",
    "missing_dependency",
    "missing_fact",
    "other",
)

POLICY_BLOCKING_BASIS = "policy_assertion"

# Phrases in a BLOCKED reason that assert a *standing decision* — the claim that
# makes prose load-bearing. Used only when the agent emitted no structured
# citations, so a refusal that leans on unmarked doctrine cannot escape
# adjudication just by omitting the citation field.
#
# Each phrase is a decision claim, not a topic word. "architecture", "policy", and
# "design" are deliberately absent: a blocker can mention them while blocking for
# an unrelated hard reason, and matching them would repeat the false-positive
# trouble already documented for ``_AMBIGUITY_TOKENS`` in preflight_flow.
_DECISION_CLAIM_TOKENS: tuple[str, ...] = (
    # Explicit "someone decided this" phrasing.
    "deliberate architectural decision",
    "deliberate design decision",
    "deliberate decision",
    "intentional design decision",
    "intentional architectural decision",
    "standing decision",
    "established decision",
    "documented decision",
    "already-decided",
    "already decided",
    # The #1108 shape: the doctrine asserted as an intentional non-choice.
    "intentionally not",
    "intentionally does not",
    "intentionally is not",
    "deliberately not",
    # Appeals to a recorded decision the refusal does not cite.
    "architectural decision record",
    "by design",
)


def reason_asserts_standing_decision(reason: str) -> bool:
    """True when a BLOCKED reason claims a standing decision without citing one."""
    lowered = (reason or "").lower()
    return any(token in lowered for token in _DECISION_CLAIM_TOKENS)


@dataclass(frozen=True)
class ProvenanceAdjudication:
    """The deterministic outcome of weighing a BLOCKED verdict's policy citations.

    ``engaged`` is False for every blocker whose basis is not a policy assertion —
    those verdicts pass through untouched, which is what keeps missing-credential and
    direct-contradiction refusals working.
    """

    engaged: bool = False
    upheld: bool = True
    resolved: tuple[ResolvedAssertion, ...] = ()
    retraction_candidates: tuple[dict, ...] = ()
    ratification_candidates: tuple[dict, ...] = ()
    warnings: tuple[str, ...] = ()
    basis: str = "none"
    inferred_from_prose: bool = False

    @property
    def downgraded(self) -> bool:
        return self.engaged and not self.upheld

    @property
    def blocking_assertions(self) -> tuple[ResolvedAssertion, ...]:
        return tuple(r for r in self.resolved if r.carries_blocking_authority)

    def refusal_detail(self) -> str:
        """The assertion + provenance class an upheld refusal must name."""
        blocking = self.blocking_assertions
        if not blocking:
            return ""
        return "Blocked by ratified policy assertion(s): " + "; ".join(r.label() for r in blocking)

    def audit_fields(self) -> dict:
        return {
            "engaged": self.engaged,
            "basis": self.basis,
            "upheld": self.upheld,
            "downgraded": self.downgraded,
            "inferred_from_prose": self.inferred_from_prose,
            "assertions": [r.to_dict() for r in self.resolved],
            "retraction_candidates": [dict(c) for c in self.retraction_candidates],
            "ratification_candidates": [dict(c) for c in self.ratification_candidates],
            "warnings": list(self.warnings),
        }


_PROSE_ASSERTION_TEXT = "(assertion not cited — inferred from the refusal reason)"


def adjudicate_blocked_verdict(
    *,
    reason: str,
    blocking_basis: str,
    citations: "list[PolicyAssertionCitation]",
    registry: PolicyAssertionRegistry,
) -> ProvenanceAdjudication:
    """Decide whether a BLOCKED verdict founded on policy prose may stand.

    Engages only when the blocker is a policy/architectural assertion: either the
    check declared ``blocking_basis: policy_assertion``, or it cited assertions, or
    its reason claims a standing decision it did not cite. Every other blocker
    returns ``engaged=False`` and is left exactly as it was.

    When engaged, the verdict stands only if at least one cited assertion resolves to
    a live ratified registry entry. Otherwise it is downgraded, and the conflict is
    recorded as a retraction candidate (the assertion is contradicted by chartered
    work and carries no operator decision) plus, when the registry never recorded the
    assertion at all, a ratification candidate so the demotion is visible.
    """
    basis = (blocking_basis or "").strip().lower()
    inferred_from_prose = False
    if basis == POLICY_BLOCKING_BASIS or citations:
        basis = POLICY_BLOCKING_BASIS
    elif basis in ("", "none", "other") and reason_asserts_standing_decision(reason):
        basis = POLICY_BLOCKING_BASIS
        inferred_from_prose = True
    else:
        return ProvenanceAdjudication(engaged=False, basis=basis or "none")

    resolved = registry.resolve_all(citations)
    if not resolved and inferred_from_prose:
        # No structured citation, but the reason asserts a standing decision. Record
        # the claim itself as the assertion so the downgrade names something the
        # operator can act on rather than silently dropping the conflict.
        resolved = [
            ResolvedAssertion(
                citation=PolicyAssertionCitation(
                    text=_PROSE_ASSERTION_TEXT,
                    source="preflight reason",
                ),
                provenance=PROVENANCE_GENERATED,
                match_basis=MATCH_UNMATCHED,
            )
        ]

    blocking = [r for r in resolved if r.carries_blocking_authority]
    if blocking:
        return ProvenanceAdjudication(
            engaged=True,
            upheld=True,
            resolved=tuple(resolved),
            basis=basis,
            inferred_from_prose=inferred_from_prose,
            warnings=tuple(f"registry: {e}" for e in registry.errors),
        )

    retraction: list[dict] = []
    ratification: list[dict] = []
    warnings: list[str] = [f"registry: {e}" for e in registry.errors]
    for item in resolved:
        retraction.append(
            {
                "assertion": item.citation.text,
                "assertion_id": item.matched_id or item.citation.assertion_id,
                "source": item.citation.source,
                "provenance": item.provenance,
                "match_basis": item.match_basis,
                "reason": ("contradicted by chartered work and carries no operator decision"),
            }
        )
        if item.is_unmarked:
            registry_name = registry.path or POLICY_ASSERTIONS_FILENAME
            ratification.append(
                {
                    "assertion": item.citation.text,
                    "assertion_id": item.citation.assertion_id,
                    "source": item.citation.source,
                    "claimed_provenance": item.citation.claimed_provenance,
                    "claimed_reference": item.citation.claimed_reference,
                    "reason": (
                        "cited as a standing decision but the policy assertion registry "
                        "records no provenance; if this is a real operator decision, mark "
                        f"it ratified with a durable reference in {registry_name}"
                    ),
                }
            )
    warnings.append(
        "preflight BLOCKED downgraded to PROCEED: story conflicts only with "
        + ("generated rationale " if resolved else "unratified rationale ")
        + "("
        + "; ".join(r.label() for r in resolved)
        + ")"
    )
    return ProvenanceAdjudication(
        engaged=True,
        upheld=False,
        resolved=tuple(resolved),
        retraction_candidates=tuple(retraction),
        ratification_candidates=tuple(ratification),
        warnings=tuple(warnings),
        basis=basis,
        inferred_from_prose=inferred_from_prose,
    )


__all__ = [
    "BLOCKING_BASES",
    "MATCH_ID",
    "MATCH_TEXT_EXACT",
    "MATCH_TEXT_SIMILAR",
    "MATCH_UNMATCHED",
    "POLICY_ASSERTIONS_FILENAME",
    "POLICY_BLOCKING_BASIS",
    "PROVENANCE_CLASSES",
    "PROVENANCE_GENERATED",
    "PROVENANCE_RATIFIED",
    "PolicyAssertion",
    "PolicyAssertionCitation",
    "PolicyAssertionRegistry",
    "ProvenanceAdjudication",
    "ResolvedAssertion",
    "adjudicate_blocked_verdict",
    "load_policy_assertions",
    "normalize_assertion_text",
    "parse_citations",
    "policy_assertions_path",
    "reason_asserts_standing_decision",
]
