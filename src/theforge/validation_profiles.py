"""Validation profile selection: which check runs, and what its result is worth.

Validation used to be two fixed command slots — one authoritative and complete,
one advisory with no stated relationship to it. Nothing in between could be
expressed: which checks are cheap, which result decides a merge, how a scoped
run differs from a complete one. A project now declares *named profiles*, each
carrying an authority, and this module is the single place that decides which
profile a phase gets (issue #2358).

Two invariants shape everything here:

* **Exactly one profile carries merge authority.** A gate verdict is written
  only from a run of that profile. Every other run is recorded as advisory, so
  a reviewer handed a result can tell a verdict from a signal.
* **Unknown inputs widen, never narrow.** A phase with no profile to select, a
  profile that resolves to nothing, an empty declaration — every one of those
  falls back to the complete merge-authority profile. The failure mode of this
  module is *more* validation running, never less.

Projects that declare nothing keep today's behaviour exactly: the legacy
``gate_command`` is synthesized into a ``complete`` merge-authority profile and
``test_command`` into an advisory one, so the same commands run in the same
places with the same standing they already had.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from theforge.config.types import (
    VALIDATION_AUTHORITY_ADVISORY,
    VALIDATION_AUTHORITY_MERGE,
    VALIDATION_PROFILE_COMPLETE,
    VALIDATION_PROFILE_FAST,
    VALIDATION_PROFILE_TARGETED,
    ValidationConfig,
    ValidationProfile,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from theforge.task.story import TaskStory

#: Which phase is asking. ``merge`` is the coordinator's authoritative gate;
#: ``advisory`` is the dev/fix inner loop, whose result never decides a merge.
PHASE_MERGE = "merge"
PHASE_ADVISORY = "advisory"

#: Advisory preference order. Scoped first (cheapest run that still says
#: something about *this* story), then the broad cheap profile, then — via
#: widening — the complete profile. Names outside this order cannot be declared:
#: the loader rejects them, so there is no such thing as a declared advisory
#: profile this function will not consider.
_ADVISORY_PREFERENCE: tuple[str, ...] = (
    VALIDATION_PROFILE_TARGETED,
    VALIDATION_PROFILE_FAST,
)

#: Profile name recorded for a run whose command did not come from a declared
#: profile at all — a story-level ``gate_override``. It is deliberately not one
#: of the declarable names: an undeclared command has no declared standing.
PROFILE_OVERRIDE = "override"
#: Profile name recorded when the gate was suppressed entirely (``gate: none``).
PROFILE_SKIPPED = "skipped"


@dataclass(frozen=True)
class SelectedValidation:
    """The profile a phase selected, and the command it resolved to.

    ``command`` has already had the scoping context forge supplies substituted
    into it. ``widened`` records that the requested selection produced nothing
    and the complete profile was used instead — the readable form of "unknown
    input caused more validation to run".
    """

    profile: str
    authority: str
    command: str
    declared: bool  # came from a project profile declaration, not the legacy slots
    widened: bool = False

    @property
    def is_merge_authority(self) -> bool:
        """True when a result from this run can establish a gate verdict."""
        return self.authority == VALIDATION_AUTHORITY_MERGE

    def describe(self) -> str:
        """One line naming the profile and its standing, for logs and prompts."""
        return f"{self.profile} ({self.authority} authority)"


def merge_profile(config_validation: ValidationConfig) -> ValidationProfile:
    """Return the profile whose result carries merge authority.

    Declared profiles win. With none declared the legacy ``gate_command`` *is*
    the complete profile — same command, same standing, now merely named.
    """
    declared = config_validation.declared_merge_profile()
    if declared is not None:
        return declared
    return ValidationProfile(
        name=VALIDATION_PROFILE_COMPLETE,
        command=config_validation.gate_command,
        authority=VALIDATION_AUTHORITY_MERGE,
    )


def advisory_profile(config_validation: ValidationConfig) -> ValidationProfile | None:
    """Return the profile a non-authoritative phase should run, if any.

    None means nothing advisory is available and the caller should widen to the
    merge-authority profile.
    """
    if config_validation.profiles:
        for name in _ADVISORY_PREFERENCE:
            candidate = config_validation.profile(name)
            if candidate is not None and not candidate.is_merge_authority:
                return candidate
        return None
    test_command = config_validation.test_command
    if test_command and test_command.strip():
        # The legacy advisory slot, named. Reported as ``fast`` because that is
        # what it has always been: a cheaper intermediate run with no bearing on
        # the merge decision.
        return ValidationProfile(
            name=VALIDATION_PROFILE_FAST,
            command=test_command,
            authority=VALIDATION_AUTHORITY_ADVISORY,
        )
    return None


def resolve_command(
    command: str,
    *,
    config_validation: ValidationConfig,
    task: "TaskStory | None" = None,
) -> str:
    """Substitute the scoping context forge supplies into a declared command.

    Forge contributes two facts and no interpretation: what this run is about
    (``{test_target}``, per-story with a project-declared default) and which
    story it is (``{slug}``). What to do with them is the declared command's
    business.
    """
    default_target = config_validation.default_test_target or "."
    test_target = (
        getattr(task, "test_target", None) if task is not None else None
    ) or default_target
    slug = getattr(task, "slug", None) if task is not None else None
    resolved = command.replace("{test_target}", test_target)
    return resolved.replace("{slug}", slug or "baseline")


def select_validation(
    config_validation: ValidationConfig,
    *,
    phase: str = PHASE_MERGE,
    task: "TaskStory | None" = None,
) -> SelectedValidation:
    """Select the profile appropriate to ``phase`` and resolve its command.

    The one selection path in the system: VALIDATE asks for ``merge`` and gets
    the single merge-authority profile; dev/fix ask for ``advisory`` and get a
    scoped or cheap profile whose result is explicitly not a verdict. Anything
    that cannot be selected for, or that resolves to an empty command, widens to
    the complete merge-authority profile.
    """
    authoritative = merge_profile(config_validation)
    declared = bool(config_validation.profiles)
    chosen: ValidationProfile | None = authoritative
    widened = False
    if phase == PHASE_ADVISORY:
        chosen = advisory_profile(config_validation)
        if chosen is None:
            chosen, widened = authoritative, True
    resolved = resolve_command(chosen.command, config_validation=config_validation, task=task)
    if not resolved.strip():
        # A selection that produced nothing is an unknown input, and unknown
        # inputs widen: run the complete profile rather than nothing at all.
        chosen, widened = authoritative, True
        resolved = resolve_command(
            authoritative.command, config_validation=config_validation, task=task
        )
    return SelectedValidation(
        profile=chosen.name,
        authority=chosen.authority,
        command=resolved,
        declared=declared,
        widened=widened,
    )


def validation_run_record(
    selection: SelectedValidation | None,
    *,
    result: str | None,
    commit: str | None = None,
    skipped: bool = False,
    worktree_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the record of one validation run: what ran, and what it is worth.

    Written for every validation run so the scope and standing behind a verdict
    is readable afterwards rather than reconstructed from a command string.

    A ``None`` selection with ``skipped`` means no command ran at all (a story
    ``gate: none`` override): recorded as a skipped advisory run, never as a
    passing complete one. A ``None`` selection *without* ``skipped`` is a run
    whose caller carries no profile information — a pre-#2358 record shape —
    and is read the way it always behaved: the complete profile, merge
    authority. Absence means legacy, not "untrusted".
    """
    if selection is None and skipped:
        return {
            "profile": PROFILE_SKIPPED,
            "authority": VALIDATION_AUTHORITY_ADVISORY,
            "command": None,
            "result": result,
            "commit": commit,
            "worktree_state": worktree_state,
            "skipped": True,
            "declared": False,
            "widened": False,
        }
    if selection is None:
        return {
            "profile": VALIDATION_PROFILE_COMPLETE,
            "authority": VALIDATION_AUTHORITY_MERGE,
            "command": None,
            "result": result,
            "commit": commit,
            "worktree_state": worktree_state,
            "skipped": False,
            "declared": False,
            "widened": False,
        }
    return {
        "profile": selection.profile,
        "authority": selection.authority,
        "command": selection.command,
        "result": result,
        "commit": commit,
        "worktree_state": worktree_state,
        "skipped": skipped,
        "declared": selection.declared,
        "widened": selection.widened,
    }


def override_selection(command: str, *, declared: bool) -> SelectedValidation:
    """Describe a story-level ``gate_override`` run.

    On the legacy path an override keeps its historical standing: it replaced
    the gate command and its result was the gate's result. Once a project
    declares profiles, an undeclared command cannot carry the authority of a
    declared one — the override still runs, but its result is advisory, so it
    cannot establish merge trust on behalf of a profile it is not.
    """
    return SelectedValidation(
        profile=PROFILE_OVERRIDE,
        authority=(VALIDATION_AUTHORITY_ADVISORY if declared else VALIDATION_AUTHORITY_MERGE),
        command=command,
        declared=False,
    )


def last_merge_authority_record(records: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Return the most recent recorded run that carried merge authority, if any.

    The record behind a verdict: what a reviewer, an audit reader, or a merge
    decision should consult when asking "what actually established this".
    """
    for record in reversed(records or []):
        if not isinstance(record, dict):
            continue
        if record.get("authority") == VALIDATION_AUTHORITY_MERGE and not record.get("skipped"):
            return record
    return None


def has_merge_authority_result(records: list[dict[str, Any]] | None) -> bool:
    """True when some recorded run of the merge-authority profile passed.

    The trust question in one place: not "did something exit zero" but "did the
    profile that carries merge authority return PASS".
    """
    for record in records or []:
        if not isinstance(record, dict):
            continue
        if record.get("authority") != VALIDATION_AUTHORITY_MERGE:
            continue
        if record.get("skipped"):
            continue
        if str(record.get("result") or "").upper() == "PASS":
            return True
    return False
