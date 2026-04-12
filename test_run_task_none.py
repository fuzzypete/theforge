from theforge.coordinator.engine import run_task
from theforge.coordinator.state import CoordinatorState
from theforge.task import TaskStory
from pathlib import Path
from unittest.mock import MagicMock, patch

state = CoordinatorState()
state.preflight_warnings = None
state.preflight_likely_files = None

config = MagicMock()
task = TaskStory(name="Test", story_path=Path("spec.md"), slug="test")

with patch("theforge.coordinator.preflight._apply_preflight_config"):
    try:
        run_task(config, task, cached_preflight_state=state)
    except Exception as e:
        print(f"CRASH: {type(e).__name__}: {e}")
