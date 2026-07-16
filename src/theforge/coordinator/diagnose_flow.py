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
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from theforge.config import ForgeConfig, ModelProfile
from theforge.diagnose_types import (
    DIAGNOSE_OUTPUT_DESTINATIONS,
    AbsentPremise,
    DiagnosePhase,
    DiagnoseResult,
    DiagnoseState,
    DiagnosisArtifact,
    InspectedFile,
    PremiseVerdict,
    render_already_resolved_markdown,
    render_artifact_markdown,
    upsert_diagnosis_section,
)
from theforge.task.diagnose_prompts import build_diagnose_prompt, parse_diagnose_output

_log = logging.getLogger(__name__)

# ── Lazy runner slot ──────────────────────────────────────────────────
# Patch target for tests: theforge.coordinator.diagnose_flow.run_agent
run_agent = None


def _ensure_runner() -> None:
    global run_agent
    if run_agent is None:
        import theforge.runners as _r

        run_agent = _r.run_agent


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _generate_run_id() -> str:
    return os.urandom(6).hex()


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
_PATH_REF_RE = re.compile(r"([\w./\-]*\w+\.[A-Za-z0-9]+)(?::([A-Za-z_]\w*))?")


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


def _extract_affected_refs(affected_code_path: str) -> list[tuple[str, str]]:
    """Pull candidate ``(path, symbol)`` references out of a free-text field.

    ``symbol`` is the non-numeric locator following a ``:`` (a function/class
    name whose continued presence is checkable); it is empty for a bare path or
    a numeric ``:line`` locator, which anchors only on the file's existence.
    Order-preserving and deduped on the full ``(path, symbol)`` pair.
    """
    refs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for path_tok, symbol_tok in _PATH_REF_RE.findall(affected_code_path or ""):
        path = path_tok.strip().strip(".,;()[]`'\"")
        symbol = symbol_tok.strip()
        if not path:
            continue
        key = (path, symbol)
        if key in seen:
            continue
        seen.add(key)
        refs.append(key)
    return refs


def verify_premise(artifact: DiagnosisArtifact, sha: str, project_root: Path) -> PremiseVerdict:
    """Verify the diagnosis's cited code still exists at baseline ``sha``.

    Returns a resolved verdict only when git positively confirms a cited file
    or premise pattern was removed by a nameable commit.  Fails open (unresolved)
    when there is no baseline SHA, or when absence cannot be attributed to a
    removing commit — this must never turn a live diagnosis into a false
    "already resolved".
    """
    if not sha:
        return PremiseVerdict(resolved=False)

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

    return PremiseVerdict(resolved=bool(absent), absent=tuple(absent))


def _gh_fetch_issue(number: int, project_root: Path) -> dict:
    """Fetch an issue's title, body, and state via ``gh issue view``."""
    proc = subprocess.run(
        [
            "gh",
            "issue",
            "view",
            str(number),
            "--json",
            "number,title,body,state",
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
            "cost_usd": (
                round(state.agent_cost_usd, 6) if state.agent_cost_usd is not None else None
            ),
            "duration_s": round(state.agent_duration_s, 3),
            "raw_output_tail": state.agent_output[-2000:] if state.agent_output else "",
        },
        "landing": {
            "destination": state.landing_destination,
            "location": state.landed_location,
        },
        "sub_investigations": list(state.sub_investigations),
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
            }
            for h in artifact.hypotheses
        ],
        "confirmed_cause": artifact.confirmed_cause,
        "affected_code_path": artifact.affected_code_path,
        "fix_success_criterion": artifact.fix_success_criterion,
        "partial": artifact.partial,
        "notes": artifact.notes,
        "baseline_sha": artifact.baseline_sha,
        "baseline_captured_at": artifact.baseline_captured_at,
        "inspected_files": [
            {"path": f.path, "content_sha256": f.content_sha256} for f in artifact.inspected_files
        ],
        "premise_anchors": [
            {"file": a.file, "pattern": a.pattern} for a in artifact.premise_anchors
        ],
    }


# ── Profile selection ─────────────────────────────────────────────────


def _build_diagnose_profile(config: ForgeConfig) -> ModelProfile:
    """Derive the investigative-agent profile from existing config.

    Reuses ``preflight_profile`` as the base — it's already an
    investigation-shaped profile (read-only tools, sonnet-class capability) —
    and overlays the diagnose-specific budget and timeout from
    ``config.diagnose``.  This avoids inventing a parallel profile-loading
    path while still giving the diagnose flow its own resource envelope.
    """
    base = config.preflight_profile
    return dataclasses.replace(
        base,
        name="diagnose",
        budget_usd=config.diagnose.budget_usd,
        timeout_seconds=config.diagnose.timeout_seconds,
        phase="diagnose",
    )


# ── Landing strategies ────────────────────────────────────────────────


def _land_artifact(
    state: DiagnoseState,
    artifact: DiagnosisArtifact,
    destination: str,
    project_root: Path,
    *,
    dry_run: bool = False,
) -> str:
    """Land the artifact at the configured destination; return location string."""
    if destination not in DIAGNOSE_OUTPUT_DESTINATIONS:
        raise ValueError(
            f"Unknown diagnose output_destination {destination!r}; "
            f"valid: {sorted(DIAGNOSE_OUTPUT_DESTINATIONS)}"
        )
    section = render_artifact_markdown(artifact)

    if destination == "comment":
        if dry_run:
            print(section)
            return "<dry-run: comment>"
        return _gh_post_comment(state.issue_number, section, project_root)

    if destination == "body_section":
        if dry_run:
            print(section)
            return "<dry-run: body_section>"
        new_body = upsert_diagnosis_section(state.issue_body, section)
        _gh_edit_body(state.issue_number, new_body, project_root)
        return f"issue #{state.issue_number} body updated"

    # pr_to_body — write a markdown file the operator can use to author a PR
    # against the issue body manually.  Issue bodies are not tracked in git,
    # so a literal PR isn't possible; this is the documented MVP behavior.
    out_dir = project_root / ".forge" / "diagnoses"
    out_path = out_dir / f"issue-{state.issue_number}.md"
    if dry_run:
        print(section)
        return str(out_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    full = (
        f"<!-- Proposed body section for issue #{state.issue_number}. "
        f"Operator: review and apply via `gh issue edit`. -->\n\n{section}"
    )
    out_path.write_text(full, encoding="utf-8")
    return str(out_path)


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
) -> DiagnoseResult:
    """Run the diagnose flow for a single issue.

    Returns a DiagnoseResult.  The flow is autonomous by default; pass
    ``interactive=True`` to require operator confirmation before landing
    the artifact.  ``confirm_landing`` is the callable used to ask for
    confirmation (defaults to a stdin prompt); it is injectable so tests
    can drive interactive mode without TTY.

    Budget and timeout enforcement: the agent profile carries
    ``timeout_seconds`` and ``budget_usd`` from ``config.diagnose``.  The
    runner enforces wall-clock timeout; this function additionally treats
    a failed agent run as a partial-result outcome rather than a hard
    failure, returning whatever YAML the agent emitted before exit.
    """
    _ensure_runner()
    state = DiagnoseState(
        issue_number=issue_number,
        run_id=_generate_run_id(),
        started_at=_now_iso(),
    )
    state.transition(DiagnosePhase.INIT, _now_iso())

    destination = output_destination or config.diagnose.output_destination
    if destination not in DIAGNOSE_OUTPUT_DESTINATIONS:
        state.error = (
            f"Unknown output_destination {destination!r}; "
            f"valid: {sorted(DIAGNOSE_OUTPUT_DESTINATIONS)}"
        )
        state.transition(DiagnosePhase.FAILED, _now_iso())
        write_diagnose_audit(state, project_root)
        return DiagnoseResult(success=False, state=state, message=state.error)

    # ── FETCH ─────────────────────────────────────────────────────────
    state.transition(DiagnosePhase.FETCH, _now_iso())
    try:
        issue = _gh_fetch_issue(issue_number, project_root)
    except Exception as exc:
        state.error = f"FETCH failed: {exc}"
        state.transition(DiagnosePhase.FAILED, _now_iso())
        write_diagnose_audit(state, project_root)
        return DiagnoseResult(success=False, state=state, message=state.error)

    state.issue_title = str(issue.get("title", ""))
    state.issue_body = str(issue.get("body", ""))
    # Capture baseline SHA at the moment the diagnosis is anchored. Done
    # post-FETCH so a fetch failure doesn't burn a baseline timestamp; done
    # pre-INVESTIGATE so the agent runs against (and the staleness check
    # later compares against) one known commit.
    state.baseline_sha = _capture_base_sha(project_root)
    state.baseline_captured_at = _now_iso()
    issue_state = str(issue.get("state", "OPEN")).upper()
    if issue_state != "OPEN":
        state.error = f"Issue #{issue_number} is {issue_state.lower()}; refusing to diagnose"
        state.transition(DiagnosePhase.FAILED, _now_iso())
        write_diagnose_audit(state, project_root)
        return DiagnoseResult(success=False, state=state, message=state.error)

    # ── INVESTIGATE ───────────────────────────────────────────────────
    state.transition(DiagnosePhase.INVESTIGATE, _now_iso())
    profile = _build_diagnose_profile(config)
    mode = "interactive" if interactive else "autonomous"
    prompt = build_diagnose_prompt(
        issue_number=issue_number,
        title=state.issue_title,
        body=state.issue_body,
        mode=mode,
    )

    t0 = time.monotonic()
    try:
        agent_result = run_agent(
            prompt=prompt,
            profile=profile,
            working_dir=project_root,
            secrets=config.secrets,
        )
    except Exception as exc:
        state.error = f"INVESTIGATE failed: {exc}"
        state.agent_duration_s = time.monotonic() - t0
        state.transition(DiagnosePhase.FAILED, _now_iso())
        write_diagnose_audit(state, project_root)
        return DiagnoseResult(success=False, state=state, message=state.error)

    state.agent_duration_s = time.monotonic() - t0
    state.agent_output = getattr(agent_result, "output", "") or ""
    # Preserve an unmeasured cost (None) faithfully — a run killed at timeout
    # still spent real money, and coercing None to 0.0 would record that spend
    # as free in the audit. Reconstruction happens in the runner; here we only
    # avoid destroying the cost-unknown signal it may hand us.
    _raw_cost = getattr(agent_result, "cost_usd", None)
    state.agent_cost_usd = float(_raw_cost) if _raw_cost is not None else None

    # Treat failure as "possibly partial" rather than abandon — per AC, a
    # diagnosis that can't be confirmed within bounds returns the partial
    # work for operator review rather than guessing.
    partial = not bool(getattr(agent_result, "success", False))

    # Budget guard — if the agent cost exceeded the configured budget, mark
    # the result as partial regardless of whether the agent reported success.
    # A cost-unknown run (None) cannot be shown to exceed the budget, so it is
    # not treated as a budget breach here.
    budget_exceeded = (
        state.agent_cost_usd is not None
        and state.agent_cost_usd > config.diagnose.budget_usd * 1.05
    )
    if budget_exceeded:
        partial = True

    # ── PARSE ─────────────────────────────────────────────────────────
    state.transition(DiagnosePhase.PARSE, _now_iso())
    artifact = parse_diagnose_output(
        state.agent_output, issue_number=issue_number, partial=partial
    )
    if artifact is None:
        state.error = (
            f"PARSE failed: investigative agent did not emit a parseable YAML block. "
            f"Raw tail: {state.agent_output[-200:]!r}"
        )
        state.transition(DiagnosePhase.FAILED, _now_iso())
        write_diagnose_audit(state, project_root)
        return DiagnoseResult(success=False, state=state, message=state.error)

    # Anchor the artifact to the baseline SHA captured at FETCH time, and
    # hash each agent-reported inspected file against that SHA so a later
    # `forge groom` can detect when the diagnosis has gone stale relative
    # to the current base branch.
    inspected_with_hashes = _baseline_inspected_files(
        artifact.inspected_files, state.baseline_sha, project_root
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
        state.error = (
            "INVESTIGATE produced no diagnosis: the investigative agent "
            "terminated (timeout, budget, or empty completion) without emitting "
            "any substantive content. Nothing landed; issue body left untouched."
        )
        state.transition(DiagnosePhase.FAILED, _now_iso())
        write_diagnose_audit(state, project_root)
        return DiagnoseResult(success=False, state=state, message=state.error)

    # ── VERIFY_PREMISE ────────────────────────────────────────────────
    # A diagnosis is only trustworthy if the code it describes still exists.
    # Before landing any confirmed cause, verify the cited paths/patterns are
    # present at the baseline. If a premise was removed by a later commit, the
    # described symptom cannot reproduce against code that is gone — report
    # "appears already resolved" (naming the removing commit) and write no
    # confirmed-cause body, rather than manufacturing a live diagnosis.
    state.transition(DiagnosePhase.VERIFY_PREMISE, _now_iso())
    verdict = verify_premise(artifact, state.baseline_sha, project_root)
    if verdict.resolved:
        state.already_resolved = True
        state.absent_premises = verdict.absent
        state.transition(DiagnosePhase.ALREADY_RESOLVED, _now_iso())
        report = render_already_resolved_markdown(
            issue_number=issue_number,
            baseline_sha=state.baseline_sha,
            absent=verdict.absent,
        )
        if dry_run:
            print(report)
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
    # envelope, treat as TIMEOUT_PARTIAL — return the partial work for operator
    # review rather than landing a misleading "fix-ready" artifact.
    if not artifact.is_complete() or partial:
        artifact = dataclasses.replace(artifact, partial=True)
        state.artifact = artifact
        partial_phase = (
            DiagnosePhase.BUDGET_EXCEEDED if budget_exceeded else DiagnosePhase.TIMEOUT_PARTIAL
        )
        state.transition(partial_phase, _now_iso())
        if interactive and confirm_landing is None:
            confirm_landing = _stdin_confirm
        if interactive and confirm_landing is not None:
            if not confirm_landing(artifact):
                msg = "Partial artifact — operator declined to land. Audit written."
                write_diagnose_audit(state, project_root)
                return DiagnoseResult(success=False, state=state, message=msg)
        try:
            location = _land_artifact(state, artifact, destination, project_root, dry_run=dry_run)
            state.landing_destination = destination
            state.landed_location = location
        except Exception as exc:
            state.error = f"LAND failed: {exc}"
            write_diagnose_audit(state, project_root)
            return DiagnoseResult(success=False, state=state, message=state.error)
        write_diagnose_audit(state, project_root)
        return DiagnoseResult(
            success=False,
            state=state,
            message="Partial diagnosis landed — operator review required",
        )

    # ── LAND ──────────────────────────────────────────────────────────
    state.transition(DiagnosePhase.LAND, _now_iso())
    if interactive and confirm_landing is None:
        confirm_landing = _stdin_confirm
    if interactive and confirm_landing is not None:
        if not confirm_landing(artifact):
            msg = "Operator declined to land artifact. Audit written."
            write_diagnose_audit(state, project_root)
            return DiagnoseResult(success=False, state=state, message=msg)

    try:
        location = _land_artifact(state, artifact, destination, project_root, dry_run=dry_run)
    except Exception as exc:
        state.error = f"LAND failed: {exc}"
        state.transition(DiagnosePhase.FAILED, _now_iso())
        write_diagnose_audit(state, project_root)
        return DiagnoseResult(success=False, state=state, message=state.error)

    state.landing_destination = destination
    state.landed_location = location
    state.transition(DiagnosePhase.DONE, _now_iso())
    write_diagnose_audit(state, project_root)
    return DiagnoseResult(
        success=True,
        state=state,
        message=f"Diagnosis landed at {location}",
    )


def _stdin_confirm(artifact: DiagnosisArtifact) -> bool:
    """Default interactive-mode confirmation: prompt on stdin."""
    print()
    print(render_artifact_markdown(artifact))
    print()
    try:
        answer = input("Land this diagnosis? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}
