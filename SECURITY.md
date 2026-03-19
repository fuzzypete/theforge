# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |

## Security Model

TheForge executes AI agent output as code in sandboxed git worktrees. Key
security boundaries:

- **Worktree isolation:** All agent work happens in disposable git worktrees
  under `.forge/worktrees/`. Agents never modify the main working tree directly.
- **API keys:** Stored in `.forge/.env` (gitignored) or environment variables.
  Never logged or included in audit output.
- **Subprocess execution:** Agent CLIs run as subprocesses with the same
  permissions as the invoking user. TheForge does not escalate privileges.
- **Tool runtime:** API-mode agents execute tools (Read, Edit, Write, Bash,
  Glob, Grep) within the worktree. The Bash tool runs commands with the
  invoking user's permissions.

## What to report

- API key leakage in logs, audit output, or agent prompts
- Path traversal outside the worktree boundary
- Injection attacks via spec files, review output, or config
- Unintended credential exposure in any output format

## How to report

**Do not open a public issue for security vulnerabilities.**

Email: security@theforge.dev (or open a private security advisory on GitHub)

Include:
- Description of the vulnerability
- Steps to reproduce
- Impact assessment
- Suggested fix (if any)

We will acknowledge receipt within 48 hours and provide a timeline for a fix.

## Scope

TheForge trusts the AI agents it invokes — it does not sandbox their output
beyond worktree isolation. If you configure an agent with Bash tool access,
that agent can execute arbitrary commands as your user. This is by design:
the security boundary is the worktree, not the agent.
