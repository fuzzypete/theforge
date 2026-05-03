"""Shared root-file allowlists for the no_scratch_files convention."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AppliedRootFilePreset:
    """A named root-file preset applied to a project."""

    name: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class RootFileAllowances:
    """Resolved root-file allowances grouped by source."""

    defaults: tuple[str, ...]
    presets: tuple[AppliedRootFilePreset, ...]
    project_additions: tuple[str, ...]
    effective: frozenset[str]


# Files that legitimately live in the project root across public repos.
# Hidden files (dotfiles) are always allowed separately by the check itself.
DEFAULT_ALLOWED_ROOT_FILES: tuple[str, ...] = (
    "AGENTS.md",
    "CHANGELOG.md",
    "CHANGELOG.rst",
    "CHANGELOG.txt",
    "CLAUDE.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "CONVENTIONS.md",
    "Dockerfile",
    "LICENSE",
    "LICENSE.md",
    "LICENSE.rst",
    "LICENSE.txt",
    "Makefile",
    "README.md",
    "README.rst",
    "README.txt",
    "RELEASING.md",
    "SECURITY.md",
    "dockerignore",
    "forge.yaml",
    "makefile",
)


# Stack-specific presets intentionally cover conventional root files only.
ROOT_FILE_STACK_PRESETS: dict[str, tuple[str, ...]] = {
    "go": (
        "go.mod",
        "go.sum",
        "go.work",
        "go.work.sum",
    ),
    "java": (
        "build.gradle",
        "build.gradle.kts",
        "gradle.properties",
        "gradlew",
        "gradlew.bat",
        "mvnw",
        "mvnw.cmd",
        "pom.xml",
        "settings.gradle",
        "settings.gradle.kts",
    ),
    "javascript": (
        "bun.lock",
        "bun.lockb",
        "jsconfig.json",
        "npm-shrinkwrap.json",
        "package-lock.json",
        "package.json",
        "pnpm-lock.yaml",
        "tsconfig.base.json",
        "tsconfig.json",
        "yarn.lock",
    ),
    "python": (
        "Pipfile",
        "Pipfile.lock",
        "conftest.py",
        "poetry.lock",
        "pyproject.toml",
        "pytest.ini",
        "requirements-dev.txt",
        "requirements-test.txt",
        "requirements.txt",
        "setup.cfg",
        "setup.py",
        "tox.ini",
        "uv.lock",
    ),
    "rust": (
        "Cargo.lock",
        "Cargo.toml",
        "build.rs",
        "clippy.toml",
        "rust-toolchain.toml",
        "rustfmt.toml",
    ),
}

ROOT_FILE_STACK_ALIASES: dict[str, str] = {
    "go": "go",
    "golang": "go",
    "java": "java",
    "javascript": "javascript",
    "js": "javascript",
    "node": "javascript",
    "nodejs": "javascript",
    "python": "python",
    "py": "python",
    "rust": "rust",
}


def normalize_root_file_stacks(stacks: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    """Normalize configured stack names into canonical preset ids."""
    if not stacks:
        return ()
    normalized: list[str] = []
    seen: set[str] = set()
    for stack in stacks:
        canonical = ROOT_FILE_STACK_ALIASES.get(stack.strip().lower())
        if canonical is None:
            valid = ", ".join(sorted(ROOT_FILE_STACK_PRESETS))
            raise ValueError(
                "forge.yaml 'conventions.hard.stack' contains unknown preset "
                f"{stack!r}; expected one of: {valid}"
            )
        if canonical not in seen:
            normalized.append(canonical)
            seen.add(canonical)
    return tuple(normalized)


def resolve_root_file_allowances(
    *,
    stack: tuple[str, ...] | list[str] | None = None,
    allowed_root_files: tuple[str, ...] | list[str] | None = None,
) -> RootFileAllowances:
    """Resolve the effective allowlist for repo-root files."""
    defaults = tuple(DEFAULT_ALLOWED_ROOT_FILES)
    stack_names = normalize_root_file_stacks(stack)
    presets = tuple(
        AppliedRootFilePreset(
            name=name,
            files=ROOT_FILE_STACK_PRESETS[name],
        )
        for name in stack_names
    )

    preset_allowed: set[str] = set()
    for preset in presets:
        preset_allowed.update(preset.files)

    project_items = tuple(allowed_root_files or ())
    project_additions = tuple(
        item
        for item in project_items
        if item not in DEFAULT_ALLOWED_ROOT_FILES and item not in preset_allowed
    )

    effective = frozenset(defaults) | preset_allowed | frozenset(project_items)
    return RootFileAllowances(
        defaults=defaults,
        presets=presets,
        project_additions=project_additions,
        effective=effective,
    )
