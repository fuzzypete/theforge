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
        if allowed_root_files:
            allowed = ", ".join(f"`{p}`" for p in allowed_root_files)
            allowed_note = f"The project additionally permits these repo-root files: {allowed}."
        else:
            allowed_note = "The project has not declared any additional permitted repo-root files."
        sections.append(
            "### No scratch files at the repo root\n\n"
            "Files at the repository root are restricted to a canonical allowlist "
            "(README, LICENSE, CHANGELOG, pyproject.toml, etc.) plus any files the "
            "project explicitly declares as allowed. "
            f"{allowed_note}\n\n"
            "Any commit in this change that introduces a new repo-root file outside "
            "this allowlist (for example: `test_*.py`, `*.lock` scratch files, "
            "ad-hoc audit YAML, rename leftovers) is a **P1 finding**. Cite the "
            "specific file path and the convention by name (`no_scratch_files`). "
            "The runtime workspace hygiene gate will also reject these — your job "
            "as reviewer is to catch them before they land."
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
