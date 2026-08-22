"""Evaluate a report body against the *target repository's* shape gate.

The gate that decides whether an issue is well-shaped is the one resident in
the repository the issue is filed into — ``.github/workflows/shape-check.yml``
runs ``python -m theforge.shape_check`` from that repository's own checkout. A
verdict produced by the observing project's installed release is therefore not
the target's verdict: the observing project routinely runs an older release
than the repo it reports into, which is the whole reason the report has to
carry its runtime identity in the first place.

So this module resolves the target's gate revision and runs *that* code:

1. resolve the target's default branch and pin its head commit sha;
2. download ``src/theforge/shape_check/*.py`` at that sha via ``gh api``;
3. run the downloaded package in an isolated subprocess and read back the
   verdict it produces.

Every failure along that path raises :class:`TargetGateError`. There is no
fallback to the local gate — a body whose target-owned gate state cannot be
established is exactly the unknown state the caller must refuse to file in.

The downloaded package is stdlib-only by construction (the target's own
workflow installs it without runtime dependencies for that reason) and is
executed with ``python -I`` against a temporary package root, so it cannot see
the observing project's environment, site customisations, or installed
``theforge``.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

#: Path of the gate package inside the target repository.
GATE_PACKAGE_PATH = "src/theforge/shape_check"

GH_TIMEOUT_SECONDS = 60
GATE_RUN_TIMEOUT_SECONDS = 60

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]

_RUNNER_SOURCE = """\
import json
import sys

sys.path.insert(0, sys.argv[2])

from theforge.shape_check.check import check

payload = json.loads(open(sys.argv[1], encoding="utf-8").read())
result = check(payload["title"], payload["body"], payload["labels"])
verdict = getattr(result, "verdict", None)
shape = getattr(result, "shape", None)
print(
    json.dumps(
        {
            "verdict": getattr(verdict, "value", verdict),
            "shape": getattr(shape, "value", shape),
            "reasons": [
                {
                    "code": getattr(r, "code", None),
                    "severity": getattr(getattr(r, "severity", None), "value", None),
                    "detail": getattr(r, "detail", None),
                }
                for r in getattr(result, "reasons", ())
            ],
        }
    )
)
"""


class TargetGateError(RuntimeError):
    """The target repository's gate state could not be established."""


@dataclass(frozen=True)
class GateReason:
    code: str
    severity: str | None
    detail: str | None


@dataclass(frozen=True)
class TargetGateVerdict:
    """A verdict produced by the gate revision resident in the target repo."""

    repo: str
    ref: str
    sha: str
    verdict: str
    shape: str | None
    reasons: tuple[GateReason, ...]

    @property
    def source(self) -> str:
        return f"{self.repo}@{self.sha[:12]} ({self.ref})"


def _default_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=GH_TIMEOUT_SECONDS,
    )


def evaluate_target_gate(
    *,
    repo: str,
    title: str,
    body: str,
    labels: list[str],
    runner: Runner | None = None,
) -> TargetGateVerdict:
    """Run ``repo``'s own shape gate over the body. Never falls back locally."""
    run = runner or _default_runner
    ref = _default_branch(repo, run)
    sha = _head_sha(repo, ref, run)
    sources = _download_gate_package(repo, sha, run)
    payload = _run_gate(title=title, body=body, labels=labels, sources=sources)
    verdict = payload.get("verdict")
    if not isinstance(verdict, str) or not verdict.strip():
        raise TargetGateError(
            f"{repo}'s gate at {sha[:12]} produced no verdict "
            f"({verdict!r}); the body cannot be placed in a known gate state"
        )
    shape = payload.get("shape")
    reasons = tuple(
        GateReason(
            code=str(entry.get("code")),
            severity=_opt_str(entry.get("severity")),
            detail=_opt_str(entry.get("detail")),
        )
        for entry in payload.get("reasons") or []
        if isinstance(entry, dict)
    )
    return TargetGateVerdict(
        repo=repo,
        ref=ref,
        sha=sha,
        verdict=verdict.strip(),
        shape=_opt_str(shape),
        reasons=reasons,
    )


# ── target revision resolution ────────────────────────────────────────────────


def _gh(command: list[str], run: Runner, *, what: str) -> str:
    try:
        proc = run(command)
    except FileNotFoundError as exc:
        raise TargetGateError(f"cannot {what}: gh is not installed ({exc})") from exc
    except subprocess.TimeoutExpired as exc:
        raise TargetGateError(f"cannot {what}: gh timed out after {exc.timeout}s") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip() or (proc.stdout or "").strip() or "gh failed"
        raise TargetGateError(f"cannot {what}: {detail}")
    return proc.stdout or ""


def _default_branch(repo: str, run: Runner) -> str:
    out = _gh(
        [
            "gh",
            "repo",
            "view",
            repo,
            "--json",
            "defaultBranchRef",
            "--jq",
            ".defaultBranchRef.name",
        ],
        run,
        what=f"resolve {repo}'s default branch",
    ).strip()
    if not out:
        raise TargetGateError(f"cannot resolve {repo}'s default branch: gh returned nothing")
    return out


def _head_sha(repo: str, ref: str, run: Runner) -> str:
    out = _gh(
        ["gh", "api", f"repos/{repo}/commits/{ref}", "--jq", ".sha"],
        run,
        what=f"pin {repo}@{ref} to a commit",
    ).strip()
    if not out:
        raise TargetGateError(f"cannot pin {repo}@{ref} to a commit: gh returned no sha")
    return out


def _download_gate_package(repo: str, sha: str, run: Runner) -> dict[str, str]:
    """Return ``{filename: source}`` for the gate package at ``sha``."""
    listing = _gh(
        [
            "gh",
            "api",
            f"repos/{repo}/contents/{GATE_PACKAGE_PATH}?ref={sha}",
            "--jq",
            '.[] | select(.type == "file") | .name',
        ],
        run,
        what=f"list {repo}:{GATE_PACKAGE_PATH} at {sha[:12]}",
    )
    names = [line.strip() for line in listing.splitlines() if line.strip().endswith(".py")]
    # A name carrying a path separator would escape the package root we lay the
    # download out in; the API should never return one, so treat it as hostile.
    unsafe = [name for name in names if "/" in name or name.startswith(".")]
    if unsafe:
        raise TargetGateError(f"{repo}:{GATE_PACKAGE_PATH} listed unusable file names {unsafe!r}")
    if not names:
        raise TargetGateError(
            f"{repo} has no {GATE_PACKAGE_PATH} at {sha[:12]}; it exposes no shape gate "
            "this report could be placed against"
        )
    sources: dict[str, str] = {}
    for name in names:
        encoded = _gh(
            [
                "gh",
                "api",
                f"repos/{repo}/contents/{GATE_PACKAGE_PATH}/{name}?ref={sha}",
                "--jq",
                ".content",
            ],
            run,
            what=f"download {repo}:{GATE_PACKAGE_PATH}/{name} at {sha[:12]}",
        )
        try:
            sources[name] = base64.b64decode(encoded).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise TargetGateError(
                f"cannot decode {repo}:{GATE_PACKAGE_PATH}/{name} at {sha[:12]}: {exc}"
            ) from exc
    if "check.py" not in sources:
        raise TargetGateError(
            f"{repo}:{GATE_PACKAGE_PATH} at {sha[:12]} has no check.py; "
            "its gate entry point cannot be located"
        )
    return sources


# ── isolated execution ────────────────────────────────────────────────────────


def _run_gate(
    *, title: str, body: str, labels: list[str], sources: dict[str, str]
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="forge-target-gate-") as tmp:
        root = Path(tmp)
        package = root / "theforge" / "shape_check"
        package.mkdir(parents=True)
        # A minimal namespace root: the target's own theforge/__init__.py reads
        # installed-distribution metadata that does not exist here, and the gate
        # never needs it.
        (root / "theforge" / "__init__.py").write_text("", encoding="utf-8")
        for name, source in sources.items():
            (package / name).write_text(source, encoding="utf-8")
        runner_path = root / "_run_gate.py"
        runner_path.write_text(_RUNNER_SOURCE, encoding="utf-8")
        payload_path = root / "payload.json"
        payload_path.write_text(
            json.dumps({"title": title, "body": body, "labels": list(labels)}),
            encoding="utf-8",
        )
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(runner_path),
                    str(payload_path),
                    str(root),
                ],
                capture_output=True,
                text=True,
                cwd=str(root),
                timeout=GATE_RUN_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise TargetGateError(
                f"the target repository's gate did not finish within {exc.timeout}s"
            ) from exc
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip().splitlines()
            raise TargetGateError(
                "the target repository's gate could not be executed: "
                + (detail[-1] if detail else f"exit {proc.returncode}")
            )
        try:
            parsed = json.loads(proc.stdout or "")
        except json.JSONDecodeError as exc:
            raise TargetGateError(
                f"the target repository's gate produced unreadable output: {exc}"
            ) from exc
    if not isinstance(parsed, dict):
        raise TargetGateError("the target repository's gate produced a non-object result")
    return parsed


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


__all__ = [
    "GATE_PACKAGE_PATH",
    "GateReason",
    "TargetGateError",
    "TargetGateVerdict",
    "evaluate_target_gate",
]
