from pathlib import Path


def test_gate_runs_index_before_pytest() -> None:
    makefile = Path("Makefile").read_text()
    gate_start = makefile.index("gate:\n")
    gate_end = makefile.index("gate-serial:")
    gate_block = makefile[gate_start:gate_end]

    index_cmd = "forge index"
    guard_cmd = "forge check-story-config"
    scrubbed_invocation = "$(SCRUBBED_GATE_CMD)"
    pytest_cmd = "PYTHONPATH=src python -m pytest tests/ -q -n auto --dist worksteal"

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
