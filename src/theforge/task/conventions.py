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
