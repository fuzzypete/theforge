from theforge.root_file_conventions import resolve_root_file_allowances


def render_conventions_block(conventions: list[str] | None) -> str:
    """Render the soft conventions prompt block.

    Returns an empty string when conventions is None or empty.
    """
    if not conventions:
        return ""
    items = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(conventions))
    return (
        "\n## Project Conventions\n\n"
        "The following conventions apply to this project. Respect them in your work"
        " and flag violations you observe.\n\n"
        f"{items}\n"
    )


def render_hard_conventions_block(
    *,
    stack: tuple[str, ...] | list[str] | None = None,
    allowed_root_files: tuple[str, ...] | list[str] | None = None,
    no_scratch_files: bool | None = None,
) -> str:
    """Render the hard (mechanically enforced) conventions block for reviewers.

    Hard conventions are checked at runtime by the workspace hygiene gate.
    Reviewers receive them so they can reject diffs that introduce violations
    upfront, rather than letting the runtime gate be the only backstop.

    Returns an empty string when no hard conventions are supplied.
    """
    sections: list[str] = []
    if no_scratch_files:
        allowances = resolve_root_file_allowances(
            stack=stack,
            allowed_root_files=allowed_root_files,
        )
        defaults = ", ".join(f"`{item}`" for item in allowances.defaults)
        if allowances.presets:
            preset_lines = "\n".join(
                f"  - `{preset.name}`: {', '.join(f'`{item}`' for item in preset.files)}"
                for preset in allowances.presets
            )
        else:
            preset_lines = "  - none"
        if allowances.project_additions:
            additions = ", ".join(f"`{item}`" for item in allowances.project_additions)
        else:
            additions = "`none`"
        sections.append(
            "### No scratch files at the repo root\n\n"
            "Allowed root files (current effective set):\n"
            f"Defaults (community standards): {defaults}\n"
            f"Stack presets:\n{preset_lines}\n"
            f"Project additions: {additions}\n\n"
            "Any commit in this change that introduces a new repo-root file outside "
            "this effective set (not allowed by the defaults, an applied preset, "
            "or a project-specific addition) is a **P1 finding**. Cite the "
            "specific file path and the convention by name (`no_scratch_files`). "
            "The runtime workspace hygiene gate will also reject these, so use the "
            "breakdown above to audit whether a repo-root file is actually legitimate."
        )
    if not sections:
        return ""
    body = "\n\n".join(sections)
    return (
        "\n## Project Conventions (Hard — Mechanically Enforced)\n\n"
        "These conventions are declared by the project and enforced at runtime. "
        "Violations introduced by these commits MUST be reported as P1 findings — "
        "they will fail the hygiene gate and block the change regardless of your "
        "verdict, so flagging them upfront keeps the pipeline moving.\n\n"
        f"{body}\n"
    )
