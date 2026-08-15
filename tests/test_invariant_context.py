"""Selective invariant injection through context assembly (#1875).

Covers the config gate, the plan/dev capsule path, the conservative fallback,
the review asymmetry, the manifest's included/dropped/uncertain visibility, the
preflight exclusion, and the coordinator phase seam into the audit record.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from coord_test_helpers import patch_gate_shell

from tests.coord_test_helpers import (
    APPROVE_REVIEW,
    PREFLIGHT_PROCEED_MEDIUM,
    _make_agent_result,
    _make_plan_config,
    _make_task,
    _shell_with_gate,
)
from theforge.config.load import load_config
from theforge.config.types import KnowledgeConfig
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.engine import run_task
from theforge.invariant_index import rebuild_invariant_index
from theforge.task.context_assembler import (
    ContextAssembler,
    ContextItem,
    ContextPack,
    _included_ids,
)
from theforge.task.invariant_selector import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    INVARIANT_KIND,
    RENDER_CAPSULE,
    RENDER_SOURCE_SECTION,
    select_invariants,
)
from theforge.task.prior_run_selector import PRIOR_RUN_KIND

_GLOBS = ("*.md", "docs/**/*.md", "**/CONVENTIONS.md")

_POLICY = """# Project policy

## Coordinator rules

Introductory prose that only the broad rendering should reach.

<!-- forge-invariant id="coordinator-pure"
     scope="area:coordinator phase:plan,dev,review files:src/coordinator/*.py"
     enforcement="review" -->
The coordinator is pure Python; no model decides retry or escalation.
<!-- /forge-invariant -->

## Schema rules

<!-- forge-invariant id="schema-boundary"
     scope="area:schema phase:plan,dev,review files:src/schemas.py"
     enforcement="gate" -->
The review schema is the integrity boundary; do not relax cross-validation.
<!-- /forge-invariant -->

## Prompt rules

<!-- forge-invariant id="ac-authoritative"
     scope="area:prompts phase:plan,dev,review" enforcement="review" -->
Acceptance criteria are authoritative; notes are advisory hints.
<!-- /forge-invariant -->

## Review rules

<!-- forge-invariant id="review-only" scope="area:review phase:review" enforcement="review" -->
Reviewers evaluate the commit, not the plan.
<!-- /forge-invariant -->
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "policy.md").write_text(_POLICY, encoding="utf-8")
    rebuild_invariant_index(tmp_path, _GLOBS)
    return tmp_path


def _assembler(root: Path, **kwargs) -> ContextAssembler:
    return ContextAssembler(root, invariant_context=True, **kwargs)


def _ids(manifest_section: list[dict]) -> list[str]:
    return [item["id"] for item in manifest_section]


# ── Config ───────────────────────────────────────────────────────────────────


def test_invariant_context_is_off_by_default():
    assert KnowledgeConfig().invariant_context is False


def test_config_parses_the_gate_and_the_source_globs(tmp_path: Path):
    path = tmp_path / "forge.yaml"
    path.write_text(
        "project:\n  name: demo\n  language: python\n"
        "knowledge:\n"
        "  invariant_context: true\n"
        "  invariant_sources:\n"
        "    - 'docs/**/*.md'\n"
        "    - 'POLICY.md'\n",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.knowledge.invariant_context is True
    assert config.knowledge.invariant_sources == ("docs/**/*.md", "POLICY.md")


def test_config_defaults_keep_the_portable_source_globs(tmp_path: Path):
    path = tmp_path / "forge.yaml"
    path.write_text("project:\n  name: demo\n  language: python\n", encoding="utf-8")

    config = load_config(path)

    assert config.knowledge.invariant_context is False
    # Layout-neutral: naming a docs/ directory would presume one project's shape.
    assert config.knowledge.invariant_sources == ("**/*.md",)


@pytest.mark.parametrize(
    "block,message",
    [
        ("knowledge:\n  invariant_context: yes-please\n", "must be a bool"),
        ("knowledge:\n  invariant_sources: 'docs/*.md'\n", "must be a list"),
        ("knowledge:\n  invariant_sources:\n    - 7\n", "must be non-empty strings"),
    ],
)
def test_config_rejects_malformed_invariant_settings(tmp_path: Path, block: str, message: str):
    path = tmp_path / "forge.yaml"
    path.write_text("project:\n  name: demo\n  language: python\n" + block, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_config(path)


def test_from_config_propagates_the_gate(tmp_path: Path):
    path = tmp_path / "forge.yaml"
    path.write_text(
        "project:\n  name: demo\n  language: python\nknowledge:\n  invariant_context: true\n",
        encoding="utf-8",
    )

    assert ContextAssembler.from_config(load_config(path)).invariant_context is True


def test_disabled_assembler_never_reads_the_index(project: Path):
    pack = ContextAssembler(project).assemble(
        phase="dev", story_text="coordinator retry", file_list=["src/coordinator/engine.py"]
    )

    assert pack.invariant_context["enabled"] is False
    assert "coordinator is pure Python" not in pack.content


# ── Selective injection for plan/dev ─────────────────────────────────────────


def test_dev_receives_a_capsule_when_file_scope_matches(project: Path):
    pack = _assembler(project).assemble(
        phase="dev",
        story_text="retry loop",
        file_list=["src/coordinator/engine.py"],
        budget=400,
    )

    manifest = pack.invariant_context
    assert manifest["enabled"] is True
    included = {item["id"]: item for item in manifest["included"]}
    assert included["coordinator-pure"]["rendering_mode"] == RENDER_CAPSULE
    assert included["coordinator-pure"]["scope_confidence"] == CONFIDENCE_HIGH
    assert "file_scope_match" in included["coordinator-pure"]["reason"]
    assert "The coordinator is pure Python" in pack.content
    # A capsule is the marked region only — not the surrounding section.
    assert "Introductory prose" not in pack.content


def test_a_capsule_from_a_multi_line_marker_renders_only_the_rule(project: Path):
    """`coordinator-pure` wraps its opening marker, as the convention's example does.

    The rendered capsule used to trail the scope and enforcement attribute lines
    into the prompt, and the digest computed over the real body then reported the
    unchanged source as stale.
    """
    selection = select_invariants(
        project, phase="dev", story_text="retry loop", file_list=["src/coordinator/engine.py"]
    )
    capsule = next(c for c in selection.candidates if c.invariant_id == "coordinator-pure")

    assert capsule.rendering_mode == RENDER_CAPSULE
    body = capsule.content.split("\n\n", 1)[1]
    assert body == "The coordinator is pure Python; no model decides retry or escalation."
    assert capsule.source_digest_matches is True

    pack = _assembler(project).assemble(
        phase="dev", story_text="retry loop", file_list=["src/coordinator/engine.py"], budget=400
    )
    assert "source regions changed" not in pack.invariant_context["note"]


def test_confident_file_scope_mismatch_is_the_only_drop(project: Path):
    manifest = (
        _assembler(project)
        .assemble(
            phase="dev",
            story_text="retry loop",
            file_list=["src/coordinator/engine.py"],
            budget=400,
        )
        .invariant_context
    )

    dropped = {item["id"]: item["reason"] for item in manifest["dropped"]}
    assert "files_out_of_scope" in dropped["schema-boundary"]
    assert "phase_not_applicable" in dropped["review-only"]


def test_included_items_are_visible_in_the_context_manifest(project: Path):
    pack = _assembler(project).assemble(
        phase="dev", story_text="retry loop", file_list=["src/coordinator/engine.py"], budget=400
    )

    entry = next(item for item in pack.included if item.kind == INVARIANT_KIND)
    assert entry.source.endswith("#coordinator-pure") or "#" in entry.source
    assert entry.included is True


def test_inclusion_is_recorded_against_the_selector_identity_not_the_path(tmp_path: Path):
    """A project's own file naming must not move an invariant into `dropped`.

    The manifest recovered the invariant id by splitting the display `source`,
    so a source path containing the delimiter injected the rule into the prompt
    while reporting it dropped under budget pressure.
    """
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "rules#v2.md").write_text(
        "## Rules\n\n"
        '<!-- forge-invariant id="hash-path" scope="area:coordinator phase:dev" -->\n'
        "A rule in a file whose name contains a hash.\n"
        "<!-- /forge-invariant -->\n",
        encoding="utf-8",
    )
    rebuild_invariant_index(tmp_path, _GLOBS)

    pack = _assembler(tmp_path).assemble(
        phase="dev",
        story_text="coordinator retry",
        file_list=["src/coordinator/engine.py"],
        budget=400,
    )

    manifest = pack.invariant_context
    assert "a hash" in pack.content
    assert _ids(manifest["included"]) == ["hash-path"]
    assert manifest["dropped"] == []


def test_included_ids_reads_identity_off_the_item_never_off_the_source():
    """Pins the contract the hash-path bug broke, for both indexed kinds."""
    items = [
        ContextItem(
            source="docs/rules#v2.md#hash-path",
            kind=INVARIANT_KIND,
            required=False,
            lines=1,
            content="x",
            reason="r",
            item_id="hash-path",
        ),
        ContextItem(
            source="knowledge:run:with:colons",
            kind=PRIOR_RUN_KIND,
            required=False,
            lines=1,
            content="x",
            reason="r",
            item_id="run:with:colons",
        ),
        # Renders no indexed entity, so it contributes no id.
        ContextItem(
            source="CONVENTIONS.md",
            kind="claude_invariants",
            required=True,
            lines=1,
            content="x",
            reason="r",
        ),
    ]

    assert _included_ids(items, INVARIANT_KIND) == {"hash-path"}
    assert _included_ids(items, PRIOR_RUN_KIND) == {"run:with:colons"}
    assert _included_ids(items, "claude_invariants") == set()


# ── Conservative fallback ────────────────────────────────────────────────────


def test_area_only_scope_without_a_match_falls_back_to_the_broader_source(project: Path):
    manifest = (
        _assembler(project)
        .assemble(
            phase="dev",
            story_text="retry loop",
            file_list=["src/coordinator/engine.py"],
            budget=400,
        )
        .invariant_context
    )

    uncertain = {item["id"]: item for item in manifest["uncertain"]}
    assert "ac-authoritative" in uncertain
    assert uncertain["ac-authoritative"]["rendering_mode"] == RENDER_SOURCE_SECTION
    assert uncertain["ac-authoritative"]["scope_confidence"] == CONFIDENCE_LOW
    assert "area_unmatched" in uncertain["ac-authoritative"]["reason"]
    # Uncertain means included-broadly, never dropped.
    assert "ac-authoritative" in _ids(manifest["included"])


def test_no_file_list_widens_every_rule_instead_of_guessing(project: Path):
    pack = _assembler(project).assemble(
        phase="plan", story_text="an unclassifiable story", file_list=None, budget=600
    )
    manifest = pack.invariant_context

    assert set(_ids(manifest["included"])) == {
        "coordinator-pure",
        "schema-boundary",
        "ac-authoritative",
    }
    assert all(item["rendering_mode"] == RENDER_SOURCE_SECTION for item in manifest["included"])
    assert set(_ids(manifest["uncertain"])) == set(_ids(manifest["included"]))
    assert "no_file_list_to_match_scope" in manifest["included"][0]["reason"]
    assert "Introductory prose" in pack.content


def test_unparsable_scope_metadata_widens_rather_than_narrows(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "policy.md").write_text(
        "## Odd\n\n"
        '<!-- forge-invariant id="odd-scope" scope="module:coordinator files:src/other/*.py" -->\n'
        "A rule with a scope key nobody parsed.\n"
        "<!-- /forge-invariant -->\n",
        encoding="utf-8",
    )
    rebuild_invariant_index(tmp_path, _GLOBS)

    manifest = (
        _assembler(tmp_path)
        .assemble(phase="dev", story_text="story", file_list=["src/app.py"], budget=400)
        .invariant_context
    )

    assert _ids(manifest["included"]) == ["odd-scope"]
    assert "unparsed_scope" in manifest["included"][0]["reason"]
    assert _ids(manifest["uncertain"]) == ["odd-scope"]


def test_note_separates_nothing_indexed_from_something_withheld(project: Path):
    unmarked = project / "unmarked"
    unmarked.mkdir()
    empty = (
        _assembler(unmarked)
        .assemble(phase="dev", story_text="story", file_list=["src/app.py"])
        .invariant_context
    )
    assert "no project invariants are indexed" in empty["note"]

    populated = (
        _assembler(project)
        .assemble(
            phase="dev", story_text="story", file_list=["src/coordinator/engine.py"], budget=400
        )
        .invariant_context
    )
    assert "indexed invariants included" in populated["note"]
    assert "scope confidence was low" in populated["note"]


def test_budget_pressure_is_named_distinctly_from_scope_exclusion(project: Path):
    manifest = (
        _assembler(project)
        .assemble(
            phase="plan", story_text="story", file_list=["src/coordinator/engine.py"], budget=1
        )
        .invariant_context
    )

    assert manifest["included"] == []
    assert {item["reason"] for item in manifest["dropped"]} >= {"budget_pressure"}


def test_stale_source_regions_are_reported_not_suppressed(project: Path):
    policy = project / "docs" / "policy.md"
    policy.write_text(
        policy.read_text(encoding="utf-8").replace(
            "The coordinator is pure Python; no model decides retry or escalation.",
            "The coordinator is pure Python. Amended after the index was built.",
        ),
        encoding="utf-8",
    )

    pack = _assembler(project).assemble(
        phase="dev", story_text="retry", file_list=["src/coordinator/engine.py"], budget=400
    )
    manifest = pack.invariant_context

    entry = next(item for item in manifest["included"] if item["id"] == "coordinator-pure")
    assert entry["source_digest_matches"] is False
    # The source document stays authoritative: the amended text is what is shown.
    assert "Amended after the index was built" in pack.content
    assert "source regions changed since the index was built" in manifest["note"]


# ── Review is deliberately broader ───────────────────────────────────────────


def test_review_gets_broader_context_than_dev(project: Path):
    dev = (
        _assembler(project)
        .assemble(
            phase="dev", story_text="retry", file_list=["src/coordinator/engine.py"], budget=900
        )
        .invariant_context
    )
    review = (
        _assembler(project)
        .assemble(
            phase="review", story_text="retry", file_list=["src/coordinator/engine.py"], budget=900
        )
        .invariant_context
    )

    assert set(_ids(dev["included"])) < set(_ids(review["included"]))
    assert "schema-boundary" in _ids(review["included"])
    assert all(item["rendering_mode"] == RENDER_SOURCE_SECTION for item in review["included"])
    override = next(item for item in review["included"] if item["id"] == "schema-boundary")
    assert "broad_phase_override" in override["reason"]


def test_review_still_honours_phase_scope(project: Path):
    review = (
        _assembler(project)
        .assemble(
            phase="review", story_text="retry", file_list=["src/coordinator/engine.py"], budget=900
        )
        .invariant_context
    )

    assert "review-only" in _ids(review["included"])

    plan = (
        _assembler(project)
        .assemble(phase="plan", story_text="retry", file_list=None, budget=900)
        .invariant_context
    )
    assert "review-only" not in _ids(plan["included"])


# ── Preflight exclusion ──────────────────────────────────────────────────────


def test_preflight_receives_no_invariant_prose(project: Path):
    pack = _assembler(project).assemble(
        phase="preflight",
        story_text="coordinator retry escalation",
        file_list=["src/coordinator/engine.py"],
        budget=900,
    )

    manifest = pack.invariant_context
    assert manifest["included"] == []
    assert manifest["uncertain"] == []
    assert "not injected in the preflight phase" in manifest["note"]
    assert "The coordinator is pure Python" not in pack.content
    assert all(item.kind != INVARIANT_KIND for item in pack.included)


def test_selector_refuses_preflight_without_reading_the_index(project: Path):
    selection = select_invariants(
        project, phase="preflight", story_text="story", file_list=["src/coordinator/engine.py"]
    )

    assert selection.phase_eligible is False
    assert selection.candidates == ()
    assert selection.entry_count == 0


# ── Coordinator seam ─────────────────────────────────────────────────────────


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.plan_flow.run_agent")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch_gate_shell()
def test_invariant_context_flows_through_phase_seams_into_audit_state(
    mock_shell, mock_dev, mock_preflight, mock_plan, mock_pool, tmp_path
):
    """The gate must survive the whole phase flow, and preflight must stay clean."""
    config = replace(
        _make_plan_config(tmp_path),
        knowledge=KnowledgeConfig(invariant_context=True),
    )
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir()
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "policy.md").write_text(_POLICY, encoding="utf-8")
    rebuild_invariant_index(tmp_path, _GLOBS)

    mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
    mock_preflight.return_value = _make_agent_result(
        success=True, output=PREFLIGHT_PROCEED_MEDIUM, profile_name="preflight"
    )
    mock_plan.return_value = _make_agent_result(
        success=True,
        output=(
            "```yaml\n"
            "plan:\n"
            "  approach: Update the thing.\n"
            "  steps:\n"
            "    - id: 1\n"
            "      description: Update implementation\n"
            "      files:\n"
            "        - src/coordinator/engine.py\n"
            "      action: modify\n"
            "      details: Apply the behavior change.\n"
            "```\n"
        ),
        profile_name="plan",
    )
    mock_dev.return_value = _make_agent_result(success=True, output="Done.", profile_name="dev")
    mock_pool.return_value = [
        _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
    ]

    result = run_task(config, task)

    assert result.success is True
    captured: dict[str, ContextPack] = {}
    for entry in result.state.context_manifests:
        captured.setdefault(entry["phase"], entry["manifest"])

    preflight = captured["preflight"].invariant_context
    assert preflight["enabled"] is True
    assert preflight["included"] == []
    assert "The coordinator is pure Python" not in captured["preflight"].content

    dev = captured["dev"].invariant_context
    assert dev["enabled"] is True
    assert "coordinator-pure" in _ids(dev["included"])

    review = captured["review"].invariant_context
    assert set(_ids(dev["included"])) <= set(_ids(review["included"]))

    # The decision has to survive into the audit record, not just into state.
    record = generate_audit_log(config, task, result)
    by_phase = {entry["phase"]: entry for entry in record["context_manifests"]}
    assert "coordinator-pure" in _ids(by_phase["dev"]["invariant_context"]["included"])
    assert by_phase["preflight"]["invariant_context"]["included"] == []
    assert yaml.safe_dump(record["context_manifests"])
