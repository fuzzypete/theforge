---
name: "Adaptive model assignment per story"
slug: adaptive-model-assignment
pytest_target: tests/
---

# Adaptive model assignment per story

Today every story gets the same dev model, same review pool, same
timeouts. But a 5-AC React component story and a 12-AC cross-platform
iOS+watchOS story shouldn't run the same way. The human shouldn't have
to think about which model goes where — just declare what's available
and let the system match models to stories.

The preflight already assesses complexity. Extend it to also emit
domain tags (react, ios-native, python, watchos, etc). Then the
coordinator uses complexity + domains to pick dev model, review pool
composition, tool sets, timeouts, and budget from the available pool.

## Config format

```yaml
agents:
  - name: sonnet
    cli: claude
    model: sonnet
  - name: opus
    cli: claude
    model: opus
  - name: codex
    provider: openai
    model: gpt-5.1-codex-mini
  - name: gemini
    provider: google
    model: gemini-2.5-flash

assignment:
  min_reviewers: 1
  max_reviewers: 3
  min_dev_timeout: 600
  max_dev_timeout: 1800
  budget_per_story_usd: 10.00    # max spend per story; scales down for small
```

No per-agent role assignment. No per-story config. The coordinator
reads the pool and the preflight output and decides.

## Assignment logic

Preflight output gains:
```yaml
complexity: medium
domains: [react, zustand, css]
estimated_files: 4
```

Coordinator assignment rules (deterministic, not LLM):
- **Dev model**: small → cheapest capable; medium → mid-tier; large → strongest
- **Review count**: small → min_reviewers; large → max_reviewers
- **Reviewer selection**: prefer models whose strengths match domains
  (codex for logic, gemini for patterns, opus for architecture)
- **Timeouts**: interpolate between min/max based on complexity
- **Budget**: proportional to complexity within budget_per_story_usd cap
- **Tool sets**: drop tools irrelevant to domains (no Bash pytest for
  pure iOS native stories, no Swift tooling for React stories)

## Acceptance criteria

- `agents:` key in forge.yaml defines the available model pool
- `assignment:` key sets min/max bounds for reviewers, timeouts, budget
- Preflight output includes `domains` list alongside complexity
- Coordinator selects dev model based on complexity + available pool
- Coordinator selects reviewer count between min and max based on complexity
- Coordinator selects which reviewers based on domain match
- Timeouts scale between min/max based on complexity
- Per-story budget respects budget_per_story_usd cap
- Existing `profiles:` config still works (explicit overrides adaptive)
- All assignment decisions logged at verbose level
- Deterministic: same story + same pool = same assignment every time
