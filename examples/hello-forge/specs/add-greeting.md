---
name: "Add greeting endpoint"
slug: add-greeting
---

# Add Greeting Endpoint

## Problem

The app has no HTTP endpoints. We need a simple `/greet` endpoint that returns
a personalized greeting, so we can verify the forge pipeline end-to-end.

## Acceptance criteria

- GET `/greet?name=Alice` returns `{"message": "Hello, Alice!"}`
- GET `/greet` (no name) returns `{"message": "Hello, world!"}`
- A test in `tests/test_greet.py` verifies both cases
- Existing tests continue to pass
