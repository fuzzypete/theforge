"""Prompt construction and output parsing for the diagnose flow.

The diagnose prompt structure is intentionally distinct from plan prompts:
plan describes how to *act on* a known cause; diagnose describes how to
*find* the cause.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import yaml

from theforge.diagnose_types import (
    DiagnosisArtifact,
    Hypothesis,
    InspectedFile,
    PremiseAnchor,
    RelatedFinding,
    ScopeCoverageLocation,
    SymptomScopeCoverage,
)
from theforge.shape_check.parsing import BUG_EXPECTATION_HEADING, extract_section

_DIAGNOSE_PROMPT_TEMPLATE = """\
You are an investigative diagnosis agent.  A symptom bug has been reported but
the cause is unknown.  Your job is NOT to fix anything.  Your job is to find
out **what is actually happening** so a separate fix flow can act on a real
cause.

Mode: {mode}

== ISSUE #{issue_number}: {title} ==

{body}
{starting_evidence}
{environment}

== INSTRUCTIONS ==

1. Reproduce or examine evidence for the reported symptom.  Use the available
   tools (Read, Bash, Glob, Grep) to inspect the codebase, run history, logs,
   and tests.  Do not modify any files.
2. Form a small set of plausible hypotheses for the cause.
3. For each hypothesis, gather evidence that either rules it out or confirms
   it.  Spawn focused sub-investigations as needed (e.g. read a specific
   audit YAML, grep for a code path, run a targeted command).
4. Settle on a single confirmed cause OR — if you cannot confirm one within
   your budget — report the strongest remaining hypothesis with
   ``confirmed_cause: ""`` and explain in ``notes`` what evidence is missing.
   **Do NOT guess** — partial honest output is more valuable than a confident
   wrong diagnosis.
5. Before confirming a cause, verify the bug's premise still exists in the
   current baseline.  A bug's premise can be silently deleted by an intervening
   commit — the code it describes may already be gone.  List the concrete,
   falsifiable premise anchors (file + a literal code substring you actually
   saw at HEAD) in ``premise_anchors``.  These are checked mechanically after
   you finish: if a cited file or pattern is absent from the baseline, the run
   is reported as "already resolved" rather than landed as a live diagnosis.
   Do NOT emit a confirmed cause for code that no longer exists.
6. **Scope the confirmed cause to THIS issue's stated symptom — nothing more,
   and nothing less.** The diagnosis boundary must match the issue boundary.
   While investigating you may notice other real defects in nearby code that
   are NOT the cause of this issue's symptom (a different bug, another issue's
   domain, a latent problem you happened to see).  Do NOT fold those into
   ``confirmed_cause`` or ``affected_code_path`` — a downstream dev implements
   the confirmed cause verbatim, so anything you put there becomes part of THIS
   issue's fix and over-scopes it.  Instead, record each such adjacent problem
   as a separate entry in ``related_findings``, naming the owning/related issue
   if you know it (e.g. ``"#1649"``).  Ask of every claim you put in
   ``confirmed_cause``: "is this the cause of the *stated* symptom?"  If it is
   a neighboring problem rather than the cause of what the issue reports, it
   belongs in ``related_findings``, not in the fix scope.
7. If the issue states the symptom categorically (for example "every surface",
   "any story", "regardless of X"), account for the stated scope before you
   confirm a cause.  Check structurally analogous sibling locations: the same
   construct, the same omission or behavior, at a sibling site.  Either cover
   those locations in the affected code path or record which ones you examined
   and excluded in ``symptom_scope_coverage``.  A nearby location with a
   different construct or a distinct defect is **adjacent-but-different** and
   stays a ``related_findings`` entry instead.  Do NOT broaden a single
   concrete-instance symptom into a categorical one; leave
   ``symptom_scope_coverage.symptom_is_categorical`` false for one-off
   symptoms.

== OUTPUT FORMAT ==

{output_format}
"""

# The requested serialization contract, factored out of the investigation prompt
# so the parse-retry prompt (:func:`build_diagnose_reformat_prompt`) restates the
# *same* contract verbatim. A retry that quoted a drifted copy of the schema
# would be asking for a different document than the one the parser accepts.
_DIAGNOSE_OUTPUT_FORMAT = """\
Emit a single fenced YAML block.  Required keys:

```yaml
observed_symptom: |
  Plain-language description of what is going wrong.
reproduction_or_evidence: |
  Exact reproduction steps OR a citation of evidence already on disk
  (audit file, log line, failing test).
hypotheses:
  - statement: "Hypothesis A — what could be causing it"
    status: ruled_out      # or: confirmed | inconclusive
    evidence: "What you observed that ruled this out / confirmed it"
  - statement: "Hypothesis B"
    status: confirmed
    evidence: "..."
confirmed_cause: |
  The single root cause supported by the evidence.  Empty string if
  no cause was confirmed.
affected_code_path: |
  File path(s) and function/line locations involved in the bug.
fix_success_criterion: |
  An observable, verifiable criterion the fix must satisfy.  Phrased so
  a reviewer can check whether the symptom is actually gone.
notes: |
  Optional — any caveats, unresolved threads, or partial-investigation
  context the operator should see.
inspected_files:
  # REQUIRED. List every file path you read or grepped during the
  # investigation, one repo-relative path per entry. This anchors the
  # diagnosis to a baseline so a later `forge groom` can detect when the
  # diagnosis has gone stale because one of these files was changed by
  # an intervening commit.
  - "<repo-relative path to a file you inspected>"
  - "<repo-relative path to another file you inspected>"
premise_anchors:
  # REQUIRED when confirmed_cause is non-empty. Each entry names a file and a
  # literal code substring the bug's premise depends on and that you verified
  # is present at HEAD. These are checked mechanically: if a file or pattern
  # here is absent from the baseline, the diagnosis is reported as "already
  # resolved" (premise removed) instead of landed as a live confirmed cause.
  - file: "<repo-relative path the bug lives in>"
    pattern: "<exact code substring that must exist for the bug to be live>"
related_findings:
  # OPTIONAL. Adjacent, real defects you noticed in nearby code that are NOT
  # the cause of THIS issue's stated symptom. Recording them here keeps them
  # OUT of the fix scope (confirmed_cause) so the dev does not build them as
  # part of this issue. Name the owning/related issue in `related` when known.
  # Omit or leave empty if you noticed no separate adjacent problems.
  - summary: "<one-line description of a separate adjacent defect>"
    related: "<owning/related issue ref, e.g. '#1649', or empty>"
symptom_scope_coverage:
  # REQUIRED when the issue's stated symptom is categorical ("every", "any",
  # "regardless", etc.). Keep the default non-categorical record for symptoms
  # scoped to one concrete instance. Do NOT invent a broader scope than the
  # issue states.
  symptom_is_categorical: false
  stated_scope: ""
  examined_locations: []
  # Example categorical record:
  # symptom_is_categorical: true
  # stated_scope: "Every sibling surface that renders this symptom"
  # examined_locations:
  #   - location: "path/to/sibling_surface_a.ext:render_output"
  #     status: covered
  #     rationale: "Same renderer construct and same omitted field as the confirmed cause."
  #   - location: "path/to/sibling_surface_b.ext:serialize_output"
  #     status: excluded
  #     rationale: "Sibling surface checked; it uses a different serializer
  #       and already carries the field."
```
"""

_DIAGNOSE_REFORMAT_PROMPT_TEMPLATE = """\
Your previous diagnosis output could not be parsed as YAML:

{parse_error}

Your INVESTIGATION is finished and its CONTENT is correct — the confirmed cause,
the hypotheses, the evidence, and the fix-success criterion are what you meant to
say.  ONLY the ENCODING (YAML serialization) is broken.  Re-emit the SAME
diagnosis with the syntax error fixed.

Preserve your previous output verbatim:
  - Keep the SAME confirmed_cause, word for word.  Do NOT re-scope it.
  - Keep EVERY hypothesis, with the same statement, status, and evidence.
  - Keep EVERY inspected_files entry, premise_anchors entry,
    related_findings entry, and symptom_scope_coverage entry.
  - Keep the same notes.

Dropping or altering a value to make the parse error go away is a WRONG
response.  If a scalar failed to parse, fix its QUOTING/ESCAPING — the usual
culprit is an unterminated quoted string or a colon inside an unquoted value.
Prefer a block scalar (``key: |``) or single quotes for any multi-line or
punctuation-heavy value.  Do NOT delete the value, do NOT empty an optional
field such as ``related_findings`` to sidestep the error, and do NOT weaken a
confirmed cause into an inconclusive one.

Do NOT investigate anything further.  Do NOT read files, run commands, or revise
your conclusions.  This turn is a serialization repair and nothing else.

{output_format}

Your previous output (fix its YAML syntax, keep its content):

{original_output}
"""


def _forge_relpath(parts: tuple[str, ...]) -> str:
    """Render a repo-relative path from a path-constant tuple.

    Uses forward slashes on every platform so the briefing reads identically
    regardless of the host OS.
    """
    return "/".join(parts)


def build_environment_briefing() -> str:
    """Return the ``== ENVIRONMENT ==`` orientation section for the prompt.

    The audit paths and the landing-field semantics are derived from the modules
    that own them — :data:`theforge.coordinator.audit_substrate.AUDIT_PATH_REGISTRY`
    for the canonical audit paths and
    :data:`theforge.coordinator.landing_record.LANDING_OUTCOME_BY_PATH` for the
    landing outcomes — rather than being hardcoded here. The audit-path block is
    rendered by *iterating* the registry, so adding a new audit path (or a new
    landing ``landing_path`` -> outcome mapping) in code surfaces in the briefing
    automatically, with no hand-edit to this template (issue #1425 AC2).

    Imports are function-local so the low-dependency prompt module does not pull
    the coordinator's audit stack in at import time.
    """
    from theforge.coordinator import audit_substrate  # noqa: PLC0415
    from theforge.coordinator.landing_record import (  # noqa: PLC0415
        LANDING_OUTCOME_BY_PATH,
    )

    audits = _forge_relpath(audit_substrate.AUDITS_RELPATH)

    audit_path_lines = "\n".join(
        f"- {info.label}: {info.display}" for info in audit_substrate.AUDIT_PATH_REGISTRY
    )
    landing_lines = "\n".join(
        f"    - landing_path '{path}' -> landing.outcome '{outcome}'"
        for path, outcome in LANDING_OUTCOME_BY_PATH.items()
    )

    return f"""\
== ENVIRONMENT ==

This is TheForge — a Python multi-agent SDLC orchestrator. You have one shot and
a fixed budget, so start from the project's known layout instead of
rediscovering it. All orchestration state lives under `.forge/`.

Audit trail (where run and sprint history live):
{audit_path_lines}
- Sprint run log (full agent transcript): .forge/logs/<sprint-name>/run-<run-id>.log
- Per-story audit: .forge/logs/<sprint-name>/<slug>/audit.yaml
- Sprint state: .forge/sprints/<sprint-id>/state.yaml

Audit field semantics that commonly mislead:
- `merge: true` only means `res.merge.get("merged")` — it does NOT distinguish a
  fresh PR that shipped this worktree's commits from an already-merged guard
  that discarded them. Read the structured `landing` record instead;
  `landing.fresh_pr_created` is the trustworthy "did we ship code" signal.
- `landing.outcome` is derived from the internal landing path:
{landing_lines}
- `landing_status` (per-run): 'pending_integration' | 'landed' | 'failed' | null.
- `outcome_code`: the run's error_type when set, else the lowercased final
  phase (e.g. 'done' | 'failed' | 'escalate' | 'skipped').

Queries that most often crack landing/merge investigations fast:
- gh pr list --head <branch> --state all   (did this branch's PR ever ship?)
- gh pr view <PR> --json state,mergedAt,mergeCommit,title
- gh issue view <N> --comments             (operator notes not in the body)
- grep -rn <keyword> {audits}              (prior runs touching this story)

Locating the code under investigation:
- The failing code lives in this project's own source tree — discover it with
  Glob/Grep, not by assuming a fixed layout.
- Let the audit trail point you: an audit record's `branch`, `slug`, and
  `affected_code_path` fields, plus `git log --oneline <branch>` and
  `git show <sha>`, take you straight to the commits a run actually touched."""


def build_diagnose_prompt(
    *,
    issue_number: int,
    title: str,
    body: str,
    mode: str = "autonomous",
    starting_evidence: str = "",
) -> str:
    """Return the diagnose-agent prompt for one issue.

    ``mode`` is "autonomous" or "interactive"; it is conveyed to the agent so
    it can adjust verbosity (interactive runs may benefit from richer notes
    explaining intermediate steps).

    ``starting_evidence`` is an optional, already-rendered STARTING EVIDENCE
    block (see :func:`theforge.coordinator.diagnose_evidence.build_starting_evidence`)
    pre-loaded from references the operator cited in the issue body. It is
    embedded verbatim right after the issue block so the agent does not spend
    budget rediscovering pointers the operator already provided. When empty,
    the prompt is byte-for-byte what it was before auto-injection existed
    (best-effort: no references → no change).

    The prompt embeds an environment briefing (see
    :func:`build_environment_briefing`) so the agent starts from TheForge's
    known audit/log layout instead of burning its budget rediscovering it.
    """
    evidence_block = f"\n{starting_evidence}\n" if starting_evidence else ""
    return _DIAGNOSE_PROMPT_TEMPLATE.format(
        issue_number=issue_number,
        title=title or f"Issue #{issue_number}",
        body=body or "(issue body is empty)",
        mode=mode,
        starting_evidence=evidence_block,
        environment=build_environment_briefing(),
        output_format=_DIAGNOSE_OUTPUT_FORMAT,
    )


def build_diagnose_reformat_prompt(*, original_output: str, parse_error: str) -> str:
    """Return a reformat-only retry prompt for unparseable diagnose output.

    A completed investigation is the expensive part of a diagnose run; a YAML
    syntax slip in its serialization is the cheap part.  This prompt asks the
    agent for *serialization repair only*: it hands back the agent's verbatim
    prior emission plus the parser's error and restates the same output contract,
    so the reachable response is "the same diagnosis, correctly encoded".

    It deliberately does NOT ask for renewed investigation (which would spend the
    budget again for a result already produced) and does NOT relax the schema
    (dropping the field that failed to encode is named as a wrong answer).  The
    parser stays strict on the retry output — a retry earns its way in by
    producing conforming YAML, never by having the contract widened for it.
    """
    return _DIAGNOSE_REFORMAT_PROMPT_TEMPLATE.format(
        parse_error=parse_error or "(no parser detail available)",
        output_format=_DIAGNOSE_OUTPUT_FORMAT,
        original_output=original_output or "(previous output was empty)",
    )


_FENCE_LINE_RE = re.compile(r"^(?P<indent>[ \t]*)```(?P<info>[^\n`]*)[ \t]*$", re.MULTILINE)
_CATEGORICAL_SCOPE_SENTENCE_RE = re.compile(
    r"(?:(?<=^)|(?<=[.!?]\s))"
    r"(?P<quantifier>every|each|any|all)\s+"
    r"(?P<phrase>[a-z][\w-]*(?:\s+[a-z][\w-]*){0,5})\s+"
    r"(?P<verb>"
    r"should|must|needs?\s+to|can(?:not)?|cannot|does(?:\s+not|n't)|"
    r"fails?\s+to|is|are|preserves?|includes?|covers?|renders?|drops?|"
    r"omits?|keeps?|retains?|loses?|prints?"
    r")\b",
    re.IGNORECASE,
)
_CATEGORICAL_SCOPE_PREPOSITION_RE = re.compile(
    r"\b(?:on|across|for|in)\s+"
    r"(?P<quantifier>every|each|any|all)\s+"
    r"(?P<phrase>[a-z][\w-]*(?:\s+[a-z][\w-]*){0,5})\b",
    re.IGNORECASE,
)
_CATEGORICAL_SCOPE_REGARDLESS_RE = re.compile(r"\bregardless\s+of\b", re.IGNORECASE)
_CARDINAL_SCOPE_WORDS = frozenset(
    {
        "0",
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
        "7",
        "8",
        "9",
        "10",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "first",
        "second",
        "third",
        "single",
    }
)
_SCOPE_NOUN_HINTS = frozenset(
    {
        "adapter",
        "backend",
        "branch",
        "caller",
        "case",
        "command",
        "endpoint",
        "flow",
        "frontend",
        "handler",
        "implementation",
        "issue",
        "location",
        "mode",
        "output",
        "path",
        "provider",
        "renderer",
        "serializer",
        "site",
        "story",
        "surface",
        "variant",
        "view",
    }
)


def _normalize_scope_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _phrase_is_scope_like(phrase: str) -> bool:
    tokens = [token.lower() for token in re.findall(r"[a-z0-9][\w-]*", phrase)]
    if not tokens:
        return False
    if tokens[0] in _CARDINAL_SCOPE_WORDS:
        return False
    return any(token in _SCOPE_NOUN_HINTS for token in tokens)


def _text_asserts_categorical_scope(text: str) -> bool:
    normalized = _normalize_scope_text(text)
    if not normalized:
        return False
    if _CATEGORICAL_SCOPE_REGARDLESS_RE.search(normalized):
        return True
    return any(
        _phrase_is_scope_like(match.group("phrase"))
        for pattern in (_CATEGORICAL_SCOPE_SENTENCE_RE, _CATEGORICAL_SCOPE_PREPOSITION_RE)
        for match in pattern.finditer(normalized)
    )


def derive_issue_scope_requirement(issue_title: str, issue_body: str) -> tuple[bool, str]:
    """Return whether the fetched issue text asserts categorical symptom scope.

    Prefer the bug issue's expected-behavior section, which is where this repo's
    bug-body contract puts the generalized rule, but also inspect the issue
    title so categorical claims stated only there still fail closed. Detection
    stays intentionally narrow: the fail-closed scope requirement is for
    category-level surface/path/story-style claims, not incidental quantifiers
    inside a single concrete example.
    """
    normalized_title = _normalize_scope_text(issue_title or "")
    expected = extract_section(issue_body or "", BUG_EXPECTATION_HEADING)
    normalized_expected = _normalize_scope_text(expected or "")
    normalized_body = _normalize_scope_text(issue_body or "")

    if normalized_expected and _text_asserts_categorical_scope(normalized_expected):
        return True, normalized_expected
    if normalized_title and _text_asserts_categorical_scope(normalized_title):
        return True, normalized_title
    if (
        not normalized_expected
        and normalized_body
        and _text_asserts_categorical_scope(normalized_body)
    ):
        return True, normalized_body
    if normalized_expected:
        return False, normalized_expected
    if normalized_body:
        return False, normalized_body
    return False, normalized_title


@dataclass(frozen=True)
class _FenceLine:
    indent: int
    info: str
    start: int
    end: int

    @property
    def is_close(self) -> bool:
        return self.info == ""

    @property
    def opens_yaml(self) -> bool:
        return self.info.lower().startswith("yaml")


def _iter_fence_lines(output: str) -> list[_FenceLine]:
    return [
        _FenceLine(
            indent=len(match.group("indent")),
            info=match.group("info").strip(),
            start=match.start(),
            end=match.end(),
        )
        for match in _FENCE_LINE_RE.finditer(output)
    ]


def _extract_yaml_block(output: str) -> str:
    """Pull the first fenced YAML block out of agent output, falling back to raw."""
    fences = _iter_fence_lines(output)
    if not fences:
        return output

    opener = next((fence for fence in fences if fence.opens_yaml), fences[0])
    closer = next(
        (
            fence
            for fence in fences
            if fence.start > opener.start and fence.is_close and fence.indent <= opener.indent
        ),
        None,
    )
    if closer is None:
        raise ValueError("Malformed fenced response: opening fence is missing a matching close.")
    start = opener.end
    if start < len(output) and output[start] == "\n":
        start += 1
    end = closer.start
    if end > start and output[end - 1] == "\n":
        end -= 1
    return output[start:end]


def _parse_yaml_mapping(yaml_text: str) -> dict[object, object]:
    parsed = yaml.safe_load(yaml_text)
    if not isinstance(parsed, dict):
        raise TypeError(type(parsed).__name__)
    return parsed


def _normalize_parse_error(exc: yaml.YAMLError) -> str:
    detail = " ".join(str(exc).split())
    return f"YAML syntax error: {detail}"


def _normalize_extract_error(exc: ValueError) -> str:
    return str(exc)


def _normalize_root_error(root_type: str) -> str:
    return f"YAML block did not parse to a mapping of diagnosis keys (root was {root_type})."


@dataclass(frozen=True)
class DiagnoseParseOutcome:
    """Result of one strict parse attempt over diagnose agent output.

    ``artifact`` is None exactly when the output did not yield a YAML mapping;
    ``error`` then carries the concrete reason (the ``yaml.YAMLError`` message, or
    the non-mapping root shape).  Callers need that distinction to tell a
    recoverable serialization defect — worth one reformat retry — from an agent
    that emitted no diagnosis at all.
    """

    artifact: DiagnosisArtifact | None
    error: str = ""


def parse_diagnose_output(
    output: str,
    *,
    issue_number: int,
    partial: bool = False,
) -> DiagnosisArtifact | None:
    """Parse agent output into a DiagnosisArtifact.

    Returns None when the YAML cannot be parsed at all.  When the YAML is
    parseable but missing fields, returns an artifact with empty strings for
    missing values; the caller decides what to do with an incomplete artifact.

    Thin wrapper over :func:`parse_diagnose_output_result` for callers that only
    need the artifact-or-None answer.
    """
    return parse_diagnose_output_result(
        output, issue_number=issue_number, partial=partial
    ).artifact


def parse_diagnose_output_result(
    output: str,
    *,
    issue_number: int,
    partial: bool = False,
) -> DiagnoseParseOutcome:
    """Strictly parse agent output, reporting *why* a parse failed.

    Parsing is unchanged and remains the artifact integrity boundary: a document
    that is not a YAML mapping is still rejected.  The only addition over
    :func:`parse_diagnose_output` is that the rejection reason is returned instead
    of discarded, so the diagnose flow can put it in the audit trail and in a
    bounded reformat-retry prompt.
    """
    try:
        yaml_text = _extract_yaml_block(output)
        parsed = _parse_yaml_mapping(yaml_text)
    except ValueError as exc:
        return DiagnoseParseOutcome(None, _normalize_extract_error(exc))
    except yaml.YAMLError as exc:
        return DiagnoseParseOutcome(None, _normalize_parse_error(exc))
    except TypeError as exc:
        return DiagnoseParseOutcome(None, _normalize_root_error(str(exc)))

    raw_hypotheses = parsed.get("hypotheses") or []
    hypotheses: list[Hypothesis] = []
    if isinstance(raw_hypotheses, list):
        for entry in raw_hypotheses:
            if not isinstance(entry, dict):
                continue
            hypotheses.append(
                Hypothesis(
                    statement=str(entry.get("statement", "")).strip(),
                    status=str(entry.get("status", "inconclusive")).strip().lower()
                    or "inconclusive",
                    evidence=str(entry.get("evidence", "")).strip(),
                )
            )

    raw_inspected = parsed.get("inspected_files") or []
    inspected: list[InspectedFile] = []
    if isinstance(raw_inspected, list):
        seen: set[str] = set()
        for entry in raw_inspected:
            if isinstance(entry, str):
                path = entry.strip()
                digest = ""
            elif isinstance(entry, dict):
                path = str(entry.get("path", "")).strip()
                digest = str(entry.get("content_sha256", "")).strip()
            else:
                continue
            if not path or path in seen:
                continue
            seen.add(path)
            inspected.append(InspectedFile(path=path, content_sha256=digest))

    raw_anchors = parsed.get("premise_anchors") or []
    anchors: list[PremiseAnchor] = []
    if isinstance(raw_anchors, list):
        seen_anchors: set[tuple[str, str]] = set()
        for entry in raw_anchors:
            if isinstance(entry, str):
                file = entry.strip()
                pattern = ""
            elif isinstance(entry, dict):
                file = str(entry.get("file", "")).strip()
                pattern = str(entry.get("pattern", "")).strip()
            else:
                continue
            if not file:
                continue
            key = (file, pattern)
            if key in seen_anchors:
                continue
            seen_anchors.add(key)
            anchors.append(PremiseAnchor(file=file, pattern=pattern))

    raw_related = parsed.get("related_findings") or []
    related: list[RelatedFinding] = []
    if isinstance(raw_related, list):
        seen_related: set[tuple[str, str]] = set()
        for entry in raw_related:
            if isinstance(entry, str):
                summary = entry.strip()
                ref = ""
            elif isinstance(entry, dict):
                summary = str(entry.get("summary", "")).strip()
                ref = str(entry.get("related", "")).strip()
            else:
                continue
            if not summary:
                continue
            key = (summary, ref)
            if key in seen_related:
                continue
            seen_related.add(key)
            related.append(RelatedFinding(summary=summary, related=ref))

    raw_scope = parsed.get("symptom_scope_coverage")
    scope_categorical = False
    scope_text = ""
    scope_locations: list[ScopeCoverageLocation] = []
    if isinstance(raw_scope, dict):
        scope_categorical = bool(raw_scope.get("symptom_is_categorical", False))
        scope_text = str(raw_scope.get("stated_scope", "")).strip()
        raw_locations = raw_scope.get("examined_locations") or []
        if isinstance(raw_locations, list):
            seen_locations: set[tuple[str, str, str]] = set()
            for entry in raw_locations:
                if isinstance(entry, str):
                    location = entry.strip()
                    status = ""
                    rationale = ""
                elif isinstance(entry, dict):
                    location = str(entry.get("location", "")).strip()
                    status = str(entry.get("status", "")).strip().lower()
                    rationale = str(entry.get("rationale", "")).strip()
                else:
                    continue
                if not location:
                    continue
                key = (location, status, rationale)
                if key in seen_locations:
                    continue
                seen_locations.add(key)
                scope_locations.append(
                    ScopeCoverageLocation(
                        location=location,
                        status=status,
                        rationale=rationale,
                    )
                )

    artifact = DiagnosisArtifact(
        issue_number=issue_number,
        observed_symptom=str(parsed.get("observed_symptom", "")).strip(),
        reproduction_or_evidence=str(parsed.get("reproduction_or_evidence", "")).strip(),
        hypotheses=tuple(hypotheses),
        confirmed_cause=str(parsed.get("confirmed_cause", "")).strip(),
        affected_code_path=str(parsed.get("affected_code_path", "")).strip(),
        fix_success_criterion=str(parsed.get("fix_success_criterion", "")).strip(),
        partial=partial,
        notes=str(parsed.get("notes", "")).strip(),
        inspected_files=tuple(inspected),
        premise_anchors=tuple(anchors),
        related_findings=tuple(related),
        symptom_scope_coverage=SymptomScopeCoverage(
            symptom_is_categorical=scope_categorical,
            stated_scope=scope_text,
            examined_locations=tuple(scope_locations),
        ),
    )
    return DiagnoseParseOutcome(artifact)
