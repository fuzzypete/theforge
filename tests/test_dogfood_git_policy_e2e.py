"""End-to-end verification that TheForge's canonical git policy behaves correctly
against real per-run audit / knowledge artifacts, using real ``git``.

This is the committed, reproducible evidence for the dogfood story's
"per-run files behave as expected end-to-end" acceptance criterion. A live
``forge sprint`` is operator-gated (it spends money and requires explicit
authorization), so the runtime behavior is proven here deterministically and
without external cost:

- ``cmd_init(--shared-memory)`` writes the *real* canonical template.
- The *real* per-run writer (``_write_per_run_record``) lands a redacted record
  at ``.forge/audits/runs/{run_id}.json`` — the tracked path — with the
  redaction pass applied before the bytes hit disk.
- Actual ``git`` commands (``ls-files``, ``check-attr``) confirm which files
  travel with the repo (audit runs, knowledge summaries) and which stay local
  (secrets, worktrees, logs, root handoff), and that tracked per-run files are
  marked ``linguist-generated`` so GitHub collapses them in PR diffs.
- The runtime precondition guard blocks a run when a catastrophic machine-local
  path is tracked, and passes on the clean template.

Together these exercise the full chain the story promises to prove on real
traffic, so the same guarantees hold before any downstream project adopts it.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from theforge.cli import cmd_init
from theforge.cli.shared import _write_per_run_record, check_run_preconditions

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True
    )


def _tracked(repo: Path) -> set[str]:
    """Return the set of git-tracked paths after staging the whole worktree."""
    _git(repo, "add", "-A")
    out = _git(repo, "ls-files").stdout
    return {line for line in out.splitlines() if line}


@pytest.fixture
def initialized_repo(tmp_path, monkeypatch):
    """A fresh git repo initialized with the canonical --shared-memory template."""
    monkeypatch.chdir(tmp_path)
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    assert cmd_init(argparse.Namespace(shared_memory=True)) == 0
    return tmp_path


def _write_run_record(repo: Path, run_id: str, audit: dict) -> Path:
    """Drive the real per-run writer and return the record path."""
    result = SimpleNamespace(state=SimpleNamespace(run_id=run_id))
    config = SimpleNamespace(project_root=repo)
    audits_dir = repo / ".forge" / "audits"
    audits_dir.mkdir(parents=True, exist_ok=True)
    _write_per_run_record(result, config, audit, audits_dir)
    return audits_dir / "runs" / f"{run_id}.json"


class TestPerRunArtifactsTrackedUnderTemplate:
    """Per-run audit + knowledge summary files travel with the repo; local state does not."""

    def test_real_per_run_record_is_written_and_tracked(self, initialized_repo):
        run_id = "run-e2e-0123456789ab"
        audit = {"slug": "demo", "final_phase": "DONE", "forge_version": "9.9.9"}
        run_file = _write_run_record(initialized_repo, run_id, audit)

        assert run_file.exists(), "real per-run writer did not create the record"
        rel = str(run_file.relative_to(initialized_repo))
        assert rel == ".forge/audits/runs/run-e2e-0123456789ab.json"
        # The canonical template re-includes it — it travels with the repo.
        assert rel in _tracked(initialized_repo)

    def test_knowledge_summary_is_tracked(self, initialized_repo):
        summary = initialized_repo / ".forge" / "knowledge" / "summaries" / "run-e2e.yaml"
        summary.parent.mkdir(parents=True, exist_ok=True)
        summary.write_text("run_id: run-e2e\noutcome: DONE\n", encoding="utf-8")
        assert ".forge/knowledge/summaries/run-e2e.yaml" in _tracked(initialized_repo)

    def test_machine_local_and_secret_paths_stay_local(self, initialized_repo):
        repo = initialized_repo
        # Materialize the catastrophic / noise / secret paths a real run creates.
        for rel, body in [
            (".forge/.env", "OPENAI_API_KEY=sk-should-never-track\n"),
            (".forge/worktrees/wt1/file", "x"),
            (".forge/logs/run.log", "x"),
            (".forge/audits/history.jsonl", "{}\n"),
            (".forge/audits/index.sqlite", "binary"),
            ("handoff.yaml", "phase: DEV\n"),
        ]:
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")

        tracked = _tracked(repo)
        for rel in [
            ".forge/.env",
            ".forge/worktrees/wt1/file",
            ".forge/logs/run.log",
            ".forge/audits/history.jsonl",
            ".forge/audits/index.sqlite",
            "handoff.yaml",
        ]:
            assert rel not in tracked, f"{rel} must stay local under the canonical template"

    def test_tracked_per_run_files_are_linguist_generated(self, initialized_repo):
        repo = initialized_repo
        _write_run_record(repo, "run-attr-check", {"slug": "demo", "final_phase": "DONE"})
        summary = repo / ".forge" / "knowledge" / "summaries" / "run-attr-check.yaml"
        summary.parent.mkdir(parents=True, exist_ok=True)
        summary.write_text("run_id: run-attr-check\n", encoding="utf-8")

        for rel in [
            ".forge/audits/runs/run-attr-check.json",
            ".forge/knowledge/summaries/run-attr-check.yaml",
        ]:
            attr = _git(repo, "check-attr", "linguist-generated", "--", rel).stdout.strip()
            assert attr.endswith("linguist-generated: true"), (
                f"{rel} not collapsed in PR diffs: {attr}"
            )


class TestPerRunRedactionAtWrite:
    """The redaction spot-check, made reproducible: secrets never reach the on-disk record."""

    def test_secret_shaped_fields_and_env_values_are_scrubbed(self, initialized_repo):
        repo = initialized_repo
        # An env-file secret value that also appears inline in free text.
        env_secret = "SUPERSECRETVALUE1234567890"
        env_file = repo / ".forge" / ".env"
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text(f"MY_TOKEN={env_secret}\n", encoding="utf-8")
        audit = {
            "slug": "demo",
            "final_phase": "DONE",
            "forge_version": "9.9.9",
            "api_key": "live-api-key-should-vanish",
            "authorization": "Bearer abcdefabcdef",
            "runtime": {
                "environment": {
                    "HOME": "/home/x",
                    "OPENAI_API_KEY": "sk-nested-secret-xyz",
                },
            },
            "notes": f"leaked inline: {env_secret} in prose",
        }
        run_file = _write_run_record(repo, "run-redact-check", audit)
        data = json.loads(run_file.read_text(encoding="utf-8"))

        # Pass 1: secret-shaped keys are redacted.
        assert data["api_key"] == "[REDACTED]"
        assert data["authorization"] == "[REDACTED]"
        # Pass 3: environment dict collapses to a key-only list (values gone).
        env = data["runtime"]["environment"]
        assert isinstance(env, list)
        assert "OPENAI_API_KEY" in env
        raw = run_file.read_text(encoding="utf-8")
        assert "sk-nested-secret-xyz" not in raw
        # Pass 2: env-file values are scrubbed even in free-text fields.
        assert env_secret not in raw
        assert data["notes"] == "leaked inline: [REDACTED] in prose"
        # Envelope contract still intact.
        assert data["run_id"] == "run-redact-check"
        assert data["parent_run_id"] is None
        assert "schema_version" in data


class TestRuntimeGuardsUnderTemplate:
    """Story 5 runtime guards behave correctly against the canonical template."""

    def test_clean_template_has_no_blockers(self, initialized_repo):
        repo = initialized_repo
        _write_run_record(repo, "run-guard-clean", {"slug": "demo", "final_phase": "DONE"})
        _tracked(repo)  # stage everything the template allows
        blockers, _warnings = check_run_preconditions(repo)
        assert blockers == [], f"canonical template should not trip any blocker: {blockers}"

    def test_tracked_catastrophic_path_is_blocked(self, initialized_repo):
        repo = initialized_repo
        # Force-track a worktree file past the ignore rule to simulate a broken repo.
        wt = repo / ".forge" / "worktrees" / "wt1" / "state"
        wt.parent.mkdir(parents=True, exist_ok=True)
        wt.write_text("x", encoding="utf-8")
        _git(repo, "add", "-f", ".forge/worktrees/wt1/state")
        blockers, _warnings = check_run_preconditions(repo)
        assert any(".forge/worktrees/" in b for b in blockers)
        assert any("git rm --cached" in b for b in blockers)
