"""GitHub Action entrypoint for enforcing shape on issues and sweep runs.

Runs the shape_check module against an issue payload, applies/removes labels,
and posts a single bot-owned comment per issue (updated in place on edits).

Depends only on the shape_check subpackage and the stdlib. No imports from
coordinator, config, or provider adapters — the module must run in a minimal
GitHub Actions runtime.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Protocol, TextIO
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import quote, urlparse

from theforge.shape_check import Shape, ShapeResult, check
from theforge.shape_check.policy_digest import compute_policy_digest

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
    def list_open_issues(self) -> list[dict[str, Any]]: ...
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

    def _request_with_headers(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> tuple[Any, Any]:
        url = path if path.startswith(("https://", "http://")) else f"{self._base}{path}"
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
                return (json.loads(raw) if raw else None), resp.headers
        except urlerror.HTTPError as exc:
            if method == "DELETE" and exc.code == 404:
                return None, exc.headers
            raise

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        payload, _headers = self._request_with_headers(method, path, body)
        return payload

    def _next_page_path(self, link_header: str | None) -> str | None:
        if not link_header:
            return None
        repo_prefix = f"/repos/{self._repo}"
        for segment in link_header.split(","):
            parts = [part.strip() for part in segment.split(";")]
            if not any(part == 'rel="next"' for part in parts[1:]):
                continue
            url = parts[0].lstrip("<").rstrip(">")
            parsed = urlparse(url)
            path = parsed.path
            if path.startswith(repo_prefix):
                path = path[len(repo_prefix) :]
                return f"{path}?{parsed.query}" if parsed.query else path
            return url
        return None

    def _get_paginated(self, path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        next_path: str | None = path
        while next_path is not None:
            payload, headers = self._request_with_headers("GET", next_path)
            items.extend(list(payload or []))
            next_path = self._next_page_path(headers.get("Link"))
        return items

    def add_label(self, issue_number: int, label: str) -> None:
        self._request("POST", f"/issues/{issue_number}/labels", {"labels": [label]})

    def remove_label(self, issue_number: int, label: str) -> None:
        self._request("DELETE", f"/issues/{issue_number}/labels/{quote(label, safe='')}")

    def list_open_issues(self) -> list[dict[str, Any]]:
        issues = self._get_paginated("/issues?state=open&per_page=100")
        return [issue for issue in issues if not issue.get("pull_request")]

    def list_comments(self, issue_number: int) -> list[dict[str, Any]]:
        return self._get_paginated(f"/issues/{issue_number}/comments?per_page=100")

    def create_comment(self, issue_number: int, body: str) -> None:
        self._request("POST", f"/issues/{issue_number}/comments", {"body": body})

    def update_comment(self, comment_id: int, body: str) -> None:
        self._request("PATCH", f"/issues/comments/{comment_id}", {"body": body})

    def bot_login(self) -> str:
        return self._bot_login


# ----- Pure logic -----------------------------------------------------------


@dataclass(frozen=True)
class IssueState:
    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    author: str = "unknown"


@dataclass(frozen=True)
class CommentMutation:
    action: str
    body: str
    comment_id: int | None = None


@dataclass(frozen=True)
class IssueReconciliation:
    issue: IssueState
    result: ShapeResult
    add_labels: tuple[str, ...] = ()
    remove_labels: tuple[str, ...] = ()
    comment: CommentMutation | None = None

    @property
    def has_changes(self) -> bool:
        return bool(self.add_labels or self.remove_labels or self.comment is not None)


@dataclass(frozen=True)
class SweepPlan:
    open_issue_count: int
    changed_issues: tuple[IssueReconciliation, ...]
    unchanged_count: int

    @property
    def change_count(self) -> int:
        return sum(
            len(issue.add_labels) + len(issue.remove_labels) + int(issue.comment is not None)
            for issue in self.changed_issues
        )


def render_comment(result: ShapeResult, policy_digest: str) -> str:
    """Render a structured bot comment. First line is the hidden marker."""
    lines: list[str] = [COMMENT_MARKER]
    lines.append("")
    lines.append("**Story shape check**")
    lines.append("")
    lines.append(f"- shape: `{result.shape.value}`")
    lines.append(f"- admission verdict: `{result.verdict.value}`")
    lines.append(f"- suggested action: `{result.suggested_action.value}`")
    lines.append(f"- policy digest: `{policy_digest}`")
    lines.append("")
    if not result.reasons:
        lines.append("No findings — this issue looks runnable.")
    else:
        lines.append("| severity | code | detail |")
        lines.append("|---|---|---|")
        for reason in result.reasons:
            detail = reason.detail.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| `{reason.severity.value}` | `{reason.code}` | {detail} |")
    lines.append("")
    lines.append(
        "_This comment is maintained by the shape-check Action and will be "
        "updated on edits. See #806, #811._"
    )
    return "\n".join(lines)


def find_bot_comment(comments: list[dict[str, Any]], bot_login: str) -> dict[str, Any] | None:
    """Find the existing shape-check comment by author + marker, not substring scan."""
    for comment in comments:
        user = (comment.get("user") or {}).get("login") or ""
        body = comment.get("body") or ""
        if user == bot_login and COMMENT_MARKER in body:
            return comment
    return None


def _log_result(issue_number: int, result: ShapeResult, author: str) -> None:
    """Greppable one-line log for drift analysis."""
    codes = ",".join(reason.code for reason in result.reasons) or "-"
    logger.info(
        "shape_check issue=%d author=%s shape=%s verdict=%s action=%s reasons=%s",
        issue_number,
        author,
        result.shape.value,
        result.verdict.value,
        result.suggested_action.value,
        codes,
    )


def _needs_grooming_label(result: ShapeResult) -> bool:
    """Return whether the async relabeler should keep ``needs-grooming`` applied."""
    if result.shape in (Shape.TRACKING_ONLY, Shape.SUPERSEDED):
        return False
    return not result.admits_implementation_sprint


def _desired_tracking_label(result: ShapeResult) -> bool:
    return result.shape is Shape.TRACKING_ONLY


def _label_mutations(
    existing: set[str],
    result: ShapeResult,
    config: ActionConfig,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    add_labels: list[str] = []
    remove_labels: list[str] = []

    desired_needs_grooming = _needs_grooming_label(result)
    desired_tracking = _desired_tracking_label(result)

    if desired_needs_grooming and config.needs_grooming_label not in existing:
        add_labels.append(config.needs_grooming_label)
    elif not desired_needs_grooming and config.needs_grooming_label in existing:
        remove_labels.append(config.needs_grooming_label)

    if desired_tracking and config.tracking_label not in existing:
        add_labels.append(config.tracking_label)

    return tuple(add_labels), tuple(remove_labels)


def issue_from_payload(issue: dict[str, Any]) -> IssueState:
    return IssueState(
        number=int(issue["number"]),
        title=issue.get("title") or "",
        body=issue.get("body") or "",
        labels=tuple(label["name"] for label in issue.get("labels") or [] if label.get("name")),
        author=(issue.get("user") or {}).get("login") or "unknown",
    )


def plan_issue_reconciliation(
    issue: IssueState,
    comments: list[dict[str, Any]],
    *,
    bot_login: str,
    config: ActionConfig,
    policy_digest: str,
) -> IssueReconciliation:
    result = check(issue.title, issue.body, issue.labels)
    add_labels, remove_labels = _label_mutations(set(issue.labels), result, config)

    body_text = render_comment(result, policy_digest)
    existing_comment = find_bot_comment(comments, bot_login)
    comment: CommentMutation | None = None
    if existing_comment is None:
        comment = CommentMutation(action="create", body=body_text)
    elif (existing_comment.get("body") or "") != body_text:
        comment = CommentMutation(
            action="update",
            body=body_text,
            comment_id=int(existing_comment["id"]),
        )

    return IssueReconciliation(
        issue=issue,
        result=result,
        add_labels=add_labels,
        remove_labels=remove_labels,
        comment=comment,
    )


def apply_reconciliation(api: GitHubAPI, reconciliation: IssueReconciliation) -> None:
    issue_number = reconciliation.issue.number
    for label in reconciliation.add_labels:
        api.add_label(issue_number, label)
    for label in reconciliation.remove_labels:
        api.remove_label(issue_number, label)

    if reconciliation.comment is None:
        return
    if reconciliation.comment.action == "create":
        api.create_comment(issue_number, reconciliation.comment.body)
        return
    if reconciliation.comment.comment_id is None:
        raise AssertionError("update comment mutation requires comment_id")
    api.update_comment(reconciliation.comment.comment_id, reconciliation.comment.body)


def build_sweep_plan(
    api: GitHubAPI,
    config: ActionConfig,
    *,
    policy_digest: str,
) -> SweepPlan:
    changed_issues: list[IssueReconciliation] = []
    unchanged_count = 0
    issues = sorted(
        (issue_from_payload(issue) for issue in api.list_open_issues()),
        key=lambda issue: issue.number,
    )
    bot_login = api.bot_login()
    for issue in issues:
        reconciliation = plan_issue_reconciliation(
            issue,
            api.list_comments(issue.number),
            bot_login=bot_login,
            config=config,
            policy_digest=policy_digest,
        )
        if reconciliation.has_changes:
            changed_issues.append(reconciliation)
        else:
            unchanged_count += 1

    return SweepPlan(
        open_issue_count=len(issues),
        changed_issues=tuple(changed_issues),
        unchanged_count=unchanged_count,
    )


def _reason_codes(result: ShapeResult) -> str:
    codes = ",".join(reason.code for reason in result.reasons)
    return codes or "(now runnable)"


def _label_summary(
    label: str,
    action: str,
    reconciliation: IssueReconciliation,
    config: ActionConfig,
) -> str:
    if label == config.needs_grooming_label:
        if action == "add":
            return _reason_codes(reconciliation.result)
        if reconciliation.result.shape is Shape.TRACKING_ONLY:
            return "(tracking only)"
        if reconciliation.result.shape is Shape.SUPERSEDED:
            return "(superseded)"
        return "(now runnable)"

    if label == config.tracking_label:
        return "(tracking only)" if action == "add" else "(no longer tracking)"

    return _reason_codes(reconciliation.result)


def render_sweep_report(plan: SweepPlan, *, mode: str, config: ActionConfig) -> str:
    lines = [f"shape-check sweep — {mode} ({plan.open_issue_count} open issues)"]
    for reconciliation in plan.changed_issues:
        for label in reconciliation.add_labels:
            lines.append(
                f"  + {label}  #{reconciliation.issue.number}  "
                f"{_label_summary(label, 'add', reconciliation, config)}"
            )
        for label in reconciliation.remove_labels:
            lines.append(
                f"  - {label}  #{reconciliation.issue.number}  "
                f"{_label_summary(label, 'remove', reconciliation, config)}"
            )
        if reconciliation.comment is not None:
            prefix = "+" if reconciliation.comment.action == "create" else "~"
            lines.append(
                f"  {prefix} comment  #{reconciliation.issue.number}  "
                f"{reconciliation.comment.action}"
            )
    lines.append(f"{plan.unchanged_count} unchanged")
    lines.append(f"{plan.change_count} changes")
    return "\n".join(lines)


def run_sweep(
    api: GitHubAPI,
    config: ActionConfig | None = None,
    *,
    mode: str,
    output: TextIO,
    policy_digest: str | None = None,
) -> SweepPlan:
    config = config or ActionConfig()
    digest = policy_digest or compute_policy_digest()
    plan = build_sweep_plan(api, config, policy_digest=digest)
    if mode == "apply":
        for reconciliation in plan.changed_issues:
            apply_reconciliation(api, reconciliation)
    output.write(render_sweep_report(plan, mode=mode, config=config))
    output.write("\n")
    return plan


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
    issue = issue_from_payload(event["issue"])
    reconciliation = plan_issue_reconciliation(
        issue,
        api.list_comments(issue.number),
        bot_login=api.bot_login(),
        config=config,
        policy_digest=compute_policy_digest(),
    )
    _log_result(issue.number, reconciliation.result, issue.author)
    apply_reconciliation(api, reconciliation)
    return reconciliation.result


# ----- CLI entrypoint -------------------------------------------------------


def _load_event_from_env() -> dict[str, Any]:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        raise SystemExit("GITHUB_EVENT_PATH not set — must run inside a GitHub Action")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m theforge.shape_check")
    parser.add_argument("mode", nargs="?", choices=("preview", "apply"))
    return parser.parse_args(argv)


def _resolve_mode(args_mode: str | None) -> str | None:
    mode = args_mode or (os.environ.get("SHAPE_CHECK_MODE") or "").strip().lower() or None
    if mode is None:
        return None
    if mode not in {"preview", "apply"}:
        raise SystemExit("SHAPE_CHECK_MODE must be preview or apply")
    return mode


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("SHAPE_CHECK_LOG_LEVEL", "INFO"),
        format="%(levelname)s %(name)s %(message)s",
    )
    args = _parse_args(argv)
    mode = _resolve_mode(args.mode)

    repo = os.environ.get("GITHUB_REPOSITORY") or ""
    token = os.environ.get("GITHUB_TOKEN") or ""
    bot_login = os.environ.get("SHAPE_CHECK_BOT_LOGIN", "github-actions[bot]")
    config = ActionConfig(
        needs_grooming_label=os.environ.get(
            "SHAPE_CHECK_NEEDS_GROOMING_LABEL", DEFAULT_NEEDS_GROOMING_LABEL
        ),
        tracking_label=os.environ.get("SHAPE_CHECK_TRACKING_LABEL", DEFAULT_TRACKING_LABEL),
        auto_close_superseded=os.environ.get("SHAPE_CHECK_AUTO_CLOSE_SUPERSEDED") == "1",
    )

    if mode is not None:
        if not repo or not token:
            raise SystemExit("GITHUB_REPOSITORY and GITHUB_TOKEN must be set")
        api = HttpGitHubAPI(repo=repo, token=token, bot_login=bot_login)
        try:
            run_sweep(api, config, mode=mode, output=sys.stdout)
        except urlerror.HTTPError as exc:
            if mode == "preview" and exc.code == 403:
                message = (
                    "shape-check sweep preview unavailable: open issue listing returned 403 "
                    "(token lacks issues: read)"
                )
                logger.warning(message)
                print(message)
                return 0
            raise
        return 0

    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    if event_name != "issues":
        logger.info("shape_check skipped: event=%s is not issues", event_name)
        return 0

    event = _load_event_from_env()
    action_kind = event.get("action") or ""
    if action_kind not in ("opened", "edited"):
        logger.info("shape_check skipped: action=%s", action_kind)
        return 0

    if not repo or not token:
        raise SystemExit("GITHUB_REPOSITORY and GITHUB_TOKEN must be set")

    api = HttpGitHubAPI(repo=repo, token=token, bot_login=bot_login)
    run_action(event, api, config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
