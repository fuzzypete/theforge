# Local Models (Ollama / vLLM)

A local model is an ordinary **API transport** pointed at a local endpoint.
Locality is endpoint metadata (`base_url`) — there is no `local/` provider and
no `local` transport kind. TheForge routes local models through the existing
OpenAI adapter; no additional adapter code is needed.

## Prerequisites

Start your local inference server before running `forge`:

```bash
# Ollama
ollama serve
ollama pull codestral   # or llama3.1, qwen2.5-coder, deepseek-coder

# vLLM
vllm serve codestral --host 127.0.0.1 --port 11434
```

## forge.yaml configuration

Declare the model with `provider: openai`, an `api` transport, and a `base_url`
pointing to the local server. No API key is required for local endpoints.

```yaml
models:
  enabled:
    - provider: openai
      model: codestral        # must match the model name in your server
      transport:
        kind: api
      base_url: http://localhost:11434/v1
      routing:
        tier: fast
      cost:
        input_per_mtok: 0     # local inference is not billed per token
        output_per_mtok: 0

    - provider: openai
      model: qwen2.5-coder
      transport:
        kind: api
      base_url: http://localhost:11434/v1
      routing:
        tier: fast
      cost:
        input_per_mtok: 0
        output_per_mtok: 0

budget_usd: 1.00

overrides:
  dev:
    timeout_seconds: 300      # local models are slower than cloud APIs
```

### Registry shorthand

These identities are pre-registered as API transports with a localhost
`base_url`, so they can be selected in `models:` directly:

| Model identity                | Model name       | Default `base_url`             |
|-------------------------------|------------------|--------------------------------|
| `openai/codestral/api`        | `codestral`      | `http://localhost:11434/v1`    |
| `openai/deepseek-coder/api`   | `deepseek-coder` | `http://localhost:11434/v1`    |
| `openai/llama3.1/api`         | `llama3.1`       | `http://localhost:11434/v1`    |
| `openai/qwen2.5-coder/api`    | `qwen2.5-coder`  | `http://localhost:11434/v1`    |

```yaml
models:
  enabled:
    - openai/qwen2.5-coder/api
```

Point one at a different server by declaring it inline:

```yaml
models:
  enabled:
    - provider: openai
      model: qwen2.5-coder
      transport:
        kind: api
      base_url: http://127.0.0.1:8000/v1
      routing:
        tier: fast
```

> **Important**: a local model needs a `base_url`. Without one the OpenAI
> adapter contacts the real OpenAI API with a model name it doesn't recognise.

## Cost tracking

All profiles with a `localhost` or `127.0.0.1` `base_url` report cost as
**$0.00**. Token counts are still tracked for budgeting purposes.

## Tool-calling fallback

Some local models do not support the OpenAI tool-calling API. If a model
returns a 400 error mentioning tools, TheForge automatically retries using
single-shot text mode with an instruction to emit structured JSON. This
fallback is transparent — you will see a warning in the log but the task
will continue.

## Verifying connectivity

```bash
forge check-providers
```

Local profiles are labelled `[local]` in the output and show `$0.000` for cost:

```
  local-reviewer         openai     qwen2.5-coder              ✓  2.3s  $0.000 [local]
```

---

## See also

- [Provider Setup Guide](choose-your-provider-setup.md) — named patterns including local/privacy-first and hybrid
- [Getting Started](getting-started.md) — full setup walkthrough
- [Troubleshooting](troubleshooting.md) — provider auth and connectivity issues
- [CLI Reference](cli-reference.md) — forge check-providers and secrets-init
