"""gh CLI contract tests for the ghaw runner.

Validates that argv constructed by runner_ghaw's builders is accepted by the
installed `gh` CLI. The runner shells out to core `gh` subcommands
(workflow run / run list / run view / run download / run cancel), so the
drift surface is gh's flag grammar, not the gh-aw extension.
"""

from __future__ import annotations

import pytest

from tests.conftest import require_cli
from tests.contract.conftest import assert_cli_accepts_argv
from theforge.runners import runner_ghaw

pytestmark = pytest.mark.cli_contract


@pytest.mark.parametrize(
    "shape,builder",
    [
        (
            "workflow_dispatch",
            lambda: runner_ghaw.build_argv(
                workflow="contract-check.lock.yml",
                ref="main",
                dispatch_id="contract",
                prompt="contract-check",
            ),
        ),
        (
            "run_list",
            lambda: runner_ghaw.build_run_list_argv(
                workflow="contract-check.lock.yml", ref="main"
            ),
        ),
        (
            "run_view",
            lambda: runner_ghaw.build_run_view_argv(run_id="1"),
        ),
        (
            "run_download",
            lambda: runner_ghaw.build_run_download_argv(run_id="1", dest="/tmp/contract"),
        ),
        (
            "run_cancel",
            lambda: runner_ghaw.build_run_cancel_argv(run_id="1"),
        ),
    ],
)
def test_ghaw_argv_accepted(shape: str, builder) -> None:
    require_cli("gh")
    assert_cli_accepts_argv(f"ghaw/{shape}", builder())
