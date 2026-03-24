"""forge init / secrets-init / version subcommands."""

from __future__ import annotations

import argparse
import importlib.metadata
import subprocess
import sys
from pathlib import Path

from theforge.config import PROVIDER_API_KEY_MAP, generate_default_config

_GITIGNORE_ENTRY = ".forge/.env"

_STORY_TEMPLATE = """\
---
# Story frontmatter — required fields
name: "Short human-readable title"
slug: my-feature-slug        # used for branch and worktree names
pytest_target: tests/        # path passed to pytest for gate
---

# Story title

## Problem / context

One paragraph explaining WHY this change is needed and WHAT problem it solves.
Background and motivation belong here. This section is informational — it is
NOT a list of requirements.

## Acceptance criteria

<!--
  ACs are the definitive checklist. The dev agent will implement exactly what
  is stated here. Write them as observable, testable behaviors.
  Each AC should be a single bullet starting with a verb.
-->

- The system does X when Y
- Existing tests continue to pass
- New test verifies Z

## Notes (optional)

Any additional context, constraints, or design guidance. The dev agent will
read this as context, not as requirements.
"""


def _ensure_gitignored(project_root: Path) -> None:
    """Append .forge/.env to .gitignore if not already present."""
    gitignore = project_root / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8")
        if _GITIGNORE_ENTRY in content.splitlines():
            return
        separator = "" if content.endswith("\n") else "\n"
        gitignore.write_text(content + separator + _GITIGNORE_ENTRY + "\n", encoding="utf-8")
    else:
        gitignore.write_text(_GITIGNORE_ENTRY + "\n", encoding="utf-8")


def _generate_secrets_skeleton() -> str:
    """Generate .env skeleton from PROVIDER_API_KEY_MAP."""
    lines = [
        "# .forge/.env — project-scoped secrets for TheForge",
        "# Copy this file to .forge/.env and fill in the values you need.",
        "# This file (.env.example) is tracked; .env is gitignored.",
        "",
    ]
    for _provider, key in PROVIDER_API_KEY_MAP.items():
        lines.append(f"# {key}=")
    lines.append("# NTFY_URL=https://ntfy.sh/your-topic-here")
    lines.append("")
    return "\n".join(lines)


def cmd_secrets_init(args: "argparse.Namespace") -> int:
    """Create .forge/.env skeleton and update .gitignore."""

    project_root = Path.cwd()
    env_path = project_root / ".forge" / ".env"
    secrets_yaml_path = project_root / ".forge" / "secrets.yaml"

    if secrets_yaml_path.exists() and not env_path.exists():
        print(
            "⚠ .forge/secrets.yaml detected — migrate to .forge/.env (see .forge/.env.example)",
            file=sys.stderr,
        )

    if env_path.exists():
        print(
            f"Warning: {env_path} already exists. Not overwriting.",
            file=sys.stderr,
        )
        return 0

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(_generate_secrets_skeleton(), encoding="utf-8")
    print(f"Created {env_path}")

    _ensure_gitignored(project_root)
    print(f"Updated .gitignore to exclude {_GITIGNORE_ENTRY}")
    return 0


def cmd_init(args: "argparse.Namespace") -> int:
    """Generate a starter forge.yaml in the current directory."""

    target = Path.cwd() / "forge.yaml"
    if target.exists():
        print(f"forge.yaml already exists: {target}", file=sys.stderr)
        return 1

    target.write_text(generate_default_config(), encoding="utf-8")
    print(f"Created {target}")

    stories_dir = Path.cwd() / "stories"
    stories_dir.mkdir(exist_ok=True)
    template_path = stories_dir / "TEMPLATE.md"
    if not template_path.exists():
        template_path.write_text(_STORY_TEMPLATE, encoding="utf-8")
        print(f"Created {template_path}")

    print("Edit forge.yaml to match your project, then run: forge run <story-file>")

    _ensure_gitignored(Path.cwd())
    return 0


def cmd_version(args: "argparse.Namespace") -> int:
    """Print the installed version of TheForge."""

    try:
        version = importlib.metadata.version("theforge")
    except importlib.metadata.PackageNotFoundError:
        version = "(not installed)"

    print(f"TheForge version: {version}")

    # Check for editable install
    try:
        dist = importlib.metadata.distribution("theforge")
        # Check for 'direct_url.json' which indicates an editable install
        if dist.read_text("direct_url.json"):
            # Try to get git info
            try:
                # Get branch name
                branch = subprocess.check_output(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
                # Get commit hash
                commit = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
                # Get tag distance
                tag_distance = subprocess.check_output(
                    ["git", "describe", "--tags", "--long"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
                print(f"  Branch: {branch}")
                print(f"  Commit: {commit}")
                print(f"  Tag distance: {tag_distance}")
            except (subprocess.CalledProcessError, FileNotFoundError):
                print("  (Git information not available)")
    except (FileNotFoundError, importlib.metadata.PackageNotFoundError):
        pass  # Not an editable install or direct_url.json not found

    return 0
