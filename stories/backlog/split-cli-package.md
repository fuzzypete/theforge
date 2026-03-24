---
name: "Split cli.py into a cli/ package"
slug: split-cli-package
pytest_target: tests/
---

# Split cli.py into a cli/ package

## Problem

`cli.py` is 2,372 lines containing every CLI subcommand, all argument parsing,
and output formatting in a single file. Any story that touches a command risks
merge conflicts with unrelated command changes. The file exceeds reasonable
working-context limits for dev agents.

## Solution

Convert `src/theforge/cli.py` into a `src/theforge/cli/` package. Each
subcommand gets its own module. A thin entrypoint builds the parser and
dispatches.

### Target layout

```
src/theforge/cli/
  __init__.py      — re-exports build_parser, main (backward compat)
  main.py          — top-level parser construction + dispatch
  run.py           — forge run
  sprint.py        — forge sprint
  review.py        — forge review
  audit.py         — forge audit
  daemon.py        — forge daemon
  telemetry.py     — forge telemetry
  ideate.py        — forge ideate
  providers.py     — forge providers
  status.py        — forge status / forge check-config
  shared.py        — shared helpers (output formatting, common arg groups)
```

## Constraints

- Pure structural refactor — zero behavioral change.
- `from theforge.cli import main` must continue to work.
- Every command's `--help` output must be identical before and after.
- No new dependencies.

## Acceptance Criteria

- [ ] `make test` passes with the same test count.
- [ ] `make lint` passes.
- [ ] `from theforge.cli import main` works.
- [ ] `forge --help` and every subcommand `--help` produce identical output.
- [ ] No single file in `src/theforge/cli/` exceeds 400 lines.
- [ ] The old `cli.py` file no longer exists.
