# Refactor TheForge into a well-structured Python project

TheForge's `src/theforge/` is a flat namespace with 30+ top-level modules. Several have grown past maintainability — `coordinator.py` (2200 lines), `runner_api.py` (2100 lines), `coord_phases.py` (1400 lines), `sprint.py` (1200 lines). A partial extraction of coordinator internals into `coord_*` siblings was attempted but stalled — the original file never shrank because every extracted symbol was re-exported back for backward compatibility, and circular dependency workarounds accumulated rather than being resolved.

The project needs a package structure that follows Python best practices: clear subpackages by responsibility, small intentional public surfaces, and a layout where new code naturally lands in the right place without touching god files.

Critically, the structure must prevent future bloat — not just clean up current bloat. Whatever comes out of this should make it obvious when a module is growing wrong and where new work belongs.

## Constraints

- `make test` and `make lint` must pass throughout
- CLI behavior and config semantics remain unchanged
- No new features — purely structural
- Migration must leave `make test` passing at the end — no compat shims to preserve broken intermediate states

## Acceptance criteria

- No god files
- No backward-compat re-export chains
- Package layout communicates ownership and responsibility
- A new contributor can tell where code belongs without reading a guide
- The structure actively resists the patterns that created the current mess
