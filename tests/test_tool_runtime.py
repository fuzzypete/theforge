"""Tests for tool_runtime.py — tool handlers, schemas, and provider translation."""

from __future__ import annotations

import subprocess

from theforge.tool_runtime import (
    MAX_TOOL_OUTPUT_BYTES,
    TOOL_NAME_MAP,
    TOOL_REGISTRY,
    ToolDef,
    _handle_bash,
    _handle_glob,
    _handle_grep,
    _handle_read_file,
    _resolve_safe,
    truncate_output,
)


class TestTruncateOutput:
    def test_no_truncation_when_under_limit(self):
        text = "hello world"
        assert truncate_output(text, max_bytes=100) == text

    def test_truncates_at_byte_limit(self):
        text = "a" * 200
        result = truncate_output(text, max_bytes=100)
        assert "[output truncated" in result
        assert "200 bytes" in result

    def test_truncation_notice_shows_kb(self):
        text = "x" * (MAX_TOOL_OUTPUT_BYTES + 100)
        result = truncate_output(text)
        assert "[output truncated" in result
        assert f"{MAX_TOOL_OUTPUT_BYTES // 1024}KB" in result


class TestResolveSafe:
    def test_safe_path(self, tmp_path):
        resolved = _resolve_safe(tmp_path, "src/foo.py")
        assert resolved is not None
        assert resolved == (tmp_path / "src" / "foo.py").resolve()

    def test_traversal_rejected(self, tmp_path):
        result = _resolve_safe(tmp_path, "../secret.txt")
        assert result is None

    def test_deep_traversal_rejected(self, tmp_path):
        result = _resolve_safe(tmp_path, "a/b/../../../../../../etc/passwd")
        assert result is None

    def test_absolute_path_inside(self, tmp_path):
        # An absolute path that's inside working_dir should work
        target = tmp_path / "file.txt"
        result = _resolve_safe(tmp_path, str(target))
        assert result is not None


class TestReadFile:
    def test_reads_file_contents(self, tmp_path):
        f = tmp_path / "hello.txt"
        f.write_text("hello world\n", encoding="utf-8")
        result = _handle_read_file(path="hello.txt", working_dir=tmp_path)
        assert result == "hello world\n"

    def test_file_not_found(self, tmp_path):
        result = _handle_read_file(path="nonexistent.py", working_dir=tmp_path)
        assert "FileNotFoundError" in result
        assert "nonexistent.py" in result

    def test_directory_error(self, tmp_path):
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        result = _handle_read_file(path="subdir", working_dir=tmp_path)
        assert "directory" in result

    def test_path_traversal_rejected(self, tmp_path):
        result = _handle_read_file(path="../outside.txt", working_dir=tmp_path)
        assert "path traversal rejected" in result

    def test_line_range_start_and_end(self, tmp_path):
        f = tmp_path / "code.py"
        lines = [f"line{i}" for i in range(1, 11)]
        f.write_text("\n".join(lines), encoding="utf-8")
        result = _handle_read_file(path="code.py", working_dir=tmp_path, start_line=3, end_line=5)
        assert "3: line3" in result
        assert "4: line4" in result
        assert "5: line5" in result
        assert "line1" not in result
        assert "line6" not in result

    def test_line_range_start_only(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
        result = _handle_read_file(path="code.py", working_dir=tmp_path, start_line=3)
        assert "3: c" in result
        assert "4: d" in result
        assert "1: a" not in result

    def test_line_range_end_only(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
        result = _handle_read_file(path="code.py", working_dir=tmp_path, end_line=2)
        assert "1: a" in result
        assert "2: b" in result
        assert "3: c" not in result

    def test_truncates_large_file(self, tmp_path):
        f = tmp_path / "big.txt"
        content = "x" * (MAX_TOOL_OUTPUT_BYTES + 500)
        f.write_text(content, encoding="utf-8")
        result = _handle_read_file(path="big.txt", working_dir=tmp_path)
        assert "[output truncated" in result

    def test_custom_max_bytes(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("a" * 200, encoding="utf-8")
        result = _handle_read_file(path="file.txt", working_dir=tmp_path, max_bytes=50)
        assert "[output truncated" in result


class TestBash:
    def test_runs_command(self, tmp_path):
        result = _handle_bash(command="echo hello", working_dir=tmp_path)
        assert "hello" in result
        assert "Exit code: 0" in result

    def test_captures_stderr(self, tmp_path):
        result = _handle_bash(command="echo err >&2", working_dir=tmp_path)
        assert "err" in result

    def test_returns_exit_code_on_failure(self, tmp_path):
        result = _handle_bash(command="exit 42", working_dir=tmp_path)
        assert "Exit code: 42" in result

    def test_command_failure_returns_string_not_exception(self, tmp_path):
        # Should never raise — always return str
        result = _handle_bash(command="ls /nonexistent_path_xyz", working_dir=tmp_path)
        assert isinstance(result, str)

    def test_timeout_returns_error_string(self, tmp_path, monkeypatch):
        def raise_timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="sleep", timeout=30)

        monkeypatch.setattr(subprocess, "run", raise_timeout)
        result = _handle_bash(command="sleep 999", working_dir=tmp_path)
        assert "timed out" in result
        assert isinstance(result, str)

    def test_truncates_large_output(self, tmp_path):
        result = _handle_bash(
            command=f"python3 -c \"print('x' * {MAX_TOOL_OUTPUT_BYTES + 500})\"",
            working_dir=tmp_path,
            max_bytes=100,
        )
        assert "[output truncated" in result


class TestGrep:
    def test_finds_matches(self, tmp_path):
        f = tmp_path / "foo.py"
        f.write_text("def hello():\n    return 42\n", encoding="utf-8")
        result = _handle_grep(pattern="def hello", working_dir=tmp_path)
        assert "foo.py" in result
        assert "def hello" in result

    def test_no_matches_returns_sentinel(self, tmp_path):
        f = tmp_path / "foo.py"
        f.write_text("hello world\n", encoding="utf-8")
        result = _handle_grep(pattern="zzznomatch", working_dir=tmp_path)
        assert result == "(no matches)"

    def test_invalid_regex_returns_error(self, tmp_path):
        result = _handle_grep(pattern="[unclosed", working_dir=tmp_path)
        assert "Error" in result
        assert "regex" in result

    def test_glob_filter(self, tmp_path):
        (tmp_path / "a.py").write_text("import os\n", encoding="utf-8")
        (tmp_path / "b.txt").write_text("import os\n", encoding="utf-8")
        result = _handle_grep(pattern="import os", working_dir=tmp_path, glob="*.py")
        assert "a.py" in result
        assert "b.txt" not in result

    def test_path_scoped_search(self, tmp_path):
        subdir = tmp_path / "src"
        subdir.mkdir()
        (subdir / "code.py").write_text("TARGET\n", encoding="utf-8")
        (tmp_path / "other.py").write_text("TARGET\n", encoding="utf-8")
        result = _handle_grep(pattern="TARGET", working_dir=tmp_path, path="src")
        assert "src/code.py" in result
        # other.py is outside the scoped path
        assert "other.py" not in result

    def test_path_traversal_rejected(self, tmp_path):
        result = _handle_grep(pattern="x", working_dir=tmp_path, path="../outside")
        assert "path traversal rejected" in result

    def test_returns_file_line_format(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
        result = _handle_grep(pattern="beta", working_dir=tmp_path)
        assert "test.py:2: beta" in result

    def test_path_is_a_file_searches_that_file(self, tmp_path):
        """When path resolves to a file, grep searches within it directly."""
        f = tmp_path / "single.py"
        f.write_text("TARGET\nother\n", encoding="utf-8")
        (tmp_path / "other.py").write_text("TARGET\n", encoding="utf-8")
        result = _handle_grep(pattern="TARGET", working_dir=tmp_path, path="single.py")
        assert "single.py" in result
        # Should not include the other file
        assert "other.py" not in result


class TestGlob:
    def test_finds_files(self, tmp_path):
        (tmp_path / "a.py").touch()
        (tmp_path / "b.py").touch()
        result = _handle_glob(pattern="*.py", working_dir=tmp_path)
        assert "a.py" in result
        assert "b.py" in result

    def test_recursive_pattern(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "deep.py").touch()
        result = _handle_glob(pattern="**/*.py", working_dir=tmp_path)
        assert "sub/deep.py" in result

    def test_no_matches_returns_sentinel(self, tmp_path):
        result = _handle_glob(pattern="*.nonexistent", working_dir=tmp_path)
        assert result == "(no matches)"

    def test_returns_relative_paths(self, tmp_path):
        sub = tmp_path / "src"
        sub.mkdir()
        (sub / "file.py").touch()
        result = _handle_glob(pattern="**/*.py", working_dir=tmp_path)
        assert result.startswith("src/") or "src/file.py" in result


class TestToolNameMap:
    def test_maps_capitalized_to_canonical(self):
        assert TOOL_NAME_MAP["Read"] == "read_file"
        assert TOOL_NAME_MAP["Bash"] == "bash"
        assert TOOL_NAME_MAP["Grep"] == "grep"
        assert TOOL_NAME_MAP["Glob"] == "glob"
        assert TOOL_NAME_MAP["Write"] == "write_file"
        assert TOOL_NAME_MAP["Edit"] == "edit_file"


class TestToolRegistry:
    def test_registry_has_phase1_tools(self):
        assert "read_file" in TOOL_REGISTRY
        assert "bash" in TOOL_REGISTRY
        assert "grep" in TOOL_REGISTRY
        assert "glob" in TOOL_REGISTRY

    def test_all_entries_are_tooldefs(self):
        for name, tool in TOOL_REGISTRY.items():
            assert isinstance(tool, ToolDef), f"{name} is not a ToolDef"
            assert tool.name == name

    def test_handlers_are_callable(self):
        for name, tool in TOOL_REGISTRY.items():
            assert callable(tool.handler), f"{name}.handler is not callable"


class TestProviderSchemaTranslation:
    def test_to_openai_function(self):
        tool = TOOL_REGISTRY["read_file"]
        schema = tool.to_openai_function()
        assert schema["type"] == "function"
        assert "function" in schema
        fn = schema["function"]
        assert fn["name"] == "read_file"
        assert "description" in fn
        assert "parameters" in fn
        assert fn["parameters"]["type"] == "object"

    def test_to_anthropic_tool(self):
        tool = TOOL_REGISTRY["bash"]
        schema = tool.to_anthropic_tool()
        assert schema["name"] == "bash"
        assert "description" in schema
        assert "input_schema" in schema
        assert schema["input_schema"]["type"] == "object"

    def test_to_google_declaration(self):
        tool = TOOL_REGISTRY["grep"]
        schema = tool.to_google_declaration()
        assert schema["name"] == "grep"
        assert "description" in schema
        assert "parameters" in schema

    def test_all_tools_have_valid_openai_schema(self):
        for name, tool in TOOL_REGISTRY.items():
            schema = tool.to_openai_function()
            assert schema["type"] == "function"
            assert schema["function"]["name"] == name

    def test_all_tools_have_valid_anthropic_schema(self):
        for name, tool in TOOL_REGISTRY.items():
            schema = tool.to_anthropic_tool()
            assert schema["name"] == name
            assert "input_schema" in schema

    def test_read_file_schema_has_optional_line_params(self):
        tool = TOOL_REGISTRY["read_file"]
        schema = tool.to_openai_function()
        params = schema["function"]["parameters"]
        props = params["properties"]
        assert "path" in props
        assert "start_line" in props
        assert "end_line" in props
        # path is required, line params are optional
        assert "path" in params["required"]
        assert "start_line" not in params["required"]
        assert "end_line" not in params["required"]
