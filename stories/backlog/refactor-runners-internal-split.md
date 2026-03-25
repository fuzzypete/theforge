---
name: "Decompose runner_api.py within runners package"
slug: "refactor-runners-internal-split"
pytest_target: tests/
---

# Decompose runner_api.py within runners package

## Problem

After the runners/ package is formed, `runner_api.py` (2177 lines) will be the largest module in the codebase. It contains four distinct provider adapters (OpenAI, Anthropic, Google, DeepSeek), four corresponding finalizers, the agent loop manager, and shared utilities — all in one file. Adding or modifying a provider requires navigating a 2000+ line file and risking collateral changes to unrelated providers.

## Requirements

- Break `runner_api.py` into cohesive internal modules within `runners/`, grouped by responsibility (provider adapters, finalizers, loop management, shared utilities).
- Each provider's adapter and finalizer code should be locatable without scrolling through unrelated providers.
- The `runners/` public API (exposed through `__init__.py`) must not change.
- No module within `runners/` should exceed the 500-line inspection threshold without cohesion justification.

## Acceptance Criteria

- [ ] `runner_api.py` no longer exists as a single 2000+ line file
- [ ] Provider-specific code is isolated so modifying one provider doesn't touch another's module
- [ ] `runners/__init__.py` public API unchanged
- [ ] `make test`, `make lint`, and `make gate` pass
- [ ] No behavioral changes
