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

- an immutable **input head** (the SHA the attempt starts from or evaluates),
- an optional immutable **comparison base** (required for review: the subject is
  the commit range comparison-base..input-head, per ADR-0005),
- the task and role (dev, review, plan, diagnose, verify),
- a **routing constraint**: the provider and model TheForge selected for the
  attempt, and the set of providers and models any nested agent may use (by
  default, only the selected one),
- the requested budget and capabilities (tools, toolchain, network).

It returns:

- an **output revision, role-dependent and optional**: a dev attempt returns an
  immutable output head descended from the input head, and the reviewable object
  is the range input-head..output-head; review, plan, diagnose and verify
  attempts normally return none and must not be read as an empty range,
- a structured outcome (handoff, verdict, plan, diagnosis, or verification result),
- usage and cost as the harness measured them, **attributed per agent**: the
  actual provider and model of every nested agent that ran, with its usage,
- evidence (capture sufficient for review verdicts and audit replay, per ADR-0002),
- a terminal status.

A branch name is a mutable transport locator, never the handoff unit. No attempt
may depend on a workspace left behind by another attempt; everything an attempt
needs is named by immutable revisions in its input.

**The terminal gate is its own attempt.** Running the project's declared gate
commands against a finished revision is a `verify` attempt: input head in;
per-command exit status, captured output and the environment identity it ran
under out; no output revision. TheForge turns that evidence into the gate
verdict. The verdict is never harness-reported.

**In-attempt verification is a different thing and is preserved.** ADR-0007 lets
a dev agent, mid-loop, request one of the project's declared whole commands and
receive the result back into the same loop. That runs against the attempt's own
uncommitted workspace, so it is not a `verify` attempt and does not cross the
boundary: it is a capability of the adapter hosting the attempt, and it does not
violate the no-shared-workspace rule because the workspace never leaves the
attempt. What stays TheForge's is the policy ADR-0007 fixed — the project
declares the commands, they are whole commands, the agent requests and never
executes — plus an audit record of every request. Its results inform the agent;
they are never the gate verdict, which comes only from the terminal `verify`
attempt. Whether an external harness offers this natively, or needs a
checkpoint-and-continuation protocol back to forge, is adapter design for the
follow-on spike, not decided here.

**Nested routing has an owner.** The harness owns subagent *loops*; TheForge owns
which providers and models those loops may use. Two obligations follow, and they
are not equally negotiable:

- **Enforcement is mandatory.** An adapter that can create nested agents and
  cannot confine them to the routing constraint is refused for any such attempt.
  This is not a capability policy may waive; waiving it would hand provider
  choice to the harness. An adapter that cannot create nested agents satisfies it
  trivially.
- **Observability is negotiable only when explicit.** An adapter that cannot
  report per-agent identity and usage must say so in the result, marking identity
  or usage as *aggregate* or *unmeasured* rather than omitting it. Policy then
  decides, under the same unmeasured-spend rules that govern any cost record.

An attempt whose result shows a model outside its constraint is untrustworthy
evidence, not a successful run.

**Inside the attempt belongs to the harness:** agent and subagent loops and
within-attempt scheduling; sessions, session resume, retries, process
supervision and timeouts; workspace checkout, sandboxing and credentials; tool
execution and transcript parsing; provider-native accounting and telemetry —
all within the routing constraint the attempt was given.

**Outside the attempt remains TheForge:** readiness and refusal; DAG and story
scheduling; cross-provider selection and availability; review-cycle policy and
independent cross-provider review; evidence validation and normalization;
aggregate budget policy and unmeasured-spend refusal; story state, audit,
operator status, and landing authority.

The line therefore runs *through* `sprint/runner.py`, not around it. A
story-level retry after a review rejection is policy; resuming an agent session
inside one cycle is machinery. The gate *verdict* recorded as coordinator-owned is
policy; the process that runs the project's gate commands is machinery, reached
through a `verify` attempt.

### 2. Multi-provider neutrality is non-negotiable

Vendor harnesses, however capable, do not provide an independently governed,
provider-neutral policy and evidence boundary: their model choice, economics,
telemetry and failure semantics are the vendor's. TheForge's routing across providers, review of one
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
a refusal with a legible reason — not a silent weakening. One item is not
policy's to waive: enforcement of the routing constraint on nested agents (§1). This replaces ADR-0004
§4's one-time "operator must accept the ceiling-only budget model" with a
standing per-adapter check.

### 4. The local harness is an adapter in maintenance-only status

Today's CLI runners, seatbelt sandbox, worktree lifecycle, and the sprint
process's daemonization, locks, re-exec and process supervision are, together, the
**local harness adapter**. The scheduling, story state and budget policy that the
same sprint process hosts are not part of it. It is presently required: the live
adopter workload (Apple toolchains) has no hosted home today, and it is the
dogfood substrate. This ADR does not claim hosted execution can never serve those
workloads, and does not make local execution permanent architecture.

The local harness is **maintenance-only: blocking fixes and safety or integrity
fixes only.** Maintenance-only does not mean abandoned, and it does not mean
permanently local.

### 5. The execution-machinery backlog rule

Two tests apply to any defect or proposed work. They are orthogonal: the first
classifies the *architecture*, the second can only change the *disposition*.
Neither reclassifies the other's answer.

1. **Classification — would this mechanism still need to exist in TheForge if
   every attempt ran through a conforming external harness?** Yes: it is product
   work. No: it is execution machinery. This answer alone decides whether the
   area is worth hardening.
2. **Override — does the defect corrupt or withhold something TheForge decides
   from (readiness, trust, spend, review, landing), or breach a safety boundary
   (credential exposure, repository corruption, discarded work)?** This never
   turns machinery into product. It only decides whether machinery gets fixed
   anyway.

| Classification | Override or blocking? | Disposition |
|---|---|---|
| Product work | n/a | normal floor test |
| Execution machinery | neither | file and backlog; do not fix |
| Execution machinery | blocks current work with no workaround, or test 2 is yes | fix now, **scoped to unblocking or restoring the record** — no hardening of the mechanism |

Everything is still captured through the intake pipeline; the rule changes the
milestone and the fix decision, never whether the defect is recorded. A backlog
disposition must state both answers.

The operative statement of this rule lives in `CONVENTIONS.md`.

### Illustrative classification — the 2026-09-14 cases

| Defect | Test 1: still needed in TheForge behind a conforming harness? | Test 2: corrupts what forge decides from, or breaches safety? | Disposition |
|---|---|---|---|
| Sprint re-exec drops per-story cost | no — re-exec is local process supervision | yes — the cost record forge decides spend from is wrong | execution machinery, override applies → fix now, scoped to restoring the cost record; re-exec itself is not hardened |
| RCA reports an unknown failure class | yes — evidence interpretation | n/a — already product | product work → floor test |
| An advisory is rendered as a skip | yes — story state and operator status | n/a — already product | product work → floor test |
| Carried spend is unbounded | yes — aggregate budget arithmetic | n/a — already product | product work → floor test |
| *(hypothetical)* Stuck detection fires early on a slow toolchain; the operator resumes and the story completes with its record intact | no — session supervision | no — nothing forge decides from is wrong | execution machinery → file and backlog |

The rule as first recorded in operator memory named all four real categories —
re-exec, budget carry, RCA classes, status rendering — as backlog, because they
"live in the sprint runner." Under the interface rule none of the four is a
routine backlog item: three are product work, and the fourth is machinery that
stays machinery but is fixed under the override because it corrupts a record
forge decides from. That difference is the reason this
ADR exists. It also shows the shape of a true backlog item: the mechanism is
local, a workaround exists, and every record forge relies on stays correct.

## Consequences

- **Bug sorting changes now.** Operators and agents classify by the two tests,
  not by module path. "It is in `sprint/`" is not an argument in either direction.
- **ADR-0004 stands as the historical gh-aw decision.** Its defer verdict and
  re-entry conditions for that product are unchanged. Its §2 ownership table is
  superseded by §1 here, and its §4 operator-acceptance gate is superseded by §3.
- **ADR-0007's verification authority is retained; its executor is not
  privileged.** Coordinator-owned gate verdicts are product. Running the terminal
  gate is a `verify` attempt (§1), and a future adapter may run it elsewhere so
  long as forge derives the verdict itself from the returned evidence. ADR-0007's
  in-attempt verification requests are preserved as an adapter capability under
  forge-owned policy (§1); they never produce the verdict.
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
  for anything unpleasant to fix. The override test exists to stop that, and a
  backlog disposition must state both answers. The opposite risk is scope creep
  through the override: a fix admitted by test 2 restores the record and stops.

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
