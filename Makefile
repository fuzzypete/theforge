.PHONY: fmt lint test test-parallel gate gate-py gate-venv gate-strict gate-serial test-integration clean

# Dependency metadata pip actually reads when installing this project. poetry.lock
# is deliberately excluded: CI installs with `pip install -e ".[dev]"`, which
# ignores it, so keying on it would reinstall for a file that cannot change what
# CI resolves.
GATE_DEPS_FILE = pyproject.toml
# Written inside each per-version gate venv; holds the checksum of GATE_DEPS_FILE
# that the venv's installed packages correspond to.
GATE_DEPS_STAMP = .forge-gate-deps

SCRUBBED_ENV_VARS := OPENAI_API_KEY ANTHROPIC_API_KEY GOOGLE_API_KEY GEMINI_API_KEY DEEPSEEK_API_KEY XAI_API_KEY GROQ_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN OPENAI_BASE_URL DOTENV_PATH DOTENV_FILE PYTHON_DOTENV_DISABLED
GATE_PYTEST_CMD = PYTHONPATH=src python -m pytest tests/ -q -n auto --dist worksteal
# Virtualenv whose bin/ leads PATH for the gate. `gate-py` overrides it so a
# matrix leg runs this exact command list under a different interpreter — the
# interpreter is the only variable between legs, and between local gate and CI.
GATE_VENV = .venv
SCRUBBED_GATE_CMD = env -i PATH="$(GATE_VENV)/bin:$$PATH" HOME="$$HOME" PYTHON_DOTENV_DISABLED=1 /bin/sh -c 'unset $(SCRUBBED_ENV_VARS); export PYTHON_DOTENV_DISABLED=1; ruff check src/ tests/ && ruff format --check src/ tests/ && $(GATE_PYTEST_CMD)'

# Format
fmt:
	ruff format src/ tests/
	ruff check --fix src/ tests/

# Lint (no auto-fix)
lint:
	ruff check src/ tests/
	ruff format --check src/ tests/

test:
	PYTHONPATH=src python -m pytest tests/ -v -n auto --dist worksteal

# Opt-in parallel run: useful for local iteration when not running a sprint.
# Uses worksteal for even distribution — tests are fully parallel-safe.
# Avoid running this inside a forge sprint with max_parallel > 1 — between
# the two layers you can easily saturate all cores.
test-parallel:
	PYTHONPATH=src python -m pytest tests/ -v -n auto --dist worksteal

# Gate: run lint, format check, and tests. Exit 0 = PASS, non-zero = FAIL.
# The coordinator reads the exit code; no handoff file is written.
gate:
	@mkdir -p .forge/index .forge && \
	forge index && \
	forge check-story-config && \
	$(SCRUBBED_GATE_CMD)

# Transitional alias retained for one release while the scrubbed gate rolls out.
gate-strict:
	@$(SCRUBBED_GATE_CMD)

# Provision or refresh the per-version gate venv. Split out of gate-py so the
# refresh rule can be exercised without running the suite.
#
# Venv *creation* is guarded by directory existence; dependency *installation*
# is not. CI installs dependencies fresh for every run, so a leg that reused a
# venv built before $(GATE_DEPS_FILE) changed would prove a dependency set CI
# never installs — the same class of gate/CI disagreement this matrix exists to
# close. The venv is therefore keyed on the checksum of the dependency metadata:
# a changed checksum reinstalls, an unchanged one reuses. This mirrors how the
# coordinator provisions the dev venv (coordinator/workspace.py
# _run_setup_split), which likewise guards only venv creation by existence.
#
# The stamp is written only after a successful install, so an interrupted or
# failed install is retried on the next run rather than cached as good.
gate-venv:
	@test -n "$(PY)" || { echo "gate-venv requires PY=<version>, e.g. make gate-venv PY=3.11" >&2; exit 2; }
	@command -v python$(PY) >/dev/null 2>&1 || { echo "gate-venv: python$(PY) not found on PATH" >&2; exit 2; }
	@test -d .venv-$(PY) || python$(PY) -m venv .venv-$(PY)
	@stamp=".venv-$(PY)/$(GATE_DEPS_STAMP)"; \
	want=$$(cksum $(GATE_DEPS_FILE)); \
	if [ ! -f "$$stamp" ] || [ "$$(cat "$$stamp")" != "$$want" ]; then \
		echo "gate-venv: installing .[dev] into .venv-$(PY) ($(GATE_DEPS_FILE) changed or venv is new)"; \
		.venv-$(PY)/bin/pip install -e '.[dev]' -q || exit 1; \
		printf '%s\n' "$$want" > "$$stamp"; \
	fi

# One leg of the interpreter matrix: run the `gate` target verbatim under a
# specific Python version. `make gate-py PY=3.11`. The venv carries the same
# extras CI installs (".[dev]", not ".[all,dev]") so a leg proves what the
# required merge checks prove. No lint/test commands are duplicated here — this
# target only chooses the interpreter. Driven by validation.python_versions in
# forge.yaml.
gate-py:
	@$(MAKE) --no-print-directory gate-venv PY=$(PY)
	@$(MAKE) --no-print-directory gate GATE_VENV=.venv-$(PY)

# Serial gate: same checks without xdist, useful for debugging hangs.
gate-serial:
	@env -i PATH=".venv/bin:$$PATH" HOME="$$HOME" PYTHON_DOTENV_DISABLED=1 /bin/sh -c 'unset $(SCRUBBED_ENV_VARS); export PYTHON_DOTENV_DISABLED=1; ruff check src/ tests/ && ruff format --check src/ tests/ && PYTHONPATH=src python -m pytest tests/ -q'

test-integration:
	@THEFORGE_RUN_INTEGRATION=1 THEFORGE_ALLOW_AGENT_CREDS=1 PYTHONPATH=src .venv/bin/python -m pytest tests/ -v -n auto --dist worksteal -m network_integration

clean:
	rm -rf .forge/worktrees/ .forge/audit.yaml
