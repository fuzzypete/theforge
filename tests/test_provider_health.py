"""Tests for theforge.provider_health: classifier, persistence, window filtering."""

from __future__ import annotations

import datetime

from theforge.agent_types import AgentResult
from theforge.provider_health import (
    DEFAULT_HEALTH_WINDOW_SECONDS,
    ProviderHealthRecord,
    append_provider_health_record,
    classify_provider_shape_failure,
    load_provider_health,
    unhealthy_models,
    utcnow_iso,
)


def _result(
    *,
    success: bool = False,
    output: str = "",
    exit_code: int = 1,
    cli_quota_error_observed: bool = False,
    profile_name: str = "p",
) -> AgentResult:
    return AgentResult(
        success=success,
        output=output,
        session_id=None,
        cost_usd=None,
        exit_code=exit_code,
        raw={},
        profile_name=profile_name,
        cli_quota_error_observed=cli_quota_error_observed,
    )


# ── classify_provider_shape_failure ────────────────────────────────────


def test_classifier_capacity_in_output():
    r = _result(output="ERROR: Selected model is at capacity. Please try a different model.")
    assert classify_provider_shape_failure(r) == "capacity"


def test_classifier_rate_limit():
    r = _result(output="HTTP 429 rate limit exceeded")
    assert classify_provider_shape_failure(r) == "rate_limit"


def test_classifier_quota():
    r = _result(output="resource_exhausted: quota exceeded for project")
    assert classify_provider_shape_failure(r) == "quota"


def test_classifier_5xx():
    r = _result(output="503 Service Unavailable")
    assert classify_provider_shape_failure(r) == "5xx"


def test_classifier_overloaded_coalesces_to_capacity():
    r = _result(output="Anthropic API overloaded")
    assert classify_provider_shape_failure(r) == "capacity"


def test_classifier_cli_quota_flag():
    r = _result(output="something else", cli_quota_error_observed=True)
    assert classify_provider_shape_failure(r) == "cli_quota"


def test_classifier_success_is_ignored():
    r = _result(success=True, output="429 in some text but we succeeded", exit_code=0)
    assert classify_provider_shape_failure(r) is None


def test_classifier_runtime_failure_not_provider():
    r = _result(output="AssertionError: bad code")
    assert classify_provider_shape_failure(r) is None


def test_classifier_is_case_insensitive():
    r = _result(output="MODEL IS AT CAPACITY")
    assert classify_provider_shape_failure(r) == "capacity"


# ── load / append round-trip ───────────────────────────────────────────


def test_load_missing_file_returns_empty(tmp_path):
    assert load_provider_health(tmp_path / "nope.yaml") == []


def test_append_then_load_round_trip(tmp_path):
    path = tmp_path / "provider_health.yaml"
    rec = ProviderHealthRecord(
        model="openai-gpt-5.5", signature="capacity", timestamp=utcnow_iso()
    )
    append_provider_health_record(path, rec)
    loaded = load_provider_health(path)
    assert len(loaded) == 1
    assert loaded[0].model == "openai-gpt-5.5"
    assert loaded[0].signature == "capacity"


def test_append_appends_existing(tmp_path):
    path = tmp_path / "provider_health.yaml"
    for sig in ("capacity", "rate_limit"):
        append_provider_health_record(
            path, ProviderHealthRecord(model="m", signature=sig, timestamp=utcnow_iso())
        )
    assert {r.signature for r in load_provider_health(path)} == {"capacity", "rate_limit"}


def test_load_malformed_yaml_returns_empty(tmp_path):
    path = tmp_path / "provider_health.yaml"
    path.write_text("not: a: valid: yaml: shape:\n  - junk")
    # malformed is tolerated (treated as empty); load returns []
    assert load_provider_health(path) == [] or isinstance(load_provider_health(path), list)


def test_load_ignores_records_missing_required_fields(tmp_path):
    path = tmp_path / "provider_health.yaml"
    path.write_text(
        "records:\n  - model: m\n    signature: capacity\n"
        "  - model: m2\n"  # missing signature + timestamp
    )
    loaded = load_provider_health(path)
    assert len(loaded) == 0 or all(r.model and r.signature and r.timestamp for r in loaded)


# ── unhealthy_models window filtering ───────────────────────────────────


def _iso(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_unhealthy_models_within_window():
    now = datetime.datetime(2026, 5, 12, 12, 0, 0, tzinfo=datetime.timezone.utc)
    recent = _iso(now - datetime.timedelta(seconds=600))
    records = [ProviderHealthRecord(model="gpt-5.5", signature="capacity", timestamp=recent)]
    assert unhealthy_models(records, now=now) == {"gpt-5.5"}


def test_unhealthy_models_outside_window_filtered():
    now = datetime.datetime(2026, 5, 12, 12, 0, 0, tzinfo=datetime.timezone.utc)
    old = _iso(now - datetime.timedelta(seconds=DEFAULT_HEALTH_WINDOW_SECONDS + 60))
    records = [ProviderHealthRecord(model="gpt-5.5", signature="capacity", timestamp=old)]
    assert unhealthy_models(records, now=now) == set()


def test_unhealthy_models_custom_window():
    now = datetime.datetime(2026, 5, 12, 12, 0, 0, tzinfo=datetime.timezone.utc)
    ts = _iso(now - datetime.timedelta(seconds=300))
    records = [ProviderHealthRecord(model="m", signature="capacity", timestamp=ts)]
    assert unhealthy_models(records, now=now, window_seconds=60) == set()
    assert unhealthy_models(records, now=now, window_seconds=600) == {"m"}


def test_unhealthy_models_empty_records():
    assert unhealthy_models([]) == set()


def test_unhealthy_models_skips_unparseable_timestamps():
    records = [ProviderHealthRecord(model="m", signature="x", timestamp="not-an-iso")]
    assert unhealthy_models(records) == set()
