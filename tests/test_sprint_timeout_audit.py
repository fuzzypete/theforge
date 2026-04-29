from pathlib import Path
from unittest.mock import patch

from tests.test_sprint_resume import _make_config, _make_manifest, _make_spec_file
from theforge.sprint import run_sprint


def test_run_sprint_timeout_writes_story_audit(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    spec = _make_spec_file(tmp_path, "Feature A", "feature-a")
    manifest = _make_manifest(tmp_path, [spec.name])

    class _NeverDoneFuture:
        def cancel(self):
            return True

        def result(self):
            raise AssertionError("should not be called")

    class _FakeExecutor:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def submit(self, *args, **kwargs):
            return _NeverDoneFuture()

    with (
        patch("theforge.coordinator.workspace.pull_base_branch", return_value=True),
        patch(
            "theforge.sprint.runner._run_baseline_gate",
            return_value={"passed": True, "duration_seconds": 0.0, "message": "ok"},
        ),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.ThreadPoolExecutor", _FakeExecutor),
        patch("theforge.sprint.runner.wait", return_value=(set(), set())),
        patch("theforge.sprint.runner.time.monotonic", side_effect=[0.0, 4000.0, 4000.0]),
    ):
        run_sprint(config, manifest)

    audit_path = tmp_path / ".forge" / "audits" / "history.jsonl"
    assert audit_path.exists()
    assert "feature-a" in audit_path.read_text(encoding="utf-8")
