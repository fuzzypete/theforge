# Local Models (Ollama / vLLM)

TheForge routes local models through the existing OpenAI adapter by setting
`base_url` in your profile. No additional adapter code is needed.

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

Add a profile with `provider: openai`, your model name, and a `base_url`
pointing to the local server. No API key is required for local endpoints.

```yaml
profiles:
  dev:
    provider: openai
    model: codestral          # must match the model name in your server
    base_url: http://localhost:11434/v1
    timeout_seconds: 300
    allowed_tools:
      - read_file
      - bash
      - glob
      - grep
      - write_file

  review_pool:
    - name: local-reviewer
      provider: openai
      model: qwen2.5-coder
      base_url: http://localhost:11434/v1
      timeout_seconds: 120
```

### Registry shorthand

The following model keys are pre-registered and can be used in profiles without
specifying `cli`:

| Registry key             | Model name       |
|--------------------------|------------------|
| `openai/codestral`       | `codestral`      |
| `openai/deepseek-coder`  | `deepseek-coder` |
| `openai/llama3.1`        | `llama3.1`       |
| `openai/qwen2.5-coder`   | `qwen2.5-coder`  |

> **Important**: All local registry entries require `base_url` to be set.
> Without `base_url`, the OpenAI adapter will attempt to contact the real
> OpenAI API with a model name it doesn't recognise.

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
