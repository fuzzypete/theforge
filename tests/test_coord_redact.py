"""Tests for coordinator/redact.py (best-effort secret redaction)."""

from __future__ import annotations

from pathlib import Path

import pytest

from theforge.coordinator.redact import TELEMETRY_NUMERIC_KEYS, redact

# ── redact() unit tests ─────────────────────────────────────────────────────


class TestRedactEnvFile:
    def test_replaces_env_value_in_string(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("MY_SECRET=supersecretvalue123\n")
        result = redact({"output": "token is supersecretvalue123 done"}, env)
        assert result["output"] == "token is [REDACTED] done"

    def test_skips_values_shorter_than_8_chars(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("SHORT=dev\n")  # 3 chars — should not be treated as a secret
        result = redact({"output": "env is dev here"}, env)
        assert result["output"] == "env is dev here"

    def test_ignores_missing_env_file(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.env"
        result = redact({"key": "value"}, missing)
        assert result == {"key": "value"}

    def test_none_env_file_no_crash(self) -> None:
        result = redact({"x": "hello"}, None)
        assert result == {"x": "hello"}

    def test_strips_quoted_values_in_env(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text('API_KEY="mysecretkey9876"\n')
        result = redact({"header": "Bearer mysecretkey9876"}, env)
        assert result["header"] == "Bearer [REDACTED]"

    def test_ignores_comment_lines(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("# This is a comment\nREAL_SECRET=actualsecretval\n")
        result = redact({"v": "actualsecretval is here"}, env)
        assert result["v"] == "[REDACTED] is here"

    def test_recursive_replacement_in_nested_dict(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("DEEP_SECRET=deepsecretvalue\n")
        obj = {"outer": {"inner": "contains deepsecretvalue here"}}
        result = redact(obj, env)
        assert result["outer"]["inner"] == "contains [REDACTED] here"

    def test_recursive_replacement_in_list(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("LIST_SECRET=listsecretvalue1\n")
        obj = {"items": ["safe string", "contains listsecretvalue1 inside"]}
        result = redact(obj, env)
        assert result["items"][0] == "safe string"
        assert result["items"][1] == "contains [REDACTED] inside"

    def test_preserves_context_around_secret(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("MY_TOKEN=secrettoken99\n")
        story_body = "Story: use token secrettoken99 to auth. See docs for details."
        result = redact({"body": story_body}, env)
        assert result["body"] == "Story: use token [REDACTED] to auth. See docs for details."

    def test_multiple_distinct_secrets_all_redacted(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("FIRST_SECRET=firstsecret12\nSECOND_SECRET=secondsecret34\n")
        val = "call firstsecret12 then secondsecret34 done"
        result = redact({"cmd": val}, env)
        assert "firstsecret12" not in result["cmd"]
        assert "secondsecret34" not in result["cmd"]
        assert "call" in result["cmd"]
        assert "then" in result["cmd"]
        assert "done" in result["cmd"]


class TestRedactSecretKeys:
    def test_scrubs_key_named_secret(self) -> None:
        result = redact({"secret": "my-secret-value"}, None)
        assert result["secret"] == "[REDACTED]"

    def test_scrubs_key_named_token(self) -> None:
        result = redact({"token": "tok_abc123"}, None)
        assert result["token"] == "[REDACTED]"

    def test_scrubs_key_named_password(self) -> None:
        result = redact({"password": "hunter2"}, None)
        assert result["password"] == "[REDACTED]"

    def test_scrubs_key_named_api_key(self) -> None:
        result = redact({"api_key": "ak_12345"}, None)
        assert result["api_key"] == "[REDACTED]"

    def test_scrubs_key_named_api_dash_key(self) -> None:
        result = redact({"api-key": "ak_12345"}, None)
        assert result["api-key"] == "[REDACTED]"

    def test_scrubs_key_named_authorization(self) -> None:
        result = redact({"authorization": "Bearer token123"}, None)
        assert result["authorization"] == "[REDACTED]"

    def test_case_insensitive_key_match(self) -> None:
        result = redact({"API_KEY": "value", "Secret": "s", "TOKEN": "t"}, None)
        assert result["API_KEY"] == "[REDACTED]"
        assert result["Secret"] == "[REDACTED]"
        assert result["TOKEN"] == "[REDACTED]"

    def test_leaves_clean_key_intact(self) -> None:
        result = redact({"status": "ok", "message": "all good"}, None)
        assert result["status"] == "ok"
        assert result["message"] == "all good"


class TestRedactNumericTelemetry:
    """Numeric values on secret-shaped keys are measurements, not credentials.

    These tests are the mechanical guard on the carve-out in `_redact_obj`: a
    widening of `_SECRET_KEY_RE`, or a removal of the numeric exemption, fails
    here instead of silently discarding usage counts again (#2202).
    """

    @pytest.mark.parametrize("key", sorted(TELEMETRY_NUMERIC_KEYS))
    def test_catalogued_telemetry_key_survives_with_numeric_value(self, key: str) -> None:
        result = redact({key: 1234}, None)
        assert result[key] == 1234

    @pytest.mark.parametrize("key", sorted(TELEMETRY_NUMERIC_KEYS))
    def test_catalogued_telemetry_key_still_scrubbed_with_string_value(self, key: str) -> None:
        # The carve-out is value-shaped, not key-shaped: a string on the same
        # key could carry a credential and must still be removed.
        result = redact({key: "tok_abc123"}, None)
        assert result[key] == "[REDACTED]"

    def test_zero_count_survives(self) -> None:
        result = redact({"cache_read_tokens": 0}, None)
        assert result["cache_read_tokens"] == 0

    def test_float_value_on_secret_key_survives(self) -> None:
        result = redact({"token_ratio": 0.25}, None)
        assert result["token_ratio"] == 0.25

    def test_string_secret_keys_unaffected_by_carve_out(self) -> None:
        result = redact(
            {"api_key": "ak_12345", "authorization": "Bearer x", "password": "hunter2"},
            None,
        )
        assert result == {
            "api_key": "[REDACTED]",
            "authorization": "[REDACTED]",
            "password": "[REDACTED]",
        }

    def test_null_secret_value_still_scrubbed(self) -> None:
        # None is not a measurement; leave it reading as scrubbed rather than
        # as "no credential was present".
        result = redact({"token": None}, None)
        assert result["token"] == "[REDACTED]"

    def test_container_on_secret_key_still_scrubbed(self) -> None:
        result = redact({"tokens": {"raw": "tok_abc"}, "token_list": ["tok_a"]}, None)
        assert result["tokens"] == "[REDACTED]"
        assert result["token_list"] == "[REDACTED]"

    def test_model_usage_entry_keeps_counts_and_cost(self) -> None:
        entry = {
            "model": "claude-opus-5",
            "input_tokens": 1500,
            "output_tokens": 320,
            "cache_read_tokens": 90_000,
            "cache_creation_tokens": 12,
            "cost_usd": 0.42,
        }
        result = redact({"cost": {"agents": [{"model_usage": [entry]}]}}, None)
        assert result["cost"]["agents"][0]["model_usage"][0] == entry


class TestRedactAuditRenderSeam:
    """Counts rendered by audit_render survive the scrubber applied to them.

    Unit coverage above pins redact() in isolation; this exercises the actual
    render → redact seam, so a change to either side that reintroduces the loss
    is caught.
    """

    def test_rendered_model_usage_survives_redaction(self, tmp_path: Path) -> None:
        from theforge.agent_types import AgentResult, ModelUsage
        from theforge.coordinator.audit_render import _model_usage_entries

        env = tmp_path / ".env"
        env.write_text("ANTHROPIC_API_KEY=sk-ant-realsecretvalue\n")

        result = AgentResult(
            success=True,
            output="done",
            session_id="s1",
            cost_usd=0.42,
            exit_code=0,
            raw={},
            model_usage=(
                ModelUsage(
                    model="claude-opus-5",
                    input_tokens=1500,
                    output_tokens=320,
                    cache_read_tokens=90_000,
                    cache_creation_tokens=12,
                    cost_usd=0.42,
                ),
            ),
        )
        rendered = _model_usage_entries(result)
        scrubbed = redact({"cost": {"agents": [{"model_usage": rendered}]}}, env)

        usage = scrubbed["cost"]["agents"][0]["model_usage"][0]
        assert usage["input_tokens"] == 1500
        assert usage["output_tokens"] == 320
        assert usage["cache_read_tokens"] == 90_000
        assert usage["cache_creation_tokens"] == 12
        assert usage["cost_usd"] == 0.42
        assert usage["model"] == "claude-opus-5"
        # Cost per unit of work is derivable from the stored record.
        assert usage["cost_usd"] / usage["output_tokens"] > 0

    def test_credential_alongside_usage_still_scrubbed(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("ANTHROPIC_API_KEY=sk-ant-realsecretvalue\n")
        record = {
            "api_key": "sk-ant-realsecretvalue",
            "command": "run --key sk-ant-realsecretvalue",
            "model_usage": [{"input_tokens": 10, "output_tokens": 2}],
        }
        scrubbed = redact(record, env)
        assert scrubbed["api_key"] == "[REDACTED]"
        assert "sk-ant-realsecretvalue" not in scrubbed["command"]
        assert scrubbed["model_usage"][0] == {"input_tokens": 10, "output_tokens": 2}


class TestRedactEnvironmentDict:
    def test_replaces_environment_dict_with_sorted_keys(self) -> None:
        result = redact(
            {"environment": {"PATH": "/usr/bin", "SECRET_KEY": "abc", "HOME": "/root"}},
            None,
        )
        assert result["environment"] == ["HOME", "PATH", "SECRET_KEY"]

    def test_leaves_environment_non_dict_unchanged(self) -> None:
        result = redact({"environment": ["already", "a", "list"]}, None)
        assert result["environment"] == ["already", "a", "list"]

    def test_environment_key_nested_inside_dict(self) -> None:
        result = redact(
            {"runtime": {"environment": {"A": "1", "B": "2"}}},
            None,
        )
        assert result["runtime"]["environment"] == ["A", "B"]


class TestRedactTuples:
    def test_redacts_secret_string_inside_tuple(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("TUPLE_SECRET=tuplesecretvalue1\n")
        obj = ("safe", "contains tuplesecretvalue1")
        result = redact(obj, env)
        assert isinstance(result, tuple)
        assert result[0] == "safe"
        assert result[1] == "contains [REDACTED]"

    def test_preserves_tuple_shape(self) -> None:
        result = redact({"data": ("a", "b", 42)}, None)
        assert isinstance(result["data"], tuple)
        assert result["data"] == ("a", "b", 42)

    def test_redacts_secret_key_value_in_tuple(self) -> None:
        result = redact({"items": ({"token": "tok_abc"}, "safe")}, None)
        assert isinstance(result["items"], tuple)
        assert result["items"][0]["token"] == "[REDACTED]"
        assert result["items"][1] == "safe"

    def test_redacts_nested_tuples(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("NESTED_SECRET=nestedsecretval1\n")
        obj = (("inner", "nestedsecretval1"),)
        result = redact(obj, env)
        assert isinstance(result, tuple)
        assert isinstance(result[0], tuple)
        assert result[0][1] == "[REDACTED]"


class TestRedactReturnsCopy:
    def test_original_not_mutated(self) -> None:
        original = {"password": "secret123", "name": "Alice"}
        result = redact(original, None)
        assert original["password"] == "secret123"
        assert result["password"] == "[REDACTED]"

    def test_non_string_scalars_pass_through(self) -> None:
        result = redact({"count": 42, "flag": True, "ratio": 1.5}, None)
        assert result == {"count": 42, "flag": True, "ratio": 1.5}

    def test_none_values_pass_through(self) -> None:
        result = redact({"val": None}, None)
        assert result["val"] is None
