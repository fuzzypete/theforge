"""Extractor and index-command coverage for the invariant-index spike (#1875)."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from theforge.cli.index import cmd_index
from theforge.config.types import KnowledgeConfig
from theforge.invariant_index import (
    COMPLETENESS_FULL,
    COMPLETENESS_NONE,
    COMPLETENESS_PARTIAL,
    INVARIANT_INDEX_PATH,
    INVARIANT_INDEX_SCHEMA_VERSION,
    build_invariant_index,
    discover_sources,
    extract_from_text,
    load_invariant_index,
    rebuild_invariant_index,
)

_GLOBS = ("*.md", "docs/**/*.md", "**/CONVENTIONS.md")


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


_MARKED = """# Policy

## Audit rules

Some preamble.

<!-- forge-invariant id="summaries-advisory"
     scope="area:audit phase:plan,dev files:src/audit/*.py"
     enforcement="review" -->
Summaries advise agents; they never drive coordinator control flow.
<!-- /forge-invariant -->

Trailing prose.
"""


# ── Marker parsing ───────────────────────────────────────────────────────────


def test_marker_parsing_records_identity_provenance_and_scope():
    entries, diagnostics = extract_from_text(_MARKED, "docs/policy.md")

    assert diagnostics == []
    (entry,) = entries
    assert entry["id"] == "summaries-advisory"
    assert entry["source_path"] == "docs/policy.md"
    assert entry["source_anchor"] == "Audit rules"
    assert entry["enforcement"] == "review"
    assert entry["scope"]["areas"] == ["audit"]
    assert entry["scope"]["phases"] == ["dev", "plan"]
    assert entry["scope"]["files"] == ["src/audit/*.py"]
    assert entry["applicability"]["scope_completeness"] == COMPLETENESS_FULL
    assert entry["source_digest"].startswith("sha256:")


def test_index_entry_carries_source_anchor_and_line_span_not_prose():
    entries, _ = extract_from_text(_MARKED, "docs/policy.md")
    (entry,) = entries

    lines = _MARKED.splitlines()
    assert lines[entry["start_line"] - 1].startswith("<!-- forge-invariant")
    assert lines[entry["end_line"] - 1].startswith("<!-- /forge-invariant")
    body = "\n".join(lines[entry["body_start_line"] - 1 : entry["body_end_line"]])
    assert "never drive coordinator control flow" in body

    # The index is metadata only: the rule text itself is never copied into it.
    serialized = yaml.safe_dump(entry)
    assert "never drive coordinator control flow" not in serialized


def test_body_span_starts_after_a_multi_line_opening_marker():
    """_MARKED's opening marker wraps, as the documented convention example does.

    Deriving the span from the marker's start line swept the scope and
    enforcement attribute lines into the body.
    """
    (entry,) = extract_from_text(_MARKED, "docs/policy.md")[0]

    lines = _MARKED.splitlines()
    body = lines[entry["body_start_line"] - 1 : entry["body_end_line"]]

    assert body == ["Summaries advise agents; they never drive coordinator control flow."]
    assert entry["body_lines"] == 1
    assert not any("scope=" in line or "enforcement=" in line for line in body)


def test_a_body_sharing_a_line_with_its_markers_is_a_diagnostic():
    entries, diagnostics = extract_from_text(
        '<!-- forge-invariant id="inline" -->A rule.<!-- /forge-invariant -->\n', "docs/a.md"
    )

    assert entries == []
    assert any("shares a line with its markers" in d.reason for d in diagnostics)


def test_enforcement_defaults_to_advisory_and_scope_may_be_omitted():
    entries, diagnostics = extract_from_text(
        '<!-- forge-invariant id="bare" -->\nA rule.\n<!-- /forge-invariant -->\n',
        "docs/a.md",
    )

    assert diagnostics == []
    assert entries[0]["enforcement"] == "advisory"
    assert entries[0]["applicability"]["scope_completeness"] == COMPLETENESS_NONE


def test_area_only_scope_is_partial_completeness():
    entries, _ = extract_from_text(
        '<!-- forge-invariant id="areas" scope="area:prompts" -->\nA rule.\n'
        "<!-- /forge-invariant -->\n",
        "docs/a.md",
    )

    assert entries[0]["applicability"]["scope_completeness"] == COMPLETENESS_PARTIAL


# ── Malformed markers become diagnostics, never exceptions ───────────────────


def test_unterminated_marker_is_a_diagnostic_and_skips_the_entry():
    entries, diagnostics = extract_from_text(
        '<!-- forge-invariant id="open" -->\nA rule with no close marker.\n', "docs/a.md"
    )

    assert entries == []
    assert [d.reason for d in diagnostics] == ["unterminated forge-invariant block"]


def test_missing_and_invalid_ids_are_diagnostics():
    _, missing = extract_from_text(
        "<!-- forge-invariant -->\nA rule.\n<!-- /forge-invariant -->\n", "docs/a.md"
    )
    _, invalid = extract_from_text(
        '<!-- forge-invariant id="Not Valid" -->\nA rule.\n<!-- /forge-invariant -->\n',
        "docs/a.md",
    )

    assert any("missing required attribute 'id'" in d.reason for d in missing)
    assert any("invalid id" in d.reason for d in invalid)


def test_unknown_enforcement_falls_back_to_advisory_with_a_diagnostic():
    entries, diagnostics = extract_from_text(
        '<!-- forge-invariant id="x" enforcement="mandatory" -->\nA rule.\n'
        "<!-- /forge-invariant -->\n",
        "docs/a.md",
    )

    assert entries[0]["enforcement"] == "advisory"
    assert any("unknown enforcement" in d.reason for d in diagnostics)


def test_unparsed_scope_key_never_reports_full_completeness():
    entries, diagnostics = extract_from_text(
        '<!-- forge-invariant id="x" scope="module:audit files:src/*.py" -->\nA rule.\n'
        "<!-- /forge-invariant -->\n",
        "docs/a.md",
    )

    assert any("unknown scope key" in d.reason for d in diagnostics)
    applicability = entries[0]["applicability"]
    assert applicability["scope_completeness"] != COMPLETENESS_FULL
    assert applicability["unparsed_scope_keys"] == ["module"]


def test_empty_body_is_a_diagnostic():
    entries, diagnostics = extract_from_text(
        '<!-- forge-invariant id="x" -->\n\n<!-- /forge-invariant -->\n', "docs/a.md"
    )

    assert entries == []
    assert any("empty invariant body" in d.reason for d in diagnostics)


def test_duplicate_ids_keep_the_first_and_report_the_second(tmp_path: Path):
    marked = '<!-- forge-invariant id="dup" -->\n{}\n<!-- /forge-invariant -->\n'
    _write(tmp_path, "a.md", marked.format("One."))
    _write(tmp_path, "b.md", marked.format("Two."))

    result = build_invariant_index(tmp_path, _GLOBS)

    assert [entry["source_path"] for entry in result.entries] == ["a.md"]
    assert any("duplicate invariant id" in d.reason for d in result.diagnostics)


def test_unreadable_source_is_a_diagnostic_not_a_crash(tmp_path: Path, monkeypatch):
    _write(tmp_path, "a.md", '<!-- forge-invariant id="x" -->\nRule.\n<!-- /forge-invariant -->\n')
    original = Path.read_text

    def _boom(self, *args, **kwargs):
        if self.name == "a.md":
            raise OSError("permission denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _boom)
    result = build_invariant_index(tmp_path, _GLOBS)

    assert result.entries == []
    assert any("unreadable" in d.reason for d in result.diagnostics)


# ── Source discovery and determinism ─────────────────────────────────────────


def test_configured_globs_bound_discovery(tmp_path: Path):
    _write(tmp_path, "docs/adr/one.md", "x")
    _write(tmp_path, "src/pkg/CONVENTIONS.md", "x")
    _write(tmp_path, "src/pkg/notes.md", "x")

    found = discover_sources(tmp_path, _GLOBS)

    assert [p.as_posix() for p in found] == ["docs/adr/one.md", "src/pkg/CONVENTIONS.md"]


def test_discovery_skips_derived_and_vendored_directories(tmp_path: Path):
    _write(tmp_path, ".forge/knowledge/notes.md", "x")
    _write(tmp_path, ".pytest_cache/README.md", "x")
    _write(tmp_path, "node_modules/pkg/CONVENTIONS.md", "x")
    _write(tmp_path, "build/generated/notes.md", "x")
    _write(tmp_path, "keep.md", "x")

    assert [p.as_posix() for p in discover_sources(tmp_path, ("**/*.md",))] == ["keep.md"]


def test_the_default_glob_names_no_project_layout():
    # The feature has to work on any target project, so the shipped default
    # cannot presume a docs/ directory exists.
    assert KnowledgeConfig().invariant_sources == ("**/*.md",)


def test_default_glob_finds_markers_wherever_a_project_keeps_them(tmp_path: Path):
    _write(
        tmp_path,
        "POLICY.md",
        '<!-- forge-invariant id="root" -->\nR.\n<!-- /forge-invariant -->\n',
    )
    _write(
        tmp_path,
        "handbook/rules/two.md",
        '<!-- forge-invariant id="nested" -->\nN.\n<!-- /forge-invariant -->\n',
    )

    result = build_invariant_index(tmp_path, KnowledgeConfig().invariant_sources)

    assert [entry["id"] for entry in result.entries] == ["root", "nested"]


# ── Documentation examples are not invariants ────────────────────────────────


def test_markers_inside_fenced_code_blocks_are_ignored(tmp_path: Path):
    _write(
        tmp_path,
        "guide.md",
        "# How to mark an invariant\n\n"
        "```md\n"
        '<!-- forge-invariant id="illustration" scope="area:x" enforcement="review" -->\n'
        "An example a project shows while documenting the convention.\n"
        "<!-- /forge-invariant -->\n"
        "```\n\n"
        "## A real rule\n\n"
        '<!-- forge-invariant id="genuine" -->\n'
        "An actual rule this project asserts.\n"
        "<!-- /forge-invariant -->\n",
    )

    result = build_invariant_index(tmp_path, ("**/*.md",))

    assert [entry["id"] for entry in result.entries] == ["genuine"]
    assert result.diagnostics == ()


def test_documenting_the_convention_does_not_file_duplicate_ids(tmp_path: Path):
    """The repo's own docs illustrate real ids; that must not collide with them."""
    _write(
        tmp_path,
        "rules.md",
        '<!-- forge-invariant id="shared" -->\nThe real rule.\n<!-- /forge-invariant -->\n',
    )
    _write(
        tmp_path,
        "guide.md",
        "~~~md\n"
        '<!-- forge-invariant id="shared" -->\nThe same id, shown as an example.\n'
        "<!-- /forge-invariant -->\n"
        "~~~\n",
    )

    result = build_invariant_index(tmp_path, ("**/*.md",))

    assert [entry["source_path"] for entry in result.entries] == ["rules.md"]
    assert result.diagnostics == ()


def test_an_unterminated_fence_suppresses_the_rest_of_the_file(tmp_path: Path):
    _write(
        tmp_path,
        "guide.md",
        "```md\n"
        '<!-- forge-invariant id="never-closed-fence" -->\nExample.\n<!-- /forge-invariant -->\n',
    )

    assert build_invariant_index(tmp_path, ("**/*.md",)).entries == []


def test_ordering_is_deterministic_across_rebuilds(tmp_path: Path):
    _write(tmp_path, "z.md", '<!-- forge-invariant id="z" -->\nZ.\n<!-- /forge-invariant -->\n')
    _write(
        tmp_path,
        "docs/a.md",
        '<!-- forge-invariant id="a2" -->\nA2.\n<!-- /forge-invariant -->\n'
        '<!-- forge-invariant id="a1" -->\nA1.\n<!-- /forge-invariant -->\n',
    )

    first = build_invariant_index(tmp_path, _GLOBS).payload
    second = build_invariant_index(tmp_path, _GLOBS).payload

    assert first == second
    assert [entry["id"] for entry in first["invariants"]] == ["a2", "a1", "z"]


def test_rebuild_writes_the_derived_index_and_reloads_it(tmp_path: Path):
    _write(tmp_path, "docs/policy.md", _MARKED)

    result = rebuild_invariant_index(tmp_path, _GLOBS)

    assert result.path == tmp_path / INVARIANT_INDEX_PATH
    payload = yaml.safe_load(result.path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == INVARIANT_INDEX_SCHEMA_VERSION
    assert payload["invariant_count"] == 1
    assert [entry["id"] for entry in load_invariant_index(tmp_path)] == ["summaries-advisory"]


def test_rebuild_drops_entries_whose_markers_were_removed(tmp_path: Path):
    _write(tmp_path, "docs/policy.md", _MARKED)
    rebuild_invariant_index(tmp_path, _GLOBS)

    _write(tmp_path, "docs/policy.md", "# Policy\n\nNo markers any more.\n")
    rebuild_invariant_index(tmp_path, _GLOBS)

    assert load_invariant_index(tmp_path) == []


def test_load_degrades_to_empty_on_missing_or_wrong_schema(tmp_path: Path):
    assert load_invariant_index(tmp_path) == []

    path = tmp_path / INVARIANT_INDEX_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("schema_version: 99\ninvariants: [{id: x}]\n", encoding="utf-8")
    assert load_invariant_index(tmp_path) == []

    path.write_text("{{ not yaml", encoding="utf-8")
    assert load_invariant_index(tmp_path) == []


# ── forge index --invariants ─────────────────────────────────────────────────


def _forge_yaml(root: Path, extra: str = "") -> Path:
    return _write(
        root,
        "forge.yaml",
        "project:\n  name: demo\n  language: python\n" + extra,
    )


def test_index_invariants_command_writes_the_index(tmp_path: Path, capsys):
    config_path = _forge_yaml(tmp_path)
    _write(tmp_path, "docs/policy.md", _MARKED)

    code = cmd_index(argparse.Namespace(config=str(config_path), knowledge=False, invariants=True))

    assert code == 0
    assert str(tmp_path / INVARIANT_INDEX_PATH) in capsys.readouterr().out
    assert [entry["id"] for entry in load_invariant_index(tmp_path)] == ["summaries-advisory"]


def test_index_invariants_command_prints_diagnostics(tmp_path: Path, capsys):
    config_path = _forge_yaml(tmp_path)
    _write(tmp_path, "docs/broken.md", '<!-- forge-invariant id="open" -->\nRule.\n')

    code = cmd_index(argparse.Namespace(config=str(config_path), knowledge=False, invariants=True))

    assert code == 0
    assert "unterminated forge-invariant block" in capsys.readouterr().err


def test_index_invariants_command_honours_configured_sources(tmp_path: Path):
    config_path = _forge_yaml(
        tmp_path, "knowledge:\n  invariant_sources:\n    - 'policies/*.md'\n"
    )
    _write(tmp_path, "docs/policy.md", _MARKED)
    _write(
        tmp_path,
        "policies/rules.md",
        '<!-- forge-invariant id="only-this" -->\nRule.\n<!-- /forge-invariant -->\n',
    )

    cmd_index(argparse.Namespace(config=str(config_path), knowledge=False, invariants=True))

    assert [entry["id"] for entry in load_invariant_index(tmp_path)] == ["only-this"]


def test_index_without_invariants_flag_leaves_the_invariant_index_alone(tmp_path: Path):
    config_path = _forge_yaml(tmp_path)
    _write(tmp_path, "docs/policy.md", _MARKED)

    cmd_index(argparse.Namespace(config=str(config_path), knowledge=False, invariants=False))

    assert not (tmp_path / INVARIANT_INDEX_PATH).exists()
