"""Ready-label queue: list sprint-eligible issues carrying the ``ready`` label.

Per ADR-0001, the mid-sprint workflow does **not** introduce a ``forge queue``
command. "Queue for next sprint" means the ``ready`` label is applied, and
normal sprint selection picks the issue up. This module surfaces that eligible
set so the operator can see, at a glance, which open issues are ready for the
next sprint — optionally scoped to a milestone.

No queue *ordering* or *priority* semantics are added: the list is simply the
current set of open, ``ready``-labeled issues, recomputed from GitHub on each
call so it always reflects live label/milestone state.

The ``ready`` label is a human-applied marker and nothing enforces that it is
applied only after ``capture → shape → diagnose → groom``. A queue whose
entries the sprint gate would refuse is worse than no queue: it moves the
discovery of inadmissibility from planning time to sprint time, after budget
is committed. So each entry is also run through the shared
``admissibility.classify_admissibility`` — the same decision
``sprint.shape_gate`` enforces at sprint entry — and entries the gate would
refuse are rendered with their blocking verdict rather than as ``ready``
(#2027).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .admissibility import classify_admissibility

READY_LABEL = "ready"

_GH_TIMEOUT_SECONDS = 30

# Dev-runnable and operator work-object type labels, in the order we prefer to
# report them when an issue carries more than one. This is display-only — the
# ``ready`` label decides membership in this listing, and
# ``classify_admissibility`` decides whether a member is actually admissible.
_TYPE_LABELS = (
    "bug",
    "enhancement",
    "task",
    "documentation",
    "epic",
    "operator-action",
)


@dataclass(frozen=True)
class ReadyEntry:
    """An open, ``ready``-labeled issue and the sprint gate's verdict on it.

    ``admissible`` is the shape gate's answer, not the label's: an entry with
    ``admissible=False`` carries the ``ready`` label but would be refused at
    sprint entry, and ``verdict``/``detail`` say why.
    """

    issue_number: int
    title: str
    type_label: str
    admissible: bool = True
    verdict: str = ""
    detail: str = ""


def _issue_type_label(labels: list[str]) -> str:
    """Return the best display type for an issue from its label set.

    Prefers a known work-object type (bug, enhancement, ...) in a stable order;
    falls back to ``"—"`` when the issue carries no recognized type label.
    """
    label_set = {name.lower() for name in labels}
    for candidate in _TYPE_LABELS:
        if candidate in label_set:
            return candidate
    return "—"


def _gh_list_ready_issues(project_root: Path, milestone: str | None) -> list[dict]:
    """Return open ``ready``-labeled issues (number/title/labels/body) via the gh CLI.

    The body is fetched in the same listing call — without it the shape gate's
    admissibility decision cannot be evaluated from the data the queue holds,
    which is exactly how the listing and sprint entry came to disagree (#2027).

    When ``milestone`` is given, the listing is scoped to that GitHub milestone.
    Best-effort: returns an empty list on any gh failure so the status surface
    degrades to "no ready issues" rather than crashing.
    """
    cmd = [
        "gh",
        "issue",
        "list",
        "--label",
        READY_LABEL,
        "--state",
        "open",
        "--limit",
        "200",
        "--json",
        "number,title,labels,body",
    ]
    if milestone:
        cmd.extend(["--milestone", milestone])

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=_GH_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []

    if proc.returncode != 0:
        return []

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []

    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict) and "number" in item]


def _label_names(issue: dict) -> list[str]:
    """Extract label names from a gh issue JSON record."""
    labels = issue.get("labels")
    if not isinstance(labels, list):
        return []
    names: list[str] = []
    for label in labels:
        if isinstance(label, dict):
            name = label.get("name")
            if isinstance(name, str):
                names.append(name)
    return names


def _semantic_readiness(
    *,
    issue_number: int,
    title: str,
    body: str,
    labels: list[str],
    project_root: Path,
):
    """Derive semantic readiness for one listed issue, or ``None`` on failure.

    Imported lazily so this low-dependency surface keeps its stdlib +
    ``shape_check`` import cost for the structural answer. Best-effort like the
    rest of this module: a store read failure degrades the listing to the
    structural verdict rather than crashing ``forge status --ready``.
    """
    try:
        from .eval.semantic_readiness import semantic_readiness_for_issue  # noqa: PLC0415

        return semantic_readiness_for_issue(
            issue_number=issue_number,
            title=title,
            body=body,
            labels=labels,
            project_root=project_root,
        )
    except Exception:  # noqa: BLE001
        return None


def build_ready_queue(
    project_root: Path,
    *,
    milestone: str | None = None,
    fetch_issues: Callable[[], list[dict]] | None = None,
    semantic_readiness: Callable[..., object] | None = None,
) -> list[ReadyEntry]:
    """Return the ``ready``-labeled issue set with each entry's gate verdict.

    Every entry is classified with ``classify_admissibility`` — the same
    decision ``sprint.shape_gate`` applies at sprint entry — so an entry the
    gate would refuse carries ``admissible=False`` and its blocking verdict
    rather than being presented as sprint-ready.

    ``milestone`` optionally scopes the listing to one GitHub milestone.
    An admissible entry is then checked against its recorded, operator-ratified
    semantic readiness through the same derivation the sprint gate uses
    (#2785), so a revision whose policy-required review is unevaluated or
    unratified is not advertised as ready here either.

    ``fetch_issues`` and ``semantic_readiness`` are injection seams for
    testing; both default to the live implementations. Entries are sorted by
    issue number for stable, tooling-friendly output.
    """

    fetch_issues = fetch_issues or (lambda: _gh_list_ready_issues(project_root, milestone))
    semantic_readiness = semantic_readiness or _semantic_readiness

    issues = fetch_issues()
    entries: list[ReadyEntry] = []
    for issue in issues:
        labels = _label_names(issue)
        title = str(issue.get("title", "") or "")
        body = str(issue.get("body", "") or "")
        number = int(issue["number"])
        # No llm_caller here: a status listing must not spend agent budget, and
        # the gate's own _resolve_classifier falls back to the heuristic
        # classifier without a caller, so both sides evaluate identically.
        verdict = classify_admissibility(title, body, labels)
        admissible = verdict.admissible
        entry_verdict = verdict.verdict
        detail = verdict.detail
        if admissible:
            # Same overlay, same order as the sprint gate: consulted only once
            # the structural verdict admits, and reading the ratified state
            # rather than evaluator output.
            readiness = semantic_readiness(
                issue_number=number,
                title=title,
                body=body,
                labels=labels,
                project_root=project_root,
            )
            if readiness is not None and readiness.withholds_admission:
                admissible = False
                entry_verdict = readiness.reason_code
                detail = readiness.detail
        entries.append(
            ReadyEntry(
                issue_number=number,
                title=title,
                type_label=_issue_type_label(labels),
                admissible=admissible,
                verdict=entry_verdict,
                detail=detail,
            )
        )
    entries.sort(key=lambda entry: entry.issue_number)
    return entries


_DETAIL_WIDTH = 160


def _entry_marker(entry: ReadyEntry) -> str:
    """Return the status-column token for an entry: ``ready`` or the refusal."""
    if entry.admissible:
        return "ready"
    return f"BLOCKED:{entry.verdict}" if entry.verdict else "BLOCKED"


def _short_detail(detail: str) -> str:
    """Return a one-line, bounded form of a gate refusal detail.

    Shape-check details embed full remediation instructions and can run to
    several hundred characters; unabridged they bury the listing they annotate.
    ``forge shape <n>`` prints the full text.
    """
    collapsed = " ".join(detail.split())
    if len(collapsed) <= _DETAIL_WIDTH:
        return collapsed
    return collapsed[: _DETAIL_WIDTH - 1].rstrip() + "…"


def format_ready_queue(entries: list[ReadyEntry], *, milestone: str | None = None) -> str:
    """Render the ready-label queue as human- and tooling-parseable text.

    Column order is stable (issue ref, type, status marker, title) so adjacent
    tooling can consume it without breaking on cosmetic changes. The status
    column carries the sprint gate's verdict, so a ``ready``-labeled issue the
    gate would refuse is never presented as admissible::

        Ready for next sprint (2 issues, 1 blocked by shape gate):
          #1487  bug  ready                    status --watch blank during preflight
          #1512  bug  BLOCKED:needs_diagnosis  cut-rc.sh shim wrapper regression

        1 issue carries the `ready` label but would be refused at sprint entry:
          #1512  needs_diagnosis: Bug has no Diagnosis section — not fix-ready. …
        Run `forge shape <n>` for the full verdict, then `forge groom <n>` / …
    """
    scope = f" in {milestone}" if milestone else ""
    if not entries:
        return f"Ready for next sprint{scope}: none."

    blocked = [entry for entry in entries if not entry.admissible]
    noun = "issue" if len(entries) == 1 else "issues"
    counts = f"{len(entries)} {noun}"
    if blocked:
        counts += f", {len(blocked)} blocked by shape gate"

    lines = [f"Ready for next sprint{scope} ({counts}):"]
    type_width = max((len(entry.type_label) for entry in entries), default=0)
    marker_width = max((len(_entry_marker(entry)) for entry in entries), default=0)
    for entry in entries:
        lines.append(
            f"  #{entry.issue_number}  {entry.type_label.ljust(type_width)}  "
            f"{_entry_marker(entry).ljust(marker_width)}  {entry.title}"
        )

    if blocked:
        subject = "1 issue carries" if len(blocked) == 1 else f"{len(blocked)} issues carry"
        lines.append("")
        lines.append(f"{subject} the `{READY_LABEL}` label but would be refused at sprint entry:")
        for entry in blocked:
            detail = f": {_short_detail(entry.detail)}" if entry.detail else ""
            lines.append(f"  #{entry.issue_number}  {entry.verdict or 'blocked'}{detail}")
        lines.append(
            "Run `forge shape <n>` for the full verdict, then `forge groom <n>` / "
            "`forge diagnose <n>` before sprint selection."
        )

    return "\n".join(lines)
