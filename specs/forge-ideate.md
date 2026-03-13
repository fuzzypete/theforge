---
name: "forge ideate: multi-LLM deliberation for spec generation"
slug: forge-ideate
file_scope:
  - src/theforge/ideate.py
  - src/theforge/cli.py
  - src/theforge/coordinator.py
  - tests/test_ideate.py
pytest_target: tests/
---

# forge ideate: Multi-LLM Deliberation for Spec Generation

## Problem

Multi-model value is inversely correlated with spec clarity. At the DEV
stage the spec is clear and a single strong model is optimal. At the REVIEW
stage the work is concrete and independent blind-spot coverage is the value.
But at ideation — when the problem is ambiguous and the solution space is
open — this is where multi-model deliberation produces the most leverage.

Currently there is no first-class way to run the deliberation protocol
before a spec exists. The operator must manually prompt each LLM, collate
the outputs, identify convergence, and synthesize a spec themselves. This
is exactly the kind of orchestration busywork TheForge should eliminate.

## Design

### The deliberation protocol

```
Phase 1: Independent generation
    Each model in the pool receives only the brief (no cross-contamination).
    Each independently produces structured ideas, constraints, and risks.

Phase 2: Cross-review
    Each model receives the Phase 1 outputs from all other models plus its
    own, and produces a structured critique identifying agreements,
    disagreements, and blind spots.
    This is structurally identical to the existing review pool — the
    coordinator fans out the same prompt+context to all models.

Phase 3: Convergence detection + synthesis
    The synthesis model (Opus) reads all Phase 1 outputs and all Phase 2
    cross-reviews. It:
      - Identifies converged items (same conclusion in N of M models)
      - Flags divergent items (genuine disagreement)
      - For divergent items with a second pass available, loops back to
        Phase 1 with the disagreement framed as a narrower question
      - Emits a final structured spec or a human-decision brief if
        residual divergence remains after max iterations

Phase 4: Human review of synthesized spec
    The operator reviews the draft spec before any forge run is invoked.
    This is the only mandatory human gate in the ideation flow.
```

### Relationship to existing architecture

Phase 2 of the deliberation protocol IS the existing review pool — the
coordinator fans out to each model and gathers independent outputs. The
difference from code review is:

- Input: a brief (text) instead of a diff
- Output: structured ideas/constraints instead of APPROVE/REQUEST_CHANGES
- Convergence signal: same conclusion in N/M instead of unanimous APPROVE
- Iteration target: the brief/framing instead of the implementation

IDEATE is DECOMPOSE done right. When the question is "what sub-problems
should this spec address," running the deliberation protocol on that
question produces a spec list that the campaign runner can execute. The
coordinator remains fully deterministic — only the ideation agents are LLMs.

---

## Requirements

### R1: New module `src/theforge/ideate.py`

Contains all ideation-specific logic:

```python
@dataclass(frozen=True)
class IdeationRound:
    """One full Phase 1 + Phase 2 cycle."""
    round_number: int
    phase1_outputs: dict[str, str]   # model_name → output
    phase2_outputs: dict[str, str]   # model_name → cross-review output
    converged_items: list[str]       # items where N/M models agreed
    divergent_items: list[str]       # items still in disagreement
    synthesis_output: str            # full synthesis text

@dataclass
class IdeationResult:
    success: bool
    spec_path: Path | None           # written spec file, or None if human-decision needed
    rounds: list[IdeationRound]
    final_synthesis: str             # final synthesized spec text
    residual_divergence: list[str]   # items needing human executive decision
    total_cost_usd: float
    human_decision_required: bool
```

#### `run_ideation(config, brief, output_path, *, max_rounds=2) -> IdeationResult`

Executes the full deliberation protocol:

1. **Phase 1**: Fan out `_build_phase1_prompt(brief)` to all models in
   `config.review_pool` using `run_agent_pool()`. Each model generates
   independently (no cross-contamination).

2. **Phase 2**: Fan out `_build_phase2_prompt(brief, phase1_outputs)` to
   all models. Each model receives its own Phase 1 output plus all others.
   Uses `run_agent_pool()` again.

3. **Synthesis**: Run `config.synthesis_profile` (or `review_pool[0]` if no
   synthesis profile) with `_build_synthesis_prompt(brief, phase1_outputs,
   phase2_outputs)`. The synthesis model produces:
   - A list of converged items (clearly agreed by majority)
   - A list of divergent items (genuine disagreement)
   - A draft spec in the standard frontmatter format

4. **Convergence check**: If `divergent_items` is non-empty and
   `round_number < max_rounds`, loop back to Phase 1 with the brief
   narrowed to the divergent items only.

5. **Output**: Write the synthesized spec to `output_path`. If residual
   divergence remains after max rounds, write the spec with a
   `## Human Decisions Required` section listing the unresolved items.

#### Convergence detection

Convergence is approximate — the synthesis model identifies it, not a
mechanical string-match. The synthesis prompt instructs it to:

> "For each idea or constraint that appeared in Phase 1 outputs, assess
> whether it was agreed by 2 or more models. List converged items as bullet
> points. List items where models genuinely disagreed as divergent items.
> Be conservative: if in doubt, mark as divergent."

This keeps convergence detection in the LLM (where it belongs — semantic
similarity is hard mechanically) while keeping the process control in the
coordinator.

### R2: Prompt builders in `ideate.py`

#### `_build_phase1_prompt(brief: str) -> str`

```
You are participating in a structured ideation process.

BRIEF:
{brief}

Your task: Independently produce your best thinking on this brief.
Do NOT hedge or hold back ideas because another model might disagree.

Structure your response as:
## Core Ideas
[3-7 concrete ideas or approaches]

## Key Constraints
[constraints the solution must satisfy]

## Risks and Blind Spots
[what could go wrong, what this approach might miss]

## Recommended Approach
[your single strongest recommendation]
```

#### `_build_phase2_prompt(brief: str, phase1_outputs: dict[str, str]) -> str`

Includes the brief plus all Phase 1 outputs (labelled by model name),
then:

```
Your task: Cross-review the above outputs.

Structure your response as:
## Agreements
[items where you and other models converged]

## Disagreements
[items where models genuinely differed — describe the tension]

## Blind Spots Identified
[things no model addressed that should have been]

## Your Updated Position
[how (if at all) seeing other models' outputs changed your thinking]
```

#### `_build_synthesis_prompt(brief, phase1_outputs, phase2_outputs) -> str`

Includes all Phase 1 and Phase 2 outputs, then instructs the synthesis
model to produce a draft spec in the standard TheForge frontmatter format:

```
---
name: "<derived from brief>"
slug: "<kebab-case>"
file_scope: []
pytest_target: tests/
---

# <title>

## Problem
...

## Requirements
...

## Acceptance Criteria
- [ ] ...

## Human Decisions Required
[list any items that remained divergent after deliberation]
```

The synthesis model must output valid YAML frontmatter followed by
markdown. The coordinator validates that frontmatter parses.

### R3: `forge ideate` CLI subcommand

```
forge ideate <brief-or-file> [--output specs/<slug>.md] [--rounds N] [--dry-run]
```

- `<brief-or-file>`: either a short brief string or a path to a `.md`/`.txt`
  file containing the brief
- `--output`: where to write the generated spec (default:
  `specs/<synthesized-slug>.md`)
- `--rounds`: max deliberation rounds before surfacing residual divergence
  (default: 2; max: 3)
- `--dry-run`: run deliberation and print the synthesized spec to stdout
  without writing a file

If the pool has only one model (no synthesis), `forge ideate` runs a
single-model spec-generation pass (Phase 1 only, no cross-review). It
still produces a valid spec but without the deliberation benefit.

If the pool has no synthesis profile configured and `len(review_pool) > 1`,
raise an error: synthesis is required for the cross-review consolidation step.

### R4: Output to `forge audit` and logging

Log progress as it happens:

```
[forge] ▸ IDEATE   opus+gpt-5.4+gemini-2.5-pro  round=1
[forge]   ▸ Phase 1   generating independently...
[forge]   ↳ opus done (45s)
[forge]   ↳ codex done (120s)
[forge]   ↳ gemini done (30s)
[forge]   ▸ Phase 2   cross-reviewing...
[forge]   ↳ opus done (38s)
[forge]   ↳ codex done (95s)
[forge]   ↳ gemini done (28s)
[forge]   ▸ Synthesis   consolidating...
[forge]   ↳ synthesis done (22s)
[forge]   Converged: 4 items  Divergent: 2 items
[forge] ▸ IDEATE   round=2   (2 divergent items remain)
...
[forge] ✓ IDEATE   spec written: specs/my-feature.md  $0.84  4m 12s
[forge] ⚠ 1 item requires human decision — see ## Human Decisions Required
```

### R5: Tests in `tests/test_ideate.py`

- `test_phase1_fanout`: mock `run_agent_pool`, verify Phase 1 prompt sent
  to all models without cross-contamination
- `test_phase2_includes_all_phase1_outputs`: verify Phase 2 prompt
  contains all Phase 1 outputs by model name
- `test_synthesis_writes_spec_file`: mock synthesis output as valid
  frontmatter + markdown, verify file written correctly
- `test_single_model_pool_skips_crossreview`: pool of 1 → Phase 2 skipped,
  Phase 1 output goes directly to output
- `test_max_rounds_respected`: set max_rounds=1, divergence after round 1
  → human_decision_required=True, spec still written with Human Decisions section
- `test_dry_run_no_file_written`: `--dry-run` → spec printed to stdout, no
  file created
- `test_no_synthesis_profile_raises`: pool > 1, no synthesis profile → ValueError
- `test_ideation_result_cost_accumulates`: verify total_cost_usd sums across
  all pool invocations and synthesis

### R6: Integration with vision.md roadmap

Update `docs/vision.md` Phase 11 (Decompose) to note that `forge ideate`
is the implementation path: DECOMPOSE becomes `forge ideate` applied to
"what sub-problems does this spec contain", with the deliberation output
feeding directly into a campaign manifest.

---

## Acceptance Criteria

1. `forge ideate "brief text"` runs the full deliberation protocol and
   writes a valid spec file to `specs/`
2. Phase 1 outputs are generated independently (no model sees others' output)
3. Phase 2 receives all Phase 1 outputs for cross-review
4. Synthesis produces valid frontmatter + markdown
5. Residual divergence appears in a `## Human Decisions Required` section
6. `--dry-run` prints to stdout without writing
7. Single-model pool produces a spec via Phase 1 only (no error)
8. Pool > 1 without synthesis profile raises a clear error
9. All existing tests pass
10. New tests cover all deliberation phases and edge cases

## Out of Scope

- Automatic `forge run` invocation after ideation (human reviews spec first)
- Campaign manifest generation from ideation output (Phase 11 / DECOMPOSE)
- Storing Phase 1/Phase 2 raw outputs in audit log (can add later)
- Brief templating or spec refinement from existing specs
