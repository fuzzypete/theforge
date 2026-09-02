from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from theforge.shape_check.issue_spec import RECOGNIZED_TYPE_LABELS

ALLOW_MUTATE_FORGE_YAML_KEY = "allow_mutate_forge_yaml"

# The story types a file-based story may declare are the issue types the
# specification recognizes — one vocabulary, not two that can drift apart.
RECOGNIZED_STORY_TYPES: frozenset[str] = RECOGNIZED_TYPE_LABELS


class StoryTypeError(ValueError):
    """Raised when a story declares an unknown ``type`` value."""


@dataclass(frozen=True)
class FrontmatterParseResult:
    """Parsed frontmatter plus any operator-facing recovery warning."""

    data: dict
    warning: str | None = None


@dataclass(frozen=True)
class BatchMember:
    """One story in a cost-aware batch group, as seen by the shared dev pass.

    Carries only what the dev prompt needs to state the story: its identity and
    its spec text. The member keeps its own :class:`TaskStory`, state row,
    review, cost, and audit everywhere else — this type exists solely so a
    single dev assignment can name every story it is expected to implement.
    """

    name: str
    slug: str
    story_text: str
    display_ref: str | None = None  # e.g. "Issue #712"; falls back to slug


@dataclass(frozen=True)
class TaskStory:
    """A single unit of work for the orchestrator to execute."""

    name: str  # human-readable, e.g. "Phase 6H: per-user export"
    slug: str  # workspace slug, e.g. "export-service"
    story_path: Path | None = None  # path to the story file (None for issue-sourced stories)
    story_text: str | None = None  # inline story content (used when story_path is None)
    test_target: str | None = None  # stack-neutral test target substituted into gate_command
    gate_override: str | None = None  # from frontmatter "gate" key; "none" skips gate
    depends_on: list[str] = field(default_factory=list)  # slugs that must have merged first
    collision_deps: list[str] = field(
        default_factory=list
    )  # slugs injected by collision detection; soft edges that release on any terminal upstream
    inferred_dependencies: list[str] = field(default_factory=list)  # inferred from GH blockers
    dependency_warnings: list[str] = field(default_factory=list)  # non-authoritative prose matches
    github_issue: int | None = None  # GH issue number; PR will include "Closes #N"
    allow_mutate_forge_yaml: bool = False  # explicit opt-in for forge.yaml guard override
    type: str | None = None  # structured story type from frontmatter or GH label
    type_warnings: list[str] = field(default_factory=list)  # missing-type migration warnings
    fix_ready: bool | None = None  # True iff bug has full Diagnosis section or non-bug type
    investigation_ready: bool = False  # True when bug passes gate but cause is not yet asserted
    readiness_warnings: list[str] = field(default_factory=list)  # why fix_ready is False/None
    # Cost-aware batch group (#727). Non-empty only on the group *leader* the
    # sprint scheduler dispatches: it lists every story the shared dev pass must
    # implement, leader first. Empty for every ordinary story.
    batch_members: tuple[BatchMember, ...] = ()
    batch_group: str | None = None  # id of the batch group, when batched


# Backward-compat alias
TaskSpec = TaskStory


def load_story(story_path: Path) -> str:
    """Read the story file content. Raises FileNotFoundError if missing."""
    return story_path.read_text(encoding="utf-8")


# Backward-compat alias
load_spec = load_story


def parse_story_frontmatter(story_path: Path) -> dict:
    return inspect_story_frontmatter(story_path).data


def _parse_frontmatter_block(text: str, *, source_name: str) -> FrontmatterParseResult:
    """Parse a leading YAML frontmatter block and preserve recovery warnings."""
    if not text.startswith("---"):
        return FrontmatterParseResult(data={})

    end = text.find("---", 3)
    if end == -1:
        return FrontmatterParseResult(
            data={},
            warning=(
                f"{source_name} starts with YAML frontmatter but has no closing '---'; "
                "ignoring the block and any dependency declarations inside it"
            ),
        )

    frontmatter = text[3:end].strip()
    try:
        result = yaml.safe_load(frontmatter) or {}
    except yaml.YAMLError:
        return FrontmatterParseResult(
            data={},
            warning=(
                f"{source_name} has malformed YAML frontmatter; "
                "ignoring the block and any dependency declarations inside it"
            ),
        )

    if not isinstance(result, dict):
        return FrontmatterParseResult(
            data={},
            warning=(
                f"{source_name} frontmatter must be a YAML mapping; "
                "ignoring the block and any dependency declarations inside it"
            ),
        )

    return FrontmatterParseResult(data=result)


def inspect_story_frontmatter(story_path: Path) -> FrontmatterParseResult:
    """Extract YAML frontmatter from a story file.

    Story files can optionally have YAML frontmatter delimited by ---::

        ---
        name: Phase 6H: per-user export
        slug: export-service
        gate: none
        ---

        # Story content starts here...

    If no frontmatter is present, returns empty dict.
    """
    text = story_path.read_text(encoding="utf-8")
    parsed = _parse_frontmatter_block(text, source_name=f"story file {story_path.name!r}")
    result = parsed.data
    if not result:
        return parsed

    # R3: gate must be a string if present; drop non-string values to prevent
    # AttributeError when _is_gate_skip() calls .lower() on a non-string.
    if "gate" in result and not isinstance(result["gate"], str):
        result = {k: v for k, v in result.items() if k != "gate"}
    if ALLOW_MUTATE_FORGE_YAML_KEY in result and not isinstance(
        result[ALLOW_MUTATE_FORGE_YAML_KEY], bool
    ):
        result = {k: v for k, v in result.items() if k != ALLOW_MUTATE_FORGE_YAML_KEY}

    if "type" in result:
        raw_type = result["type"]
        if not isinstance(raw_type, str):
            raise StoryTypeError(
                f"story 'type' must be a string, got {type(raw_type).__name__}: {raw_type!r}"
            )
        normalized = raw_type.strip().lower()
        if normalized not in RECOGNIZED_STORY_TYPES:
            raise StoryTypeError(
                f"unknown story type {raw_type!r} — must be one of: "
                f"{', '.join(sorted(RECOGNIZED_STORY_TYPES))}"
            )
        result["type"] = normalized

    return FrontmatterParseResult(data=result, warning=parsed.warning)


def frontmatter_allows_forge_yaml_mutation(frontmatter: dict) -> bool:
    """Return whether story metadata explicitly opts into forge.yaml mutations."""
    return frontmatter.get(ALLOW_MUTATE_FORGE_YAML_KEY) is True


# Backward-compat alias
parse_spec_frontmatter = parse_story_frontmatter


def extract_acceptance_criteria(story_body: str) -> list[str]:
    """Extract acceptance-criteria bullet lines from a story body.

    Looks for an ``## Acceptance criteria`` (or ``### ...``) heading and collects
    the bullet items beneath it until the next heading. Best-effort: returns an
    empty list when no such section is present, so a caller that also carries the
    full body is never left with nothing.

    Lives here because it is story parsing, and because more than one advisory
    step now needs it — the escalation advisor's evidence packet and the
    preflight decomposition assessment. Two copies would drift into disagreeing
    about what counts as a criterion, and the assessment's whole coverage check
    rests on this list matching the one an operator would count by hand.
    """
    criteria: list[str] = []
    in_section = False
    for raw in (story_body or "").splitlines():
        line = raw.strip()
        lowered = line.lower()
        if lowered.startswith("#"):
            heading = lowered.lstrip("#").strip()
            if in_section:
                # A new heading ends the AC section.
                if heading.startswith("acceptance"):
                    continue
                break
            if heading.startswith("acceptance"):
                in_section = True
            continue
        if in_section and (line.startswith("- ") or line.startswith("* ")):
            criteria.append(line[2:].strip())
    return criteria
