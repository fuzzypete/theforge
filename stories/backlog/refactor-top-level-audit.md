---
name: "Audit and organize remaining top-level modules"
slug: "refactor-top-level-audit"
pytest_target: tests/
---

# Audit and organize remaining top-level modules

## Problem

After coordinator/ and runners/ are extracted, roughly 15 modules remain at the top level of `theforge/`. Some are small and focused (artifacts, sessions, traces) and belong there. Others are large (`sprint.py` at 1267 lines, `ideate.py` at 804 lines), share a concern with other modules (three separate validators, notification backends), or have unclear ownership. The flat layout doesn't communicate which modules are stable utilities vs. active subsystems.

## Requirements

- Audit every remaining top-level module for size, cohesion, and whether it belongs at the top level or inside an existing package.
- Modules that share a clear concern should be grouped (e.g., the three validators).
- Modules over 500 lines should be reviewed for multi-concern splits per the inspection threshold policy.
- Modules that are clearly internal to coordinator/ or runners/ but were missed in earlier stories should be relocated.
- Small, focused, stable modules may remain top-level — not everything needs a package.
- After this story, a new contributor should be able to look at `src/theforge/` and understand the layout without a guide.

## Acceptance Criteria

- [ ] Every top-level module has been evaluated and either confirmed as correctly placed or relocated
- [ ] No top-level module exceeds 500 lines without documented cohesion justification
- [ ] Related modules are grouped where grouping aids navigation
- [ ] `make test`, `make lint`, and `make gate` pass
- [ ] No behavioral changes
