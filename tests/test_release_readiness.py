"""Tests for release readiness reporting in scripts/release.sh."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_release_dry_run_reports_untriaged_finding_count(tmp_path: Path) -> None:
    script_src = Path("scripts/release.sh")
    script_dst = tmp_path / "release.sh"
    shutil.copy(script_src, script_dst)

    (tmp_path / "pyproject.toml").write_text('version = "1.2.3.dev0"\n', encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        "## [Unreleased]\n\n## [1.2.3]\n\n- Existing release note.\n",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gh_log = tmp_path / "gh.log"

    _write_executable(fake_bin / "make", "#!/usr/bin/env bash\nexit 0\n")

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["BASH_FUNC_gh%%"] = f"""() {{
  printf '%s\\n' "$*" >> {gh_log}
  if [[ "$1 $2" == "issue list" ]]; then
    if [[ "$*" == *'--label forge-finding'* && "$*" == *'--label needs-triage'* ]]; then
      echo 2
    else
      echo 0
    fi
  fi
}}"""
    env["BASH_FUNC_git%%"] = """() {
  if [[ "$1 $2" == "status --porcelain" ]]; then
    return 0
  fi
  echo "+ git $*"
}"""
    result = subprocess.run(
        ["bash", str(script_dst), "--dry-run", "1.2.3"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Open needs-triage forge-findings: 2" in result.stdout

    gh_calls = gh_log.read_text(encoding="utf-8")
    assert "--label forge-finding" in gh_calls
    assert "--label needs-triage" in gh_calls
    assert "issue edit" not in gh_calls
