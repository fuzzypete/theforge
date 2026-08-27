from __future__ import annotations

import textwrap
from pathlib import Path

from theforge.shape_check import ShapeVerdict, check

ROOT = Path(__file__).resolve().parents[1]
AUTHORING_GUIDE = ROOT / "docs" / "guides" / "authoring.md"


def test_authoring_guide_declares_canonical_bug_headings_and_shared_vocabulary() -> None:
    text = AUTHORING_GUIDE.read_text(encoding="utf-8")
    assert "## Shared vocabulary" in text
    assert "canonical heading `## Observed`" in text
    assert "canonical heading `## Expected`" in text
    assert "It sits **after** `## Observed` and `## Expected`." in text
    for term in (
        "**generation**",
        "**seating**",
        "**allocation**",
        "**budget**",
        "**ceiling**",
        "**band**",
        "**invocation**",
    ):
        assert term in text


def test_documented_runnable_issue_shapes_are_admissible() -> None:
    cases = (
        (
            "Add `forge status --json` flag for machine-readable output",
            textwrap.dedent(
                """\
                ## Why

                Operators scripting against `forge status` currently parse human-formatted
                text, which breaks whenever the display layout changes. A stable JSON shape
                lets them script reliably without coupling to the TTY rendering.

                ## Acceptance criteria

                - `forge status --json` writes a single JSON object to stdout and exits 0
                  when there is an active sprint.
                - The JSON object reports sprint id, current phase, completed story count,
                  remaining story count, and elapsed seconds.
                - `forge status --json` with no active sprint writes `{"active": false}` and
                  exits 0.
                - `forge status --json --invalid-flag` rejects the unknown flag and exits
                  non-zero.
                - Existing human output produced by `forge status` (no flag) is unchanged.

                ## Example

                ```
                $ forge status --json
                {"active": true, "sprint_id": "2026-05-01-1430", "phase": "review",
                 "completed": 2, "remaining": 1, "elapsed_seconds": 412}

                $ forge status --json   # no active sprint
                {"active": false}
                ```

                ## Notes

                The existing `forge status` renderer already computes these values for the
                TTY path; surfacing them as JSON should not require a second source of
                truth.
                """
            ),
            ["enhancement"],
        ),
        (
            "`forge sprint --resume` re-runs already-merged stories",
            textwrap.dedent(
                """\
                ## Observed

                Ran `forge sprint --resume` on a sprint where two of three stories had been
                reviewed, approved, and merged to main in a previous session. The resume run
                re-entered both merged stories at the dev phase and produced a second set of
                commits for work that had already landed.

                ## Expected

                Resuming a sprint should never repeat work that has already reached a
                terminal merged state. A story whose branch has been merged into the base
                branch is finished from the sprint runner's perspective, regardless of which
                phase the audit log last recorded for it. Resume should advance only stories
                that are still in flight.

                ## Diagnosis

                - **Observed symptom:** sprint resume re-enters already-merged stories at the
                  dev phase, producing duplicate commits for landed work.
                - **Evidence:** run id `1ff6b0bb7992` — resume log shows both merged stories
                  re-entering dev.
                - **Confirmed cause:** `_is_already_merged` requires at least one commit
                  ahead, so a zero-delta APPROVE is misclassified as unmerged.
                - **Affected code path:** `sprint.runner._is_already_merged`.
                - **Fix-success criterion:** resume identifies a zero-delta APPROVE story as
                  already merged and does not re-dispatch it.
                """
            ),
            ["bug"],
        ),
        (
            "Move retry-policy fields out of `profiles.dev` into top-level `retry:`",
            textwrap.dedent(
                """\
                ## Why

                Retry counts (`max_dev_iterations`, `max_review_cycles`) are policy that
                applies to the whole sprint, not to a single profile. Having them nested
                under `profiles.dev` confuses operators reading `forge.yaml` and forces
                duplicate values whenever a project defines multiple dev profiles.

                ## Acceptance criteria

                - `forge.yaml` files that already declare a top-level `retry:` block load
                  unchanged and produce the same effective config they do today.
                - `forge.yaml` files that still declare retry fields under `profiles.dev`
                  load successfully, emit a deprecation warning naming the moved fields,
                  and produce the same effective behavior as the top-level form.
                - `forge check-config` reports the resolved retry policy from a single
                  source (the top-level block), regardless of which form was written.
                - The full test suite passes with no changes to existing behavioral tests.

                ## Example

                Before:

                ```yaml
                profiles:
                  dev:
                    model: sonnet
                    max_dev_iterations: 3
                    max_review_cycles: 2
                ```

                After:

                ```yaml
                profiles:
                  dev:
                    model: sonnet

                retry:
                  max_dev_iterations: 3
                  max_review_cycles: 2
                ```
                """
            ),
            ["task"],
        ),
        (
            "Tighten three `forge status` output fields",
            textwrap.dedent(
                """\
                ## Why

                Three small display issues in `forge status` keep getting reported
                separately. They share rendering code and reviewing them in isolation costs
                more than batching them.

                ## Acceptance criteria

                - `forge status` emits elapsed time as `Hh Mm Ss` (e.g. `1h 04m 12s`)
                  instead of raw seconds.
                - `forge status` reports the active phase in lowercase and rejects an
                  unknown phase value with a clear error rather than rendering it raw.
                - `forge status` writes "no active sprint" to stdout (not stderr) and exits
                  0 when there is nothing to report.

                ## Example

                ```
                $ forge status
                sprint: 2026-05-01-1430
                phase:  review
                elapsed: 1h 04m 12s
                stories: 2 done / 1 remaining
                ```
                """
            ),
            ["task"],
        ),
        (
            "Document `forge.yaml` `retry:` block in the inputs reference",
            textwrap.dedent(
                """\
                ## Why

                Operators tuning retry behavior have to read source to discover which keys
                are valid under `retry:` and what their defaults are. The inputs reference
                covers `validation:` and `workspace:` in detail but skips `retry:`.

                ## Acceptance criteria

                - `docs/guides/inputs-reference.md` contains a `### Retry policy` subsection
                  under the project-config heading.
                - That subsection lists every key currently accepted under `retry:` in a
                  table with name, default, and one-line description.
                - The reference reports each key's default by reading from the loader, not
                  by hand-copying — so the doc passes review against current code.
                - The page renders without broken internal links when built with the docs
                  toolchain.

                ## Example

                The new subsection follows the existing pattern used for `validation:`:

                ```markdown
                ### Retry policy

                | Field | Default | Description |
                |-------|---------|-------------|
                | `max_dev_iterations` | 3 | Dev attempts within one review cycle |
                | `max_review_cycles`  | 2 | Full dev→review loops before ESCALATE |
                ```
                """
            ),
            ["task"],
        ),
    )

    for title, body, labels in cases:
        result = check(title, body, labels)
        assert result.verdict is ShapeVerdict.RUNNABLE, (title, result.verdict, result.reasons)
