"""Deterministic backlog reporting for ``forge triage``.

This slice inventories the open finding backlog and gathers only mechanical
staleness evidence: whether cited artifacts still exist in the current tree and
how much the cited code has churned since the finding was filed. No tracker
write, model call, or judgment about disposition lives here; the output is the
structured artifact the later proposal stage consumes.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import yaml

from theforge.triage_snapshot import canonicalize_finding_snapshot

RECOGNIZED_FINDING_LABELS: tuple[str, ...] = ("forge-finding", "review-finding", "needs-triage")
NEEDS_TRIAGE_LABEL = "needs-triage"
HYGIENE_POOL = "Hygiene"
UNPOOLED_STATE = "unpooled"

STATUS_STALE = "stale_evidence"
STATUS_ACTIVE = "active"
STATUS_PARTIAL = "partially_verified"
STATUS_UNVERIFIED = "unverified"

_GH_TIMEOUT_SECONDS = 30
_GIT_TIMEOUT_SECONDS = 30
_SEARCH_TIMEOUT_SECONDS = 30
_PATH_SUFFIXES = (".py", ".md", ".sh", ".yaml", ".yml", ".json", ".toml", ".ini", ".txt")
_GH_LINE_ANCHOR_RE = re.compile(r"^L(?P<line>\d+)(?:-L(?P<end_line>\d+))?$")

_GH_ISSUE_JQ = (
    ".[] | select(.pull_request == null) | "
    '{number: .number, title: .title, body: (.body // ""), '
    'createdAt: (.created_at // ""), '
    "labels: ((.labels // []) | map(.name)), "
    "milestone: (.milestone.title // null)}"
)
_GH_MILESTONE_JQ = ".[] | .title"


class TriageBacklogReportError(RuntimeError):
    """The backlog report could not be generated from live repository state."""


@dataclass(frozen=True)
class BacklogIssue:
    """One open GitHub issue in the finding backlog."""

    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    created_at: str
    milestone: str | None = None

    @property
    def issue_ref(self) -> str:
        return f"#{self.number}"

    def state_label(self) -> str:
        if self.milestone:
            return self.milestone
        if NEEDS_TRIAGE_LABEL in {label.lower() for label in self.labels}:
            return NEEDS_TRIAGE_LABEL
        return UNPOOLED_STATE

    def display_labels(self) -> str:
        hidden = {label.lower() for label in RECOGNIZED_FINDING_LABELS}
        visible = [
            label
            for label in self.labels
            if label.lower() not in hidden and label.lower() != NEEDS_TRIAGE_LABEL
        ]
        return ",".join(sorted(visible, key=str.lower)) or "—"


@dataclass(frozen=True)
class EvidenceEntry:
    """One mechanically gathered evidence row for a backlog finding."""

    evidence_id: str
    kind: str
    summary: str
    detail: str = ""
    checkable: bool = True
    observed_status: str = STATUS_ACTIVE
    artifact: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.evidence_id,
            "kind": self.kind,
            "summary": self.summary,
            "checkable": self.checkable,
            "detail": self.detail,
            "observed_status": self.observed_status,
        }
        if self.artifact:
            payload["artifact"] = self.artifact
        return payload


@dataclass(frozen=True)
class BacklogFindingRecord:
    """One finding in the generated backlog report."""

    finding_id: str
    issue_ref: str
    issue_number: int
    title: str
    body: str
    labels: tuple[str, ...]
    display_labels: str
    opened_at: str
    age_days: int | None
    pool_state: str
    verification_status: str
    evidence: tuple[EvidenceEntry, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "finding_id": self.finding_id,
            "issue_ref": self.issue_ref,
            "issue_number": self.issue_number,
            "title": self.title,
            "body": self.body,
            "labels": list(self.labels),
            "display_labels": self.display_labels,
            "opened_at": self.opened_at,
            "age_days": self.age_days,
            "pool_state": self.pool_state,
            "verification_status": self.verification_status,
            "evidence": [entry.to_dict() for entry in self.evidence],
        }

    def snapshot_dict(self) -> dict[str, object]:
        """Return the stable finding/evidence state used for stale ratification."""
        return canonicalize_finding_snapshot(self.to_dict())


@dataclass(frozen=True)
class BacklogTriageReport:
    """Human- and machine-readable deterministic backlog inventory."""

    generated_at: str
    current_milestone: str | None
    named_milestones: tuple[str, ...]
    findings: tuple[BacklogFindingRecord, ...]
    milestone_inventory_error: str | None = None

    def to_dict(self) -> dict[str, object]:
        state_counts = Counter(finding.pool_state for finding in self.findings)
        status_counts = Counter(finding.verification_status for finding in self.findings)
        payload: dict[str, object] = {
            "generated_at": self.generated_at,
            "current_milestone": self.current_milestone,
            "named_milestones": list(self.named_milestones),
            "summary": {
                "total_open": len(self.findings),
                "by_state": dict(sorted(state_counts.items(), key=lambda item: item[0].lower())),
                "by_verification_status": dict(
                    sorted(status_counts.items(), key=lambda item: item[0].lower())
                ),
            },
            "findings": [finding.to_dict() for finding in self.findings],
        }
        if self.milestone_inventory_error:
            payload["milestone_inventory_error"] = self.milestone_inventory_error
        return payload


@dataclass(frozen=True)
class PathCitation:
    """A repo-relative artifact path cited in a finding body."""

    path: str
    line: int | None = None
    end_line: int | None = None


@dataclass(frozen=True)
class SymbolCitation:
    """A symbol-like identifier cited in a finding body."""

    symbol: str


@dataclass(frozen=True)
class InvalidPathCitation:
    """A path-like token that could not be attributed mechanically."""

    raw: str
    detail: str


@dataclass(frozen=True)
class SymbolLookupResult:
    """Symbol search hits plus any candidate scopes skipped as unresolvable."""

    hits: tuple[tuple[str, int], ...]
    searched_scopes: tuple[str, ...] = ()
    unresolved_candidates: tuple[str, ...] = ()


def _parse_iso8601(timestamp: str) -> datetime:
    text = str(timestamp or "").strip()
    if not text:
        raise ValueError("missing timestamp")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _gh_api_lines(endpoint: str, jq_expr: str, project_root: Path) -> list[str]:
    try:
        proc = subprocess.run(
            ["gh", "api", "--paginate", "--jq", jq_expr, endpoint],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=_GH_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TriageBacklogReportError(f"gh api {endpoint!r} failed: {exc}") from exc
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or "unknown gh api failure"
        raise TriageBacklogReportError(f"gh api {endpoint!r} failed: {err}")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def fetch_backlog_issues(project_root: Path) -> list[BacklogIssue]:
    """Return the union of open issues carrying any recognized finding label."""
    issues_by_number: dict[int, BacklogIssue] = {}
    for label in RECOGNIZED_FINDING_LABELS:
        endpoint = (
            "repos/{owner}/{repo}/issues"
            f"?labels={quote(label, safe='')}&state=open&per_page=100"
        )
        for line in _gh_api_lines(endpoint, _GH_ISSUE_JQ, project_root):
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TriageBacklogReportError(
                    f"gh api returned malformed issue JSON for label {label!r}: {exc}"
                ) from exc
            if not isinstance(raw, dict):
                raise TriageBacklogReportError(
                    f"gh api returned a non-mapping issue row for label {label!r}"
                )
            number = int(raw["number"])
            merged_labels = set(str(name) for name in raw.get("labels") or [])
            if number in issues_by_number:
                merged_labels.update(issues_by_number[number].labels)
            issues_by_number[number] = BacklogIssue(
                number=number,
                title=str(raw.get("title") or ""),
                body=str(raw.get("body") or ""),
                labels=tuple(sorted(merged_labels, key=str.lower)),
                created_at=str(raw.get("createdAt") or ""),
                milestone=str(raw["milestone"]).strip() if raw.get("milestone") else None,
            )
    return [issues_by_number[number] for number in sorted(issues_by_number)]


def fetch_open_milestones(project_root: Path) -> tuple[str, ...]:
    """Return open milestone titles in stable order."""
    endpoint = "repos/{owner}/{repo}/milestones?state=open&per_page=100"
    seen: set[str] = set()
    titles: list[str] = []
    for line in _gh_api_lines(endpoint, _GH_MILESTONE_JQ, project_root):
        title = str(line).strip()
        if title and title not in seen:
            seen.add(title)
            titles.append(title)
    return tuple(sorted(titles, key=str.lower))


def _is_path_like(token: str) -> bool:
    text = token.strip().strip(".,:;()[]")
    if not text:
        return False
    if text.startswith(("http://", "https://")):
        return False
    candidate = text.split("#", 1)[0]
    if not candidate:
        return False
    if "/" in candidate:
        return True
    return candidate.endswith(_PATH_SUFFIXES)


def _github_line_anchor(fragment: str) -> tuple[int, int | None] | None:
    match = _GH_LINE_ANCHOR_RE.fullmatch(fragment)
    if match is None:
        return None
    line = int(match.group("line"))
    raw_end = match.group("end_line")
    if raw_end is None:
        return line, None
    end_line = int(raw_end)
    if end_line < line:
        raise ValueError(f"unsupported path anchor #{fragment}")
    if end_line == line:
        return line, None
    return line, end_line


def _colon_line_anchor(fragment: str) -> tuple[int, int | None] | None:
    if fragment.isdigit():
        return int(fragment), None
    if "-" not in fragment:
        return None
    raw_line, raw_end = fragment.split("-", 1)
    if not raw_line.isdigit() or not raw_end.isdigit():
        return None
    line = int(raw_line)
    end_line = int(raw_end)
    if end_line < line:
        raise ValueError(f"unsupported path anchor :{fragment}")
    if end_line == line:
        return line, None
    return line, end_line


def _parse_path_token(token: str) -> PathCitation | None:
    text = token.strip().strip(".,;()[]")
    if not _is_path_like(text):
        return None
    line: int | None = None
    end_line: int | None = None
    if "#" in text:
        candidate_path, fragment = text.split("#", 1)
        if not candidate_path:
            return None
        anchored_location = _github_line_anchor(fragment)
        if anchored_location is None:
            raise ValueError(f"unsupported path anchor #{fragment}")
        text = candidate_path
        line, end_line = anchored_location
    if ":" in text:
        candidate_path, candidate_line = text.rsplit(":", 1)
        anchored_location = _colon_line_anchor(candidate_line)
        if anchored_location is not None:
            candidate_line_number, candidate_end_line = anchored_location
            if line is not None and (
                line != candidate_line_number or end_line != candidate_end_line
            ):
                raise ValueError("path citation mixes multiple line references")
            text = candidate_path
            line = candidate_line_number
            end_line = candidate_end_line
    if not text or text.startswith(("http://", "https://")):
        return None
    return PathCitation(path=text, line=line, end_line=end_line)


def _path_only_fallback_for_invalid_anchor(token: str) -> PathCitation | None:
    text = token.strip().strip(".,;()[]")
    if "#" not in text:
        return None
    candidate_path, fragment = text.split("#", 1)
    if not candidate_path:
        return None
    try:
        anchored_location = _github_line_anchor(fragment)
    except ValueError:
        anchored_location = None
    if anchored_location is not None:
        return None
    base_path = candidate_path
    if ":" in base_path:
        stripped_path, candidate_line = base_path.rsplit(":", 1)
        try:
            colon_anchor = _colon_line_anchor(candidate_line)
        except ValueError:
            colon_anchor = None
        if colon_anchor is not None:
            base_path = stripped_path
    return _parse_path_token(base_path)


def _line_label(relpath: str, citation: PathCitation) -> str:
    if citation.line is None:
        return relpath
    if citation.end_line is None:
        return f"{relpath}:{citation.line}"
    return f"{relpath}:{citation.line}-{citation.end_line}"


def _is_symbol_like(token: str) -> bool:
    if not token or "/" in token or " " in token:
        return False
    if not token.replace("_", "").isalnum():
        return False
    if not (token[0].isalpha() or token[0] == "_"):
        return False
    return "_" in token or any(char.isupper() for char in token[1:]) or len(token) >= 8


def parse_citations(
    body: str,
) -> tuple[tuple[PathCitation, ...], tuple[SymbolCitation, ...], tuple[InvalidPathCitation, ...]]:
    """Extract conservative, mechanically checkable citations from a finding body."""
    text = str(body or "")
    paths: list[PathCitation] = []
    seen_paths: set[tuple[str, int | None, int | None]] = set()
    invalid_paths: list[InvalidPathCitation] = []
    seen_invalid_paths: set[tuple[str, str]] = set()

    def _record_path_citation(citation: PathCitation) -> None:
        key = (citation.path, citation.line, citation.end_line)
        if key not in seen_paths:
            seen_paths.add(key)
            paths.append(citation)

    def _record_path_token(raw_token: str) -> None:
        trimmed = raw_token.strip().strip(".,;()[]")
        try:
            citation = _parse_path_token(raw_token)
        except ValueError as exc:
            if _is_path_like(trimmed):
                key = (trimmed, str(exc))
                if key not in seen_invalid_paths:
                    seen_invalid_paths.add(key)
                    invalid_paths.append(InvalidPathCitation(raw=trimmed, detail=str(exc)))
                fallback = _path_only_fallback_for_invalid_anchor(trimmed)
                if fallback is not None:
                    _record_path_citation(fallback)
            return
        if citation is None:
            return
        _record_path_citation(citation)

    chunks = text.split("`")
    for index, chunk in enumerate(chunks):
        if index % 2 == 0:
            continue
        _record_path_token(chunk)

    plain_tokens = text.replace("`", " ").replace("\n", " ").split()
    for token in plain_tokens:
        _record_path_token(token)

    symbols: list[SymbolCitation] = []
    seen_symbols: set[str] = set()
    for index, chunk in enumerate(chunks):
        if index % 2 == 0:
            continue
        token = chunk.strip().strip(".,;:()[]")
        if _is_symbol_like(token) and token not in seen_symbols:
            seen_symbols.add(token)
            symbols.append(SymbolCitation(symbol=token))

    for raw_token in plain_tokens:
        token = raw_token.strip().strip(".,;:()[]")
        lower = token.lower()
        if lower in {"symbol", "function", "method", "class", "attribute", "constant"}:
            continue
        if _is_symbol_like(token) and token not in seen_symbols:
            prefix = text.lower()
            hint = f"symbol {token.lower()}" in prefix or f"function {token.lower()}" in prefix
            if hint:
                seen_symbols.add(token)
                symbols.append(SymbolCitation(symbol=token))

    return tuple(paths), tuple(symbols), tuple(invalid_paths)


def _normalize_repo_path(project_root: Path, raw_path: str) -> str:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        raise ValueError(f"absolute path citation is outside the repo contract: {raw_path}")
    resolved = (project_root / candidate).resolve()
    root = project_root.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path citation escapes the repo root: {raw_path}") from exc
    return relative.as_posix()


def _file_line_count(path: Path) -> int:
    with path.open(encoding="utf-8", errors="replace") as handle:
        return sum(1 for _ in handle)


def _git_churn_count(project_root: Path, relpath: str, created_at: str) -> int:
    proc = subprocess.run(
        ["git", "log", "--format=%H", f"--since={created_at}", "--", relpath],
        capture_output=True,
        text=True,
        cwd=str(project_root),
        timeout=_GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or "git log failed"
        raise RuntimeError(err)
    return sum(1 for line in proc.stdout.splitlines() if line.strip())


def _run_symbol_search(
    project_root: Path,
    symbol: str,
    scope: str,
) -> subprocess.CompletedProcess[str]:
    cmd = ["rg", "--with-filename", "--line-number", "--fixed-strings", symbol, scope]
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=_SEARCH_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        return subprocess.run(
            ["git", "grep", "-n", "-F", symbol, "--", scope],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=_SEARCH_TIMEOUT_SECONDS,
            check=False,
        )


def _symbol_hits(
    project_root: Path,
    symbol: str,
    candidate_paths: tuple[str, ...] = (),
) -> SymbolLookupResult:
    scopes = candidate_paths or (".",)
    searchable_scopes: list[str] = []
    unresolved_candidates: list[str] = []
    if candidate_paths:
        for scope in scopes:
            if (project_root / scope).exists():
                searchable_scopes.append(scope)
            else:
                unresolved_candidates.append(scope)
    else:
        searchable_scopes.append(".")

    hits: list[tuple[str, int]] = []
    for scope in searchable_scopes:
        proc = _run_symbol_search(project_root, symbol, scope)
        if proc.returncode not in {0, 1}:
            err = proc.stderr.strip() or proc.stdout.strip() or "symbol search failed"
            raise RuntimeError(err)
        for line in proc.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) < 2 or not parts[1].isdigit():
                continue
            hits.append((parts[0], int(parts[1])))
    deduped: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for hit in hits:
        if hit not in seen:
            seen.add(hit)
            deduped.append(hit)
    return SymbolLookupResult(
        hits=tuple(deduped),
        searched_scopes=tuple(searchable_scopes),
        unresolved_candidates=tuple(unresolved_candidates),
    )


def _resolve_extensionless_path(project_root: Path, relpath: str) -> str | None:
    """Resolve a dotted-free path citation against known source extensions.

    A citation like ``src/theforge/cli/triage`` names a module by its
    extensionless import-style path even though the tracked artifact on disk
    is ``src/theforge/cli/triage.py``. Trying the repository's known source
    extensions before declaring the citation absent keeps STALE verdicts
    resting on a check that would actually have succeeded had the artifact
    still existed.
    """
    if relpath.endswith(_PATH_SUFFIXES):
        return None
    for suffix in _PATH_SUFFIXES:
        candidate = f"{relpath}{suffix}"
        if (project_root / candidate).exists():
            return candidate
    return None


def _resolved_repo_relpath(project_root: Path, relpath: str) -> str:
    """Return relpath as-is if it exists, else its extension-resolved form if any."""
    if (project_root / relpath).exists():
        return relpath
    extension_match = _resolve_extensionless_path(project_root, relpath)
    return extension_match if extension_match is not None else relpath


def _path_is_mechanically_attributable(
    citation: PathCitation,
    *,
    project_root: Path,
    relpath: str,
) -> bool:
    resolved = _resolved_repo_relpath(project_root, relpath)
    if (project_root / resolved).exists():
        return True
    if not resolved.endswith(_PATH_SUFFIXES):
        return False
    return "/" in citation.path


def _path_presence_evidence(
    citation: PathCitation,
    *,
    project_root: Path,
) -> list[EvidenceEntry]:
    relpath = _normalize_repo_path(project_root, citation.path)
    relpath = _resolved_repo_relpath(project_root, relpath)
    absolute = project_root / relpath
    checked: list[EvidenceEntry] = []
    line_label = _line_label(relpath, citation)

    if absolute.exists():
        if citation.line is not None:
            line_count = _file_line_count(absolute)
            range_end = citation.end_line or citation.line
            if range_end > line_count:
                checked.append(
                    EvidenceEntry(
                        evidence_id=f"path-line:{line_label}:absent",
                        kind="staleness",
                        summary=(
                            f"cited line {line_label} absent from current file "
                            f"(file has {line_count} line(s))"
                        ),
                        detail=f"checked {line_label} against the current tree",
                        observed_status=STATUS_STALE,
                        artifact=line_label,
                    )
                )
            else:
                checked.append(
                    EvidenceEntry(
                        evidence_id=f"path-line:{line_label}:present",
                        kind="artifact_presence",
                        summary=f"cited line {line_label} still falls within the current file",
                        detail=f"checked {line_label} against the current tree",
                        artifact=line_label,
                    )
                )
        else:
            checked.append(
                EvidenceEntry(
                    evidence_id=f"path:{relpath}:present",
                    kind="artifact_presence",
                    summary=f"cited path {relpath} present in current tree",
                    detail=f"checked {relpath} at HEAD",
                    artifact=relpath,
                )
            )
    else:
        if not _path_is_mechanically_attributable(
            citation,
            project_root=project_root,
            relpath=relpath,
        ):
            checked.append(
                EvidenceEntry(
                    evidence_id=f"path:{relpath}:unverified",
                    kind="unverified",
                    summary=f"could not attribute cited filename {relpath} to a repo path",
                    detail=(
                        "bare filename mentions without a repo-relative path stay unverified when "
                        "they do not resolve in the current tree"
                    ),
                    checkable=False,
                    observed_status=STATUS_UNVERIFIED,
                    artifact=relpath,
                )
            )
            return checked
        checked.append(
            EvidenceEntry(
                evidence_id=f"path:{relpath}:absent",
                kind="staleness",
                summary=f"cited path {relpath} absent from current tree",
                detail=f"checked {relpath} at HEAD",
                observed_status=STATUS_STALE,
                artifact=relpath,
            )
        )
    return checked


def _path_churn_evidence(
    issue: BacklogIssue,
    *,
    project_root: Path,
    relpath: str,
    churn_counter: Callable[[Path, str, str], int],
) -> tuple[list[EvidenceEntry], list[EvidenceEntry]]:
    checked: list[EvidenceEntry] = []
    failed: list[EvidenceEntry] = []
    try:
        churn = churn_counter(project_root, relpath, issue.created_at)
    except Exception as exc:  # noqa: BLE001 - becomes explicit unverified evidence
        failed.append(
            EvidenceEntry(
                evidence_id=f"path:{relpath}:churn-unverified",
                kind="unverified",
                summary=f"could not count churn for cited path {relpath}",
                detail=f"git history query failed: {exc}",
                checkable=False,
                observed_status=STATUS_UNVERIFIED,
                artifact=relpath,
            )
        )
    else:
        checked.append(
            EvidenceEntry(
                evidence_id=f"path:{relpath}:churn",
                kind="churn",
                summary=(
                    f"cited file {relpath} changed {churn} time(s) since filing on "
                    f"{issue.created_at[:10]}"
                ),
                detail=f"git log --since={issue.created_at} -- {relpath}",
                artifact=relpath,
            )
        )
    return checked, failed


def _symbol_evidence(
    issue: BacklogIssue,
    citation: SymbolCitation,
    *,
    project_root: Path,
    candidate_paths: tuple[str, ...],
    churn_counter: Callable[[Path, str, str], int],
    symbol_lookup: Callable[
        [Path, str, tuple[str, ...]],
        list[tuple[str, int]] | SymbolLookupResult,
    ],
) -> tuple[list[EvidenceEntry], list[EvidenceEntry]]:
    checked: list[EvidenceEntry] = []
    failed: list[EvidenceEntry] = []
    try:
        lookup_result = symbol_lookup(project_root, citation.symbol, candidate_paths)
    except Exception as exc:  # noqa: BLE001 - becomes explicit unverified evidence
        failed.append(
            EvidenceEntry(
                evidence_id=f"symbol:{citation.symbol}:lookup-unverified",
                kind="unverified",
                summary=f"could not search for cited symbol {citation.symbol}",
                detail=f"symbol search failed: {exc}",
                checkable=False,
                observed_status=STATUS_UNVERIFIED,
                artifact=citation.symbol,
            )
        )
        return checked, failed

    if isinstance(lookup_result, SymbolLookupResult):
        result = lookup_result
    else:
        result = SymbolLookupResult(
            hits=tuple(lookup_result),
            searched_scopes=candidate_paths or (".",),
        )

    if result.unresolved_candidates:
        failed.append(
            EvidenceEntry(
                evidence_id=f"symbol:{citation.symbol}:candidate-paths-unverified",
                kind="unverified",
                summary=(
                    f"skipped {len(result.unresolved_candidates)} unresolvable candidate "
                    f"path(s) while searching for cited symbol {citation.symbol}"
                ),
                detail=", ".join(result.unresolved_candidates),
                checkable=False,
                observed_status=STATUS_UNVERIFIED,
                artifact=citation.symbol,
            )
        )

    if not result.hits and not result.searched_scopes:
        return checked, failed

    if not result.hits:
        checked.append(
            EvidenceEntry(
                evidence_id=f"symbol:{citation.symbol}:absent",
                kind="staleness",
                summary=f"cited symbol {citation.symbol} absent from current tree",
                detail=f"searched for {citation.symbol} in {', '.join(result.searched_scopes)}",
                observed_status=STATUS_STALE,
                artifact=citation.symbol,
            )
        )
        return checked, failed

    unique_paths = sorted({path for path, _line in result.hits}, key=str.lower)
    if len(unique_paths) != 1:
        failed.append(
            EvidenceEntry(
                evidence_id=f"symbol:{citation.symbol}:ambiguous",
                kind="unverified",
                summary=(
                    f"cited symbol {citation.symbol} resolves to {len(unique_paths)} files; "
                    "churn is not attributable mechanically"
                ),
                detail=", ".join(unique_paths),
                checkable=False,
                observed_status=STATUS_UNVERIFIED,
                artifact=citation.symbol,
            )
        )
        return checked, failed

    target_path = unique_paths[0]
    first_line = next(line for path, line in result.hits if path == target_path)
    checked.append(
        EvidenceEntry(
            evidence_id=f"symbol:{citation.symbol}:present",
            kind="artifact_presence",
            summary=f"cited symbol {citation.symbol} present at {target_path}:{first_line}",
            detail=f"searched for {citation.symbol} at HEAD",
            artifact=f"{target_path}:{first_line}",
        )
    )
    try:
        churn = churn_counter(project_root, target_path, issue.created_at)
    except Exception as exc:  # noqa: BLE001 - becomes explicit unverified evidence
        failed.append(
            EvidenceEntry(
                evidence_id=f"symbol:{citation.symbol}:churn-unverified",
                kind="unverified",
                summary=f"could not count churn for cited symbol {citation.symbol}",
                detail=f"git history query failed for {target_path}: {exc}",
                checkable=False,
                observed_status=STATUS_UNVERIFIED,
                artifact=citation.symbol,
            )
        )
    else:
        checked.append(
            EvidenceEntry(
                evidence_id=f"symbol:{citation.symbol}:churn",
                kind="churn",
                summary=(
                    f"cited file {target_path} changed {churn} time(s) since filing on "
                    f"{issue.created_at[:10]}"
                ),
                detail=f"git log --since={issue.created_at} -- {target_path}",
                artifact=target_path,
            )
        )
    return checked, failed


def _verification_status(entries: tuple[EvidenceEntry, ...]) -> str:
    """Roll up finding verification with stale checkable evidence taking precedence."""
    has_checkable = any(entry.checkable for entry in entries)
    has_uncheckable = any(not entry.checkable for entry in entries)
    if not has_checkable and has_uncheckable:
        return STATUS_UNVERIFIED
    if any(entry.observed_status == STATUS_STALE for entry in entries):
        return STATUS_STALE
    if has_checkable and has_uncheckable:
        return STATUS_PARTIAL
    return STATUS_ACTIVE


def _age_days(created_at: str, *, now: datetime) -> int | None:
    try:
        opened = _parse_iso8601(created_at)
    except ValueError:
        return None
    return (now.date() - opened.date()).days


def _dedupe_evidence(entries: list[EvidenceEntry]) -> tuple[EvidenceEntry, ...]:
    deduped: list[EvidenceEntry] = []
    seen_ids: set[str] = set()
    for entry in entries:
        if entry.evidence_id in seen_ids:
            continue
        seen_ids.add(entry.evidence_id)
        deduped.append(entry)
    return tuple(deduped)


def build_backlog_report(
    issues: list[BacklogIssue],
    *,
    project_root: Path,
    current_milestone: str | None,
    named_milestones: tuple[str, ...],
    now: datetime | None = None,
    churn_counter: Callable[[Path, str, str], int] | None = None,
    symbol_lookup: Callable[
        [Path, str, tuple[str, ...]],
        list[tuple[str, int]] | SymbolLookupResult,
    ]
    | None = None,
    milestone_inventory_error: str | None = None,
) -> BacklogTriageReport:
    """Build the deterministic report for an already-fetched backlog issue set."""
    now = now or datetime.now(UTC)
    churn_counter = churn_counter or _git_churn_count
    symbol_lookup = symbol_lookup or _symbol_hits

    findings: list[BacklogFindingRecord] = []
    for issue in sorted(issues, key=lambda item: item.number):
        paths, symbols, invalid_paths = parse_citations(issue.body)
        evidence: list[EvidenceEntry] = []
        failures: list[EvidenceEntry] = []

        if not paths and not symbols and not invalid_paths:
            failures.append(
                EvidenceEntry(
                    evidence_id="citation:none",
                    kind="unverified",
                    summary=(
                        "body cites no checkable artifact (prose-only or unparseable citation)"
                    ),
                    detail=(
                        "no repo path or symbol-like citation could be parsed from the issue body"
                    ),
                    checkable=False,
                    observed_status=STATUS_UNVERIFIED,
                )
            )

        for invalid in invalid_paths:
            failures.append(
                EvidenceEntry(
                    evidence_id=f"path:{invalid.raw}:invalid",
                    kind="unverified",
                    summary=f"could not mechanically verify cited path token {invalid.raw}",
                    detail=invalid.detail,
                    checkable=False,
                    observed_status=STATUS_UNVERIFIED,
                    artifact=invalid.raw,
                )
            )

        normalized_paths: list[str] = []
        churn_paths: dict[str, None] = {}
        for citation in paths:
            try:
                relpath = _normalize_repo_path(project_root, citation.path)
            except ValueError as exc:
                failures.append(
                    EvidenceEntry(
                        evidence_id=f"path:{citation.path}:invalid",
                        kind="unverified",
                        summary=f"could not normalize cited path {citation.path}",
                        detail=str(exc),
                        checkable=False,
                        observed_status=STATUS_UNVERIFIED,
                        artifact=citation.path,
                    )
                )
                continue
            evidence.extend(_path_presence_evidence(citation, project_root=project_root))
            if _path_is_mechanically_attributable(
                citation,
                project_root=project_root,
                relpath=relpath,
            ):
                resolved_relpath = _resolved_repo_relpath(project_root, relpath)
                normalized_paths.append(resolved_relpath)
                churn_paths.setdefault(resolved_relpath, None)

        for relpath in churn_paths:
            checked, failed = _path_churn_evidence(
                issue,
                project_root=project_root,
                relpath=relpath,
                churn_counter=churn_counter,
            )
            evidence.extend(checked)
            failures.extend(failed)

        candidate_paths = tuple(sorted(set(normalized_paths), key=str.lower))
        for citation in symbols:
            checked, failed = _symbol_evidence(
                issue,
                citation,
                project_root=project_root,
                candidate_paths=candidate_paths,
                churn_counter=churn_counter,
                symbol_lookup=symbol_lookup,
            )
            evidence.extend(checked)
            failures.extend(failed)

        ordered_evidence = _dedupe_evidence(
            sorted(
                [*failures, *evidence],
                key=lambda entry: (
                    0 if not entry.checkable else 1,
                    0 if entry.observed_status == STATUS_STALE else 1,
                    entry.evidence_id,
                ),
            )
        )
        findings.append(
            BacklogFindingRecord(
                finding_id=issue.issue_ref,
                issue_ref=issue.issue_ref,
                issue_number=issue.number,
                title=issue.title,
                body=issue.body,
                labels=issue.labels,
                display_labels=issue.display_labels(),
                opened_at=issue.created_at,
                age_days=_age_days(issue.created_at, now=now),
                pool_state=issue.state_label(),
                verification_status=_verification_status(ordered_evidence),
                evidence=ordered_evidence,
            )
        )

    return BacklogTriageReport(
        generated_at=now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        current_milestone=current_milestone,
        named_milestones=named_milestones,
        findings=tuple(findings),
        milestone_inventory_error=milestone_inventory_error,
    )


def collect_backlog_report(
    project_root: Path,
    *,
    current_milestone: str | None = None,
    now: datetime | None = None,
) -> BacklogTriageReport:
    """Fetch the live backlog and build its deterministic report artifact."""
    issues = fetch_backlog_issues(project_root)
    milestone_inventory_error: str | None = None
    try:
        open_milestones = fetch_open_milestones(project_root)
    except TriageBacklogReportError as exc:
        open_milestones = ()
        milestone_inventory_error = str(exc)

    named_milestones = tuple(
        title
        for title in open_milestones
        if title
        and title != HYGIENE_POOL
        and (current_milestone is None or title.lower() != current_milestone.lower())
    )
    return build_backlog_report(
        issues,
        project_root=project_root,
        current_milestone=current_milestone,
        named_milestones=named_milestones,
        now=now,
        milestone_inventory_error=milestone_inventory_error,
    )


def default_report_path(project_root: Path, generated_at: str) -> Path:
    stamp = generated_at.replace("-", "").replace(":", "").replace("+00:00", "Z")
    stamp = stamp.replace(".", "")
    return project_root / ".forge" / "triage" / f"report-{stamp}.yaml"


def write_backlog_report(
    project_root: Path,
    report: BacklogTriageReport,
    *,
    output_path: Path | None = None,
) -> Path:
    """Write the structured artifact and return its path."""
    target = output_path or default_report_path(project_root, report.generated_at)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = yaml.safe_dump(report.to_dict(), sort_keys=False, default_flow_style=False)
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(target)
    return target


def _state_summary_label(state: str) -> str:
    if state == HYGIENE_POOL:
        return "hygiene-pool"
    return state


def _headline_evidence(
    finding: BacklogFindingRecord,
) -> tuple[EvidenceEntry | None, list[EvidenceEntry]]:
    entries = list(finding.evidence)
    if not entries:
        return None, []
    if finding.verification_status == STATUS_UNVERIFIED:
        preferred = [entry for entry in entries if not entry.checkable]
    elif finding.verification_status == STATUS_STALE:
        preferred = [entry for entry in entries if entry.observed_status == STATUS_STALE]
    elif finding.verification_status == STATUS_PARTIAL:
        preferred = [entry for entry in entries if entry.checkable and entry.kind != "churn"]
        preferred = preferred or [entry for entry in entries if entry.checkable]
    else:
        preferred = [entry for entry in entries if entry.kind == "churn"] or entries
    first = preferred[0]
    remaining = [entry for entry in entries if entry is not first]
    return first, remaining


def render_backlog_report(
    report: BacklogTriageReport,
    artifact_path: Path,
    project_root: Path,
) -> str:
    """Render the operator-facing summary."""
    if not report.findings:
        path_text = (
            str(artifact_path.relative_to(project_root))
            if artifact_path.is_relative_to(project_root)
            else str(artifact_path)
        )
        return "\n".join(
            [
                "FINDING BACKLOG — 0 open",
                "",
                "No open finding-labeled issues matched the backlog query.",
                f"structured report: {path_text}",
            ]
        )

    state_counts = Counter(finding.pool_state for finding in report.findings)
    summary_bits = [
        f"{count} {_state_summary_label(state)}"
        for state, count in sorted(state_counts.items(), key=lambda item: item[0].lower())
    ]
    lines = [f"FINDING BACKLOG — {len(report.findings)} open ({', '.join(summary_bits)})", ""]
    for finding in report.findings:
        headline, remainder = _headline_evidence(finding)
        age = f"{finding.age_days}d" if finding.age_days is not None else "unknown"
        status_label = finding.verification_status.upper().replace("_", "-")
        first_summary = headline.summary if headline is not None else "no evidence entries"
        lines.append(
            f"{finding.issue_ref:<6} {finding.display_labels:<10} {finding.pool_state:<12} "
            f"age {age:<7} {status_label}: {first_summary}"
        )
        for entry in remainder:
            lines.append(f"       {entry.summary}")
    lines.append("")
    if report.milestone_inventory_error:
        lines.append(f"milestone inventory warning: {report.milestone_inventory_error}")
    path_text = (
        str(artifact_path.relative_to(project_root))
        if artifact_path.is_relative_to(project_root)
        else str(artifact_path)
    )
    lines.append(f"structured report: {path_text}")
    return "\n".join(lines)
