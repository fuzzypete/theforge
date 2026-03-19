---
name: "API agent loop — tool-use runtime for API-mode agents"
slug: api-agent-loop
pytest_target: tests/
---

# API Agent Loop

## Problem

API-mode agents in TheForge are currently stateless text-judgment calls: prompt in,
structured text out, no tool access. This makes them unsuitable for any task that
requires inspecting code — which is most tasks. CLI-mode agents (claude, codex, gemini)
have tool access because their CLI runtimes provide it. API-mode agents have none
because TheForge delegates tool execution entirely to the CLI runtime.

All three major providers (OpenAI, Anthropic, Google) support a tool-use protocol
where the model emits tool call requests and the caller executes them locally. TheForge
does not implement this protocol. The result: API agents can only review what's injected
into the prompt, breaking the commit-centric review philosophy where reviewers discover
code from commits using tools.

## Goal

TheForge becomes the tool runtime for API-mode agents. The model requests tool calls
via the provider's tool-use protocol; TheForge executes them locally in the worktree
and feeds results back. The agent loop continues until the model emits a final
structured response. `allowed_tools` on `ModelProfile` becomes the actual set of tools
exposed to the API model — not a hint, but the contract.

## Architecture

```
┌──────────────────────────────────────────────────┐
│  TheForge Coordinator                            │
│                                                  │
│   ┌─────────────┐       ┌─────────────────────┐  │
│   │ runner_api   │──────▶│  Agent Loop          │  │
│   │  (dispatch)  │       │                     │  │
│   └─────────────┘       │  1. Send prompt +    │  │
│                          │     tool schemas     │  │
│                          │  2. Model responds   │  │
│                          │     with tool_call   │  │
│                          │  3. Execute locally   │  │
│                          │  4. Send result back  │  │
│                          │  5. Repeat until      │  │
│                          │     final text        │  │
│                          └──────────┬──────────┘  │
│                                     │             │
│                          ┌──────────▼──────────┐  │
│                          │  Tool Runtime        │  │
│                          │                     │  │
│                          │  read_file(path)    │  │
│                          │  write_file(path,c) │  │
│                          │  edit_file(path,o,n)│  │
│                          │  bash(cmd)          │  │
│                          │  grep(pattern,path) │  │
│                          │  glob(pattern)      │  │
│                          └─────────────────────┘  │
│                                                  │
└──────────────────────────────────────────────────┘
```

## Acceptance Criteria

### AC-1: Tool schema registry

A module `tool_runtime.py` defines all available tools as a registry. This is the
single canonical source for tool names, schemas, and provider-specific translations.

```python
@dataclass
class ToolDef:
    name: str                      # "read_file", "bash", etc.
    description: str               # shown to the model
    parameters: dict               # JSON Schema for arguments
    handler: Callable[..., str]    # executes the tool, returns text result

    def to_openai_function(self) -> dict: ...
    def to_anthropic_tool(self) -> dict: ...
    def to_google_declaration(self) -> dict: ...

TOOL_REGISTRY: dict[str, ToolDef] = { ... }
```

The name-mapping from forge.yaml's capitalized convention to registry names is also
centralized here:

```python
TOOL_NAME_MAP = {
    "Read": "read_file",
    "Bash": "bash",
    "Grep": "grep",
    "Glob": "glob",
    "Write": "write_file",
    "Edit": "edit_file",
}
```

Tools implemented in phase 1:
- `read_file(path: str) -> str` — read file contents, scoped to working_dir
- `bash(command: str) -> str` — run shell command in working_dir, timeout 30s,
  capture stdout+stderr, return exit code + output
- `grep(pattern: str, path: str | None, glob: str | None) -> str` — regex search,
  returns matching lines with file:line prefix
- `glob(pattern: str) -> str` — file pattern match, returns newline-separated paths

Phase 2 tools (dev agents):
- `write_file(path: str, content: str) -> str` — write file, scoped to working_dir
- `edit_file(path: str, old_string: str, new_string: str) -> str` — exact string
  replacement, scoped to working_dir

### AC-2: Tool filtering from allowed_tools

`allowed_tools` on `ModelProfile` controls which tools from the registry are
exposed to the model. The agent loop filters the registry:

```python
tool_schemas = [
    TOOL_REGISTRY[TOOL_NAME_MAP[name]].to_openai_function()  # or .to_anthropic_tool(), etc.
    for name in profile.allowed_tools
    if name in TOOL_NAME_MAP
]
```

If `allowed_tools` is empty, no tools are provided (current stateless behavior).
This preserves backward compatibility — existing API profiles with `allowed_tools: ()`
continue to work as text-judgment-only.

### AC-3: Agent loop per provider

Each provider adapter (`_run_openai`, `_run_anthropic`, `_run_google`) gains an
agent loop that:

1. Sends the initial prompt + tool schemas to the model
2. Inspects the response for tool call requests
3. Executes each tool call via the registry handler
4. Sends tool results back to the model
5. Repeats until the model emits a final text/structured response
6. Accumulates token usage across all loop iterations for cost tracking

The loop has two safety limits:
- **Max iterations**: configurable, default 25. Prevents runaway tool loops.
- **Timeout**: uses `profile.timeout_seconds` as a **wall-clock deadline** for the
  entire loop (all iterations combined). Checked before each iteration and before
  each tool execution. Individual tool timeouts (e.g. bash 30s) count against the
  global budget — if a bash call takes 25s of a 120s total, 95s remain for the rest
  of the loop. If the global timeout is reached mid-tool, the tool is terminated and
  the loop returns a failure result with accumulated cost.

Provider-specific tool call protocol:

**OpenAI (Chat Completions)**:
```python
response = client.chat.completions.create(
    model=..., messages=messages, tools=tool_schemas, ...
)
# If response.choices[0].message.tool_calls is not None:
#   execute each, append tool results to messages, loop
```

**OpenAI (Responses API — Codex models)**:
```python
response = client.responses.create(
    model=..., input=..., tools=tool_schemas, ...
)
# If response has function_call output items:
#   execute each, append to input, loop
```

**Anthropic**:
```python
response = client.messages.create(
    model=..., messages=messages, tools=tool_schemas, ...
)
# If response.stop_reason == "tool_use":
#   execute each tool_use block, append tool_result, loop
```

**Google**:
```python
response = client.models.generate_content(
    model=..., contents=contents, tools=tool_declarations, ...
)
# If response.candidates[0].content.parts has function_call:
#   execute each, append function_response, loop
```

### AC-4: Working directory scoping and tool output limits

All tool handlers receive `working_dir: Path` and scope filesystem operations to it:
- `read_file`: resolves path relative to working_dir, rejects `..` traversal
- `bash`: sets `cwd=working_dir` on subprocess
- `grep`/`glob`: searches within working_dir only
- `write_file`/`edit_file`: writes within working_dir only

This provides the same sandboxing that CLI agents get from running inside the worktree.

**Output truncation**: tool results are capped at 50KB. If a tool output exceeds this
limit, it is truncated and a notice is appended:

```
[output truncated — {original_size} bytes, showing first 50KB]
```

This prevents a single large file read or verbose test output from exhausting the
model's context window. The 50KB default is configurable via `max_tool_output_bytes`
on `ModelProfile` (optional, defaults to 51200).

**Security**: API agents inherit the same security profile as CLI agents regarding
shell execution. The `bash` tool runs commands as the forge user with the worktree
as cwd. No command filtering is applied — the agent is trusted within the worktree,
consistent with CLI-mode behavior.

### AC-5: Cost accumulation across iterations

Each loop iteration's token usage is accumulated into a single `ModelUsage` record:

```python
total_input_tokens += iteration_usage.input_tokens
total_output_tokens += iteration_usage.output_tokens
total_cache_read_tokens += iteration_usage.cache_read_tokens
total_cache_creation_tokens += iteration_usage.cache_creation_tokens
# cost estimated from totals at the end
```

All four token fields are accumulated (input, output, cache_read, cache_creation)
to maintain consistency with the `ModelUsage` definition and ensure accurate cost
reporting for providers that use prompt caching (Anthropic, Google).

The `AgentResult.cost_usd` reflects the entire loop, not just the last call.

### AC-6: Structured output on final iteration

The final iteration (when the model stops calling tools) must return the structured
review YAML/JSON. For models that support it, the final call includes the
`response_format` / `tool_choice` constraint to force structured output. For models
that don't support combining tools + structured output in the same call, the loop
does a final no-tools call with just `response_format` after the model signals
completion.

### AC-7: Logging

Each tool call is logged at verbose level with duration, matching CLI agent log format:

```
[forge]   ↳ read_file: src/theforge/runner.py (2ms)
[forge]   ↳ bash: python -m pytest tests/ -q (4.2s)
[forge]   ↳ grep: cost_usd in src/theforge/ (12ms)
```

Tool output truncation is logged at verbose level:

```
[forge]   ↳ read_file: data/large.json (truncated 2.1MB → 50KB)
```

Total iterations and tool calls logged at normal level on completion:

```
[forge]   ... codex-reviewer done (45s, 12 tool calls, 8 iterations)
```

### AC-8: Tests

- `test_tool_runtime.py`: each tool handler tested in isolation with a temp dir
  - read_file returns contents, rejects path traversal
  - read_file truncates output beyond max_tool_output_bytes
  - bash runs commands, returns output, respects timeout
  - bash returns exit code + stderr on failure
  - grep matches patterns, returns formatted results
  - glob finds files by pattern
  - provider-specific schema translation (to_openai_function, to_anthropic_tool,
    to_google_declaration) returns correct format for each provider
- `test_runner_api.py`: agent loop tested with mocked provider SDKs
  - mock model returns tool_call → verify handler called → mock final response
  - verify token accumulation across iterations (all four fields)
  - verify max iteration limit terminates loop
  - verify timeout terminates loop (including mid-tool timeout)
  - verify empty allowed_tools skips tool registration (stateless mode)

## Implementation Notes

### File structure

```
src/theforge/
  tool_runtime.py     # NEW: ToolDef, TOOL_REGISTRY, TOOL_NAME_MAP, handlers,
                      #       provider-specific schema methods
  runner_api.py       # MODIFIED: agent loop in each provider adapter
```

### Provider-specific schema translation

Schema translation lives on `ToolDef` itself, not in provider adapters. Each adapter
calls the appropriate method:

```python
# In _run_openai:
tools = [registry[t].to_openai_function() for t in resolved_tools]

# In _run_anthropic:
tools = [registry[t].to_anthropic_tool() for t in resolved_tools]

# In _run_google:
tools = [{"function_declarations": [registry[t].to_google_declaration() for t in resolved_tools]}]
```

This keeps translation centralized in `tool_runtime.py` and prevents adapters from
diverging on schema format.

### Backward compatibility

- Profiles with `allowed_tools: ()` continue to work as stateless text-judgment
  (no tools registered, no loop, single call — current behavior)
- CLI-mode profiles are unaffected (they don't go through runner_api.py)
- The `mode` property on `ModelProfile` remains unchanged

### Security

- `bash` tool: no command filtering (same as CLI agents). The agent is trusted
  within the worktree. Commands run as the forge user with the worktree as cwd.
  API agents inherit the same security profile and risks as CLI agents.
- Path traversal: `read_file`, `write_file`, `edit_file` reject paths that resolve
  outside `working_dir` (via `Path.resolve()` prefix check).
- No network tools. The model cannot make HTTP requests except through `bash`
  (same as CLI agents).

## Out of Scope

- MCP tool protocol (future: expose MCP servers as tools to API agents)
- Streaming responses (batch mode is sufficient for review tasks)
- Image/multimodal tool results
- Agent-to-agent communication
- Persistent agent state across calls (no session resume for API mode — each
  run is self-contained)
