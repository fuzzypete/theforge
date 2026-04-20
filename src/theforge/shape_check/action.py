"""GitHub Action entrypoint for enforcing shape on issues.opened/edited.

Runs the shape_check module against an issue payload, applies/removes labels,
and posts a single bot-owned comment per issue (updated in place on edits).

Depends only on the shape_check subpackage and the stdlib. No imports from
coordinator, config, or provider adapters — the module must run in a minimal
GitHub Actions runtime.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Protocol
from urllib import error as urlerror
from urllib import request as urlrequest

from theforge.shape_check import Shape, ShapeResult, check

COMMENT_MARKER = "<!-- shape-check-v1 -->"

DEFAULT_NEEDS_GROOMING_LABEL = "needs-grooming"
DEFAULT_TRACKING_LABEL = "epic"

logger = logging.getLogger("shape_check.action")


@dataclass(frozen=True)
class ActionConfig:
    needs_grooming_label: str = DEFAULT_NEEDS_GROOMING_LABEL
    tracking_label: str = DEFAULT_TRACKING_LABEL
    auto_close_superseded: bool = False


# ----- GitHub API boundary --------------------------------------------------


class GitHubAPI(Protocol):
    """Minimal surface of the GitHub REST API required by the action."""

    def add_label(self, issue_number: int, label: str) -> None: ...
    def remove_label(self, issue_number: int, label: str) -> None: ...
    def list_comments(self, issue_number: int) -> list[dict[str, Any]]: ...
    def create_comment(self, issue_number: int, body: str) -> None: ...
    def update_comment(self, comment_id: int, body: str) -> None: ...
    def bot_login(self) -> str: ...


class HttpGitHubAPI:
    """HTTP-backed GitHubAPI using stdlib urllib — no extra dependencies."""

    def __init__(self, repo: str, token: str, bot_login: str) -> None:
        self._repo = repo
        self._token = token
        self._bot_login = bot_login
        self._base = f"https://api.github.com/repos/{repo}"

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = f"{self._base}{path}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urlrequest.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "theforge-shape-check-action")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urlrequest.urlopen(req) as resp:  # noqa: S310 (trusted host)
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urlerror.HTTPError as exc:
            if method == "DELETE" and exc.code == 404:
                return None
            raise

    def add_label(self, issue_number: int, label: str) -> None:
        self._request("POST", f"/issues/{issue_number}/labels", {"labels": [label]})

    def remove_label(self, issue_number: int, label: str) -> None:
        from urllib.parse import quote

        self._request("DELETE", f"/issues/{issue_number}/labels/{quote(label, safe='')}")

    def list_comments(self, issue_number: int) -> list[dict[str, Any]]:
        result = self._request("GET", f"/issues/{issue_number}/comments?per_page=100")
        return list(result or [])

    def create_comment(self, issue_number: int, body: str) -> None:
        self._request("POST", f"/issues/{issue_number}/comments", {"body": body})

    def update_comment(self, comment_id: int, body: str) -> None:
        self._request("PATCH", f"/issues/comments/{comment_id}", {"body": body})

    def bot_login(self) -> str:
        return self._bot_login


# ----- Pure logic -----------------------------------------------------------


def render_comment(result: ShapeResult) -> str:
    """Render a structured bot comment. First line is the hidden marker."""
    lines: list[str] = [COMMENT_MARKER]
    lines.append("")
    lines.append("**Story shape check**")
    lines.append("")
    lines.append(f"- shape: `{result.shape.value}`")
    lines.append(f"- suggested action: `{result.suggested_action.value}`")
    lines.append("")
    if not result.reasons:
        lines.append("No findings — this issue looks runnable.")
    else:
        lines.append("| severity | code | detail |")
        lines.append("|---|---|---|")
        for r in result.reasons:
            detail = r.detail.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| `{r.severity.value}` | `{r.code}` | {detail} |")
    lines.append("")
    lines.append(
        "_This comment is maintained by the shape-check Action and will be "
        "updated on edits. See #806, #811._"
    )
    return "\n".join(lines)


def find_bot_comment(comments: list[dict[str, Any]], bot_login: str) -> dict[str, Any] | None:
    """Find the existing shape-check comment by author + marker, not substring scan."""
    for c in comments:
        user = (c.get("user") or {}).get("login") or ""
        body = c.get("body") or ""
        if user == bot_login and COMMENT_MARKER in body:
            return c
    return None


def _log_result(issue_number: int, result: ShapeResult, author: str) -> None:
    """Greppable one-line log for drift analysis."""
    codes = ",".join(r.code for r in result.reasons) or "-"
    logger.info(
        "shape_check issue=%d author=%s shape=%s action=%s reasons=%s",
        issue_number,
        author,
        result.shape.value,
        result.suggested_action.value,
        codes,
    )


def run_action(
    event: dict[str, Any],
    api: GitHubAPI,
    config: ActionConfig | None = None,
) -> ShapeResult:
    """Act on a GitHub issues.opened/edited event payload.

    Labels, posts/updates a bot comment, and returns the ShapeResult.
    Never raises on a well-formed payload unless the API itself fails.
    """
    config = config or ActionConfig()
    issue = event["issue"]
    issue_number = int(issue["number"])
    title = issue.get("title") or ""
    body = issue.get("body") or ""
    labels = [lbl["name"] for lbl in issue.get("labels") or [] if lbl.get("name")]
    author = (issue.get("user") or {}).get("login") or "unknown"

    result = check(title, body, labels)
    _log_result(issue_number, result, author)

    existing = set(labels)
    desired_needs_grooming = result.shape is Shape.NEEDS_GROOMING
    desired_tracking = result.shape is Shape.TRACKING_ONLY

    if desired_needs_grooming and config.needs_grooming_label not in existing:
        api.add_label(issue_number, config.needs_grooming_label)
    elif not desired_needs_grooming and config.needs_grooming_label in existing:
        api.remove_label(issue_number, config.needs_grooming_label)

    if desired_tracking and config.tracking_label not in existing:
        api.add_label(issue_number, config.tracking_label)

    # Comment: exactly one bot-owned comment, updated on edits.
    body_text = render_comment(result)
    comments = api.list_comments(issue_number)
    existing_comment = find_bot_comment(comments, api.bot_login())
    if existing_comment is None:
        api.create_comment(issue_number, body_text)
    elif (existing_comment.get("body") or "") != body_text:
        api.update_comment(int(existing_comment["id"]), body_text)

    return result


# ----- CLI entrypoint -------------------------------------------------------


def _load_event_from_env() -> dict[str, Any]:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        raise SystemExit("GITHUB_EVENT_PATH not set — must run inside a GitHub Action")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("SHAPE_CHECK_LOG_LEVEL", "INFO"),
        format="%(levelname)s %(name)s %(message)s",
    )
    del argv  # unused; action reads env vars

    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    if event_name != "issues":
        logger.info("shape_check skipped: event=%s is not issues", event_name)
        return 0

    event = _load_event_from_env()
    action_kind = event.get("action") or ""
    if action_kind not in ("opened", "edited"):
        logger.info("shape_check skipped: action=%s", action_kind)
        return 0

    repo = os.environ.get("GITHUB_REPOSITORY") or ""
    token = os.environ.get("GITHUB_TOKEN") or ""
    bot_login = os.environ.get("SHAPE_CHECK_BOT_LOGIN", "github-actions[bot]")
    if not repo or not token:
        raise SystemExit("GITHUB_REPOSITORY and GITHUB_TOKEN must be set")

    config = ActionConfig(
        needs_grooming_label=os.environ.get(
            "SHAPE_CHECK_NEEDS_GROOMING_LABEL", DEFAULT_NEEDS_GROOMING_LABEL
        ),
        tracking_label=os.environ.get("SHAPE_CHECK_TRACKING_LABEL", DEFAULT_TRACKING_LABEL),
        auto_close_superseded=os.environ.get("SHAPE_CHECK_AUTO_CLOSE_SUPERSEDED") == "1",
    )
    api = HttpGitHubAPI(repo=repo, token=token, bot_login=bot_login)
    run_action(event, api, config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
