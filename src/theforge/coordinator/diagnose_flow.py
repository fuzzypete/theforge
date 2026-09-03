"""``forge diagnose`` flow — root-cause discovery for symptom bugs.

This module is intentionally separate from the dev/review pipeline:

- Different state machine (DiagnosePhase, not Phase)
- Different prompt structure (find the cause vs. act on a known cause)
- Different success criterion (a confirmed cause + fix-success criterion,
  not a passing gate)

The flow does NOT run plan/dev/review phases.  It runs a single investigative
agent invocation, parses its output into a structured artifact, and lands
the artifact at an operator-configured destination so the issue becomes
fix-ready for a subsequent ``forge sprint`` run.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from theforge import detach
from theforge.config import DEFAULT_INVESTIGATION_TOOLS, ForgeConfig, ModelProfile
from theforge.config.model_identity import PHASE_DIAGNOSE
from theforge.coordinator.diagnose_evidence import (
    build_starting_evidence,
    parse_attached_evidence,
)
from theforge.coordinator.log_tee import get_worker_slug, set_worker_slug
from theforge.coordinator.util import _log as _progress_log
from theforge.diagnose_types import (
    DIAGNOSE_OUTPUT_DESTINATIONS,
    AbsentPremise,
    DiagnosePartialReason,
    DiagnosePhase,
    DiagnoseResult,
    DiagnoseState,
    DiagnosisArtifact,
    InspectedFile,
    PremiseVerdict,
    UncheckedPremise,
    render_already_resolved_markdown,
    render_artifact_markdown,
    upsert_diagnosis_section,
)
from theforge.shape_check.document import (
    parse_issue_document,
    render_issue_document,
    replace_section,
    with_section,
)
from theforge.shape_check.heuristics import (
    _diagnosis_cause_unknown,
    check_bug_missing_expected,
    check_bug_missing_observed,
    is_bug_format_issue,
)
from theforge.shape_check.issue_spec import BUG_SPEC, EXPECTED_SECTION, OBSERVED_SECTION
from theforge.shape_check.producer import require_conforming_body
from theforge.shape_check.types import ShapeVerdict
from theforge.task.diagnose_prompts import (
    DiagnoseParseOutcome,
    build_diagnose_prompt,
    build_diagnose_reformat_prompt,
    derive_issue_scope_requirement,
    parse_diagnose_output_result,
)

_log = logging.getLogger(__name__)

# ── Lazy runner slot ──────────────────────────────────────────────────
# Patch target for tests: theforge.coordinator.diagnose_flow.run_agent
run_agent = None

_DELEGATED_WAITING_FAILURE_CODE = "delegated_without_observed_outcome"
_DELEGATION_TOOL_NAMES: frozenset[str] = frozenset({"agent", "schedulewakeup"})
_WAITING_PLACEHOLDER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bstill waiting (?:on|for)\b", re.IGNORECASE),
    re.compile(
        r"\bwait(?:ing)? for the (?:investigation|exploration|sub-?agent)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bi['’]ll be notified when (?:it|they) complete", re.IGNORECASE),
)
_DEFERRED_OUTCOME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bi['’]ll report(?: the diagnosis| back)? once\b", re.IGNORECASE),
    re.compile(r"\bi['’]ll report(?: the diagnosis| back)? when\b", re.IGNORECASE),
    re.compile(r"\bno need to poll\b", re.IGNORECASE),
    re.compile(r"\b(?:results?|findings?|details?) to follow\b", re.IGNORECASE),
)
_TEXT_ONLY_DELEGATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsub-?agent\b", re.IGNORECASE),
    re.compile(r"\b(?:investigation|exploration) agent\b", re.IGNORECASE),
    re.compile(
        r"\b(?:delegat(?:e|ed|es|ing|ion)|dispatch(?:ed|es|ing)?|spawn(?:ed|s|ing)?)\b"
        r".{0,80}\b(?:sub-?agent|(?:investigation|exploration) agent)\b",
        re.IGNORECASE | re.DOTALL,
    ),
)


def _ensure_runner() -> None:
    global run_agent
    if run_agent is None:
        import theforge.runners as _r

        run_agent = _r.run_agent


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _generate_run_id() -> str:
    return os.urandom(6).hex()


def _delegation_tools_used(result: object) -> tuple[str, ...]:
    """Return delegation tool names observed in a runner tool trace."""
    trace = getattr(result, "tool_trace", ()) or ()
    used: list[str] = []
    for entry in trace:
        if not isinstance(entry, dict):
            continue
        tool = str(entry.get("tool", "") or "").strip()
        if tool.lower() in _DELEGATION_TOOL_NAMES and tool not in used:
            used.append(tool)
    return tuple(used)


def _has_delegated_placeholder_signal(output: str) -> bool:
    """Return True when the output reads like deferred delegated work."""
    return any(pattern.search(output) for pattern in _WAITING_PLACEHOLDER_PATTERNS) or any(
        pattern.search(output) for pattern in _DEFERRED_OUTCOME_PATTERNS
    )


def _ended_on_delegated_waiting_placeholder(result: object) -> str:
    """Describe a billed top-level turn that delegated and stopped on a status line.

    This is narrower than "unparseable diagnose output": the observed defect is a
    top-level investigation turn that successfully ends after spawning delegated
    work, while its own terminal text says that work is still in flight. A
    reformat retry cannot recover a diagnosis that was never emitted.
    """
    if not bool(getattr(result, "success", False)):
        return ""
    output = str(getattr(result, "output", "") or "").strip()
    if not output:
        return ""
    tools = _delegation_tools_used(result)
    if tools:
        return f"used {', '.join(tools)}"
    mentions_delegated_agent = any(
        pattern.search(output) for pattern in _TEXT_ONLY_DELEGATION_PATTERNS
    )
    if mentions_delegated_agent and _has_delegated_placeholder_signal(output):
        return "described delegated work in its own output"
    return ""


# ── Issue I/O via gh CLI ──────────────────────────────────────────────


# ── Baseline capture ──────────────────────────────────────────────────


def _capture_base_sha(project_root: Path) -> str:
    """Return the current HEAD SHA of the repo at ``project_root``.

    Empty string when ``project_root`` is not a git checkout — the diagnose
    flow still proceeds; the staleness check downstream simply has no
    baseline to compare against.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _hash_file_at_sha(path: str, sha: str, project_root: Path) -> str:
    """Return the sha256 hex digest of ``path`` at git ``sha``.

    Falls back to hashing the working-tree file when the path is not
    tracked at that SHA (returns "" when neither lookup succeeds). Empty
    return signals an inspected file we cannot baseline — the staleness
    check treats those as informational only.
    """
    if not sha or not path:
        return ""
    try:
        proc = subprocess.run(
            ["git", "show", f"{sha}:{path}"],
            capture_output=True,
            cwd=str(project_root),
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc is not None and proc.returncode == 0:
        return hashlib.sha256(proc.stdout).hexdigest()
    fs_path = project_root / path
    try:
        return hashlib.sha256(fs_path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _baseline_inspected_files(
    files: tuple[InspectedFile, ...], sha: str, project_root: Path
) -> tuple[InspectedFile, ...]:
    """Hash each inspected file against the baseline SHA."""
    if not files:
        return ()
    return tuple(
        InspectedFile(path=f.path, content_sha256=_hash_file_at_sha(f.path, sha, project_root))
        for f in files
    )


# ── Premise verification ──────────────────────────────────────────────
# A confirmed-cause diagnosis is only trustworthy if the code it describes
# still exists.  These helpers query git for the continued presence of the
# cited paths/patterns at the diagnosis baseline.  They fail *open*: an
# "already resolved" verdict is only returned when git positively shows the
# cited reference existed and was removed by a nameable commit.  A path token
# we cannot resolve (misparsed, never tracked) yields no deletion commit and
# is therefore ignored, never diverting a live diagnosis.

_UNIT_SEP = "\x1f"

# Candidate references inside a free-text ``affected_code_path`` field: a dotted
# filename (optionally with directories) plus an optional ``:locator`` — either a
# line number (``:142``) or a symbol name (``:buggy_func``). The symbol, when
# present, is a checkable premise: a diagnosis that pins the bug to a function
# that has been deleted from a still-present file must not land as live.
_PATH_REF_RE = re.compile(
    r"(?P<path>[\w./\-]*\w+\.[A-Za-z0-9]+)(?::(?P<locator>[A-Za-z_]\w*|\d+))?"
)
_LINE_LOCATOR_SYMBOL_RE = re.compile(
    r"^[\s,;()\[\]`'\"-]*(?:in\s+)?([A-Za-z_]\w*)\b",
)


def _path_exists_at_sha(path: str, sha: str, project_root: Path) -> bool:
    """Return True when ``path`` is a tracked blob at git ``sha``."""
    if not path or not sha:
        return False
    try:
        proc = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}:{path}"],
            capture_output=True,
            cwd=str(project_root),
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _pattern_present_at_sha(path: str, pattern: str, sha: str, project_root: Path) -> bool | None:
    """Return whether ``pattern`` appears in ``path`` at ``sha``.

    ``None`` when the file cannot be read at that SHA (cannot determine).
    """
    if not path or not sha:
        return None
    try:
        proc = subprocess.run(
            ["git", "show", f"{sha}:{path}"],
            capture_output=True,
            cwd=str(project_root),
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    text = proc.stdout.decode("utf-8", errors="replace")
    return pattern in text


def _find_deleting_commit(path: str, sha: str, project_root: Path) -> tuple[str, str] | None:
    """Return (commit_sha, summary) of the most recent commit that deleted ``path``.

    Reachable from ``sha``.  ``None`` when no deletion is recorded (the path
    was never tracked, or still exists) — the fail-open signal.
    """
    return _git_log_first(
        ["--diff-filter=D", f"--format=%H{_UNIT_SEP}%s", sha, "--", path],
        project_root,
    )


def _find_pattern_removing_commit(
    path: str, pattern: str, sha: str, project_root: Path
) -> tuple[str, str] | None:
    """Return (commit_sha, summary) of the commit that removed ``pattern`` from ``path``.

    Uses git's pickaxe (``-S``, fixed-string) to find the most recent commit
    reachable from ``sha`` that changed the number of occurrences of
    ``pattern`` in ``path``.  ``None`` when the pattern has no such history —
    the fail-open signal.
    """
    if not pattern:
        return None
    return _git_log_first(
        [f"-S{pattern}", f"--format=%H{_UNIT_SEP}%s", sha, "--", path],
        project_root,
    )


def _symbol_present_in_reachable_history(
    path: str, symbol: str, sha: str, project_root: Path
) -> bool:
    """Return True when ``symbol`` appears in ``path`` history reachable from ``sha``."""
    if not path or not symbol or not sha:
        return False
    try:
        proc = subprocess.run(
            [
                "git",
                "log",
                "-G",
                rf"\b{re.escape(symbol)}\b",
                "-1",
                "--format=%H",
                sha,
                "--",
                path,
            ],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _path_seen_in_history(path: str, sha: str, project_root: Path) -> bool:
    """Return True when ``path`` appears anywhere in history reachable from ``sha``."""
    if not path or not sha:
        return False
    try:
        proc = subprocess.run(
            ["git", "log", "-1", "--format=%H", sha, "--", path],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _git_log_first(extra_args: list[str], project_root: Path) -> tuple[str, str] | None:
    """Run ``git log -1`` with ``extra_args``; return (sha, summary) or None."""
    try:
        proc = subprocess.run(
            ["git", "log", "-1", *extra_args],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    line = proc.stdout.strip()
    if not line:
        return None
    commit, _, summary = line.partition(_UNIT_SEP)
    commit = commit.strip()
    if not commit:
        return None
    return commit, summary.strip()


def _extract_line_locator_symbol(suffix: str) -> str:
    """Return the symbol after a ``path:line`` citation when the tail is compact.

    We accept compact follow-on symbol shapes such as ``path:line, symbol`` and
    ``path:line in symbol``, not free prose like ``path:301, parse for
    background``. The symbol token must be followed only by
    punctuation/whitespace or the end of the citation.
    """
    match = _LINE_LOCATOR_SYMBOL_RE.match(suffix)
    if not match:
        return ""
    candidate = match.group(1).strip()
    trailing = suffix[match.end(1) :]
    if trailing and not re.match(r"^\s*(?:$|[.,;:)\]}`'\"-])", trailing):
        return ""
    return candidate


def _extract_affected_refs(affected_code_path: str) -> list[tuple[str, str]]:
    """Pull candidate ``(path, symbol)`` references out of a free-text field.

    ``symbol`` is the non-numeric locator following a ``:`` (a function/class
    name whose continued presence is checkable); it is empty for a bare path or
    a numeric ``:line`` locator, which anchors only on the file's existence.
    Order-preserving and deduped on the full ``(path, symbol)`` pair.
    """
    refs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    text = affected_code_path or ""
    for match in _PATH_REF_RE.finditer(text):
        path = match.group("path").strip().strip(".,;()[]`'\"")
        locator = (match.group("locator") or "").strip()
        symbol = locator if locator and not locator.isdigit() else ""
        if locator.isdigit():
            suffix = text[match.end() : match.end() + 80]
            symbol = _extract_line_locator_symbol(suffix)
        if not path:
            continue
        key = (path, symbol)
        if key in seen:
            continue
        seen.add(key)
        refs.append(key)
    return refs


def _should_ignore_untracked_bare_path(path: str, symbol: str) -> bool:
    """Return True for filename-like prose that should not become an audit premise.

    Bare unqualified filenames inside narrative prose are common in
    ``affected_code_path``. When they are absent at baseline and never appeared
    in reachable history, surfacing them as unchecked premises adds noise. Keep
    repo-qualified paths and symbol-bearing citations audit-visible.
    """
    return not symbol and "/" not in path


def verify_premise(artifact: DiagnosisArtifact, sha: str, project_root: Path) -> PremiseVerdict:
    """Verify the diagnosis's cited code still exists at baseline ``sha``.

    Returns a resolved verdict only when git positively confirms a cited file
    or premise pattern was removed by a nameable commit.  Fails open (unresolved)
    when there is no baseline SHA, or when absence cannot be attributed to a
    removing commit — this must never turn a live diagnosis into a false
    "already resolved".
    """
    unable_to_check: list[UncheckedPremise] = []
    if not sha:
        for anchor in artifact.premise_anchors:
            path = anchor.file.strip()
            pattern = anchor.pattern.strip()
            if path:
                unable_to_check.append(
                    UncheckedPremise(
                        file=path,
                        pattern=pattern,
                        reason="baseline SHA unavailable; premise not checked",
                    )
                )
        for path, symbol in _extract_affected_refs(artifact.affected_code_path):
            unable_to_check.append(
                UncheckedPremise(
                    file=path,
                    pattern=symbol,
                    reason="baseline SHA unavailable; affected code path not checked",
                )
            )
        return PremiseVerdict(resolved=False, unable_to_check=tuple(unable_to_check))

    absent: list[AbsentPremise] = []
    covered: set[str] = set()

    for anchor in artifact.premise_anchors:
        path = anchor.file.strip()
        pattern = anchor.pattern.strip()
        if not path:
            continue
        if not _path_exists_at_sha(path, sha, project_root):
            commit = _find_deleting_commit(path, sha, project_root)
            if commit:
                absent.append(
                    AbsentPremise(
                        file=path,
                        pattern="",
                        removing_commit=commit[0],
                        removing_summary=commit[1],
                    )
                )
                covered.add(path)
            else:
                unable_to_check.append(
                    UncheckedPremise(
                        file=path,
                        pattern="",
                        reason=(
                            "file absent at baseline but no removing commit could be identified"
                        ),
                    )
                )
            continue
        if pattern and _pattern_present_at_sha(path, pattern, sha, project_root) is False:
            commit = _find_pattern_removing_commit(path, pattern, sha, project_root)
            if commit:
                absent.append(
                    AbsentPremise(
                        file=path,
                        pattern=pattern,
                        removing_commit=commit[0],
                        removing_summary=commit[1],
                    )
                )
            else:
                unable_to_check.append(
                    UncheckedPremise(
                        file=path,
                        pattern=pattern,
                        reason=(
                            "pattern absent at baseline but no removing commit could be identified"
                        ),
                    )
                )

    # AC: a diagnosis that cites an affected code path must confirm the path
    # currently exists. Verify the file(s) named in affected_code_path — and,
    # when the citation pins the bug to a named symbol (``path:func``), that the
    # symbol itself still exists in the (possibly still-present) file. Only
    # report absence backed by a removing commit (fail open otherwise).
    for path, symbol in _extract_affected_refs(artifact.affected_code_path):
        if not _path_exists_at_sha(path, sha, project_root):
            if path in covered:
                continue
            commit = _find_deleting_commit(path, sha, project_root)
            if commit:
                absent.append(
                    AbsentPremise(
                        file=path,
                        pattern="",
                        removing_commit=commit[0],
                        removing_summary=commit[1],
                    )
                )
                covered.add(path)
            else:
                seen_in_history = _path_seen_in_history(path, sha, project_root)
                if not seen_in_history and _should_ignore_untracked_bare_path(path, symbol):
                    continue
                reason = (
                    "file absent at baseline but no removing commit could be identified"
                    if seen_in_history
                    else ("cited path absent at baseline and not present in reachable git history")
                )
                unable_to_check.append(
                    UncheckedPremise(
                        file=path,
                        pattern=symbol,
                        reason=reason,
                    )
                )
            continue
        # File is present; if the citation named a symbol that has since been
        # removed from it, the described bug can no longer reproduce there.
        if symbol and _pattern_present_at_sha(path, symbol, sha, project_root) is False:
            commit = _find_pattern_removing_commit(path, symbol, sha, project_root)
            if commit:
                absent.append(
                    AbsentPremise(
                        file=path,
                        pattern=symbol,
                        removing_commit=commit[0],
                        removing_summary=commit[1],
                    )
                )
            else:
                if _symbol_present_in_reachable_history(path, symbol, sha, project_root):
                    unable_to_check.append(
                        UncheckedPremise(
                            file=path,
                            pattern=symbol,
                            reason=(
                                "symbol absent at baseline but no removing commit "
                                "could be identified"
                            ),
                        )
                    )

    deduped_unchecked: list[UncheckedPremise] = []
    seen_unchecked: set[tuple[str, str, str]] = set()
    for premise in unable_to_check:
        key = (premise.file, premise.pattern, premise.reason)
        if key in seen_unchecked:
            continue
        seen_unchecked.add(key)
        deduped_unchecked.append(premise)
    return PremiseVerdict(
        resolved=bool(absent),
        absent=tuple(absent),
        unable_to_check=tuple(deduped_unchecked),
    )


def _unchecked_attached_premises(artifact: DiagnosisArtifact) -> tuple[UncheckedPremise, ...]:
    """Report premises the coordinator deliberately skipped on attached evidence."""
    reason = (
        "premise check skipped: diagnosis is anchored to attached cross-project evidence, "
        "so this checkout cannot verify the cited code safely"
    )
    unchecked: list[UncheckedPremise] = []
    seen: set[tuple[str, str]] = set()
    for anchor in artifact.premise_anchors:
        path = anchor.file.strip()
        pattern = anchor.pattern.strip()
        if not path:
            continue
        key = (path, pattern)
        if key in seen:
            continue
        seen.add(key)
        unchecked.append(UncheckedPremise(file=path, pattern=pattern, reason=reason))
    for path, symbol in _extract_affected_refs(artifact.affected_code_path):
        key = (path, symbol)
        if key in seen:
            continue
        seen.add(key)
        unchecked.append(UncheckedPremise(file=path, pattern=symbol, reason=reason))
    if not unchecked:
        unchecked.append(
            UncheckedPremise(
                file=artifact.affected_code_path.strip()
                or "affected code path described only in prose",
                pattern="",
                reason=(
                    f"{reason}; the diagnosis did not record any premise anchors or "
                    "file-like affected-code references"
                ),
            )
        )
    return tuple(unchecked)


def _gh_fetch_issue(number: int, project_root: Path) -> dict:
    """Fetch an issue's title, body, state, labels, and comments via ``gh``.

    Comments are part of the fetch because an issue filed by ``forge report``
    carries the observed run's artifacts *as comments* — the body only holds the
    manifest naming them. Without them the evidence that travelled with the
    report is unreachable and the diagnosis falls back to describing whichever
    checkout it happens to run in.
    """
    proc = subprocess.run(
        [
            "gh",
            "issue",
            "view",
            str(number),
            "--json",
            "number,title,body,state,labels,comments",
        ],
        capture_output=True,
        text=True,
        cwd=str(project_root),
        timeout=30,
    )
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(f"gh issue view #{number} failed: {err}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh issue view #{number} returned malformed JSON: {exc}") from exc


def _gh_post_comment(number: int, body: str, project_root: Path) -> str:
    """Post a comment on an issue via ``gh issue comment``.  Returns the URL."""
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(body)
        tmp_path = f.name
    try:
        proc = subprocess.run(
            [
                "gh",
                "issue",
                "comment",
                str(number),
                "--body-file",
                tmp_path,
            ],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=30,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(f"gh issue comment #{number} failed: {err}")
    return proc.stdout.strip()


def _gh_edit_body(number: int, new_body: str, project_root: Path) -> None:
    """Replace the body of an issue via ``gh issue edit --body-file``."""
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(new_body)
        tmp_path = f.name
    try:
        proc = subprocess.run(
            [
                "gh",
                "issue",
                "edit",
                str(number),
                "--body-file",
                tmp_path,
            ],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=30,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(f"gh issue edit #{number} failed: {err}")


# ── Audit emission ────────────────────────────────────────────────────


def _audit_dir(project_root: Path) -> Path:
    return project_root / ".forge" / "audits"


def diagnose_raw_output_path(
    project_root: Path, issue_number: int, run_id: str, *, attempt: int = 0
) -> Path:
    """Return the deterministic sidecar path for a run's complete agent output.

    Sits beside the run's audit YAML under ``.forge/audits`` and shares its
    ``diagnose-issue-{N}-{run_id}`` stem, so an operator holding an audit file can
    find the raw output without a search. ``attempt`` 0 is the investigation's own
    output; a reformat parse retry writes to its own numbered sidecar rather than
    overwriting attempt 0, because the original emission is the evidence that
    distinguishes an agent serialization defect from a parser defect.
    """
    stem = f"diagnose-issue-{issue_number}-{run_id}"
    suffix = ".raw.txt" if attempt <= 0 else f".raw.retry{attempt}.txt"
    return _audit_dir(project_root) / f"{stem}{suffix}"


def diagnose_fallback_output_path(
    project_root: Path, state: DiagnoseState, *, attempt: int = 0
) -> Path:
    """Return the second durable location for a run's complete agent output.

    Used when the audit sidecar cannot be written. Lives in the run's own log
    directory — already created and written to by this flow, and the place
    ``forge logs`` points an operator at — so a failure isolated to
    ``.forge/audits`` does not cost the recoverability guarantee.
    """
    slug = state.run_slug or f"diagnose-{state.issue_number}"
    suffix = ".raw.txt" if attempt <= 0 else f".raw.retry{attempt}.txt"
    return project_root / ".forge" / "logs" / slug / f"run-{state.run_id}{suffix}"


def _write_output_sidecar(
    state: DiagnoseState, project_root: Path, text: str, attempt: int
) -> tuple[Path | None, str]:
    """Write one agent emission durably. Returns ``(path, error)``.

    Tries the canonical audit sidecar, then the run-log fallback. ``error`` is
    empty exactly when the text reached disk, and names every location that
    refused it otherwise — a caller must not report a file that is not there.

    Records the written path on ``state.raw_output_paths`` so every emission this
    run paid for is enumerable from the audit, whether or not it parsed.
    """
    if not text:
        return None, ""
    candidates = (
        diagnose_raw_output_path(project_root, state.issue_number, state.run_id, attempt=attempt),
        diagnose_fallback_output_path(project_root, state, attempt=attempt),
    )
    errors: list[str] = []
    for path in candidates:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            errors.append(f"{path}: {exc}")
            continue
        if str(path) not in state.raw_output_paths:
            state.raw_output_paths.append(str(path))
        return path, ""
    return None, "failed to persist agent output (" + "; ".join(errors) + ")"


def persist_agent_output(
    state: DiagnoseState, project_root: Path, *, attempt: int = 0, emit=None
) -> Path | None:
    """Persist the agent's full output durably; record provenance on ``state``.

    The audit's ``raw_output_tail`` is bounded at 2000 characters — enough to
    glance at, not enough to recover from. A run that spent budget and produced
    content must persist that content in full: without it a failure record cannot
    distinguish an agent defect from a parser defect, and a substantively complete
    diagnosis dies with the process (#2055).

    Persistence is guaranteed rather than best-effort, by escalating through three
    locations until one accepts the content:

    1. the audit sidecar (``.forge/audits/diagnose-issue-N-<run>.raw.txt``),
    2. the run-log fallback (``.forge/logs/<slug>/run-<run>.raw.txt``),
    3. inline in the audit record itself, as ``agent.raw_output``.

    Tier 3 cannot silently lose the output: it is written by the same
    ``write_diagnose_audit`` call the operator reads the failure from, and if even
    that write fails the exception propagates out of the flow rather than being
    swallowed. So an operator either has the complete output or has a loud error —
    never a run that quietly consumed a paid-for investigation.

    ``state.raw_output_chars``/``_sha256`` describe whatever ``state.agent_output``
    currently holds regardless of which tier stored it, so the recorded digest
    always matches the recorded content.

    Returns the file path written, or None when the content is carried inline.
    """
    if not state.agent_output:
        return None
    state.raw_output_chars = len(state.agent_output)
    state.raw_output_sha256 = hashlib.sha256(state.agent_output.encode("utf-8")).hexdigest()
    path, error = _write_output_sidecar(state, project_root, state.agent_output, attempt)
    if path is None:
        # Every file location refused the write. Carry the complete output in the
        # audit record instead of degrading to the bounded tail — an audit that
        # omits what the agent actually said is the failure mode #2055 is about.
        state.raw_output_path = ""
        state.raw_output_error = error
        state.raw_output_inline = state.agent_output
        if emit is not None:
            emit(
                f"  [diagnose] WARNING: {error} — carrying the full output inline "
                "in the run audit (agent.raw_output) instead"
            )
        return None
    state.raw_output_error = ""
    state.raw_output_path = str(path)
    state.raw_output_inline = ""
    if emit is not None:
        emit(f"  [diagnose] full agent output persisted ({state.raw_output_chars} chars): {path}")
    return path


def _describe_persisted_agent_output(state: DiagnoseState) -> str:
    """Return where the complete investigative output survived for inspection."""
    if state.raw_output_path:
        return state.raw_output_path
    if state.raw_output_inline:
        return "inline in this run's audit record, field agent.raw_output"
    return "<no output to persist>"


def write_diagnose_audit(state: DiagnoseState, project_root: Path) -> Path:
    """Write the audit YAML for a diagnose run.

    Path: ``<project_root>/.forge/audits/diagnose-issue-{N}-{run_id}.yaml``
    Contents include phase transitions, agent cost/duration, the artifact (if
    any), and any landing location — enough for the operator to inspect what
    hypotheses were tested and what evidence was gathered.
    """
    audit_dir = _audit_dir(project_root)
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / f"diagnose-issue-{state.issue_number}-{state.run_id}.yaml"
    payload: dict = {
        "kind": "diagnose",
        "run_id": state.run_id,
        "issue_number": state.issue_number,
        "issue_title": state.issue_title,
        "started_at": state.started_at,
        "ended_at": _now_iso(),
        "final_phase": state.phase.name,
        "phase_transitions": [{"phase": name, "at": ts} for name, ts in state.phase_transitions],
        "agent": {
            "reported_success": state.agent_reported_success,
            "cost_usd": (
                round(state.agent_cost_usd, 6) if state.agent_cost_usd is not None else None
            ),
            "duration_s": round(state.agent_duration_s, 3),
            "failure_code": state.agent_failure_code,
            "raw_output_tail": state.agent_output[-2000:] if state.agent_output else "",
            # Full-output recoverability (#2055): the tail above is for a quick
            # glance; these fields point at the complete output on disk.
            "raw_output_path": state.raw_output_path,
            "raw_output_paths": list(state.raw_output_paths),
            "raw_output_chars": state.raw_output_chars,
            "raw_output_sha256": state.raw_output_sha256,
            "raw_output_error": state.raw_output_error,
            # Tier-3 carrier: non-empty only when no file location accepted the
            # write, so the complete output is still recoverable from this record.
            "raw_output": state.raw_output_inline,
            "parse_retries": [dict(entry) for entry in state.parse_retries],
        },
        "landing": {
            "destination": state.landing_destination,
            "location": state.landed_location,
        },
        "sub_investigations": list(state.sub_investigations),
        "starting_evidence": {
            "reference_labels": list(state.starting_evidence_labels),
            "chars": state.starting_evidence_chars,
            "declined_unqualified_refs": list(state.starting_evidence_declined),
        },
        # Non-empty ``source`` marks the run that read an observed run's evidence
        # off the issue instead of inspecting this checkout. ``read`` and
        # ``unreadable`` are the exact coverage the agent was shown, so a claim
        # of having read evidence is checkable against what was actually handed
        # over — and a diagnosis citing local state on this path is visible as a
        # contradiction rather than being indistinguishable from a local run.
        "attached_evidence": {
            "source": state.attached_evidence_source,
            "run_id": state.attached_evidence_run_id,
            "forge_version": state.attached_evidence_forge_version,
            "read": list(state.attached_evidence_read),
            "unreadable": list(state.attached_evidence_unreadable),
            "chars": state.attached_evidence_chars,
            # The local anchors this path deliberately does not produce.
            "local_baseline_skipped": bool(state.attached_evidence_source),
            "local_premise_check_skipped": bool(state.attached_evidence_source),
        },
        "issue_scope_requirement": {
            "symptom_is_categorical": state.issue_scope_is_categorical,
            "stated_scope": state.issue_scope_text,
        },
        "baseline": {
            "sha": state.baseline_sha,
            "captured_at": state.baseline_captured_at,
        },
        "already_resolved": state.already_resolved,
        "absent_premises": [
            {
                "file": a.file,
                "pattern": a.pattern,
                "removing_commit": a.removing_commit,
                "removing_summary": a.removing_summary,
            }
            for a in state.absent_premises
        ],
        "unchecked_premises": [
            {
                "file": a.file,
                "pattern": a.pattern,
                "reason": a.reason,
            }
            for a in state.unchecked_premises
        ],
        # Verification/provenance fields the artifact left unrecorded. These no
        # longer force a partial landing (#2797), so the record is the only
        # place the strict schema gap survives a run that landed runnable.
        "missing_metadata_fields": list(state.missing_metadata_fields),
        "error": state.error,
    }
    if state.artifact is not None:
        payload["artifact"] = _artifact_to_dict(state.artifact)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _artifact_to_dict(artifact: DiagnosisArtifact) -> dict:
    return {
        "issue_number": artifact.issue_number,
        "observed_symptom": artifact.observed_symptom,
        "reproduction_or_evidence": artifact.reproduction_or_evidence,
        "hypotheses": [
            {
                "statement": h.statement,
                "status": h.status,
                "evidence": h.evidence,
                "claim_verification": {
                    "verification_type": h.claim_verification.verification_type,
                    "detail": h.claim_verification.detail,
                },
                "evidence_provenance": {
                    "source_type": h.evidence_provenance.source_type,
                    "detail": h.evidence_provenance.detail,
                },
            }
            for h in artifact.hypotheses
        ],
        "confirmed_cause": artifact.confirmed_cause,
        "confirmed_cause_support": artifact.confirmed_cause_support,
        "confirmed_cause_verification": {
            "verification_type": artifact.confirmed_cause_verification.verification_type,
            "detail": artifact.confirmed_cause_verification.detail,
        },
        "confirmed_cause_support_provenance": {
            "source_type": artifact.confirmed_cause_support_provenance.source_type,
            "detail": artifact.confirmed_cause_support_provenance.detail,
        },
        "affected_code_path": artifact.affected_code_path,
        "fix_success_criterion": artifact.fix_success_criterion,
        "partial": artifact.partial,
        "partial_reason": artifact.partial_reason.value,
        "advisory_repair_proposal": artifact.advisory_repair_proposal,
        "notes": artifact.notes,
        "baseline_sha": artifact.baseline_sha,
        "baseline_captured_at": artifact.baseline_captured_at,
        "inspected_files": [
            {"path": f.path, "content_sha256": f.content_sha256} for f in artifact.inspected_files
        ],
        "premise_anchors": [
            {"file": a.file, "pattern": a.pattern} for a in artifact.premise_anchors
        ],
        "related_findings": [
            {"summary": r.summary, "related": r.related} for r in artifact.related_findings
        ],
        "symptom_scope_coverage": {
            "symptom_is_categorical": artifact.symptom_scope_coverage.symptom_is_categorical,
            "stated_scope": artifact.symptom_scope_coverage.stated_scope,
            "examined_locations": [
                {
                    "location": location.location,
                    "status": location.status,
                    "rationale": location.rationale,
                }
                for location in artifact.symptom_scope_coverage.examined_locations
            ],
        },
        "unchecked_premises": [
            {
                "file": premise.file,
                "pattern": premise.pattern,
                "reason": premise.reason,
            }
            for premise in artifact.unchecked_premises
        ],
    }


# ── Profile selection ─────────────────────────────────────────────────


def _build_diagnose_profile(
    config: ForgeConfig, *, timeout_seconds: float | None = None
) -> ModelProfile:
    """Derive the investigative-agent profile from existing config.

    Reuses ``preflight_profile`` as the base — it's already an
    investigation-shaped profile (sonnet-class capability) — and overlays the
    diagnose-specific budget and timeout from ``config.diagnose``.  This avoids
    inventing a parallel profile-loading path while still giving the diagnose
    flow its own resource envelope.

    The tool surface is overlaid rather than inherited: preflight's is
    deliberately narrowed to deny delegation it cannot be resumed for (#2346),
    and diagnose is a different job — reproducing a symptom is exactly the
    investigation that needs to run commands. It keeps the shared read-only
    investigation set, unchanged by preflight's narrowing.

    ``timeout_seconds``, if given, overrides ``config.diagnose.timeout_seconds``
    for this invocation only (e.g. ``forge diagnose --timeout``).
    """
    base = config.preflight_profile
    return dataclasses.replace(
        base,
        name="diagnose",
        allowed_tools=DEFAULT_INVESTIGATION_TOOLS,
        budget_usd=config.diagnose.budget_usd,
        timeout_seconds=(
            timeout_seconds if timeout_seconds is not None else config.diagnose.timeout_seconds
        ),
        phase=PHASE_DIAGNOSE,
    )


# ── Progress heartbeat ────────────────────────────────────────────────
#
# Interval between diagnose progress heartbeats. The investigative agent can
# run for the full diagnose timeout (default 600s); ``run_agent`` blocks for
# that whole window and the diagnose flow otherwise emits nothing to the
# console between the runner's "Starting diagnose" line and the result. That
# silence makes a live run indistinguishable from a hang, so we emit an
# elapsed-time line at PROGRESS level every interval.
_DIAGNOSE_HEARTBEAT_INTERVAL_S = 30.0


def _run_agent_with_heartbeat(
    *,
    prompt: str,
    profile: ModelProfile,
    working_dir: Path,
    secrets: dict[str, str] | None,
    heartbeat_interval_s: float | None = None,
    on_heartbeat: "callable | None" = None,
) -> "object":
    """Run the investigative agent, emitting a periodic progress heartbeat.

    ``run_agent`` blocks for the entire model run. We run it on a background
    thread and emit an elapsed-time line every ``heartbeat_interval_s`` seconds
    (always shown, not verbose-gated) so the operator can see the agent is still
    working rather than staring at a silent console for up to the full timeout.

    ``on_heartbeat`` is the sink each heartbeat line is routed through; it
    defaults to ``_progress_log`` (console only). ``run_diagnose_flow`` passes a
    sink that also tees the line into the per-run log so ``forge logs`` follows
    the heartbeat live.

    Any exception raised inside the agent thread is re-raised to the caller so
    the existing INVESTIGATE error handling is unchanged. Returns the
    ``AgentResult`` produced by ``run_agent``.
    """
    if heartbeat_interval_s is None:
        heartbeat_interval_s = _DIAGNOSE_HEARTBEAT_INTERVAL_S
    if on_heartbeat is None:
        on_heartbeat = _progress_log
    box: dict[str, object] = {}

    # The worker slug is thread-local; propagate it into the nested agent
    # thread so runner progress lines under ``forge diagnose --parallel`` stay
    # attributed to the same issue as the caller's heartbeat lines.
    caller_slug = get_worker_slug()

    def _run() -> None:
        if caller_slug:
            set_worker_slug(caller_slug)
        try:
            box["result"] = run_agent(
                prompt=prompt,
                profile=profile,
                working_dir=working_dir,
                secrets=secrets,
            )
        except BaseException as exc:  # noqa: BLE001 — re-raised in caller thread
            box["exc"] = exc

    start = time.monotonic()
    thread = threading.Thread(target=_run, name="diagnose-agent", daemon=True)
    thread.start()
    while thread.is_alive():
        thread.join(timeout=heartbeat_interval_s)
        if thread.is_alive():
            elapsed = int(time.monotonic() - start)
            on_heartbeat(
                f"  [diagnose] still investigating "
                f"({elapsed}s elapsed, timeout {int(profile.timeout_seconds)}s)"
            )

    if "exc" in box:
        raise box["exc"]  # type: ignore[misc]
    return box["result"]


# ── Landing strategies ────────────────────────────────────────────────


def _emit_dry_run(text: str) -> None:
    """Print operator-facing dry-run markdown to stdout.

    When a worker slug is set (``forge diagnose --parallel``), every line is
    prefixed with the slug so concurrent multi-line output stays attributable
    to its issue. Serial runs (no slug) print the text verbatim, unchanged from
    pre-parallel behavior.
    """
    slug = get_worker_slug()
    if not slug:
        print(text)
        return
    prefix = f"[{slug}] "
    print("\n".join(prefix + line for line in text.splitlines()))


def _resolve_output_destination(
    *,
    configured_destination: str,
    override_destination: str | None,
    issue_body: str,
    issue_labels: list[str],
) -> str:
    """Resolve the landing destination for this issue shape.

    Explicit CLI/config overrides win as-is. The shipped default remains
    body-section landing for bug-format issues because the shape gate reads the
    Diagnosis artifact from the body; non-bug issues default to comments so
    diagnose does not manufacture plan-in-body blockers on enhancement stories.
    """
    if override_destination is not None:
        return override_destination
    if configured_destination != "body_section":
        return configured_destination
    if is_bug_format_issue(issue_body, issue_labels):
        return "body_section"
    return "comment"


def _declared_diagnosis_verdict(
    artifact: DiagnosisArtifact,
    section: str,
    *,
    issue_requires_categorical_scope: bool,
) -> ShapeVerdict:
    """The lifecycle state the landed Diagnosis section is meant to produce.

    Read off the artifact and its own rendered section — not off the merged
    issue body the validator then evaluates — so a landing that moves the issue
    somewhere else is a mismatch rather than a restatement of the answer.
    """
    if artifact.lifecycle_blocking_missing_fields(
        issue_requires_categorical_scope=issue_requires_categorical_scope
    ):
        return ShapeVerdict.NEEDS_DIAGNOSIS
    if _diagnosis_cause_unknown(section):
        return ShapeVerdict.DIAGNOSIS_CAUSE_UNKNOWN
    return ShapeVerdict.RUNNABLE


def _ensure_bug_capture_sections(
    body: str,
    artifact: DiagnosisArtifact,
    issue_labels: list[str],
) -> str:
    """Add missing required bug capture sections before validating the landed body."""
    if not is_bug_format_issue(body, issue_labels):
        return body

    document = parse_issue_document(body, type_spec=BUG_SPEC)
    observed_text = (
        artifact.observed_symptom.strip() or "The reported bug occurs in the observed run."
    )
    expected_text = (
        artifact.fix_success_criterion.strip()
        or "The reported bug should not occur in the target repository."
    )
    if check_bug_missing_observed("", body, issue_labels) is not None:
        if document.section(OBSERVED_SECTION.key) is None:
            document = with_section(
                document,
                OBSERVED_SECTION.key,
                observed_text,
                type_spec=BUG_SPEC,
            )
        else:
            document = replace_section(document, OBSERVED_SECTION.key, observed_text)
    if check_bug_missing_expected("", body, issue_labels) is not None:
        if document.section(EXPECTED_SECTION.key) is None:
            document = with_section(
                document,
                EXPECTED_SECTION.key,
                expected_text,
                type_spec=BUG_SPEC,
            )
        else:
            document = replace_section(document, EXPECTED_SECTION.key, expected_text)
    return render_issue_document(document)


def _bug_capture_reasons(title: str, body: str, issue_labels: list[str]) -> tuple[str, ...]:
    """Return blocking bug-capture reason details diagnose must refuse up front."""
    reasons = []
    for check_fn in (check_bug_missing_observed, check_bug_missing_expected):
        reason = check_fn(title, body, issue_labels)
        if reason is not None:
            reasons.append(f"{reason.code}: {reason.detail}")
    return tuple(reasons)


def _land_artifact(
    state: DiagnoseState,
    artifact: DiagnosisArtifact,
    destination: str,
    project_root: Path,
    *,
    issue_labels: list[str] | None = None,
    dry_run: bool = False,
) -> str:
    """Land the artifact at the configured destination; return location string."""
    if destination not in DIAGNOSE_OUTPUT_DESTINATIONS:
        raise ValueError(
            f"Unknown diagnose output_destination {destination!r}; "
            f"valid: {sorted(DIAGNOSE_OUTPUT_DESTINATIONS)}"
        )
    section = render_artifact_markdown(
        artifact,
        issue_requires_categorical_scope=state.issue_scope_is_categorical,
    )

    if destination == "comment":
        if dry_run:
            _emit_dry_run(section)
            return "<dry-run: comment>"
        return _gh_post_comment(state.issue_number, section, project_root)

    if destination == "body_section":
        if dry_run:
            _emit_dry_run(section)
            return "<dry-run: body_section>"
        new_body = upsert_diagnosis_section(state.issue_body, section)
        new_body = _ensure_bug_capture_sections(new_body, artifact, list(issue_labels or []))
        # Validate before mutating. A Diagnosis section carrying file:line
        # citations reads as an implementation plan on a non-bug issue, so the
        # very landing that makes a bug fix-ready can make an enhancement
        # inadmissible (#2136). Refuse before the edit and name what the body
        # would fail, rather than writing it and leaving the next gate run to
        # find out.
        require_conforming_body(
            producer="forge-diagnose",
            title=state.issue_title,
            body=new_body,
            labels=list(issue_labels or []),
            declared=_declared_diagnosis_verdict(
                artifact,
                section,
                issue_requires_categorical_scope=state.issue_scope_is_categorical,
            ),
            previous_body=state.issue_body,
        )
        _gh_edit_body(state.issue_number, new_body, project_root)
        return f"issue #{state.issue_number} body updated"

    # pr_to_body — write a markdown file the operator can use to author a PR
    # against the issue body manually.  Issue bodies are not tracked in git,
    # so a literal PR isn't possible; this is the documented MVP behavior.
    out_dir = project_root / ".forge" / "diagnoses"
    out_path = out_dir / f"issue-{state.issue_number}.md"
    if dry_run:
        _emit_dry_run(section)
        return str(out_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    full = (
        f"<!-- Proposed body section for issue #{state.issue_number}. "
        f"Operator: review and apply via `gh issue edit`. -->\n\n{section}"
    )
    out_path.write_text(full, encoding="utf-8")
    return str(out_path)


# ── Parse retry ───────────────────────────────────────────────────────
# A completed investigation is the expensive part of a diagnose run; a YAML
# syntax slip in its serialization is the cheap part. Before #2055 the cheap
# failure destroyed the expensive result: one unterminated quoted scalar in an
# optional field discarded a confirmed cause, its hypotheses and its evidence,
# with no retry and only a 2000-character tail retained. This is the review
# path's per-reviewer parse retry (review_pool._build_review_retry_prompt) applied
# to the one flow that has no pool to fall back on.


def _accumulate_retry_cost(state: DiagnoseState, cost: float | None) -> None:
    """Fold one retry's cost into the run total without faking a measurement.

    An unmeasured retry (``None``) makes the run's *total* unknown: leaving the
    pre-retry number in place would record the retry's real spend as free. Once
    unknown, the total stays unknown — a later measured retry cannot recover the
    missing one.
    """
    if cost is None:
        state.agent_cost_usd = None
        return
    if state.agent_cost_usd is None:
        return
    state.agent_cost_usd += cost


def _retry_diagnose_parse(
    *,
    state: DiagnoseState,
    initial: DiagnoseParseOutcome,
    issue_number: int,
    partial: bool,
    config: ForgeConfig,
    project_root: Path,
    profile: ModelProfile,
    emit: "callable",
) -> DiagnoseParseOutcome:
    """Ask the agent to re-serialize its completed investigation, bounded by config.

    Runs at most ``config.retry.max_diagnose_parse_retries`` reformat-only
    invocations against the same profile and runner path as the investigation.
    The investigation itself is NOT re-run: each attempt hands the agent back its
    own verbatim output plus the parser's error and asks for serialization repair.

    Parsing stays strict on every attempt, and ``state.agent_output`` is replaced
    only by a retry output that actually parses — a failed repair must not
    overwrite the original investigation an operator may still want to salvage.
    Every attempt is appended to ``state.parse_retries`` so the audit shows what
    was retried, what it cost, and whether it worked.

    Returns the first parsing outcome, or the last failure when the budget is
    exhausted (``max_diagnose_parse_retries: 0`` returns ``initial`` untouched).

    Two cases return ``initial`` without spending anything, because there is no
    completed investigation to re-serialize: an agent that emitted nothing at all
    (the timeout/kill shape — the content floor downstream reports that as
    "produced no diagnosis"), and a run that has already breached its budget
    envelope, where more spend is exactly what the envelope forbids.
    """
    if not state.agent_output.strip():
        emit(
            "  [diagnose] agent emitted no output — no serialization to repair, "
            "skipping reformat retry"
        )
        return initial
    if (
        state.agent_cost_usd is not None
        and state.agent_cost_usd > config.diagnose.budget_usd * 1.05
    ):
        emit(
            f"  [diagnose] budget ${config.diagnose.budget_usd} already exceeded — "
            "skipping reformat retry"
        )
        return initial
    max_retries = max(0, int(config.retry.max_diagnose_parse_retries))
    parsed = initial
    for attempt in range(1, max_retries + 1):
        emit(
            f"  ↻ [diagnose] agent output failed YAML parse — reformat retry "
            f"{attempt}/{max_retries}: {parsed.error[:160]}"
        )
        record: dict = {
            "attempt": attempt,
            "parse_error": parsed.error,
            "cost_usd": None,
            "duration_s": 0.0,
            "success": False,
        }
        prompt = build_diagnose_reformat_prompt(
            original_output=state.agent_output, parse_error=parsed.error
        )
        t0 = time.monotonic()
        try:
            retry_result = _run_agent_with_heartbeat(
                prompt=prompt,
                profile=profile,
                working_dir=project_root,
                secrets=config.secrets,
                on_heartbeat=emit,
            )
        except Exception as exc:
            duration = time.monotonic() - t0
            state.agent_duration_s += duration
            record["duration_s"] = round(duration, 3)
            record["error"] = f"reformat retry crashed: {exc}"
            state.parse_retries.append(record)
            emit(f"  [diagnose] reformat retry {attempt} crashed: {exc}")
            break

        duration = time.monotonic() - t0
        state.agent_duration_s += duration
        record["duration_s"] = round(duration, 3)
        raw_cost = getattr(retry_result, "cost_usd", None)
        cost = float(raw_cost) if raw_cost is not None else None
        record["cost_usd"] = round(cost, 6) if cost is not None else None
        _accumulate_retry_cost(state, cost)
        retry_output = getattr(retry_result, "output", "") or ""
        record["output_chars"] = len(retry_output)
        record["output_tail"] = retry_output[-2000:]
        if not getattr(retry_result, "success", False):
            record["failure_code"] = getattr(retry_result, "failure_code", None)

        candidate = parse_diagnose_output_result(
            retry_output, issue_number=issue_number, partial=partial
        )
        if candidate.artifact is not None:
            # When the prior emission survives only inline (no file location took
            # it), carry it in this attempt's record before it is replaced — the
            # invariant is that a retry never destroys an earlier emission.
            if state.raw_output_inline:
                record["previous_output"] = state.raw_output_inline
            state.agent_output = retry_output
            record["success"] = True
            path = persist_agent_output(state, project_root, attempt=attempt, emit=emit)
            record["output_path"] = str(path) if path is not None else ""
            state.parse_retries.append(record)
            emit(
                f"  [diagnose] reformat retry {attempt} produced parseable YAML — "
                "investigation recovered"
            )
            return candidate
        # A repair that still does not parse cost real money and is itself
        # evidence (did the agent re-investigate? did it drop the field?), so it
        # gets its own sidecar rather than surviving only as a 2000-char tail.
        record["parse_error_after"] = candidate.error
        failed_path, failed_error = _write_output_sidecar(
            state, project_root, retry_output, attempt
        )
        record["output_path"] = str(failed_path) if failed_path is not None else ""
        if failed_error:
            # Same escalation as the investigation's own output: if no file
            # location took it, the attempt record carries it in full rather than
            # leaving only the bounded tail.
            record["output_path_error"] = failed_error
            record["output"] = retry_output
        state.parse_retries.append(record)
        parsed = candidate
    return parsed


# ── Public entry point ────────────────────────────────────────────────


def run_diagnose_flow(
    *,
    issue_number: int,
    config: ForgeConfig,
    project_root: Path,
    interactive: bool = False,
    output_destination: str | None = None,
    dry_run: bool = False,
    confirm_landing: "callable | None" = None,
    timeout_seconds: float | None = None,
) -> DiagnoseResult:
    """Run the diagnose flow for a single issue.

    Returns a DiagnoseResult.  The flow is autonomous by default; pass
    ``interactive=True`` to require operator confirmation before landing
    the artifact.  ``confirm_landing`` is the callable used to ask for
    confirmation (defaults to a stdin prompt); it is injectable so tests
    can drive interactive mode without TTY.

    Budget and timeout enforcement: the agent profile carries
    ``timeout_seconds`` and ``budget_usd`` from ``config.diagnose``.  Pass
    ``timeout_seconds`` to override the configured value for this run only
    (e.g. ``forge diagnose --timeout``).  The runner enforces wall-clock
    timeout; this function additionally treats a failed agent run as a
    partial-result outcome rather than a hard failure, returning whatever
    YAML the agent emitted before exit.
    """
    _ensure_runner()
    state = DiagnoseState(
        issue_number=issue_number,
        run_id=_generate_run_id(),
        started_at=_now_iso(),
    )

    # ── Register the run for live observability ────────────────────────
    # Give the foreground diagnose flow the same PID-file + per-run-log
    # registration detached sprint/run use, so ``forge status`` sees the live
    # run and ``forge logs <run_id>`` can follow it. Bespoke and lightweight —
    # no daemonize (diagnose stays foreground and, under --parallel, runs as
    # threads sharing one PID; each thread owns its own run_id/file/marker).
    slug = f"diagnose-{issue_number}"
    state.run_slug = slug
    log_fh = None
    try:
        log_dir = project_root / ".forge" / "logs" / slug
        log_dir.mkdir(parents=True, exist_ok=True)
        log_fh = open(  # noqa: SIM115 — closed in the finally below
            log_dir / f"run-{state.run_id}.log", "a", encoding="utf-8"
        )
    except OSError:
        log_fh = None
    detach.write_pid(state.run_id, slug, project_root)
    detach.write_diagnose_marker(state.run_id, project_root)

    def _write_log(line: str) -> None:
        if log_fh is None:
            return
        try:
            log_fh.write(line + "\n")
            log_fh.flush()
        except OSError:
            pass

    def _emit(line: str) -> None:
        """Tee a progress line into the per-run log AND the console."""
        _write_log(line)
        _progress_log(line)

    def _emit_phase(phase: DiagnosePhase) -> None:
        """Record a phase transition and tee it as a followable log line.

        The literal ``[forge] ▸ <PHASE>`` prefix is the exact token
        ``detach.read_run_status`` greps for, so ``forge status`` reports the
        live phase off the per-run log.
        """
        state.transition(phase, _now_iso())
        _write_log(f"[forge] ▸ {phase.name}")
        _progress_log(f"▸ {phase.name}")

    outcome = "completed"
    try:
        return _run_diagnose_flow_body(
            state=state,
            issue_number=issue_number,
            config=config,
            project_root=project_root,
            interactive=interactive,
            output_destination=output_destination,
            dry_run=dry_run,
            confirm_landing=confirm_landing,
            timeout_seconds=timeout_seconds,
            emit=_emit,
            emit_phase=_emit_phase,
        )
    except BaseException:
        outcome = "stopped"
        raise
    finally:
        if log_fh is not None:
            try:
                log_fh.close()
            except OSError:
                pass
        detach.remove_pid(state.run_id, project_root)
        detach.write_run_ended(state.run_id, project_root, outcome)


def _run_diagnose_flow_body(
    *,
    state: DiagnoseState,
    issue_number: int,
    config: ForgeConfig,
    project_root: Path,
    interactive: bool,
    output_destination: str | None,
    dry_run: bool,
    confirm_landing: "callable | None",
    timeout_seconds: float | None = None,
    emit: "callable",
    emit_phase: "callable",
) -> DiagnoseResult:
    """Body of :func:`run_diagnose_flow`, run under run registration.

    Split out so the caller can wrap the entire flow in a try/finally that
    tears down the PID file, diagnose marker, per-run log, and ``.ended``
    sentinel on any exit path. ``emit`` tees a progress line into the per-run
    log and the console; ``emit_phase`` records a phase transition and tees its
    followable ``[forge] ▸ <PHASE>`` marker line.
    """
    emit_phase(DiagnosePhase.INIT)

    configured_destination = output_destination or config.diagnose.output_destination
    if configured_destination not in DIAGNOSE_OUTPUT_DESTINATIONS:
        state.error = (
            f"Unknown output_destination {configured_destination!r}; "
            f"valid: {sorted(DIAGNOSE_OUTPUT_DESTINATIONS)}"
        )
        emit_phase(DiagnosePhase.FAILED)
        write_diagnose_audit(state, project_root)
        return DiagnoseResult(success=False, state=state, message=state.error)

    # ── FETCH ─────────────────────────────────────────────────────────
    emit_phase(DiagnosePhase.FETCH)
    try:
        issue = _gh_fetch_issue(issue_number, project_root)
    except Exception as exc:
        state.error = f"FETCH failed: {exc}"
        emit_phase(DiagnosePhase.FAILED)
        write_diagnose_audit(state, project_root)
        return DiagnoseResult(success=False, state=state, message=state.error)

    state.issue_title = str(issue.get("title", ""))
    state.issue_body = str(issue.get("body", ""))
    issue_labels = [
        str(lbl.get("name", "")).strip()
        for lbl in (issue.get("labels") or [])
        if isinstance(lbl, dict)
    ]
    destination = _resolve_output_destination(
        configured_destination=configured_destination,
        override_destination=output_destination,
        issue_body=state.issue_body,
        issue_labels=issue_labels,
    )

    # Operator-action issues describe a human deliverable, not a dev-runnable
    # defect. Diagnosis is a dev-cycle preparatory flow, so running it against an
    # operator-action issue is structurally meaningless and would land a
    # misleading "fix-ready" artifact. Refuse before spending any budget, naming
    # the issue number and the reason so the operator can distinguish this from a
    # shape-gate refusal without reading documentation.
    # Imported lazily: sprint.__init__ pulls in the runner → coordinator.engine
    # chain, so a module-level import risks a circular import during flow load.
    from theforge.sprint.shape_gate import OPERATOR_ACTION_LABEL

    label_names = {name.lower() for name in issue_labels}
    if OPERATOR_ACTION_LABEL.lower() in label_names:
        state.error = (
            f"Refusing to diagnose: issue #{issue_number} is labeled "
            f"'{OPERATOR_ACTION_LABEL}'. Operator-action issues describe human "
            "deliverables; they are not candidates for forge diagnose. Close "
            "manually when the action is done."
        )
        emit_phase(DiagnosePhase.FAILED)
        write_diagnose_audit(state, project_root)
        return DiagnoseResult(success=False, state=state, message=state.error)

    # ── ATTACHED EVIDENCE ─────────────────────────────────────────────
    # An issue filed by ``forge report`` from the project where the behavior was
    # observed carries that run's record with it: a manifest in the body,
    # artifacts in the comments. When it is there, it is the ONLY description of
    # the observed run this flow uses — the checkout executing the diagnosis is
    # a different runtime, and reading its state to answer a question about the
    # observed run inverts the answer rather than approximating it.
    attached = parse_attached_evidence(
        issue_body=state.issue_body,
        comments=issue.get("comments"),
    )
    if attached.is_present:
        state.attached_evidence_source = attached.observed_project or "an unnamed project"
        state.attached_evidence_run_id = attached.run_id
        state.attached_evidence_forge_version = attached.forge_version
        state.attached_evidence_read = list(attached.read_labels)
        state.attached_evidence_unreadable = list(attached.unreadable_labels)
        state.attached_evidence_chars = attached.chars
        emit(f"  evidence   : {attached.source_description}")
        emit(f"  reading    : {', '.join(attached.read_labels) or 'nothing (manifest only)'}")
        if attached.unreadable_labels:
            emit(f"  unreadable : {'; '.join(attached.unreadable_labels)}")

    # Capture baseline SHA at the moment the diagnosis is anchored. Done
    # post-FETCH so a fetch failure doesn't burn a baseline timestamp; done
    # pre-INVESTIGATE so the agent runs against (and the staleness check
    # later compares against) one known commit.
    #
    # Skipped entirely on the attached-evidence path: the baseline exists to
    # anchor a diagnosis to the commit it was made against, and this checkout's
    # HEAD is not that commit. Stamping it would put a local SHA (and local file
    # hashes, and a local premise check) on a diagnosis of a run that happened
    # somewhere else — the same substitution the attached path exists to stop.
    if not attached.is_present:
        state.baseline_sha = _capture_base_sha(project_root)
    state.baseline_captured_at = _now_iso()
    issue_state = str(issue.get("state", "OPEN")).upper()
    if issue_state != "OPEN":
        state.error = f"Issue #{issue_number} is {issue_state.lower()}; refusing to diagnose"
        emit_phase(DiagnosePhase.FAILED)
        write_diagnose_audit(state, project_root)
        return DiagnoseResult(success=False, state=state, message=state.error)

    bug_capture_reasons = _bug_capture_reasons(state.issue_title, state.issue_body, issue_labels)
    if bug_capture_reasons:
        state.error = (
            f"Refusing to diagnose: issue #{issue_number} is missing required bug capture "
            f"content. {' | '.join(bug_capture_reasons)}"
        )
        emit_phase(DiagnosePhase.FAILED)
        write_diagnose_audit(state, project_root)
        return DiagnoseResult(success=False, state=state, message=state.error)

    # ── STARTING EVIDENCE ─────────────────────────────────────────────
    # Deterministic pre-load: scan the issue body for references the operator
    # cited (run/sprint ids, branches, PR/issue numbers, audit paths) and hand
    # the agent bounded excerpts up front, so its budget is spent on
    # hypothesis-formation rather than rediscovering pointers already in the
    # body. Best-effort — an issue with no recognizable references yields an
    # empty block and the prompt is unchanged from pre-feature behavior.
    # Namespace-aware: only repo-qualified references (owner/repo#NNNN, github
    # URLs) are resolved. A bare #NNNN is reported as declined rather than
    # resolved against this checkout, which on a cross-project issue would hand
    # the agent a same-numbered local PR/issue as "evidence".
    #
    # Skipped when the issue carries attached evidence: that pre-load resolves
    # references against THIS checkout, which is precisely the substitution a
    # cross-project report must not receive.
    if attached.is_present:
        evidence_text = attached.text
    else:
        evidence = build_starting_evidence(
            issue_body=state.issue_body,
            project_root=project_root,
            self_issue_number=issue_number,
        )
        evidence_text = evidence.text
        state.starting_evidence_labels = list(evidence.reference_labels)
        state.starting_evidence_chars = len(evidence.text)
        state.starting_evidence_declined = list(evidence.declined_labels)
        if evidence.reference_labels:
            emit(
                f"  [diagnose] pre-loaded {len(evidence.reference_labels)} evidence "
                f"excerpt(s) from issue-body references: "
                f"{', '.join(evidence.reference_labels)}"
            )
        if evidence.declined_labels:
            emit(
                f"  [diagnose] declined {len(evidence.declined_labels)} unqualified "
                f"issue-body reference(s) (no repository named, not resolved against "
                f"this checkout): {', '.join(evidence.declined_labels)}"
            )
    (
        state.issue_scope_is_categorical,
        state.issue_scope_text,
    ) = derive_issue_scope_requirement(state.issue_title, state.issue_body)

    # ── INVESTIGATE ───────────────────────────────────────────────────
    emit_phase(DiagnosePhase.INVESTIGATE)
    profile = _build_diagnose_profile(config, timeout_seconds=timeout_seconds)
    mode = "interactive" if interactive else "autonomous"
    prompt = build_diagnose_prompt(
        issue_number=issue_number,
        title=state.issue_title,
        body=state.issue_body,
        mode=mode,
        starting_evidence=evidence_text,
        evidence_is_attached=attached.is_present,
    )

    t0 = time.monotonic()
    try:
        agent_result = _run_agent_with_heartbeat(
            prompt=prompt,
            profile=profile,
            working_dir=project_root,
            secrets=config.secrets,
            on_heartbeat=emit,
        )
    except Exception as exc:
        # The runner RETURNS timeout/budget as an AgentResult (success=False,
        # failure_code="timeout"); it does not raise. So this path is a genuine
        # crash, not a timeout — label it as such.
        state.error = f"INVESTIGATE crashed: {exc}"
        state.agent_duration_s = time.monotonic() - t0
        emit_phase(DiagnosePhase.FAILED)
        write_diagnose_audit(state, project_root)
        return DiagnoseResult(success=False, state=state, message=state.error)

    state.agent_duration_s = time.monotonic() - t0
    state.agent_output = getattr(agent_result, "output", "") or ""
    state.agent_reported_success = bool(getattr(agent_result, "success", False))
    # Capture the runner's machine-readable failure identifier (e.g. "timeout")
    # so terminal messaging and the audit trail can distinguish a timeout from a
    # budget breach or an empty completion. Normalize to str|None so a
    # non-string value never reaches the YAML audit serializer.
    _failure_code = getattr(agent_result, "failure_code", None)
    state.agent_failure_code = _failure_code if isinstance(_failure_code, str) else None
    # Preserve an unmeasured cost (None) faithfully — a run killed at timeout
    # still spent real money, and coercing None to 0.0 would record that spend
    # as free in the audit. Reconstruction happens in the runner; here we only
    # avoid destroying the cost-unknown signal it may hand us.
    _raw_cost = getattr(agent_result, "cost_usd", None)
    state.agent_cost_usd = float(_raw_cost) if _raw_cost is not None else None

    # Treat failure as "possibly partial" rather than abandon — per AC, a
    # diagnosis that can't be confirmed within bounds returns the partial
    # work for operator review rather than guessing.
    partial = not state.agent_reported_success

    # Budget guard — if the agent cost exceeded the configured budget, mark
    # the result as partial regardless of whether the agent reported success.
    # A cost-unknown run (None) cannot be shown to exceed the budget, so it is
    # not treated as a budget breach here. Re-evaluated after any parse retry,
    # whose spend counts against the same envelope.
    def _budget_exceeded() -> bool:
        return (
            state.agent_cost_usd is not None
            and state.agent_cost_usd > config.diagnose.budget_usd * 1.05
        )

    budget_exceeded = _budget_exceeded()
    if budget_exceeded:
        partial = True

    # ── PARSE ─────────────────────────────────────────────────────────
    emit_phase(DiagnosePhase.PARSE)
    # Persist the agent's complete output BEFORE the first parse attempt, so both
    # the success and the failure path leave the investigation recoverable from
    # disk. A run that spent budget and produced content must not have that
    # content exist only in this process's memory (#2055).
    persist_agent_output(state, project_root, emit=emit)

    parsed = parse_diagnose_output_result(
        state.agent_output, issue_number=issue_number, partial=partial
    )
    if parsed.artifact is None:
        delegated_observation = _ended_on_delegated_waiting_placeholder(agent_result)
        if delegated_observation:
            state.agent_failure_code = _DELEGATED_WAITING_FAILURE_CODE
            state.error = (
                "INVESTIGATE did not produce an observed outcome: the investigative "
                f"agent {delegated_observation} and ended its billed turn on a "
                "delegation placeholder "
                "instead of an investigation. Skipping YAML reformat retries because "
                "no diagnosis was emitted. Full agent output: "
                f"{_describe_persisted_agent_output(state)}. "
                f"Raw tail: {state.agent_output[-200:]!r}"
            )
            emit_phase(DiagnosePhase.FAILED)
            write_diagnose_audit(state, project_root)
            return DiagnoseResult(success=False, state=state, message=state.error)
        # The investigation is done; only its serialization is broken. Ask for the
        # same diagnosis, correctly encoded, before declaring the paid-for work
        # lost. Parsing of each retry stays strict.
        parsed = _retry_diagnose_parse(
            state=state,
            initial=parsed,
            issue_number=issue_number,
            partial=partial,
            config=config,
            project_root=project_root,
            profile=profile,
            emit=emit,
        )
        # Retry invocations spend against the same budget envelope.
        budget_exceeded = _budget_exceeded()
        if budget_exceeded:
            partial = True

    artifact = parsed.artifact
    if artifact is None:
        attempts = len(state.parse_retries)
        state.error = (
            f"PARSE failed: investigative agent did not emit a parseable YAML block "
            f"({parsed.error}) and {attempts} reformat retry attempt(s) did not "
            f"produce one. Full agent output: {_describe_persisted_agent_output(state)}. "
            f"Raw tail: {state.agent_output[-200:]!r}"
        )
        emit_phase(DiagnosePhase.FAILED)
        write_diagnose_audit(state, project_root)
        return DiagnoseResult(success=False, state=state, message=state.error)

    # Anchor the artifact to the baseline SHA captured at FETCH time, and
    # hash each agent-reported inspected file against that SHA so a later
    # `forge groom` can detect when the diagnosis has gone stale relative
    # to the current base branch.
    #
    # On the attached-evidence path there is no such anchor to stamp: the
    # inspected paths name the observed project, and hashing same-named files
    # out of this checkout would attach local content digests to a diagnosis of
    # a run that never touched them. The paths are carried as the agent reported
    # them, unhashed.
    inspected_with_hashes = (
        artifact.inspected_files
        if attached.is_present
        else _baseline_inspected_files(artifact.inspected_files, state.baseline_sha, project_root)
    )
    artifact = dataclasses.replace(
        artifact,
        baseline_sha=state.baseline_sha,
        baseline_captured_at=state.baseline_captured_at,
        inspected_files=inspected_with_hashes,
    )
    state.artifact = artifact

    # Content floor — an artifact that parsed but carries no substantive field
    # is a failure to diagnose, not a partial diagnosis. This is the shape a
    # killed/timed-out agent emits (empty output still parses to a non-None
    # all-empty dict). Landing it would pollute the issue body with
    # structurally-complete-but-content-empty scaffolding and report "partial
    # diagnosis landed" when there is nothing to review. Fail without mutating
    # any operator-visible state.
    if not artifact.has_substantive_content():
        # Name the actual terminal cause rather than lumping timeout/budget/empty
        # together (#1595). The runner returns "timeout" as a failure_code; a
        # budget breach is detected above; anything else is an empty completion.
        if state.agent_failure_code == "timeout":
            cause = f"timed out after {int(profile.timeout_seconds)}s"
        elif budget_exceeded:
            cause = f"exceeded budget ${config.diagnose.budget_usd}"
        else:
            cause = "completed without emitting a diagnosis"
        state.error = (
            f"INVESTIGATE produced no diagnosis: the investigative agent {cause} "
            "without emitting any substantive content. Nothing landed; issue body "
            "left untouched."
        )
        emit_phase(DiagnosePhase.FAILED)
        write_diagnose_audit(state, project_root)
        return DiagnoseResult(success=False, state=state, message=state.error)

    # ── VERIFY_PREMISE ────────────────────────────────────────────────
    # A diagnosis is only trustworthy if the code it describes still exists.
    # Before landing any confirmed cause, verify the cited paths/patterns are
    # present at the baseline. If a premise was removed by a later commit, the
    # described symptom cannot reproduce against code that is gone — report
    # "appears already resolved" (naming the removing commit) and write no
    # confirmed-cause body, rather than manufacturing a live diagnosis.
    #
    # Not run on the attached-evidence path. The check asks git "does the cited
    # code still exist *here*", and "here" is the wrong repository: a path this
    # project never had, or removed for unrelated reasons, would report a live
    # cross-project defect as already resolved and suppress the evidence-based
    # diagnosis entirely. With no local premise to check, the fail-open
    # direction is to land the diagnosis.
    emit_phase(DiagnosePhase.VERIFY_PREMISE)
    verdict = (
        PremiseVerdict(
            resolved=False,
            unable_to_check=_unchecked_attached_premises(artifact),
        )
        if attached.is_present
        else verify_premise(artifact, state.baseline_sha, project_root)
    )
    if verdict.unable_to_check:
        state.unchecked_premises = verdict.unable_to_check
        artifact = dataclasses.replace(artifact, unchecked_premises=verdict.unable_to_check)
        state.artifact = artifact
    if verdict.resolved:
        state.already_resolved = True
        state.absent_premises = verdict.absent
        emit_phase(DiagnosePhase.ALREADY_RESOLVED)
        report = render_already_resolved_markdown(
            issue_number=issue_number,
            baseline_sha=state.baseline_sha,
            absent=verdict.absent,
            unable_to_check=verdict.unable_to_check,
        )
        if dry_run:
            _emit_dry_run(report)
        first = verdict.absent[0]
        extra = f" (+{len(verdict.absent) - 1} more)" if len(verdict.absent) > 1 else ""
        target = f"{first.file}" + (f":{first.pattern}" if first.pattern else "")
        message = (
            f"Issue #{issue_number} appears already resolved — premise `{target}` "
            f"removed by commit {first.removing_commit[:12]}{extra}. "
            "No confirmed-cause diagnosis written."
        )
        write_diagnose_audit(state, project_root)
        return DiagnoseResult(success=False, state=state, message=message)

    # If essential fields are missing OR the run breached its budget/timeout
    # envelope, return the partial work for operator review rather than landing
    # a misleading "fix-ready" artifact. Name the specific reason when known.
    #
    # "Essential" is the lifecycle-blocking subset, not the strict schema list:
    # an artifact whose only gap is unrecorded verification metadata renders a
    # Diagnosis section the shape gate reads as RUNNABLE, so declaring it
    # NEEDS_DIAGNOSIS refuses the write against a body that does not occupy the
    # declared state and discards the whole investigation (#2797). The strict
    # list still reaches the audit via ``missing_metadata_fields``.
    lifecycle_blocking_fields = artifact.lifecycle_blocking_missing_fields(
        issue_requires_categorical_scope=state.issue_scope_is_categorical
    )
    state.missing_metadata_fields = artifact.missing_verification_metadata_fields(
        issue_requires_categorical_scope=state.issue_scope_is_categorical
    )
    partial_requires_partial_landing = (
        budget_exceeded or state.agent_failure_code == "timeout" or bool(lifecycle_blocking_fields)
    )
    if partial_requires_partial_landing:
        if budget_exceeded:
            partial_phase = DiagnosePhase.BUDGET_EXCEEDED
            partial_reason = DiagnosePartialReason.BUDGET_EXCEEDED
        elif state.agent_failure_code == "timeout":
            partial_phase = DiagnosePhase.TIMEOUT_PARTIAL
            partial_reason = DiagnosePartialReason.TIMEOUT
        elif artifact.confirmed_cause.strip():
            # A cause was confirmed; what's missing is some other lifecycle
            # field (scope coverage, reproduction, etc). Distinct from the
            # honest-no-cause case below so an operator can tell "ready to
            # sprint after a small gap" from "re-run needed" without opening
            # the landed body (#2803).
            partial_phase = DiagnosePhase.CAUSE_FOUND_PARTIAL
            partial_reason = DiagnosePartialReason.CAUSE_FOUND_INCOMPLETE
        else:
            partial_phase = DiagnosePhase.NO_CAUSE_FOUND
            partial_reason = DiagnosePartialReason.NO_CAUSE_FOUND
        artifact = dataclasses.replace(
            artifact,
            partial=True,
            partial_reason=partial_reason,
        )
        state.artifact = artifact
        if interactive and confirm_landing is None:
            confirm_landing = _stdin_confirm
        if interactive and confirm_landing is not None:
            if not _confirm_landing_with_scope_requirement(
                confirm_landing,
                artifact,
                issue_requires_categorical_scope=state.issue_scope_is_categorical,
            ):
                artifact = dataclasses.replace(
                    artifact, partial_reason=DiagnosePartialReason.DISCARDED
                )
                state.artifact = artifact
                emit_phase(DiagnosePhase.DISCARDED)
                msg = "Partial artifact — operator declined to land. Audit written."
                write_diagnose_audit(state, project_root)
                return DiagnoseResult(success=False, state=state, message=msg)
        emit_phase(partial_phase)
        try:
            location = _land_artifact(
                state,
                artifact,
                destination,
                project_root,
                issue_labels=issue_labels,
                dry_run=dry_run,
            )
            state.landing_destination = destination
            state.landed_location = location
        except Exception as exc:
            state.error = f"LAND failed: {exc}"
            write_diagnose_audit(state, project_root)
            return DiagnoseResult(success=False, state=state, message=state.error)
        write_diagnose_audit(state, project_root)
        # Name the actual cause in the operator message rather than a generic
        # "Partial diagnosis landed" (#1595).
        if budget_exceeded:
            cause_label = f"budget exceeded (${config.diagnose.budget_usd})"
        elif state.agent_failure_code == "timeout":
            cause_label = f"timed out after {int(profile.timeout_seconds)}s"
        elif lifecycle_blocking_fields == ("symptom_scope_coverage",):
            cause_label = "diagnosis scope-coverage incomplete"
        elif artifact.confirmed_cause.strip():
            cause_label = "cause found, diagnosis otherwise incomplete"
        else:
            cause_label = "no cause found"
        return DiagnoseResult(
            success=False,
            state=state,
            message=f"Partial diagnosis landed ({cause_label}) — operator review required",
        )

    if artifact.partial:
        artifact = dataclasses.replace(
            artifact,
            partial=False,
            partial_reason=DiagnosePartialReason.UNCLASSIFIED,
        )
        state.artifact = artifact

    # ── LAND ──────────────────────────────────────────────────────────
    emit_phase(DiagnosePhase.LAND)
    if interactive and confirm_landing is None:
        confirm_landing = _stdin_confirm
    if interactive and confirm_landing is not None:
        if not _confirm_landing_with_scope_requirement(
            confirm_landing,
            artifact,
            issue_requires_categorical_scope=state.issue_scope_is_categorical,
        ):
            artifact = dataclasses.replace(
                artifact, partial_reason=DiagnosePartialReason.DISCARDED
            )
            state.artifact = artifact
            emit_phase(DiagnosePhase.DISCARDED)
            msg = "Operator declined to land artifact. Audit written."
            write_diagnose_audit(state, project_root)
            return DiagnoseResult(success=False, state=state, message=msg)

    try:
        location = _land_artifact(
            state,
            artifact,
            destination,
            project_root,
            issue_labels=issue_labels,
            dry_run=dry_run,
        )
    except Exception as exc:
        state.error = f"LAND failed: {exc}"
        emit_phase(DiagnosePhase.FAILED)
        write_diagnose_audit(state, project_root)
        return DiagnoseResult(success=False, state=state, message=state.error)

    state.landing_destination = destination
    state.landed_location = location
    emit_phase(DiagnosePhase.DONE)
    write_diagnose_audit(state, project_root)
    message = f"Diagnosis landed at {location}"
    if state.missing_metadata_fields:
        # The landing is runnable, but say what the artifact never recorded so
        # the gap is visible without opening the audit.
        message += " — verification metadata unrecorded: " + ", ".join(
            state.missing_metadata_fields
        )
    return DiagnoseResult(
        success=True,
        state=state,
        message=message,
    )


def _stdin_confirm(
    artifact: DiagnosisArtifact, *, issue_requires_categorical_scope: bool = False
) -> bool:
    """Default interactive-mode confirmation: prompt on stdin."""
    print()
    print(
        render_artifact_markdown(
            artifact,
            issue_requires_categorical_scope=issue_requires_categorical_scope,
        )
    )
    print()
    try:
        answer = input("Land this diagnosis? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _confirm_landing_with_scope_requirement(
    confirm_landing: "callable",
    artifact: DiagnosisArtifact,
    *,
    issue_requires_categorical_scope: bool,
) -> bool:
    """Call the landing confirmer, preserving one-arg test hooks."""
    try:
        parameters = inspect.signature(confirm_landing).parameters.values()
    except (TypeError, ValueError):
        parameters = ()

    accepts_scope_requirement = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or parameter.name == "issue_requires_categorical_scope"
        for parameter in parameters
    )
    if accepts_scope_requirement:
        return bool(
            confirm_landing(
                artifact,
                issue_requires_categorical_scope=issue_requires_categorical_scope,
            )
        )
    return bool(confirm_landing(artifact))
