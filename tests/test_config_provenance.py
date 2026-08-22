"""Configuration identity in the run record (#2056).

A run outcome recorded without the configuration that produced it cannot be
compared against another run's outcome. These tests cover the whole seam the
provenance travels along: derived at ``load_config``, written into the per-run
audit record (including the load-vs-finish comparison that makes a mid-flight
edit visible), lifted onto older records by the reader, classified by RCA, and
surfaced by ``forge explain``.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from unittest.mock import patch

import yaml

from theforge.cli import explain
from theforge.cli.shared import _apply_dev_model_override
from theforge.config import load_config
from theforge.config.provenance import (
    VALUE_SOURCE_CLI_OVERRIDE,
    VALUE_SOURCE_DERIVED,
    VALUE_SOURCE_ENVIRONMENT,
    build_provenance,
    resolved_config_payload,
    resolved_config_sha256,
)
from theforge.coordinator import audit_substrate
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.sprint.rca import build_sprint_rca
from theforge.task import TaskStory

_auth_ok = patch("theforge.config.load.check_agent_auth", return_value=(True, ""))
_import_ok = patch("importlib.import_module")

_BASE_YAML = """
project: provenance-test
models:
  - claude/sonnet
"""


def _write_config(root: Path, text: str = _BASE_YAML) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "forge.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _load(path: Path):
    with _auth_ok, _import_ok:
        return load_config(path)


# ── Step 1: identity derived at load ──────────────────────────────────────────


def test_equivalent_configs_share_a_resolved_digest(tmp_path: Path) -> None:
    """Two loads of an equivalent resolved configuration digest identically.

    The digest identifies the *configuration*, not the checkout it was read
    from, so a worktree and its parent must agree — otherwise no reader could
    compare two runs of the same project.
    """
    a = _load(_write_config(tmp_path / "a"))
    b = _load(_write_config(tmp_path / "b"))

    assert a.provenance is not None
    assert a.provenance.resolved_sha256 == b.provenance.resolved_sha256
    # ...but they are honest about where each came from.
    assert a.provenance.source_path != b.provenance.source_path
    assert a.provenance.source_sha256 == b.provenance.source_sha256


def test_meaningful_config_change_changes_the_resolved_digest(tmp_path: Path) -> None:
    """A configuration difference the run can feel changes the digest."""
    before = _load(_write_config(tmp_path / "a"))
    after = _load(
        _write_config(
            tmp_path / "b",
            _BASE_YAML + "\nvalidation:\n  gate_command: make other-gate\n",
        )
    )

    assert before.provenance.resolved_sha256 != after.provenance.resolved_sha256


def test_secrets_do_not_participate_in_the_resolved_digest(tmp_path: Path) -> None:
    """Rotating a secret is not a configuration change, and the digest is not an oracle."""
    root = tmp_path / "proj"
    config_path = _write_config(root)
    plain = _load(config_path)

    (root / ".forge").mkdir(parents=True, exist_ok=True)
    (root / ".forge" / ".env").write_text("SOME_TOKEN=abc123\n", encoding="utf-8")
    with_secret = _load(config_path)

    assert with_secret.secrets  # the secret really was loaded
    assert with_secret.provenance.resolved_sha256 == plain.provenance.resolved_sha256


def test_resolved_digest_is_stable_across_repeated_computation(tmp_path: Path) -> None:
    config = _load(_write_config(tmp_path / "a"))
    assert resolved_config_sha256(config) == resolved_config_sha256(config)


def test_source_attribution_does_not_change_the_resolved_digest(tmp_path: Path) -> None:
    """Config-source metadata lives off-digest inside ConfigProvenance."""
    config_path = _write_config(tmp_path / "a")
    config = _load(config_path)

    attributed = build_provenance(
        config,
        config_path,
        yaml_leaf_paths=("knowledge.prior_run_context",),
        environment_sources={"notifications.ntfy.url": VALUE_SOURCE_ENVIRONMENT},
        derived_path_prefixes=("plan",),
    )

    assert attributed.resolved_sha256 == config.provenance.resolved_sha256


# ── Secret-derived values must not reach the digest ───────────────────────────

_NTFY_YAML = _BASE_YAML + "\nnotifications:\n  backend: ntfy\n"


def _load_with_env_secret(root: Path, value: str, yaml_text: str = _NTFY_YAML):
    """Load a config whose NTFY_URL comes from ``.forge/.env`` (project secrets)."""
    config_path = _write_config(root, yaml_text)
    (root / ".forge").mkdir(parents=True, exist_ok=True)
    (root / ".forge" / ".env").write_text(f"NTFY_URL={value}\n", encoding="utf-8")
    return _load(config_path)


def test_rotating_a_secret_sourced_ntfy_url_is_not_a_configuration_change(
    tmp_path: Path,
) -> None:
    """NTFY_URL resolves into notifications.ntfy.url — a bearer credential.

    Excluding only the top-level ``secrets`` dict is not enough: the loader lands
    the secret in a typed field, where digesting it would both fingerprint the
    secret and make two runs of one configuration look different.
    """
    first = _load_with_env_secret(tmp_path / "a", "https://ntfy.sh/topic-alpha-9f2")
    second = _load_with_env_secret(tmp_path / "b", "https://ntfy.sh/topic-beta-77c")

    assert first.notifications.ntfy is not None  # the secret really was resolved in
    assert first.notifications.ntfy.url != second.notifications.ntfy.url
    assert first.provenance.resolved_sha256 == second.provenance.resolved_sha256


def test_rotating_an_environment_sourced_ntfy_url_is_not_a_configuration_change(
    tmp_path: Path, monkeypatch
) -> None:
    """Same guarantee when NTFY_URL comes from the process environment.

    This one is not in ``config.secrets`` at all, so only the path-based
    redaction can catch it.
    """
    monkeypatch.setenv("NTFY_URL", "https://ntfy.sh/topic-alpha-9f2")
    first = _load(_write_config(tmp_path / "a", _NTFY_YAML))
    monkeypatch.setenv("NTFY_URL", "https://ntfy.sh/topic-beta-77c")
    second = _load(_write_config(tmp_path / "b", _NTFY_YAML))

    assert first.notifications.ntfy.url != second.notifications.ntfy.url
    assert first.provenance.resolved_sha256 == second.provenance.resolved_sha256


def test_the_resolved_payload_never_contains_a_secret_value(tmp_path: Path) -> None:
    """Direct check on the digested payload, not just on digest equality."""
    secret = "https://ntfy.sh/topic-alpha-9f2"
    config = _load_with_env_secret(tmp_path / "a", secret)

    payload = json.dumps(resolved_config_payload(config))

    assert secret not in payload
    assert "topic-alpha-9f2" not in payload


def test_enabling_a_notification_backend_is_still_a_configuration_change(
    tmp_path: Path,
) -> None:
    """Redaction preserves presence: the URL is hidden, the backend is not."""
    without = _load(_write_config(tmp_path / "a"))
    with_ntfy = _load_with_env_secret(tmp_path / "b", "https://ntfy.sh/topic-alpha-9f2")

    assert without.provenance.resolved_sha256 != with_ntfy.provenance.resolved_sha256


def test_environment_sourced_notification_value_records_redacted_environment_source(
    tmp_path: Path,
) -> None:
    config = _load_with_env_secret(tmp_path / "a", "https://ntfy.sh/topic-alpha-9f2")

    assert config.provenance is not None
    assert (
        config.provenance.resolved_value_sources["notifications.ntfy.url"]
        == VALUE_SOURCE_ENVIRONMENT
    )
    assert config.provenance.resolved_values["notifications.ntfy.url"] == "<redacted>"
    assert (
        config.provenance.resolved_value_sources["notifications.backends[0].url"]
        == VALUE_SOURCE_ENVIRONMENT
    )


def test_derived_plan_model_is_labeled_derived(tmp_path: Path) -> None:
    """A plan profile derived from `models:` is not a forge.yaml leaf."""
    config = _load(_write_config(tmp_path / "proj"))

    assert config.provenance is not None
    assert config.provenance.resolved_value_sources["plan.ref.model"] == VALUE_SOURCE_DERIVED


def test_a_secret_landing_in_any_field_is_scrubbed(tmp_path: Path) -> None:
    """The guard is not a notifications special case.

    Any value carrying a loaded secret is redacted wherever it sits, so a future
    field that resolves a secret cannot silently start fingerprinting it.
    """
    loaded = _load(_write_config(tmp_path / "proj"))

    def _with(token: str):
        return dataclasses.replace(
            loaded,
            secrets={"SOME_TOKEN": token},
            validation=dataclasses.replace(
                loaded.validation, gate_command=f"make gate TOKEN={token}"
            ),
        )

    alpha = _with("s3cret-value-alpha")
    beta = _with("s3cret-value-beta")

    assert resolved_config_sha256(alpha) == resolved_config_sha256(beta)
    assert "s3cret-value-alpha" not in json.dumps(resolved_config_payload(alpha))


def test_a_short_secret_does_not_redact_unrelated_values(tmp_path: Path) -> None:
    """Substring scrubbing is length-gated: a two-character secret must not
    redact half the configuration and flatten every digest together."""
    loaded = _load(_write_config(tmp_path / "proj"))
    short = dataclasses.replace(loaded, secrets={"TINY": "ma"})

    payload = json.dumps(resolved_config_payload(short))

    assert "make gate" in payload


# ── Step 2: the identity reaches the audit record ─────────────────────────────


def _result(state: CoordinatorState) -> CoordinatorResult:
    state.run_id = "cfgprov00001"
    state.started_at = "2026-07-29T00:00:00+00:00"
    return CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="done")


def _audit_for(config, tmp_path: Path) -> dict:
    story = tmp_path / "spec.md"
    story.write_text("# spec", encoding="utf-8")
    task = TaskStory(name="Test", slug="issue-2056", story_path=story)
    return generate_audit_log(config, task, _result(CoordinatorState()))


def test_audit_record_names_the_configuration_the_run_used(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path / "proj")
    config = _load(config_path)

    record = _audit_for(config, tmp_path)

    block = record["configuration"]
    assert block["resolved_sha256"] == config.provenance.resolved_sha256
    assert block["source_path"] == str(config_path.resolve())
    assert block["source_sha256"] == config.provenance.source_sha256
    assert block["changed_during_run"] is False
    assert block["finish_read_error"] is None


def test_mid_flight_config_edit_is_visible_from_the_audit_record_alone(tmp_path: Path) -> None:
    """The join this issue asks forge to record: load-time vs finish-time source.

    No ``git log``, no correlation against anything outside the audit trail —
    the record itself states that the configuration moved under the run.
    """
    config_path = _write_config(tmp_path / "proj")
    config = _load(config_path)

    # The operator edits forge.yaml while the run is still executing.
    config_path.write_text(
        _BASE_YAML + "\nsandbox:\n  capability_profile: xcode\n", encoding="utf-8"
    )

    record = _audit_for(config, tmp_path)

    block = record["configuration"]
    assert block["changed_during_run"] is True
    assert block["source_sha256"] == config.provenance.source_sha256
    assert block["source_sha256_at_finish"] != block["source_sha256"]


def test_two_records_are_comparable_on_configuration(tmp_path: Path) -> None:
    """A reader can tell same-config runs from different-config runs."""
    same_a = _audit_for(_load(_write_config(tmp_path / "a")), tmp_path)
    same_b = _audit_for(_load(_write_config(tmp_path / "b")), tmp_path)
    different = _audit_for(
        _load(_write_config(tmp_path / "c", _BASE_YAML + "\nvalidation:\n  gate_timeout: 4242\n")),
        tmp_path,
    )

    assert same_a["configuration"]["resolved_sha256"] == same_b["configuration"]["resolved_sha256"]
    assert (
        same_a["configuration"]["resolved_sha256"] != different["configuration"]["resolved_sha256"]
    )


def test_unreadable_source_at_finish_records_the_error_not_a_verdict(tmp_path: Path) -> None:
    """A config file deleted mid-run must not read as "unchanged"."""
    config_path = _write_config(tmp_path / "proj")
    config = _load(config_path)
    config_path.unlink()

    block = _audit_for(config, tmp_path)["configuration"]

    assert block["changed_during_run"] is None
    assert block["source_sha256_at_finish"] is None
    assert block["finish_read_error"]
    # The load-time identity survives — the run is still attributable.
    assert block["resolved_sha256"] == config.provenance.resolved_sha256


def test_config_without_source_identity_records_explicit_nulls(tmp_path: Path) -> None:
    """A config that never came from a file says so, rather than dropping the key."""
    loaded = _load(_write_config(tmp_path / "proj"))
    config = dataclasses.replace(loaded, provenance=None)

    block = _audit_for(config, tmp_path)["configuration"]

    assert block["source_path"] is None
    assert block["source_sha256"] is None
    assert block["source_sha256_at_finish"] is None
    assert block["changed_during_run"] is None
    assert block["finish_read_error"] is None
    # The resolved configuration is still identifiable — it is a property of the
    # config object, not of the file it may or may not have come from.
    assert block["resolved_sha256"] == loaded.provenance.resolved_sha256


def test_resolved_digest_reflects_run_time_overrides(tmp_path: Path) -> None:
    """CLI overrides rebuild the config after load; the record must name what ran.

    A digest copied from load time would say two runs shared a configuration when
    one of them was re-pointed at a different dev model on the command line.
    """
    loaded = _load(_write_config(tmp_path / "proj"))
    overridden = dataclasses.replace(
        loaded, validation=dataclasses.replace(loaded.validation, gate_command="make other-gate")
    )

    baseline = _audit_for(loaded, tmp_path)["configuration"]
    after = _audit_for(overridden, tmp_path)["configuration"]

    assert after["resolved_sha256"] != baseline["resolved_sha256"]
    # Same source file, though — the override is not a forge.yaml edit.
    assert after["source_sha256"] == baseline["source_sha256"]
    assert after["changed_during_run"] is False


def test_cli_override_value_is_recorded_with_cli_override_source(tmp_path: Path) -> None:
    loaded = _load(_write_config(tmp_path / "proj"))
    overridden = _apply_dev_model_override(loaded, "anthropic/claude-opus-4-6")

    block = _audit_for(overridden, tmp_path)["configuration"]["recorded_values"]
    entry = block["entries"]["dev_profile.model"]

    assert entry["value"] == "claude-opus-4-6"
    assert entry["source"] == VALUE_SOURCE_CLI_OVERRIDE


def test_ambiguous_recorded_config_keys_persist_lossless_path_tokens(tmp_path: Path) -> None:
    config = _load(_write_config(tmp_path / "proj"))
    path_tokens = config.provenance.resolved_value_path_tokens
    ambiguous_key = next(
        path
        for path, tokens in path_tokens.items()
        if path.startswith("model_registry.") and path.endswith(".provider") and len(tokens) >= 3
    )

    block = _audit_for(config, tmp_path)["configuration"]["recorded_values"]
    entry = block["entries"][ambiguous_key]

    assert entry["path_tokens"] == list(path_tokens[ambiguous_key])
    assert "path_tokens" not in block["entries"]["project"]


# ── Step 3: schema version + migration ────────────────────────────────────────


def test_new_records_are_written_at_the_current_schema_version(tmp_path: Path) -> None:
    """New records carry the writer's current version — v16 introduced the
    configuration block; later bumps must not silently write a stale version."""
    config = _load(_write_config(tmp_path / "proj"))
    current = audit_substrate.CURRENT_RECORD_SCHEMA_VERSION
    assert current >= 16, "the configuration block landed in v16"
    assert _audit_for(config, tmp_path)["schema_version"] == current


def test_v15_records_migrate_to_a_null_configuration_block() -> None:
    """Historical records say "cannot name it", not "no configuration change"."""
    migrated = audit_substrate._migrate_v15_to_v16({"run_id": "old", "task": {"slug": "x"}})

    assert migrated["configuration"] is None
    assert migrated["run_id"] == "old"


def test_v15_to_v16_does_not_clobber_a_present_block() -> None:
    existing = {"resolved_sha256": "abc"}
    migrated = audit_substrate._migrate_v15_to_v16({"configuration": existing})
    assert migrated["configuration"] is existing


def test_v34_records_migrate_to_absent_recorded_config_values() -> None:
    migrated = audit_substrate._migrate_v34_to_v35(
        {"configuration": {"resolved_sha256": "abc", "source_sha256": "def"}}
    )

    assert migrated["configuration"]["recorded_values"] is None


def test_v35_records_migrate_to_v36_without_rewriting_recorded_config_entries() -> None:
    record = {
        "configuration": {
            "recorded_values": {
                "format_version": 1,
                "entries": {
                    "model_registry.google/gemini-2.5-pro/api.provider": {
                        "value": "google",
                        "source": "default",
                    }
                },
            }
        }
    }

    assert audit_substrate._migrate_v35_to_v36(record) is record


def test_configuration_block_round_trips_through_the_substrate(tmp_path: Path) -> None:
    """Seam check: the block survives write → index → read."""
    project = tmp_path / "proj"
    config = _load(_write_config(project))
    record = _audit_for(config, tmp_path)

    audit_substrate.seed_records(project, [record])
    conn = audit_substrate.create_or_open(project)
    try:
        stored = audit_substrate.latest_record_for(conn, run_id=record["run_id"])
    finally:
        conn.close()

    assert stored is not None
    assert stored["configuration"]["resolved_sha256"] == config.provenance.resolved_sha256
    assert (
        stored["configuration"]["recorded_values"]["entries"]["project"]["source"] == "forge.yaml"
    )


def test_recorded_config_lookup_reads_only_the_stored_record(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    config = _load(
        _write_config(project, _BASE_YAML + "\nknowledge:\n  prior_run_context: true\n")
    )
    record = _audit_for(config, tmp_path)

    audit_substrate.seed_records(project, [record])
    conn = audit_substrate.create_or_open(project)
    try:
        stored = audit_substrate.latest_record_for(conn, run_id=record["run_id"])
    finally:
        conn.close()

    lookup = audit_substrate.lookup_recorded_configuration_value(
        stored,
        "knowledge.prior_run_context",
    )

    assert lookup["status"] == "resolved"
    assert lookup["value"] is True
    assert lookup["source"] == "forge.yaml"


def test_lookup_reports_current_schema_indexed_list_paths_as_resolved(tmp_path: Path) -> None:
    record = _audit_for(_load(_write_config(tmp_path / "proj")), tmp_path)
    entries = record["configuration"]["recorded_values"]["entries"]
    entries.update(
        {
            "review_pool[0].model": {"value": "claude-opus-4-6", "source": "forge.yaml"},
            "agents[0].model": {"value": "claude-sonnet-4-5", "source": "forge.yaml"},
            "conventions_soft[0]": {"value": "Prefer focused modules.", "source": "forge.yaml"},
            "notifications.backends[0].url": {"value": "<redacted>", "source": "environment"},
        }
    )

    for key in (
        "review_pool[0].model",
        "agents[0].model",
        "conventions_soft[0]",
        "notifications.backends[0].url",
    ):
        lookup = audit_substrate.lookup_recorded_configuration_value(record, key)
        assert lookup["status"] == "resolved"


def test_lookup_uses_recorded_path_tokens_for_ambiguous_current_schema_keys(
    tmp_path: Path,
) -> None:
    record = _audit_for(_load(_write_config(tmp_path / "proj")), tmp_path)
    entries = record["configuration"]["recorded_values"]["entries"]
    ambiguous_key = next(
        path
        for path, entry in entries.items()
        if path.startswith("model_registry.")
        and path.endswith(".provider")
        and "path_tokens" in entry
    )

    lookup = audit_substrate.lookup_recorded_configuration_value(record, ambiguous_key)

    assert lookup["status"] == "resolved"
    assert lookup["value"] == entries[ambiguous_key]["value"]


def test_lookup_reports_absent_for_digest_only_records() -> None:
    record = audit_substrate._migrate_record(
        {
            "schema_version": 34,
            "forge_version": "0.14.2",
            "configuration": {"resolved_sha256": "abc"},
        },
        from_version=34,
    )

    lookup = audit_substrate.lookup_recorded_configuration_value(
        record, "knowledge.prior_run_context"
    )

    assert lookup["status"] == "absent"


def test_lookup_reports_future_paths_as_uninterpreted(tmp_path: Path) -> None:
    config = _load(_write_config(tmp_path / "proj"))
    record = _audit_for(config, tmp_path)
    record["configuration"]["recorded_values"]["entries"]["future.config.flag"] = {
        "value": True,
        "source": "forge.yaml",
    }

    lookup = audit_substrate.lookup_recorded_configuration_value(record, "future.config.flag")

    assert lookup["status"] == "uninterpreted"
    assert lookup["value"] is True


# ── Step 4: RCA stops calling a configuration change a merge failure ──────────


def _sprint_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".forge" / "logs" / "issues-246"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_yaml(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _summary(story: dict) -> dict:
    return {
        "sprint": {
            "name": "test-sprint",
            "run_id": "6c83b3061455",
            "finished_at": "2026-07-29T03:00:00Z",
        },
        "stories": [story],
    }


def test_dirty_forge_yaml_merge_failure_classifies_as_configuration_change(
    tmp_path: Path,
) -> None:
    """The adopter case verbatim: the config change was named and called a merge problem."""
    d = _sprint_dir(tmp_path)
    _write_yaml(
        d / "sprint-summary.yaml",
        _summary(
            {
                "slug": "issue-246",
                "issue": 246,
                "outcome": "MERGE_FAILED",
                "error": "Uncommitted changes in project root: M forge.yaml",
            }
        ),
    )

    entry = build_sprint_rca(d / "sprint-summary.yaml")["stories"]["issue-246"]

    assert entry["primary_failure_class"] == "configuration_changed"
    action = " ".join(entry["recommended_next_actions"]).lower()
    assert "forge.yaml" in action
    assert "inspect #246's pr" not in action


def test_ordinary_merge_conflict_is_still_a_merge_failure(tmp_path: Path) -> None:
    """The new rule must be narrow: a real merge failure keeps its class."""
    d = _sprint_dir(tmp_path)
    _write_yaml(
        d / "sprint-summary.yaml",
        _summary(
            {
                "slug": "issue-246",
                "issue": 246,
                "outcome": "MERGE_FAILED",
                "error": "merge conflict in src/theforge/config/load.py",
            }
        ),
    )

    entry = build_sprint_rca(d / "sprint-summary.yaml")["stories"]["issue-246"]

    assert entry["primary_failure_class"] == "merge_failed"


def test_recorded_mid_flight_change_classifies_from_the_run_audit(tmp_path: Path) -> None:
    """The run's own provenance is sufficient evidence — no error text needed."""
    d = _sprint_dir(tmp_path)
    _write_yaml(
        d / "sprint-summary.yaml",
        _summary({"slug": "issue-246", "issue": 246, "outcome": "FAILED", "error": "boom"}),
    )
    _write_yaml(
        d / "issue-246" / "audit.yaml",
        {
            "configuration": {
                "source_path": "/proj/forge.yaml",
                "source_sha256": "aaa",
                "source_sha256_at_finish": "bbb",
                "resolved_sha256": "ccc",
                "changed_during_run": True,
            }
        },
    )

    entry = build_sprint_rca(d / "sprint-summary.yaml")["stories"]["issue-246"]

    assert entry["primary_failure_class"] == "configuration_changed"
    excerpts = " ".join(str(e.get("excerpt")) for e in entry["evidence"])
    assert "aaa -> bbb" in excerpts


def test_unchanged_configuration_does_not_fire_the_rule(tmp_path: Path) -> None:
    d = _sprint_dir(tmp_path)
    _write_yaml(
        d / "sprint-summary.yaml",
        _summary({"slug": "issue-246", "issue": 246, "outcome": "FAILED", "error": "boom"}),
    )
    _write_yaml(
        d / "issue-246" / "audit.yaml",
        {"configuration": {"changed_during_run": False, "resolved_sha256": "ccc"}},
    )

    entry = build_sprint_rca(d / "sprint-summary.yaml")["stories"]["issue-246"]

    assert entry["primary_failure_class"] != "configuration_changed"


# ── Step 5: explain surfaces it ───────────────────────────────────────────────


def test_explain_renders_the_configuration_identity(tmp_path: Path) -> None:
    config = _load(_write_config(tmp_path / "proj"))
    record = _audit_for(config, tmp_path)

    text = "\n".join(explain.render_configuration(record["configuration"]))

    assert config.provenance.resolved_sha256 in text
    assert "changed during run: no" in text


def test_explain_flags_a_configuration_that_changed_mid_run() -> None:
    text = "\n".join(
        explain.render_configuration(
            {
                "source_path": "/proj/forge.yaml",
                "source_sha256": "aaa",
                "source_sha256_at_finish": "bbb",
                "resolved_sha256": "ccc",
                "changed_during_run": True,
            }
        )
    )

    assert "changed during run: yes" in text
    assert "bbb" in text


def test_explain_distinguishes_a_record_with_no_configuration_identity() -> None:
    text = "\n".join(explain.render_configuration(None))

    assert "not recorded" in text
    assert "changed during run" not in text
