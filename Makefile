.PHONY: fmt lint test test-parallel gate gate-strict gate-serial test-integration clean

SCRUBBED_ENV_VARS := OPENAI_API_KEY ANTHROPIC_API_KEY GOOGLE_API_KEY GEMINI_API_KEY DEEPSEEK_API_KEY XAI_API_KEY GROQ_API_KEY ANTHROPIC_AUTH_TOKEN OPENAI_BASE_URL DOTENV_PATH DOTENV_FILE PYTHON_DOTENV_DISABLED
GATE_PYTEST_CMD = PYTHONPATH=src python -m pytest tests/ -q -n auto --dist worksteal
SCRUBBED_GATE_CMD = env -i PATH=".venv/bin:$$PATH" HOME="$$HOME" PYTHON_DOTENV_DISABLED=1 /bin/sh -c 'unset $(SCRUBBED_ENV_VARS); export PYTHON_DOTENV_DISABLED=1; ruff check src/ tests/ && ruff format --check src/ tests/ && $(GATE_PYTEST_CMD)'

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

# Serial gate: same checks without xdist, useful for debugging hangs.
gate-serial:
	@env -i PATH=".venv/bin:$$PATH" HOME="$$HOME" PYTHON_DOTENV_DISABLED=1 /bin/sh -c 'unset $(SCRUBBED_ENV_VARS); export PYTHON_DOTENV_DISABLED=1; ruff check src/ tests/ && ruff format --check src/ tests/ && PYTHONPATH=src python -m pytest tests/ -q'

test-integration:
	@THEFORGE_RUN_INTEGRATION=1 THEFORGE_ALLOW_AGENT_CREDS=1 PYTHONPATH=src .venv/bin/python -m pytest tests/ -v -n auto --dist worksteal -m network_integration

clean:
	rm -rf .forge/worktrees/ .forge/audit.yaml
