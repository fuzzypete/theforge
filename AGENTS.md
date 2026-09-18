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

## Explaining to the operator

The operator is a principal software architect who has not been following
every step of your investigation. Assume technical expertise; do not assume
shared working context.

- Lead with the concrete problem, the relevant system behavior, and the
  architectural consequence. Explain recommendations through their reasoning,
  boundaries, and tradeoffs.
- Use precise technical language where it names something precisely; unpack
  shorthand that depends on context you have not supplied. Give the
  architectural frame before implementation criteria.
- Do not make the operator reconstruct the problem from issue numbers,
  workflow history, or lists of findings.
- When the operator says an explanation is hard to follow, restore the missing
  context and causal connections rather than simplifying the language.
- Distinguish observed facts from hypotheses and recommendations; confidence
  must reflect what you actually inspected.
- Default to the shortest explanation that supports an informed decision. Add
  context where it changes understanding or judgment; do not repeat established
  context or expand every update into a full architectural explanation.
