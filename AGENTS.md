# Agent Guidance for TheForge

This is the **canonical, AI-agnostic entry point** for any AI agent working in
this repository — Claude Code, Codex, Gemini CLI, or otherwise. `CLAUDE.md` and
`GEMINI.md` are thin redirects to this file; keep shared guidance here so every
agent reads the same source.

## Start here

- **`CONVENTIONS.md`** — all project conventions, architecture, testing rules,
  and workflow. Read it before writing code.
- Directory-level `CONVENTIONS.md` files under `src/theforge/` provide
  subsystem-local guidance. When working inside `coordinator/`, `runners/`,
  `sprint/`, `task/`, `config/`, or `cli/`, read the nearest local
  `CONVENTIONS.md` in addition to the root conventions file.
- **`docs/guides/controller-runbook.md`** — read this **first** when you are
  *operating* TheForge rather than developing it: running or diagnosing sprints,
  cutting release candidates, or filing issues from sprint failures. It holds
  the commands, the flags that matter, and the traps (e.g. the `base_branch`
  fast-forward that can corrupt a checked-out release branch).

## Notes for all agents

- No AI-specific guidance currently differs from this shared source.
- Do not modify `AGENTS.md`, `CLAUDE.md`, or `GEMINI.md` unless the task
  explicitly requires it.
- The `# ── TheForge ──` marker block in this repo's `.gitignore`/`.gitattributes`
  is the canonical template emitted by `forge init` — the single source of truth
  is `src/theforge/cli/init_commands.py` (`_gitignore_block` /
  `_gitattributes_block`). Do not hand-edit the marker block; change the template
  builders and re-sync. See `CONTRIBUTING.md` → "Git Policy" and
  `docs/plans/forge-storage-layout.md`.
