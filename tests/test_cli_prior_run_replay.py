from __future__ import annotations

import argparse
from pathlib import Path

from theforge.cli.prior_run_replay import cmd_prior_run_replay


def test_cmd_prior_run_replay_renders_json(monkeypatch, capsys, tmp_path: Path) -> None:
    config_path = tmp_path / "forge.yaml"
    config_path.write_text("project: demo\n", encoding="utf-8")
    judgments_path = tmp_path / "judgments.yaml"
    judgments_path.write_text("corpora: {}\n", encoding="utf-8")

    monkeypatch.setattr(
        "theforge.cli.prior_run_replay.run_prior_run_replay",
        lambda corpora, judgments_path: {
            "generated_at": "2026-08-20T00:00:00+00:00",
            "judgments_path": str(judgments_path),
            "corpora": [],
            "aggregate": {
                "story_count": 0,
                "stories_with_candidates": 0,
                "qualifying_signal_counts": {},
                "candidate_cap_useful_phase_count": 0,
                "claim_cap_useful_phase_count": 0,
            },
        },
    )

    args = argparse.Namespace(
        config=str(config_path),
        corpus=[f"demo={tmp_path}"],
        judgments=str(judgments_path),
        format="json",
    )

    assert cmd_prior_run_replay(args) == 0
    out = capsys.readouterr().out
    assert '"story_count": 0' in out
    assert '"corpora": []' in out
