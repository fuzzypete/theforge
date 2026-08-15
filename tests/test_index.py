from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from theforge.cli.index import cmd_index
from theforge.indexer import generate_index
from theforge.knowledge_index import KNOWLEDGE_INDEX_PATH


def test_generate_index_writes_modules_yaml(tmp_path: Path) -> None:
    (tmp_path / "forge.yaml").write_text("project: test\n", encoding="utf-8")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        'from .mod import PublicThing\n__all__ = ["PublicThing"]\n', encoding="utf-8"
    )
    (pkg / "mod.py").write_text(
        "\n".join(
            [
                "import os",
                "from pkg import helpers",
                "",
                "class PublicThing:",
                "    pass",
                "",
                "def public_fn():",
                "    return helpers",
                "",
                "def _private():",
                "    return os",
                "",
            ]
        ),
        encoding="utf-8",
    )

    payload = generate_index(tmp_path, now=datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc))

    index_path = tmp_path / ".forge" / "index" / "modules.yaml"
    assert index_path.exists()
    data = yaml.safe_load(index_path.read_text(encoding="utf-8"))
    assert data == payload
    entries = {entry["path"]: entry for entry in data["modules"]}
    assert entries["pkg/mod.py"]["public_symbols"] == ["PublicThing", "public_fn"]
    assert entries["pkg/mod.py"]["imports"] == ["os", "pkg.helpers"]
    assert entries["pkg/mod.py"]["generated_at"] == "2024-01-02T03:04:05+00:00"
    assert "git_sha" in entries["pkg/mod.py"]


def test_generate_index_is_deterministic_and_refreshes_metadata_for_reused_entries(
    tmp_path: Path,
) -> None:
    (tmp_path / "forge.yaml").write_text("project: test\n", encoding="utf-8")
    src = tmp_path / "sample.py"
    src.write_text("import os\n\ndef public_fn():\n    return os.name\n", encoding="utf-8")

    first = generate_index(tmp_path, now=datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
    second = generate_index(tmp_path, now=datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
    assert first == second

    src.write_text("import sys\n\ndef public_fn():\n    return sys.version\n", encoding="utf-8")
    third = generate_index(tmp_path, now=datetime(2024, 1, 2, 3, 5, 0, tzinfo=timezone.utc))
    entries = {entry["path"]: entry for entry in third["modules"]}
    assert entries["sample.py"]["generated_at"] == "2024-01-02T03:05:00+00:00"
    assert entries["sample.py"]["imports"] == ["sys"]


def test_generate_index_refreshes_git_sha_for_reused_entries(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "forge.yaml").write_text("project: test\n", encoding="utf-8")
    src = tmp_path / "sample.py"
    src.write_text("def public_fn():\n    return 1\n", encoding="utf-8")

    monkeypatch.setattr("theforge.indexer._current_git_sha", lambda _: "sha-one")
    generate_index(tmp_path, now=datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc))

    monkeypatch.setattr("theforge.indexer._current_git_sha", lambda _: "sha-two")
    payload = generate_index(tmp_path, now=datetime(2024, 1, 2, 3, 5, 0, tzinfo=timezone.utc))

    entries = {entry["path"]: entry for entry in payload["modules"]}
    assert entries["sample.py"]["git_sha"] == "sha-two"
    assert entries["sample.py"]["generated_at"] == "2024-01-02T03:05:00+00:00"


def test_cmd_index_generates_index_file(tmp_path: Path) -> None:
    (tmp_path / "forge.yaml").write_text("project: test\n", encoding="utf-8")
    (tmp_path / "module.py").write_text("def public_fn():\n    return 1\n", encoding="utf-8")

    args = type("Args", (), {"config": str(tmp_path / "forge.yaml"), "knowledge": False})()
    rc = cmd_index(args)

    assert rc == 0
    assert (tmp_path / ".forge" / "index" / "modules.yaml").exists()


def test_cmd_index_can_rebuild_the_knowledge_index_and_report_skips(
    tmp_path: Path, capsys
) -> None:
    (tmp_path / "forge.yaml").write_text("project: test\n", encoding="utf-8")
    summaries = tmp_path / ".forge" / "knowledge" / "summaries"
    summaries.mkdir(parents=True)
    (summaries / "run-good.yaml").write_text(
        yaml.safe_dump(
            {
                "run_id": "run-good",
                "generated_at": "2026-08-14T09:00:00+00:00",
                "story": {"slug": "api-retry", "name": "API retry", "github_issue": 301},
                "story_shape": {"work_type": "feature", "complexity": "medium"},
                "domains": ["backend"],
                "changed_files": ["src/client.py"],
                "learned_patterns": ["retry"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (summaries / "run-bad.yaml").write_text("run_id: [unterminated\n", encoding="utf-8")

    args = type("Args", (), {"config": str(tmp_path / "forge.yaml"), "knowledge": True})()
    rc = cmd_index(args)

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == str(tmp_path / KNOWLEDGE_INDEX_PATH)
    assert "Skipped .forge/knowledge/summaries/run-bad.yaml: invalid YAML:" in captured.err
    assert not (tmp_path / ".forge" / "index" / "modules.yaml").exists()
