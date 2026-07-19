# What's in `.forge/`?

TheForge writes everything it generates under a single `.forge/` directory.
This guide explains what ends up there in terms of **four categories** —
not a file-by-file inventory, because the exact set of files changes as
TheForge grows. Understanding the categories tells you what's safe to
delete, what should never be committed, and what travels with your repo.

## The four categories

| Category | What lives there | Travels with the repo? | Committing the wrong thing would... |
|----------|-------------------|------------------------|--------------------------------------|
| **Secrets** | API keys, tokens (`.forge/.env`, `secrets.yaml`) | Never | Leak credentials |
| **Machine-local runtime state** | Active worktrees, locks, in-flight run bookkeeping, phase-routing state | Never | Break runs on another machine or clone: stale PIDs, nested git repos, false-positive locks |
| **Derived views** | Logs, rebuildable indexes, rollups computed from other tracked data | Never (regenerable) | Create merge-conflict churn for data that adds no information over its source |
| **Project memory + config** | Per-run audit records, knowledge summaries, hook scripts, `.env.example` | **Default: yes** (opt out with `forge init --local-memory`) | Nothing — this is the working memory of the project, meant to be shared |

The category that trips people up is the third one: some derived views
contain data that *is* tracked elsewhere (a rebuilt index over per-run
audit files, for example). The rule is still "never track it" — if it's
computed from something else that's already tracked, tracking the
derived copy too just creates something that can drift and conflict.

## The canonical template is the source of truth

Rather than hand-copying the exact `.gitignore`/`.gitattributes` rules
here — which would drift the moment TheForge adds a new subdirectory
under `.forge/` — the authoritative version lives in the code that
writes it:

- `forge init` writes the `.gitignore` and `.gitattributes` blocks from
  the template builders in `src/theforge/cli/init_commands.py`
  (`_gitignore_block`, `_gitattributes_block`).
- The blocks are marked with `# ── TheForge ──` start/end lines, so
  `forge init` can find and update its own block idempotently without
  touching anything else in your `.gitignore`.
- The `.gitignore` shape is **default-deny with explicit re-includes**:
  everything under `.forge/` is ignored by default, and only the
  project-memory and config paths are explicitly re-included. This means
  a future TheForge version that adds a new runtime directory defaults
  to ignored — you don't need to update anything to stay safe.

If you want to know exactly which paths are tracked today, read the
template TheForge just wrote into your `.gitignore` — it's the same
block for every project, and it's self-documenting.

For the design rationale behind this shape, see
[`docs/plans/forge-storage-layout.md`](../plans/forge-storage-layout.md).

## Shared memory vs. local memory

By default, `forge init` writes a template that tracks project memory
(category four above) — per-run audit records and knowledge summaries
travel with the repo like any other project artifact. This is the right
default for most projects: that data is the project's working memory,
not a private log.

Pick `--local-memory` at init time instead when:

- Your repo is public, and you don't want exploratory prompts, partial
  plans, or cost data showing up in the public diff.
- Your organization has a stricter data-boundary policy than "same
  trust boundary as source code."

```bash
forge init --local-memory
```

This is an **init-time choice, not a runtime flag**. TheForge
deliberately doesn't offer a config setting to flip memory sharing
mid-project — that invites "it worked on my machine" bugs where
contributors on the same branch disagree about whether a record exists
locally. If you need to change the choice later, edit the `.gitignore`
block by hand (delete or add back the project-memory re-include lines).
Records already committed stay committed; this only affects what gets
tracked going forward.

## Mental model

> TheForge is a **coordinator**, not an autonomous IDE.

- **Each phase has a narrow job** — workspace setup, preflight check,
  dev, validate, review. No phase does more than one thing.
- **Models produce artifacts, not runtime authority** — an agent writes
  code or a review verdict; the coordinator, plain Python, decides what
  happens next.
- **Validation and review are gates, not suggestions** — a FAIL gate
  stops the run; a P1 finding triggers a retry. This is mechanical, not
  advisory.
- **Your repo is always safe** — all agent work happens in a worktree on
  a feature branch. Main is untouched until you explicitly merge.

`.forge/` is where that coordination leaves its evidence: what ran, what
it produced, and what's safe to throw away. The category model above is
what keeps that evidence from becoming clutter or a liability.
