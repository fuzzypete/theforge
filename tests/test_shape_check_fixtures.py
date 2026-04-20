"""Gold regression and meta-check tests for shape_check."""

from __future__ import annotations

from pathlib import Path

from theforge.shape_check import Shape, SuggestedAction, check

FIXTURES = Path(__file__).parent / "shape_check_fixtures"


def _split_title_body(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    assert lines and lines[0].startswith("# "), "fixture must start with '# Title'"
    title = lines[0][2:].strip()
    body = "\n".join(lines[1:]).lstrip("\n")
    return title, body


def test_gold_issue_717_pre_rewrite_is_policy_soup():
    """Pre-rewrite #717 body bundles six clusters; must be caught as split-needs."""
    text = (FIXTURES / "issue_717_pre_rewrite.md").read_text()
    title, body = _split_title_body(text)
    result = check(title, body, labels=["enhancement"])
    assert result.shape is Shape.NEEDS_GROOMING
    assert result.suggested_action is SuggestedAction.SPLIT
    assert any(r.code == "too_many_behavioral_clusters" for r in result.reasons), (
        f"expected too_many_behavioral_clusters, got {[r.code for r in result.reasons]}"
    )


def test_meta_issue_806_passes_its_own_shape_check():
    """Per spec: this story itself must pass shape check when the feature ships."""
    text = (FIXTURES / "issue_806_self.md").read_text()
    title, body = _split_title_body(text)
    result = check(title, body, labels=[])
    assert result.shape is Shape.RUNNABLE, (
        f"meta-check failed: shape={result.shape}, "
        f"reasons={[(r.code, r.detail) for r in result.reasons]}"
    )
    assert result.suggested_action is SuggestedAction.PROCEED
