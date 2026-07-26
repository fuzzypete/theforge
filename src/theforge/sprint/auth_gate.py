"""Sprint-launch authentication readiness gate.

A sprint spends money and wall clock on remote agents. It should establish
that it can reach them *before* it begins, not discover per invocation that it
cannot. Sprint ``45fee01e8027`` (#1952) burned six minutes and produced a
failed story on a run where the very first agent call — and every one after it
— returned ``401 OAuth access token has been revoked``. That condition was
fully knowable before any story was dispatched.

This module owns the pre-dispatch half of the answer: inspect the credential
each configured profile will present, and refuse to launch when one carries
positive evidence that no agent is reachable. The post-launch half — a fatal
auth failure discovered *after* dispatch — is the circuit breaker in
:mod:`theforge.sprint.runner`.

Deliberately narrow in two directions:

- It refuses only on **credential** evidence. A missing CLI binary or unset API
  key is already surfaced as a startup warning; promoting those to a hard abort
  here would change unrelated behavior.
- It refuses only on **positive** evidence. Absence of an inspectable
  credential store is not evidence of a dead credential, and a false abort on a
  healthy host is a worse failure than the one this gate prevents.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING, NamedTuple

from ..config.auth import check_claude_credentials
from ..config.profiles import iter_config_profiles

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config.types import ForgeConfig


class SprintAuthUnavailable(RuntimeError):
    """A sprint cannot start because a configured agent credential is dead.

    Raised before any story is dispatched, so no story acquires a failure
    verdict and nothing about the work is asserted: the run never happened.
    """


class AuthGateFailure(NamedTuple):
    """One profile that cannot authenticate, with the operator-facing cause."""

    label: str
    profile_name: str
    reason: str


def check_sprint_auth_readiness(config: "ForgeConfig") -> list[AuthGateFailure]:
    """Return the credential failures that should prevent this sprint launching.

    Every CLI-backed Claude profile the run would invoke has its OAuth
    credential store inspected. Sandbox readiness is excluded — this gate asks
    whether an agent can be *reached*, and the sandbox posture is a separate,
    already-enforced concern with its own failure path.

    A profile whose credential store cannot be inspected, or whose probe raises,
    is skipped rather than reported: this gate refuses on evidence only.
    """
    failures: list[AuthGateFailure] = []
    seen: set[tuple[str, str]] = set()
    try:
        profiles = list(iter_config_profiles(config))
    except Exception:
        return failures

    # Same resolution order check_agent_auth uses: environment first, then the
    # project's own secrets overriding it.
    merged: dict[str, str] = {**os.environ, **(config.secrets or {})}

    for label, profile in profiles:
        # Only credential-store evidence aborts a launch. A missing binary or an
        # unset API key is pre-existing warning-level behavior and stays that
        # way — promoting it here would change unrelated failure paths.
        if getattr(profile, "cli", None) != "claude":
            continue
        try:
            ready, reason = check_claude_credentials(merged)
        except Exception:
            continue
        if ready:
            continue
        dedup_key = (profile.name, reason)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        failures.append(AuthGateFailure(label=label, profile_name=profile.name, reason=reason))
    return failures


def format_auth_gate_message(failures: list[AuthGateFailure]) -> str:
    """Render the operator-facing abort message for *failures*.

    Names the credential as the cause explicitly. An operator reading this must
    not be able to mistake it for a statement about the stories in the sprint.
    """
    lines = [
        "Sprint aborted before any story was dispatched: agent credentials are not usable.",
    ]
    for failure in failures:
        lines.append(f"  • {failure.label} profile {failure.profile_name!r}: {failure.reason}")
    lines.append(
        "No story was run and none was marked failed — this is a substrate "
        "condition, not a property of the work."
    )
    return "\n".join(lines)


def enforce_sprint_auth_readiness(
    config: "ForgeConfig",
    *,
    log: Callable[[str], None] | None = None,
) -> None:
    """Abort the sprint launch when a configured credential cannot authenticate.

    Raises:
        SprintAuthUnavailable: when at least one profile's credential store
            carries positive evidence that no agent is reachable.
    """
    failures = check_sprint_auth_readiness(config)
    if not failures:
        return
    message = format_auth_gate_message(failures)
    if log is not None:
        for line in message.splitlines():
            log(line)
    raise SprintAuthUnavailable(message)
