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

# Gate: run tests and write .forge/handoff.yaml
gate:
	@mkdir -p .forge/index .forge && \
	forge index && \
	PYTHONPATH=src python -m pytest tests/ -q -n auto --dist worksteal && \
	python -c "\
import yaml, pathlib; \
pathlib.Path('.forge/handoff.yaml').write_text(yaml.dump({'gate_decision': 'PASS', 'scope_completed': [], 'deferred_followups': [], 'next_recommended_step': 'merge'})); \
print('[gate] PASS')" || \
	python -c "\
import yaml, pathlib; \
pathlib.Path('.forge/handoff.yaml').write_text(yaml.dump({'gate_decision': 'FAIL', 'scope_completed': [], 'deferred_followups': ['fix test failures'], 'next_recommended_step': 'fix failing tests'})); \
print('[gate] FAIL')"

gate-serial:
	@mkdir -p .forge && \
	PYTHONPATH=src python -m pytest tests/ -q && \
	python -c "\
import yaml, pathlib; \
pathlib.Path('.forge/handoff.yaml').write_text(yaml.dump({'gate_decision': 'PASS', 'scope_completed': [], 'deferred_followups': [], 'next_recommended_step': 'merge'})); \
print('[gate] PASS')" || \
	python -c "\
import yaml, pathlib; \
pathlib.Path('.forge/handoff.yaml').write_text(yaml.dump({'gate_decision': 'FAIL', 'scope_completed': [], 'deferred_followups': ['fix test failures'], 'next_recommended_step': 'fix failing tests'})); \
print('[gate] FAIL')"

clean:
	rm -rf .forge/worktrees/ .forge/handoff.yaml .forge/audit.yaml
