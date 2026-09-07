#!/usr/bin/env python3
"""Measure how often a diagnosed fix-success criterion admits a symptom-only change.

Answers two separate questions over a stated window of the diagnose audit corpus:

    criteria admitting a symptom-removing change:  N / D
    of those, where such a change actually landed: M / N

`N / D` alone does not justify acting — a criterion that could have been gamed
but was not is evidence the surrounding issue body and review are doing the
work. `M / N` is the rate that justifies acting, so the two are counted and
printed separately and never collapsed.

The mechanical half is derived, not hand-assembled: the diagnose audit YAMLs
under ``<corpus-root>/.forge/audits/`` supply the window, the cohort, the
selected attempt and the criterion text; ``git log`` over the integration refs
supplies the commit that actually landed, by matching the issue's exact title
against commit subjects with a trailing ``(#N)`` stripped (that trailing number
is the *story/PR* number, not the issue number, which is why grepping for
``(#issue)`` finds nothing).

The qualitative half — whether a criterion admits a cause-preserving change,
and whether the change that landed was one — cannot be derived and lives in a
checked-in adjudication file. That file also records the mechanical facts each
judgment was made against (the selected run id, a hash of the criterion text,
the landed commit sha). A normal run re-derives those facts and refuses to
render if any of them drifted, which is what makes the report reproducible
rather than hand-assembled.

Usage:
    measure_criterion_gaming.py --adjudications FILE [--out FILE] [--format md|json]
    measure_criterion_gaming.py --adjudications FILE --refresh-facts

Normal runs are read-only and offline: they read the corpus and ``git log``
only. ``--refresh-facts`` is the only mode that writes the adjudication file,
and the only mode that calls ``gh`` — it caches each issue's title and state so
a later re-render validates and prints without network or GitHub auth.

Operates on the repository rather than shipping with it, so it lives in
`scripts/` and is unit-tested from `tests/test_measure_criterion_gaming.py`.
Never `import theforge`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import yaml

# The window's lower bound. Chosen so the cohort is the one every calibration
# figure in this file's tests was measured against; a 2026-08-20 bound admits 62
# issues, which is a different measurement, not a wider version of this one.
DEFAULT_SINCE = "2026-09-01T00:00:00+00:00"

# #2595 is the spec's own worked example and predates the window by eleven days.
# It is named here rather than pulled in by widening the bound, so that its
# inclusion is visible in the report instead of hiding inside a date.
DEFAULT_INCLUDE_ISSUES = (2595,)

# `git log --all` also traverses preserved feature, backup and archive refs —
# this repository deliberately retains escalated worktree branches — so a
# committed but never-landed attempt would otherwise be read as the landed
# change. Only commits reachable from an integration ref count as landed.
DEFAULT_INTEGRATION_REFS = (
    "refs/heads/main",
    "refs/remotes/origin/main",
    "refs/heads/release/*",
    "refs/remotes/origin/release/*",
)

# Trailing "(#1234)" on a squash-merge commit subject: the PR/story number
# GitHub appends, which routinely differs from the diagnosed issue number.
_TRAILING_PR_RE = re.compile(r"\s*\(#\d+\)\s*$")

# The landing markers write_diagnose_audit actually records. There is no
# `dry_run` key on the audit — do not look for one; `location` is the signal.
_DRY_RUN_PREFIX = "<dry-run:"

CLASSIFICATIONS = (
    "symptom_only",
    "cause_addressing",
    "no_landed_change",
    "unresolved",
)

SELECTION_BASES = ("body_contains_criterion", "latest_done")

DECISION_OVERRIDES = ("act", "close")


class MeasurementError(RuntimeError):
    """A drift, corpus or adjudication problem that must stop the render."""


# --------------------------------------------------------------------------
# Corpus
# --------------------------------------------------------------------------


def normalize_criterion(text: str) -> str:
    """NFC, CRLF -> LF, trailing whitespace stripped per line.

    Issue bodies round-trip through GitHub, which reflows line endings, so the
    criterion recorded in the audit and the criterion sitting in the body are
    routinely byte-different and semantically identical.
    """
    normalized = unicodedata.normalize("NFC", text)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in normalized.split("\n")).strip()


def collapse_whitespace(text: str) -> str:
    """Second-chance comparison form: all whitespace runs become one space."""
    return " ".join(normalize_criterion(text).split())


def criterion_sha256(text: str) -> str:
    return hashlib.sha256(normalize_criterion(text).encode("utf-8")).hexdigest()


def load_attempts(corpus_root: Path) -> list[dict]:
    """Read every ``diagnose-issue-*.yaml`` under the corpus root.

    Returns one dict per diagnose attempt with the fields the cohort rule and
    the adjudicator need. Raises if the directory holds no diagnose audits, so
    a mistyped root produces a named failure rather than an empty report.
    """
    audit_dir = corpus_root / ".forge" / "audits"
    paths = sorted(audit_dir.glob("diagnose-issue-*.yaml"))
    attempts: list[dict] = []
    for path in paths:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        artifact = data.get("artifact") or {}
        if not isinstance(artifact, dict):
            artifact = {}
        landing = data.get("landing") or {}
        if not isinstance(landing, dict):
            landing = {}
        issue_number = data.get("issue_number")
        if not isinstance(issue_number, int):
            continue
        attempts.append(
            {
                "path": path,
                "run_id": str(data.get("run_id") or ""),
                "issue_number": issue_number,
                "issue_title": str(data.get("issue_title") or ""),
                "started_at": str(data.get("started_at") or ""),
                "final_phase": str(data.get("final_phase") or ""),
                "landing_location": landing.get("location"),
                "confirmed_cause": str(artifact.get("confirmed_cause") or ""),
                "fix_success_criterion": str(artifact.get("fix_success_criterion") or ""),
            }
        )
    if not attempts:
        raise MeasurementError(
            f"no diagnose audits found under {audit_dir} — "
            "pass --corpus-root pointing at the checkout whose .forge/audits/ holds them"
        )
    return attempts


def _in_window(started_at: str, since: str, until: str) -> bool:
    # ISO-8601 UTC timestamps compare correctly as strings once the offsets
    # match, but the corpus mixes "+00:00" and "Z", so parse rather than assume.
    try:
        stamp = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return _parse_bound(since) <= stamp <= _parse_bound(until)


def _parse_bound(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def select_cohort(
    attempts: list[dict],
    *,
    since: str,
    until: str,
    include_issues: tuple[int, ...] = (),
) -> dict:
    """Apply the window and the cohort rule, counting every exclusion by clause.

    The cohort rule, per attempt:

      * ``final_phase == "DONE"`` — a failed or partial diagnose produced no
        criterion the dev was graded against;
      * ``artifact.fix_success_criterion`` non-empty;
      * ``landing.location`` non-null and not a ``<dry-run:`` marker — a
        criterion that never reached the issue body graded nobody.

    A location that is path-shaped, and therefore indistinguishable between a
    dry and a live landing, goes in its own named bucket rather than being
    silently admitted. That bucket is defensively present and empty in the
    current corpus.
    """
    exclusions: dict[str, int] = {
        "excluded_by_window": 0,
        "not_done": 0,
        "empty_criterion": 0,
        "no_landing": 0,
        "dry_run_landing": 0,
        "indistinguishable_landing": 0,
    }
    by_issue: dict[int, list[dict]] = {}
    windowed_issues: set[int] = set()
    excluded_window_issues: set[int] = set()
    phase_counts: dict[str, int] = {}

    for attempt in attempts:
        included_by_name = attempt["issue_number"] in include_issues
        if not included_by_name and not _in_window(attempt["started_at"], since, until):
            exclusions["excluded_by_window"] += 1
            excluded_window_issues.add(attempt["issue_number"])
            continue
        windowed_issues.add(attempt["issue_number"])
        phase = attempt["final_phase"] or "UNKNOWN"
        phase_counts[phase] = phase_counts.get(phase, 0) + 1

        if attempt["final_phase"] != "DONE":
            exclusions["not_done"] += 1
            continue
        if not attempt["fix_success_criterion"].strip():
            exclusions["empty_criterion"] += 1
            continue
        location = attempt["landing_location"]
        if location is None:
            exclusions["no_landing"] += 1
            continue
        location = str(location)
        if location.startswith(_DRY_RUN_PREFIX):
            exclusions["dry_run_landing"] += 1
            continue
        if _is_path_shaped(location):
            exclusions["indistinguishable_landing"] += 1
            continue
        by_issue.setdefault(attempt["issue_number"], []).append(attempt)

    for attempts_for_issue in by_issue.values():
        attempts_for_issue.sort(key=lambda a: a["started_at"])

    return {
        "by_issue": by_issue,
        "exclusions": exclusions,
        "phase_counts": phase_counts,
        "windowed_issues": sorted(windowed_issues),
        "excluded_by_window_issues": sorted(excluded_window_issues - windowed_issues),
    }


def _is_path_shaped(location: str) -> bool:
    """A location that names a file path rather than an issue body update.

    ``issue #N body updated`` is a live landing; ``<dry-run: ...>`` is caught
    earlier. Anything that looks like a path could be either, so it is counted
    rather than assumed.
    """
    return "/" in location and not location.startswith("issue #")


# --------------------------------------------------------------------------
# Attempt selection (dedup across multiple diagnose attempts on one issue)
# --------------------------------------------------------------------------


def select_attempt(attempts: list[dict], issue_body: str | None) -> dict:
    """Pick the attempt whose criterion is the one the dev was graded against.

    An issue diagnosed more than once carries only the last-landed criterion in
    its body, so body containment identifies the governing attempt directly.
    When the body was later reshaped and no attempt matches, fall back to the
    latest qualifying attempt and stamp the row ``latest_done`` — a row whose
    governing criterion an adjudicator must confirm by hand.
    """
    skipped: list[dict] = []
    if issue_body:
        body_exact = normalize_criterion(issue_body)
        body_loose = collapse_whitespace(issue_body)
        matches = [
            a
            for a in attempts
            if normalize_criterion(a["fix_success_criterion"]) in body_exact
            or collapse_whitespace(a["fix_success_criterion"]) in body_loose
        ]
        if len(matches) == 1:
            chosen = matches[0]
            for other in attempts:
                if other is not chosen:
                    skipped.append(
                        {
                            "run_id": other["run_id"],
                            "reason": "criterion_not_in_issue_body",
                        }
                    )
            return {"attempt": chosen, "selection": "body_contains_criterion", "skipped": skipped}

    chosen = max(attempts, key=lambda a: a["started_at"])
    for other in attempts:
        if other is not chosen:
            skipped.append({"run_id": other["run_id"], "reason": "superseded_by_later_attempt"})
    return {"attempt": chosen, "selection": "latest_done", "skipped": skipped}


# --------------------------------------------------------------------------
# Landed-change join
# --------------------------------------------------------------------------


def build_subject_index(log_output: str) -> dict[str, set[str]]:
    """Map commit subject (trailing ``(#N)`` stripped) -> set of shas."""
    index: dict[str, set[str]] = {}
    for line in log_output.splitlines():
        if "\x00" not in line:
            continue
        sha, subject = line.split("\x00", 1)
        key = _TRAILING_PR_RE.sub("", subject).strip()
        if key:
            index.setdefault(key, set()).add(sha.strip())
    return index


def git_subject_index(corpus_root: Path, integration_refs: tuple[str, ...]) -> dict[str, set[str]]:
    refs = _resolve_refs(corpus_root, integration_refs)
    if not refs:
        raise MeasurementError(
            f"no integration refs matched {list(integration_refs)} in {corpus_root} — "
            "a landed-change join over unmerged refs would count rejected attempts as landed"
        )
    result = _run(["git", "log", "--format=%H%x00%s", *refs], corpus_root)
    return build_subject_index(result)


def _resolve_refs(corpus_root: Path, patterns: tuple[str, ...]) -> list[str]:
    out = _run(["git", "for-each-ref", "--format=%(refname)", *patterns], corpus_root)
    return [line.strip() for line in out.splitlines() if line.strip()]


def join_landed_change(title: str, state: str, index: dict[str, set[str]]) -> dict:
    """Resolve the commit that landed the fix for an issue, or say why not.

    Returns a dict with ``classification_hint`` naming the mechanical outcome:
    a unique commit, a proven-absent change (no commit and the issue is still
    OPEN), or an unresolved join whose reason is recorded so U is attributable.
    """
    shas = sorted(index.get(normalize_criterion(title), set()))
    if len(shas) == 1:
        return {"landed_commit": shas[0], "landed_reason": None, "outcome": "commit_found"}
    if not shas:
        if state.upper() == "OPEN":
            return {
                "landed_commit": None,
                "landed_reason": "issue still OPEN and no commit carries its title",
                "outcome": "no_landed_change",
            }
        return {
            "landed_commit": None,
            "landed_reason": (
                f"issue is {state} but no integration commit carries its title — "
                "landed under a reworded subject, or squashed into another commit"
            ),
            "outcome": "unresolved",
        }
    return {
        "landed_commit": None,
        "landed_reason": f"{len(shas)} integration commits share this title: {', '.join(shas)}",
        "outcome": "unresolved",
    }


def _run(cmd: list[str], cwd: Path) -> str:
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode != 0:
        raise MeasurementError(f"`{' '.join(cmd[:3])}...` failed: {proc.stderr.strip()}")
    return proc.stdout


def fetch_issue(number: int, corpus_root: Path, repo: str | None) -> dict:
    """Fetch title/state/body for one issue. Only reached under --refresh-facts."""
    cmd = ["gh", "issue", "view", str(number), "--json", "title,state,stateReason,body"]
    if repo:
        cmd += ["--repo", repo]
    try:
        proc = subprocess.run(cmd, cwd=str(corpus_root), capture_output=True, text=True)
    except FileNotFoundError as exc:  # pragma: no cover - environment-dependent
        raise MeasurementError(
            "`gh` is not on PATH; --refresh-facts needs it to cache issue titles and states"
        ) from exc
    if proc.returncode != 0:
        raise MeasurementError(
            f"`gh issue view {number}` failed (auth or network?): {proc.stderr.strip()}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise MeasurementError(f"`gh issue view {number}` returned non-JSON output") from exc


# --------------------------------------------------------------------------
# Adjudication file
# --------------------------------------------------------------------------

_STUB_JUDGMENTS = {
    "admits_symptom_removing_change": None,
    "admitted_change": None,
    "admits_rationale": None,
    "landed_classification": None,
    "inspected_source": None,
    "governance_unconfirmed": False,
    "notes": None,
}


def load_adjudications(path: Path) -> dict:
    if not path.exists():
        raise MeasurementError(f"adjudication file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise MeasurementError(f"adjudication file is not a YAML mapping: {path}")
    window = data.get("window") or {}
    issues = data.get("issues") or {}
    if not isinstance(window, dict) or not isinstance(issues, dict):
        raise MeasurementError(f"adjudication file needs `window` and `issues` mappings: {path}")
    override = data.get("decision_override")
    if override is not None and override not in DECISION_OVERRIDES:
        raise MeasurementError(
            f"decision_override must be one of {list(DECISION_OVERRIDES)}, got {override!r}"
        )
    return {
        "window": window,
        "issues": {int(k): (v or {}) for k, v in issues.items()},
        "decision_override": override,
        "decision_override_rationale": data.get("decision_override_rationale"),
    }


def window_from(adjudications: dict) -> dict:
    window = adjudications["window"]
    return {
        "since": str(window.get("since") or DEFAULT_SINCE),
        "until": str(window.get("until") or _now_iso()),
        "include_issues": tuple(int(n) for n in (window.get("include_issues") or ())),
        "corpus_root": window.get("corpus_root"),
        "integration_refs": tuple(window.get("integration_refs") or DEFAULT_INTEGRATION_REFS),
        # The date the judgments were filed, for the record's `Status:` line.
        # Stamped at --refresh-facts and then fixed, so a re-render of unchanged
        # facts does not silently re-date the record.
        # Local date, not UTC: the `Status:` line dates the filing for a reader
        # in the operator's timezone, and a UTC stamp taken in the evening
        # reads as tomorrow.
        "record_date": str(window.get("record_date") or datetime.now().date().isoformat()),
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Fact derivation + drift check
# --------------------------------------------------------------------------


def derive_facts(
    cohort: dict,
    recorded: dict[int, dict],
    index: dict[str, set[str]],
    *,
    issue_lookup=None,
) -> dict[int, dict]:
    """Re-derive the mechanical facts for every cohort issue.

    ``issue_lookup`` is supplied only under --refresh-facts; without it the
    cached title/state on each recorded row is used, so a normal run needs no
    network. Body containment likewise cannot re-run offline, so the recorded
    selection basis is validated (the selected run must still be a qualifying
    attempt with an unchanged criterion) rather than recomputed.
    """
    facts: dict[int, dict] = {}
    for number, attempts in sorted(cohort["by_issue"].items()):
        row = recorded.get(number, {})
        if issue_lookup is not None:
            fetched = issue_lookup(number)
            title = str(fetched.get("title") or "")
            state = str(fetched.get("state") or "")
            state_reason = fetched.get("stateReason")
            body = fetched.get("body") or ""
            chosen = select_attempt(attempts, body)
        else:
            title = str(row.get("issue_title") or "")
            state = str(row.get("issue_state") or "")
            state_reason = row.get("issue_state_reason")
            recorded_run = row.get("run_id")
            match = [a for a in attempts if a["run_id"] == recorded_run]
            if not match:
                raise MeasurementError(
                    f"#{number}: recorded run_id {recorded_run!r} is not a qualifying "
                    "attempt in the corpus — re-run with --refresh-facts"
                )
            basis = row.get("selection")
            if basis not in SELECTION_BASES:
                raise MeasurementError(
                    f"#{number}: selection must be one of {list(SELECTION_BASES)}, got {basis!r}"
                )
            chosen = {
                "attempt": match[0],
                "selection": basis,
                "skipped": row.get("skipped_attempts") or [],
            }

        attempt = chosen["attempt"]
        joined = join_landed_change(title, state, index)
        facts[number] = {
            "run_id": attempt["run_id"],
            "selection": chosen["selection"],
            "skipped_attempts": chosen["skipped"],
            "criterion_sha256": criterion_sha256(attempt["fix_success_criterion"]),
            "criterion": normalize_criterion(attempt["fix_success_criterion"]),
            "confirmed_cause": normalize_criterion(attempt["confirmed_cause"]),
            "issue_title": title,
            "issue_state": state,
            "issue_state_reason": state_reason,
            "landed_commit": joined["landed_commit"],
            "landed_reason": joined["landed_reason"],
            "join_outcome": joined["outcome"],
            "attempt_count": len(attempts),
        }
    return facts


_DRIFT_FIELDS = ("run_id", "criterion_sha256", "landed_commit", "issue_title", "issue_state")


def check_drift(facts: dict[int, dict], recorded: dict[int, dict]) -> list[str]:
    """Compare re-derived mechanical facts against what the judgments were made against."""
    problems: list[str] = []
    missing = sorted(set(facts) - set(recorded))
    extra = sorted(set(recorded) - set(facts))
    for number in missing:
        problems.append(f"#{number}: in the cohort but has no adjudication row")
    for number in extra:
        problems.append(f"#{number}: has an adjudication row but is not in the cohort")
    for number, derived in sorted(facts.items()):
        row = recorded.get(number)
        if row is None:
            continue
        for field in _DRIFT_FIELDS:
            if row.get(field) != derived[field]:
                problems.append(
                    f"#{number}: {field} drifted — adjudicated against "
                    f"{row.get(field)!r}, corpus now says {derived[field]!r}"
                )
        problems.extend(_judgment_problems(number, row))
    return problems


def _judgment_problems(number: int, row: dict) -> list[str]:
    problems: list[str] = []
    admits = row.get("admits_symptom_removing_change")
    if admits is None:
        problems.append(f"#{number}: unadjudicated — admits_symptom_removing_change is unset")
        return problems
    if not isinstance(admits, bool):
        problems.append(f"#{number}: admits_symptom_removing_change must be a bool")
    if admits and not (row.get("admitted_change") or "").strip():
        problems.append(f"#{number}: admits=true requires admitted_change naming the change")
    if not admits and not (row.get("admits_rationale") or "").strip():
        problems.append(f"#{number}: admits=false requires admits_rationale")
    classification = row.get("landed_classification")
    if classification not in CLASSIFICATIONS:
        problems.append(
            f"#{number}: landed_classification must be one of {list(CLASSIFICATIONS)}, "
            f"got {classification!r}"
        )
    elif (
        classification in ("symptom_only", "cause_addressing")
        and not (row.get("inspected_source") or "").strip()
    ):
        problems.append(f"#{number}: {classification} requires inspected_source citing the sha")
    return problems


def cross_check_classification(facts: dict[int, dict], recorded: dict[int, dict]) -> list[str]:
    """The mechanical join constrains which classifications are even available."""
    problems: list[str] = []
    for number, derived in sorted(facts.items()):
        row = recorded.get(number) or {}
        classification = row.get("landed_classification")
        outcome = derived["join_outcome"]
        if outcome == "no_landed_change" and classification != "no_landed_change":
            problems.append(
                f"#{number}: join proved no landed change (issue OPEN, no commit) "
                f"but the row says {classification!r}"
            )
        if outcome == "unresolved" and classification != "unresolved":
            problems.append(
                f"#{number}: join is unresolved ({derived['landed_reason']}) "
                f"but the row says {classification!r}"
            )
        if outcome == "commit_found" and classification in ("no_landed_change", "unresolved"):
            problems.append(
                f"#{number}: join resolved commit {derived['landed_commit']} "
                f"but the row says {classification!r}"
            )
    return problems


# --------------------------------------------------------------------------
# Rollups + decision
# --------------------------------------------------------------------------


def roll_up(facts: dict[int, dict], recorded: dict[int, dict]) -> dict:
    """Count the two questions separately. Collapsing them overstates the problem."""
    denominator = sorted(facts)
    admitting = [n for n in denominator if recorded[n].get("admits_symptom_removing_change")]
    governance_unconfirmed = [n for n in admitting if recorded[n].get("governance_unconfirmed")]
    countable = [n for n in admitting if n not in governance_unconfirmed]
    realised = [n for n in countable if recorded[n].get("landed_classification") == "symptom_only"]
    unresolved = [n for n in countable if recorded[n].get("landed_classification") == "unresolved"]
    resolved = [n for n in countable if n not in unresolved]
    return {
        "D": len(denominator),
        "denominator_issues": denominator,
        "N": len(admitting),
        "admitting_issues": admitting,
        "M": len(realised),
        "realised_issues": realised,
        "U": len(unresolved),
        "unresolved_issues": unresolved,
        "R": len(resolved),
        "governance_unconfirmed": governance_unconfirmed,
    }


def decide(rollup: dict, override: str | None) -> tuple[str, str]:
    """Binary by construction: act when the failure mode was realised, else close.

    U is a caveat printed beside the decision, never a third outcome — an
    unmeasured row cannot make the question open-ended forever.
    """
    if override in DECISION_OVERRIDES:
        return override, "operator override"
    if rollup["M"] >= 1:
        return "act", f"M = {rollup['M']} — the failure mode was realised at least once"
    return "close", "M = 0 — the failure mode was not realised in this window"


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _quote(text: str, indent: str = "> ") -> str:
    lines = [line for line in text.split("\n")]
    return "\n".join(f"{indent}{line}".rstrip() for line in lines)


def render_markdown(
    *,
    window: dict,
    cohort: dict,
    facts: dict[int, dict],
    recorded: dict[int, dict],
    rollup: dict,
    decision: tuple[str, str],
) -> str:
    exclusions = cohort["exclusions"]
    out: list[str] = []
    add = out.append

    add("# Measurement: how often a diagnosed fix-success criterion admits a symptom-only change")
    add("")
    add(
        f"Status: record ({window['record_date']}, issue #2603). This is a measurement, not a "
        "policy change. It replaces an instinct with a number; the decision it records is "
        "scoped to the window below and to nothing else."
    )
    add("")
    add(
        "Generated by `scripts/measure_criterion_gaming.py` from "
        f"`{window['adjudications_path']}`. Do not hand-edit — regenerate instead, or the "
        "drift check stops meaning anything."
    )
    add("")
    add("## Window and corpus")
    add("")
    add(f"- corpus root: `{window['corpus_root']}`")
    add(f"- since: `{window['since']}`")
    add(f"- until: `{window['until']}`")
    add(
        "- included by name (outside the window): "
        + (
            ", ".join(f"#{n}" for n in window["include_issues"])
            if window["include_issues"]
            else "none"
        )
    )
    refs = ", ".join(f"`{r}`" for r in window["integration_refs"])
    add(f"- integration refs for the landed-change join: {refs}")
    add("")
    add("## Cohort rule and exclusions")
    add("")
    add("An attempt enters the cohort when all three hold:")
    add("")
    add("1. `final_phase == DONE`;")
    add("2. `artifact.fix_success_criterion` is non-empty;")
    add("3. `landing.location` is non-null and is not a `<dry-run:` marker.")
    add("")
    add("Attempts excluded, counted per clause:")
    add("")
    add("| Exclusion | Attempts |")
    add("| --- | --- |")
    add(f"| outside the window | {exclusions['excluded_by_window']} |")
    add(f"| `final_phase != DONE` | {exclusions['not_done']} |")
    add(f"| empty `fix_success_criterion` | {exclusions['empty_criterion']} |")
    add(f"| `landing.location: null` (nothing landed) | {exclusions['no_landing']} |")
    add(f"| `<dry-run:` landing marker | {exclusions['dry_run_landing']} |")
    add(
        "| path-shaped landing, dry/live indistinguishable "
        f"| {exclusions['indistinguishable_landing']} |"
    )
    add("")
    add(
        f"Issues excluded entirely by the window: "
        f"{len(cohort['excluded_by_window_issues'])}. "
        "That is the dominant exclusion — the corpus reaches back well before this window."
    )
    add("")
    phases = ", ".join(f"{k}: {v}" for k, v in sorted(cohort["phase_counts"].items()))
    add(f"In-window attempt phases: {phases}.")
    add("")
    add("## Denominator")
    add("")
    listed = ", ".join(f"#{n}" for n in rollup["denominator_issues"])
    add(f"**D = {rollup['D']}** diagnosed issues: {listed}")
    add("")
    by_basis: dict[str, list[int]] = {}
    for number, derived in sorted(facts.items()):
        by_basis.setdefault(derived["selection"], []).append(number)
    add("Selected attempt per issue (an issue diagnosed more than once carries only the")
    add("last-landed criterion in its body, so containment identifies the governing attempt):")
    add("")
    for basis in SELECTION_BASES:
        numbers = by_basis.get(basis, [])
        add(
            f"- `{basis}`: {len(numbers)}"
            + (" — " + ", ".join(f"#{n}" for n in numbers) if numbers else "")
        )
    add("")
    join_counts: dict[str, list[int]] = {}
    for number, derived in sorted(facts.items()):
        join_counts.setdefault(derived["join_outcome"], []).append(number)
    add("Landed-change join (issue title matched against integration-ref commit subjects,")
    add("trailing `(#N)` story number stripped):")
    add("")
    for outcome, label in (
        ("commit_found", "unique commit found"),
        ("no_landed_change", "no commit and issue still OPEN — nothing landed"),
        ("unresolved", "join failed"),
    ):
        numbers = join_counts.get(outcome, [])
        add(
            f"- {label}: {len(numbers)}"
            + (" — " + ", ".join(f"#{n}" for n in numbers) if numbers else "")
        )
    add("")
    add("## Per-issue rows")
    add("")
    add("| Issue | Criterion admits a symptom-removing change | Landed change was one | Notes |")
    add("| --- | --- | --- | --- |")
    for number in rollup["denominator_issues"]:
        row = recorded[number]
        derived = facts[number]
        admits = row.get("admits_symptom_removing_change")
        admits_cell = "yes — " + _cell(row.get("admitted_change", "")) if admits else "no"
        classification = row.get("landed_classification")
        landed_cell = {
            "symptom_only": "**yes**",
            "cause_addressing": "no",
            "no_landed_change": "no change landed",
            "unresolved": "unresolved",
        }[classification]
        notes = _cell(row.get("notes") or row.get("admits_rationale") or "")
        add(f"| #{number} | {admits_cell} | {landed_cell} | {notes} |")
    add("")
    add("## Criteria named")
    add("")
    add(
        "Every row where either predicate is true, or the landed outcome is unresolved, "
        "quotes its criterion so the pattern can be characterised rather than described "
        "in the abstract."
    )
    add("")
    named = [
        n
        for n in rollup["denominator_issues"]
        if recorded[n].get("admits_symptom_removing_change")
        or recorded[n].get("landed_classification") in ("symptom_only", "unresolved")
    ]
    if not named:
        add("_No row met the naming condition in this window._")
    for number in named:
        row = recorded[number]
        derived = facts[number]
        add(f"### #{number} — {derived['issue_title']}")
        add("")
        add(f"- selected diagnose run `{derived['run_id']}` (`{derived['selection']}`)")
        landed = derived["landed_commit"] or f"none — {derived['landed_reason']}"
        add(f"- landed commit: `{landed}`")
        add(f"- classification: `{row.get('landed_classification')}`")
        if row.get("governance_unconfirmed"):
            add("- **governance unconfirmed** — excluded from M")
        add("")
        add("Recorded fix-success criterion:")
        add("")
        add(_quote(derived["criterion"]))
        add("")
        if row.get("admits_symptom_removing_change"):
            add(
                "Change that would satisfy it while leaving the cause in place: "
                f"{row.get('admitted_change')}"
            )
        else:
            add(f"Why no such change exists: {row.get('admits_rationale')}")
        add("")
        if row.get("notes"):
            add(f"Notes: {row['notes']}")
            add("")
    add("## Rates")
    add("")
    add("The two questions are reported separately. A criterion that admits a")
    add("symptom-removing change but did not receive one is a different finding from one")
    add("that did.")
    add("")
    add("```")
    add(f"criteria admitting a symptom-removing change:  {rollup['N']} / {rollup['D']}")
    add(f"of those, where such a change actually landed: {rollup['M']} / {rollup['N']}")
    add("```")
    add("")
    add(f"`M / N` = {rollup['M']} / {rollup['N']} is the headline rate — the rate at which")
    add("the failure mode was realised. `N / D` alone does not justify acting: a criterion")
    add("that could have been gamed but was not is evidence the surrounding issue body and")
    add("review are doing the work, which is itself the finding.")
    add("")
    if rollup["U"]:
        add(
            f"Coverage caveat: U = {rollup['U']} admitting "
            f"{'row' if rollup['U'] == 1 else 'rows'} "
            + ", ".join(f"#{n}" for n in rollup["unresolved_issues"])
            + " could not have their landed change resolved, so the true rate is bounded by"
        )
        add("")
        add("```")
        add(f"{rollup['M']} / {rollup['N']}  through  {rollup['M'] + rollup['U']} / {rollup['N']}")
        add(f"supplemental, over resolved evidence only: {rollup['M']} / {rollup['R']}")
        add("```")
        add("")
        add("Neither bound nor the supplemental figure replaces `M / N`.")
        add("")
    if rollup["governance_unconfirmed"]:
        add(
            "Excluded from M as `governance_unconfirmed` (the selected criterion could not "
            "be confirmed as the one the dev was graded against): "
            + ", ".join(f"#{n}" for n in rollup["governance_unconfirmed"])
        )
        add("")
    add("## Decision")
    add("")
    verdict, basis = decision
    add(f"**{verdict.upper()}** — {basis}.")
    add("")
    if verdict == "close":
        add(
            "The question is closed as not occurring at a rate worth acting on. The "
            "operator-facing doctrine against symptom-shaped criteria was written from "
            "hand-authored issues; on this evidence it does not transfer to diagnose "
            "output, and a session should not hand-edit a diagnosed body on intuition."
        )
    else:
        add(
            "The failure mode was realised, so the criteria named above are the pattern to act on."
        )
    if rollup["U"]:
        add("")
        add(
            f"Caveat, not a third outcome: U = {rollup['U']} unresolved admitting "
            f"{'row' if rollup['U'] == 1 else 'rows'} could flip this to `act` if "
            "resolved against it."
        )
    add("")
    return "\n".join(out) + "\n"


def _cell(text: str) -> str:
    """Collapse a judgment string into one markdown table cell."""
    return " ".join(str(text).split()).replace("|", "\\|")


def render_json(
    *,
    window: dict,
    cohort: dict,
    facts: dict[int, dict],
    recorded: dict[int, dict],
    rollup: dict,
    decision: tuple[str, str],
) -> str:
    payload = {
        "window": {
            **window,
            "include_issues": list(window["include_issues"]),
            "integration_refs": list(window["integration_refs"]),
        },
        "exclusions": cohort["exclusions"],
        "phase_counts": cohort["phase_counts"],
        "rates": {
            "D": rollup["D"],
            "N": rollup["N"],
            "M": rollup["M"],
            "U": rollup["U"],
            "R": rollup["R"],
            "denominator_issues": rollup["denominator_issues"],
            "admitting_issues": rollup["admitting_issues"],
            "realised_issues": rollup["realised_issues"],
            "unresolved_issues": rollup["unresolved_issues"],
            "governance_unconfirmed": rollup["governance_unconfirmed"],
        },
        "decision": {"verdict": decision[0], "basis": decision[1]},
        "issues": {
            str(number): {
                **{k: v for k, v in facts[number].items()},
                "admits_symptom_removing_change": recorded[number].get(
                    "admits_symptom_removing_change"
                ),
                "admitted_change": recorded[number].get("admitted_change"),
                "admits_rationale": recorded[number].get("admits_rationale"),
                "landed_classification": recorded[number].get("landed_classification"),
                "inspected_source": recorded[number].get("inspected_source"),
                "governance_unconfirmed": bool(recorded[number].get("governance_unconfirmed")),
                "notes": recorded[number].get("notes"),
            }
            for number in rollup["denominator_issues"]
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


# --------------------------------------------------------------------------
# --refresh-facts
# --------------------------------------------------------------------------


def refresh_facts_document(
    *,
    window: dict,
    facts: dict[int, dict],
    recorded: dict[int, dict],
    decision_override: str | None,
    decision_override_rationale: str | None,
) -> dict:
    """Rewrite the mechanical fields in place, preserving every judgment.

    New cohort issues get stub rows with null judgments; a stub fails the next
    normal run by name, so a truncated adjudication pass cannot silently shrink
    the denominator.
    """
    issues: dict[int, dict] = {}
    for number, derived in sorted(facts.items()):
        prior = recorded.get(number, {})
        row = {
            "run_id": derived["run_id"],
            "selection": derived["selection"],
            "criterion_sha256": derived["criterion_sha256"],
            "issue_title": derived["issue_title"],
            "issue_state": derived["issue_state"],
            "issue_state_reason": derived["issue_state_reason"],
            "landed_commit": derived["landed_commit"],
            "landed_reason": derived["landed_reason"],
            "skipped_attempts": derived["skipped_attempts"],
        }
        for key, default in _STUB_JUDGMENTS.items():
            row[key] = prior.get(key, default)
        issues[number] = row
    document = {
        "window": {
            "since": window["since"],
            "until": window["until"],
            "include_issues": list(window["include_issues"]),
            "corpus_root": window["corpus_root"],
            "integration_refs": list(window["integration_refs"]),
            "record_date": window["record_date"],
        },
        "issues": issues,
    }
    if decision_override is not None:
        document["decision_override"] = decision_override
        document["decision_override_rationale"] = decision_override_rationale
    return document


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def default_corpus_root() -> Path:
    """The operator checkout, not this worktree.

    A dev worktree's own `.forge/audits/` holds nothing; the diagnose YAMLs live
    in the checkout that ran them, which is the parent of the common git dir.
    """
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):  # pragma: no cover
        return Path.cwd()
    return Path(common).resolve().parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus-root", type=Path, default=None)
    parser.add_argument("--since", default=None)
    parser.add_argument("--until", default=None)
    parser.add_argument("--include-issue", type=int, action="append", default=None)
    parser.add_argument("--integration-ref", action="append", default=None)
    parser.add_argument("--adjudications", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--format", choices=("md", "json"), default="md")
    parser.add_argument("--repo", default=None)
    parser.add_argument(
        "--refresh-facts",
        action="store_true",
        help="rewrite the adjudication file's mechanical fields (the only mode that writes it, "
        "and the only mode that calls `gh`)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run_measurement(args)
    except MeasurementError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _run_measurement(args) -> int:
    adjudications = load_adjudications(args.adjudications)
    window = window_from(adjudications)

    if args.refresh_facts:
        if args.since:
            window["since"] = args.since
        window["until"] = args.until or _now_iso()
        if args.include_issue is not None:
            window["include_issues"] = tuple(args.include_issue)
        elif not window["include_issues"]:
            window["include_issues"] = DEFAULT_INCLUDE_ISSUES
        if args.integration_ref:
            window["integration_refs"] = tuple(args.integration_ref)
    else:
        for flag, key in (("since", "since"), ("until", "until")):
            value = getattr(args, flag)
            if value is not None and value != window[key]:
                raise MeasurementError(
                    f"--{flag} {value!r} disagrees with the adjudicated window "
                    f"{window[key]!r}; re-run with --refresh-facts to move the window"
                )

    corpus_root = args.corpus_root or (
        Path(window["corpus_root"]) if window["corpus_root"] else default_corpus_root()
    )
    corpus_root = Path(corpus_root)
    window["corpus_root"] = str(corpus_root)
    window["adjudications_path"] = str(args.adjudications)

    attempts = load_attempts(corpus_root)
    cohort = select_cohort(
        attempts,
        since=window["since"],
        until=window["until"],
        include_issues=window["include_issues"],
    )
    index = git_subject_index(corpus_root, window["integration_refs"])

    if args.refresh_facts:
        lookup = lambda number: fetch_issue(number, corpus_root, args.repo)  # noqa: E731
        facts = derive_facts(cohort, adjudications["issues"], index, issue_lookup=lookup)
        document = refresh_facts_document(
            window=window,
            facts=facts,
            recorded=adjudications["issues"],
            decision_override=adjudications["decision_override"],
            decision_override_rationale=adjudications["decision_override_rationale"],
        )
        args.adjudications.write_text(
            yaml.safe_dump(document, sort_keys=True, width=100, allow_unicode=True),
            encoding="utf-8",
        )
        print(f"refreshed {args.adjudications} ({len(facts)} cohort issues)", file=sys.stderr)
        return 0

    facts = derive_facts(cohort, adjudications["issues"], index)
    problems = check_drift(facts, adjudications["issues"])
    problems += cross_check_classification(facts, adjudications["issues"])
    if problems:
        raise MeasurementError(
            "adjudication file does not match the corpus:\n  " + "\n  ".join(problems)
        )

    rollup = roll_up(facts, adjudications["issues"])
    decision = decide(rollup, adjudications["decision_override"])
    renderer = render_markdown if args.format == "md" else render_json
    rendered = renderer(
        window=window,
        cohort=cohort,
        facts=facts,
        recorded=adjudications["issues"],
        rollup=rollup,
        decision=decision,
    )
    if args.out:
        args.out.write_text(rendered, encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
