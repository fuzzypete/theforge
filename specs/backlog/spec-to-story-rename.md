---
name: "Rename spec→story throughout codebase (terminology alignment)"
slug: spec-to-story-rename
pytest_target: tests/
---

# Spec → Story Rename

## Problem

TheForge uses "spec" throughout: `TaskSpec`, `spec_path`, `SpecTriage`,
`_validate_spec_paths`, `load_spec`, `specs/` directory, sprint manifest
`specs:` key, CLI output, docs. But the user-facing term is **story** —
that's what teams call the markdown files they drop into theforge.

The mismatch creates friction:
- Users write "stories" but the CLI talks about "specs"
- `forge run specs/backlog/my-feature.md` — why `specs/`?
- Sprint manifests have a `specs:` key but the files are stories
- `TaskSpec` — is that the spec file or the runtime task?

## What changes

### User-facing (highest priority)
- `specs/` directory → `stories/` (or keep `specs/` as alias; new projects use `stories/`)
- Sprint manifest key `specs:` → `stories:`
- CLI output: "Running spec" → "Running story"
- `forge ideate` output path: `specs/<slug>.md` → `stories/<slug>.md`
- `forge run` help text, error messages

### Internal identifiers (source + tests)
- `TaskSpec` → `StorySpec` (or just `Story`)
- `spec_path` → `story_path`
- `SpecTriage` → `StoryTriage`
- `_validate_spec_paths` → `_validate_story_paths`
- `_build_task_from_spec` → `_build_task_from_story`
- `load_spec` / `parse_spec_frontmatter` → `load_story` / `parse_story_frontmatter`
- Sprint manifest internal: `SprintManifest.specs` → `SprintManifest.stories`

### Backward compatibility
- Sprint manifests: accept both `specs:` and `stories:` keys (warn on `specs:`)
- `forge run` positional arg: accept paths in `specs/` and `stories/`
- forge.yaml: no changes needed (no spec references in forge.yaml)
- Existing `specs/` directories in projects: work unchanged

### What does NOT change
- `specs/done/`, `specs/backlog/`, `specs/archive/` in theforge's own repo —
  these are the meta-specs for theforge development. Rename as part of this
  story or leave for a follow-up; either is fine.
- The word "spec" in prose comments where it means "specification" generically
- `pytest_target`, `file_scope`, `slug` frontmatter keys — unrelated

## Implementation notes

This is a large mechanical rename. The dev agent should:
1. Use `replace_all` edits for identifier renames in Python source
2. Update sprint manifest parser to accept both `specs:` and `stories:` with deprecation warning
3. Update CLI help strings and error messages
4. Update `forge.yaml` comments and docs
5. Rename `specs/` → `stories/` in the example project (`examples/hello-forge/`)
6. Run `make fmt && make lint` after each file to catch issues early

Do NOT rename theforge's own `specs/` directory as part of this story —
that's a separate housekeeping task.

## Acceptance criteria

- [ ] `TaskSpec` renamed to `StorySpec` (or `Story`) throughout source + tests
- [ ] `spec_path` → `story_path` in all function signatures and variables
- [ ] `SpecTriage` → `StoryTriage` in sprint.py and tests
- [ ] Sprint manifest parser accepts `stories:` key; `specs:` still works with deprecation warning logged
- [ ] `load_spec` → `load_story`, `parse_spec_frontmatter` → `parse_story_frontmatter`
- [ ] CLI output says "story" not "spec" in user-facing messages
- [ ] `forge ideate` default output path is `stories/<slug>.md`
- [ ] `examples/hello-forge/` uses `stories/` directory
- [ ] All 980+ existing tests pass with new names
- [ ] No regressions in sprint resume, triage, or coordinator behaviour
