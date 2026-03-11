.PHONY: fmt lint test gate clean

# Format
fmt:
	ruff format src/ tests/
	ruff check --fix src/ tests/

# Lint (no auto-fix)
lint:
	ruff check src/ tests/
	ruff format --check src/ tests/

# Tests
test:
	python -m pytest tests/ -v

# Gate: run tests and write handoff.yaml
gate:
	@python -m pytest tests/ -q && \
	python -c "\
import yaml, pathlib; \
pathlib.Path('handoff.yaml').write_text(yaml.dump({'gate_decision': 'PASS', 'scope_completed': [], 'deferred_followups': [], 'next_recommended_step': 'merge'})); \
print('[gate] PASS')" || \
	python -c "\
import yaml, pathlib; \
pathlib.Path('handoff.yaml').write_text(yaml.dump({'gate_decision': 'FAIL', 'scope_completed': [], 'deferred_followups': ['fix test failures'], 'next_recommended_step': 'fix failing tests'})); \
print('[gate] FAIL')"

clean:
	rm -rf .forge/worktrees/ handoff.yaml forge_audit.yaml
