from __future__ import annotations

from theforge.knowledge_admissibility import (
    REASON_NO_VERDICT,
    REASON_SOURCES_CHANGED,
    RESOLUTION_RESOLVED,
    SOURCE_STATE_CHANGED,
    STATUS_ADMISSIBLE_WITH_REDUCED_RANK,
    STATUS_INADMISSIBLE,
    KnowledgeSourceFact,
    KnowledgeSummaryFacts,
    evaluate_summary_admissibility,
    interpret_persisted_verdict,
)


def test_missing_verdict_fails_closed() -> None:
    verdict = interpret_persisted_verdict(None)

    assert verdict.status == STATUS_INADMISSIBLE
    assert verdict.rank == "excluded"
    assert verdict.reasons == (REASON_NO_VERDICT,)


def test_same_summary_and_facts_yield_the_same_verdict() -> None:
    summary = {
        "what_was_learned": [
            {
                "claim": "keep retry logic close to the client",
                "evidence": [{"type": "file", "path": "src/client.py"}],
            }
        ]
    }
    facts = KnowledgeSummaryFacts(
        source_run_tainted=False,
        source_run_resolution=RESOLUTION_RESOLVED,
        provenance_resolution=RESOLUTION_RESOLVED,
        cited_sources=(
            KnowledgeSourceFact(
                cited_path="src/client.py",
                state=SOURCE_STATE_CHANGED,
                current_path="src/client.py",
                commits_since_summary=2,
            ),
        ),
    )

    first = evaluate_summary_admissibility(summary, facts)
    second = evaluate_summary_admissibility(summary, facts)

    assert first == second
    assert first.status == STATUS_ADMISSIBLE_WITH_REDUCED_RANK
    assert first.rank == "reduced"
    assert first.reasons == (REASON_SOURCES_CHANGED,)
