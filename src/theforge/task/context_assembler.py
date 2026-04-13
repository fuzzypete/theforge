from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

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


@dataclass(frozen=True)
class ClaudeDocSection:
    path: Path
    invariants: str
    advisory: str


class ContextAssembler:
    def __init__(self, project_root: Path, *, budgets: ContextBudgetConfig | None = None) -> None:
        self.project_root = project_root
        self.budgets = budgets or ContextBudgetConfig()

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
        )

    def assemble(
        self,
        *,
        phase: str,
        story_text: str,
        file_list: list[str] | None = None,
        budget: int | None = None,
    ) -> ContextPack:
        normalized_phase = phase.lower()
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
                drop_reason="budget exceeded",
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
