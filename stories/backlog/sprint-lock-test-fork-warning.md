---
name: "Sprint lock tests emit fork() deprecation warning on Python 3.12"
slug: sprint-lock-test-fork-warning
pytest_target: tests/test_sprint_lock.py
---

# Sprint lock tests emit fork() deprecation warning on Python 3.12

## Observed behavior

Running the test suite on Python 3.12 produces:

```
DeprecationWarning: This process (pid=...) is multi-threaded,
use of fork() may lead to deadlocks in the child.
```

The warning originates from `multiprocessing.popen_fork` called by tests in
`tests/test_sprint_lock.py`.

## Expected behavior

The test suite runs cleanly with no deprecation warnings.
