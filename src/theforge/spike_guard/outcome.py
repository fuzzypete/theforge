"""The recorded outcome of a spike, and what makes it a legal exit.

A spike is chartered to answer a question. Its two legal exits are a recorded
decision *not* to proceed, carrying its reasoning, and a follow-on work item
that exists in the pipeline. Closing with neither leaves the answer in operator
memory, where six weeks later it is indistinguishable from a spike that was
never chartered (#2600).

This module states that rule as data and pure functions:

- the marker an outcome is recorded with, and how it parses;
- what each outcome kind must carry to be complete;
- what a follow-on issue must look like to count as "in the pipeline";
- where a *conditional* answer's trigger condition must live.

Nothing here talks to GitHub. The caller supplies the facts — the spike's body
and comments, and the follow-on issue's state, labels and body — and gets back
a decision with an operator-facing reason. :mod:`theforge.spike_guard.guard`
is the layer that fetches those facts.

Stdlib only, no imports from the rest of the package (project convention 4).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum

#: The label that makes an issue a spike. Nothing else marks one.
SPIKE_LABEL = "spike"

#: The versioned marker an outcome is recorded under. Written as an HTML
#: comment so it is invisible in rendered Markdown but exact to parse — the
#: record is machine-checked, so it must not depend on prose wording.
OUTCOME_MARKER = "forge-spike-outcome-v1"

#: The heading a conditional answer's trigger condition lives under, on the
#: *follow-on* issue. A condition written on the spike is prose in a closed
#: document; on the follow-on it is carried by an artifact that stays visible.
TRIGGER_CONDITION_HEADING = "Spike trigger condition"

#: The fields that heading must carry, and what each one answers.
TRIGGER_CONDITION_FIELDS: tuple[tuple[str, str], ...] = (
    ("What must be true", "the state of the world that would make the answer yes"),
    ("How to know", "how an operator or the system would observe it had become true"),
)

#: Labels that mark an issue as not yet in the pipeline; a follow-on carrying
#: one of these is a draft, not a work item.
NOT_PIPELINE_LABELS: frozenset[str] = frozenset({"todo:draft"})

_OUTCOME_BLOCK_RE = re.compile(
    rf"<!--\s*{re.escape(OUTCOME_MARKER)}\b(?P<body>.*?)-->",
    re.DOTALL | re.IGNORECASE,
)
_FIELD_RE = re.compile(r"^\s*(?P<key>[A-Za-z][A-Za-z _-]*)\s*:\s*(?P<value>.*?)\s*$")
_ISSUE_REF_RE = re.compile(r"#?(\d+)")


class SpikeOutcomeKind(str, Enum):
    """The three answers a spike may record.

    ``DO_NOT_PROCEED`` is a complete and successful outcome, not an absence —
    the question is closed rather than dropped, so it is recorded as a decision
    with its reasoning.
    """

    DO_NOT_PROCEED = "do_not_proceed"
    FOLLOW_UP = "follow_up"
    CONDITIONAL_FOLLOW_UP = "conditional_follow_up"


#: Kinds whose legality depends on a follow-on issue existing in the pipeline.
_FOLLOW_UP_KINDS = frozenset({SpikeOutcomeKind.FOLLOW_UP, SpikeOutcomeKind.CONDITIONAL_FOLLOW_UP})


@dataclass(frozen=True)
class SpikeOutcome:
    """One parsed outcome record.

    ``malformed`` carries the reason a marker was found but could not be read
    as an outcome, so a typo surfaces as a specific refusal rather than as
    "no outcome recorded".
    """

    kind: SpikeOutcomeKind | None
    reason: str = ""
    follow_up: int | None = None
    malformed: str = ""

    @property
    def needs_follow_up(self) -> bool:
        return self.kind in _FOLLOW_UP_KINDS

    @property
    def needs_trigger_condition(self) -> bool:
        return self.kind is SpikeOutcomeKind.CONDITIONAL_FOLLOW_UP


@dataclass(frozen=True)
class IssueFacts:
    """The GitHub facts a closure decision is made from."""

    number: int
    state: str = "OPEN"
    labels: tuple[str, ...] = ()
    body: str = ""

    @property
    def is_open(self) -> bool:
        return self.state.strip().upper() == "OPEN"

    @property
    def normalized_labels(self) -> frozenset[str]:
        return frozenset(label.strip().lower() for label in self.labels if label.strip())

    @property
    def is_spike(self) -> bool:
        return SPIKE_LABEL in self.normalized_labels


@dataclass(frozen=True)
class ClosureDecision:
    """Whether a close may proceed, and the reason either way."""

    allowed: bool
    reason: str

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.allowed


def _skeleton(kind: str, *fields: str) -> str:
    lines = [f"<!-- {OUTCOME_MARKER}", f"outcome: {kind}", *fields, "-->"]
    return "\n".join(lines)


#: The operator-facing remediation quoted by every refusal. Generated from the
#: same constants the parser reads, so the instructions cannot drift from what
#: is accepted.
REMEDIATION = (
    "A spike closes on one of two recorded outcomes. Record one by adding this "
    "marker to the spike's body or a comment on it:\n\n"
    f"{_skeleton('do_not_proceed', 'reason: <why the answer is no>')}\n\n"
    "or, when there is follow-on work:\n\n"
    f"{_skeleton('follow_up', 'follow-up: #<issue>')}\n\n"
    "or, when the answer is conditional:\n\n"
    f"{_skeleton('conditional_follow_up', 'follow-up: #<issue>')}\n\n"
    f"A follow-on issue must be open, carry exactly one type label, and not be "
    f"a {'/'.join(sorted(NOT_PIPELINE_LABELS))} draft. A conditional outcome "
    f"additionally requires a '## {TRIGGER_CONDITION_HEADING}' section on the "
    "follow-on issue carrying "
    + " and ".join(f"'**{label}:**'" for label, _ in TRIGGER_CONDITION_FIELDS)
    + "."
)


def _parse_fields(block: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in block.splitlines():
        match = _FIELD_RE.match(line)
        if match is None:
            continue
        key = match.group("key").strip().lower().replace("-", "_").replace(" ", "_")
        fields[key] = match.group("value").strip()
    return fields


def _parse_block(block: str) -> SpikeOutcome:
    fields = _parse_fields(block)
    raw_kind = fields.get("outcome", "")
    normalized = raw_kind.strip().lower().replace("-", "_").replace(" ", "_")
    try:
        kind = SpikeOutcomeKind(normalized)
    except ValueError:
        legal = ", ".join(k.value for k in SpikeOutcomeKind)
        detail = f"outcome: {raw_kind!r}" if raw_kind else "no 'outcome:' field"
        return SpikeOutcome(
            kind=None,
            malformed=(
                f"the recorded outcome names no legal exit ({detail}); expected one of: {legal}"
            ),
        )

    reason = fields.get("reason", "")
    follow_up_raw = fields.get("follow_up", "")
    follow_up: int | None = None
    if follow_up_raw:
        ref = _ISSUE_REF_RE.search(follow_up_raw)
        if ref is not None:
            follow_up = int(ref.group(1))

    if kind is SpikeOutcomeKind.DO_NOT_PROCEED and not reason:
        return SpikeOutcome(
            kind=kind,
            malformed=(
                "outcome 'do_not_proceed' records a decision, so it must carry a "
                "non-empty 'reason:' naming why the answer is no"
            ),
        )
    if kind in _FOLLOW_UP_KINDS and follow_up is None:
        return SpikeOutcome(
            kind=kind,
            malformed=(
                f"outcome {kind.value!r} must carry a 'follow-up: #<issue>' field "
                "naming the follow-on work item"
            ),
        )
    return SpikeOutcome(kind=kind, reason=reason, follow_up=follow_up)


def parse_spike_outcomes(text: str) -> tuple[SpikeOutcome, ...]:
    """Return every outcome marker found in ``text``, in document order."""
    return tuple(_parse_block(m.group("body")) for m in _OUTCOME_BLOCK_RE.finditer(text or ""))


def find_spike_outcome(texts: Iterable[str]) -> SpikeOutcome | None:
    """Return the outcome recorded across ``texts``, or ``None`` if there is none.

    A well-formed record wins over a malformed one wherever it appears, so a
    corrected marker in a later comment supersedes an earlier typo. When only
    malformed markers exist, the first is returned so its specific complaint is
    what the operator sees.
    """
    malformed: SpikeOutcome | None = None
    for text in texts:
        for outcome in parse_spike_outcomes(text):
            if not outcome.malformed:
                return outcome
            if malformed is None:
                malformed = outcome
    return malformed


def _section_lines(body: str, heading: str) -> list[str] | None:
    """Return the lines under ``heading``, or ``None`` when it is absent."""
    wanted = heading.strip().lower()
    collected: list[str] | None = None
    level = 0
    for line in (body or "").splitlines():
        match = re.match(r"^(#{1,6})\s+(?P<text>.*?)\s*$", line)
        if match is not None:
            text = re.sub(r"[\s:.\-—]+$", "", match.group("text").strip()).lower()
            if text == wanted:
                collected = []
                level = len(match.group(1))
                continue
            if collected is not None and len(match.group(1)) <= level:
                break
            if collected is None:
                continue
        if collected is not None:
            collected.append(line)
    return collected


def missing_trigger_condition_fields(body: str) -> tuple[str, ...]:
    """Return the trigger-condition fields ``body`` fails to carry.

    A missing section reports every field as missing, so the refusal names what
    to write rather than merely that a heading is absent.
    """
    lines = _section_lines(body, TRIGGER_CONDITION_HEADING)
    if lines is None:
        return tuple(label for label, _ in TRIGGER_CONDITION_FIELDS)
    missing: list[str] = []
    for label, _ in TRIGGER_CONDITION_FIELDS:
        pattern = re.compile(
            rf"{re.escape(label)}\s*:?\s*\**\s*(?P<value>.*?)\s*$",
            re.IGNORECASE,
        )
        satisfied = False
        for line in lines:
            probe = line.replace("*", "").replace("_", "").strip().lstrip("-").strip()
            match = pattern.match(probe)
            if match is not None and match.group("value").strip():
                satisfied = True
                break
        if not satisfied:
            missing.append(label)
    return tuple(missing)


def validate_follow_up(follow_up: IssueFacts | None, reference: int | None) -> str:
    """Return why ``follow_up`` is not a pipeline work item, or ``""`` if it is."""
    if follow_up is None:
        return (
            f"the recorded outcome names follow-on issue #{reference}, which could not "
            "be read — a follow-on must exist in the pipeline"
        )
    if not follow_up.is_open:
        return (
            f"follow-on issue #{follow_up.number} is {follow_up.state.upper()}; a spike's "
            "follow-on must be open work, not an already-resolved reference"
        )
    labels = follow_up.normalized_labels
    draft = sorted(labels & NOT_PIPELINE_LABELS)
    if draft:
        return (
            f"follow-on issue #{follow_up.number} carries {draft[0]!r}, so it is a draft "
            "rather than a work item visible where other work is visible"
        )
    # Imported lazily so this module keeps its stdlib-only import surface at
    # load time while still deriving the vocabulary from the one specification
    # that owns it (ADR-0009) rather than restating it.
    from theforge.shape_check.issue_spec import RECOGNIZED_TYPE_LABELS

    types = sorted(labels & RECOGNIZED_TYPE_LABELS)
    if len(types) != 1:
        expected = ", ".join(sorted(RECOGNIZED_TYPE_LABELS))
        found = ", ".join(types) if types else "none"
        return (
            f"follow-on issue #{follow_up.number} declares {found} type label(s); exactly "
            f"one of {expected} is required for it to be dispatchable work"
        )
    return ""


def evaluate_spike_closure(
    spike: IssueFacts,
    *,
    texts: Sequence[str] = (),
    follow_up: IssueFacts | None = None,
) -> ClosureDecision:
    """Decide whether spike ``spike`` may close, given the recorded facts.

    ``texts`` are the places an outcome may have been recorded — the spike's
    body, its comments, and any comment the caller is about to post with the
    close. ``follow_up`` is the referenced issue's facts when one was named;
    :func:`required_follow_up` says which number to fetch.
    """
    if not spike.is_spike:
        return ClosureDecision(True, f"#{spike.number} is not a spike; closure is unchanged")

    outcome = find_spike_outcome(texts)
    if outcome is None:
        return ClosureDecision(
            False,
            f"spike #{spike.number} records no outcome, so closing it would leave its "
            f"result in operator memory only.\n\n{REMEDIATION}",
        )
    if outcome.malformed:
        return ClosureDecision(
            False,
            f"spike #{spike.number} records a malformed outcome: {outcome.malformed}.\n\n"
            f"{REMEDIATION}",
        )

    if outcome.kind is SpikeOutcomeKind.DO_NOT_PROCEED:
        return ClosureDecision(
            True,
            f"spike #{spike.number} records a do-not-proceed decision: {outcome.reason}",
        )

    problem = validate_follow_up(follow_up, outcome.follow_up)
    if problem:
        return ClosureDecision(False, f"spike #{spike.number}: {problem}.\n\n{REMEDIATION}")
    assert follow_up is not None  # validate_follow_up rejects None

    if outcome.needs_trigger_condition:
        missing = missing_trigger_condition_fields(follow_up.body)
        if missing:
            fields = ", ".join(f"'**{label}:**'" for label in missing)
            return ClosureDecision(
                False,
                f"spike #{spike.number} records a conditional outcome, but follow-on issue "
                f"#{follow_up.number} does not carry the condition: its "
                f"'## {TRIGGER_CONDITION_HEADING}' section is missing {fields}. The "
                "condition must live on the follow-on artifact, not in the closed "
                f"spike's prose.\n\n{REMEDIATION}",
            )

    return ClosureDecision(
        True,
        f"spike #{spike.number} records follow-on work in #{follow_up.number}",
    )


def required_follow_up(texts: Sequence[str]) -> int | None:
    """Return the follow-on issue number the recorded outcome names, if any."""
    outcome = find_spike_outcome(texts)
    if outcome is None or outcome.malformed or not outcome.needs_follow_up:
        return None
    return outcome.follow_up
