.PHONY: fmt lint test test-parallel gate gate-serial clean

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
	ruff check src/ tests/ && \
	ruff format --check src/ tests/ && \
	PYTHONPATH=src python -m pytest tests/ -q -n auto --dist worksteal

# Serial gate: same checks without xdist, useful for debugging hangs.
gate-serial:
	@ruff check src/ tests/ && \
	ruff format --check src/ tests/ && \
	PYTHONPATH=src python -m pytest tests/ -q

clean:
	rm -rf .forge/worktrees/ .forge/audit.yaml
