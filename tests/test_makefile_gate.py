from pathlib import Path


def test_gate_runs_index_before_pytest() -> None:
    makefile = Path("Makefile").read_text()
    gate_start = makefile.index("gate:\n")
    gate_end = makefile.index("\n\ngate-serial:")
    gate_block = makefile[gate_start:gate_end]

    index_cmd = "PYTHONPATH=src python -m theforge.cli.main index"
    pytest_cmd = "PYTHONPATH=src python -m pytest tests/ -q -n auto --dist worksteal"

    assert index_cmd in gate_block
    assert pytest_cmd in gate_block
    assert gate_block.index(index_cmd) < gate_block.index(pytest_cmd)
