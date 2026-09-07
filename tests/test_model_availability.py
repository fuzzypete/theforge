"""Account-catalog availability resolution without model dispatch."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from theforge.config import TransportSpec, auth
from theforge.config.model_identity import (
    AVAILABILITY_FRESHNESS_STALE,
    IDENTITY_STATUS_RETIRED,
    MODEL_AVAILABILITY_AVAILABLE,
    MODEL_AVAILABILITY_UNAVAILABLE,
    MODEL_AVAILABILITY_UNVERIFIED,
    IdentityVerification,
)


def _target(
    *,
    model: str = "gpt-5.6-terra",
    kind: str = "cli",
    runner: str = "codex",
    key: str = "target",
    identity: IdentityVerification | None = None,
    base_url: str | None = None,
) -> auth.ModelAvailabilityTarget:
    return auth.ModelAvailabilityTarget(
        canonical_id=f"openai/{model}/{kind}",
        provider="openai",
        model=model,
        transport=TransportSpec(
            kind=kind,
            runner=runner,
            executable="codex" if kind == "cli" else None,
        ),
        base_url=base_url,
        key=key,
        **({"identity": identity} if identity is not None else {}),
    )


def _codex_account(monkeypatch, tmp_path, models: list[dict], fetched_at: datetime) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text("{}", encoding="utf-8")
    cache_path = tmp_path / "models_cache.json"
    cache_path.write_text(
        json.dumps({"fetched_at": fetched_at.isoformat(), "models": models}), encoding="utf-8"
    )
    monkeypatch.setattr(auth, "codex_auth_path", lambda: auth_path)
    monkeypatch.setattr(auth, "codex_models_cache_path", lambda: cache_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def test_codex_cache_catalog_resolves_present_and_missing_models(monkeypatch, tmp_path) -> None:
    _codex_account(
        monkeypatch,
        tmp_path,
        [{"slug": "gpt-5.6-terra"}],
        datetime.now(timezone.utc),
    )

    results = auth.resolve_model_availability(
        [_target(key="present"), _target(model="gpt-5.4", key="missing")]
    )

    assert results["present"].state == MODEL_AVAILABILITY_AVAILABLE
    assert results["missing"].state == MODEL_AVAILABILITY_UNAVAILABLE
    assert results["missing"].reason == "not in account catalog"
    assert results["present"].auth_mode == "ChatGPT-account auth"


def test_missing_or_unsupported_catalog_is_unverified(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(auth, "codex_auth_path", lambda: tmp_path / "missing-auth.json")
    monkeypatch.setattr(auth, "codex_models_cache_path", lambda: tmp_path / "missing-cache.json")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    claude = auth.ModelAvailabilityTarget(
        canonical_id="anthropic/opus/cli",
        provider="anthropic",
        model="opus",
        transport=TransportSpec(kind="cli", runner="claude", executable="claude"),
        key="claude",
    )

    results = auth.resolve_model_availability([_target(), claude])

    assert results["target"].state == MODEL_AVAILABILITY_UNVERIFIED
    assert "could not be determined" in (results["target"].reason or "")
    assert results["claude"].state == MODEL_AVAILABILITY_UNVERIFIED
    assert results["claude"].reason == "provider publishes no account catalog"


def test_failed_openai_catalog_attempt_is_unverified(monkeypatch) -> None:
    class BrokenOpenAI:
        def __init__(self, **_kwargs) -> None:
            pass

        @property
        def models(self):
            raise RuntimeError("expired login")

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=BrokenOpenAI))

    result = auth.resolve_model_availability([_target(kind="api", runner="openai")])["target"]

    assert result.state == MODEL_AVAILABILITY_UNVERIFIED
    assert "account catalog request failed" in (result.reason or "")


def test_local_openai_catalog_uses_dispatch_dummy_key_without_an_api_key(monkeypatch) -> None:
    received: dict[str, object] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs) -> None:
            received.update(kwargs)
            self.models = SimpleNamespace(
                list=lambda: SimpleNamespace(data=[SimpleNamespace(id="gpt-5.6-terra")])
            )

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))

    result = auth.resolve_model_availability(
        [_target(kind="api", runner="openai", base_url="http://localhost:11434/v1")]
    )["target"]

    assert result.state == MODEL_AVAILABILITY_AVAILABLE
    assert result.auth_mode == "local endpoint (no API key)"
    assert received == {
        "api_key": "local",
        "base_url": "http://localhost:11434/v1",
        "timeout": 10.0,
    }


def test_local_openai_catalog_with_a_configured_key_reports_api_key_auth(monkeypatch) -> None:
    received: dict[str, object] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs) -> None:
            received.update(kwargs)
            self.models = SimpleNamespace(
                list=lambda: SimpleNamespace(data=[SimpleNamespace(id="gpt-5.6-terra")])
            )

    monkeypatch.setenv("OPENAI_API_KEY", "configured-key")
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))

    result = auth.resolve_model_availability(
        [_target(kind="api", runner="openai", base_url="http://localhost:11434/v1")]
    )["target"]

    assert result.state == MODEL_AVAILABILITY_AVAILABLE
    assert result.auth_mode == "API-key auth"
    assert received == {
        "api_key": "configured-key",
        "base_url": "http://localhost:11434/v1",
        "timeout": 10.0,
    }


def test_stale_codex_cache_is_not_reused(monkeypatch, tmp_path) -> None:
    _codex_account(
        monkeypatch,
        tmp_path,
        [{"slug": "gpt-5.6-terra"}],
        datetime.now(timezone.utc) - timedelta(days=181),
    )

    result = auth.resolve_model_availability([_target()])["target"]

    assert result.state == MODEL_AVAILABILITY_UNVERIFIED
    assert result.freshness == AVAILABILITY_FRESHNESS_STALE
    assert result.reason == "account catalog cache is stale"


def test_codex_cache_allows_duplicate_model_entries(monkeypatch, tmp_path) -> None:
    _codex_account(
        monkeypatch,
        tmp_path,
        [{"slug": "gpt-5.6-terra"}, {"slug": "gpt-5.6-terra"}],
        datetime.now(timezone.utc),
    )

    result = auth.resolve_model_availability([_target()])["target"]

    assert result.state == MODEL_AVAILABILITY_AVAILABLE


def test_same_model_can_diverge_by_auth_mode(monkeypatch, tmp_path) -> None:
    _codex_account(
        monkeypatch,
        tmp_path,
        [{"slug": "gpt-5.6-terra"}],
        datetime.now(timezone.utc),
    )

    class FakeOpenAI:
        def __init__(self, **_kwargs) -> None:
            self.models = SimpleNamespace(list=lambda: SimpleNamespace(data=[]))

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    chatgpt = auth.resolve_model_availability([_target(key="chatgpt")])["chatgpt"]
    api = auth.resolve_model_availability(
        [_target(kind="api", runner="openai", key="api")], {"OPENAI_API_KEY": "test-key"}
    )["api"]

    assert chatgpt.state == MODEL_AVAILABILITY_AVAILABLE
    assert chatgpt.auth_mode == "ChatGPT-account auth"
    assert api.state == MODEL_AVAILABILITY_UNAVAILABLE
    assert api.auth_mode == "API-key auth"


def test_retired_identity_overrides_catalog_evidence(monkeypatch, tmp_path) -> None:
    _codex_account(
        monkeypatch,
        tmp_path,
        [{"slug": "gpt-5.6-terra"}],
        datetime.now(timezone.utc),
    )
    retired = IdentityVerification(
        status=IDENTITY_STATUS_RETIRED,
        retired_reason="withdrawn by provider",
    )

    result = auth.resolve_model_availability([_target(identity=retired)])["target"]

    assert result.state == MODEL_AVAILABILITY_UNAVAILABLE
    assert "retired upstream" in (result.reason or "")


def test_retired_non_codex_target_reports_its_dispatch_auth_mode() -> None:
    retired = IdentityVerification(
        status=IDENTITY_STATUS_RETIRED,
        retired_reason="withdrawn by provider",
    )

    result = auth.resolve_model_availability(
        [_target(kind="api", runner="deepseek", identity=retired)]
    )["target"]

    assert result.state == MODEL_AVAILABILITY_UNAVAILABLE
    assert result.auth_mode == "API-key auth"
