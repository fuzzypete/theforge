# Expected Output — hello-forge

A realistic terminal transcript of `forge run specs/add-greeting.md --verbose`.
Use this as a reference to verify your run is behaving normally.

---

## Successful run (happy path)

```
$ forge run specs/add-greeting.md --verbose

[forge] ═══════════════════════════════════════════════
[forge]   Story:   Add greeting endpoint
[forge]   Slug:    add-greeting
[forge]   Config:  forge.yaml
[forge] ═══════════════════════════════════════════════

[forge] ▸ WORKSPACE   add-greeting
[forge]   create_command: git worktree add .forge/worktrees/add-greeting -b forge/add-greeting main
[forge]   Created worktree at: .forge/worktrees/add-greeting
[forge]   setup_command: pip install pytest
[forge]   Workspace ready

[forge] ▸ PREFLIGHT   sonnet
[forge]   Checking if story is already implemented...
[forge]   Verdict: PROCEED
[forge]   Reason: src/app.py exists but has no HTTP endpoint or JSON output

[forge] ▸ DEV         sonnet  iter 1/3
[forge]   Sending implementation prompt (2,847 tokens)
[forge]   ↳ Read: src/app.py
[forge]   ↳ Read: tests/test_app.py
[forge]   ↳ Edit: src/app.py
[forge]     + Added greet_json(name=None) returning dict
[forge]   ↳ Write: tests/test_greet.py
[forge]     + 3 tests for greet_json
[forge]   Dev complete (1m 43s, $0.48)

[forge] ▸ VALIDATE    pytest
[forge]   Running: python -m pytest tests/ -q
[forge]   ...
[forge]   5 passed in 0.4s
[forge]   Gate: PASS

[forge] ▸ REVIEW      claude-reviewer (opus)
[forge]   Sending review prompt (4,112 tokens)
[forge]   Reading: src/app.py (22 lines)
[forge]   Reading: tests/test_greet.py (18 lines)
[forge]   Review complete (2m 29s, $0.62)
[forge]   ✓ REVIEW   APPROVE  0 P1  0 P2

[forge] ▸ DONE        add-greeting
[forge]   Branch: forge/add-greeting
[forge]   Duration: 5m 14s
[forge]   Cost: $1.10 total ($0.48 dev, $0.62 review)
[forge]   Audit: .forge/worktrees/add-greeting/forge_audit.yaml

[forge] ═══════════════════════════════════════════════
[forge]   APPROVE — ready to merge
[forge]   Run: git merge forge/add-greeting
[forge]   Or:  forge run specs/add-greeting.md --auto-merge
[forge] ═══════════════════════════════════════════════
```

---

## Failed validation (VALIDATE → DEV retry)

If tests fail after the first dev iteration, the coordinator retries:

```
[forge] ▸ DEV         sonnet  iter 1/3
[forge]   ...
[forge]   Dev complete (1m 52s, $0.51)

[forge] ▸ VALIDATE    pytest
[forge]   Running: python -m pytest tests/ -q
[forge]   FAILED tests/test_greet.py::test_greet_with_name - AssertionError
[forge]   1 failed in 0.4s
[forge]   Gate: FAIL

[forge] ▸ DEV         sonnet  iter 2/3
[forge]   Sending fix prompt with gate failure details...
[forge]   ↳ Read: tests/test_greet.py
[forge]   ↳ Edit: src/app.py
[forge]   Dev complete (1m 11s, $0.29)

[forge] ▸ VALIDATE    pytest
[forge]   Running: python -m pytest tests/ -q
[forge]   5 passed in 0.4s
[forge]   Gate: PASS

[forge] ▸ REVIEW      claude-reviewer (opus)
[forge]   ...
```

---

## Review request-changes (REVIEW → DEV retry)

If a reviewer finds a P1 issue, the coordinator loops back to DEV:

```
[forge] ▸ REVIEW      claude-reviewer (opus)
[forge]   Review complete (2m 44s, $0.68)
[forge]   ✗ REVIEW   REQUEST_CHANGES  1 P1  0 P2

[forge]   P1 findings:
[forge]     src/app.py:8 — greet_json does not handle empty string name
[forge]       suggestion: treat empty string same as None

[forge] ▸ DEV         sonnet  iter 2/3  (review cycle 1/2)
[forge]   Sending fix prompt with P1 findings...
[forge]   ↳ Edit: src/app.py
[forge]   Dev complete (0m 58s, $0.22)

[forge] ▸ VALIDATE    pytest
[forge]   5 passed in 0.4s
[forge]   Gate: PASS

[forge] ▸ REVIEW      claude-reviewer (opus)
[forge]   Review complete (2m 19s, $0.59)
[forge]   ✓ REVIEW   APPROVE  0 P1  0 P2

[forge] ▸ DONE        add-greeting
```

---

## Escalation

If max iterations or review cycles are exceeded:

```
[forge] ▸ DEV         sonnet  iter 3/3
[forge]   ...

[forge] ▸ VALIDATE    pytest
[forge]   3 failed in 0.4s
[forge]   Gate: FAIL

[forge] ▸ ESCALATE    add-greeting
[forge]   Max dev iterations (3) reached without a PASS gate.
[forge]   Branch forge/add-greeting left in place for inspection.
[forge]   Audit: .forge/worktrees/add-greeting/forge_audit.yaml

[forge] ═══════════════════════════════════════════════
[forge]   ESCALATE — manual intervention required
[forge]   Inspect: cd .forge/worktrees/add-greeting
[forge]   Resume:  forge run specs/add-greeting.md --resume
[forge] ═══════════════════════════════════════════════
```

---

## Resume after interruption

If you interrupt the run (Ctrl+C) or it crashes mid-phase:

```
$ forge run specs/add-greeting.md --resume --verbose

[forge] ▸ RESUME      add-greeting
[forge]   Found existing worktree: .forge/worktrees/add-greeting
[forge]   Last completed phase: DEV (iter 1)
[forge]   Resuming from: VALIDATE
[forge]   ...
```

See [Resume Semantics](../../docs/guides/cli-reference.md#resume-behavior) for
full details on what each interrupted state resumes to.
