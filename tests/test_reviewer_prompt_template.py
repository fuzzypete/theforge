"""Guards for the verification-gated reviewer prompt template artifact.

These lock in the acceptance criteria for the reusable reviewer prompt
template (issue #1826): it exists as a durable doc artifact, grounds review in
tree-state proof *before* analysis, carries per-claim certainty tags and
anti-flattery framing, and is discoverable from the review-trust context.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "docs" / "guides" / "reviewer-prompt-template.md"
REFUSAL = ROOT / "docs" / "vision" / "refusal-capability.md"
README = ROOT / "README.md"


def test_reviewer_prompt_template_artifact_exists() -> None:
    assert TEMPLATE.exists(), "durable reviewer prompt template artifact is missing"


def test_template_places_tree_state_grounding_before_analysis() -> None:
    text = TEMPLATE.read_text()
    lower = text.lower()
    # Tree-state proof must be a precondition placed before the analysis step.
    proof_idx = lower.find("git rev-parse head")
    analyse_idx = lower.find("=== now review ===")
    assert proof_idx != -1, "template must require tree-state proof (git rev-parse HEAD)"
    assert analyse_idx != -1, "template must have an explicit analysis gate"
    assert proof_idx < analyse_idx, "tree-state proof must come BEFORE analysis"
    # The abort-on-mismatch precondition makes confabulation the expensive path.
    assert "stop" in lower and "wrong tree" in lower


def test_template_has_per_claim_certainty_tags() -> None:
    text = TEMPLATE.read_text()
    for tag in ("[VERIFIED]", "[INFERRED]", "[SPECULATIVE]"):
        assert tag in text, f"template must define the {tag} per-claim certainty tag"


def test_template_has_anti_flattery_framing() -> None:
    lower = TEMPLATE.read_text().lower()
    assert "anti-flattery" in lower
    # The reviewer must be told agreement/flattery is not the goal.
    assert "non-answer" in lower or "looks good" in lower


def test_template_is_discoverable_from_review_trust_context() -> None:
    # Discoverable from the refusal-capability doctrine (the review-trust context)
    # and from the README guides index.
    assert "reviewer-prompt-template.md" in REFUSAL.read_text()
    assert "reviewer-prompt-template.md" in README.read_text()


def test_template_relates_to_reviewer_tree_currency() -> None:
    lower = TEMPLATE.read_text().lower()
    assert "tree-currency" in lower or "tree currency" in lower
    assert "stale checkout" in lower
