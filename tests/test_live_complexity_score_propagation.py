"""Cross-phase live-state complexity_score propagation (issue #1917).

The live sprint-status COMPLEXITY column renders a numeric score when one is
present and a band word otherwise. Before this fix only the DEV-phase live-state
payload carried ``complexity_score``; every sibling phase (preflight, plan,
plan-review, review, validate) emitted the band alone, so a story that never
entered DEV rendered its band while a DEV-passed story rendered its number — a
single table mixing both forms.

These tests lock two properties:

- **Propagation:** once a numeric score is known, every phase's live-state
  update carries it, so the renderer's preference for the numeric form holds
  uniformly in any phase — including stories that terminate without DEV.
- **Hazard (no clear):** ``complexity_score`` is emitted only when the score is
  known. The sprint state writer treats an explicit ``complexity_score: None``
  as a *clear* instruction, so a payload built before the score is computed
  (the first preflight update, and the mid-sprint preflight re-run) must omit
  the key rather than wipe a score an earlier phase established.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from theforge.coordinator.reviewer_progress import ReviewerProgressChannel
from theforge.coordinator.util import live_complexity_fields
from theforge.sprint.status_reader import read_live_status
from theforge.sprint.story_state import SprintStoryState, StoryOutcome

# ── Unit: the shared payload helper ───────────────────────────────────────────


class TestLiveComplexityFields:
    def test_includes_score_when_known(self) -> None:
        assert live_complexity_fields("medium", 6) == {
            "complexity": "medium",
            "complexity_score": 6,
        }

    def test_omits_score_key_when_none(self) -> None:
        # A None score means "not computed yet" — the key must be absent so the
        # state writer preserves an established score instead of clearing it.
        fields = live_complexity_fields("medium", None)
        assert fields == {"complexity": "medium"}
        assert "complexity_score" not in fields

    def test_zero_is_a_known_score(self) -> None:
        # 0 is falsy but a legitimately-known score; it must still be emitted.
        assert live_complexity_fields("small", 0) == {
            "complexity": "small",
            "complexity_score": 0,
        }


# ── Seam helpers ──────────────────────────────────────────────────────────────


def _state_update_fn(state: SprintStoryState, slug: str):
    """Mirror the coordinator's per-story ``state_update_fn`` wrapper.

    The real wrapper routes a phase payload dict into ``SprintStoryState``'s
    canonical ``transition`` — the exact seam that turns a phase's emitted
    ``complexity_score`` (or its absence) into persisted live state.
    """

    def _update(updates: dict) -> None:
        state.transition(slug, **updates)

    return _update


def _rendered_complexity(state: SprintStoryState, tmp_path: Path) -> tuple:
    """Serialize live state to a .state file and read it back through the reader."""
    run_id = "run-1917"
    runs_dir = tmp_path / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / f"{run_id}.state").write_text(
        yaml.safe_dump({"sprint_name": "s", "stories": state.as_dict()}),
        encoding="utf-8",
    )
    entries = read_live_status(run_id, tmp_path)
    assert entries is not None
    entry = entries[0]
    return entry.complexity, entry.complexity_score


# Payloads mirror what each phase module now constructs via live_complexity_fields.
def _preflight_pre_parse(complexity, score) -> dict:
    return {
        "phase": "PREFLIGHT",
        "iteration": 0,
        "cost_usd": 0.0,
        **live_complexity_fields(complexity, score),
        "detail": {"preflight_verdict": "PROCEED"},
    }


def _preflight_post_parse(complexity, score) -> dict:
    return {
        "phase": "PREFLIGHT",
        "iteration": 0,
        "cost_usd": 0.0,
        **live_complexity_fields(complexity, score),
        "detail": {"preflight_verdict": "PROCEED"},
    }


def _plan(complexity, score) -> dict:
    return {
        "phase": "PLAN",
        "iteration": 0,
        "cost_usd": 0.0,
        **live_complexity_fields(complexity, score),
        "detail": {"plan_attempt": 1},
    }


def _review(complexity, score) -> dict:
    return {
        "phase": "REVIEW",
        "iteration": 1,
        "cost_usd": 0.0,
        **live_complexity_fields(complexity, score),
        "detail": {"review_cycle": 1},
    }


def _validate(complexity, score) -> dict:
    return {
        "phase": "VALIDATE",
        "iteration": 1,
        "cost_usd": 0.0,
        **live_complexity_fields(complexity, score),
        "detail": {"gate_status": "PASS"},
    }


# ── Seam: numeric score reaches live state in every non-DEV phase ─────────────


class TestCrossPhasePropagation:
    def test_score_present_after_every_phase_update(self, tmp_path: Path) -> None:
        """A story with a computed score renders numeric in each phase, no DEV."""
        state = SprintStoryState()
        state.register("issue-1911", "Issue #1911", outcome=StoryOutcome.RUNNING)
        update = _state_update_fn(state, "issue-1911")

        # First preflight payload fires before the score is parsed.
        update(_preflight_pre_parse("medium", None))
        band, score = _rendered_complexity(state, tmp_path)
        assert band == "medium"
        assert score is None  # nothing computed yet — band-only is correct here

        # After the parse step the numeric score is known.
        update(_preflight_post_parse("medium", 6))
        assert _rendered_complexity(state, tmp_path) == ("medium", 6)

        # Plan carries the score.
        update(_plan("medium", 6))
        assert _rendered_complexity(state, tmp_path) == ("medium", 6)

        # Plan-review reviewer progress carries the score.
        pr_channel = ReviewerProgressChannel(
            reviewer_names=["gemini", "gpt"],
            phase="PLAN_REVIEW",
            iteration=1,
            cost_usd=0.0,
            complexity="medium",
            complexity_score=6,
            state_update_fn=update,
        )
        pr_channel.cb({"label": "gemini", "iter": 1, "tool_calls": 1})
        assert _rendered_complexity(state, tmp_path) == ("medium", 6)

        # Review reviewer progress carries the score.
        rv_channel = ReviewerProgressChannel(
            reviewer_names=["gemini", "gpt"],
            phase="REVIEW",
            iteration=1,
            cost_usd=0.0,
            complexity="medium",
            complexity_score=6,
            state_update_fn=update,
        )
        rv_channel.cb({"label": "gpt", "iter": 2, "tool_calls": 3})
        assert _rendered_complexity(state, tmp_path) == ("medium", 6)

        # Review summary payload carries the score.
        update(_review("medium", 6))
        assert _rendered_complexity(state, tmp_path) == ("medium", 6)

        # Validate carries the score.
        update(_validate("medium", 6))
        assert _rendered_complexity(state, tmp_path) == ("medium", 6)


# ── Seam: the clear-a-score hazard ────────────────────────────────────────────


class TestScorePreservationHazard:
    def test_pre_parse_preflight_does_not_clear_established_score(self, tmp_path: Path) -> None:
        """A mid-sprint preflight re-run, fired before its own score is computed,
        must not wipe a score an earlier phase established."""
        state = SprintStoryState()
        state.register("issue-1899", "Issue #1899", outcome=StoryOutcome.RUNNING)
        update = _state_update_fn(state, "issue-1899")

        # An earlier phase established a numeric score.
        update(_review("medium", 5))
        assert _rendered_complexity(state, tmp_path) == ("medium", 5)

        # The pre-parse preflight payload (score still None) omits the key, so the
        # established score survives instead of being cleared to None.
        update(_preflight_pre_parse("medium", None))
        assert _rendered_complexity(state, tmp_path) == ("medium", 5)

    def test_reviewer_channel_without_score_preserves_established_score(
        self, tmp_path: Path
    ) -> None:
        """A reviewer channel constructed with no score (legacy default) must not
        overwrite a numeric live display with band-only state."""
        state = SprintStoryState()
        state.register("issue-1910", "Issue #1910", outcome=StoryOutcome.RUNNING)
        update = _state_update_fn(state, "issue-1910")

        update(_plan("large", 8))
        assert _rendered_complexity(state, tmp_path) == ("large", 8)

        # complexity_score omitted → default None → key never emitted.
        channel = ReviewerProgressChannel(
            reviewer_names=["gemini"],
            phase="REVIEW",
            iteration=1,
            cost_usd=0.0,
            complexity="large",
            state_update_fn=update,
        )
        channel.cb({"label": "gemini", "iter": 1, "tool_calls": 1})
        assert _rendered_complexity(state, tmp_path) == ("large", 8)

    def test_no_phase_payload_emits_explicit_none_score(self) -> None:
        """The hazard invariant at the source: no helper-built payload carries an
        explicit ``complexity_score: None`` before the score is computed."""
        for payload in (
            _preflight_pre_parse("medium", None),
            _preflight_post_parse("medium", None),
            _plan("medium", None),
            _review("medium", None),
            _validate("medium", None),
        ):
            assert "complexity_score" not in payload
