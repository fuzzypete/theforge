"""Mirrors ``src/theforge/prior_run_replay_render.py`` per the test mirror convention."""

from __future__ import annotations

import json

import yaml

from theforge.prior_run_replay_render import render_json, render_terminal, render_yaml


def _report() -> dict:
    return {
        "generated_at": "2026-08-20T00:00:00+00:00",
        "judgments_path": "/tmp/judgments.yaml",
        "aggregate": {
            "story_count": 2,
            "stories_with_candidates": 1,
            "qualifying_signal_counts": {
                "file_overlap": 1,
                "dir_overlap": 0,
            },
            "candidate_cap_useful_phase_count": 3,
            "claim_cap_useful_phase_count": 1,
        },
        "corpora": [
            {
                "name": "theforge",
                "root": "/repo/theforge",
                "story_count": 2,
                "fence_probes": [
                    {
                        "probe_run_id": "story-123",
                        "co_surfaced": True,
                        "matched": {
                            "1a6b6e18d232": {"offered": True, "reason": "file_overlap"},
                            "73d7de156730": {"offered": False, "reason": None},
                        },
                    }
                ],
            }
        ],
    }


class TestRenderTerminal:
    def test_render_terminal_summarizes_counts_and_fence_probe_details(self) -> None:
        out = render_terminal(_report())

        assert "Prior-run selection replay - 2 stories, 1 with >=1 candidate" in out
        assert "Qualifying signals:" in out
        assert "  file_overlap: 1" in out
        assert "Useful truncation pressure:" in out
        assert "candidate cap: 3 phase(s)" in out
        assert "Corpus theforge (2 stories)" in out
        assert "root: /repo/theforge" in out
        assert "1a6b6e18d232: offered [file_overlap]" in out
        assert "73d7de156730: not offered" in out
        assert "co-surfaced=True" in out
        assert out.endswith("\n")


class TestStructuredRenderers:
    def test_render_yaml_round_trips_the_report(self) -> None:
        assert yaml.safe_load(render_yaml(_report())) == _report()

    def test_render_json_round_trips_the_report(self) -> None:
        assert json.loads(render_json(_report())) == _report()
