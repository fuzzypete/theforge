"""Sprint-entry shape gate — label check + local shape_check re-run.

Filters issues before preflight spends money on them. Defense-in-depth behind
the #811 GitHub Action: catches stale ``needs-grooming`` labels, issues that
existed before the Action was deployed, and edits made while the Action was
offline.

Pure orchestration over ``shape_check`` and ``gh`` CLI. Fail-closed on
unreachable ``gh`` calls: an issue whose labels/body cannot be fetched is
left runnable (the pre-existing sources.py fetch will surface any real
error). We do not want the shape gate itself to be a new source of silent
drops.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..shape_check import Shape, ShapeResult, check
from ..shape_check.types import Severity

NEEDS_GROOMING_LABEL = "needs-grooming"

# Bot comments from #806b embed machine-readable reason codes on a single
# line so local re-runs can match the Action's verdict exactly. Accept either
# a simple CSV form or a JSON array.
_BOT_REASONS_RE = re.compile(
    r"<!--\s*shape-check-reasons:\s*(?P<payload>.+?)\s*-->",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class SkippedIssue:
    """A sprint-entry rejection record exposed to audit/summary output."""

    issue_number: int
    reason_codes: tuple[str, ...]
    source: str  # "label" or "local_check"
    title: str = ""
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "issue_number": self.issue_number,
            "reason_codes": list(self.reason_codes),
            "source": self.source,
            "title": self.title,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ShapeGateResult:
    runnable: list[dict] = field(default_factory=list)
    skipped: list[SkippedIssue] = field(default_factory=list)


def _fetch_issue_detail(number: int, project_root: Path | None) -> dict | None:
    """Fetch ``{title, body, labels}`` for a single issue via ``gh``.

    Returns ``None`` on any fetch failure so callers can decide whether to
    fail-open (leave the issue runnable) or fail-closed.
    """
    try:
        proc = subprocess.run(
            [
                "gh",
                "issue",
                "view",
                str(number),
                "--json",
                "title,body,labels",
            ],
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


def _fetch_bot_reason_codes(number: int, project_root: Path | None) -> list[str]:
    """Return reason codes embedded in the #806b shape-check bot comment.

    Looks for a machine-readable marker of the form
    ``<!-- shape-check-reasons: code1,code2 -->`` or a JSON array. Returns an
    empty list when no bot comment is found or it cannot be parsed — callers
    should re-derive locally in that case.
    """
    try:
        proc = subprocess.run(
            [
                "gh",
                "issue",
                "view",
                str(number),
                "--comments",
                "--json",
                "comments",
            ],
            capture_output=True,
            text=True,
            cwd=str(project_root) if project_root else None,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if proc.returncode != 0:
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    for comment in reversed(data.get("comments") or []):
        body = comment.get("body", "") if isinstance(comment, dict) else ""
        match = _BOT_REASONS_RE.search(body)
        if match is None:
            continue
        payload = match.group("payload").strip()
        # JSON array form
        if payload.startswith("["):
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return [str(c).strip() for c in parsed if str(c).strip()]
        # CSV form
        return [part.strip() for part in payload.split(",") if part.strip()]
    return []


def _blocking_codes(result: ShapeResult) -> list[str]:
    return [r.code for r in result.reasons if r.severity is Severity.BLOCKING]


_VALID_CLASSIFIER_MODES = frozenset({"heuristic", "off", "llm"})


def _resolve_classifier(classifier_mode: str, llm_caller=None) -> str:
    """Return the classifier mode to actually use at sprint time.

    Honors the configured ``shape_check.classifier`` value when supported.
    Falls back to ``heuristic`` when:

    - The mode is unknown (defensive — misconfiguration shouldn't block sprints).
    - The mode is ``llm`` but no ``llm_caller`` is available to refine fuzzy
      reasons. (The ``classifier.classify`` function already silently falls
      back in this case; resolving explicitly keeps the sprint gate honest
      about what it actually ran.)
    """
    if classifier_mode not in _VALID_CLASSIFIER_MODES:
        return "heuristic"
    if classifier_mode == "llm" and llm_caller is None:
        return "heuristic"
    return classifier_mode


def apply_shape_gate(
    issues: list[dict],
    project_root: Path | None,
    *,
    classifier_mode: str = "heuristic",
    force: bool = False,
    fetch_detail=_fetch_issue_detail,
    fetch_bot_codes=_fetch_bot_reason_codes,
    llm_caller=None,
) -> ShapeGateResult:
    """Partition issues into runnable vs skipped before preflight runs.

    Algorithm:

    1. For each ``{number, title}`` in *issues*, fetch labels + body.
    2. If ``needs-grooming`` is present, skip with ``source='label'`` and
       pull reason codes from the #806b bot comment; if that is absent,
       re-derive the codes by running the local shape check.
    3. Otherwise re-run the local shape check against the current body
       (the defense-in-depth step that closes the stale-label loophole).
       If the check returns a non-``RUNNABLE`` shape, skip with
       ``source='local_check'``.

    ``force=True`` returns every input issue as runnable but still populates
    ``skipped`` so the CLI can surface a prominent warning listing reasons.
    """
    effective_mode = _resolve_classifier(classifier_mode, llm_caller=llm_caller)
    runnable: list[dict] = []
    skipped: list[SkippedIssue] = []

    for issue in issues:
        number = int(issue["number"])
        title_short = issue.get("title", "")
        detail = fetch_detail(number, project_root)
        if detail is None:
            # Fail-open: let the downstream source.fetch surface the real error.
            runnable.append(issue)
            continue

        labels = detail["labels"]
        title = detail["title"] or title_short
        body = detail["body"]

        if NEEDS_GROOMING_LABEL in labels:
            codes = fetch_bot_codes(number, project_root)
            if not codes:
                local = check(
                    title,
                    body,
                    labels,
                    classifier_mode=effective_mode,
                    llm_caller=llm_caller,
                )
                codes = _blocking_codes(local) or ["needs_grooming_label"]
            skipped.append(
                SkippedIssue(
                    issue_number=number,
                    reason_codes=tuple(codes),
                    source="label",
                    title=title_short,
                    detail=f"issue carries '{NEEDS_GROOMING_LABEL}' label",
                )
            )
            continue

        local = check(
            title,
            body,
            labels,
            classifier_mode=effective_mode,
            llm_caller=llm_caller,
        )
        if local.shape is not Shape.RUNNABLE:
            codes = _blocking_codes(local) or [r.code for r in local.reasons]
            skipped.append(
                SkippedIssue(
                    issue_number=number,
                    reason_codes=tuple(codes),
                    source="local_check",
                    title=title_short,
                    detail=f"local shape check: {local.suggested_action.value}",
                )
            )
            continue

        runnable.append(issue)

    if force:
        # Escape hatch: caller wants to run every input issue. Skip list is
        # preserved so CLI can render a prominent warning.
        return ShapeGateResult(runnable=list(issues), skipped=skipped)

    return ShapeGateResult(runnable=runnable, skipped=skipped)


def format_skipped_warning(skipped: list[SkippedIssue]) -> str:
    """Render a human-readable warning for the skipped issue list."""
    if not skipped:
        return ""
    lines = [f"[forge] {len(skipped)} issue(s) flagged by shape gate:"]
    for entry in skipped:
        codes = ", ".join(entry.reason_codes) or "<no codes>"
        lines.append(
            f"  - #{entry.issue_number} ({entry.source}): {codes}"
            + (f" — {entry.title}" if entry.title else "")
        )
    return "\n".join(lines)
