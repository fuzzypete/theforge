"""Validation/execution complexity assessment for preflight sizing.

Preflight's legacy ``complexity_score`` collapses two independent axes of story
difficulty into one number: the *implementation* envelope (how much code changes)
and the *validation/execution* envelope (how much must be exercised and verified
to prove the work is done). The single score is dominated by code-change signals —
body length, AC bullet count, file-path mentions — so a story whose code change is
small but whose validation envelope is large (issue-1326: a real cost-bearing
``forge sprint`` exercising seven operator-visible surfaces, gating a release) is
sized as ordinary work and routed with ordinary timeouts and review budgets.

This module derives a *separate* validation-complexity score from body-local
structural signals so that envelope is sized on its own terms. Detection is
structural — counted surfaces, parsed dependency lists, verb-object recognition —
not single-token keyword matching. Every rule that fires records a ``rule_id`` +
``signal`` evidence pair (parallel to the RCA engine's evidence shape) so the
score is auditable and a lazy ``TOKEN in body`` rule can be rejected at review.

Pure Python, stdlib-only. The coordinator owns process decisions; this is a
deterministic sizing input, never an LLM call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Integer complexity scale bounds. Kept local (rather than imported from
# ``preflight``) so this module stays low-dependency and stdlib-only per the
# pure-data-module convention. These mirror preflight.COMPLEXITY_SCORE_MIN/MAX.
_SCORE_MIN = 1
_SCORE_MAX = 10

# A story with no validation signals starts at the floor so its projected
# complexity is driven entirely by the implementation envelope (projection is a
# max, so a baseline-1 validation score never lifts an ordinary story).
VALIDATION_BASELINE_SCORE = 1

# Documented projection rule recorded on the preflight output so consumers can
# tell a projected legacy score from a native one.
PROJECTION_MAX_IMPL_VALIDATION = "max_implementation_validation"

# Dimensions an evidence entry can belong to.
DIMENSION_IMPLEMENTATION = "implementation"
DIMENSION_VALIDATION = "validation"


@dataclass(frozen=True)
class ComplexityEvidence:
    """A single rule firing: which rule matched, what it matched, on which axis.

    ``rule_id`` follows the same descriptive-id pattern used by the RCA engine's
    evidence shape; ``signal`` is the concrete structural observation that fired
    the rule (a count, a parsed list, a matched verb-object span).
    """

    rule_id: str
    signal: str
    dimension: str

    def as_dict(self) -> dict[str, str]:
        return {"rule_id": self.rule_id, "signal": self.signal, "dimension": self.dimension}


@dataclass(frozen=True)
class ValidationAssessment:
    """Result of assessing the validation/execution envelope of a story body."""

    score: int
    evidence: list[ComplexityEvidence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _RuleHit:
    """Internal per-rule result before it is folded into an assessment."""

    rule_id: str
    signal: str
    contribution: int


# ── Structural helpers ────────────────────────────────────────────────────

# Operator-visible artifact extensions. A body reference ending in one of these
# is a concrete output file / rendered view the deliverable must produce or read.
_ARTIFACT_EXT = (
    "yaml",
    "yml",
    "jsonl",
    "json",
    "log",
    "md",
    "sqlite",
    "db",
    "toml",
    "csv",
    "txt",
)

_ARTIFACT_RE = re.compile(
    r"\b[\w./-]+\.(?:" + "|".join(_ARTIFACT_EXT) + r")\b",
    re.IGNORECASE,
)
_FLAG_RE = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]+")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
# A multi-word lowercase command invocation, e.g. `audit show`, `forge check-config`.
_COMMAND_SPAN_RE = re.compile(r"^[a-z][a-z0-9_-]*(?:\s+[a-z0-9:_-]+)+$")


def _acceptance_region(body: str) -> str:
    """Return the acceptance-criteria section of a markdown body, else the body.

    Rules that talk about "the AC enumerates ..." look here first so that
    surfaces mentioned only in background prose do not inflate the count. When no
    acceptance-criteria heading exists (file-based stories, terse specs), the
    whole body is used — structural detection still applies, just over more text.
    """
    if not body:
        return ""
    lines = body.splitlines()
    start: int | None = None
    heading_level = 0
    for idx, line in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m and "acceptance" in m.group(2).lower():
            start = idx + 1
            heading_level = len(m.group(1))
            break
    if start is None:
        return body
    collected: list[str] = []
    for line in lines[start:]:
        m = re.match(r"^(#{1,6})\s+", line)
        if m and len(m.group(1)) <= heading_level:
            break
        collected.append(line)
    region = "\n".join(collected).strip()
    return region or body


def _distinct_surfaces(text: str) -> list[str]:
    """Return the distinct operator-visible surfaces referenced in ``text``.

    A surface is a CLI command invocation, a command flag, or an output/artifact
    file — the things an operator must each run or inspect to verify the work.
    Detection is structural: parsed backtick command spans, flag tokens, and
    filename tokens carrying a known artifact extension. This is deliberately not
    ``KEYWORD in text`` — it counts *parsed* references and de-duplicates them.
    """
    surfaces: set[str] = set()
    for raw in _BACKTICK_RE.findall(text):
        span = raw.strip()
        if _COMMAND_SPAN_RE.match(span):
            surfaces.add(" ".join(span.split()).lower())
    for flag in _FLAG_RE.findall(text):
        surfaces.add(flag.lower())
    for m in _ARTIFACT_RE.finditer(text):
        surfaces.add(m.group(0).lower())
    return sorted(surfaces)


# ── Rules ──────────────────────────────────────────────────────────────────


def _rule_validation_surface_count(body: str, ac_region: str) -> _RuleHit | None:
    """AC enumerates multiple operator-visible surfaces that must each be verified."""
    surfaces = _distinct_surfaces(ac_region)
    count = len(surfaces)
    if count >= 5:
        contribution = 4
    elif count >= 3:
        contribution = 2
    else:
        return None
    shown = ", ".join(surfaces[:8])
    return _RuleHit(
        rule_id="validation_surface_count",
        signal=f"{count} operator-visible surfaces enumerated in acceptance criteria: {shown}",
        contribution=contribution,
    )


_COST_CMD_RE = re.compile(r"\bforge\s+(sprint|run|merge|diagnose|groom|triage)\b", re.IGNORECASE)
_DIRECTIVE_RE = re.compile(
    r"\b(run|runs|running|invoke|invokes|invoking|execute|executes|executing|"
    r"trigger|triggers|triggering|launch|launches|launching|perform|performs|"
    r"kick\s+off)\b",
    re.IGNORECASE,
)
_FIXTURE_RE = re.compile(
    r"\b(mock|mocked|mocks|fake|faked|fakes|stub|stubbed|stubs|fixture|fixtures|"
    r"simulate[ds]?|dry[-\s]run|as\s+a\s+test)\b",
    re.IGNORECASE,
)


def _rule_cost_bearing_dogfood(body: str, ac_region: str) -> _RuleHit | None:
    """Deliverable invokes a cost-bearing forge command within its own AC.

    Verb-object recognition: a directive verb (run / invoke / execute / trigger /
    launch) must precede a cost-bearing ``forge`` subcommand, and the surrounding
    window must not mark it as a test fixture (mock / fake / stub / simulate /
    dry-run). Merely mentioning ``forge sprint`` in prose does not fire — the
    directive verb and fixture exclusion are the structural discriminators.
    """
    for m in _COST_CMD_RE.finditer(body):
        preceding = body[max(0, m.start() - 45) : m.start()]
        window = body[max(0, m.start() - 45) : m.end() + 25]
        if _DIRECTIVE_RE.search(preceding) and not _FIXTURE_RE.search(window):
            command = " ".join(m.group(0).split())
            verb_match = list(_DIRECTIVE_RE.finditer(preceding))[-1]
            return _RuleHit(
                rule_id="cost_bearing_dogfood",
                signal=(
                    "acceptance criteria require a real cost-bearing invocation "
                    f"('{verb_match.group(0).strip()}' … '{command}'), not a test fixture"
                ),
                contribution=2,
            )
    return None


_VERSION_RE = re.compile(r"\bv?\d+\.\d+(?:\.\d+)?\b")
_GATE_RE = re.compile(
    r"\b(block|blocks|blocked|blocking|gate|gates|gated|gating)\b", re.IGNORECASE
)
_RELEASE_RE = re.compile(r"\brelease[sd]?\b", re.IGNORECASE)
_UNTIL_PASS_RE = re.compile(
    r"until\b[^.\n]*\b(pass|passes|passing|green|complete|completes|succeed|succeeds)\b",
    re.IGNORECASE,
)


def _rule_release_blocking_validation(body: str, ac_region: str) -> _RuleHit | None:
    """AC contains release-gate language ("block vN.M release", "blocked until …").

    Structural conjunction: a gating verb must sit near the word "release" and
    near either a version token (vN.M) or an "until … passes" completion clause.
    Requiring all three in proximity means a bare "release" or a bare "blocked"
    cannot fire this rule on its own.
    """
    for gate in _GATE_RE.finditer(body):
        window = body[max(0, gate.start() - 90) : gate.end() + 90]
        if not _RELEASE_RE.search(window):
            continue
        version = _VERSION_RE.search(window)
        until = _UNTIL_PASS_RE.search(window)
        if version or until:
            anchor = version.group(0) if version else "until-passes clause"
            return _RuleHit(
                rule_id="release_blocking_validation",
                signal=(
                    "acceptance criteria gate a release: "
                    f"'{gate.group(0)}' near 'release' and '{anchor}'"
                ),
                contribution=1,
            )
    return None


_DEP_CONTEXT_RE = re.compile(
    r"\b(depends?\s+on|dependenc(?:y|ies)|depends_on|blocked\s+by|"
    r"requires|builds?\s+on|prerequisite[s]?)\b",
    re.IGNORECASE,
)
_ISSUE_REF_RE = re.compile(r"#(\d+)")


def _rule_dependency_fan_in(body: str, ac_region: str) -> _RuleHit | None:
    """Body declares dependencies on multiple other stories in the same release.

    Structural parse: from each dependency-declaring phrase ("depends on",
    "dependencies", "blocked by", …) scan the following list region and collect
    issue references. Distinct count >= 2 fires. Issue references that are not in
    a dependency context ("see #123 for background") are never counted, so a lone
    mention cannot inflate the score.
    """
    found: set[int] = set()
    for ctx in _DEP_CONTEXT_RE.finditer(body):
        tail = body[ctx.end() : ctx.end() + 200]
        # Stop at a blank line — dependency lists do not span paragraph breaks.
        tail = tail.split("\n\n", 1)[0]
        for ref in _ISSUE_REF_RE.findall(tail):
            found.add(int(ref))
    if len(found) < 2:
        return None
    listed = ", ".join(f"#{n}" for n in sorted(found))
    return _RuleHit(
        rule_id="dependency_fan_in",
        signal=f"declared dependencies on {len(found)} other stories: {listed}",
        contribution=1,
    )


_RULES = (
    _rule_validation_surface_count,
    _rule_cost_bearing_dogfood,
    _rule_release_blocking_validation,
    _rule_dependency_fan_in,
)


def _build_warnings(hits: dict[str, _RuleHit]) -> list[str]:
    """Derive operator-facing warnings from the set of fired validation rules."""
    warnings: list[str] = []
    surface_hit = hits.get("validation_surface_count")
    if surface_hit is not None and surface_hit.contribution >= 4:
        count = surface_hit.signal.split(" ", 1)[0]
        warnings.append(
            f"validation envelope spans {count} operator-visible surfaces; "
            "consider splitting read-only checks from cost-bearing execution"
        )
    if "cost_bearing_dogfood" in hits and "release_blocking_validation" in hits:
        warnings.append("release-blocking dogfood validation")
    return warnings


def assess_validation_complexity(story_content: str | None) -> ValidationAssessment:
    """Score the validation/execution envelope of a story body from structural signals.

    Returns a :class:`ValidationAssessment` whose ``score`` is in
    ``[_SCORE_MIN, _SCORE_MAX]`` (baseline 1 for a story with no validation
    signals), ``evidence`` names each validation rule that fired, and
    ``warnings`` surfaces the envelope to operators.
    """
    body = story_content or ""
    ac_region = _acceptance_region(body)

    hits: dict[str, _RuleHit] = {}
    for rule in _RULES:
        hit = rule(body, ac_region)
        if hit is not None:
            hits[hit.rule_id] = hit

    score = VALIDATION_BASELINE_SCORE + sum(hit.contribution for hit in hits.values())
    score = max(_SCORE_MIN, min(_SCORE_MAX, score))

    evidence = [
        ComplexityEvidence(hit.rule_id, hit.signal, DIMENSION_VALIDATION) for hit in hits.values()
    ]
    return ValidationAssessment(
        score=score,
        evidence=evidence,
        warnings=_build_warnings(hits),
    )


def implementation_evidence(
    implementation_score: int | None,
    *,
    large_categories: tuple[str, ...] | list[str] = (),
    contract_change: bool = False,
    agent_failed: bool = False,
) -> list[ComplexityEvidence]:
    """Build the implementation-axis evidence accompanying the code-change score.

    The implementation score is the preflight model's code-change sizing (plus any
    deterministic coordinator overrides). This records where that number came from
    so the projected legacy score is auditable on both axes.
    """
    evidence: list[ComplexityEvidence]
    if agent_failed:
        evidence = [
            ComplexityEvidence(
                "implementation_agent_failure",
                "preflight agent failed; implementation envelope set to conservative maximum",
                DIMENSION_IMPLEMENTATION,
            )
        ]
    else:
        evidence = [
            ComplexityEvidence(
                "implementation_model_score",
                f"preflight model sized the code-change envelope at {implementation_score}",
                DIMENSION_IMPLEMENTATION,
            )
        ]
    if contract_change:
        evidence.append(
            ComplexityEvidence(
                "implementation_contract_change",
                "contract change forces at least medium implementation complexity "
                "(shared-interface blast radius)",
                DIMENSION_IMPLEMENTATION,
            )
        )
    for category in large_categories:
        evidence.append(
            ComplexityEvidence(
                "implementation_large_category",
                f"large-category signal: {category}",
                DIMENSION_IMPLEMENTATION,
            )
        )
    return evidence


def project_complexity_score(
    implementation_score: int | None,
    validation_score: int,
) -> tuple[int, str]:
    """Project the legacy ``complexity_score`` from the two native scores.

    First-cut projection rule is ``max(implementation, validation)`` so existing
    routing/timeout/review-budget consumers reading ``complexity_score`` receive
    the validation lift without reading the new fields. Returns
    ``(projected_score, projection_rule_id)``.
    """
    impl = implementation_score if implementation_score is not None else validation_score
    projected = max(impl, validation_score)
    projected = max(_SCORE_MIN, min(_SCORE_MAX, projected))
    return projected, PROJECTION_MAX_IMPL_VALIDATION
