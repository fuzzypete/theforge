# Dropped legacy rates (`PRICING_TABLE`, removed in #2388)

Until #2388 a rate could be declared in two places: the model catalog
(`src/theforge/config/data/models.yaml`, data, per-entry provenance, overridable
per project) and `PRICING_TABLE`, a dictionary compiled into
`src/theforge/runners/rate_registry.py` that no configuration could override and
that accounting consulted whenever the registry missed.

The table is gone. This file is the record of what it held, so a rate removed
here is not a rate silently lost: an operator who later enables one of these
models has the figure that was in force and can declare it on a catalog entry —
with `pricing_provenance`, which is what the table could never carry.

Figures are USD per 1M tokens, `(input, output)`, as the table stated them on
the day it was removed. **They are unattributed and undated** — that is the
defect that removed them. Treat any figure below as a starting point to check
against the provider's current pricing page, not as a price to declare as-is.

## Carried into the catalog

These ten rows duplicated a catalog entry that already priced the same
`(provider, model)` with provenance. The catalog figures are identical to the
table's, so removing the row changed no price. Nothing to migrate.

| Provider  | Model                  | Table rate      | Catalog entry (with provenance) |
| --------- | ---------------------- | --------------- | ------------------------------- |
| openai    | `gpt-5.4-mini`         | 0.25 / 2.00     | `openai/gpt-5.4-mini` (cli, api) |
| openai    | `gpt-5.4`              | 1.25 / 10.00    | `openai/gpt-5.4` (cli, api)     |
| openai    | `gpt-5.4-pro`          | 15.00 / 120.00  | `openai/gpt-5.4-pro` (cli, api) |
| openai    | `gpt-5.5`              | 5.00 / 30.00    | `openai/gpt-5.5` (cli)          |
| anthropic | `claude-opus-4-6`      | 15.00 / 75.00   | `anthropic/claude-opus-4-6` (cli) |
| anthropic | `claude-sonnet-4-6`    | 3.00 / 15.00    | `anthropic/claude-sonnet-4-6` (cli) |
| anthropic | `claude-haiku-4-5`     | 1.00 / 5.00     | `anthropic/claude-haiku-4-5` (cli) |
| google    | `gemini-3.5-flash`     | 1.50 / 9.00     | `google/gemini-3.5-flash` (api) |
| google    | `gemini-3.1-pro-preview` | 2.00 / 12.00  | `google/gemini-3.1-pro-preview` (api, cli) |
| google    | `gemini-2.5-pro`       | 1.25 / 10.00    | `google/gemini-2.5-pro` (api, cli) |

## Dropped

These eleven rows priced identities no catalog entry names, so nothing this
repository can dispatch could reach them: they were prior-generation families
kept alive only by the table. They are recorded here and declared nowhere.

| Provider | Model                                | Rate at removal |
| -------- | ------------------------------------ | --------------- |
| openai   | `o4-mini`                            | 1.10 / 4.40     |
| openai   | `gpt-4o`                             | 2.50 / 10.00    |
| openai   | `gpt-4o-mini`                        | 0.15 / 0.60     |
| openai   | `gpt-5.1-codex-mini`                 | 1.50 / 6.00     |
| openai   | `gpt-5.1-codex`                      | 3.00 / 12.00    |
| openai   | `gpt-5.1-codex-max`                  | 6.00 / 24.00    |
| google   | `gemini-3.1-pro-preview-customtools` | 2.00 / 12.00    |
| google   | `gemini-2.5-flash`                   | 0.30 / 2.50     |
| google   | `gemini-2.5-flash-lite`              | 0.10 / 0.40     |
| google   | `gemini-2.0-flash`                   | 0.10 / 0.40     |
| google   | `gemini-2.0-flash-lite`              | 0.075 / 0.30    |

## Re-enabling one of these

Add a catalog entry (or a `models.custom` entry in `forge.yaml`) naming the
identity and its transport, and declare the price you checked against the
provider today:

```yaml
- provider: google
  model: gemini-2.5-flash
  transport: {kind: api}
  routing:
    tier: cheap
    capability: 7
    cost_rank: 1
  cost:
    input_per_mtok: 0.30
    output_per_mtok: 2.50
    pricing_provenance: gemini-2.5-flash
```

An entry whose transport reports what it was billed instead of token counts
(the Claude CLI) declares `cost: {rate_basis: provider_reported}` and no
figures — see `claude-opus-5` in the catalog.
