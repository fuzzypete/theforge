"""Prompt construction and output parsing for the diagnose flow.

The diagnose prompt structure is intentionally distinct from plan prompts:
plan describes how to *act on* a known cause; diagnose describes how to
*find* the cause.
"""

from __future__ import annotations

import yaml

from theforge.diagnose_types import (
    DiagnosisArtifact,
    Hypothesis,
    InspectedFile,
    PremiseAnchor,
    RelatedFinding,
)

_DIAGNOSE_PROMPT_TEMPLATE = """\
You are an investigative diagnosis agent.  A symptom bug has been reported but
the cause is unknown.  Your job is NOT to fix anything.  Your job is to find
out **what is actually happening** so a separate fix flow can act on a real
cause.

Mode: {mode}

== ISSUE #{issue_number}: {title} ==

{body}

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
6. **Scope the confirmed cause to THIS issue's stated symptom — nothing more.**
   The diagnosis boundary must match the issue boundary.  While investigating
   you may notice other real defects in nearby code that are NOT the cause of
   this issue's symptom (a different bug, another issue's domain, a latent
   problem you happened to see).  Do NOT fold those into ``confirmed_cause`` or
   ``affected_code_path`` — a downstream dev implements the confirmed cause
   verbatim, so anything you put there becomes part of THIS issue's fix and
   over-scopes it.  Instead, record each such adjacent problem as a separate
   entry in ``related_findings``, naming the owning/related issue if you know
   it (e.g. ``"#1649"``).  Ask of every claim you put in ``confirmed_cause``:
   "is this the cause of the *stated* symptom?"  If it is a neighboring problem
   rather than the cause of what the issue reports, it belongs in
   ``related_findings``, not in the fix scope.

== OUTPUT FORMAT ==

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
```
"""


def _forge_relpath(parts: tuple[str, ...]) -> str:
    """Render a repo-relative path from a path-constant tuple.

    Uses forward slashes on every platform so the briefing reads identically
    regardless of the host OS.
    """
    return "/".join(parts)


def build_environment_briefing() -> str:
    """Return the ``== ENVIRONMENT ==`` orientation section for the prompt.

    The audit/log paths and the landing-field semantics are derived from the
    modules that own them — :mod:`theforge.coordinator.audit_substrate` for the
    canonical audit paths and :mod:`theforge.coordinator.landing_record` for the
    landing outcomes — rather than being hardcoded here. Adding a new audit
    path constant or a new landing ``landing_path`` -> outcome mapping in code
    therefore surfaces in the briefing automatically, with no hand-edit to this
    template (issue #1425 AC2).

    Imports are function-local so the low-dependency prompt module does not pull
    the coordinator's audit stack in at import time.
    """
    from theforge.coordinator import audit_substrate  # noqa: PLC0415
    from theforge.coordinator.landing_record import (  # noqa: PLC0415
        LANDING_OUTCOME_BY_PATH,
    )

    substrate = _forge_relpath(audit_substrate.SUBSTRATE_RELPATH)
    runs = _forge_relpath(audit_substrate.RUNS_RELPATH)
    history = _forge_relpath(audit_substrate.HISTORY_RELPATH)
    audits = _forge_relpath(audit_substrate.AUDITS_RELPATH)

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
- Audit index (SQLite, canonical, queryable): {substrate}
- Per-run audit records (JSON, one file per run): {runs}/*.json
- Legacy cross-run audit history (JSONL — superseded by the index above but
  still present on older repos): {history}
- Sprint audit + summary YAML: {audits}/sprint-audit.yaml and
  {audits}/run-<run-id>-sprint-audit.yaml
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

Code locations for the major flows:
- Coordinator engine (phase state machine): src/theforge/coordinator/engine.py
- Story landing / merge (land_story):        src/theforge/coordinator/completion.py
- Audit paths + field schema:                src/theforge/coordinator/audit_substrate.py
- Sprint scheduling + audit writers:         src/theforge/sprint/
- Agent execution / providers:               src/theforge/runners/"""


def build_diagnose_prompt(
    *,
    issue_number: int,
    title: str,
    body: str,
    mode: str = "autonomous",
) -> str:
    """Return the diagnose-agent prompt for one issue.

    ``mode`` is "autonomous" or "interactive"; it is conveyed to the agent so
    it can adjust verbosity (interactive runs may benefit from richer notes
    explaining intermediate steps).

    The prompt embeds an environment briefing (see
    :func:`build_environment_briefing`) so the agent starts from TheForge's
    known audit/log layout instead of burning its budget rediscovering it.
    """
    return _DIAGNOSE_PROMPT_TEMPLATE.format(
        issue_number=issue_number,
        title=title or f"Issue #{issue_number}",
        body=body or "(issue body is empty)",
        mode=mode,
        environment=build_environment_briefing(),
    )


def _extract_yaml_block(output: str) -> str:
    """Pull the first fenced YAML block out of agent output, falling back to raw."""
    if "```yaml" in output:
        start = output.index("```yaml") + len("```yaml")
        try:
            end = output.index("```", start)
            return output[start:end]
        except ValueError:
            return output[start:]
    if "```" in output:
        start = output.index("```") + len("```")
        try:
            end = output.index("```", start)
            return output[start:end]
        except ValueError:
            return output[start:]
    return output


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
    """
    yaml_text = _extract_yaml_block(output)
    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None

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

    return DiagnosisArtifact(
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
    )
