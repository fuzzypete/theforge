"""Tests for evaluating a report body against the target repository's gate.

The gate that rules on a filed issue is the revision resident in the target
repository, not the release installed in the observing checkout. These tests
drive the real download-and-execute path with ``gh`` stubbed, including the
observer/target version skew the story's own example describes.
"""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

import pytest

from theforge.reporting import target_gate as tg

GATE_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "theforge" / "shape_check"

BUG_BODY = """\
## Observed

Sprint resume reported a story merged when no commit landed.

## Expected

Sprint resume should not report a story merged until a landed commit proves it.

## Diagnosis

- **Observed symptom:** resume false-skips zero-delta APPROVE stories.
- **Evidence:** run `f5aa21cf2d8d`.
- **Confirmed cause:** `_is_already_merged` requires a commit ahead.
- **Affected code path:** `sprint.runner._is_already_merged`.
- **Fix-success criterion:** resume identifies a zero-delta APPROVE as merged.
"""


def _proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _local_gate_sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(GATE_SOURCE_ROOT.glob("*.py"))}


def _diverged_gate(check_source: str) -> dict[str, str]:
    """The real gate package with a divergent ``check.py`` spliced in.

    ``__init__.py`` is emptied alongside it: the package re-exports names from
    ``check``, and a target revision that diverges there diverges in both.
    """
    sources = _local_gate_sources()
    sources["check.py"] = check_source
    sources["__init__.py"] = ""
    return sources


def _make_runner(
    sources: dict[str, str],
    *,
    default_branch: str = "main",
    sha: str = "1a2b3c4d5e6f77889900",
    calls: list[list[str]] | None = None,
):
    """Stub ``gh`` serving ``sources`` as the target repo's gate package."""

    def runner(command: list[str]):
        if calls is not None:
            calls.append(command)
        if command[1:3] == ["repo", "view"]:
            return _proc(stdout=f"{default_branch}\n")
        endpoint = command[2] if command[1] == "api" and len(command) > 2 else ""
        if endpoint.startswith("repos/") and "/commits/" in endpoint:
            return _proc(stdout=f"{sha}\n")
        if f"contents/{tg.GATE_PACKAGE_PATH}?" in endpoint:
            return _proc(stdout="".join(f"{name}\n" for name in sources))
        prefix = f"contents/{tg.GATE_PACKAGE_PATH}/"
        if prefix in endpoint:
            name = endpoint.split(prefix, 1)[1].split("?", 1)[0]
            if name not in sources:
                return _proc(returncode=1, stderr="gh: Not Found (HTTP 404)")
            encoded = base64.b64encode(sources[name].encode("utf-8")).decode("ascii")
            return _proc(stdout=encoded + "\n")
        return _proc(returncode=1, stderr=f"unexpected gh call: {command!r}")

    return runner


def test_verdict_comes_from_the_target_repos_pinned_revision():
    calls: list[list[str]] = []
    runner = _make_runner(_local_gate_sources(), calls=calls)

    verdict = tg.evaluate_target_gate(
        repo="fuzzypete/theforge",
        title="resume false-skips zero-delta APPROVE stories",
        body=BUG_BODY,
        labels=["bug"],
        runner=runner,
    )

    assert verdict.verdict == "runnable"
    assert verdict.repo == "fuzzypete/theforge"
    assert verdict.ref == "main"
    assert verdict.sha == "1a2b3c4d5e6f77889900"
    assert "fuzzypete/theforge@1a2b3c4d5e6f" in verdict.source
    # Every download is pinned to the one resolved commit, so the gate that
    # ruled is a single nameable revision.
    fetches = [c for c in calls if c[1] == "api" and "contents/" in c[2]]
    assert fetches and all("ref=1a2b3c4d5e6f77889900" in c[2] for c in fetches)


def test_observer_target_skew_uses_the_targets_gate_not_the_local_one():
    """A target gate that rules differently from this checkout's must win."""
    # A target revision whose gate refuses everything — a rule this checkout
    # does not have. If the local gate were consulted, the body would pass.
    sources = _diverged_gate(
        "from theforge.shape_check.types import Reason, Severity, Shape, "
        "ShapeResult, ShapeVerdict, SuggestedAction\n"
        "\n"
        "\n"
        "def check(title, body, labels, **kwargs):\n"
        "    return ShapeResult(\n"
        "        shape=Shape.NEEDS_GROOMING,\n"
        "        reasons=(Reason(code='target_only_rule', severity=Severity.BLOCKING,\n"
        "                        detail='a rule only the target repository has'),),\n"
        "        suggested_action=SuggestedAction.CLARIFY,\n"
        "        verdict=ShapeVerdict.NEEDS_OPERATOR_ACTION,\n"
        "    )\n"
    )

    verdict = tg.evaluate_target_gate(
        repo="fuzzypete/theforge",
        title="resume false-skips zero-delta APPROVE stories",
        body=BUG_BODY,
        labels=["bug"],
        runner=_make_runner(sources),
    )

    assert verdict.verdict == "needs_operator_action"
    assert [r.code for r in verdict.reasons] == ["target_only_rule"]
    assert verdict.reasons[0].severity == "blocking"


def test_target_without_a_shape_gate_cannot_be_placed():
    with pytest.raises(tg.TargetGateError) as excinfo:
        tg.evaluate_target_gate(
            repo="acme/not-forge",
            title="t",
            body=BUG_BODY,
            labels=["bug"],
            runner=_make_runner({}),
        )

    assert "no src/theforge/shape_check" in str(excinfo.value)


def test_missing_check_entry_point_cannot_be_placed():
    with pytest.raises(tg.TargetGateError) as excinfo:
        tg.evaluate_target_gate(
            repo="fuzzypete/theforge",
            title="t",
            body=BUG_BODY,
            labels=["bug"],
            runner=_make_runner({"types.py": "x = 1\n"}),
        )

    assert "no check.py" in str(excinfo.value)


def test_target_gate_that_raises_cannot_be_placed():
    sources = _diverged_gate(
        "def check(title, body, labels, **kwargs):\n    raise RuntimeError('boom')\n"
    )

    with pytest.raises(tg.TargetGateError) as excinfo:
        tg.evaluate_target_gate(
            repo="fuzzypete/theforge",
            title="t",
            body=BUG_BODY,
            labels=["bug"],
            runner=_make_runner(sources),
        )

    assert "could not be executed" in str(excinfo.value)
    assert "boom" in str(excinfo.value)


def test_target_gate_without_a_verdict_cannot_be_placed():
    sources = _diverged_gate(
        "class _R:\n"
        "    verdict = None\n"
        "    shape = None\n"
        "    reasons = ()\n"
        "\n"
        "\n"
        "def check(title, body, labels, **kwargs):\n"
        "    return _R()\n"
    )

    with pytest.raises(tg.TargetGateError) as excinfo:
        tg.evaluate_target_gate(
            repo="fuzzypete/theforge",
            title="t",
            body=BUG_BODY,
            labels=["bug"],
            runner=_make_runner(sources),
        )

    assert "produced no verdict" in str(excinfo.value)


def test_unresolvable_default_branch_cannot_be_placed():
    def runner(command: list[str]):
        return _proc(returncode=1, stderr="gh: Could not resolve to a Repository")

    with pytest.raises(tg.TargetGateError) as excinfo:
        tg.evaluate_target_gate(
            repo="fuzzypete/nope", title="t", body=BUG_BODY, labels=["bug"], runner=runner
        )

    assert "default branch" in str(excinfo.value)


def test_missing_gh_binary_cannot_be_placed():
    def runner(command: list[str]):
        raise FileNotFoundError("gh")

    with pytest.raises(tg.TargetGateError) as excinfo:
        tg.evaluate_target_gate(
            repo="fuzzypete/theforge", title="t", body=BUG_BODY, labels=["bug"], runner=runner
        )

    assert "gh is not installed" in str(excinfo.value)


def test_gh_timeout_cannot_be_placed():
    def runner(command: list[str]):
        raise subprocess.TimeoutExpired(cmd=command, timeout=60)

    with pytest.raises(tg.TargetGateError) as excinfo:
        tg.evaluate_target_gate(
            repo="fuzzypete/theforge", title="t", body=BUG_BODY, labels=["bug"], runner=runner
        )

    assert "timed out" in str(excinfo.value)


def test_path_traversing_file_name_is_refused():
    with pytest.raises(tg.TargetGateError) as excinfo:
        tg.evaluate_target_gate(
            repo="fuzzypete/theforge",
            title="t",
            body=BUG_BODY,
            labels=["bug"],
            runner=_make_runner({"../../evil.py": "x = 1\n", "check.py": "x = 1\n"}),
        )

    assert "unusable file names" in str(excinfo.value)
