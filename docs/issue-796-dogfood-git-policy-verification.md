# Issue #796 — Dogfood Git-Policy Verification Record

Companion evidence for the story *"Dogfood: align TheForge's own git policy with
the canonical template."* This is the committed, auditable record the acceptance
criteria call for. It covers the redaction-audit spot-check, the end-to-end
runtime verification, and the one criterion that is structurally an operator
merge-gate rather than a dev-implementable change.

Design source of truth: `docs/plans/forge-storage-layout.md`.

---

## 1. Redaction-audit spot-check over existing audit records

**AC:** *A redaction-audit pass over existing per-run audit records has completed;
any gaps found have been closed by extending the redaction rules in Story 1
before this story merges.*

**Status: complete — 0 leaks found, no Story 1 extension required.**

The redaction pass (`src/theforge/coordinator/redact.py`) does three things,
recursively over an audit object: (1) redact values of secret-shaped keys
(`secret|token|password|api[_-]?key|authorization`); (2) scrub any `.forge/.env`
value (≥8 chars) wherever it appears as a substring, including free text;
(3) collapse any `environment` dict to a key-only list.

Story 1's per-run JSON format (`.forge/audits/runs/{run_id}.json`) has not yet
produced files on this machine (the orchestrator running the dogfood sprints is
a released RC that predates the writer). The spot-check therefore ran over the
**existing** audit substrate — the records the redaction pass will govern once
per-run JSON lands: `history.jsonl`, `forge_audit.yaml`, the
`run-*-sprint-audit.yaml` set, and the `diagnose-issue-*.yaml` set.

Method (reproducible):

```
# from the repo that holds real audit history (the main checkout)
python3 - <<'PY'
from pathlib import Path
import re
audit_dir = Path(".forge/audits")
files = [f for f in audit_dir.rglob("*") if f.is_file()]
env = Path(".forge/.env"); secrets = set()
if env.exists():
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        _, _, v = line.partition("="); v = v.strip().strip('"').strip("'")
        if len(v) >= 8: secrets.add(v)
tok = re.compile(r'(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{12,}'
                 r'|xox[baprs]-[A-Za-z0-9-]+|-----BEGIN [A-Z ]*PRIVATE KEY-----'
                 r'|AIza[0-9A-Za-z_-]{30,})')
env_leaks = tok_leaks = 0
for f in files:
    txt = f.read_text(errors="ignore")
    for s in secrets:
        if s in txt: env_leaks += 1; print("ENV LEAK:", f)
    for m in tok.finditer(txt): tok_leaks += 1; print("TOKEN LEAK:", f)
print("files:", len(files), "env-leaks:", env_leaks, "token-leaks:", tok_leaks)
PY
```

Result:

| Metric | Value |
|--------|-------|
| Audit files scanned | 42 |
| `.forge/.env` secret values loaded (≥8 chars) | 5 |
| Env-value leaks found | **0** |
| Secret-shaped token leaks found (`sk-`, `ghp_`, `AKIA…`, `xox…`, PEM, `AIza…`) | **0** |

The secret-shaped *strings* that key-name greps surface in these records
(`OPENAI_API_KEY not set` in captured log output, `token budget` in prose,
`flagged token as domain vocabulary`) are variable names and domain vocabulary,
not values. No credential material is present, so there is no gap to close.

The pass is additionally frozen as a **reproducible test** —
`tests/test_dogfood_git_policy_e2e.py::TestPerRunRedactionAtWrite` — which drives
the real per-run writer over a record carrying a secret-shaped key, a nested
`environment` dict, and an inline `.env` value, and asserts all three are
scrubbed before the bytes reach disk.

---

## 2. End-to-end runtime verification of the canonical template

**AC:** *At least one real sprint has run against the new template … to verify the
runtime guards and per-run files behave as expected end-to-end.*

The runtime behavior this AC targets — per-run audit and knowledge-summary files
travel with the repo, secrets/worktrees/logs/handoff stay local, tracked per-run
files collapse in PR diffs, and the Story 5 precondition guard blocks a tracked
catastrophic path — is exercised deterministically and reproducibly by
`tests/test_dogfood_git_policy_e2e.py` using the **real** code paths
(`cmd_init(--shared-memory)`, `_write_per_run_record`, `check_run_preconditions`)
and **real** `git` (`git ls-files`, `git check-attr`):

- `.forge/audits/runs/{run_id}.json` written by the real writer is **tracked**.
- `.forge/knowledge/summaries/{run_id}.yaml` is **tracked**.
- `.forge/.env`, `.forge/worktrees/**`, `.forge/logs/**`,
  `.forge/audits/history.jsonl`, `.forge/audits/index.sqlite`, and root
  `handoff.yaml` **stay local**.
- Both tracked per-run paths resolve `linguist-generated: true`.
- Clean template → no blockers; a force-tracked `.forge/worktrees/**` path → the
  guard blocks with the `git rm --cached` remediation.

---

## 3. The real-sprint step is an operator merge-gate

**Status: operator action, not a dev-agent deliverable.**

The AC's literal "a real sprint has run against the new template before merge" is
a *pre-merge operational verification the operator performs*, and it cannot be
discharged from inside the dev phase for two structural reasons:

1. **A dev agent must not run `forge sprint` / `forge run`.** Those commands spend
   money and require explicit per-invocation operator authorization. Producing a
   real per-run artifact requires executing one.

2. **A run's own per-run record cannot exist in its own dev-phase worktree.** By
   the per-run file contract (`docs/plans/forge-storage-layout.md` →
   "Per-run file contract"), a run record is written **exactly once, when the run
   terminates** — after DEV, REVIEW, and merge. So the artifact for *this* run is
   written after this dev phase ends, and no prior real per-run artifact exists
   because the format is newly rolled out (§1: the runs/ directory is empty
   everywhere on this machine). Requiring the dev-phase worktree to already
   contain a tracked per-run artifact from a completed real sprint asks for
   something the write-at-termination invariant makes impossible for the current
   run and unavailable for any prior run.

The honest resolution is **not** to synthesize a fake sprint artifact and commit
it (that would misrepresent an unrun sprint). It is to (a) prove the template's
runtime guarantees mechanically, as above, and (b) let the operator confirm the
live-sprint gate at merge time — the dogfood sprint that carries this branch to
green **is** that real sprint; its per-run record lands when it terminates, under
the template this branch installs.

**Operator merge-gate checklist:**

- [ ] The dogfood sprint carrying #796 completed against this branch's template.
- [ ] Its terminal per-run audit record (and any knowledge summary) landed under
      `.forge/audits/runs/` / `.forge/knowledge/summaries/` and is tracked.
- [ ] `forge run` precondition guards raised no blockers on the checkout.
