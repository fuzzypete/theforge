"""Proof of concept for the #2598 spike: protected base, parallel, merge-pr.

Two tests, and the pairing is the finding.

:func:`test_protected_base_parallel_merge_pr_poc` is the configuration the story
mandates — three independent stories, ``--parallel 2``, ``on_approve: merge-pr``,
a base branch whose remote refuses direct non-merge commits. It establishes that
project memory reaches the repository without a direct base commit, that a
completed story's artifacts do not dirty the shared checkout while a sibling is
still running, and that a fresh clone ends up with one run record per completed
story and a positive landing assertion only where a successful landing was
observed.

What it *cannot* establish is that a refusal was prevented. Under
``on_approve: merge-pr`` the runner sets ``config_lands_in_project_root`` false
and no story's entry evaluates the landing precondition at all, so there is no
refusal to prevent in that configuration — a point the story asks the design
document to state explicitly, because "no refusal happened" and "no refusal
could have happened" are different findings.

:func:`test_a_reachable_contamination_is_prevented_by_the_publication_seam`
supplies the missing half. Against a base branch that refuses forge's memory
commit the contamination is genuinely reachable: the paired counterfactual
shows story 2 entering on top of story 1's uncommitted run record when the
transport is forced back to the direct path. With the seam in place it does not.

Both halves observe the checkout's raw dirty status rather than the landing
precondition. Since #2775 that predicate excuses forge's own artifact trees —
a sprint's bookkeeping is not grounds for refusing that sprint's stories — so a
refusal is no longer the observable the contamination produces, and asserting on
one would have pinned the bug rather than the seam.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from sprint_test_helpers import run_sprint_ctx

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    ForgeConfig,
    RetryPolicy,
    ValidationConfig,
    WorkspaceConfig,
)
from theforge.coordinator.landing_evidence import (
    landing_evidence_dir,
    read_landing_attempts,
)
from theforge.coordinator.state import (
    CoordinatorResult,
    CoordinatorState,
    Phase,
    ReviewCycleMetadata,
)
from theforge.coordinator.workspace import (
    landing_precondition_error,
    project_root_dirty_status,
)
from theforge.sprint.memory_publication import MEMORY_BRANCH, MEMORY_PUBLISH_CLEAN

BASE = "main"

_FORGE_GITIGNORE = """\
.forge/**
!.forge/audits/
!.forge/audits/runs/
!.forge/audits/runs/**
!.forge/audits/landing/
!.forge/audits/landing/**
!.forge/knowledge/
!.forge/knowledge/summaries/
!.forge/knowledge/summaries/**
"""

# The reported policy: the base branch advances by merge only. Forge's own story
# landings satisfy it; a direct memory commit does not.
_PRE_RECEIVE = f"""\
#!/bin/sh
while read old new ref; do
  [ "$ref" = "refs/heads/{BASE}" ] || continue
  case "$old" in
    0000000000000000000000000000000000000000) continue ;;
  esac
  for sha in $(git rev-list --first-parent "$old..$new"); do
    if [ "$(git rev-list --parents -n 1 "$sha" | wc -w)" -lt 3 ]; then
      echo "COMMIT BLOCKED: Non-merge commit $sha on {BASE}." >&2
      exit 1
    fi
  done
done
exit 0
"""

# A ``gh`` that answers from a JSON file the test owns. Enough of the surface
# for the merge-pr landing path and the reconciliation observer: list open and
# merged PRs for a head branch, view a PR's state, and accept a create.
_FAKE_GH = """\
#!/usr/bin/env python3
import json, os, sys

state = json.load(open(os.environ["FAKE_GH_STATE"]))
args = sys.argv[1:]


def flag(name, default=None):
    return args[args.index(name) + 1] if name in args else default


if args[:2] == ["pr", "list"]:
    head, want = flag("--head"), (flag("--state") or "open").lower()
    print(json.dumps([p for p in state["prs"] if p["head"] == head and p["state"] == want]))
    sys.exit(0)
if args[:2] == ["pr", "view"]:
    url = args[2]
    for pr in state["prs"]:
        if pr["url"] == url:
            print("MERGED" if pr["state"] == "merged" else "OPEN")
            sys.exit(0)
    sys.exit(1)
if args[:2] == ["pr", "create"]:
    print("https://example.test/memory/1")
    sys.exit(0)
sys.exit(1)
"""


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check
    )
    return proc.stdout.strip()


@pytest.fixture()
def protected_project(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch", BASE)

    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "--initial-branch", BASE)
    _git(root, "config", "user.email", "forge@example.com")
    _git(root, "config", "user.name", "Forge Test")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    (root / ".gitignore").write_text(_FORGE_GITIGNORE, encoding="utf-8")
    _git(root, "add", "README.md", ".gitignore")
    _git(root, "commit", "-m", "seed")
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "push", "-u", "origin", BASE)

    return root


def _protect_base(tmp_path: Path) -> None:
    """Install the merge-only policy on the remote.

    Installed by each test *after* its manifest is seeded, because seeding is
    the operator's own commit and the policy is about what forge does next.
    """
    hook = tmp_path / "origin.git" / "hooks" / "pre-receive"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(_PRE_RECEIVE, encoding="utf-8")
    hook.chmod(0o755)


@pytest.fixture()
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scriptable ``gh`` on PATH. Returns the JSON state file to mutate."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(_FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    state = tmp_path / "gh-state.json"
    state.write_text(json.dumps({"prs": []}), encoding="utf-8")
    monkeypatch.setenv("FAKE_GH_STATE", str(state))
    monkeypatch.setenv("PATH", f"{bindir}:{__import__('os').environ['PATH']}")
    return state


def _config(root: Path, on_approve: str, max_parallel_hint: int = 2) -> ForgeConfig:
    return ForgeConfig(
        project="poc",
        project_root=root,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
            base_branch=BASE,
            on_approve=on_approve,
            # 0 makes the queued-PR poll take one look and report a timeout,
            # which is the sprint-exit state an async landing mode is *supposed*
            # to reach when the PR has not merged yet.
            merge_wait_timeout_seconds=0,
        ),
        validation=ValidationConfig(gate_command="true"),
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
    )


def _manifest(root: Path, slugs: list[str], max_parallel: int) -> Path:
    for slug in slugs:
        (root / f"{slug}.md").write_text(
            f"---\nname: {slug}\nslug: {slug}\n---\n# {slug}\nDo the thing.",
            encoding="utf-8",
        )
    path = root / "sprint.yaml"
    path.write_text(
        yaml.dump(
            {
                "name": "Protected Base Sprint",
                "budget_usd": 30.0,
                "stories": [f"{slug}.md" for slug in slugs],
                "max_parallel": max_parallel,
            }
        ),
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "sprint stories")
    _git(root, "push", "origin", BASE)
    return path


def _result(slug: str, reviewed: str, gated: str) -> CoordinatorResult:
    state = CoordinatorState()
    state.run_id = f"run-{slug}"
    state.preflight_verdict = "PROCEED"
    preflight = MagicMock()
    preflight.cost_usd = 1.0
    state.preflight_result = preflight
    state.last_gate_commit = gated
    state.review_cycle_metadata.append(
        ReviewCycleMetadata(pool_models=["r"], successful=["r"], failed=[], synthesized=False)
    )
    state.review_cycle_metadata[-1].reviewed_commit = reviewed
    result = CoordinatorResult(
        success=True,
        phase=Phase.DONE,
        state=state,
        message="Done.",
        merge={"action": "merge-pr", "pending": True},
    )
    # What a worker returns under merge-pr: approved, landing deferred to the
    # scheduler's sole merge site.
    result.landing_status = "pending_integration"
    return result


def _write_run_artifacts(root: Path, slug: str) -> None:
    """The knowledge summary a finished story leaves in the shared checkout.

    Deliberately *not* the run record: the sprint writes that itself, through the
    real writer, and a stub standing in for it would be the record these tests
    then read back — hiding whether the genuine one was rewritten by its landing
    outcome. Summary generation is LLM-backed and does not run here, so this
    stands in for it and is what makes the shared checkout dirty at a time the
    test controls.
    """
    summaries = root / ".forge" / "knowledge" / "summaries"
    summaries.mkdir(parents=True, exist_ok=True)
    (summaries / f"run-{slug}.yaml").write_text(f"run_id: run-{slug}\n", encoding="utf-8")


def _push_story_branch(root: Path, tmp_path: Path, slug: str) -> str:
    """Create the story's branch on the remote and return its head SHA."""
    work = tmp_path / f"push-{slug}"
    _git(tmp_path, "clone", "--quiet", str(tmp_path / "origin.git"), str(work))
    _git(work, "config", "user.email", "forge@example.com")
    _git(work, "config", "user.name", "Forge Test")
    _git(work, "checkout", "-b", f"forge/{slug}")
    (work / f"{slug}-work.txt").write_text(f"{slug}\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", f"feat({slug}): work")
    _git(work, "push", "origin", f"forge/{slug}")
    return _git(work, "rev-parse", "HEAD")


def _merge_story_branch(tmp_path: Path, slug: str) -> str:
    """Merge a story branch into the base on the remote; return the merge SHA."""
    work = tmp_path / f"merge-{slug}"
    _git(tmp_path, "clone", "--quiet", str(tmp_path / "origin.git"), str(work))
    _git(work, "config", "user.email", "forge@example.com")
    _git(work, "config", "user.name", "Forge Test")
    _git(work, "fetch", "origin", f"forge/{slug}")
    _git(work, "merge", "--no-ff", "-m", f"Merge PR for {slug}", f"origin/forge/{slug}")
    _git(work, "push", "origin", BASE)
    return _git(work, "rev-parse", "HEAD")


def _set_prs(state_file: Path, prs: list[dict]) -> None:
    state_file.write_text(json.dumps({"prs": prs}), encoding="utf-8")


def test_protected_base_parallel_merge_pr_poc(
    protected_project: Path, fake_gh: Path, tmp_path: Path
) -> None:
    """Three stories, ``--parallel 2``, ``merge-pr``, base refuses direct commits.

    Story outcomes are chosen to cover the three shapes the matrix distinguishes
    for an asynchronous mode: landed and observed in-sprint, queued at sprint
    exit and observed later, and a landing that failed outright.
    """
    config = _config(protected_project, "merge-pr")
    manifest = _manifest(protected_project, ["story-a", "story-b", "story-c"], max_parallel=2)
    _protect_base(tmp_path)
    base_at_start = _git(protected_project, "rev-parse", f"origin/{BASE}")

    heads = {
        slug: _push_story_branch(protected_project, tmp_path, slug)
        for slug in ("story-a", "story-b", "story-c")
    }
    # story-a's PR is merged by the time forge lands it; story-b's is still open
    # and will merge after the sprint exits; story-c never gets one.
    merge_commit_a = _merge_story_branch(tmp_path, "story-a")
    _set_prs(
        fake_gh,
        [
            # A *stale* pull request on story-a's branch: same branch name, an
            # older merge, carrying commits that have nothing to do with this
            # run. It is listed first and merged earlier, so a lookup keyed on
            # the branch alone would have to reject it on the evidence rather
            # than on ordering.
            {
                "head": "forge/story-a",
                "state": "merged",
                "number": 0,
                "url": "https://example.test/pr/0",
                "mergeCommit": {"oid": "0" * 40},
                "mergedAt": "2026-01-01T00:00:00Z",
                "headRefOid": "9" * 40,
                "commits": [{"oid": "9" * 40}],
            },
            {
                "head": "forge/story-a",
                "state": "merged",
                "number": 1,
                "url": "https://example.test/pr/1",
                "mergeCommit": {"oid": merge_commit_a},
                "mergedAt": "2026-08-25T00:00:00Z",
                "headRefOid": heads["story-a"],
                "commits": [{"oid": heads["story-a"]}],
            },
            {
                "head": "forge/story-b",
                "state": "open",
                "number": 2,
                "url": "https://example.test/pr/2",
                "mergeCommit": None,
                "mergedAt": None,
                "headRefOid": heads["story-b"],
                "commits": [{"oid": heads["story-b"]}],
            },
        ],
    )

    # The story's scenario, sequenced rather than raced: story A finishes only
    # once story B is running, and story B stays running until story C has been
    # admitted. So C is admitted with A's canonical artifacts already written
    # and a sibling still in flight — the exact condition the acceptance
    # criterion names — and it is admitted there every run, not when the
    # scheduler happens to interleave that way.
    b_entered = threading.Event()
    c_entered = threading.Event()
    observed_dirt: dict[str, str | None] = {}
    in_flight: set[str] = set()
    active_at_c: list[set[str]] = []

    def fake_run_task(_config, task, *args, **kwargs):
        slug = task.slug
        in_flight.add(slug)
        # Has a *sibling* left artifacts in the shared checkout right now? Asked
        # of the raw status rather than of ``landing_precondition_error``: since
        # #2775 that predicate excuses forge's own artifact trees, so it can no
        # longer witness the contamination this test is about. The question the
        # story asks is about the checkout's state, not about whether forge
        # refuses over it.
        observed_dirt[slug] = project_root_dirty_status(protected_project)
        if slug == "story-a":
            b_entered.wait(timeout=30)
        elif slug == "story-b":
            b_entered.set()
            c_entered.wait(timeout=30)
        elif slug == "story-c":
            active_at_c.append(set(in_flight))
            c_entered.set()
        _write_run_artifacts(protected_project, slug)
        in_flight.discard(slug)
        return _result(slug, heads[slug], heads[slug])

    def fake_land_story(_config, task, branch, *_args, **_kwargs):
        if task.slug == "story-a":
            return (
                {
                    "action": "merge-pr",
                    "merged": True,
                    "auto_merge_queued": True,
                    "pr_url": "https://example.test/pr/1",
                    "landing_path": "fresh-merge",
                },
                "landed",
            )
        if task.slug == "story-b":
            return (
                {
                    "action": "merge-pr",
                    "merged": False,
                    "merge_queued": True,
                    "pr_url": "https://example.test/pr/2",
                    "landing_path": "fresh-merge",
                },
                "pending_integration",
            )
        return (
            {
                "action": "merge-pr",
                "merged": False,
                "error": "required checks failed",
                "landing_path": "fresh-merge",
            },
            "failed",
        )

    with (
        patch("theforge.sprint.runner.run_task", side_effect=fake_run_task),
        patch("theforge.coordinator.completion.land_story", side_effect=fake_land_story),
    ):
        run_sprint_ctx(config, manifest)

    # ── The shared checkout ──────────────────────────────────────────────
    assert active_at_c and "story-b" in active_at_c[0], (
        "story-c was not admitted while a sibling was still running"
    )
    contaminated = {slug: dirt for slug, dirt in observed_dirt.items() if dirt}
    assert contaminated == {}, f"stories admitted on top of forge's own artifacts: {contaminated}"

    # ── The protected branch ─────────────────────────────────────────────
    # Only the story merges advanced it; forge committed nothing directly.
    subjects = _git(
        protected_project,
        "log",
        "--first-parent",
        "--format=%s",
        f"{base_at_start}..origin/{BASE}",
    ).splitlines()
    assert subjects == ["Merge PR for story-a"]

    # ── Evidence at sprint exit ──────────────────────────────────────────
    landing_dir = landing_evidence_dir(protected_project)
    memory_branch_files = _git(
        protected_project, "ls-tree", "-r", "--name-only", f"origin/{MEMORY_BRANCH}"
    ).splitlines()
    published = set(memory_branch_files) | {
        f".forge/audits/landing/{p.name}" for p in landing_dir.glob("*.json")
    }
    assert ".forge/audits/landing/run-story-a.landed.json" in published
    assert ".forge/audits/landing/run-story-b.landed.json" not in published
    assert ".forge/audits/landing/run-story-c.landed.json" not in published

    # story-b is *unresolved*, not failed: forge stopped waiting for its PR.
    # Queued when the landing step ran, then timeout when forge stopped waiting
    # at wrap-up. Both are recorded; neither asserts a landing.
    assert [a["outcome"] for a in read_landing_attempts(protected_project, "run-story-b")] == [
        "queued",
        "timeout",
    ]
    assert [a["outcome"] for a in read_landing_attempts(protected_project, "run-story-c")] == [
        "failed"
    ]

    # ── The PR resolves after the sprint ─────────────────────────────────
    merge_commit_b = _merge_story_branch(tmp_path, "story-b")
    prs = json.loads(fake_gh.read_text(encoding="utf-8"))["prs"]
    for pr in prs:
        if pr["head"] == "forge/story-b":
            pr.update(
                state="merged",
                mergeCommit={"oid": merge_commit_b},
                mergedAt="2026-08-26T00:00:00Z",
                headRefOid=heads["story-b"],
                commits=[{"oid": heads["story-b"]}],
            )
    _set_prs(fake_gh, prs)

    from theforge.sprint.audit_publish import publish_story_run_artifacts_for_config
    from theforge.sprint.landing_observation import reconcile_landing_evidence

    published_now = reconcile_landing_evidence(config)
    assert [a["run_id"] for a in published_now] == ["run-story-b"]
    publish_story_run_artifacts_for_config(config, lands_locally=False)

    # ── A fresh clone ────────────────────────────────────────────────────
    clone = tmp_path / "fresh"
    _git(
        tmp_path,
        "clone",
        "--quiet",
        "--branch",
        MEMORY_BRANCH,
        str(tmp_path / "origin.git"),
        str(clone),
    )
    runs = clone / ".forge" / "audits" / "runs"
    evidence = clone / ".forge" / "audits" / "landing"

    # One immutable run record per completed story.
    assert sorted(p.name for p in runs.glob("*.json")) == [
        "run-story-a.json",
        "run-story-b.json",
        "run-story-c.json",
    ]
    for slug in ("story-a", "story-b", "story-c"):
        record = json.loads((runs / f"run-{slug}.json").read_text(encoding="utf-8"))
        assert record["run_id"] == f"run-{slug}"
        assert record["task"]["slug"] == slug
    # Neither the queued PR forge stopped waiting for nor the landing that
    # failed outright rewrote its record. Both are left as the run left them —
    # the landing was owed and unresolved when the story finished, and that is
    # still what the record says. Asserted as the value, not merely as "not
    # landed": a record stamped `failed` would also satisfy "not landed" while
    # being exactly the negative claim the model forbids.
    for slug in ("story-b", "story-c"):
        record = json.loads((runs / f"run-{slug}.json").read_text(encoding="utf-8"))
        assert record.get("landing_status") == "pending_integration", (
            f"{slug}'s record was rewritten by its landing outcome"
        )
        assert (record.get("merge") or {}).get("merged") is not True
        assert (record.get("landing_event") or {}).get("landed") is not True
    # story-b's landing was observed *after* the sprint exited; the later
    # observation added an artifact beside the record rather than touching it.
    assert (evidence / "run-story-b.landed.json").exists()

    # Positive evidence only where a successful landing was observed.
    assert sorted(p.name for p in evidence.glob("*.landed.json")) == [
        "run-story-a.landed.json",
        "run-story-b.landed.json",
    ]
    for slug, merge_commit, pr_url, carrier_ref, observer in (
        ("story-a", merge_commit_a, "https://example.test/pr/1", "#1", "sprint.integration"),
        ("story-b", merge_commit_b, "https://example.test/pr/2", "#2", "forge.reconcile"),
    ):
        assertion = json.loads((evidence / f"run-{slug}.landed.json").read_text(encoding="utf-8"))
        assert assertion["run_id"] == f"run-{slug}"
        assert assertion["reviewed_commit"] == heads[slug]
        assert assertion["gated_commit"] == heads[slug]
        assert assertion["target_branch"] == BASE
        assert assertion["landed_commit"] == merge_commit
        assert assertion["carrier_kind"] == "pull_request"
        # The verified pull request, not the stale one that reused the branch:
        # #1 carried this run's reviewed commit, #0 did not.
        assert assertion["carrier_ref"] == carrier_ref
        assert assertion["pr_url"] == pr_url
        assert assertion["landing_mode"] == "merge-pr"
        assert assertion["observer"] == observer

    # story-c: an outcome without a verified landing asserts nothing.
    assert not (evidence / "run-story-c.landed.json").exists()
    attempts = sorted(p.name for p in evidence.glob("run-story-c.attempt-*.json"))
    assert attempts, "the failed landing left no attempt record"


def test_every_admission_is_preceded_by_a_drain_of_sibling_memory(
    protected_project: Path, fake_gh: Path, tmp_path: Path
) -> None:
    """The mid-pass window, closed and asserted as a position rather than a race.

    The pass-level publish runs once per scheduling pass, so a sibling finishing
    *during* a pass leaves its record in the shared checkout for the next story
    admitted from the same ``ready`` snapshot. This drives that window directly:
    the traced drain writes a sibling's artifacts immediately before delegating
    to the real one, which is the worst case the window can produce, and the
    story admitted next must still enter a clean checkout.
    """
    config = _config(protected_project, "merge-pr")
    manifest = _manifest(protected_project, ["story-a", "story-b"], max_parallel=1)
    _protect_base(tmp_path)
    _set_prs(fake_gh, [])

    from theforge.sprint import runner as _runner

    real_drain = _runner.drain_project_memory_before_dispatch
    events: list[str] = []
    observed_dirt: dict[str, str | None] = {}
    sibling = ["run-sibling-1", "run-sibling-2"]

    def traced_drain(state):
        # A sibling finishing in the window this drain exists to close.
        if sibling:
            _write_run_artifacts(protected_project, sibling.pop(0))
        events.append("drain")
        return real_drain(state)

    def fake_run_task(_config, task, *args, **kwargs):
        events.append(f"dispatch:{task.slug}")
        # The raw status, not the landing precondition: since #2775 that
        # predicate excuses forge's own artifact trees, which is exactly the
        # dirt the drain exists to clear — so it can no longer witness whether
        # the drain ran.
        observed_dirt[task.slug] = project_root_dirty_status(protected_project)
        _write_run_artifacts(protected_project, task.slug)
        return _result(task.slug, "a" * 40, "a" * 40)

    with (
        patch("theforge.sprint.runner.run_task", side_effect=fake_run_task),
        patch("theforge.sprint.runner.drain_project_memory_before_dispatch", traced_drain),
        patch(
            "theforge.coordinator.completion.land_story",
            side_effect=lambda *a, **k: ({"action": "merge-pr", "merged": False}, "failed"),
        ),
        # Only the drain half of the transport is under test here.
        # ``stage_and_publish_project_memory`` states the split as its contract:
        # "Staging is what unblocks the next story; the publish is what gets the
        # corpus off this machine." This asserts a position in the schedule and a
        # clean checkout at admission — both the drain's doing — so the remote
        # half is stubbed rather than exercised. It costs ~80 real git
        # subprocesses per run, which is what put this test past the
        # five-second per-test convention, and leaving it out makes the claim stricter: the
        # drain alone now has to clear the checkout, with no publish able to
        # clear it instead.
        patch(
            "theforge.sprint.memory_publication.publish_staged_project_memory",
            return_value=(MEMORY_PUBLISH_CLEAN, None),
        ),
    ):
        run_sprint_ctx(config, manifest)

    dispatches = [i for i, event in enumerate(events) if event.startswith("dispatch:")]
    assert dispatches, "no story was dispatched"
    for index in dispatches:
        assert events[index - 1] == "drain", (
            f"{events[index]} was admitted without a preceding drain: {events}"
        )
    dirty = {slug: dirt for slug, dirt in observed_dirt.items() if dirt}
    assert dirty == {}, f"stories admitted into a dirty checkout: {dirty}"


def _install_base_refusing_pre_commit_hook(project: Path) -> None:
    """The protected-base policy, applied where a local-merge run meets it.

    A protected branch refuses forge's memory commit at the commit itself, which
    is what leaves the first story's artifacts standing in the shared checkout.
    """
    hook = project / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        "#!/bin/sh\n"
        "if git diff --cached --name-only | grep -q '^\\.forge/'; then\n"
        "  echo '\u26d4 COMMIT BLOCKED: Non-doc changes on main.' >&2\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)


def _preconditions_seen_by_each_story(
    project: Path, tmp_path: Path, *, seam_in_place: bool
) -> dict[str, dict[str, str | None]]:
    """Run one ``merge`` sprint and return what each story saw at its entry.

    Records two things per story: the shared checkout's raw dirty status, and
    the landing precondition derived from it. They are no longer the same claim
    (#2775) — a checkout carrying nothing but a sibling's run artifacts is dirty
    and is nonetheless admissible — so the counterfactual has to observe the
    state the seam acts on rather than a refusal the entry check no longer
    raises.

    Split out so each half of the counterfactual is its own test running a
    single sprint. Asserting both halves in one test meant running two sprints
    over real git back to back, which put it past the five-second per-test
    convention. The halves are independent claims, and neither needs the
    other to have run first — each starts from its own fresh ``protected_project``
    checkout, which is also why the reset between the two runs is gone.
    """
    config = _config(project, "merge")
    manifest = _manifest(project, ["story-a", "story-b"], max_parallel=1)
    _protect_base(tmp_path)
    _install_base_refusing_pre_commit_hook(project)

    observed: dict[str, dict[str, str | None]] = {}

    def fake_run_task(_config, task, *args, **kwargs):
        observed[task.slug] = {
            "dirt": project_root_dirty_status(project),
            "refusal": landing_precondition_error(config, lands_in_project_root=True),
        }
        _write_run_artifacts(project, task.slug)
        state = CoordinatorState()
        state.run_id = f"run-{task.slug}"
        state.preflight_verdict = "PROCEED"
        preflight = MagicMock()
        preflight.cost_usd = 1.0
        state.preflight_result = preflight
        return CoordinatorResult(
            success=True,
            phase=Phase.DONE,
            state=state,
            message="Done.",
            merge={"merged": True},
        )

    patches = [patch("theforge.sprint.runner.run_task", side_effect=fake_run_task)]
    if not seam_in_place:
        # Force the pre-#2598 behaviour: direct commit, no memory-branch
        # fallback. This is the counterfactual, not a supported mode.
        patches.append(patch("theforge.sprint.audit_publish._REFUSED_BY_POLICY", frozenset()))
    with contextlib.ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        # The counterfactual ends the way the report did: the terminal publish
        # raises because the base branch refuses forge's commit. That is the
        # bug, not a test failure — what is under test is what the *second*
        # story saw before it got there.
        with contextlib.suppress(RuntimeError):
            run_sprint_ctx(config, manifest)
    return observed


def test_the_contamination_is_reachable_when_the_transport_is_forced_to_direct_commit(
    protected_project: Path, tmp_path: Path
) -> None:
    """The counterfactual the mandated configuration cannot supply.

    A base branch that refuses forge's memory commit leaves the first story's
    artifacts standing in the shared checkout — precisely the reported failure,
    one step downstream. Without the fallback, story-b enters on top of
    story-a's uncommitted run record, which the refused commit could not clear.

    That contamination used to *also* refuse story-b at its entry. It no longer
    does (#2775): a sprint's own bookkeeping is not grounds for refusing that
    sprint's stories, so the second assertion here pins the entry check staying
    silent about exactly the dirt this counterfactual manufactures. The
    contamination is still real and still worth preventing — later story
    worktrees are cut from this checkout — which is what the paired test below
    establishes the seam does.
    """
    observed = _preconditions_seen_by_each_story(protected_project, tmp_path, seam_in_place=False)
    assert ".forge/audits/runs" in (observed["story-b"]["dirt"] or "")
    assert observed["story-b"]["refusal"] is None, (
        f"forge's own pending artifacts refused a sibling story: {observed['story-b']['refusal']}"
    )


def test_a_reachable_contamination_is_prevented_by_the_publication_seam(
    protected_project: Path, tmp_path: Path
) -> None:
    """The other half: with the seam in place the same run leaves a clean root.

    Paired with the counterfactual above — that one establishes the
    contamination is reachable at all, this one that the publication seam is
    what prevents it. Neither claim means much alone.
    """
    observed = _preconditions_seen_by_each_story(protected_project, tmp_path, seam_in_place=True)
    assert observed["story-b"]["dirt"] == "", (
        f"the seam did not clear the shared checkout: {observed['story-b']['dirt']}"
    )
    assert observed["story-b"]["refusal"] is None
