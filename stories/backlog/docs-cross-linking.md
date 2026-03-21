---
name: Tighten cross-linking between all doc pages
slug: docs-cross-linking
pytest_target: tests/
---

# Tighten Cross-Linking Between All Doc Pages

## Problem

Docs exist but don't link to each other well enough. Users land on one page
and don't know where to go next. Each doc page should have clear "next steps"
or "see also" pointers so users can navigate by intent rather than by
guessing filenames.

## Acceptance criteria

- Every guide in docs/guides/ has a "See also" or "Next steps" section at
  the bottom linking to related docs
- README documentation table links to all guides including any new ones
  (troubleshooting, first-run walkthrough, provider setup chooser)
- Getting Started links forward to: CLI Reference, troubleshooting,
  first-run walkthrough, provider setup guide
- CLI Reference links to: troubleshooting (for common errors), Getting
  Started (for setup), Inputs Reference (for file formats)
- Troubleshooting links back to: CLI Reference (for correct usage),
  Getting Started (for setup redo)
- hello-forge README links to: Getting Started, first-run walkthrough
- No orphan docs — every doc is reachable from at least two other docs
