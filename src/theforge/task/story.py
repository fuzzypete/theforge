from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

ALLOW_MUTATE_FORGE_YAML_KEY = "allow_mutate_forge_yaml"


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
    inferred_dependencies: list[str] = field(default_factory=list)  # inferred from GH blockers
    dependency_warnings: list[str] = field(default_factory=list)  # non-authoritative prose matches
    github_issue: int | None = None  # GH issue number; PR will include "Closes #N"
    allow_mutate_forge_yaml: bool = False  # explicit opt-in for forge.yaml guard override


# Backward-compat alias
TaskSpec = TaskStory


def load_story(story_path: Path) -> str:
    """Read the story file content. Raises FileNotFoundError if missing."""
    return story_path.read_text(encoding="utf-8")


# Backward-compat alias
load_spec = load_story


def parse_story_frontmatter(story_path: Path) -> dict:
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
    if not text.startswith("---"):
        return {}

    end = text.find("---", 3)
    if end == -1:
        return {}

    frontmatter = text[3:end].strip()
    try:
        result = yaml.safe_load(frontmatter) or {}
    except yaml.YAMLError:
        return {}

    if not isinstance(result, dict):
        return {}

    # R3: gate must be a string if present; drop non-string values to prevent
    # AttributeError when _is_gate_skip() calls .lower() on a non-string.
    if "gate" in result and not isinstance(result["gate"], str):
        result = {k: v for k, v in result.items() if k != "gate"}
    if ALLOW_MUTATE_FORGE_YAML_KEY in result and not isinstance(
        result[ALLOW_MUTATE_FORGE_YAML_KEY], bool
    ):
        result = {k: v for k, v in result.items() if k != ALLOW_MUTATE_FORGE_YAML_KEY}

    return result


def frontmatter_allows_forge_yaml_mutation(frontmatter: dict) -> bool:
    """Return whether story metadata explicitly opts into forge.yaml mutations."""
    return frontmatter.get(ALLOW_MUTATE_FORGE_YAML_KEY) is True


# Backward-compat alias
parse_spec_frontmatter = parse_story_frontmatter
