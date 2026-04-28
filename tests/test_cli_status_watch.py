"""Tests for `forge status --watch` live-update mode."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

from theforge.cli import status_watch

# ── Argument parsing ──────────────────────────────────────────────────────────


class TestWatchArgParsing:
    def _build_parser(self) -> argparse.ArgumentParser:
        from theforge.cli.status import register_parsers

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="cmd")
        register_parsers(sub)
        return parser

    def test_no_watch_flag_default(self) -> None:
        parser = self._build_parser()
        ns = parser.parse_args(["status"])
        assert ns.watch is None
        assert ns.no_color is False

    def test_watch_flag_default_interval(self) -> None:
        parser = self._build_parser()
        ns = parser.parse_args(["status", "--watch"])
        assert ns.watch == 2

    def test_watch_flag_explicit_interval(self) -> None:
        parser = self._build_parser()
        ns = parser.parse_args(["status", "--watch", "5"])
        assert ns.watch == 5

    def test_no_color_flag(self) -> None:
        parser = self._build_parser()
        ns = parser.parse_args(["status", "--watch", "3", "--no-color"])
        assert ns.watch == 3
        assert ns.no_color is True


# ── color_enabled ─────────────────────────────────────────────────────────────


class TestColorEnabled:
    def test_no_color_flag_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        assert status_watch.color_enabled(no_color_flag=True) is False

    def test_no_color_env_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NO_COLOR", "1")
        assert status_watch.color_enabled(no_color_flag=False) is False

    def test_non_tty_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)

        class FakeStream:
            def isatty(self) -> bool:
                return False

        assert status_watch.color_enabled(False, FakeStream()) is False

    def test_tty_with_no_overrides_enables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)

        class FakeStream:
            def isatty(self) -> bool:
                return True

        assert status_watch.color_enabled(False, FakeStream()) is True


# ── Format helpers ────────────────────────────────────────────────────────────


class TestFormatHelpers:
    def test_age_seconds(self) -> None:
        assert status_watch._format_age(5) == "5s"

    def test_age_minutes(self) -> None:
        assert status_watch._format_age(125) == "2m05s"

    def test_age_hours(self) -> None:
        assert status_watch._format_age(3725) == "1h02m"

    def test_delta_zero(self) -> None:
        assert status_watch._format_delta(0.0) == "—"

    def test_delta_negligible(self) -> None:
        assert status_watch._format_delta(0.001) == "—"

    def test_delta_positive(self) -> None:
        assert status_watch._format_delta(0.42) == "+$0.42"

    def test_delta_negative(self) -> None:
        assert status_watch._format_delta(-1.5) == "-$1.50"


# ── render_frame: deltas and spinner ──────────────────────────────────────────


def _entry(slug: str, status: str = "running", cost: float = 0.0):
    from theforge.sprint.status_reader import StoryStatusEntry

    return StoryStatusEntry(
        slug=slug,
        path=slug,
        status=status,
        phase="DEV",
        cost_usd=cost,
    )


class TestRenderFrame:
    def test_first_frame_has_zero_delta(self, tmp_path: Path) -> None:
        entries = [_entry("story-a", cost=1.50)]
        state: dict = {"costs": {}, "interval": 2.0}

        with (
            patch(
                "theforge.cli.sprint_status.display_sprint_status",
                return_value=0,
            ),
            patch(
                "theforge.sprint.status_reader.read_live_status",
                return_value=entries,
            ),
            patch.object(status_watch, "_last_audit_mtime", return_value=None),
        ):
            text = status_watch.render_frame("run-x", tmp_path, state, frame_idx=0, color=False)

        # First frame: no prior cost recorded → delta is 0 → rendered as em-dash.
        assert "story-a" in text
        assert state["costs"] == {"story-a": 1.50}

    def test_second_frame_shows_delta(self, tmp_path: Path) -> None:
        state: dict = {"costs": {"story-a": 1.50}, "interval": 2.0}
        entries = [_entry("story-a", cost=2.10)]

        with (
            patch(
                "theforge.cli.sprint_status.display_sprint_status",
                return_value=0,
            ),
            patch(
                "theforge.sprint.status_reader.read_live_status",
                return_value=entries,
            ),
            patch.object(status_watch, "_last_audit_mtime", return_value=None),
        ):
            text = status_watch.render_frame("run-x", tmp_path, state, frame_idx=1, color=False)

        assert "+$0.60" in text
        assert state["costs"] == {"story-a": 2.10}

    def test_event_age_surfaces(self, tmp_path: Path) -> None:
        entries = [_entry("story-a", status="running", cost=0.0)]
        state: dict = {"costs": {}, "interval": 2.0}
        fixed_now = 1000.0
        evt_time = 970.0  # 30s ago

        with (
            patch(
                "theforge.cli.sprint_status.display_sprint_status",
                return_value=0,
            ),
            patch(
                "theforge.sprint.status_reader.read_live_status",
                return_value=entries,
            ),
            patch.object(status_watch, "_last_audit_mtime", return_value=evt_time),
        ):
            text = status_watch.render_frame(
                "run-x",
                tmp_path,
                state,
                frame_idx=0,
                color=False,
                now_fn=lambda: fixed_now,
            )

        assert "30s" in text

    def test_running_uses_spinner_when_recent_event(self, tmp_path: Path) -> None:
        entries = [_entry("story-a", status="running", cost=0.0)]
        state: dict = {"costs": {}, "interval": 2.0}
        fixed_now = 1000.0
        evt_time = 999.0  # 1s ago — not stalled

        with (
            patch(
                "theforge.cli.sprint_status.display_sprint_status",
                return_value=0,
            ),
            patch(
                "theforge.sprint.status_reader.read_live_status",
                return_value=entries,
            ),
            patch.object(status_watch, "_last_audit_mtime", return_value=evt_time),
        ):
            text = status_watch.render_frame(
                "run-x",
                tmp_path,
                state,
                frame_idx=0,
                color=False,
                now_fn=lambda: fixed_now,
            )

        assert status_watch.SPINNER_FRAMES[0] in text

    def test_running_shows_stall_when_stale(self, tmp_path: Path) -> None:
        entries = [_entry("story-a", status="running", cost=0.0)]
        state: dict = {"costs": {}, "interval": 2.0}
        fixed_now = 1000.0
        evt_time = 900.0  # 100s ago — well past stall threshold (max(5, 4))

        with (
            patch(
                "theforge.cli.sprint_status.display_sprint_status",
                return_value=0,
            ),
            patch(
                "theforge.sprint.status_reader.read_live_status",
                return_value=entries,
            ),
            patch.object(status_watch, "_last_audit_mtime", return_value=evt_time),
        ):
            text = status_watch.render_frame(
                "run-x",
                tmp_path,
                state,
                frame_idx=0,
                color=False,
                now_fn=lambda: fixed_now,
            )

        assert status_watch.STALL_CHAR in text
        # Spinner should NOT appear for a stalled story.
        assert status_watch.SPINNER_FRAMES[0] not in text


# ── run_watch_loop ────────────────────────────────────────────────────────────


class TestRunWatchLoop:
    def test_runs_max_frames_then_exits_cleanly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch.object(
                status_watch,
                "render_frame",
                return_value="frame-body\n",
            ),
            patch.object(status_watch, "is_tty", return_value=False),
        ):
            sleeps: list[float] = []
            rc = status_watch.run_watch_loop(
                "run-x",
                tmp_path,
                interval=0.01,
                color=False,
                sleep_fn=sleeps.append,
                max_frames=3,
            )

        assert rc == 0
        # Three frames rendered; sleep happens between frames, not after the last.
        assert len(sleeps) == 2
        out = capsys.readouterr().out
        assert out.count("frame-body") == 3

    def test_keyboard_interrupt_exits_zero(self, tmp_path: Path) -> None:
        def raising_sleep(_seconds: float) -> None:
            raise KeyboardInterrupt

        with (
            patch.object(
                status_watch,
                "render_frame",
                return_value="frame-body\n",
            ),
            patch.object(status_watch, "is_tty", return_value=False),
        ):
            rc = status_watch.run_watch_loop(
                "run-x",
                tmp_path,
                interval=0.01,
                color=False,
                sleep_fn=raising_sleep,
            )

        assert rc == 0

    def test_first_frame_failure_returns_nonzero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch.object(
                status_watch,
                "render_frame",
                side_effect=RuntimeError("state unreadable"),
            ),
            patch.object(status_watch, "is_tty", return_value=False),
        ):
            rc = status_watch.run_watch_loop(
                "run-x",
                tmp_path,
                interval=0.01,
                color=False,
                sleep_fn=lambda _s: None,
                max_frames=1,
            )

        assert rc == 1
        err = capsys.readouterr().err
        assert "state unreadable" in err

    def test_cursor_restored_on_tty_exit(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch.object(
                status_watch,
                "render_frame",
                return_value="frame-body\n",
            ),
            patch.object(status_watch, "is_tty", return_value=True),
        ):
            status_watch.run_watch_loop(
                "run-x",
                tmp_path,
                interval=0.01,
                color=True,
                sleep_fn=lambda _s: None,
                max_frames=1,
            )

        out = capsys.readouterr().out
        assert status_watch.HIDE_CURSOR in out
        assert status_watch.SHOW_CURSOR in out


# ── cmd_status routing ───────────────────────────────────────────────────────


class TestCmdStatusWatchRouting:
    def _config(self, tmp_path: Path):
        from tests.test_cli_status import _make_forge_config

        return _make_forge_config(tmp_path)

    def test_non_tty_falls_back_to_snapshot(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from theforge.cli import cmd_status

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = self._config(tmp_path)
        args = argparse.Namespace(run_id=None, recent=False, last=False, watch=2, no_color=False)

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
            patch("theforge.cli.status._find_active_run_id", return_value="run-1"),
            patch("theforge.cli.status._is_sprint_run", return_value=True),
            patch("theforge.cli.status_watch.is_tty", return_value=False),
            patch("theforge.cli.sprint_status.display_sprint_status", return_value=0) as snap,
            patch("theforge.pending.cleanup_stale"),
            patch("theforge.pending.list_pending", return_value=[]),
        ):
            rc = cmd_status(args)

        assert rc == 0
        snap.assert_called_once()

    def test_tty_sprint_enters_watch_loop(self, tmp_path: Path) -> None:
        from theforge.cli import cmd_status

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = self._config(tmp_path)
        args = argparse.Namespace(run_id=None, recent=False, last=False, watch=3, no_color=True)

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
            patch("theforge.cli.status._find_active_run_id", return_value="run-1"),
            patch("theforge.cli.status._is_sprint_run", return_value=True),
            patch("theforge.cli.status_watch.is_tty", return_value=True),
            patch("theforge.cli.status_watch.run_watch_loop", return_value=0) as loop,
        ):
            rc = cmd_status(args)

        assert rc == 0
        loop.assert_called_once()
        # interval propagated, color disabled by --no-color flag.
        kwargs = loop.call_args.kwargs
        assert kwargs["color"] is False
        assert loop.call_args.args[2] == 3.0

    def test_tty_single_run_falls_back(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from theforge.cli import cmd_status

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = self._config(tmp_path)
        args = argparse.Namespace(run_id=None, recent=False, last=False, watch=2, no_color=False)
        mock_status = {"phase": "DEV", "cost_usd": 0.5, "elapsed_seconds": 60}

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
            patch("theforge.cli.status._find_active_run_id", return_value="run-1"),
            patch("theforge.cli.status._is_sprint_run", return_value=False),
            patch("theforge.cli.status_watch.is_tty", return_value=True),
            patch("theforge.detach.read_run_status", return_value=mock_status),
            patch("theforge.cli.status_watch.run_watch_loop", return_value=0) as loop,
            patch("theforge.pending.cleanup_stale"),
            patch("theforge.pending.list_pending", return_value=[]),
        ):
            rc = cmd_status(args)

        assert rc == 0
        loop.assert_not_called()
