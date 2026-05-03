"""Tests for coordinator/redact.py (best-effort secret redaction)."""

from __future__ import annotations

from pathlib import Path

from theforge.coordinator.redact import redact

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
