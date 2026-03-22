---
name: "Coordinator HTTP timeout — bound all outbound calls"
slug: coordinator-http-timeout
pytest_target: tests/
---

# Coordinator HTTP timeout — bound all outbound calls

## Problem

Coordinator-level HTTP calls (ntfy notifications, GitHub API via `gh`) have no
socket connect timeout. When a connection hangs, the entire sprint blocks
indefinitely. Observed in production: `sock_connect` blocked on `poll()` for
over an hour with TCP sockets stuck in CLOSE_WAIT.

The runner has watchdog timeouts for agent subprocesses, but the coordinator's
own outbound calls have no equivalent protection. Notification and PR creation
are best-effort operations that should never hang the pipeline.

## Acceptance criteria

- All `urllib.request.urlopen` calls in `coord_notify.py` have an explicit
  `timeout=` parameter (connect + read, 15-30s)
- `_ntfy_publish` is wrapped in try/except so notification failures log a
  warning and continue — never block the sprint
- Any subprocess calls to `gh` (PR creation, issue filing) have a timeout
- Notification and PR creation failures are logged at WARNING level with
  the error detail, then the pipeline continues
- No new dependencies introduced
- Existing tests pass
