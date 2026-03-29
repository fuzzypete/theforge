---
name: "Forge-managed tools for deterministic agent behavior"
slug: forge-tools-for-agents
pytest_target: tests/
---

# Forge-Managed Tools for Agents

## Problem

Agents freestyle shell commands when they want to verify tests, check lint, or
inspect project structure. This leads to non-deterministic behavior: deepseek ran
`npx vitest run` from the repo root instead of `apps/gym-ui/`, producing 435
phantom test failures. The correct command was already defined as `gate_command`
in the project config, but the reviewer had no structured way to invoke it.

The tool registry already provides `read_file`, `bash`, `grep`, `glob`,
`write_file`, and `edit_file` to API-mode agents. But there are no forge-specific
tools that expose project configuration (gate command, setup command, lint) as
structured operations. Agents must discover these commands on their own, and often
get them wrong.

For CLI-mode agents (claude-code, codex), there's no mechanism at all to surface
forge-managed commands.

## Solution

Extend the tool registry with forge-specific tools that wrap project configuration.
These tools use the same `gate_command`, `setup_command`, and lint configuration
the coordinator uses, ensuring agents run exactly the same commands.

For CLI-mode agents, inject the equivalent commands as environment variables and
prompt instructions so the agent knows to use them instead of guessing.

## Acceptance criteria

- A `run_gate` tool is available to API-mode agents that executes the project's gate command
- The tool uses the same command resolution as the coordinator (config + task overrides + template vars)
- Tool output includes exit code and stdout/stderr (truncated to tool output limits)
- CLI-mode agent prompts include the resolved gate command with instructions to use it for test verification
- CLI-mode agents receive `FORGE_GATE_CMD` environment variable with the resolved command
- Reviewers' system prompts instruct them to use `run_gate` instead of running test commands directly
- The tool registry extension point is generic enough to add future forge tools (run_lint, read_spec, etc.)
- API-mode agents that lack tool support (text-only judgment calls) are unaffected
