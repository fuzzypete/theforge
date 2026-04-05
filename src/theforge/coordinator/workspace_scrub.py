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

    Drops commits whose entire diff touches only .forge/ artifact paths and strips
    those paths from mixed commits via an automated interactive rebase.

    Artifact detection is driven by ``git ls-tree origin/<base_branch> -- .forge/``:
    any .forge/ path that is already tracked in the base branch is treated as an
    intentional project file (hooks, .env.example, …) and is never touched.
    Everything else under .forge/ that appears in a branch-only commit is considered
    a runtime artifact and is eligible for scrubbing.

    Failures are logged as warnings but never raised so the dev flow remains automatic.
    """
    ok, out = _cu._run_shell(f"git log --format=%H origin/{base_branch}..HEAD", workspace_path)
    if not ok:
        return

    commits = [line.strip() for line in out.splitlines() if line.strip()]
    commit_prefixes = {sha[:7]: sha for sha in commits}
    if not commits:
        return

    # Files tracked in the base branch are intentional project files — preserve them.
    # Everything else under .forge/ that appears in a branch commit is a runtime artifact.
    ok_tree, tree_out = _cu._run_shell(
        f"git ls-tree -r --name-only origin/{base_branch} -- .forge/",
        workspace_path,
    )
    tracked_in_base: set[str] = set(tree_out.splitlines()) if ok_tree else set()

    forge_only: list[str] = []
    mixed: list[str] = []
    mixed_artifacts: dict[str, list[str]] = {}  # sha -> artifact paths to remove

    for sha in commits:
        ok_diff, diff_out = _cu._run_shell(
            f"git diff-tree --no-commit-id -r --name-only {sha}", workspace_path
        )
        if not ok_diff:
            continue
        files = [line.strip() for line in diff_out.splitlines() if line.strip()]
        if not files:
            continue
        artifact_files = [p for p in files if p.startswith(".forge/") and p not in tracked_in_base]
        if not artifact_files:
            continue
        real_files = [
            p for p in files if not (p.startswith(".forge/") and p not in tracked_in_base)
        ]
        if not real_files:
            forge_only.append(sha)
        else:
            mixed.append(sha)
            mixed_artifacts[sha] = artifact_files

    if not forge_only and not mixed:
        return

    _cu._log(
        "  WORKSPACE  scrubbing "
        f"{len(forge_only)} forge-only + {len(mixed)} mixed commits "
        f"from {branch_name}"
    )

    editor_script = "\n".join(
        [
            "#!/usr/bin/env python3",
            "import pathlib",
            "import shlex",
            "import sys",
            "",
            f"forge_only = {forge_only!r}",
            f"mixed = {mixed!r}",
            f"mixed_artifacts = {mixed_artifacts!r}",
            f"commit_prefixes = {commit_prefixes!r}",
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
            "        artifacts = mixed_artifacts.get(full_sha, [])",
            "        if artifacts:",
            "            rm_args = ' '.join(shlex.quote(p) for p in artifacts)",
            "            rewritten.append(",
            '                f"exec git rm -f --cached -- {rm_args} && "',
            '                f"(git diff --cached --quiet || git commit --amend --no-edit)"',
            "            )",
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
