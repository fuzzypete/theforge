---
name: "forge version: append -dev suffix when editable install is ahead of tag"
slug: version-dev-suffix
pytest_target: tests/
---

# Version -dev suffix

## Problem

`forge version` shows `0.2.1` on an editable install that is 5 commits ahead
of the v0.2.1 tag. This is indistinguishable from the released version, making
it impossible to tell whether you're running a release or development tip.

## Requirements

1. When the installed version has commits ahead of the latest tag, the version
   string displays `<version>-dev+<short-hash>` (e.g., `0.2.1-dev+g8704ff0`)
2. When the install is at exactly a tag (distance = 0), display the clean
   version only (e.g., `0.2.1`)
3. When installed from PyPI (non-editable), display the clean version only —
   git metadata is unavailable in that case

## Acceptance Criteria

- [ ] Editable install with commits ahead of tag shows `<version>-dev+g<hash>`
- [ ] Editable install at exact tag shows clean version with no suffix
- [ ] Non-editable (PyPI) install shows clean version only
- [ ] `forge version` output includes the dev suffix when applicable
- [ ] Existing tests pass
- [ ] New tests cover: dev suffix appended when ahead, clean when at tag,
      clean when git unavailable

## Out of Scope

- Changing the version bumping/release process
- Adding build metadata beyond the short hash
- Modifying `forge version` output format (branch, commit, tag distance lines)
