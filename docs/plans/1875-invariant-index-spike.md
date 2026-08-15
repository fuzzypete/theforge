# Invariant index spike: authoritative project invariants as selective context

**Issue:** #1875 · **Status:** spike complete, adoption decision below ·
**Depends on:** #1860 (prior-run context assembly), #1866, #1867

## What this spike asked

After the v0.14/v0.15 knowledge feed-forward loop, TheForge can put *prior-run
lessons* into future context. This spike asks the next question: can **stable
project invariants** — the rules a project already wrote down and already
believes — be extracted, indexed, and selectively injected in a way that reduces
churn (plan regenerations, review cycles, restated findings)?

The dangerous version of this feature is obvious, so the spike was built to make
it structurally impossible:

- an `invariants.yaml` that restates project rules becomes a **second source of
  truth** that drifts from the doc it paraphrased; and
- narrow scope selection produces **correlated misses** — the plan agent and the
  reviewer both blind to the same rule, because the same bad scope tag decided
  for both.

## The annotation convention

A project marks a rule *inside the document that already owns it* — an ADR,
`CONVENTIONS.md`, a policy doc, anything Markdown:

```md
<!-- forge-invariant id="summaries-advisory"
     scope="area:audit phase:plan,dev,review files:src/theforge/knowledge_*.py"
     enforcement="review" -->
LLM-generated summaries advise agents; they never drive coordinator control flow.
<!-- /forge-invariant -->
```

| Attribute | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Stable identity, `[a-z0-9][a-z0-9._-]*`. Duplicates across sources are a diagnostic; the first wins. |
| `scope` | no | Whitespace-separated `key:value` tokens. Known keys: `area`, `phase`, `files` (comma-separated values). |
| `enforcement` | no | `advisory` (default), `review`, or `gate`. Declares how the project enforces the rule; TheForge uses it only for ranking. |

A marker inside a fenced code block is ignored. An example shown while
*documenting* the convention is a marker about the convention, not an assertion
of a rule — without that exemption this very document would file its own
illustrations as live invariants, duplicate-id diagnostics and all.

The convention is **portable**: nothing in it names TheForge's own doc culture.
A target project points `knowledge.invariant_sources` at whatever Markdown it
keeps its rules in.

Configuration is source locations only — never invariant prose:

```yaml
knowledge:
  invariant_context: false      # off by default
  invariant_sources:
    - "**/*.md"                 # the default
```

The default is deliberately **every Markdown file in the repository**. A default
naming `docs/` would presume one project's layout, and the whole point is that
this works on a target project whose rules live in `handbook/`, `rfcs/`, or the
repository root. Discovery skips hidden directories and the usual vendored/build
names (`node_modules`, `vendor`, `build`, `dist`, …), and only files containing
the literal `forge-invariant` are parsed at all, so the broad default costs a
substring scan. Projects with a settled documentation layout can narrow it.

## The derived index

`forge index --invariants` writes `.forge/knowledge/invariants/index.yaml`.
It is derived, rebuildable, disposable, and gitignored — the same status as
`.forge/knowledge/index.yaml`.

Each entry records **provenance and applicability metadata only**:

| Field | Purpose |
| --- | --- |
| `id`, `source_path`, `source_anchor` | Identity and where the rule actually lives |
| `start_line` / `end_line` / `body_*_line` | The span consumers re-read at render time |
| `scope_raw`, `scope`, `applicability` | Declared scope verbatim, parsed, plus `scope_completeness` and any `unparsed_scope_keys` |
| `enforcement` | The project's declared enforcement level |
| `source_digest` | SHA-256 of the marked body, for staleness reporting |

The index deliberately **does not copy the rule text**. Consumers re-read the
source document. That is what keeps the ADR authoritative and the index merely
useful: an index that drifts is *visibly* stale (digest mismatch, reported in
the manifest) rather than quietly wrong.

Extraction is deterministic and stdlib-light. Every malformed marker —
unterminated block, missing/invalid id, unknown enforcement, unparsed scope key,
empty body, duplicate id, unreadable file — becomes a diagnostic printed by the
command, never an exception. One broken annotation does not cost a project its
index.

## Selection and the conservative fallback

`ContextAssembler` consults `task/invariant_selector.py` only when
`knowledge.invariant_context` is on. Disabled means the index is never read at
all, so no marked prose can reach a prompt by any later path.

**Preflight is excluded outright.** Preflight's output (sufficiency, complexity,
likely files, refusal) drives coordinator control flow, so it may not receive
invariant prose — the same ADR-0002 clause 5 boundary that governs prior-run
summaries. The preflight manifest still records the decision, so the exclusion
is auditable rather than invisible.

The concrete confidence triggers:

| Declared scope | File list known? | Decision |
| --- | --- | --- |
| `full` (file globs) | yes, a glob matches | **high** → capsule (the marked region only) |
| `full` (file globs) | yes, no glob matches | **high** → the one confident drop (`files_out_of_scope`) |
| `full` (file globs) | no | **low** → broad source section |
| `partial` (areas only) | area token found in a touched path or the story | **high** → capsule |
| `partial` (areas only) | not found | **low** → broad source section |
| `none` | — | **low** → broad source section |
| any, with `unparsed_scope_keys` | — | **low** → broad source section |

"Broad" means the **enclosing Markdown section** — from the nearest heading
through the next heading of the same or higher level, bounded at 120 lines with
a pointer to the file for the remainder. Bounded, but strictly more than the
marked lines: the surrounding paragraphs a human would read to understand the
rule.

Uncertainty therefore *widens*. The only narrowing a low-confidence path can
produce is nothing at all.

### Review is deliberately broader than plan/dev

Review always renders source sections, and — the part that matters — review does
**not** apply the confident `files_out_of_scope` drop. A rule that plan/dev
narrowed away still reaches the reviewer, tagged
`broad_phase_override(files_out_of_scope(...))`.

This is the asymmetry the story asks for: a bad scope tag can cost the producer
a rule without also blinding the reviewer who would catch the resulting mistake.
It costs review tokens, and that is the intended trade for the length of the
proof.

## What the manifest shows

Every assembly records `invariant_context` in the audit record under
`context_manifests[]`:

```yaml
invariant_context:
  enabled: true
  phase: dev
  selection_mode: selective
  included:  [{id, source_path, source_anchor, enforcement, rendering_mode,
               scope_confidence, reason, score, source_digest_matches}]
  dropped:   [{id, source_path, reason}]      # files_out_of_scope | phase_not_applicable | budget_pressure
  uncertain: [...]                            # the subset of `included` that widened
  note: "2 of 4 indexed invariants included; 1 included as full source sections
         because scope confidence was low"
```

`uncertain` is the field the adoption decision hangs on. It is not a failure
count — it is the measure of how much of the proof was carried by the fallback
rather than by real scope matching.

## Measurement

`forge knowledge-report` gains an **invariant-context proof** section
(`knowledge_invariant_proof.py`, rendered into both the terminal view and the
structured payload under `invariant_context_proof`).

Cohorts come from the manifest, never from config: included-something is
treatment, enabled-but-nothing-included is a genuine control, disabled or absent
is unclassified. Preflight manifests never classify.

It reports **four churn metrics only** — plan regeneration rate, review
restated-finding rate, average dev iterations, average review cycles — reusing
the existing `knowledge_effectiveness` cohort machinery. Cost per story and
stories per dollar are **deliberately excluded from the churn comparison**:
the story's success claim is churn reduction, and prompt-size or spend movement
must not be able to carry the verdict. Token and cost movement remain visible in
the surrounding knowledge report as secondary telemetry.

Below three runs per cohort the section reports `insufficient_data` with the
counts that produced it. That is the expected output today, and saying so is the
point — a spike that reports a number off two runs has proved nothing.

## Adoption decision

**Revise and continue as opt-in proof machinery. Do not promote to a default,
and do not claim a benefit yet.**

What the spike settled:

- The **derived-metadata shape works.** Storing provenance rather than prose
  removes the second-source-of-truth risk entirely, and re-reading at render
  time makes the source document authoritative by construction rather than by
  convention.
- The **conservative fallback is cheap to state and cheap to verify.** Every
  low-confidence path widens; the single narrowing path requires an explicit
  project-declared file glob *and* a known file list.
- The **review asymmetry is implementable** without a second selection engine —
  one flag on the same selector.

What it did **not** settle, and what would decide promotion:

1. **No churn evidence exists yet.** Zero runs have executed with
   `invariant_context: true`. The proof section will report `insufficient_data`
   until at least three runs per cohort accumulate. Until then the honest claim
   is "the mechanism exists and is measurable", not "it helps".
2. **`uncertain_share` is the real risk.** If most inclusions arrive through the
   fallback, this feature is a verbose way to paste documentation into prompts.
   Marked-scope quality, not selection cleverness, is what would fix that — and
   that is a cost borne by the adopting project, not by TheForge.
3. **Marking discipline is unproven at scale.** Four markers exist in this
   repository. Whether a project keeps `scope` tags accurate as code moves is
   the same maintenance question that makes most annotation conventions rot.
   The digest-mismatch signal in the manifest is the early-warning surface for
   this; it has never fired in anger.

Concrete next step before any promotion: run a bounded cohort (≥3 runs each
side, matched work type and complexity) with `invariant_context: true`, then
read the proof section. Drop the feature if `uncertain_share` stays high while
churn does not move; keep it opt-in either way until it does.

## Non-goals honoured

- No LLM-authored invariant is authoritative — extraction is pure Python and
  reads only human-written markers.
- No standalone `invariants.yaml` restating rules — the index is metadata over
  spans in the project's own docs.
- No knowledge graph or ERD generation.
- No mechanical coordinator decision reads invariant prose — the coordinator
  reads only the manifest's counts, and only for reporting.

## References

- `src/theforge/invariant_index.py` — extractor and derived index
- `src/theforge/task/invariant_selector.py` — selection, confidence, rendering
- `src/theforge/task/invariant_manifest.py` — audit-visible decision record
- `src/theforge/knowledge_invariant_proof.py` — churn proof
- `docs/adr/0002-audit-substrate-and-queryable-run-history.md` clause 5
- `docs/plans/knowledge-capture.md` (sibling: prior-run knowledge)
- `docs/plans/forge-storage-layout.md` (derived-artifact status)
