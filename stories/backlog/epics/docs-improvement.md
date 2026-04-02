---
name: Documentation improvement — from architecture to operator confidence
slug: docs-improvement
---

# Documentation Improvement Epic

Move docs from "here is the architecture and philosophy" to "I can install this,
understand its boundaries, run it, debug it, and trust it."

Optimizes for four things: time to first successful run, operator confidence,
debuggability, and decision clarity.

## Stories

All docs-only, no code dependencies — run as a single sprint.

Priority order (highest impact first, but all are independent):

1. [docs-readme-restructure](../docs-readme-restructure.md) — Restructure README around a landing funnel
2. [docs-terminology-consistency](../docs-terminology-consistency.md) — Audit and fix terminology drift
3. [docs-hello-forge-golden-path](../docs-hello-forge-golden-path.md) — Make hello-forge fully self-contained
4. [docs-troubleshooting-guide](../docs-troubleshooting-guide.md) — Add troubleshooting.md (highest-value missing doc)
5. [docs-first-run-walkthrough](../docs-first-run-walkthrough.md) — Narrated terminal transcript
6. [docs-cli-use-this-when](../docs-cli-use-this-when.md) — Opinionated "use this when" CLI guidance
7. [docs-runtime-artifacts](../docs-runtime-artifacts.md) — Filesystem layout + mental model
8. [docs-resume-semantics](../docs-resume-semantics.md) — Resume behavior matrix
9. [docs-provider-setup-chooser](../docs-provider-setup-chooser.md) — Provider setup decision guide
10. [docs-diagrams](../docs-diagrams.md) — Lifecycle, control boundaries, failure recovery diagrams
11. [docs-cross-linking](../docs-cross-linking.md) — Tighten navigation between all docs
12. [adaptive-assignment-docs-alignment](../adaptive-assignment-docs-alignment.md) — Reconcile shipped status and add a "how it works today" block

## If you only do five things

1. Add a killer 5-minute quickstart to the README (#1)
2. Make hello-forge fully self-contained and known-good (#3)
3. Add troubleshooting.md (#4)
4. Add "what gets created / where state lives" section (#7)
5. Add the coordinator-vs-models control-boundary diagram (#10)

## Doc tone target

Sound more like: "Here's how to succeed, and here's what happens when things break."
Sound less like: "Here is the elegant conceptual framework."

Source: external review by geeps (2026-03-21).
