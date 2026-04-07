from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from theforge.cli.index import cmd_index
from theforge.indexer import generate_index


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

    rc = cmd_index(type("Args", (), {"config": str(tmp_path / "forge.yaml")})())

    assert rc == 0
    assert (tmp_path / ".forge" / "index" / "modules.yaml").exists()
