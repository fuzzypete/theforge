---
name: "Remove compatibility facades and normalize imports"
slug: structure-cleanup-facades
pytest_target: tests/
depends_on: [split-cli-package, split-task-package, coordinator-relocate-modules, coordinator-extract-entrypoints, coordinator-split-phases, split-config-package]
---

# Remove compatibility facades and normalize imports

## Problem

After the package splits, several modules will have compatibility facades
(re-exports from the old import paths). Internal callers should import from
the specific submodule, not through facades. Leaving facades permanently adds
indirection and hides the real dependency graph.

## Solution

Update all internal imports across the codebase to point at the specific
submodule where each symbol is defined. Remove re-exports that are no longer
needed. Keep re-exports only for symbols that are part of the documented public
API (used by external consumers or forge.yaml plugin hooks).

## Constraints

- Pure import cleanup — zero behavioral change.
- Public API imports (`theforge.coordinator.run_task`, `theforge.config.ForgeConfig`,
  `theforge.cli.main`) must remain stable for external callers.
- No new dependencies.

## Acceptance Criteria

- [ ] `make test` passes with the same test count.
- [ ] `make lint` passes.
- [ ] No internal `from theforge.coordinator import ...` uses the facade — all
      point to specific coord modules.
- [ ] No internal `from theforge.config import ...` uses the facade — all
      point to specific config modules.
- [ ] Facade files contain only public API re-exports, no business logic.
