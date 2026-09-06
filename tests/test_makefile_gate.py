from pathlib import Path


def test_gate_runs_index_before_pytest() -> None:
    makefile = Path("Makefile").read_text()
    gate_start = makefile.index("gate:\n")
    gate_end = makefile.index("gate-serial:")
    gate_block = makefile[gate_start:gate_end]

    index_cmd = "forge index"
    guard_cmd = "forge check-story-config"
    scrubbed_invocation = "$(SCRUBBED_GATE_CMD)"
    # Worker count is $(GATE_WORKERS), defaulted to `auto` so CI and a solo gate are
    # unchanged; a parallel sprint caps it via the environment. Both halves are
    # asserted below so the default cannot be silently changed.
    pytest_cmd = "PYTHONPATH=src python -m pytest tests/ -q -n $(GATE_WORKERS) --dist worksteal"

    assert index_cmd in gate_block
    assert guard_cmd in gate_block
    assert scrubbed_invocation in gate_block
    assert gate_block.index(index_cmd) < gate_block.index(scrubbed_invocation)
    assert (
        gate_block.index(index_cmd)
        < gate_block.index(guard_cmd)
        < gate_block.index(scrubbed_invocation)
    )
    # The scrubbed gate command itself carries the pytest invocation.
    assert pytest_cmd in makefile
    # The default must stay `auto`: CI runs a bare `make gate` and should use every
    # core. Only a parallel sprint overrides it, through the environment.
    assert "GATE_WORKERS ?= auto" in makefile
