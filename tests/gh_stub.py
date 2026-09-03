"""A ``gh`` stub shared by the workflow behavior tests.

The workflows that close issues now call the spike closure guard, and the guard
reads GitHub through ``gh``. Each workflow test already stubbed ``gh`` at the
process boundary; this is that stub, widened once so the guard's own reads are
answered the same way — the alternative was three drifting copies of the same
JSON-shaped fake.

Facts are driven by ``FAKE_*`` environment variables. Each may be set globally
(``FAKE_LABELS``) or per issue number (``FAKE_LABELS_2348``); the per-issue
value wins, which is what a follow-on-issue lookup needs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: Log files the stub appends invocations to, one per mutating subcommand.
LOG_NAMES = ("label", "close", "edit", "comment", "reopen")

GH_STUB = r"""#!/usr/bin/env bash
set -eu

# lookup PREFIX NUMBER -> the per-issue value if set, else the global default.
lookup() {
  local prefix="$1" n="$2" v
  eval "v=\${${prefix}_${n}:-__unset__}"
  if [ "$v" = "__unset__" ]; then
    eval "v=\${${prefix}:-}"
  fi
  printf '%s' "$v"
}

json_string() { python3 -c 'import json,sys; sys.stdout.write(json.dumps(sys.argv[1]))' "$1"; }

json_labels() {
  local out="[" first=1 l
  # `read` reports EOF on empty input; an issue with no labels is not an error.
  IFS=',' read -ra parts <<< "${1:-}" || true
  # ${parts[@]+...} keeps an empty array from tripping `set -u`.
  for l in ${parts[@]+"${parts[@]}"}; do
    if [ -z "$l" ]; then continue; fi
    if [ "$first" = 1 ]; then first=0; else out="$out,"; fi
    out="$out{\"name\":$(json_string "$l")}"
  done
  printf '%s' "$out]"
}

record() { printf '%s\n' "$*" >> "$1"; }

case "$1" in
  label)
    record "${LABEL_LOG:-/dev/null}" "$*"
    exit 0
    ;;
  api)
    if [ "$2" = "graphql" ]; then
      printf '%s' "${FAKE_PARENT:-}"
      exit 0
    fi
    for a in "$@"; do
      case "$a" in
        *select*) printf '%s\n' ${FAKE_SUBS_OPEN:-}; exit 0 ;;
      esac
    done
    printf '%s\n' ${FAKE_SUBS_ALL:-}
    exit 0
    ;;
  issue)
    sub="$2"
    n="${3:-}"
    case "$sub" in
      view)
        jq_mode=0
        for a in "$@"; do
          if [ "$a" = "--jq" ]; then jq_mode=1; fi
        done
        state=$(lookup FAKE_STATE "$n")
        if [ -z "$state" ]; then state="OPEN"; fi
        if [ "$jq_mode" = 1 ]; then
          printf '%s' "$state"
          exit 0
        fi
        labels=$(json_labels "$(lookup FAKE_LABELS "$n")")
        body=$(json_string "$(lookup FAKE_BODY "$n")")
        comment=$(lookup FAKE_COMMENT "$n")
        if [ -z "$comment" ]; then
          comments="[]"
        else
          comments="[{\"body\":$(json_string "$comment")}]"
        fi
        printf '{"state":"%s","labels":%s,"body":%s,"comments":%s}' \
          "$state" "$labels" "$body" "$comments"
        exit 0
        ;;
      close)   record "$CLOSE_LOG" "$*"; exit 0 ;;
      edit)    record "$EDIT_LOG" "$*"; exit 0 ;;
      comment) record "$COMMENT_LOG" "$*"; exit 0 ;;
      reopen)  record "$REOPEN_LOG" "$*"; exit 0 ;;
    esac
    ;;
esac
echo "unexpected gh call: $*" >&2
exit 1
"""

_REPO_ROOT = Path(__file__).resolve().parents[1]


def install_stubs(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    """Write the ``gh`` stub and a ``python3`` shim; return (bindir, logs).

    The ``python3`` shim points at the interpreter running the tests so the
    workflow's ``python3 -m theforge.spike_guard`` executes the *real* guard,
    not a second fake of it. Only ``gh`` is faked.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)

    gh = bindir / "gh"
    gh.write_text(GH_STUB)
    gh.chmod(0o755)

    python3 = bindir / "python3"
    python3.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python3.chmod(0o755)

    logs = {name: tmp_path / f"{name}.log" for name in LOG_NAMES}
    for path in logs.values():
        path.write_text("")
    return bindir, logs


def stub_env(bindir: Path, logs: dict[str, Path], tmp_path: Path) -> dict[str, str]:
    """The base environment a stubbed workflow step runs under."""
    return {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "PYTHONPATH": str(_REPO_ROOT / "src"),
        "GH_TOKEN": "stub",
        **{f"{name.upper()}_LOG": str(path) for name, path in logs.items()},
    }


def workflow_step(document: dict, job: str) -> str:
    """Return the one shell step of ``job`` — the step that carries a ``run``.

    Selected by content rather than by index so adding a checkout step ahead of
    it does not silently make the test run the wrong thing.
    """
    steps = [step for step in document["jobs"][job]["steps"] if "run" in step]
    assert len(steps) == 1, f"expected exactly one run step in {job}, got {len(steps)}"
    return steps[0]["run"]
