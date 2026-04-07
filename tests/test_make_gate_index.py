from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_gate_runs_index_before_tests_and_updates_modules_yaml(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / ".forge" / "index").mkdir(parents=True)
    (repo / "tests").mkdir()

    (repo / "Makefile").write_text(
        ".PHONY: gate\n"
        "gate:\n"
        "\t@mkdir -p .forge/index .forge && \\\n"
        "\tforge index && \\\n"
        "\tPYTHONPATH=src python -m pytest tests/ -q -n auto --dist worksteal && \\\n"
        "\tpython -c \"print('PASS')\" || \\\n"
        "\tpython -c \"print('FAIL')\"\n",
        encoding="utf-8",
    )

    (repo / "forge").write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        "import sys\n"
        "if sys.argv[1:] != ['index']:\n"
        "    raise SystemExit(2)\n"
        "Path('.forge/index/modules.yaml').write_text('updated: true\\n', encoding='utf-8')\n"
        "print('index ran')\n",
        encoding="utf-8",
    )
    (repo / "forge").chmod(0o755)

    (repo / "python").write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "if args[:3] == ['-m', 'pytest', 'tests/']:\n"
        "    modules = Path('.forge/index/modules.yaml')\n"
        "    assert modules.exists(), 'index must run before tests'\n"
        "    print('tests passed')\n"
        "    raise SystemExit(0)\n"
        "if args[:2] == ['-c', \"print('PASS')\"]:\n"
        "    print('PASS')\n"
        "    raise SystemExit(0)\n"
        "if args[:2] == ['-c', \"print('FAIL')\"]:\n"
        "    print('FAIL')\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(f'unexpected args: {args!r}')\n",
        encoding="utf-8",
    )
    (repo / "python").chmod(0o755)

    env = {**os.environ, "PATH": f"{repo}:{os.environ['PATH']}"}
    result = subprocess.run(["make", "gate"], cwd=repo, capture_output=True, text=True, env=env)

    assert result.returncode == 0, result.stderr
    assert (repo / ".forge" / "index" / "modules.yaml").read_text(
        encoding="utf-8"
    ) == "updated: true\n"
    assert "index ran" in result.stdout


def test_gate_still_reports_fail_when_tests_fail_even_if_index_succeeds(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / ".forge" / "index").mkdir(parents=True)
    (repo / "tests").mkdir()

    (repo / "Makefile").write_text(
        ".PHONY: gate\n"
        "gate:\n"
        "\t@mkdir -p .forge/index .forge && \\\n"
        "\tforge index && \\\n"
        "\tPYTHONPATH=src python -m pytest tests/ -q -n auto --dist worksteal && \\\n"
        "\tpython -c \"print('PASS')\" || \\\n"
        "\tpython -c \"print('FAIL')\"\n",
        encoding="utf-8",
    )

    (repo / "forge").write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        "import sys\n"
        "if sys.argv[1:] != ['index']:\n"
        "    raise SystemExit(2)\n"
        "Path('.forge/index/modules.yaml').write_text('updated: true\\n', encoding='utf-8')\n"
        "print('index ran')\n",
        encoding="utf-8",
    )
    (repo / "forge").chmod(0o755)

    (repo / "python").write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        "if args[:3] == ['-m', 'pytest', 'tests/']:\n"
        "    print('tests failed')\n"
        "    raise SystemExit(1)\n"
        "if args[:2] == ['-c', \"print('PASS')\"]:\n"
        "    print('PASS')\n"
        "    raise SystemExit(0)\n"
        "if args[:2] == ['-c', \"print('FAIL')\"]:\n"
        "    print('FAIL')\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(f'unexpected args: {args!r}')\n",
        encoding="utf-8",
    )
    (repo / "python").chmod(0o755)

    env = {**os.environ, "PATH": f"{repo}:{os.environ['PATH']}"}
    result = subprocess.run(["make", "gate"], cwd=repo, capture_output=True, text=True, env=env)

    assert result.returncode == 0
    assert "FAIL" in result.stdout
    assert (repo / ".forge" / "index" / "modules.yaml").exists()
