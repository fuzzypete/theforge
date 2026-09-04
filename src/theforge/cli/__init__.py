"""TheForge CLI package.

Re-exports ``main`` and ``build_parser`` for backward compatibility,
plus all public command functions for direct import.
"""

from theforge.cli.audit import cmd_audit
from theforge.cli.author import cmd_author
from theforge.cli.baseline_fix import cmd_baseline_fix
from theforge.cli.daemon import cmd_daemon
from theforge.cli.hooks import cmd_init_hooks
from theforge.cli.ideate import cmd_ideate
from theforge.cli.index import cmd_index
from theforge.cli.init_commands import (
    _ensure_gitattributes,
    _ensure_gitignored,
    _generate_secrets_skeleton,
    _gitattributes_block,
    _gitignore_block,
    cmd_init,
    cmd_secrets_init,
    cmd_version,
)
from theforge.cli.main import build_parser, main
from theforge.cli.providers import cmd_check_providers
from theforge.cli.review import cmd_review
from theforge.cli.run import cmd_run
from theforge.cli.shared import (
    _apply_dev_model_override,
    _apply_plan_model_override,
    _build_task,
    _find_config,
    _parse_story_frontmatter,
    _write_audit,
)
from theforge.cli.sprint import cmd_sprint
from theforge.cli.status import cmd_decide, cmd_logs, cmd_status, cmd_stop
from theforge.cli.telemetry import cmd_telemetry
from theforge.cli.todo import cmd_todo

__all__ = [
    "main",
    "build_parser",
    "cmd_audit",
    "cmd_author",
    "cmd_baseline_fix",
    "cmd_check_providers",
    "cmd_daemon",
    "cmd_decide",
    "cmd_ideate",
    "cmd_index",
    "cmd_init",
    "cmd_init_hooks",
    "cmd_logs",
    "cmd_review",
    "cmd_run",
    "cmd_secrets_init",
    "cmd_sprint",
    "cmd_status",
    "cmd_stop",
    "cmd_telemetry",
    "cmd_todo",
    "cmd_version",
    "_apply_dev_model_override",
    "_apply_plan_model_override",
    "_build_task",
    "_ensure_gitattributes",
    "_ensure_gitignored",
    "_find_config",
    "_generate_secrets_skeleton",
    "_gitattributes_block",
    "_gitignore_block",
    "_parse_story_frontmatter",
    "_write_audit",
]
