---
name: "Rename spec → story across the project"
slug: spec-to-story-rename
pytest_target: tests/
---

# Rename spec → story

## Problem

The term "spec" encourages implementation-heavy documents — detailed technical
specifications that prescribe HOW, not just WHAT. "Story" signals intent and
outcome. The discovery doc, dev prompt preamble, and vision doc already use
"story" but the code, CLI, directory structure, and sprint manifests all say
"spec."

This terminology mismatch causes agents to write implementation-coupled
documents when they should be writing behavior-focused stories. It also
confuses the human mental model: "write a spec" triggers a different
writing style than "write a story."

## Acceptance Criteria

- [ ] The primary user-facing term is "story" everywhere: CLI help text,
      error messages, log output, forge.yaml comments, documentation
- [ ] The directory for story files is named to reflect the new terminology
- [ ] Sprint manifests use the new key name for listing story files
- [ ] Internal code types and variables reflect the rename
- [ ] `forge init` scaffolds with the new naming
- [ ] `forge ideate` output uses the new naming
- [ ] Backward compatibility: old key names in forge.yaml and sprint manifests
      still work (with deprecation warning in logs)
- [ ] All existing tests pass (updated to reflect new names)
- [ ] CLAUDE.md conventions reference "story" not "spec"
