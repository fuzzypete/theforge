---
name: "Add farewell endpoint"
slug: add-farewell
pytest_target: tests/
---

# Add Farewell Endpoint

## Problem

Users can greet but not say goodbye. Add a `/farewell` endpoint that mirrors
the greeting pattern.

## Acceptance criteria

- GET `/farewell?name=Alice` returns `{"message": "Goodbye, Alice!"}`
- GET `/farewell` (no name) returns `{"message": "Goodbye, world!"}`
- A test in `tests/test_farewell.py` verifies both cases
- Existing tests continue to pass
