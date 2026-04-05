"""Native GitHub PR integration helpers."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from theforge.config import ModelProfile
from theforge.review import ReviewResult

_pr_log = logging.getLogger(__name__)


def assign_pr_reviewers(pr_url: str, reviewer_profiles: list[ModelProfile], cwd: Path) -> dict:
    """Assign configured GitHub reviewers to a PR via gh CLI."""
    handles = [profile.github_handle for profile in reviewer_profiles if profile.github_handle]
    if not handles:
        return {"success": True, "error": None}

    try:
        proc = subprocess.run(
            ["gh", "pr", "edit", pr_url, "--add-reviewer", ",".join(handles)],
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=30,
        )
        if proc.returncode == 0:
            return {"success": True, "error": None}
        err = proc.stderr.strip() or proc.stdout.strip()
        _pr_log.warning("PR reviewer assignment failed (gh exited %d): %s", proc.returncode, err)
        if "429" in err:
            _pr_log.warning("GitHub API rate limit warning during reviewer assignment: %s", err)
        return {"success": False, "error": err}
    except Exception as exc:
        _pr_log.warning("PR reviewer assignment failed: %s", exc)
        return {"success": False, "error": str(exc)}


def post_findings_comment(
    pr_url: str,
    parsed_review: ReviewResult,
    reviewer_profiles: list[ModelProfile],
    cwd: Path,
) -> dict:
    """Post structured review findings to a PR via gh CLI."""
    findings = [f for f in parsed_review.findings if f.severity in {"P1", "P2"}]
    lines = [
        "## Forge Review Findings",
        "",
        f"**Verdict:** {parsed_review.verdict}",
        "",
    ]
    if findings:
        for finding in findings:
            location = finding.file
            if finding.line is not None:
                location = f"{location}:{finding.line}"
            lines.append(f"- **{finding.severity}** `{location}` — {finding.description}")
            if finding.suggestion:
                lines.append(f"  - Suggestion: {finding.suggestion}")
    else:
        lines.append("_No P1/P2 findings._")

    lines.extend(["", "Reviewed by:"])
    for profile in reviewer_profiles:
        lines.append(f"- {profile.name}")

    body = "\n".join(lines)

    try:
        proc = subprocess.run(
            ["gh", "pr", "comment", pr_url, "--body", body],
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=30,
        )
        if proc.returncode == 0:
            return {"success": True, "error": None}
        err = proc.stderr.strip() or proc.stdout.strip()
        _pr_log.warning("PR findings comment failed (gh exited %d): %s", proc.returncode, err)
        if "429" in err:
            _pr_log.warning("GitHub API rate limit warning during PR comment: %s", err)
        return {"success": False, "error": err}
    except Exception as exc:
        _pr_log.warning("PR findings comment failed: %s", exc)
        return {"success": False, "error": str(exc)}
