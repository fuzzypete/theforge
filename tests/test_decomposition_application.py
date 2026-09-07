"""Applying an accepted decomposition proposal (#2824).

The unit under test is the mutation itself: what it creates, in what order,
what it writes into the created bodies, when it closes the original, and what
it does when a create fails partway through. Every ``gh`` call is stubbed —
these assertions are about the argv the application would issue, which is
exactly the surface a real run's correctness rests on.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from theforge.coordinator.decomposition_application import (
    APPLY_STATUS_APPLIED,
    APPLY_STATUS_FAILED,
    DECOMPOSITION_MARKER,
    ApplicationRefused,
    apply_decomposition,
    dependency_order,
    proposal_is_appliable,
)
from theforge.task import TaskStory


def _assessment() -> dict:
    """A four-slice proposal whose edges form a diamond, as the example shows."""
    return {
        "slices": [
            {
                "id": 1,
                "title": "extract the portable diagnosis record",
                "scope": "Define the record type and its serialization; leave every caller alone.",
                "depends_on": [],
                "covers_criteria": [1],
            },
            {
                "id": 2,
                "title": "route diagnosis through the record",
                "scope": "Assemble diagnosis output from the record. Leaves the CLI to slice 3.",
                "depends_on": [1],
                "covers_criteria": [2],
            },
            {
                "id": 3,
                "title": "port the CLI surface",
                "scope": "Read the record in the CLI printer; no change to the record itself.",
                "depends_on": [1],
                "covers_criteria": [3],
            },
            {
                "id": 4,
                "title": "cross-project acceptance",
                "scope": "Prove the record survives an end-to-end run in a second project.",
                "depends_on": [2, 3],
                "covers_criteria": [4],
            },
        ],
        "unsettled": ["whether AC 4 belongs with slice 3"],
    }


_STORY = """\
## What

Make the diagnosis record portable across projects.

## Acceptance criteria

- The record type exists and round-trips through the audit.
- Diagnosis output is assembled from the record rather than a per-call dict.
- The CLI prints from the record.
- A second project consumes the record end to end.
"""


def _task(tmp_path: Path, *, issue: int | None = 2541, type_: str | None = "enhancement"):
    return TaskStory(
        name="Make the diagnosis record portable",
        slug="issue-2541",
        story_path=None,
        story_text=_STORY,
        github_issue=issue,
        type=type_,
    )


class _Config:
    def __init__(self, root: Path) -> None:
        self.project_root = root


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["gh"], returncode=1, stdout="", stderr=stderr)


class FakeGh:
    """A ``gh`` stand-in that hands out issue numbers and records every call."""

    def __init__(
        self,
        *,
        labels=("enhancement",),
        milestone="v0.16.0",
        fail_on_slice=None,
        state="OPEN",
        state_reason=None,
    ):
        self.calls: list[list[str]] = []
        self.labels = list(labels)
        self.milestone = milestone
        self.fail_on_slice = fail_on_slice
        # The original's live state, which `gh issue close` moves the way the
        # real command does — closing an already-closed issue leaves its reason
        # alone, which is the whole reason the close is verified by reading back.
        self.state = state
        self.state_reason = state_reason
        self.next_number = 2900

    def __call__(self, args: list[str], project_root: Path):
        self.calls.append(list(args))
        if args[:3] == ["gh", "issue", "view"]:
            fields = args[args.index("--json") + 1].split(",")
            payload: list[str] = []
            if "state" in fields:
                payload.append(f'"state": "{self.state}"')
            if "stateReason" in fields:
                reason = f'"{self.state_reason}"' if self.state_reason else "null"
                payload.append(f'"stateReason": {reason}')
            if "labels" in fields:
                labels = ", ".join(f'{{"name": "{name}"}}' for name in self.labels)
                payload.append(f'"labels": [{labels}]')
            if "milestone" in fields:
                payload.append(
                    f'"milestone": {{"title": "{self.milestone}"}}'
                    if self.milestone
                    else '"milestone": null'
                )
            return _ok("{" + ", ".join(payload) + "}")
        if args[:3] == ["gh", "issue", "create"]:
            title = args[args.index("--title") + 1]
            if self.fail_on_slice is not None and title.startswith(self.fail_on_slice):
                return _fail("gh: API rate limit exceeded")
            number = self.next_number
            self.next_number += 1
            return _ok(f"https://github.com/acme/theforge/issues/{number}")
        if args[:3] == ["gh", "issue", "close"]:
            if self.state == "OPEN":
                self.state = "CLOSED"
                self.state_reason = "NOT_PLANNED"
            return _ok("")
        raise AssertionError(f"unexpected gh call: {args}")

    def creates(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["gh", "issue", "create"]]

    def closes(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["gh", "issue", "close"]]

    def body_for(self, title_prefix: str) -> str:
        for call in self.creates():
            if call[call.index("--title") + 1].startswith(title_prefix):
                return call[call.index("--body") + 1]
        raise AssertionError(f"no create call for {title_prefix!r}")


def _apply(tmp_path, gh, **kwargs):
    return apply_decomposition(
        assessment=kwargs.pop("assessment", _assessment()),
        task=kwargs.pop("task", _task(tmp_path)),
        config=_Config(tmp_path),
        runner=gh,
        **kwargs,
    )


# ── Creation, content, and edges ──────────────────────────────────────────────


def test_accepting_creates_one_issue_per_slice_with_title_and_scope(tmp_path):
    gh = FakeGh()
    outcome = _apply(tmp_path, gh)

    assert outcome.status == APPLY_STATUS_APPLIED
    assert len(outcome.created) == 4
    assert [item.issue_number for item in outcome.created] == [2900, 2901, 2902, 2903]
    for call in gh.creates():
        title = call[call.index("--title") + 1]
        body = call[call.index("--body") + 1]
        # Both halves of the slice travel into the issue: the title names it,
        # the scope boundary is what makes it a different story from its siblings.
        assert title
        assert body.count("## What") == 1
    assert gh.body_for("extract the portable").splitlines()[-20:]
    assert "Define the record type" in gh.body_for("extract the portable")


def test_declared_edges_are_written_as_frontmatter_at_creation_time(tmp_path):
    gh = FakeGh()
    _apply(tmp_path, gh)

    # Slice 2 depends on slice 1, which was created as #2900. The edge is in the
    # created body — the form sprint.sources parses as a hard dependency — and
    # not added by a later edit or comment, which the scheduler never reads.
    body = gh.body_for("route diagnosis")
    assert body.startswith("---\ndepends_on:\n  - issue-2900\n---")
    assert "Depends on #2900" in body

    # The diamond's join carries both of its edges.
    joined = gh.body_for("cross-project acceptance")
    assert "  - issue-2901" in joined.split("---")[1]
    assert "  - issue-2902" in joined.split("---")[1]

    # A root slice declares no edge at all rather than an empty block.
    assert not gh.body_for("extract the portable").startswith("---")
    assert "issue issue edit" not in str(gh.calls)
    assert not [c for c in gh.calls if c[:3] == ["gh", "issue", "edit"]]


def test_created_slices_carry_the_originals_type_label_and_milestone(tmp_path):
    gh = FakeGh(labels=("enhancement", "v0.16"), milestone="v0.16.0")
    _apply(tmp_path, gh)

    for call in gh.creates():
        assert call[call.index("--label") + 1] == "enhancement"
        assert call[call.index("--milestone") + 1] == "v0.16.0"


def test_created_slices_carry_the_original_acceptance_criteria_they_cover(tmp_path):
    gh = FakeGh()
    _apply(tmp_path, gh)

    assert "The record type exists" in gh.body_for("extract the portable")
    assert "The CLI prints from the record" in gh.body_for("port the CLI")
    # And only theirs: a slice does not inherit criteria the proposal put elsewhere.
    assert "The CLI prints from the record" not in gh.body_for("extract the portable")


def test_created_slices_are_runnable_at_creation(tmp_path):
    """Each rendered body passes the shared issue-shape gate as RUNNABLE.

    A slice that exists but is skipped by intake is the failure "runnable at
    creation" names, so the rendering is checked against the same gate a sprint
    admits issues through rather than against this module's own idea of shape.
    """
    from theforge.shape_check import check
    from theforge.shape_check.types import ShapeVerdict

    gh = FakeGh()
    _apply(tmp_path, gh)
    for call in gh.creates():
        title = call[call.index("--title") + 1]
        body = call[call.index("--body") + 1]
        labels = call[call.index("--label") + 1].split(",")
        result = check(title, body, labels)
        assert result.verdict is ShapeVerdict.RUNNABLE, (title, result.reasons)


def test_every_created_body_carries_the_decomposition_marker(tmp_path):
    gh = FakeGh()
    _apply(tmp_path, gh)
    for call in gh.creates():
        body = call[call.index("--body") + 1]
        assert f"<!-- {DECOMPOSITION_MARKER} source=2541 slice=" in body


# ── Closing the original ──────────────────────────────────────────────────────


def test_original_closes_last_and_as_decomposed_not_completed(tmp_path):
    gh = FakeGh()
    outcome = _apply(tmp_path, gh)

    assert outcome.source_issue_closed is True
    close = gh.closes()[0]
    assert close[3] == "2541"
    assert close[close.index("--reason") + 1] == "not planned"
    comment = close[close.index("--comment") + 1]
    assert "decomposed, not as completed" in comment
    for number in (2900, 2901, 2902, 2903):
        assert f"#{number}" in comment
    # Ordering: every create precedes the close.
    assert gh.calls.index(close) > max(gh.calls.index(c) for c in gh.creates())
    # And the close is verified by reading the issue back — a `gh issue close`
    # against an already-closed issue exits zero without changing its reason,
    # so the exit code is not evidence the original reads as decomposed.
    assert gh.calls[-1][:3] == ["gh", "issue", "view"]
    assert "stateReason" in gh.calls[-1][gh.calls[-1].index("--json") + 1]


def test_a_spike_original_closes_with_a_recorded_outcome(tmp_path):
    """The close routes through the repository-wide spike closure guard."""
    from theforge.spike_guard import OUTCOME_MARKER
    from theforge.spike_guard.outcome import ClosureDecision

    gh = FakeGh(labels=("spike",))
    seen: dict = {}

    def _guard(number, project_root, *, known_type=None, closing_comment=None):
        seen["number"] = number
        seen["comment"] = closing_comment
        return ClosureDecision(True, "outcome recorded")

    with patch("theforge.spike_guard.check_spike_closure", _guard):
        outcome = _apply(tmp_path, gh, task=_task(tmp_path, type_="spike"))

    assert outcome.status == APPLY_STATUS_APPLIED
    assert seen["number"] == 2541
    assert OUTCOME_MARKER in seen["comment"]
    assert "follow-up: #2900" in seen["comment"]


def test_a_refused_spike_close_leaves_the_original_open_and_reports_it(tmp_path):
    from theforge.spike_guard.outcome import ClosureDecision

    gh = FakeGh(labels=("spike",))

    def _guard(number, project_root, *, known_type=None, closing_comment=None):
        return ClosureDecision(False, "no recorded outcome")

    with patch("theforge.spike_guard.check_spike_closure", _guard):
        outcome = _apply(tmp_path, gh, task=_task(tmp_path, type_="spike"))

    assert outcome.status == APPLY_STATUS_FAILED
    assert outcome.source_issue_closed is False
    assert gh.closes() == []
    assert len(outcome.created) == 4
    assert "was not durably closed as decomposed" in outcome.error


# ── Partial failure ───────────────────────────────────────────────────────────


def test_a_failed_create_leaves_the_original_open_and_reports_what_exists(tmp_path):
    gh = FakeGh(fail_on_slice="port the CLI")
    outcome = _apply(tmp_path, gh)

    assert outcome.status == APPLY_STATUS_FAILED
    assert outcome.source_issue_closed is False
    assert gh.closes() == []
    # What was created is named, so the operator acts on the tracker rather than
    # searching it.
    assert [item.issue_number for item in outcome.created] == [2900, 2901]
    assert "rate limit" in outcome.error


def test_reentry_after_a_partial_failure_creates_only_the_missing_slices(tmp_path):
    first = FakeGh(fail_on_slice="port the CLI")
    partial = _apply(tmp_path, first)
    assert len(partial.created) == 2

    second = FakeGh()
    second.next_number = 2910
    outcome = _apply(
        tmp_path,
        second,
        prior_created=[item.to_dict() for item in partial.created],
    )

    assert outcome.status == APPLY_STATUS_APPLIED
    created_titles = [c[c.index("--title") + 1] for c in second.creates()]
    assert created_titles == ["port the CLI surface", "cross-project acceptance"]
    # The reused numbers, not fresh ones, are what the dependants point at.
    assert second.body_for("cross-project acceptance").split("---")[1].count("issue-2901") == 1
    assert [item.issue_number for item in outcome.created] == [2900, 2901, 2910, 2911]


def test_a_fully_applied_split_reentered_creates_nothing(tmp_path):
    gh = FakeGh()
    applied = _apply(tmp_path, gh)

    again = FakeGh()
    outcome = _apply(
        tmp_path,
        again,
        prior_created=[item.to_dict() for item in applied.created],
        source_issue_already_closed=True,
    )

    assert outcome.status == APPLY_STATUS_APPLIED
    assert again.creates() == []
    assert again.closes() == []


# ── Refusals: nothing is created ──────────────────────────────────────────────


def test_a_cyclic_proposal_refuses_before_creating_anything(tmp_path):
    assessment = _assessment()
    assessment["slices"][0]["depends_on"] = [4]
    gh = FakeGh()
    outcome = _apply(tmp_path, gh, assessment=assessment)

    assert outcome.status == APPLY_STATUS_FAILED
    assert "cycle" in outcome.error
    assert gh.creates() == []
    assert gh.closes() == []


def test_a_label_edit_that_removed_the_type_falls_back_to_the_storys_own(tmp_path):
    """The offer was made from the type intake derived; a later label edit does
    not invalidate the split the operator accepted, so the slices inherit that
    type rather than the application failing on something they never saw."""
    gh = FakeGh(labels=("needs-grooming",))
    outcome = _apply(tmp_path, gh)

    assert outcome.status == APPLY_STATUS_APPLIED
    for call in gh.creates():
        assert call[call.index("--label") + 1] == "enhancement"


def test_an_original_with_no_appliable_type_anywhere_refuses(tmp_path):
    gh = FakeGh(labels=("needs-grooming",))
    outcome = _apply(tmp_path, gh, task=_task(tmp_path, type_="bug"))

    assert outcome.status == APPLY_STATUS_FAILED
    assert "runnable at creation" in outcome.error
    assert gh.creates() == []


def test_an_original_relabelled_to_a_non_appliable_type_refuses(tmp_path):
    """Relabelled ``bug`` after intake read it as an enhancement.

    The relabelling is a decision someone made after the proposal was produced:
    creating enhancement slices from the stale type and closing the live bug
    would act on a story that no longer exists as described.
    """
    gh = FakeGh(labels=("bug",))
    outcome = _apply(tmp_path, gh)

    assert outcome.status == APPLY_STATUS_FAILED
    assert "is now typed bug" in outcome.error
    assert gh.creates() == []
    assert gh.closes() == []


def test_an_original_relabelled_to_an_epic_refuses(tmp_path):
    """``epic`` declares a type too — it is simply not one a slice can be."""
    gh = FakeGh(labels=("epic",))
    outcome = _apply(tmp_path, gh)

    assert outcome.status == APPLY_STATUS_FAILED
    assert "is now typed epic" in outcome.error
    assert gh.creates() == []


def test_two_appliable_type_labels_are_an_ambiguity_and_refuse(tmp_path):
    """Which type the slices inherit is not forge's to guess."""
    gh = FakeGh(labels=("enhancement", "task"))
    outcome = _apply(tmp_path, gh)

    assert outcome.status == APPLY_STATUS_FAILED
    assert "2 appliable type labels" in outcome.error
    assert gh.creates() == []


def test_a_corrupt_criteria_mapping_refuses_rather_than_substituting(tmp_path):
    """A dropped criterion index would silently ship the scope-boundary fallback."""
    assessment = _assessment()
    assessment["slices"][1]["covers_criteria"] = ["two"]
    gh = FakeGh()
    outcome = _apply(tmp_path, gh, assessment=assessment)

    assert outcome.status == APPLY_STATUS_FAILED
    assert "non-integer acceptance-criterion index" in outcome.error
    assert gh.creates() == []


def test_a_single_slice_payload_is_not_a_split(tmp_path):
    gh = FakeGh()
    outcome = _apply(tmp_path, gh, assessment={"slices": [{"id": 1, "title": "t", "scope": "s"}]})

    assert outcome.status == APPLY_STATUS_FAILED
    assert gh.creates() == []


def test_dependency_order_places_every_dependency_first(tmp_path):
    from theforge.coordinator.decomposition_application import _validated_slices

    ordered = dependency_order(_validated_slices(_assessment()))
    positions = {item.slice_id: index for index, item in enumerate(ordered)}
    for item in ordered:
        for dep in item.depends_on:
            assert positions[dep] < positions[item.slice_id]


def test_dependency_order_refuses_a_cycle():
    from theforge.coordinator.decomposition_application import _Slice

    with pytest.raises(ApplicationRefused):
        dependency_order([_Slice(1, "a", "s", (2,)), _Slice(2, "b", "s", (1,))])


# ── Applicability ─────────────────────────────────────────────────────────────


def test_a_file_backed_story_is_never_appliable(tmp_path):
    """No issue to create siblings beside, and none to close."""
    assert not proposal_is_appliable(_assessment(), _task(tmp_path, issue=None))


def test_a_bug_original_is_not_appliable(tmp_path):
    """A bug slice needs observed/expected/diagnosis this module cannot write."""
    assert not proposal_is_appliable(_assessment(), _task(tmp_path, type_="bug"))


def test_a_tracker_backed_feature_story_with_a_split_is_appliable(tmp_path):
    assert proposal_is_appliable(_assessment(), _task(tmp_path))
    assert proposal_is_appliable(_assessment(), _task(tmp_path, type_="task"))


def test_no_assessment_is_not_appliable(tmp_path):
    assert not proposal_is_appliable(None, _task(tmp_path))
    assert not proposal_is_appliable({"slices": []}, _task(tmp_path))


# ── Interruption: what exists is recorded as it starts existing ───────────────


def test_each_created_slice_is_handed_to_the_recorder_before_the_next_create(tmp_path):
    """The callback is what makes an interruption recoverable.

    A recorder that only ran at the end would lose every issue filed before a
    kill — which is exactly how a re-entry came to file the first slice twice.
    """
    seen: list[tuple[int, int]] = []
    gh = FakeGh()

    def _record(item):
        # Ordering is the assertion: at the moment slice N is recorded, N is
        # the most recent create and no later create has been attempted.
        seen.append((item.slice_id, len(gh.creates())))

    _apply(tmp_path, gh, on_created=_record)

    assert [slice_id for slice_id, _ in seen] == [1, 2, 3, 4]
    assert [creates for _, creates in seen] == [1, 2, 3, 4]


def test_an_interruption_after_the_first_create_leaves_that_issue_recorded(tmp_path):
    """Simulates a kill: the recorder raises the way a dying process would."""

    class _Killed(Exception):
        pass

    recorded: list[dict] = []
    gh = FakeGh()

    def _record(item):
        recorded.append(item.to_dict())
        if len(recorded) == 1:
            raise _Killed("process died after the first slice was created")

    # The recorder's failure must not lose the issue or stop the application:
    # the issue exists either way, and unwinding is not available.
    _apply(tmp_path, gh, on_created=_record)

    assert [entry["issue"] for entry in recorded][0] == 2900


def test_reentry_reconciles_slices_the_tracker_already_carries(tmp_path):
    """The window the recorder cannot close: created, then killed before recording."""

    class _ReconcilingGh(FakeGh):
        def __call__(self, args, project_root):
            if args[:3] == ["gh", "issue", "list"]:
                self.calls.append(list(args))
                body = f"<!-- {DECOMPOSITION_MARKER} source=2541 slice=1 -->"
                return _ok(f'[{{"number": 2900, "body": "{body}"}}]')
            return super().__call__(args, project_root)

    gh = _ReconcilingGh()
    gh.next_number = 2910
    outcome = _apply(tmp_path, gh, reconcile_existing=True)

    assert outcome.status == APPLY_STATUS_APPLIED
    # Slice 1 was recovered from the tracker rather than created a second time.
    created_titles = [c[c.index("--title") + 1] for c in gh.creates()]
    assert "extract the portable diagnosis record" not in created_titles
    assert len(created_titles) == 3
    assert [item.issue_number for item in outcome.created] == [2900, 2910, 2911, 2912]
    # And the recovered number is what the dependants' edges point at.
    assert gh.body_for("route diagnosis").startswith("---\ndepends_on:\n  - issue-2900\n---")


def test_reconciliation_only_matches_this_proposals_own_marker(tmp_path):
    """A body carrying another issue's marker is not this split's slice."""

    class _WrongMarkerGh(FakeGh):
        def __call__(self, args, project_root):
            if args[:3] == ["gh", "issue", "list"]:
                self.calls.append(list(args))
                body = f"<!-- {DECOMPOSITION_MARKER} source=9999 slice=1 -->"
                return _ok(f'[{{"number": 1234, "body": "{body}"}}]')
            return super().__call__(args, project_root)

    gh = _WrongMarkerGh()
    outcome = _apply(tmp_path, gh, reconcile_existing=True)

    assert [item.issue_number for item in outcome.created] == [2900, 2901, 2902, 2903]


def test_a_failed_reconciliation_search_does_not_block_the_application(tmp_path):
    class _SearchBrokenGh(FakeGh):
        def __call__(self, args, project_root):
            if args[:3] == ["gh", "issue", "list"]:
                self.calls.append(list(args))
                return _fail("gh: search unavailable")
            return super().__call__(args, project_root)

    gh = _SearchBrokenGh()
    outcome = _apply(tmp_path, gh, reconcile_existing=True)

    assert outcome.status == APPLY_STATUS_APPLIED
    assert len(gh.creates()) == 4


def test_a_first_attempt_never_pays_for_the_reconciliation_search(tmp_path):
    gh = FakeGh()
    _apply(tmp_path, gh)

    assert not [c for c in gh.calls if c[:3] == ["gh", "issue", "list"]]


# ── The original can change under an open pause ───────────────────────────────


def test_an_original_closed_as_completed_during_the_pause_refuses(tmp_path):
    """Someone closed #2541 while the operator was deciding.

    The story this proposal splits is over. Creating its slices would file work
    nobody asked for, and `gh issue close` against an already-closed issue exits
    zero without touching its reason — so the split would be reported and
    audited as decomposed while the original still reads completed.
    """
    gh = FakeGh(state="CLOSED", state_reason="COMPLETED")
    outcome = _apply(tmp_path, gh)

    assert outcome.status == APPLY_STATUS_FAILED
    assert "closed while the pause was open" in outcome.error
    assert "closed as completed" in outcome.error
    assert outcome.source_issue_closed is False
    assert gh.creates() == []
    assert gh.closes() == []


def test_an_original_reopened_state_is_read_from_the_tracker_not_assumed(tmp_path):
    """A closed-as-not-planned original that this application did not close.

    Indistinguishable from its own earlier close, and treated the same way:
    finish the split rather than closing an issue that already reads decomposed.
    """
    gh = FakeGh(state="CLOSED", state_reason="NOT_PLANNED")
    outcome = _apply(tmp_path, gh)

    assert outcome.status == APPLY_STATUS_APPLIED
    assert outcome.source_issue_closed is True
    assert len(gh.creates()) == 4
    assert gh.closes() == []


def test_a_close_that_leaves_the_original_completed_is_not_reported_as_applied(tmp_path):
    """The read-back is the assertion: exit zero is not evidence of a reason."""

    class _CloseChangesNothingGh(FakeGh):
        def __call__(self, args, project_root):
            if args[:3] == ["gh", "issue", "close"]:
                self.calls.append(list(args))
                # Succeeds, changes nothing — what GitHub does when the issue
                # was closed between the state read and the close.
                self.state = "CLOSED"
                self.state_reason = "COMPLETED"
                return _ok("")
            return super().__call__(args, project_root)

    gh = _CloseChangesNothingGh()
    outcome = _apply(tmp_path, gh)

    assert outcome.status == APPLY_STATUS_FAILED
    assert outcome.source_issue_closed is False
    assert "not durably closed as decomposed" in outcome.error
    assert "completed" in outcome.error
    # The slices exist and are reported, which is what the operator acts on.
    assert [item.issue_number for item in outcome.created] == [2900, 2901, 2902, 2903]


def test_a_verification_read_that_fails_does_not_report_a_decomposed_close(tmp_path):
    class _UnverifiableGh(FakeGh):
        def __call__(self, args, project_root):
            if args[:3] == ["gh", "issue", "view"] and self.closes():
                self.calls.append(list(args))
                return _fail("gh: API unavailable")
            return super().__call__(args, project_root)

    gh = _UnverifiableGh()
    outcome = _apply(tmp_path, gh)

    assert outcome.status == APPLY_STATUS_FAILED
    assert outcome.source_issue_closed is False
    assert "not durably closed as decomposed" in outcome.error


def test_an_original_reopened_after_an_earlier_close_is_closed_again(tmp_path):
    """The record says this application closed it; the tracker says it is open.

    The live read wins: finishing the split closes it again rather than
    reporting a closure that is no longer true.
    """
    gh = FakeGh()
    outcome = _apply(
        tmp_path,
        gh,
        prior_created=[
            {"slice_id": 1, "title": "extract", "issue": 2800, "depends_on_issues": []},
        ],
        source_issue_already_closed=True,
    )

    assert outcome.status == APPLY_STATUS_APPLIED
    assert outcome.source_issue_closed is True
    assert len(gh.closes()) == 1
    assert gh.state == "CLOSED"
    assert gh.state_reason == "NOT_PLANNED"
