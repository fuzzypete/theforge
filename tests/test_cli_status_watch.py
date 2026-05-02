"""Tests for `forge status --watch` live-update mode."""

from __future__ import annotations

import argparse
import os
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

    def test_watch_zero_rejected(self) -> None:
        parser = self._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["status", "--watch", "0"])

    def test_watch_negative_rejected(self) -> None:
        parser = self._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["status", "--watch", "-1"])

    def test_watch_non_integer_rejected(self) -> None:
        parser = self._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["status", "--watch", "abc"])


class TestCmdStatusInvalidWatch:
    def test_negative_watch_via_namespace_returns_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Direct Namespace callers (bypassing argparse) still get rejected."""
        from theforge.cli import cmd_status

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        from tests.test_cli_status import _make_forge_config

        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id=None, recent=False, last=False, watch=-1, no_color=False)
        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
        ):
            rc = cmd_status(args)
        assert rc == 2
        err = capsys.readouterr().err
        assert "positive integer" in err


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
        monkeypatch.setenv("TERM", "xterm-256color")

        class FakeStream:
            def isatty(self) -> bool:
                return True

        assert status_watch.color_enabled(False, FakeStream()) is True

    def test_term_dumb_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Colorless TTYs (TERM=dumb) must not receive ANSI color sequences."""
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("TERM", "dumb")

        class FakeStream:
            def isatty(self) -> bool:
                return True

        assert status_watch.color_enabled(False, FakeStream()) is False

    def test_term_unset_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("TERM", raising=False)

        class FakeStream:
            def isatty(self) -> bool:
                return True

        assert status_watch.color_enabled(False, FakeStream()) is False

    def test_term_unknown_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("TERM", "unknown")

        class FakeStream:
            def isatty(self) -> bool:
                return True

        assert status_watch.color_enabled(False, FakeStream()) is False


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

    def test_stall_threshold_defaults_from_interval(self) -> None:
        assert status_watch._stall_threshold_seconds(2.0) == 5.0
        assert status_watch._stall_threshold_seconds(10.0) == 20.0

    def test_stall_threshold_can_be_overridden(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(status_watch.STALL_THRESHOLD_ENV, "17")
        assert status_watch._stall_threshold_seconds(2.0) == 17.0


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
            text, ok, _err = status_watch.render_frame(
                "run-x", tmp_path, state, frame_idx=0, color=False
            )

        # First frame: no prior cost recorded → delta is 0 → rendered as em-dash.
        assert ok is True
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
            text, _ok, _err = status_watch.render_frame(
                "run-x", tmp_path, state, frame_idx=1, color=False
            )

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
            text, _ok, _err = status_watch.render_frame(
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
            text, _ok, _err = status_watch.render_frame(
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
            text, _ok, _err = status_watch.render_frame(
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

    def _render(
        self,
        tmp_path: Path,
        entries: list,
        *,
        now_fn=None,
        last_mtime=None,
        state: dict | None = None,
    ) -> str:
        if state is None:
            state = {"costs": {}, "interval": 2.0}
        with (
            patch("theforge.cli.sprint_status.display_sprint_status", return_value=0),
            patch("theforge.sprint.status_reader.read_live_status", return_value=entries),
            patch.object(status_watch, "_last_audit_mtime", return_value=last_mtime),
        ):
            text, _ok, _err = status_watch.render_frame(
                "run-x",
                tmp_path,
                state,
                frame_idx=0,
                color=False,
                now_fn=now_fn,
            )
        return text

    def test_header_uses_activity_and_event_age(self, tmp_path: Path) -> None:
        text = self._render(tmp_path, [])
        assert "ACTIVITY" in text
        assert "EVENT AGE" in text
        assert "ACT" not in text.split("ACTIVITY")[0]  # old short header gone
        assert "EVT AGE" not in text

    def test_running_live_label(self, tmp_path: Path) -> None:
        entries = [_entry("story-a", status="running")]
        text = self._render(
            tmp_path,
            entries,
            now_fn=lambda: 1000.0,
            last_mtime=999.0,
        )
        assert "live" in text

    def test_running_stalled_label(self, tmp_path: Path) -> None:
        entries = [_entry("story-a", status="running")]
        text = self._render(
            tmp_path,
            entries,
            now_fn=lambda: 1000.0,
            last_mtime=900.0,  # 100s ago — past stall threshold
        )
        assert "stalled" in text

    def test_stalled_story_surfaces_logs_hint(self, tmp_path: Path) -> None:
        entries = [_entry("story-a", status="running")]
        text = self._render(
            tmp_path,
            entries,
            now_fn=lambda: 1000.0,
            last_mtime=900.0,
        )
        assert "forge logs run-x" in text

    def test_done_label(self, tmp_path: Path) -> None:
        text = self._render(tmp_path, [_entry("story-a", status="done")])
        assert "done" in text

    def test_failed_label(self, tmp_path: Path) -> None:
        text = self._render(tmp_path, [_entry("story-a", status="failed")])
        assert "failed" in text

    def test_skipped_label(self, tmp_path: Path) -> None:
        text = self._render(tmp_path, [_entry("story-a", status="skipped")])
        assert "skipped" in text

    def test_blocked_label(self, tmp_path: Path) -> None:
        text = self._render(tmp_path, [_entry("story-a", status="blocked")])
        assert "blocked" in text


# ── run_watch_loop ────────────────────────────────────────────────────────────


class TestRunWatchLoop:
    def test_runs_max_frames_then_exits_cleanly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch.object(
                status_watch,
                "render_frame",
                return_value=("frame-body\n", True, ""),
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
                return_value=("frame-body\n", True, ""),
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
                return_value=("frame-body\n", True, ""),
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

    def test_follows_reexec_redirect_without_restarting_watch(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seen_run_ids: list[str] = []

        def fake_render_frame(
            run_id: str,
            _project_root: Path,
            state: dict,
            frame_idx: int,
            *,
            color: bool,
            now_fn=None,
        ) -> tuple[str, bool, str]:
            del state, frame_idx, color, now_fn
            seen_run_ids.append(run_id)
            return f"{run_id}\n", True, ""

        with (
            patch.object(status_watch, "render_frame", side_effect=fake_render_frame),
            patch.object(status_watch, "is_tty", return_value=False),
            patch.object(
                status_watch,
                "_resolve_followed_run_id",
                side_effect=["run-old", "run-new", "run-new"],
            ),
        ):
            rc = status_watch.run_watch_loop(
                "run-old",
                tmp_path,
                interval=0.01,
                color=False,
                sleep_fn=lambda _s: None,
                max_frames=3,
            )

        assert rc == 0
        assert seen_run_ids == ["run-old", "run-new", "run-new"]
        out = capsys.readouterr().out
        assert "run-old" in out
        assert "run-new" in out

    def test_reexec_redirect_resets_cost_deltas_for_new_run(self, tmp_path: Path) -> None:
        seen_costs: list[dict[str, float]] = []

        def fake_render_frame(
            run_id: str,
            _project_root: Path,
            state: dict,
            frame_idx: int,
            *,
            color: bool,
            now_fn=None,
        ) -> tuple[str, bool, str]:
            del run_id, frame_idx, color, now_fn
            seen_costs.append(dict(state["costs"]))
            state["costs"]["story-a"] = 1.25
            return "frame\n", True, ""

        with (
            patch.object(status_watch, "render_frame", side_effect=fake_render_frame),
            patch.object(status_watch, "is_tty", return_value=False),
            patch.object(
                status_watch,
                "_resolve_followed_run_id",
                side_effect=["run-old", "run-new"],
            ),
        ):
            rc = status_watch.run_watch_loop(
                "run-old",
                tmp_path,
                interval=0.01,
                color=False,
                sleep_fn=lambda _s: None,
                max_frames=2,
            )

        assert rc == 0
        assert seen_costs == [{}, {}]


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

    def test_tty_watch_waits_through_sprint_startup_window(self, tmp_path: Path) -> None:
        from theforge.cli import cmd_status

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = self._config(tmp_path)
        args = argparse.Namespace(run_id=None, recent=False, last=False, watch=3, no_color=False)

        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / "run-1.pid").write_text("123\nissues-test\n")

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
            patch("theforge.cli.status._find_active_run_id", return_value="run-1"),
            patch("theforge.cli.status._is_sprint_run", side_effect=[False, False, True]),
            patch("theforge.cli.status.time.sleep"),
            patch("theforge.cli.status.time.monotonic", side_effect=[0.0, 0.01, 0.02]),
            patch("theforge.cli.status_watch.is_tty", return_value=True),
            patch("theforge.cli.status_watch.run_watch_loop", return_value=0) as loop,
            patch("theforge.pending.cleanup_stale"),
            patch("theforge.pending.list_pending", return_value=[]),
        ):
            rc = cmd_status(args)

        assert rc == 0
        loop.assert_called_once()

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


# ── Review-feedback regression coverage ──────────────────────────────────────


class TestSnapshotFailurePropagates:
    """display_sprint_status returning non-zero on first frame must exit non-zero."""

    def test_first_frame_snapshot_failure_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch.object(
                status_watch,
                "render_frame",
                return_value=("", False, "snapshot diag\n"),
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
        # The captured diagnostic from display_sprint_status must reach stderr
        # so the operator sees WHY the watch session refused to start.
        err = capsys.readouterr().err
        assert "snapshot diag" in err

    def test_first_frame_failure_falls_back_when_no_captured_diagnostic(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """If the snapshot returned non-zero but emitted nothing, we still surface a message."""
        with (
            patch.object(
                status_watch,
                "render_frame",
                return_value=("", False, ""),
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
        assert "failed to read sprint state" in err


class TestRenderFrameCapturesStderr:
    """render_frame must keep stderr OUT of the terminal but RETURN it for the caller."""

    def test_render_frame_returns_captured_stderr(self, tmp_path: Path) -> None:
        def fake_display(_run_id: str, _project_root: Path) -> int:
            import sys as _sys

            print("boom: cannot read state", file=_sys.stderr)
            return 1

        with (
            patch(
                "theforge.cli.sprint_status.display_sprint_status",
                side_effect=fake_display,
            ),
            patch(
                "theforge.sprint.status_reader.read_live_status",
                return_value=[],
            ),
        ):
            text, ok, captured_err = status_watch.render_frame(
                "run-x", tmp_path, {"costs": {}, "interval": 2.0}, 0, color=False
            )

        assert ok is False
        assert "boom: cannot read state" in captured_err
        # stdout/text path stays clean
        assert "boom" not in text


class TestNoFullScreenClear:
    """Redraw must not issue ANSI clear-screen (\\x1b[2J)."""

    def test_redraw_uses_cursor_up_not_clear_screen(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch.object(
                status_watch,
                "render_frame",
                return_value=("line1\nline2\n", True, ""),
            ),
            patch.object(status_watch, "is_tty", return_value=False),
        ):
            status_watch.run_watch_loop(
                "run-x",
                tmp_path,
                interval=0.01,
                color=False,
                sleep_fn=lambda _s: None,
                max_frames=3,
            )

        out = capsys.readouterr().out
        # No full-screen clear sequence anywhere in the redraw output.
        assert "\x1b[2J" not in out
        # In-place redraw uses cursor-up + erase-below on subsequent frames.
        assert "\x1b[2A" in out  # CURSOR_UP(2) for the 2-line body
        assert status_watch.CLEAR_BELOW in out


class TestAuditMtimeRunScoping:
    """`_last_audit_mtime` must look only inside the resolved sprint log dir."""

    def test_returns_none_when_sprint_log_dir_is_none(self, tmp_path: Path) -> None:
        result = status_watch._last_audit_mtime("story-a", "run-x", tmp_path, sprint_log_dir=None)
        assert result is None

    def test_reads_only_from_passed_sprint_dir(self, tmp_path: Path) -> None:
        # Create two sibling sprint log dirs containing the same slug.
        old_dir = tmp_path / ".forge" / "logs" / "old-sprint"
        cur_dir = tmp_path / ".forge" / "logs" / "current-sprint"
        (old_dir / "story-a").mkdir(parents=True)
        (cur_dir / "story-a").mkdir(parents=True)
        old_audit = old_dir / "story-a" / "audit.yaml"
        cur_audit = cur_dir / "story-a" / "audit.yaml"
        old_audit.write_text("old")
        cur_audit.write_text("cur")

        # Make the OLD audit newer than the current to prove scoping.
        os.utime(old_audit, (2_000_000_000, 2_000_000_000))
        os.utime(cur_audit, (1_000_000_000, 1_000_000_000))

        result = status_watch._last_audit_mtime(
            "story-a", "run-x", tmp_path, sprint_log_dir=cur_dir
        )
        assert result == 1_000_000_000

    def test_resolve_sprint_log_dir_from_state_file(self, tmp_path: Path) -> None:
        sprint_name = "sprint-2025-04-27"
        sprint_dir = tmp_path / ".forge" / "logs" / sprint_name
        sprint_dir.mkdir(parents=True)
        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / "run-x.state").write_text(f"sprint_name: {sprint_name}\n")

        resolved = status_watch._resolve_sprint_log_dir("run-x", tmp_path)
        assert resolved == sprint_dir
