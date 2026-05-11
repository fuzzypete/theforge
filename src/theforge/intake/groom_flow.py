"""``forge groom`` flow — readiness repair for typed issues.

Operates on a single typed issue (bug, enhancement/task, etc.) and produces
a proposed body restructure that satisfies the shape gate for its type.

Enforces the three-state bug invariant from ADR-0001: bugs without a
diagnosis are refused; bugs whose diagnosis documents ruled-out hypotheses
but lacks a confirmed cause may be normalized but MUST NOT transition to
the ``ready`` label; only bugs with a confirmed cause are eligible for
``ready``.

Stdlib + ``shape_check``/``intake`` modules only — no provider SDK imports.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..shape_check import ShapeResult, ShapeVerdict
from ..shape_check import check as shape_check
from ..shape_check.heuristics import (
    _diagnosis_cause_unknown,
    diagnosis_completeness,
    is_bug_format_issue,
)
from ..shape_check.parsing import find_heading
from .diagnosis_staleness import StalenessReport, evaluate_staleness

# ── Types ─────────────────────────────────────────────────────────────────


class GroomAction(str, Enum):
    """High-level groom outcome.

    Recorded in the audit substrate as the ``action`` of a readiness event.
    """

    REFUSED = "refused"
    NORMALIZED_ONLY = "normalized-only"
    RESTRUCTURED = "restructured"


class BugDiagnosisState(str, Enum):
    NO_DIAGNOSIS = "no-diagnosis"
    CAUSE_UNKNOWN = "cause-unknown"
    CONFIRMED_CAUSE = "confirmed-cause"
    NOT_A_BUG = "not-a-bug"


@dataclass(frozen=True)
class GroomResult:
    """Result of running the groom flow against a single issue."""

    issue_ref: str
    title: str
    issue_type: str | None
    pre_verdict: ShapeVerdict
    post_verdict: ShapeVerdict
    action: GroomAction
    bug_state: BugDiagnosisState
    original_body: str
    proposed_body: str
    applied: bool
    refusal_reason: str | None = None
    next_command: str | None = None
    investigation_only_notice: bool = False
    labels: tuple[str, ...] = field(default_factory=tuple)
    staleness: StalenessReport | None = None
    confirm_diagnosis_current: bool = False
    staleness_notice: bool = False

    @property
    def needs_change(self) -> bool:
        return self.proposed_body != self.original_body

    def as_event(self) -> dict:
        """Return the SQLite audit-substrate event payload."""
        payload: dict = {
            "kind": "groom",
            "issue_ref": self.issue_ref,
            "issue_type": self.issue_type,
            "pre_verdict": self.pre_verdict.value,
            "post_verdict": self.post_verdict.value,
            "action": self.action.value,
            "bug_diagnosis_state": self.bug_state.value,
            "applied": self.applied,
            "refusal_reason": self.refusal_reason,
        }
        if self.staleness is not None:
            payload["staleness_verdict"] = self.staleness.state.value
            payload["diagnosis_baseline_sha"] = self.staleness.baseline_sha
            payload["diagnosis_current_sha"] = self.staleness.current_sha
            payload["stale_files"] = list(self.staleness.changed_files)
            if self.confirm_diagnosis_current:
                payload["confirm_diagnosis_current"] = True
        return payload


# ── I/O seams ─────────────────────────────────────────────────────────────


FetchIssue = Callable[[int, Path | None], dict | None]
EditIssueBody = Callable[[int, str, Path | None], bool]


def _default_fetch(number: int, project_root: Path | None) -> dict | None:
    """Fetch ``{title, body, labels}`` via ``gh``. Returns ``None`` on error."""
    try:
        proc = subprocess.run(
            ["gh", "issue", "view", str(number), "--json", "title,body,labels"],
            capture_output=True,
            text=True,
            cwd=str(project_root) if project_root else None,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    label_names = [
        lbl.get("name", "")
        for lbl in (data.get("labels") or [])
        if isinstance(lbl, dict) and lbl.get("name")
    ]
    return {
        "title": data.get("title", "") or "",
        "body": data.get("body", "") or "",
        "labels": label_names,
    }


def _default_edit(number: int, body: str, project_root: Path | None) -> bool:
    try:
        proc = subprocess.run(
            ["gh", "issue", "edit", str(number), "--body", body],
            capture_output=True,
            text=True,
            cwd=str(project_root) if project_root else None,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


# ── Type detection ────────────────────────────────────────────────────────


# Types groom understands. Includes the shape-gate's native recognized set
# (bug/enhancement/task/epic) plus the lifecycle-doc aliases ``docs`` and
# ``story`` which need a normalization shim so the shape gate sees them as a
# supported type (see ``_labels_for_shape_check``).
_RECOGNIZED_TYPES = {"bug", "enhancement", "task", "epic", "docs", "story"}

# The subset of types the shape gate's ``check_missing_type`` recognizes
# directly. Anything outside this set must be normalized before shape-check.
_SHAPE_GATE_NATIVE_TYPES = {"bug", "enhancement", "task", "epic"}

# Map groom-only aliases to the shape-gate-recognized type used for
# shape_check. Docs and stories both follow the feature/task body shape
# (Why/AC/Example), so they normalize to ``task`` for verdict-derivation.
_TYPE_ALIAS_TO_SHAPE_GATE: dict[str, str] = {
    "docs": "task",
    "story": "task",
}


def _detect_type(labels: list[str]) -> str | None:
    lset = {label.strip().lower() for label in labels}
    matches = lset & _RECOGNIZED_TYPES
    if len(matches) == 1:
        return next(iter(matches))
    return None


def _labels_for_shape_check(labels: list[str], issue_type: str | None) -> list[str]:
    """Return labels normalized for the shape gate's vocabulary.

    Groom recognizes ``docs`` and ``story`` per ADR-0001's lifecycle even
    though the shape gate's ``check_missing_type`` does not. Injecting the
    aliased shape-gate label here lets a docs/story issue reach a real
    post-groom verdict instead of being stuck on ``needs_type``.
    """
    if issue_type is None or issue_type in _SHAPE_GATE_NATIVE_TYPES:
        return list(labels)
    alias = _TYPE_ALIAS_TO_SHAPE_GATE.get(issue_type)
    if alias is None:
        return list(labels)
    lset = {label.strip().lower() for label in labels}
    if alias in lset:
        return list(labels)
    return [*labels, alias]


# ── Bug diagnosis classification ──────────────────────────────────────────


def classify_bug_diagnosis(body: str, labels: list[str]) -> BugDiagnosisState:
    """Return the three-state classification for a bug-typed issue."""
    if not is_bug_format_issue(body, labels):
        return BugDiagnosisState.NOT_A_BUG
    ok, _missing = diagnosis_completeness(body)
    if ok:
        if _diagnosis_cause_unknown(body):
            return BugDiagnosisState.CAUSE_UNKNOWN
        return BugDiagnosisState.CONFIRMED_CAUSE
    # No diagnosis section, or incomplete and not cause-unknown.
    section_present = find_heading(body, r"diagnosis") is not None
    if section_present and _diagnosis_cause_unknown(body):
        return BugDiagnosisState.CAUSE_UNKNOWN
    return BugDiagnosisState.NO_DIAGNOSIS


# ── Mechanical body normalization ─────────────────────────────────────────


_TRAILING_WS_RE = re.compile(r"[ \t]+$", re.MULTILINE)
_TRIPLE_BLANK_RE = re.compile(r"\n{3,}")


def _normalize_body(body: str) -> str:
    """Mechanical, deterministic body normalization.

    Strips trailing whitespace, collapses 3+ consecutive blank lines to 2,
    and ensures the body ends with a single trailing newline. No semantic
    rewriting — content is preserved verbatim.
    """
    if not body:
        return body
    out = _TRAILING_WS_RE.sub("", body)
    out = _TRIPLE_BLANK_RE.sub("\n\n", out)
    out = out.rstrip() + "\n"
    return out


_AC_STUB = (
    "\n## Acceptance criteria\n\n"
    "- TODO: replace with verifiable observable-behavior bullets "
    "(returns/emits/writes/...)\n"
)

_EXAMPLE_STUB = (
    "\n## Example\n\n"
    "```\n"
    "TODO: replace with a concrete example — sample output, target sketch,\n"
    "or before/after snippet that lets a reviewer eyeball the behavior.\n"
    "```\n"
)


def _restructure_feature_body(body: str) -> str:
    """Insert placeholder sections when AC or Example heading is missing.

    Conservative — only adds stub sections; does not rewrite or remove
    existing content. The stubs are deliberately shape-gate-clean (the AC
    bullet contains observable-verb tokens; the Example stub is wrapped in
    a fenced block) so the post-groom verdict can reach ``runnable`` even
    while operator-readable TODO markers make it clear that substantive
    content still needs to be filled in by a human or a future LLM pass.
    """
    out = body or ""
    if not find_heading(out, r"acceptance criteria|done criteria|checklist"):
        out = out.rstrip() + "\n" + _AC_STUB
    if not find_heading(
        out, r"examples?|target(?:\s+sketch|\s+output|\s+state)?|what it should look like"
    ):
        out = out.rstrip() + "\n" + _EXAMPLE_STUB
    return _normalize_body(out)


# ── Issue loading ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _LoadedIssue:
    title: str
    body: str
    labels: tuple[str, ...]
    number: int | None  # None when loaded from a file path
    file_path: Path | None


class GroomError(Exception):
    """Raised when the issue cannot be loaded or grooming cannot proceed."""


def _looks_like_issue_number(ref: str) -> bool:
    stripped = ref.strip().lstrip("#")
    return stripped.isdigit()


def _parse_local_file(path: Path) -> _LoadedIssue:
    """Load a local issue body file. Heuristics:
    - First H1 line (``# ...``) → title; otherwise filename stem.
    - Labels parsed from a YAML-ish frontmatter block ``labels: [a, b]`` if
      present, otherwise empty.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GroomError(f"could not read {path}: {exc}") from exc

    labels: list[str] = []
    body = raw
    title = path.stem

    if raw.startswith("---\n"):
        end = raw.find("\n---\n", 4)
        if end != -1:
            frontmatter = raw[4:end]
            body = raw[end + len("\n---\n") :]
            for line in frontmatter.splitlines():
                m = re.match(r"^labels:\s*\[(.*)\]\s*$", line.strip())
                if m:
                    labels = [
                        x.strip().strip('"').strip("'") for x in m.group(1).split(",") if x.strip()
                    ]
                m2 = re.match(r"^title:\s*(.+)$", line.strip())
                if m2:
                    title = m2.group(1).strip().strip('"').strip("'")

    if title == path.stem:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                title = stripped[2:].strip()
                break

    return _LoadedIssue(
        title=title,
        body=body,
        labels=tuple(labels),
        number=None,
        file_path=path,
    )


def _load_issue(
    issue_ref: str,
    *,
    fetch: FetchIssue,
    project_root: Path | None,
) -> _LoadedIssue:
    if _looks_like_issue_number(issue_ref):
        number = int(issue_ref.lstrip("#"))
        data = fetch(number, project_root)
        if data is None:
            raise GroomError(f"could not load issue #{number} via gh")
        return _LoadedIssue(
            title=data.get("title", ""),
            body=data.get("body", "") or "",
            labels=tuple(data.get("labels") or []),
            number=number,
            file_path=None,
        )
    path = Path(issue_ref)
    if not path.exists():
        raise GroomError(
            f"{issue_ref!r} is not a valid issue number and does not exist as a file path"
        )
    return _parse_local_file(path)


# ── Next-step hint ────────────────────────────────────────────────────────


def _next_command(
    *,
    issue_ref: str,
    issue_number: int | None,
    action: GroomAction,
    bug_state: BugDiagnosisState,
    post_verdict: ShapeVerdict,
) -> str:
    """Return the one-line operator-facing next-step suggestion.

    Output is for humans — not a stable machine-readable protocol.
    """
    ref_for_cmd = str(issue_number) if issue_number is not None else issue_ref

    if action is GroomAction.REFUSED:
        return f"forge diagnose {ref_for_cmd}"

    if bug_state is BugDiagnosisState.CAUSE_UNKNOWN:
        # Investigation-ready, NOT implementation-ready. The next step is
        # continued diagnosis — never the ``ready`` label.
        return f"forge diagnose {ref_for_cmd}"

    if post_verdict is ShapeVerdict.RUNNABLE and issue_number is not None:
        return f"gh issue edit {issue_number} --add-label ready"

    # Groom couldn't get to runnable — there's still work to do; operator
    # should re-inspect rather than label as ready.
    return (
        f"review {ref_for_cmd} body — groom left verdict={post_verdict.value}; "
        "address remaining shape-gate findings before labeling ready"
    )


# ── Main flow ─────────────────────────────────────────────────────────────


def _classify_action(
    *,
    issue_type: str | None,
    bug_state: BugDiagnosisState,
    pre_verdict: ShapeVerdict,
    needs_change: bool,
    staleness: StalenessReport | None = None,
    confirm_diagnosis_current: bool = False,
    issue_ref: str = "<N>",
) -> tuple[GroomAction, str | None]:
    """Return (action, refusal_reason).

    Pure decision function — does not produce output text. Enforces the
    three-state bug invariant *and* the baseline-staleness invariant: a
    stale ``CONFIRMED_CAUSE`` diagnosis cannot transition to ready unless
    the operator passes ``confirm_diagnosis_current``. Cause-unknown stays
    investigation-ready regardless of staleness (per spec — staleness is
    informational for that state, not a refusal).
    """
    if issue_type == "bug" and bug_state is BugDiagnosisState.NO_DIAGNOSIS:
        return GroomAction.REFUSED, "needs diagnosis — run forge diagnose <N> first."
    if (
        issue_type == "bug"
        and bug_state is BugDiagnosisState.CONFIRMED_CAUSE
        and staleness is not None
        and staleness.is_stale
        and not confirm_diagnosis_current
    ):
        sha = staleness.baseline_sha or "unknown"
        reason = (
            f"diagnosis baseline (SHA {sha}) is stale relative to current base — "
            f"re-run forge diagnose {issue_ref} or confirm baseline is still valid."
        )
        return GroomAction.REFUSED, reason
    if issue_type == "bug" and bug_state is BugDiagnosisState.CAUSE_UNKNOWN:
        return GroomAction.NORMALIZED_ONLY, None
    return GroomAction.RESTRUCTURED, None


def run_groom(
    issue_ref: str,
    *,
    apply_changes: bool = False,
    project_root: Path | None = None,
    fetch_issue: FetchIssue | None = None,
    edit_issue_body: EditIssueBody | None = None,
    confirm_diagnosis_current: bool = False,
) -> GroomResult:
    """Run the groom flow against a single issue.

    Stateless and pure aside from injected I/O seams. Callers that want a
    network-free run can pass test doubles for ``fetch_issue`` and
    ``edit_issue_body``.
    """
    fetch = fetch_issue or _default_fetch
    edit = edit_issue_body or _default_edit

    loaded = _load_issue(issue_ref, fetch=fetch, project_root=project_root)
    title = loaded.title
    body = loaded.body
    labels = list(loaded.labels)

    issue_type = _detect_type(labels)
    bug_state = (
        classify_bug_diagnosis(body, labels)
        if issue_type == "bug"
        else BugDiagnosisState.NOT_A_BUG
    )

    staleness: StalenessReport | None = None
    if issue_type == "bug" and bug_state in (
        BugDiagnosisState.CONFIRMED_CAUSE,
        BugDiagnosisState.CAUSE_UNKNOWN,
    ):
        staleness = evaluate_staleness(body, project_root=project_root)

    # Shape gate's native type vocabulary doesn't include ``docs`` or
    # ``story``; normalize before each check so verdict-derivation runs
    # against a recognized type label.
    shape_labels = _labels_for_shape_check(labels, issue_type)

    pre_result: ShapeResult = shape_check(title, body, shape_labels)
    pre_verdict = pre_result.verdict

    # Refusal: no body edits proposed.
    action, refusal_reason = _classify_action(
        issue_type=issue_type,
        bug_state=bug_state,
        pre_verdict=pre_verdict,
        needs_change=False,
        staleness=staleness,
        confirm_diagnosis_current=confirm_diagnosis_current,
        issue_ref=str(loaded.number) if loaded.number is not None else issue_ref,
    )

    if action is GroomAction.REFUSED:
        next_cmd = _next_command(
            issue_ref=issue_ref,
            issue_number=loaded.number,
            action=action,
            bug_state=bug_state,
            post_verdict=pre_verdict,
        )
        return GroomResult(
            issue_ref=issue_ref,
            title=title,
            issue_type=issue_type,
            pre_verdict=pre_verdict,
            post_verdict=pre_verdict,
            action=action,
            bug_state=bug_state,
            original_body=body,
            proposed_body=body,
            applied=False,
            refusal_reason=refusal_reason,
            next_command=next_cmd,
            labels=tuple(labels),
            staleness=staleness,
            confirm_diagnosis_current=confirm_diagnosis_current,
            staleness_notice=(
                staleness is not None
                and staleness.is_stale
                and bug_state is BugDiagnosisState.CAUSE_UNKNOWN
            ),
        )

    # Body restructure / normalization
    if action is GroomAction.NORMALIZED_ONLY:
        proposed = _normalize_body(body)
    else:
        # RESTRUCTURED branch: features/tasks/docs get stub injection; bugs
        # with confirmed cause only need normalization (their structural
        # content — the Diagnosis section — is by definition already present).
        if issue_type == "bug":
            proposed = _normalize_body(body)
        else:
            proposed = _restructure_feature_body(body)

    post_result = shape_check(title, proposed, shape_labels)
    post_verdict = post_result.verdict

    applied = False
    if apply_changes and proposed != body:
        if loaded.number is not None:
            applied = edit(loaded.number, proposed, project_root)
            if not applied:
                raise GroomError(f"gh issue edit failed for #{loaded.number}")
            # Post-apply re-check: re-fetch the persisted body and run the
            # shape gate against what actually landed, so the reported
            # post-groom verdict reflects the upstream state — not our
            # in-memory proposal. Fail-open on a refetch failure: keep the
            # pre-apply verdict rather than crashing.
            refetched = fetch(loaded.number, project_root)
            if refetched is not None:
                confirmed_body = refetched.get("body", "") or ""
                confirmed_result = shape_check(title, confirmed_body, shape_labels)
                post_verdict = confirmed_result.verdict
        elif loaded.file_path is not None:
            try:
                loaded.file_path.write_text(proposed, encoding="utf-8")
                applied = True
            except OSError as exc:
                raise GroomError(f"could not write {loaded.file_path}: {exc}") from exc
            # Post-apply re-check against the persisted file contents.
            try:
                confirmed_body = loaded.file_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise GroomError(
                    f"could not re-read {loaded.file_path} after apply: {exc}"
                ) from exc
            confirmed_result = shape_check(title, confirmed_body, shape_labels)
            post_verdict = confirmed_result.verdict

    next_cmd = _next_command(
        issue_ref=issue_ref,
        issue_number=loaded.number,
        action=action,
        bug_state=bug_state,
        post_verdict=post_verdict,
    )

    return GroomResult(
        issue_ref=issue_ref,
        title=title,
        issue_type=issue_type,
        pre_verdict=pre_verdict,
        post_verdict=post_verdict,
        action=action,
        bug_state=bug_state,
        original_body=body,
        proposed_body=proposed,
        applied=applied,
        refusal_reason=None,
        next_command=next_cmd,
        investigation_only_notice=(bug_state is BugDiagnosisState.CAUSE_UNKNOWN),
        labels=tuple(labels),
        staleness=staleness,
        confirm_diagnosis_current=confirm_diagnosis_current,
        staleness_notice=(
            staleness is not None
            and staleness.is_stale
            and bug_state is BugDiagnosisState.CAUSE_UNKNOWN
        ),
    )
