# Claude Guidance for TheForge

This file is reserved for Claude-specific guidance in this repository.

See `CONVENTIONS.md` for all project conventions, architecture, testing rules, and workflow.

Directory-level `CONVENTIONS.md` files under `src/theforge/` provide subsystem-local guidance. When working inside `coordinator/`, `runners/`, `sprint/`, `task/`, `config/`, or `cli/`, read the nearest local `CONVENTIONS.md` in addition to the root conventions file.

## Claude-specific notes

- No Claude-specific guidance currently differs from the shared project conventions.
- Do not modify `CLAUDE.md` or `AGENTS.md` unless the story explicitly requires it.
- The `# ── TheForge ──` marker block in this repo's `.gitignore`/`.gitattributes`
  is the canonical template emitted by `forge init` — the single source of truth
  is `src/theforge/cli/init_commands.py` (`_gitignore_block` /
  `_gitattributes_block`). Do not hand-edit the marker block; change the template
  builders and re-sync. See `CONTRIBUTING.md` → "Git Policy" and
  `docs/plans/forge-storage-layout.md`.
