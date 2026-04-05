"""Coordinator helpers for scrubbing committed .forge artifacts from branch history."""

from __future__ import annotations

import os
import shlex
import stat
import tempfile
from pathlib import Path

from . import util as _cu


def _scrub_forge_history(workspace_path: Path, branch_name: str, base_branch: str) -> None:
    """Rewrite branch history to remove committed .forge artifacts.

    Drops commits whose entire diff touches only .forge/ paths and strips .forge/
    paths from mixed commits via an automated interactive rebase. Failures are
    logged as warnings but never raised so the dev flow remains automatic.
    """
    ok, out = _cu._run_shell(f"git log --format=%H origin/{base_branch}..HEAD", workspace_path)
    if not ok:
        return

    commits = [line.strip() for line in out.splitlines() if line.strip()]
    commit_prefixes = {sha[:7]: sha for sha in commits}
    if not commits:
        return

    forge_only: list[str] = []
    mixed: list[str] = []

    for sha in commits:
        ok_diff, diff_out = _cu._run_shell(
            f"git diff-tree --no-commit-id -r --name-only {sha}", workspace_path
        )
        if not ok_diff:
            continue
        files = [line.strip() for line in diff_out.splitlines() if line.strip()]
        if not files:
            continue
        forge_files = [path for path in files if path.startswith(".forge/")]
        if len(forge_files) == len(files):
            forge_only.append(sha)
        elif forge_files:
            mixed.append(sha)

    if not forge_only and not mixed:
        return

    _cu._log(
        "  WORKSPACE  scrubbing "
        f"{len(forge_only)} forge-only + {len(mixed)} mixed commits "
        f"from {branch_name}"
    )

    scrub_cmd = (
        "git rm -r -f --cached --ignore-unmatch -- .forge && "
        "git add -u && "
        "(git diff --cached --quiet || git commit --amend --no-edit)"
    )
    editor_script = "\n".join(
        [
            "#!/usr/bin/env python3",
            "import pathlib",
            "import sys",
            "",
            f"forge_only = {forge_only!r}",
            f"mixed = {mixed!r}",
            f"commit_prefixes = {commit_prefixes!r}",
            f"scrub_cmd = {scrub_cmd!r}",
            "",
            "todo_path = pathlib.Path(sys.argv[1])",
            'lines = todo_path.read_text(encoding="utf-8").splitlines()',
            "rewritten = []",
            "for line in lines:",
            "    stripped = line.strip()",
            '    if not stripped or stripped.startswith("#"):',
            "        rewritten.append(line)",
            "        continue",
            "    parts = stripped.split()",
            "    if len(parts) < 2:",
            "        rewritten.append(line)",
            "        continue",
            "    action, sha = parts[0], parts[1]",
            "    full_sha = next(",
            "        (v for k, v in commit_prefixes.items() if sha.startswith(k)), sha",
            "    )",
            "    if action not in {",
            '        "pick", "p", "reword", "r", "edit", "e",',
            '        "squash", "s", "fixup", "f"',
            "    }:",
            "        rewritten.append(line)",
            "        continue",
            "    if full_sha in forge_only:",
            '        rewritten.append(" ".join(["drop", sha, *parts[2:]]).rstrip())',
            "        continue",
            "    rewritten.append(line)",
            "    if full_sha in mixed:",
            '        rewritten.append(f"exec {scrub_cmd}")',
            'todo_path.write_text("\\n".join(rewritten) + "\\n", encoding="utf-8")',
            "",
        ]
    )

    script_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", delete=False, suffix=".py", encoding="utf-8"
        ) as handle:
            handle.write(editor_script)
            script_path = handle.name
        os.chmod(script_path, os.stat(script_path).st_mode | stat.S_IXUSR)
        env = os.environ.copy()
        env["GIT_SEQUENCE_EDITOR"] = f"python3 {shlex.quote(script_path)}"
        ok_rebase, rebase_out = _cu._run_shell(
            f"git rebase -i --keep-empty origin/{base_branch}",
            workspace_path,
            env=env,
        )
        if not ok_rebase:
            _cu._log(f"⚠ WORKSPACE  forge-history scrub failed: {rebase_out}")
    finally:
        if script_path is not None:
            try:
                Path(script_path).unlink()
            except OSError:
                pass
