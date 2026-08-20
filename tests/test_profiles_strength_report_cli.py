"""``forge profiles strength`` — the operator surface for #2308."""

from __future__ import annotations

from pathlib import Path

import yaml

from theforge.cli.main import build_parser
from theforge.cli.profiles import cmd_profiles


def _entry(bands: dict[str, tuple[int, float]]) -> dict:
    by_complexity = {
        band: {"runs": runs, "success_rate": rate, "_successes": round(runs * rate, 4)}
        for band, (runs, rate) in bands.items()
    }
    return {
        "dev": {
            "runs": sum(runs for runs, _ in bands.values()),
            "success_rate": 0.0,
            "by_complexity": by_complexity,
        }
    }


def _project(tmp_path: Path, models: dict[str, dict]) -> Path:
    (tmp_path / "forge.yaml").write_text("project: test\n", encoding="utf-8")
    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir(exist_ok=True)
    (forge_dir / "model_profiles.yaml").write_text(
        yaml.safe_dump({"models": models}), encoding="utf-8"
    )
    return tmp_path


def _run(project_root: Path, *extra: str) -> int:
    args = build_parser().parse_args(
        [
            "profiles",
            "strength",
            "--config",
            str(project_root / "forge.yaml"),
            "--project-root",
            str(project_root),
            *extra,
        ]
    )
    return cmd_profiles(args)


def _row(out: str, canonical_id: str, band: str) -> str:
    return next(
        line
        for line in out.splitlines()
        if line.startswith(canonical_id + " ") and f" {band} " in line
    )


DISAGREEMENT_MODELS = {
    "anthropic/opus/cli": _entry({"large": (47, 0.66)}),
    "openai/gpt-5.4/cli": _entry({"large": (120, 0.92)}),
    "openai/gpt-5.5/cli": _entry({"large": (80, 0.85)}),
}


class TestRendering:
    def test_renders_declared_values_observed_rate_and_samples(self, tmp_path, capsys):
        root = _project(tmp_path, {"anthropic/opus/cli": _entry({"large": (40, 0.9)})})

        assert _run(root, "--complexity", "large") == 0

        row = _row(capsys.readouterr().out, "anthropic/opus/cli", "large")
        assert "strong/10" in row
        assert "0.90" in row
        assert " 40 " in f" {row} "
        assert "observed" in row

    def test_unselected_model_renders_as_unobserved(self, tmp_path, capsys):
        root = _project(tmp_path, {"anthropic/opus/cli": _entry({"large": (40, 0.9)})})

        assert _run(root, "--complexity", "large") == 0

        row = _row(capsys.readouterr().out, "openai/gpt-5.4-pro/cli", "large")
        _model, _band, _declared, observed, samples, *_rest = row.split()
        assert row.endswith("unobserved")
        assert (observed, samples) == ("—", "0")

    def test_thin_evidence_renders_as_insufficient(self, tmp_path, capsys):
        root = _project(tmp_path, {"anthropic/opus/cli": _entry({"large": (3, 0.33)})})

        assert _run(root, "--complexity", "large", "--model", "anthropic/opus/cli") == 0

        out = capsys.readouterr().out
        assert "insufficient_evidence" in _row(out, "anthropic/opus/cli", "large")
        assert "Evidence floor: 10 runs per band" in out

    def test_supported_disagreement_is_summarised_with_peers_and_samples(self, tmp_path, capsys):
        root = _project(tmp_path, dict(DISAGREEMENT_MODELS))

        assert _run(root, "--complexity", "large") == 0

        out = capsys.readouterr().out
        assert "underperforming_declaration" in _row(out, "anthropic/opus/cli", "large")
        assert "Declarations the evidence disagrees with: 1" in out
        assert "declared strong/10, observed 0.66 over 47 runs" in out
        assert "0.85–0.92 (2)" in out

    def test_min_runs_raises_the_bar_for_a_disagreement(self, tmp_path, capsys):
        root = _project(tmp_path, dict(DISAGREEMENT_MODELS))

        assert _run(root, "--complexity", "large", "--min-runs", "60") == 0

        out = capsys.readouterr().out
        assert "insufficient_evidence" in _row(out, "anthropic/opus/cli", "large")
        assert "Declarations the evidence disagrees with: none" in out
        assert "Evidence floor: 60 runs per band" in out

    def test_filters_narrow_the_table(self, tmp_path, capsys):
        root = _project(tmp_path, {"anthropic/opus/cli": _entry({"large": (40, 0.9)})})

        assert _run(root, "--model", "anthropic/opus/cli", "--complexity", "large") == 0

        out = capsys.readouterr().out
        assert "openai/gpt-5.4/cli " not in out
        assert len([line for line in out.splitlines() if line.startswith("anthropic/")]) == 1


class TestEvidenceAttribution:
    def test_unattributable_evidence_is_flagged_and_excluded(self, tmp_path, capsys):
        root = _project(
            tmp_path,
            {
                "anthropic/opus/cli": _entry({"large": (40, 0.9)}),
                "dev": _entry({"medium": (46, 0.78)}),
            },
        )

        assert _run(root) == 0

        out = capsys.readouterr().out
        assert "Profile evidence excluded" in out
        assert "dev (unresolved_identity" in out
        assert "46 runs" in out
        assert "unobserved" in _row(out, "anthropic/opus/cli", "medium")

    def test_excluded_key_matching_a_live_identity_is_not_counted_in_its_row(
        self, tmp_path, capsys
    ):
        """#2308 review: excluded evidence must not also inflate a live row."""
        impostor = _entry({"large": (60, 0.1)})
        # Provider/model only — enough for the router's identity matching to
        # claim it for anthropic/opus, not enough to name it canonically.
        impostor["_identity"] = {"provider": "anthropic", "model": "opus"}
        root = _project(
            tmp_path,
            {"anthropic/opus/cli": _entry({"large": (40, 0.9)}), "impostor": impostor},
        )

        assert _run(root, "--complexity", "large") == 0

        out = capsys.readouterr().out
        row = _row(out, "anthropic/opus/cli", "large")
        assert row.split()[3:5] == ["0.90", "40"]
        assert "impostor (unresolved_identity" in out

    def test_recency_of_evidence_is_declared_unknown(self, tmp_path, capsys):
        root = _project(tmp_path, {"anthropic/opus/cli": _entry({"large": (40, 0.9)})})

        assert _run(root) == 0

        assert "Evidence recency: unknown" in capsys.readouterr().out

    def test_non_dev_capable_models_are_named_not_reported_unobserved(self, tmp_path, capsys):
        root = _project(tmp_path, {"anthropic/opus/cli": _entry({"large": (40, 0.9)})})

        assert _run(root) == 0

        out = capsys.readouterr().out
        assert "Live models excluded as not dev-capable" in out
        assert not [line for line in out.splitlines() if line.startswith("anthropic/haiku/cli ")]


class TestReadOnly:
    def test_command_edits_neither_catalog_nor_profiles(self, tmp_path, capsys):
        root = _project(tmp_path, dict(DISAGREEMENT_MODELS))
        config_before = (root / "forge.yaml").read_bytes()
        profiles_path = root / ".forge" / "model_profiles.yaml"
        profiles_before = profiles_path.read_bytes()

        assert _run(root) == 0

        assert (root / "forge.yaml").read_bytes() == config_before
        assert profiles_path.read_bytes() == profiles_before
        assert "does not modify any catalog declaration" in capsys.readouterr().out

    def test_missing_config_is_an_explicit_failure(self, tmp_path, capsys):
        args = build_parser().parse_args(
            [
                "profiles",
                "strength",
                "--config",
                str(tmp_path / "nowhere" / "forge.yaml"),
            ]
        )

        assert cmd_profiles(args) == 1
        assert "forge.yaml not found" in capsys.readouterr().err
