.PHONY: fmt lint test test-parallel dev-check gate gate-strict gate-serial test-integration clean

SCRUBBED_ENV_VARS := OPENAI_API_KEY ANTHROPIC_API_KEY GOOGLE_API_KEY GEMINI_API_KEY DEEPSEEK_API_KEY XAI_API_KEY GROQ_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN OPENAI_BASE_URL DOTENV_PATH DOTENV_FILE PYTHON_DOTENV_DISABLED
# xdist worker count. `auto` = one worker per logical core, correct when the gate
# owns the machine (CI, and any solo `make gate`). It is wrong under a parallel
# sprint: two concurrent gates each claim ~10 workers on a 10-logical/4-performance
# core box, and each run takes ~3.5x its solo time. Measured 2026-09-06 on 10928
# tests: -n auto 79.9s solo but two concurrent gates blew a 360s budget and
# escalated a story; -n 5 is 109.7s solo and two concurrent finish in 159s/169s.
# Override for local parallel sprints (forge.yaml gate_command), not for CI.
GATE_WORKERS ?= auto
GATE_PYTEST_CMD = PYTHONPATH=src python -m pytest tests/ -q -n $(GATE_WORKERS) --dist worksteal
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

# Dev inner loop. Runs the same lint and format checks `gate` applies, first, so
# a formatting violation costs seconds instead of surviving to the authoritative
# gate — four stories in three days stalled or died on ruff rules their dev loop
# never ran (E501, I001, and an unformatted signature). Then the gate's own
# pytest invocation, so a pass here means the same tests passed the same way.
#
# NOT authoritative: `gate` remains the only verdict that gates merge, and this
# target deliberately omits the env scrubbing and forge index/config steps that
# make `gate` trustworthy rather than merely informative.
dev-check: lint
	$(GATE_PYTEST_CMD)

# Opt-in parallel run: useful for local iteration when not running a sprint.
# Uses worksteal for even distribution — tests are fully parallel-safe.
# Avoid running this inside a forge sprint with max_parallel > 1 — between
# the two layers you can easily saturate all cores.
test-parallel:
	PYTHONPATH=src python -m pytest tests/ -v -n auto --dist worksteal

# Gate: run lint, format check, and tests. Exit 0 = PASS, non-zero = FAIL.
# The coordinator reads the exit code; no handoff file is written.
#
# Both forge invocations use .venv/bin/forge — the checkout under test — never
# the ambient PATH, which resolves to whichever released orchestrator is
# installed. SCRUBBED_GATE_CMD already pins PATH=".venv/bin:$$PATH" for the same
# reason; these two calls were the remaining hole. A bare `forge` here validates
# the checkout's config with the previous release's schema, so any change to a
# config contract fails its own gate after merge even though the change is
# correct (observed cutting v0.13.0rc17 against the #1415 model-identity
# migration: `Unknown model 'anthropic/sonnet/cli'` from the rc16 registry).
gate:
	@mkdir -p .forge/index .forge && \
	.venv/bin/forge index && \
	.venv/bin/forge check-story-config && \
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
