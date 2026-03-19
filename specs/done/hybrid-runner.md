---
name: "Hybrid runner — API transport for text-judgment agents"
slug: hybrid-runner
pytest_target: tests/
---

# Hybrid Runner

## Problem

TheForge currently runs every agent as a CLI subprocess (`claude -p`, `codex exec`,
`gemini -p`). This works for **dev agents** that need tools (file editing, bash, git)
but is wrong for **text-judgment agents** — reviewers, plan reviewers, synthesis — that
read a prompt and emit structured text. No tool use, no filesystem mutation.

Forcing text-judgment work through CLIs causes real operational problems:

1. **Rate limits are shared with interactive use.** CLI message quotas (e.g. Codex
   daily limits) are consumed by review work that could run through a separate API
   quota. Running out of CLI messages blocks both interactive development and forge
   review runs.

2. **Session ID extraction is unsafe for concurrent invocations.** Codex session IDs
   are recovered by scanning a global `~/.codex/session_index.jsonl` file after the
   run — racy when two reviewers run in parallel. Gemini returns `"latest"` which is
   not invocation-scoped. The `is_pool` flag exists specifically to disable these hacks
   in concurrent mode, which means **pool reviewers currently cannot resume sessions.**

3. **Cost accounting is broken.** Codex and Gemini CLI runners hardcode
   `cost_usd=0.0` because the CLIs don't reliably emit normalized usage data. The
   audit trail is blind to actual review spend.

4. **`runner.py` is doing too much.** It is both a generic orchestration abstraction
   and a pile of CLI-specific lifecycle hacks (heartbeat threads, temp file cleanup,
   output file parsing, stream event processing). Text-judgment calls are HTTP
   request/response pairs — none of that machinery is needed.

## Design

### Transport: `cli` vs `api`

`ModelProfile` gains a `provider` field. Transport mode is **derived, not stored**:
- If `provider` is set → mode is `api` (HTTP API call, stateless)
- If `cli` is set → mode is `cli` (subprocess, supports tools and session resume)
- Exactly one must be set. Both set or neither set is a config error.

There is no `mode` field on the dataclass. It is a derived property only.

### Provider mapping

| `provider` | API | Auth env var | SDK |
|---|---|---|---|
| `anthropic` | Messages API | `ANTHROPIC_API_KEY` | `anthropic` PyPI |
| `openai` | Responses API | `OPENAI_API_KEY` | `openai` PyPI |
| `google` | Gemini API | `GOOGLE_API_KEY` | `google-genai` PyPI |

SDKs are optional dependencies — only imported when a profile uses that provider.

### Eager provider validation

During config normalization, **eagerly validate provider readiness** for every
configured API profile:

1. `provider` value is in `SUPPORTED_PROVIDERS`
2. The provider's SDK is importable
3. The required API key environment variable is set and non-empty

All three checks run at config load time. A missing SDK or missing API key is a
`ValueError` with a clear message naming the profile, the missing dependency, and
how to fix it. The coordinator never reaches runtime with an un-runnable profile.

### Structured output

API providers support schema-constrained responses:
- OpenAI: `response_format: { type: "json_schema", json_schema: ... }`
- Anthropic: tool-use-as-schema pattern
- Google: `response_mime_type: "application/json"` + `response_schema`

When `mode == "api"`, the review schema from `schemas.py` is translated to a JSON
Schema and passed to the provider. The response comes back as validated JSON — no YAML
extraction regex, no parse/cross-validate dance. `review.py` gets a new
`parse_review_json(data: dict) -> ReviewResult` path alongside the existing
`parse_review_output(text: str) -> ReviewResult`.

**Provider adapters must return already-validated internal review JSON.** `review.py`
must not contain provider-specific repair logic. Any normalization happens inside the
adapter before the result crosses the boundary.

This is additive. CLI reviewers still emit YAML and use the existing parse path.

### Mode-aware prompt building

Review prompts are currently optimized for YAML output ("emit only a YAML block").
API reviewers with structured output don't need these instructions — the schema is
enforced by the transport.

The prompt builder must be mode-aware:
- `mode == "cli"`: include YAML format instructions (existing behaviour)
- `mode == "api"`: omit YAML format instructions; the output schema is enforced
  by the transport layer

This applies to both code review prompts (`build_review_prompt`) and plan review
prompts (`build_plan_review_prompt`). The prompt builder receives `mode` and adjusts
formatting instructions accordingly.

### Config shape

```yaml
profiles:
  dev:
    cli: claude                # mode: cli (derived)
    model: sonnet
    budget_usd: 50.00
    timeout_seconds: 600
    allowed_tools: [Read, Edit, Write, Bash, Glob, Grep]

  review_pool:
    - name: openai-reviewer
      provider: openai         # mode: api (derived)
      model: o4-mini
      budget_usd: 1.00
      timeout_seconds: 120
      reasoning_effort: medium
      review_role: correctness

    - name: gemini-reviewer
      provider: google         # mode: api (derived)
      model: gemini-2.5-pro
      budget_usd: 1.00
      timeout_seconds: 120
      review_role: patterns

    - name: local-reviewer
      provider: openai         # reuses OpenAI adapter with base_url override
      model: llama3.2
      base_url: http://localhost:11434/v1   # Ollama / LM Studio / vLLM
      budget_usd: 0.00
      timeout_seconds: 300
      review_role: patterns

plan_agent_review:
  enabled: true
  provider: openai             # was: cli: codex
  model: o4-mini
  budget_usd: 0.50
  timeout_seconds: 300
```

Backward compatibility: existing `cli:`-based configs work unchanged.

### ModelProfile changes

```python
@dataclass(frozen=True)
class ModelProfile:
    name: str
    model: str
    budget_usd: float
    timeout_seconds: int
    allowed_tools: tuple[str, ...]
    # Transport — exactly one of cli/provider is set
    cli: str | None = None        # "claude", "codex", "gemini"
    provider: str | None = None   # "anthropic", "openai", "google"
    # Optional
    timeout_medium_seconds: int | None = None
    timeout_large_seconds: int | None = None
    reasoning_effort: str | None = None
    review_role: str | None = None
    base_url: str | None = None   # overrides provider's default API endpoint

    @property
    def mode(self) -> str:
        return "api" if self.provider else "cli"
```

Validation at config load:
- Exactly one of `cli` / `provider` must be set (not both, not neither)
- `cli` must be in `SUPPORTED_CLIS`
- `provider` must be in `SUPPORTED_PROVIDERS = {"anthropic", "openai", "google"}`
- `allowed_tools` on an `api`-mode profile is a **config error** (tools are not
  supported in API mode; declaring them indicates a mistaken mental model)
- Provider readiness (SDK + API key) is validated eagerly, **except** when
  `base_url` is set to a non-remote URL (localhost/127.0.0.1) — local servers
  don't require an API key and the env var check is skipped

### Local model support

`base_url` on an API-mode profile overrides the provider's standard endpoint.
Any OpenAI-compatible local server works with `provider: openai` + `base_url`:

| Server | base_url |
|---|---|
| Ollama | `http://localhost:11434/v1` |
| LM Studio | `http://localhost:1234/v1` |
| vLLM | `http://localhost:8000/v1` |

The OpenAI SDK accepts `base_url` directly: `OpenAI(base_url=..., api_key="local")`.
`budget_usd: 0.00` is valid for local models (no metered cost).
`cost_usd` returns `None` for local models since there is no usage billing.

### Runner dispatch

`run_agent()` dispatches by `profile.mode`:

```python
def run_agent(...) -> AgentResult:
    if profile.mode == "api":
        return _run_api(...)
    # existing CLI dispatch
    runners = {"claude": _run_claude, "codex": _run_codex, "gemini": _run_gemini}
    ...
```

### API runner implementation

`_run_api()` lives in `runner_api.py` and dispatches by `profile.provider`:

```python
def run_api_agent(
    *,
    prompt: str,
    profile: ModelProfile,
    quiet: bool = False,
) -> AgentResult:
    ...
```

Signature is minimal — no `working_dir`, `session_id`, or `is_pool` because API
calls are stateless, don't access the filesystem, and are inherently concurrent-safe.

Each provider adapter is a function that:
1. Takes the prompt and profile
2. Makes an HTTP call via the provider SDK
3. Returns `AgentResult` with `session_id=None`, real `cost_usd`, and structured
   `raw` containing the full API response

### AgentResult changes

Add `structured_data: dict | None = None` to `AgentResult`:

```python
@dataclass(frozen=True)
class AgentResult:
    success: bool
    output: str                              # always present for logging
    session_id: str | None
    cost_usd: float | None                   # None = unknown (never 0.0 for unknown)
    exit_code: int
    raw: dict[str, Any]
    profile_name: str = ""
    model_usage: tuple[ModelUsage, ...] = ()
    structured_data: dict | None = None      # NEW: parsed JSON for API reviewers
```

- `output`: always populated with the text response (for logging/debugging)
- `structured_data`: populated by API reviewers with the parsed JSON dict; `None`
  for CLI reviewers (they go through YAML parse path)
- `cost_usd`: changed to `float | None`. `None` means unknown. Never use `0.0`
  to mean "unknown" — that is what caused cost blindness before.

### Cost accounting

API responses include token counts and (usually) cost. The runner normalizes to
the existing `ModelUsage` dataclass.

Cost fallback chain:
1. **Preferred:** provider-reported cost from API response
2. **Fallback:** locally estimated cost from known per-token pricing table
3. **Final fallback:** `None` — never fake-zero

The pricing table is a simple dict in `runner_api.py` mapping `(provider, model)`
to per-token rates. It doesn't need to be exhaustive — unknown models fall through
to `None`.

### File structure

```
src/theforge/
  runner.py          # existing — add mode dispatch to run_agent()
  runner_api.py      # NEW — run_api_agent() + provider adapters
  config.py          # evolve ModelProfile, add SUPPORTED_PROVIDERS
  review.py          # add parse_review_json() path
  schemas.py         # add review_json_schema() export
```

No per-provider files until a second phase proves the need.

### Session handling

API-mode agents are stateless. No session ID is extracted, stored, or passed.
`sessions.py` becomes purely a CLI concern — API reviewers don't participate.

This eliminates the `is_pool` hack for API reviewers entirely. CLI reviewers in
a parallel pool still get `is_pool=True` (existing behaviour unchanged).

## Requirements

1. Add `provider` field to `ModelProfile`; `mode` is a derived property (not stored)
2. Validate at config load: exactly one of `cli`/`provider`, supported values only
3. Eagerly validate provider readiness at config load: SDK importable, API key set
4. `allowed_tools` on an API-mode profile is a config error, not a warning
5. Implement `runner_api.py` with provider adapters for OpenAI, Anthropic, Google
6. API adapters use provider SDKs (`openai`, `anthropic`, `google-genai`)
7. SDKs are optional deps — imported at call time, validated at config load
8. Auth via env vars (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`)
9. API runners return real `cost_usd` from API responses; fallback to estimated
   cost from pricing table; final fallback `None` (never `0.0` for unknown)
10. API runners return `session_id=None` — stateless, no resume
11. `run_agent()` dispatches by `profile.mode` to CLI or API runner
12. API runners are concurrent-safe with no `is_pool` gating needed
13. Add `review_json_schema()` to `schemas.py` that exports the review schema as
    JSON Schema for structured-output API calls
14. Add `parse_review_json()` to `review.py` for parsing JSON review responses
15. Provider adapters return already-validated JSON; `review.py` has no
    provider-specific repair logic
16. Coordinator passes structured output schema when calling API-mode reviewers
17. Prompt builder is mode-aware: omit YAML format instructions for API mode
    where the output schema is enforced by the transport
18. Add `structured_data: dict | None` to `AgentResult` for API review responses
19. Change `cost_usd` from `float` to `float | None` on `AgentResult`
20. Backward compatible: existing `cli:`-based configs work unchanged
21. `forge.yaml` updated to use `provider: openai` for review_pool and
    plan_agent_review
22. Phase 1 scope: review_pool and plan_agent_review only; dev, preflight, and
    synthesis remain CLI-only and unchanged
23. Add `base_url` field to `ModelProfile`; when set, passed as `base_url` to
    the provider SDK client (enables Ollama, LM Studio, vLLM, any
    OpenAI-compatible local server via `provider: openai`)
24. Skip API key env var validation when `base_url` points to localhost/127.0.0.1
25. `cost_usd` is `None` for local model invocations (no metered billing)

## Acceptance Criteria

- [ ] `provider: openai` profile in forge.yaml triggers API-mode review
- [ ] `provider: anthropic` profile works for review pool
- [ ] `provider: google` profile works for review pool
- [ ] `cli: claude` dev profile works unchanged (regression)
- [ ] Mixed pool (one CLI reviewer + one API reviewer) works in parallel
- [ ] API reviewer returns real `cost_usd` (not `None`, not `0.0`) from API usage
- [ ] API reviewer returns `structured_data` dict, not YAML-in-text
- [ ] `session_id` is `None` for all API results (no global state)
- [ ] Configured API profiles fail validation before execution starts if SDK or
      API key is missing
- [ ] Config with both `cli` and `provider` on same profile raises ValueError
- [ ] Config with neither `cli` nor `provider` raises ValueError
- [ ] Config with `allowed_tools` on an API-mode profile raises ValueError
- [ ] API reviewer failure returns actionable provider-specific error message
- [ ] Invalid structured output from API is rejected and surfaced cleanly
- [ ] Existing non-review CLI workflows pass unchanged
- [ ] New tests cover: API dispatch, provider adapters (mocked), config validation,
      structured output parsing, cost extraction, mixed pool mode

## Implementation Order

1. Add config validation and derived transport (`config.py`)
2. Implement OpenAI API adapter (highest priority — Codex quota relief)
3. Add JSON review parsing path (`review.py`, `schemas.py`)
4. Wire into `run_agent()` dispatch (`runner.py`, `runner_api.py`)
5. Migrate review_pool and plan_agent_review in `forge.yaml`
6. Add Anthropic adapter
7. Add Google adapter
8. Mode-aware prompt builder updates (`task.py`)

## Migration

### Phase 1 (this spec)

- Add `provider` to config with eager validation
- Implement API runners for OpenAI, Anthropic, Google
- Wire into review_pool and plan_agent_review
- Keep dev, preflight, and synthesis CLI-only and unchanged

### Phase 2 (future)

- Structured output schema for plan review (not just code review)
- API-based synthesis if synthesis is text-only
- Remove `is_pool` gating for codex/gemini CLI runners if all review moves to API
- Refactor `runner.py` into `runner_cli.py` + shared `runner_base.py`

### Phase 3 (future)

- API dev agents with tool-calling support (Anthropic/OpenAI APIs support tools)
- Adaptive model selection based on cost/quality telemetry from API usage data

## Risks

- **SDK dependency management.** Three new optional PyPI deps. Mitigated by lazy
  import + clear error messaging at config load. Only the configured provider's SDK
  needs to be installed.

- **Provider asymmetry in structured output.** The three providers do not behave
  equally: OpenAI's JSON Schema mode is relatively direct; Anthropic needs more
  adapter logic around tool-style constrained output; Google's schema support may
  need more defensive validation. Each adapter must validate and normalize the
  response before returning — `review.py` must not contain provider-specific logic.

- **Prompt tuning.** Existing review prompts are optimized for YAML output. API
  reviewers with structured output don't need those instructions. Mode-aware prompt
  building (requirement 17) handles this, but the prompts themselves may need
  iteration to produce optimal results with schema-constrained output.

- **cost_usd type change.** Changing `cost_usd` from `float` to `float | None`
  affects all existing callers that format or sum costs. Coordinator cost-tracking
  logic must handle `None` gracefully (treat as 0 for summation, display as "unknown"
  in logs). This is a controlled blast radius — grep for `cost_usd` and update each
  site.
