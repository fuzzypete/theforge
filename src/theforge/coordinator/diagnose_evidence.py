"""Auto-inject starting evidence for the diagnose flow.

When an operator files a symptom bug they usually name the concrete artifact
the symptom lives in — a sprint run id, a sprint id, a branch, a PR/issue
number, or an audit/log path. The diagnose agent has a fixed budget and a
600-second wall-clock; making it *rediscover* pointers the operator already
wrote is wasted budget it could spend on hypothesis-formation instead.

This module is the deterministic, mechanical pre-load step: it scans the issue
body for recognizable references, loads bounded excerpts of the matching
log/audit/PR-history, and renders a ``STARTING EVIDENCE`` block the flow embeds
in the prompt before invoking the agent.

Design constraints:

- **Best-effort.** An issue with no recognizable references (or references that
  resolve to nothing on disk) produces an empty block — the flow then behaves
  exactly as it did before this feature existed.
- **Namespaced.** A reference is only meaningful relative to the namespace it
  was written in. A bare ``#NNNN`` carries no namespace, so this module refuses
  to resolve it rather than defaulting to whichever repository the orchestrator
  happens to be executing in: an issue filed here about an adopter project's
  ``#246`` must not be handed this repository's PR #246 as "evidence". Only
  repository-qualified references (``owner/repo#NNNN`` or a github.com issue/PR
  URL) name a namespace, and those are resolved against the repository they
  name. Loading nothing is a correct outcome; loading same-numbered content
  from the wrong repository is not.
- **Bounded.** Every excerpt is truncated to a fixed line/char cap, each
  reference kind is capped in count, and the whole block is capped in total
  size. The operator can predict the maximum prompt growth from the constants
  below; nothing an issue body cites can explode the prompt.
- **Pure orchestrator work.** No LLM calls. Extraction is regex-driven; loading
  is filesystem reads plus (optional) ``gh`` queries that fail open.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_log = logging.getLogger(__name__)

# ── Bounding constants (the operator-predictable excerpting rules) ─────
# The maximum size of the injected block is deterministic:
#   <= _MAX_TOTAL_EVIDENCE_CHARS overall, assembled from at most
#   _MAX_ITEMS_PER_KIND items of each kind, each <= _MAX_EXCERPT_CHARS.
_MAX_LOG_TAIL_LINES = 30  # run/sprint logs: last N lines only
_MAX_FILE_HEAD_LINES = 40  # generic audit/state files: first N lines
_MAX_EXCERPT_CHARS = 4000  # hard char cap on any single excerpt body
_MAX_ITEMS_PER_KIND = 5  # cap distinct references loaded per kind
# gh-backed loaders cost a subprocess (up to 30s each) per *attempted* reference
# whether or not it resolves. _MAX_ITEMS_PER_KIND only bounds *successful* loads,
# so an issue body citing many unresolved branches/#NNNN could still fire one gh
# call per reference before the agent starts. This caps attempts (resolved or
# not) so the pre-load step's wall-clock is bounded regardless of body length.
_MAX_GH_ATTEMPTS_PER_KIND = 5
_MAX_TOTAL_EVIDENCE_CHARS = 20000  # cap on the whole rendered block body
_MAX_HISTORY_LINE_MATCHES = 2  # history.jsonl lines loaded per run id
_HISTORY_LINE_CHAR_CAP = 800  # per-line truncation of a history match

# ── Reference-detection patterns ──────────────────────────────────────
# A run id or sprint id is a 12-hex token (see diagnose_flow._generate_run_id
# and sprint id generation). The same token is probed against both the run-log
# glob and the sprint dir, since the two id spaces are indistinguishable by
# shape alone.
_HEX_ID_RE = re.compile(r"\b[0-9a-f]{12}\b")
# Branch names TheForge creates: a conventional-commit-style prefix plus a slug.
_BRANCH_PREFIXES = ("feat", "fix", "chore", "refactor", "docs", "test", "perf", "hotfix")
_BRANCH_RE = re.compile(rf"\b(?:{'|'.join(_BRANCH_PREFIXES)})/[A-Za-z0-9._/-]+")
# PR / issue cross-references. Only *repository-qualified* forms are recognized;
# a bare "#NNNN" names no repository and is deliberately not matched (see the
# module docstring's namespace constraint).
#   owner/repo#NNNN — the lookbehind keeps the owner segment from starting
#   mid-path; a "feat/…"-style branch head is rejected separately in
#   _extract_qualified_refs, since "feat/issue-2057#3" is shaped like a
#   repo-qualified reference but names a branch, not a repository.
_QUALIFIED_REF_RE = re.compile(
    r"(?<![A-Za-z0-9._/-])([A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*)#(\d{1,7})\b"
)
#   https://github.com/owner/repo/issues/NNNN (or /pull/NNNN)
_GITHUB_URL_REF_RE = re.compile(
    r"https?://(?:www\.)?github\.com/([A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"/(?:issues|pull)/(\d{1,7})\b"
)
# Bare, namespace-free references — matched only so the flow can *report* that
# they were declined. They are never resolved.
_BARE_REF_RE = re.compile(r"(?<![A-Za-z0-9._/-])#(\d{1,7})\b")
# An explicit ``.forge/...`` file path with an extension (audit, log, jsonl…).
_FORGE_PATH_RE = re.compile(r"\.forge/[A-Za-z0-9._:,/-]+\.[A-Za-z0-9]+")


@dataclass
class StartingEvidence:
    """The rendered ``STARTING EVIDENCE`` block plus what fed it.

    ``text`` is the full section (header + items) ready to embed in the prompt,
    or ``""`` when nothing was found. ``reference_labels`` is the list of short
    human labels for each excerpt that was loaded — recorded in the audit so an
    operator can see, deterministically, what the orchestrator handed the agent.

    ``declined_labels`` lists namespace-free references the body cited that were
    deliberately *not* resolved (bare ``#NNNN``). Recorded for the same reason:
    an operator reading the audit can see that a reference was seen and skipped
    on purpose, rather than inferring silence from an empty evidence block.
    """

    text: str = ""
    reference_labels: list[str] = field(default_factory=list)
    declined_labels: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.text


@dataclass
class _Item:
    """One loaded excerpt: a short label/header and its (already-bounded) body."""

    label: str
    header: str
    body: str


# ── Ordered, deduped reference extraction ─────────────────────────────


def _ordered_unique(matches: list[str]) -> list[str]:
    """Return ``matches`` de-duplicated, preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for m in matches:
        if m in seen:
            continue
        seen.add(m)
        out.append(m)
    return out


def _truncate(text: str, *, max_chars: int = _MAX_EXCERPT_CHARS) -> str:
    """Clip ``text`` to ``max_chars`` with a visible truncation marker."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n… [truncated]"


def _tail_lines(text: str, n: int) -> str:
    lines = text.splitlines()
    if len(lines) <= n:
        return text.rstrip("\n")
    return "\n".join(lines[-n:])


def _head_lines(text: str, n: int) -> str:
    lines = text.splitlines()
    if len(lines) <= n:
        return text.rstrip("\n")
    return "\n".join(lines[:n])


def _read_text(path: Path) -> str | None:
    """Read a file as UTF-8, returning None on any failure (best-effort)."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


# ── Per-kind loaders (each returns a list of _Item, bounded) ──────────


def _load_run_logs(hex_ids: list[str], project_root: Path) -> list[_Item]:
    """For each hex id that matches a ``run-<id>.log``, load its last lines."""
    items: list[_Item] = []
    logs_root = project_root / ".forge" / "logs"
    for hid in hex_ids:
        if len(items) >= _MAX_ITEMS_PER_KIND:
            break
        matches = sorted(logs_root.glob(f"*/run-{hid}.log"))
        if not matches:
            continue
        log_path = matches[0]
        text = _read_text(log_path)
        if not text:
            continue
        rel = log_path.relative_to(project_root)
        body = _truncate(_tail_lines(text, _MAX_LOG_TAIL_LINES))
        items.append(
            _Item(
                label=f"run log {hid}",
                header=f"Run log {rel} (last {_MAX_LOG_TAIL_LINES} lines):",
                body=body,
            )
        )
    return items


def _load_sprint_states(hex_ids: list[str], project_root: Path) -> list[_Item]:
    """For each hex id that matches a ``.forge/sprints/<id>/`` dir, load state."""
    items: list[_Item] = []
    sprints_root = project_root / ".forge" / "sprints"
    for hid in hex_ids:
        if len(items) >= _MAX_ITEMS_PER_KIND:
            break
        state_path = sprints_root / hid / "state.yaml"
        if not state_path.is_file():
            continue
        text = _read_text(state_path)
        if not text:
            continue
        rel = state_path.relative_to(project_root)
        body = _truncate(_head_lines(text, _MAX_FILE_HEAD_LINES))
        items.append(
            _Item(
                label=f"sprint state {hid}",
                header=f"Sprint state {rel} (first {_MAX_FILE_HEAD_LINES} lines):",
                body=body,
            )
        )
    return items


def _load_history_entries(hex_ids: list[str], project_root: Path) -> list[_Item]:
    """Load ``history.jsonl`` lines mentioning a cited run/sprint id.

    Streams the file line by line and stops after collecting a bounded number
    of matches so a large history file cannot blow the budget.
    """
    history_path = project_root / ".forge" / "audits" / "history.jsonl"
    if not hex_ids or not history_path.is_file():
        return []
    wanted = set(hex_ids)
    found: dict[str, list[str]] = {hid: [] for hid in hex_ids}
    remaining = len(wanted)
    try:
        with history_path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if remaining <= 0:
                    break
                for hid in list(wanted):
                    if hid in line:
                        bucket = found[hid]
                        if len(bucket) < _MAX_HISTORY_LINE_MATCHES:
                            bucket.append(line.strip()[:_HISTORY_LINE_CHAR_CAP])
                        if len(bucket) >= _MAX_HISTORY_LINE_MATCHES:
                            wanted.discard(hid)
                            remaining -= 1
    except OSError:
        return []

    items: list[_Item] = []
    for hid in hex_ids:
        lines = found.get(hid) or []
        if not lines:
            continue
        if len(items) >= _MAX_ITEMS_PER_KIND:
            break
        body = _truncate("\n".join(lines))
        items.append(
            _Item(
                label=f"history {hid}",
                header=f"history.jsonl entries mentioning {hid}:",
                body=body,
            )
        )
    return items


def _load_forge_paths(paths: list[str], project_root: Path) -> list[_Item]:
    """Load bounded excerpts of explicitly-cited ``.forge/...`` files."""
    items: list[_Item] = []
    for rel in paths:
        if len(items) >= _MAX_ITEMS_PER_KIND:
            break
        target = project_root / rel
        if not target.is_file():
            continue
        text = _read_text(target)
        if not text:
            continue
        # A .log path is most useful at its tail; everything else at its head.
        if rel.endswith(".log"):
            excerpt = _tail_lines(text, _MAX_LOG_TAIL_LINES)
            where = f"last {_MAX_LOG_TAIL_LINES} lines"
        else:
            excerpt = _head_lines(text, _MAX_FILE_HEAD_LINES)
            where = f"first {_MAX_FILE_HEAD_LINES} lines"
        items.append(
            _Item(
                label=f"file {rel}",
                header=f"{rel} ({where}):",
                body=_truncate(excerpt),
            )
        )
    return items


# ── gh-backed loaders (fail open when gh is unavailable) ──────────────


def _run_gh(args: list[str], project_root: Path) -> str | None:
    """Run a ``gh`` query, returning stdout or None on any failure."""
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _load_branch_history(branches: list[str], project_root: Path) -> list[_Item]:
    """For each cited branch, load its PR history via ``gh pr list --head``."""
    items: list[_Item] = []
    # Bound attempts, not just successes: every iteration fires a gh call
    # regardless of whether it resolves, so an unresolved-heavy body must not
    # spend one subprocess timeout per reference.
    for branch in branches[:_MAX_GH_ATTEMPTS_PER_KIND]:
        if len(items) >= _MAX_ITEMS_PER_KIND:
            break
        out = _run_gh(
            [
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "all",
                "--json",
                "number,state,title,mergedAt",
            ],
            project_root,
        )
        if not out or out == "[]":
            continue
        items.append(
            _Item(
                label=f"branch {branch}",
                header=f"PR history for {branch} (gh pr list --head {branch} --state all):",
                body=_truncate(out),
            )
        )
    return items


def _load_pr_issue_refs(
    refs: list[tuple[str, str]], project_root: Path, *, self_issue_number: int | None
) -> list[_Item]:
    """Load a bounded summary for each repo-qualified reference.

    ``refs`` is a list of ``(owner/repo, number)`` pairs — every reference
    carries the namespace it was written in, and each lookup is pinned to that
    namespace with ``gh --repo``. ``project_root`` is still the subprocess cwd
    (it is where ``gh``'s auth/config resolve from), but it no longer decides
    *which* repository a number means.

    A reference whose number equals ``self_issue_number`` is skipped: it is
    either the issue under diagnosis (self-evidence is worthless) or a
    same-numbered issue elsewhere, and declining to load is the safe direction
    for both.
    """
    items: list[_Item] = []
    # Drop the self-issue first (it costs no gh call), then bound gh *attempts*:
    # each remaining reference fires 1-2 gh subprocesses whether or not it
    # resolves, so a body citing many unresolved refs must not spend one timeout
    # per reference before the agent starts.
    self_ref = str(self_issue_number) if self_issue_number is not None else None
    candidates = [(repo, num) for repo, num in refs if num != self_ref]
    for repo, num in candidates[:_MAX_GH_ATTEMPTS_PER_KIND]:
        if len(items) >= _MAX_ITEMS_PER_KIND:
            break
        # A #NNNN can be a PR or an issue. Try PR first (richer landing signal),
        # fall back to the issue view. Both fail open.
        pr_out = _run_gh(
            [
                "pr",
                "view",
                num,
                "--repo",
                repo,
                "--json",
                "number,state,title,mergedAt,mergeCommit",
            ],
            project_root,
        )
        if pr_out:
            items.append(
                _Item(
                    label=f"PR {repo}#{num}",
                    header=f"PR {repo}#{num} (gh pr view {num} --repo {repo}):",
                    body=_truncate(pr_out),
                )
            )
            continue
        issue_out = _run_gh(
            ["issue", "view", num, "--repo", repo, "--json", "number,state,title"],
            project_root,
        )
        if issue_out:
            items.append(
                _Item(
                    label=f"issue {repo}#{num}",
                    header=f"Issue {repo}#{num} (gh issue view {num} --repo {repo}):",
                    body=_truncate(issue_out),
                )
            )
    return items


# ── Public entry point ────────────────────────────────────────────────


def build_starting_evidence(
    *,
    issue_body: str,
    project_root: Path,
    self_issue_number: int | None = None,
) -> StartingEvidence:
    """Scan ``issue_body`` for references and pre-load bounded excerpts.

    Returns a :class:`StartingEvidence` whose ``text`` is the rendered
    ``== STARTING EVIDENCE (auto-loaded from issue body references) ==``
    section, or an empty ``StartingEvidence`` when no reference resolves to
    anything on disk / via ``gh``. The whole block is capped at
    ``_MAX_TOTAL_EVIDENCE_CHARS``; excess items are dropped (and logged) rather
    than silently exploding the prompt.

    Bare ``#NNNN`` references are reported in ``declined_labels`` and never
    resolved — they name no repository, and the repository this call happens to
    run in is not a substitute for the one the reference was written about.
    """
    body = issue_body or ""

    hex_ids = _ordered_unique(_HEX_ID_RE.findall(body))
    branches = _ordered_unique([m.rstrip(".,;:)]}'\"") for m in _BRANCH_RE.findall(body)])
    qualified_refs = _extract_qualified_refs(body)
    forge_paths = _ordered_unique([m.rstrip(".,;:)]}'\"") for m in _FORGE_PATH_RE.findall(body)])

    declined = _declined_bare_refs(body, self_issue_number=self_issue_number)

    items: list[_Item] = []
    items += _load_run_logs(hex_ids, project_root)
    items += _load_sprint_states(hex_ids, project_root)
    items += _load_history_entries(hex_ids, project_root)
    items += _load_forge_paths(forge_paths, project_root)
    items += _load_branch_history(branches, project_root)
    items += _load_pr_issue_refs(qualified_refs, project_root, self_issue_number=self_issue_number)

    if not items:
        return StartingEvidence(declined_labels=declined)

    return _render(items, declined_labels=declined)


def _extract_qualified_refs(body: str) -> list[tuple[str, str]]:
    """Return ordered, deduped ``(owner/repo, number)`` pairs cited in ``body``.

    Both written forms are accepted — ``owner/repo#NNNN`` and a github.com
    issue/PR URL — and both carry the namespace the reference was written in.
    """
    pairs = _QUALIFIED_REF_RE.findall(body) + _GITHUB_URL_REF_RE.findall(body)
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for repo, num in pairs:
        # "feat/issue-2057#3" is a branch plus a number, not owner/repo#number.
        # An ambiguous token resolves to nothing rather than to a guess.
        if repo.split("/", 1)[0] in _BRANCH_PREFIXES:
            continue
        key = (repo, num)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _declined_bare_refs(body: str, *, self_issue_number: int | None) -> list[str]:
    """Return labels for namespace-free ``#NNNN`` references that were skipped.

    A bare number that is already qualified elsewhere in the body (the ``#NNNN``
    tail of an ``owner/repo#NNNN``) is not a decline, and neither is a mention of
    the issue under diagnosis.
    """
    qualified = {num for _repo, num in _extract_qualified_refs(body)}
    self_ref = str(self_issue_number) if self_issue_number is not None else None
    out: list[str] = []
    for num in _ordered_unique(_BARE_REF_RE.findall(body)):
        if num in qualified or num == self_ref:
            continue
        out.append(f"#{num}")
    if out:
        _log.debug(
            "starting-evidence: declined %d unqualified reference(s): %s",
            len(out),
            ", ".join(out),
        )
    return out


def _render(items: list[_Item], *, declined_labels: list[str] | None = None) -> StartingEvidence:
    """Assemble loaded items into the bounded STARTING EVIDENCE block."""
    header = "== STARTING EVIDENCE (auto-loaded from issue body references) =="
    rendered: list[str] = []
    labels: list[str] = []
    total = 0
    dropped = 0
    for item in items:
        block = f"{item.header}\n{item.body}"
        # +2 accounts for the blank-line separator between items.
        if total + len(block) + 2 > _MAX_TOTAL_EVIDENCE_CHARS and rendered:
            dropped += 1
            continue
        rendered.append(block)
        labels.append(item.label)
        total += len(block) + 2

    if dropped:
        _log.debug("starting-evidence: dropped %d item(s) over total char cap", dropped)
        rendered.append(f"[{dropped} further reference excerpt(s) omitted to bound the prompt]")

    text = header + "\n\n" + "\n\n".join(rendered)
    return StartingEvidence(
        text=text,
        reference_labels=labels,
        declined_labels=list(declined_labels or []),
    )
