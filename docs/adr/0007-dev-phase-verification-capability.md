# ADR-0007: Dev-Phase Verification Capability

- **Status:** Accepted — **coordinator-mediated verification** (option A); decided 2026-07-29
- **Date:** 2026-07-29
- **Deciders:** Peter Wickersham (project lead)
- **Affected milestones:** v0.13 (unblocks adopter dogfood on non-Python toolchains), v0.14+ (capability model)
- **Related issues:** #2050 (the nesting collision — what actually blocks an Apple app-target build), #2038 (capability model is not project-extensible), #2051 (review blocks on build evidence the run cannot produce), #2029 (a capability gap never surfaces as the actionable cause), #1944 (closed — forge owns the gate), #1947 (closed — the `xcode` preset)
- **Related documents:** ADR-0004 §9 (`0004-execution-substrate.md`) — records this ceiling as field evidence; `docs/vision/refusal-capability.md`

---

## Context

TheForge confines the dev agent with a host sandbox — `sandbox-exec` on macOS,
`bwrap` on Linux — so an agent's writes stay inside its worktree. A project that
needs more can select a forge-owned capability preset by name
(`sandbox.capability_profile: xcode`), which widens write roots and mach
services without ever granting `allow default`.

Adopter `hdp` (Swift/iOS/watchOS) exercised that path against a real Apple
toolchain and found the model's floor. With the `xcode` preset selected and
applied — logged on every dev invocation as `sandbox capability profile: xcode
(6 extra write roots, 4 mach services)` — the dev agent still could not build or
test the app target. The cause is not an allowlist gap. SwiftPM compiles package
manifests by invoking `sandbox-exec` itself, and macOS refuses to apply a
sandbox inside a `sandbox-exec` confinement: `sandbox_apply: Operation not
permitted`. Apple's documented workaround (`defaults write com.apple.dt.Xcode
IDEPackageSupportDisableManifestSandbox`) is denied in turn, because
`~/Library/Preferences` is not a granted write root.

The dev agent recorded the whole thing in its own handoff (run `5096bd4a46e3`,
story `issue-248`), including that the failure persists with the agent sandbox
both enabled *and* disabled, with package resolution fully cached and
`-disableAutomaticPackageResolution -onlyUsePackageVersionsFromResolvedFile
-skipPackageUpdates` in force. Network was never reached.

**No preset content and no extension to the preset model changes this.** A
toolchain that invokes `sandbox-exec` cannot run inside a `sandbox-exec`
confinement. That is why #2038 was narrowed and #2050 split out of it.

The cost of leaving it unresolved is measured. Run `0ea58a758f52` (story
`issue-230`): six dev iterations, $46.40, six consecutive gate failures on Swift
compile errors, every iteration derived from gate traces rather than from
running the code. Run `70462a5b72a0` (story `#248`): three iterations, $10.03,
`REQUEST_CHANGES` on two findings that both said the watch target was never
built — findings the reviewer was right about and the dev could not act on.

## The asymmetry this ADR exists to resolve

TheForge has already decided that *authoritative verification runs outside the
dev sandbox*. #1944 established it, and the dev prompt states it to the agent
verbatim:

> Do NOT run the full gate command. Gate execution is coordinator-owned: after
> you complete, the coordinator runs the authoritative gate itself, **outside
> your sandbox** — running it yourself wastes time and can fail on
> sandbox-restricted operations the coordinator's run does not hit.

`coordinator/gate.py` contains no sandbox wrapper of any kind; the gate is a
plain subprocess in the worktree.

What that decision left behind is the dev's *own* feedback loop. The same prompt
tells the agent to "use targeted tests for your own feedback while developing" —
and on a project whose targeted tests are `xcodebuild` or `swift test`, that
instruction is unsatisfiable for exactly the reason the gate was moved out in
the first place. The dev is asked to verify its work with a toolchain the
confinement denies it.

So the boundary is not "agents are confined and forge is not." It is: **forge
already runs project-authored shell outside the sandbox, and only the dev's
ability to check its own work before submitting is confined.** Where a project's
inner loop needs the same capabilities as its gate, the iteration budget stops
bounding unverified proposals and starts funding them.

### What this reframes about the trust objection

The natural objection to letting the dev reach its toolchain is that build
systems execute repository files the dev agent can edit — `Package.swift`,
`project.pbxproj` run-script phases, the `Makefile` itself — so trust in a
toolchain *binary* does not transfer to an invocation whose *inputs* the agent
authored.

That objection is correct, and it applies to the gate today. `make gate` runs a
Makefile the dev agent was free to modify, unconfined, on every story. The
exposure already exists and forge already accepted it when it took ownership of
the gate.

What is genuinely at stake is therefore narrower than "should agent-adjacent
code run unconfined": it is **how often** that happens, and **who chooses the
command**. Those two questions separate the options below, and they are the
questions the decision should turn on — not a first-principles argument about
confinement that the gate already settled.

### Why the confinement cannot be selectively lifted

One shape is worth ruling out before the options, because it is the intuitive
one: *let the project allowlist specific commands that the agent may run
unconfined.* That is not expressible.

Forge launches the agent as `["sandbox-exec", "-p", profile, *cmd]`
(`runners/sandbox.py:549`; the Linux path wraps in `bwrap` equivalently). Seatbelt
confinement is a property of the **process tree**, not of the executable: every
descendant inherits it, and a profile can widen file and mach access but carries
no construct that un-confines a child. A binary named in an allowlist would
still execute inside the confinement, and SwiftPM's nested `sandbox-exec` would
still fail for exactly the reason it fails today.

It follows that **anything which runs unconfined must be launched by a process
outside the confinement** — which, in this architecture, means the coordinator.
The agent cannot be granted access to unconfined execution; it can only be
granted the ability to *request* it. Every viable option below is therefore a
form of mediation, and the design space is narrower than it first appears: what
remains open is the **unit of declaration** and the **request ergonomics**, not
whether mediation happens.

## Options

### A. Coordinator-mediated verification (recommended)

The dev agent never gains unconfined execution. It *requests* verification, and
the coordinator runs a project-declared command outside the sandbox — the same
path the gate already uses — and returns the output into the dev loop.

- Exposure is identical in kind to today's gate, and identical in mechanism.
- **The declared unit is a whole command, not a binary.** A project declares
  named verification commands (`verify-watch: xcodebuild -scheme … test`), and
  the agent requests one *by name*. This is the load-bearing detail: allowlisting
  the binary `xcodebuild` and letting the agent supply argv would hand it an
  arbitrary-argument invocation over inputs it authored — `Package.swift`,
  `project.pbxproj` run-script phases — which is the exposure the trust
  objection is actually about. Fixing the command in config removes agent
  control over argv while leaving the agent free to choose *which* declared
  check to run.
- The command set is declared in `forge.yaml`, so the *project* chooses what may
  run; the agent chooses only which declared check to ask for. Frequency is
  bounded by coordinator policy (per-iteration cap, budget), not by agent
  discretion.
- Every invocation is coordinator-initiated and therefore auditable by
  construction, which the audit substrate can record without a new trust class.
- Generalizes past this toolchain: any project whose inner loop the sandbox
  cannot host gets the same seam, with no new preset and no host-specific work.
- Cost: latency per request, and the dev's inner loop is slower than native
  execution. A design must bound request count so this does not become a
  per-token gate run.

### B. Transparent interception (a UX variant of A, not a separate trust model)

The agent invokes `xcodebuild` as it normally would; a shim earlier on `PATH`
forwards the invocation to the coordinator, which executes it outside the
confinement and returns the result.

- Per §"Why the confinement cannot be selectively lifted", this is **not** an
  alternative trust model — the execution is still coordinator-mediated. It
  differs from A only in ergonomics: the agent needs no new protocol and its
  inner loop reads as native.
- The cost is that interception hands argv back to the agent, which is precisely
  what A's whole-command declaration removes. It can be narrowed (validate argv
  against a declared template) but every such narrowing converges on A with a
  worse failure mode: a shim that silently declines looks to the agent like a
  broken toolchain rather than a refused request.
- Viable as a **later ergonomic layer over A**, once the mediated path exists and
  the argv policy is settled. Not viable as the first increment.

### C. Make the capability model project-extensible (#2038)

Let a project add write roots and mach services the shipped presets do not
anticipate.

- **This does not unblock hdp**, and the ADR should be explicit about that:
  nesting is not an allowlist problem.
- It remains independently worth doing — the set of things a real toolchain
  needs is not knowable from inside this repository — and it is what #2038 now
  tracks after being narrowed. It is not on the critical path here.

### D. Host-level isolation disjointness (macOS virtualization)

Give the dev a genuinely separate machine boundary rather than a nested one.

- The only option that removes the collision rather than routing around it, and
  it would also resolve the process-supervision self-reference measured on
  2026-07-25 (#1944 amplifier, patient-gate nesting).
- Large lift. ADR-0004 §9 already records that containerizing this class of work
  means macOS virtualization, not containers. **Defer**, with re-entry when the
  maintenance cost of the seatbelt path exceeds the cost of a VM substrate.

## Decision

**The project defines unconfined commands that TheForge can run, and the
coordinator gives the agent access to request that these be run on its behalf.**

Three properties are load-bearing in that sentence, and a design that drops any
of them is not this decision:

1. **The project defines them.** Not forge (as with presets), and not the agent.
   The declaration lives in `forge.yaml` and is reviewable like any other
   project configuration.
2. **They are whole commands.** Allowlisting a binary and letting the agent
   supply argv would hand it an arbitrary invocation over inputs it authored —
   `Package.swift`, `project.pbxproj` run-script phases, the `Makefile`. Fixing
   the command in config removes agent control over argv while leaving the agent
   free to choose *which* declared check to run.
3. **The agent requests; the coordinator runs.** The agent's granted capability
   is the request, never the execution. Per §"Why the confinement cannot be
   selectively lifted" this is not a preference — seatbelt admits no other
   arrangement — and it is what keeps every unconfined invocation
   coordinator-initiated, policy-bounded, and auditable by construction.

Adopt **A** on that basis. Treat **B** as a possible later ergonomic layer over
A, not as a first increment and not as a separate decision. Pursue **C**
independently as a smaller, unrelated improvement that does not unblock this.
Defer **D** with the re-entry condition above.

## Out of scope

- A network-egress axis for the capability model. The observed failures never
  reached the network — package resolution failed against fully-cached,
  already-resolved checkouts — so egress is separable and unevidenced here.
- gh-aw re-entry. ADR-0004 §9 already states this class of toolchain could not
  be hosted on its container stack either; nothing in this ADR is a re-entry
  condition for that decision.
- The shape of the request channel itself (tool call, sentinel file, structured
  handoff). That is implementation design for whichever option is adopted.

## Consequences

If A is adopted:

- #2050 becomes implementable against a concrete design rather than requiring a
  dev agent to invent the substrate model.
- #2051 largely dissolves: review blocks on build evidence the run could not
  produce, and under A the run can produce it.
- #2029's remaining content narrows to the RCA classification gap, which is
  where its landed diagnosis already places it.
- #2038 stays open on its own merits and stops being read as the hdp blocker.
- ADR-0004 §9's factual characterisation of the SwiftPM denial as a
  network-egress limitation is wrong and should be corrected regardless of which
  option is adopted; its *verdict* is unaffected.

If no option is adopted, adopter dogfooding on any toolchain the seatbelt model
cannot host stays blocked, and the iteration budget continues to fund unverified
proposals — which is the failure mode the budget exists to bound, not to fund.

## References

- #2050 — the nesting collision, with the dev's own handoff as evidence
- #2038 — capability model not project-extensible (narrowed; option C)
- #2051 — review blocks on unproducible build evidence
- #2029 — capability gap never surfaced as the actionable cause
- #1944 (closed) — forge owns the gate; the precedent this ADR extends
- #1947 (closed) — the `xcode` preset; `config/sandbox_capabilities.py`
- ADR-0004 §9 — the incumbent isolation model's ceiling as field evidence
- `docs/vision/refusal-capability.md` — an agent that cannot verify its own work
  cannot know when it is not ready
