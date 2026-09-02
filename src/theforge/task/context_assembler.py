from __future__ import annotations

import datetime
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from . import invariant_manifest, invariant_selector, prior_run_manifest, prior_run_selector
from .invariant_selector import INVARIANT_KIND
from .prior_run_selector import PRIOR_RUN_KIND

if TYPE_CHECKING:
    from theforge.config.types import ForgeConfig


_PHASE_TO_BUDGET_ATTR = {
    "preflight": "preflight_budget",
    "plan": "plan_budget",
    "dev": "dev_budget",
    "review": "review_budget",
}

_INDEX_CANDIDATES = (
    "STRUCTURAL_INDEX.md",
    ".forge/STRUCTURAL_INDEX.md",
    ".forge/structural_index.md",
    "STRUCTURAL_INDEX.yaml",
    ".forge/STRUCTURAL_INDEX.yaml",
    ".forge/structural_index.yaml",
)


@dataclass(frozen=True)
class ContextBudgetConfig:
    preflight_budget: int = 200
    plan_budget: int = 120
    dev_budget: int = 80
    review_budget: int = 80


@dataclass(frozen=True)
class ContextItem:
    source: str
    kind: str
    required: bool
    lines: int
    content: str
    reason: str
    score: int = 0
    # Identity of the indexed thing this item renders — a run id for prior-run
    # summaries, an invariant id for project invariants. Carried explicitly
    # because the budget loop later has to report *which* entries survived, and
    # re-deriving an id by splitting ``source`` makes that decision depend on
    # whether a project's own file paths happen to contain the delimiter.
    # ``None`` for items that render no indexed entity (docs, structural index).
    item_id: str | None = None


@dataclass(frozen=True)
class ContextManifestEntry:
    source: str
    kind: str
    required: bool
    lines: int
    included: bool
    reason: str
    score: int = 0
    item_type: str = "advisory"
    drop_reason: str | None = None


@dataclass(frozen=True)
class ContextPack:
    content: str
    included: tuple[ContextManifestEntry, ...]
    dropped: tuple[ContextManifestEntry, ...]
    budget: int
    line_count: int
    phase: str = ""
    structural_index_git_sha: str | None = None
    # Audit-visible record of the prior-run knowledge this assembly considered.
    # Defaulted so the many constructors that predate #1860 keep working.
    prior_run_context: dict = field(default_factory=prior_run_manifest.disabled_manifest)
    # Audit-visible record of the project invariants this assembly considered,
    # including the ones it could not confidently scope. Defaulted for the same
    # reason as ``prior_run_context``: constructors predating #1875 keep working.
    invariant_context: dict = field(default_factory=invariant_manifest.disabled_manifest)


@dataclass(frozen=True)
class ClaudeDocSection:
    path: Path
    invariants: str
    advisory: str


class ContextAssembler:
    def __init__(
        self,
        project_root: Path,
        *,
        budgets: ContextBudgetConfig | None = None,
        prior_run_context: bool = False,
        invariant_context: bool = False,
    ) -> None:
        self.project_root = project_root
        self.budgets = budgets or ContextBudgetConfig()
        # Off unless a config explicitly turns it on: a direct
        # ContextAssembler(project_root) never injects prior-run knowledge.
        self.prior_run_context = prior_run_context
        self.invariant_context = invariant_context

    @classmethod
    def from_config(cls, config: "ForgeConfig") -> "ContextAssembler":
        ctx = config.context
        return cls(
            config.project_root,
            budgets=ContextBudgetConfig(
                preflight_budget=ctx.preflight_budget,
                plan_budget=ctx.plan_budget,
                dev_budget=ctx.dev_budget,
                review_budget=ctx.review_budget,
            ),
            prior_run_context=config.knowledge.prior_run_context,
            invariant_context=config.knowledge.invariant_context,
        )

    def assemble(
        self,
        *,
        phase: str,
        story_text: str,
        file_list: list[str] | None = None,
        budget: int | None = None,
        agent_role: str = "",
        phase_iteration: int | None = None,
    ) -> ContextPack:
        """Assemble one phase's context pack.

        ``agent_role`` and ``phase_iteration`` describe *who* is about to read
        this pack and *which* pass of their phase it is. They do not influence
        selection or prompt text; they are recorded so a later reader can tell
        what a given author could actually have acted on (#2684). Callers that
        omit them still assemble normally — the exposure record then names an
        unattributed recipient rather than silently claiming one.
        """
        normalized_phase = phase.lower()
        rendered_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        effective_budget = (
            budget if budget is not None else self.default_budget_for_phase(normalized_phase)
        )
        structural_index = self._read_structural_index()
        touched_dirs = self._touched_directories(
            file_list=file_list, story_text=story_text, structural_index=structural_index
        )
        docs = self._load_claude_docs(touched_dirs)

        required_items: list[ContextItem] = []
        advisory_items: list[ContextItem] = []

        if structural_index:
            advisory_items.append(
                ContextItem(
                    source=str(structural_index[0]),
                    kind="structural_index",
                    required=False,
                    lines=structural_index[1],
                    content=structural_index[2],
                    reason="structural index",
                    score=self._score_text(structural_index[2], story_text, normalized_phase),
                )
            )

        for doc in docs:
            if doc.invariants:
                required_items.append(
                    ContextItem(
                        source=str(doc.path),
                        kind="claude_invariants",
                        required=True,
                        lines=_count_lines(doc.invariants),
                        content=doc.invariants,
                        reason=f"invariants for {doc.path.parent}",
                        score=10_000,
                    )
                )
            if doc.advisory:
                advisory_items.append(
                    ContextItem(
                        source=str(doc.path),
                        kind="claude_advisory",
                        required=False,
                        lines=_count_lines(doc.advisory),
                        content=doc.advisory,
                        reason=f"advisory for {doc.path.parent}",
                        score=self._score_text(doc.advisory, story_text, normalized_phase),
                    )
                )

        prior_selection = self._select_prior_runs(
            phase=normalized_phase, story_text=story_text, file_list=file_list
        )
        if prior_selection is not None:
            for candidate in prior_selection.candidates:
                advisory_items.append(
                    ContextItem(
                        source=candidate.source,
                        kind=PRIOR_RUN_KIND,
                        required=False,
                        lines=_count_lines(candidate.content),
                        content=candidate.content,
                        reason=candidate.reason,
                        score=candidate.score,
                        item_id=candidate.run_id,
                    )
                )

        invariant_selection = self._select_invariants(
            phase=normalized_phase, story_text=story_text, file_list=file_list
        )
        if invariant_selection is not None:
            for invariant in invariant_selection.candidates:
                advisory_items.append(
                    ContextItem(
                        source=invariant.source,
                        kind=INVARIANT_KIND,
                        required=False,
                        lines=_count_lines(invariant.content),
                        content=invariant.content,
                        reason=invariant.reason,
                        score=invariant.score,
                        item_id=invariant.invariant_id,
                    )
                )

        included_items = list(required_items)
        dropped_items: list[ContextItem] = []
        used_lines = sum(item.lines for item in included_items)

        for item in sorted(advisory_items, key=lambda item: (-item.score, item.source, item.kind)):
            if used_lines + item.lines <= effective_budget:
                included_items.append(item)
                used_lines += item.lines
            else:
                dropped_items.append(item)

        content = "\n\n".join(
            item.content.strip() for item in included_items if item.content.strip()
        )
        included_manifest = tuple(
            ContextManifestEntry(
                source=item.source,
                kind=item.kind,
                required=item.required,
                lines=item.lines,
                included=True,
                reason=item.reason,
                score=item.score,
                item_type="invariant" if item.required else "advisory",
            )
            for item in included_items
        )
        dropped_manifest = tuple(
            ContextManifestEntry(
                source=item.source,
                kind=item.kind,
                required=item.required,
                lines=item.lines,
                included=False,
                reason=item.reason,
                score=item.score,
                item_type="invariant" if item.required else "advisory",
                drop_reason=_drop_reason(item),
            )
            for item in dropped_items
        )
        return ContextPack(
            content=content,
            included=included_manifest,
            dropped=dropped_manifest,
            budget=effective_budget,
            line_count=_count_lines(content),
            phase=normalized_phase,
            structural_index_git_sha=self._structural_index_git_sha(structural_index[0])
            if structural_index
            else None,
            prior_run_context=(
                prior_run_manifest.build_manifest(
                    prior_selection,
                    included_run_ids=_included_ids(included_items, PRIOR_RUN_KIND),
                    phase=normalized_phase,
                    agent_role=agent_role,
                    phase_iteration=phase_iteration,
                    rendered_at=rendered_at,
                )
                if prior_selection is not None
                else prior_run_manifest.disabled_manifest(
                    agent_role=agent_role,
                    phase_iteration=phase_iteration,
                    rendered_at=rendered_at,
                )
            ),
            invariant_context=(
                invariant_manifest.build_manifest(
                    invariant_selection,
                    included_ids=_included_ids(included_items, INVARIANT_KIND),
                    phase=normalized_phase,
                )
                if invariant_selection is not None
                else invariant_manifest.disabled_manifest()
            ),
        )

    def _select_prior_runs(
        self, *, phase: str, story_text: str, file_list: list[str] | None
    ) -> prior_run_selector.PriorRunSelection | None:
        """Return the prior-run selection, or ``None`` when injection is off.

        Disabled means the index is never even read, so no prior summary can
        reach a prompt through any later code path.
        """
        if not self.prior_run_context:
            return None
        return prior_run_selector.select_prior_runs(
            self.project_root,
            phase=phase,
            story_text=story_text,
            file_list=file_list,
        )

    def _select_invariants(
        self, *, phase: str, story_text: str, file_list: list[str] | None
    ) -> invariant_selector.InvariantSelection | None:
        """Return the invariant selection, or ``None`` when injection is off.

        Disabled means the derived index is never even read, so no marked prose
        can reach a prompt through any later code path. Preflight is handled
        inside the selector, which refuses it outright: preflight output drives
        coordinator control flow (ADR-0002 clause 5).
        """
        if not self.invariant_context:
            return None
        return invariant_selector.select_invariants(
            self.project_root,
            phase=phase,
            story_text=story_text,
            file_list=file_list,
        )

    def default_budget_for_phase(self, phase: str) -> int:
        attr = _PHASE_TO_BUDGET_ATTR.get(phase.lower())
        if attr is None:
            raise ValueError(f"Unknown phase {phase!r}")
        return getattr(self.budgets, attr)

    def _structural_index_git_sha(self, rel_path: Path) -> str | None:
        try:
            result = subprocess.run(
                ["git", "log", "-n", "1", "--format=%H", "--", str(rel_path)],
                cwd=self.project_root,
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        sha = result.stdout.strip()
        return sha or None

    def _read_structural_index(self) -> tuple[Path, int, str] | None:
        for candidate in _INDEX_CANDIDATES:
            path = self.project_root / candidate
            if path.exists():
                text = path.read_text(encoding="utf-8")
                return path.relative_to(self.project_root), _count_lines(text), text
        return None

    def _touched_directories(
        self,
        *,
        file_list: list[str] | None,
        story_text: str,
        structural_index: tuple[Path, int, str] | None,
    ) -> set[Path]:
        touched: set[Path] = set()
        if file_list:
            for file_path in file_list:
                rel = Path(file_path)
                for parent in [rel.parent, *rel.parents]:
                    if str(parent) == ".":
                        break
                    touched.add(parent)
            return touched

        if structural_index is None:
            return touched

        story_terms = set(_tokenize(story_text))
        for line in structural_index[2].splitlines():
            rel = _extract_index_path(line)
            if rel is None:
                continue
            line_terms = set(_tokenize(line))
            if story_terms & line_terms:
                touched.add(rel)
        return touched

    def _load_claude_docs(self, touched_dirs: set[Path]) -> list[ClaudeDocSection]:
        docs: list[ClaudeDocSection] = []
        for rel_dir in sorted(touched_dirs):
            for candidate in self._claude_doc_candidates(rel_dir):
                if candidate.exists():
                    docs.append(self._parse_claude_doc(candidate))
        deduped: dict[Path, ClaudeDocSection] = {doc.path: doc for doc in docs}
        return [deduped[path] for path in sorted(deduped)]

    def _claude_doc_candidates(self, rel_dir: Path) -> list[Path]:
        candidates: list[Path] = []
        current = rel_dir
        while str(current) != ".":
            candidates.append(self.project_root / current / "CLAUDE.md")
            candidates.append(self.project_root / current / "CONVENTIONS.md")
            current = current.parent
        return candidates

    def _parse_claude_doc(self, path: Path) -> ClaudeDocSection:
        text = path.read_text(encoding="utf-8")
        invariants = _extract_section(text, "Invariants")
        advisory_parts = []
        context = _extract_section(text, "Context")
        if context:
            advisory_parts.append(f"## Context\n\n{context}".strip())
        purpose = _extract_section(text, "Purpose")
        if purpose:
            advisory_parts.append(f"## Purpose\n\n{purpose}".strip())
        return ClaudeDocSection(
            path=path.relative_to(self.project_root),
            invariants=(f"## Invariants\n\n{invariants}".strip() if invariants else ""),
            advisory="\n\n".join(advisory_parts),
        )

    def _score_text(self, text: str, story_text: str, phase: str) -> int:
        score = 0
        story_terms = set(_tokenize(story_text))
        text_terms = set(_tokenize(text))
        score += len(story_terms & text_terms)
        if phase in text.lower():
            score += 5
        if phase == "review" and any(
            term in text.lower() for term in ("review", "correctness", "edge", "pattern")
        ):
            score += 3
        return score


def _included_ids(items: list[ContextItem], kind: str) -> set[str]:
    """Ids of ``kind`` items that survived the budget, read off the item itself.

    The manifest's "included vs dropped under budget pressure" split is only
    trustworthy if it uses the same identity the selector used. Splitting the
    display ``source`` string to recover it made that split depend on a
    project's file naming, which is exactly the kind of coupling a portable
    feature cannot carry.
    """
    return {item.item_id for item in items if item.kind == kind and item.item_id is not None}


def _drop_reason(item: ContextItem) -> str:
    """Name why an item lost its budget slot.

    Prior-run knowledge gets its own wording so an operator reading the manifest
    can tell "we knew something relevant but required context won" apart from the
    ordinary advisory truncation that has always read ``budget exceeded``.
    """
    return (
        "budget_pressure" if item.kind in (PRIOR_RUN_KIND, INVARIANT_KIND) else "budget exceeded"
    )


def _extract_section(text: str, heading: str) -> str:
    pattern = re.compile(rf"^## {re.escape(heading)}\s*$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return ""
    start = match.end()
    next_heading = re.search(r"^##\s+", text[start:], re.MULTILINE)
    end = start + next_heading.start() if next_heading else len(text)
    return text[start:end].strip()


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_]+", text.lower())


def _count_lines(text: str) -> int:
    if not text.strip():
        return 0
    return len(text.rstrip("\n").splitlines())


def _extract_index_path(line: str) -> Path | None:
    match = re.search(
        r"(?P<path>(?:\.?[A-Za-z0-9_-]+/)+[A-Za-z0-9_.-]+|(?:\.?[A-Za-z0-9_-]+/)+)", line
    )
    if not match:
        return None
    raw_path = match.group("path").rstrip(":,)")
    rel = Path(raw_path)
    if rel.is_absolute() or str(rel) in {".", ""}:
        return None
    return rel
