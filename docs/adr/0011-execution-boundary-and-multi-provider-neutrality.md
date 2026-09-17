# ADR-0011: Execution Boundary and Multi-Provider Neutrality

- **Status:** Accepted
- **Date:** 2026-09-17
- **Deciders:** Peter Wickersham (project lead), with iterative review by Claude and Codex
- **Affected milestones:** v0.16.0 onward (bug sorting takes effect immediately); v1.0 (what is worth hardening before the surface freezes)
- **Related issues:** #3049 (tracking), #1846 (durable HITL resumption boundary for remote execution — a sub-decision under this ADR)
- **Related ADRs:** ADR-0004 (execution substrate — the July 2026 gh-aw evaluation; its *defer* verdict stands, its framing is superseded here), ADR-0005 (commit-centric review handoff — supplies the handoff unit), ADR-0007 (dev-phase verification capability — its verdict authority is retained, its local executor is execution machinery), ADR-0002 (audit substrate — the evidence bar an execution attempt must meet)
- **Related plan:** `docs/plans/restart-meta-plan-2026-07.md` (mission sentence: "SDLC policy layer, not an agent runtime")

---

## Context

The direction is already adopted. The July 2026 restart plan states that TheForge
is "the SDLC policy layer, not an agent runtime" and that agent loops, sandboxes
and dispatch infrastructure "should increasingly be someone else's problem."
ADR-0004 then evaluated one candidate substrate (gh-aw, in preview) and deferred
it, concluding that "replacing runners does not replace TheForge."

What the record never drew is the **boundary**. ADR-0004 §2 assigned story
lifecycle, DAG, gates, routing, budget policy and audit to the coordinator and
gave the substrate sandbox, credentials, capture and compute. That table treats
the sprint runner as a single unit on the keep side. In practice
`sprint/runner.py` and its siblings mix durable policy (which story runs, under
what ceiling, whether the result lands) with process machinery (daemonizing,
re-exec, locks, worktree provisioning, session supervision).

The gap became operational on 2026-09-14. A sprint surfaced four orchestration
defects at once, and the operator stated a prioritization rule: work that falls
in areas likely to be supplanted by external execution should not get development
effort unless it is immediately blocking. That rule was prioritization under
architectural uncertainty, not a change of product direction — but it was
recorded only in operator memory, and with no boundary in the repo it could not
be applied consistently. Two readings of "likely to be supplanted" sorted the
same four bugs differently.

The landscape has also moved since ADR-0004. Provider-operated harnesses
(Anthropic and OpenAI managed agents and agent SDKs), GitHub Agentic Workflows,
and graph runtimes such as LangGraph now offer durable sessions, checkpoint and
resume, isolation, parallel workers, and usage accounting — the same mechanics
behind a large share of recent forge defects. ADR-0004's single-product comparison
is no longer the right frame. The durable question is not "gh-aw or CLI runners"
but which semantics are TheForge's regardless of who executes.

## Decision

**TheForge stops supervising agent processes; it does not stop governing agent
work.**

### 1. The boundary is the execution attempt

The boundary is an interface, not a module. One **execution attempt** is a single
call to a harness adapter.

It receives:

- an immutable starting revision (base SHA),
- the task and role (dev, review, plan, diagnose),
- the selected provider and model,
- the requested budget and capabilities (tools, toolchain, network).

It returns:

- an immutable resulting revision (head SHA; the reviewable object is the commit
  range base..head, per ADR-0005 — a branch name is a mutable transport locator,
  not the handoff unit),
- a structured outcome (the handoff artifact),
- usage and cost as the harness measured them,
- evidence (capture sufficient for review verdicts and audit replay, per ADR-0002),
- a terminal status.

**Inside the attempt belongs to the harness:** agent and subagent loops and
within-attempt scheduling; sessions, session resume, retries, process
supervision and timeouts; workspace checkout, sandboxing and credentials; tool
execution and transcript parsing; provider-native accounting and telemetry.

**Outside the attempt remains TheForge:** readiness and refusal; DAG and story
scheduling; cross-provider selection and availability; review-cycle policy and
independent cross-provider review; evidence validation and normalization;
aggregate budget policy and unmeasured-spend refusal; story state, audit,
operator status, and landing authority.

The line therefore runs *through* `sprint/runner.py`, not around it. A
story-level retry after a review rejection is policy; resuming an agent session
inside one cycle is machinery. The gate *verdict* recorded as coordinator-owned is
policy; the process that runs the project's gate commands is machinery.

### 2. Multi-provider neutrality is non-negotiable

Provider-operated harnesses orchestrate their own models well and other
providers' models not at all. TheForge's routing across providers, review of one
provider's work by another, failover when a provider or credential is
unavailable, and normalization of cost, provenance and outcomes across harnesses
are wedge properties (restart plan: adaptive cross-provider routing is "refuted
as commodity").

Adopting any vendor's harness as an execution *backend* is compatible with this
ADR. Allowing any harness to become the governing *control plane* — the thing
that decides readiness, review requirements, trust, or landing — is not. The
immutable-commit handoff is what makes this workable: work produced inside one
harness can be reviewed inside another because the unit exchanged is a commit
range, not a session.

### 3. Adapters declare capabilities; policy decides sufficiency

TheForge owns budget *policy*, not necessarily budget *enforcement*. A harness
may support a hard mid-run dollar kill, a credit or token ceiling, or post-hoc
reporting only. The same holds for isolation strength, capture fidelity, and
cancellation.

Each adapter declares what it can enforce and what it can report. TheForge's
policy states the minimum it accepts for a given piece of work, and a mismatch is
a refusal with a legible reason — not a silent weakening. This replaces ADR-0004
§4's one-time "operator must accept the ceiling-only budget model" with a
standing per-adapter check.

### 4. The local harness is an adapter in maintenance-only status

Today's CLI runners, seatbelt sandbox, worktree lifecycle and sprint daemon are,
together, the **local harness adapter**. It is presently required: the live
adopter workload (Apple toolchains) has no hosted home today, and it is the
dogfood substrate. This ADR does not claim hosted execution can never serve those
workloads, and does not make local execution permanent architecture.

The local harness is **maintenance-only: blocking fixes and safety or integrity
fixes only.** Maintenance-only does not mean abandoned, and it does not mean
permanently local.

### 5. The execution-machinery backlog rule

Two tests classify any defect or proposed work:

1. **Would this mechanism exist behind every external harness adapter?** If not,
   it is execution machinery.
2. **Does this behavior affect TheForge's independent ability to decide
   readiness, trust, spend, review, or landing?** If yes, it is product work,
   whatever module it lives in.

Execution machinery defaults to **file and backlog, do not fix**, unless it
**blocks current work with no workaround, or violates a current safety or
integrity boundary** (credential exposure, repository corruption, discarded
work, untrustworthy evidence or cost records). Product work gets the normal floor
test. Everything is still captured through the intake pipeline; the rule changes
the milestone and the fix decision, never whether the defect is recorded.

When the two tests disagree — a mechanism that is local-only but whose failure
corrupts what forge believes about spend, evidence or landing — test 2 wins.

The operative statement of this rule lives in `CONVENTIONS.md`.

### Illustrative classification — the 2026-09-14 cases

| Defect | Test 1 | Test 2 | Disposition |
|---|---|---|---|
| Sprint re-exec drops per-story cost | local process supervision; would not exist behind an external adapter | the loss is in the re-exec mechanism, not in forge's cost policy | execution machinery → backlog unless blocking |
| RCA reports an unknown failure class | evidence interpretation exists behind every adapter | affects what forge learns from the record | product work → floor test |
| An advisory is rendered as a skip | story state and operator status exist behind every adapter | affects operator trust in story state | product work → floor test |
| Carried spend is unbounded | aggregate budget arithmetic exists behind every adapter | affects forge's ability to decide spend | product work → floor test |

The rule as first recorded in operator memory named all four categories —
re-exec, budget carry, RCA classes, status rendering — as backlog, because they
"live in the sprint runner." Under the interface rule, one does. That difference is the reason this ADR exists.

## Consequences

- **Bug sorting changes now.** Operators and agents classify by the two tests,
  not by module path. "It is in `sprint/`" is not an argument in either direction.
- **ADR-0004 stands as the historical gh-aw decision.** Its defer verdict and
  re-entry conditions for that product are unchanged. Its §2 ownership table is
  superseded by §1 here, and its §4 operator-acceptance gate is superseded by §3.
- **ADR-0007's verification authority is retained; its executor is not
  privileged.** Coordinator-owned gate verdicts pass test 2. Running the declared
  commands is inside the attempt, and a future adapter may run them elsewhere so
  long as the verdict remains one forge can trust independently.
- **No migration is scheduled by this ADR.** It defines the boundary and the
  sorting rule. Introducing a first-class adapter interface with capability
  declarations (ADR-0004 §2's `TransportSpec(kind="remote")` is one input), and
  trialing any specific external harness against this contract, are follow-on
  work: a spike per `CONVENTIONS.md`'s spike rule, evaluated against multiple
  substrates rather than one.
- **v1.0 hardening scope narrows.** Portability and unattended-trust work that
  lands outside the attempt is in scope. Hardening the local harness beyond
  maintenance-only is not, unless it blocks the dogfood or adopter loop.
- **#1846 (durable HITL resumption for remote execution) is a sub-decision** of
  this boundary and should be read against §1: HITL state is outside the attempt.
- **Risk: the rule becomes an excuse.** "Execution machinery" is a cheap label
  for anything unpleasant to fix. The safety-or-integrity exception and the
  test-2-wins tiebreak exist to stop that; a backlog disposition must name which
  test it failed.

## Alternatives considered

- **Amend ADR-0004 in place.** Rejected. ADR-0004 is a product evaluation with a
  dated evidence log. The boundary and the sorting rule are product-independent
  and would be buried in, and confused with, a gh-aw verdict.
- **Keep ADR-0004's module-level ownership table (sprint runner stays whole).**
  Rejected. It contradicts the adopted direction for the supervision half of the
  runner and gives no way to sort defects that straddle policy and mechanism.
- **Treat the whole sprint runner as soon-to-be-replaced.** Rejected. It would
  defer budget policy, story state and RCA defects that every future substrate
  still depends on, and it reads a prioritization rule as a product rewrite.
- **Adopt a single vendor harness as the control plane.** Rejected by §2. It
  forfeits cross-provider review and routing, which the restart plan identifies
  as wedge.
- **Leave the rule in operator memory.** Rejected by `CONVENTIONS.md` → "Capture
  converged decisions in repo-visible artifacts." The rule was misapplied within
  two days of being stated, by the agent holding the memory.
