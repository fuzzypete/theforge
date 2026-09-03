"""Fetch the facts a spike closure decision needs, and make the decision.

Every path that closes an issue — the sprint's landing close, the ALREADY_DONE
dispositions, ``forge todo``'s close, triage ratification, and the repository's
GitHub Actions — routes through :func:`check_spike_closure` immediately before
issuing ``gh issue close``. A non-spike is unaffected; a spike closes only on
one of its two legal exits (#2600).

The rule itself lives in :mod:`theforge.spike_guard.outcome` and is pure. This
module is only the ``gh`` boundary: it reads the spike, its comments and the
follow-on issue, and hands those facts to the rule.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from .outcome import (
    ClosureDecision,
    IssueFacts,
    evaluate_spike_closure,
    required_follow_up,
)

_log = logging.getLogger(__name__)

_GH_TIMEOUT_SECONDS = 30

__all__ = ["check_spike_closure", "ClosureDecision"]


def _gh_json(args: list[str], project_root: Path) -> dict | None:
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=_GH_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception as exc:
        _log.warning("gh %s failed: %s", " ".join(args), exc)
        return None
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip()
        _log.warning("gh %s failed (exit %d): %s", " ".join(args), proc.returncode, err)
        return None
    try:
        data = json.loads(proc.stdout or "{}")
    except (TypeError, ValueError) as exc:
        _log.warning("gh %s returned malformed JSON: %s", " ".join(args), exc)
        return None
    return data if isinstance(data, dict) else None


def _label_names(data: dict) -> tuple[str, ...]:
    return tuple(
        str(lbl.get("name", "")).strip()
        for lbl in (data.get("labels") or [])
        if isinstance(lbl, dict) and str(lbl.get("name", "")).strip()
    )


def _fetch_issue(number: int, project_root: Path, fields: str) -> IssueFacts | None:
    data = _gh_json(["issue", "view", str(number), "--json", fields], project_root)
    if data is None:
        return None
    return IssueFacts(
        number=number,
        state=str(data.get("state") or "OPEN"),
        labels=_label_names(data),
        body=str(data.get("body") or ""),
    )


def _fetch_comments(number: int, project_root: Path) -> list[str]:
    data = _gh_json(["issue", "view", str(number), "--json", "comments"], project_root)
    if data is None:
        return []
    return [
        str(comment.get("body") or "")
        for comment in (data.get("comments") or [])
        if isinstance(comment, dict)
    ]


def check_spike_closure(
    number: int,
    project_root: Path,
    *,
    known_type: str | None = None,
    closing_comment: str | None = None,
) -> ClosureDecision:
    """Return whether issue ``number`` may be closed.

    ``known_type`` is the caller's already-resolved story type. Passing a
    non-spike type short-circuits with no ``gh`` call at all, which keeps the
    guard off the hot path of ordinary closes: a transient ``gh`` failure must
    never hold an ordinary story issue open, because merge detection keys off
    issue closure (#2111).

    Where spike-ness is *not* already known, a failed lookup refuses the close.
    That is the conservative direction — an issue left open is recoverable,
    a spike closed with no recorded outcome is exactly the decay this guards
    against — and it is only reachable from operator-driven commands and
    repository workflows, where the refusal is reported rather than silent.

    ``closing_comment`` is the comment the caller is about to post with the
    close, so an outcome recorded in that comment counts.
    """
    if known_type is not None and known_type.strip().lower() != "spike":
        return ClosureDecision(True, f"#{number} is typed {known_type!r}; not a spike")

    spike = _fetch_issue(number, project_root, "state,labels,body")
    if spike is None:
        return ClosureDecision(
            False,
            f"could not read issue #{number} to check whether it is a spike; refusing "
            "to close rather than risk closing a spike with no recorded outcome",
        )
    if not spike.is_spike:
        return ClosureDecision(True, f"#{number} is not a spike; closure is unchanged")

    texts: list[str] = []
    if closing_comment:
        texts.append(closing_comment)
    texts.append(spike.body)
    texts.extend(_fetch_comments(number, project_root))

    follow_up_number = required_follow_up(texts)
    follow_up = (
        _fetch_issue(follow_up_number, project_root, "state,labels,body")
        if follow_up_number is not None
        else None
    )
    return evaluate_spike_closure(spike, texts=texts, follow_up=follow_up)
