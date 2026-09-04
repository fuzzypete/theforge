# Docs index

Two kinds of documents live under `docs/`: **living** documents that describe
the system as it is, and **records** that preserve how it got here. Records
are deliberately never updated to match current code — they are evidence, not
guidance. Files outside the four living directories carry an explicit
`Status:` line (see the docs discipline note in [CONVENTIONS.md](../CONVENTIONS.md)).

## Living

Start with [architecture.md](architecture.md) for the whole-system picture,
then [vision.md](vision.md) for the philosophy and reading order.

- **[guides/](guides/)** — operator-facing how-to and reference:
  [getting started](guides/getting-started.md),
  [first-run walkthrough](guides/first-run-walkthrough.md),
  [CLI reference](guides/cli-reference.md),
  [inputs reference](guides/inputs-reference.md),
  [authoring](guides/authoring.md),
  [provider setup](guides/choose-your-provider-setup.md),
  [routing policy](guides/routing-policy.md),
  [adaptive assignment](guides/adaptive-assignment.md),
  [policy assertion provenance](guides/policy-provenance.md),
  [storage layout](guides/forge-storage.md),
  [troubleshooting](guides/troubleshooting.md),
  [controller runbook](guides/controller-runbook.md), and more.
- **[adr/](adr/)** — architecture decision records. Decisions with their
  evidence; corrections and as-built divergences are preserved inline rather
  than rewritten.
- **[reference/](reference/)** — mechanical contracts
  ([issue shape](reference/issue-shape.md),
  [bug shape](reference/bug-shape.md),
  [semantic readiness](reference/semantic-readiness.md),
  [preflight partial evidence](reference/preflight-partial-evidence.md)).
- **[vision/](vision/)** — doctrine
  ([refusal-capability](vision/refusal-capability.md),
  [compound-engineering](vision/compound-engineering.md)) plus three
  historical design captures that carry their own status banners.
- [routing-symmetry-followups.md](routing-symmetry-followups.md) — living
  catalogue of open routing asymmetries (#1389).
- [plans/knowledge-capture.md](plans/knowledge-capture.md) — living plan
  feeding the v0.14 knowledge milestone.
- [plans/forge-storage-layout.md](plans/forge-storage-layout.md) — shipped
  (v0.11.0) but still the design source of truth for `.forge/` storage;
  referenced from code.

## Records

- **[plans/](plans/)** — design plans; the shipped ones are marked
  `Status: Shipped … retained for historical context` in place.
- **[postmortems/](postmortems/)** — sprint postmortems from the v0.5.0 era.
- **[post-release-reviews/](post-release-reviews/)** — doc-review and
  retrospective records filed at each release.
- [memory-migration.md](memory-migration.md) — one-time migration audit
  (referenced by tests; stays at this path).
- [v0.13-dogfood-readiness.md](v0.13-dogfood-readiness.md) — evidence record
  for issue #1869, current release cycle.
- [plans/2348-structural-decay-observer-spike.md](plans/2348-structural-decay-observer-spike.md)
  — spike record for issue #2348: design, POC and the **defer** decision for a
  codebase-scoped structural-decay observer, with the data condition that
  reopens it.
- [plans/2112-plan-advisory-resolution.md](plans/2112-plan-advisory-resolution.md)
  — measurement record for issue #2112: how often dev resolves an advisory
  plan-review finding, broken out by finding class, with the four escapes named
  and plan-review cost as a fraction of the story it guarded. Reproduce with
  `forge audits plan-advisory`.
- [plans/1848-reviewer-finding-fate-spike.md](plans/1848-reviewer-finding-fate-spike.md)
  — spike record for issue #1848: finding-fate derivation from structured
  code-review records, the live Thursday, August 20, 2026 POC output, and the
  **stay out** decision pending two missing structured events.
- **[archive/](archive/)** — closed records with no living role:
  - [coordinator-refactor-plan.md](archive/coordinator-refactor-plan.md) —
    2026-03 plan for the coordinator split (since shipped).
  - [issue-796-dogfood-git-policy-verification.md](archive/issue-796-dogfood-git-policy-verification.md)
    and
    [issue-976-score-to-band-routing-audit.md](archive/issue-976-score-to-band-routing-audit.md)
    — point-in-time verification/audit companions to closed issues.
  - [2026-03-restart-era/](archive/2026-03-restart-era/) — the pre-GitHub-issues
    repo-root `discovery/`, `plans/`, and `stories/` directories, frozen as
    they stood in March–April 2026.
