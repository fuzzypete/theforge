"""Policy-assertion provenance: registry classification and blocking adjudication (#2137).

The property under test is one-directional: ratified policy may stop chartered
work, generated or unmarked policy may not, and absence of a record is never
promoted to presence.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from theforge.escalation_advisor import (
    EvidencePacket,
    parse_advisory_report,
    render_advisory_for_pending,
    resolve_advisory_assertions,
)
from theforge.intake.shape_classify import Classification, classify
from theforge.policy_provenance import (
    MATCH_ID,
    MATCH_TEXT_EXACT,
    MATCH_TEXT_SIMILAR,
    MATCH_UNMATCHED,
    PROVENANCE_GENERATED,
    PROVENANCE_RATIFIED,
    PolicyAssertion,
    PolicyAssertionCitation,
    PolicyAssertionRegistry,
    adjudicate_blocked_verdict,
    load_policy_assertions,
    parse_citations,
    policy_assertions_path,
    reason_asserts_standing_decision,
)

# The #1108 assertion: a rationale a documentation run wrote into the routing
# policy source of truth, which then blocked chartered work at two layers.
_EFFORT_ASSERTION = "Reasoning effort is intentionally not score-controlled."


def _write_registry(project_root: Path, assertions: list[dict]) -> None:
    path = policy_assertions_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump({"version": 1, "assertions": assertions}, sort_keys=False),
        encoding="utf-8",
    )


def _ratified(text: str = _EFFORT_ASSERTION, **kwargs) -> PolicyAssertion:
    return PolicyAssertion(
        assertion_id=kwargs.pop("assertion_id", "effort-not-scored"),
        text=text,
        provenance=PROVENANCE_RATIFIED,
        reference=kwargs.pop("reference", "docs/adr/0006-adaptive-router.md#clause-4"),
        **kwargs,
    )


# ── Registry loading and classification ───────────────────────────────


class TestRegistryLoading:
    def test_missing_registry_is_empty_not_an_error(self, tmp_path: Path) -> None:
        """No file is the normal starting state: nothing ratified, nothing broken."""
        registry = load_policy_assertions(tmp_path)

        assert registry.assertions == ()
        assert registry.errors == ()
        assert registry.loaded is False

    def test_registry_resolves_relative_to_supplied_project_root(self, tmp_path: Path) -> None:
        """Resolution is anchored on the project root, not the process cwd."""
        _write_registry(
            tmp_path,
            [{"id": "x", "text": _EFFORT_ASSERTION, "provenance": "ratified", "reference": "ADR"}],
        )

        registry = load_policy_assertions(tmp_path)

        assert registry.path == str(tmp_path / ".forge" / "policy-assertions.yaml")
        assert len(registry.assertions) == 1

    def test_ratified_without_reference_is_demoted_to_generated(self, tmp_path: Path) -> None:
        """A ratification nobody can read is not a ratification."""
        _write_registry(tmp_path, [{"id": "x", "text": "A claim.", "provenance": "ratified"}])

        registry = load_policy_assertions(tmp_path)

        assert registry.assertions[0].provenance == PROVENANCE_GENERATED
        assert any("records no reference" in err for err in registry.errors)

    def test_unknown_provenance_value_normalises_to_generated(self, tmp_path: Path) -> None:
        _write_registry(tmp_path, [{"id": "x", "text": "A claim.", "provenance": "operator-ish"}])

        registry = load_policy_assertions(tmp_path)

        assert registry.assertions[0].provenance == PROVENANCE_GENERATED

    def test_malformed_registry_records_the_error_rather_than_looking_empty(
        self, tmp_path: Path
    ) -> None:
        path = policy_assertions_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("assertions: [oops\n", encoding="utf-8")

        registry = load_policy_assertions(tmp_path)

        assert registry.assertions == ()
        assert registry.errors


class TestClassification:
    def test_id_match_wins(self) -> None:
        registry = PolicyAssertionRegistry(assertions=(_ratified(),))

        resolved = registry.resolve(
            PolicyAssertionCitation(
                text="totally different wording", assertion_id="effort-not-scored"
            )
        )

        assert resolved.provenance == PROVENANCE_RATIFIED
        assert resolved.match_basis == MATCH_ID

    def test_exact_text_match_ignores_punctuation_and_case(self) -> None:
        registry = PolicyAssertionRegistry(assertions=(_ratified(),))

        resolved = registry.resolve(
            PolicyAssertionCitation(text="reasoning effort is intentionally NOT score controlled")
        )

        assert resolved.provenance == PROVENANCE_RATIFIED
        assert resolved.match_basis == MATCH_TEXT_EXACT

    def test_reworded_citation_of_a_ratified_assertion_still_resolves_ratified(self) -> None:
        """The text-fallback path: agents quote assertions loosely."""
        registry = PolicyAssertionRegistry(assertions=(_ratified(),))

        resolved = registry.resolve(
            PolicyAssertionCitation(
                text="The reasoning effort axis is intentionally not controlled by score."
            )
        )

        assert resolved.provenance == PROVENANCE_RATIFIED
        assert resolved.match_basis == MATCH_TEXT_SIMILAR
        assert resolved.reference == "docs/adr/0006-adaptive-router.md#clause-4"

    def test_negation_is_not_a_stopword(self) -> None:
        """An assertion and its negation must not collapse onto each other."""
        registry = PolicyAssertionRegistry(assertions=(_ratified(),))

        resolved = registry.resolve(
            PolicyAssertionCitation(text="Reasoning effort is score-controlled.")
        )

        assert resolved.provenance == PROVENANCE_GENERATED
        assert resolved.match_basis == MATCH_UNMATCHED

    def test_unmatched_citation_is_generated_not_unknown(self) -> None:
        registry = PolicyAssertionRegistry(assertions=(_ratified(),))

        resolved = registry.resolve(
            PolicyAssertionCitation(text="Sprints must never run on Tuesdays.")
        )

        assert resolved.provenance == PROVENANCE_GENERATED
        assert resolved.is_unmarked is True
        assert resolved.carries_blocking_authority is False

    def test_agent_claim_of_ratification_does_not_confer_it(self) -> None:
        """claimed_provenance is evidence for the operator, never authority."""
        registry = PolicyAssertionRegistry()

        resolved = registry.resolve(
            PolicyAssertionCitation(
                text=_EFFORT_ASSERTION,
                claimed_provenance="ratified",
                claimed_reference="ADR-0006 (allegedly)",
            )
        )

        assert resolved.provenance == PROVENANCE_GENERATED

    def test_retracted_ratified_assertion_loses_blocking_authority(self) -> None:
        registry = PolicyAssertionRegistry(
            assertions=(
                PolicyAssertion(
                    assertion_id="effort-not-scored",
                    text=_EFFORT_ASSERTION,
                    provenance=PROVENANCE_RATIFIED,
                    reference="docs/adr/0006.md",
                    retracted=True,
                    retracted_reason="contradicted by #1108",
                ),
            )
        )

        resolved = registry.resolve(PolicyAssertionCitation(text=_EFFORT_ASSERTION))

        assert resolved.carries_blocking_authority is False


class TestCitationParsing:
    def test_bare_strings_and_mappings_both_parse(self) -> None:
        citations = parse_citations(
            [
                "an assertion as a bare string",
                {"text": "an assertion as a mapping", "source": "docs/guides/x.md:12"},
            ]
        )

        assert [c.text for c in citations] == [
            "an assertion as a bare string",
            "an assertion as a mapping",
        ]
        assert citations[1].source == "docs/guides/x.md:12"

    def test_unreadable_value_yields_no_citations(self) -> None:
        assert parse_citations("not a list") == []
        assert parse_citations(None) == []


# ── Blocking adjudication ─────────────────────────────────────────────


class TestAdjudication:
    def test_generated_only_conflict_downgrades_and_records_candidates(self) -> None:
        """The #1108 shape: chartered work stopped by prose a run wrote."""
        adjudication = adjudicate_blocked_verdict(
            reason="Story contradicts an already-implemented, deliberate architectural decision.",
            blocking_basis="policy_assertion",
            citations=parse_citations(
                [{"text": _EFFORT_ASSERTION, "source": "docs/guides/routing-policy.md"}]
            ),
            registry=PolicyAssertionRegistry(),
        )

        assert adjudication.engaged is True
        assert adjudication.upheld is False
        assert adjudication.downgraded is True
        assert len(adjudication.retraction_candidates) == 1
        assert adjudication.retraction_candidates[0]["assertion"] == _EFFORT_ASSERTION
        assert len(adjudication.ratification_candidates) == 1
        assert adjudication.refusal_detail() == ""

    def test_registry_generated_conflict_is_retraction_only_not_ratification(self) -> None:
        """A recorded generated assertion has known provenance; nothing to ratify."""
        registry = PolicyAssertionRegistry(
            assertions=(
                PolicyAssertion(
                    assertion_id="effort-not-scored",
                    text=_EFFORT_ASSERTION,
                    provenance=PROVENANCE_GENERATED,
                    run_id="a1b2c3",
                ),
            )
        )

        adjudication = adjudicate_blocked_verdict(
            reason="Contradicts a standing decision.",
            blocking_basis="policy_assertion",
            citations=parse_citations([{"text": _EFFORT_ASSERTION}]),
            registry=registry,
        )

        assert adjudication.upheld is False
        assert len(adjudication.retraction_candidates) == 1
        assert adjudication.ratification_candidates == ()
        assert "authored by run a1b2c3" in adjudication.resolved[0].label()

    def test_ratified_conflict_upholds_and_names_assertion_with_class(self) -> None:
        adjudication = adjudicate_blocked_verdict(
            reason="Contradicts a ratified decision.",
            blocking_basis="policy_assertion",
            citations=parse_citations([{"text": _EFFORT_ASSERTION}]),
            registry=PolicyAssertionRegistry(assertions=(_ratified(),)),
        )

        assert adjudication.upheld is True
        detail = adjudication.refusal_detail()
        assert _EFFORT_ASSERTION in detail
        assert "ratified" in detail
        assert "docs/adr/0006-adaptive-router.md#clause-4" in detail

    def test_one_ratified_among_generated_citations_still_upholds(self) -> None:
        adjudication = adjudicate_blocked_verdict(
            reason="Contradicts two standing decisions.",
            blocking_basis="policy_assertion",
            citations=parse_citations(
                [{"text": "Something nobody recorded."}, {"text": _EFFORT_ASSERTION}]
            ),
            registry=PolicyAssertionRegistry(assertions=(_ratified(),)),
        )

        assert adjudication.upheld is True

    def test_missing_credentials_blocker_is_untouched(self) -> None:
        """Non-policy blockers must keep blocking — they carry no policy claim."""
        adjudication = adjudicate_blocked_verdict(
            reason="The deploy API key is absent and the dev agent cannot create it.",
            blocking_basis="missing_credentials",
            citations=[],
            registry=PolicyAssertionRegistry(),
        )

        assert adjudication.engaged is False
        assert adjudication.upheld is True

    def test_direct_contradiction_blocker_is_untouched(self) -> None:
        adjudication = adjudicate_blocked_verdict(
            reason="AC 2 requires the file to never be overwritten; AC 4 requires a fixed name.",
            blocking_basis="contradiction",
            citations=[],
            registry=PolicyAssertionRegistry(),
        )

        assert adjudication.engaged is False

    def test_blocker_that_merely_mentions_architecture_stays_blocked(self) -> None:
        """An incidental topic word is not a decision claim (cf. _AMBIGUITY_TOKENS)."""
        adjudication = adjudicate_blocked_verdict(
            reason=(
                "The architecture module this story extends does not exist and cannot be "
                "installed from any configured index."
            ),
            blocking_basis="missing_dependency",
            citations=[],
            registry=PolicyAssertionRegistry(),
        )

        assert adjudication.engaged is False
        assert adjudication.upheld is True

    def test_unbased_blocker_mentioning_architecture_stays_blocked(self) -> None:
        """Same, with no blocking_basis at all — the prose fallback must not fire."""
        adjudication = adjudicate_blocked_verdict(
            reason="The architecture docs reference a package that is not installable.",
            blocking_basis="",
            citations=[],
            registry=PolicyAssertionRegistry(),
        )

        assert adjudication.engaged is False

    def test_uncited_standing_decision_claim_is_adjudicated_as_unmarked(self) -> None:
        """A refusal cannot escape adjudication by omitting the citation field."""
        adjudication = adjudicate_blocked_verdict(
            reason=(
                "This contradicts an already-implemented, deliberate architectural decision: "
                "reasoning effort is intentionally not score-controlled."
            ),
            blocking_basis="",
            citations=[],
            registry=PolicyAssertionRegistry(),
        )

        assert adjudication.engaged is True
        assert adjudication.inferred_from_prose is True
        assert adjudication.upheld is False
        assert adjudication.retraction_candidates

    def test_reason_asserts_standing_decision_token_scope(self) -> None:
        assert reason_asserts_standing_decision("this is by design") is True
        assert reason_asserts_standing_decision("a standing decision covers this") is True
        assert reason_asserts_standing_decision("the architecture is complex") is False
        assert reason_asserts_standing_decision("policy modules are involved") is False

    def test_citations_alone_engage_adjudication_without_a_declared_basis(self) -> None:
        adjudication = adjudicate_blocked_verdict(
            reason="Conflicts with recorded policy.",
            blocking_basis="",
            citations=parse_citations([{"text": _EFFORT_ASSERTION}]),
            registry=PolicyAssertionRegistry(),
        )

        assert adjudication.engaged is True
        assert adjudication.upheld is False


# ── Escalation advisor rendering ──────────────────────────────────────


def _packet() -> EvidencePacket:
    return EvidencePacket(
        story_name="Wire reasoning effort to complexity score",
        issue_ref="#1108",
        issue_body="body",
        acceptance_criteria=["effort follows score"],
        cycles=[],
        reviewer_verdicts={},
        final_verdict=None,
        dev_diff="",
        test_failures="",
        escalation_reason="max cycles",
    )


_ADVISORY_WITH_ASSERTION = """
<advisory_report>
recommendation: elevate
rationale: "The story conflicts with a standing routing decision."
policy_assertions:
  - text: "Reasoning effort is intentionally not score-controlled."
    source: "docs/guides/routing-policy.md:52"
    claimed_provenance: ratified
options:
  - action: elevate
    evidence: "Every cycle re-litigated the same routing axis."
    forge_operation: "bump (route to a human design/architecture decision)"
    risk: "Adds a human round-trip."
    consequence: "A human decides the routing axis."
    policy_assertions:
      - text: "Reasoning effort is intentionally not score-controlled."
        source: "docs/guides/routing-policy.md:52"
</advisory_report>
"""


class TestAdvisoryProvenanceRendering:
    def test_unresolved_citations_default_to_generated(self) -> None:
        report = parse_advisory_report(_ADVISORY_WITH_ASSERTION)

        assert report.ok
        assert report.policy_assertions[0].provenance == PROVENANCE_GENERATED
        assert report.options[0].policy_assertions[0].provenance == PROVENANCE_GENERATED

    def test_generated_assertion_renders_as_advisory_with_follow_up(self) -> None:
        report = resolve_advisory_assertions(
            parse_advisory_report(_ADVISORY_WITH_ASSERTION), PolicyAssertionRegistry()
        )

        rendered = render_advisory_for_pending(report, _packet())

        assert "[generated]" in rendered
        assert "carries no blocking authority" in rendered
        assert "ratification candidate" in rendered

    def test_ratified_assertion_renders_with_its_reference(self) -> None:
        report = resolve_advisory_assertions(
            parse_advisory_report(_ADVISORY_WITH_ASSERTION),
            PolicyAssertionRegistry(assertions=(_ratified(),)),
        )

        rendered = render_advisory_for_pending(report, _packet())

        assert "[ratified]" in rendered
        assert "docs/adr/0006-adaptive-router.md#clause-4" in rendered
        assert "carries no blocking authority" not in rendered

    def test_report_without_assertions_renders_unchanged_shape(self) -> None:
        report = parse_advisory_report(
            _ADVISORY_WITH_ASSERTION.replace("policy_assertions", "unused_key")
        )

        rendered = render_advisory_for_pending(report, _packet())

        assert "policy assertions cited" not in rendered


# ── Shape classification (#1108 enters the sprint) ────────────────────

_ISSUE_1108_BODY = """\
## The observation

Reasoning effort is not wired to the complexity score. The routing policy guide
states this is intentional, but no operator decided it.

## Decision

Wire reasoning effort to the complexity score, retracting the rationale that
says otherwise.

## Acceptance criteria

- Reasoning effort is derived from the complexity score for every phase.
- The routing policy guide records the retraction.
"""


class TestShapeClassificationOfCharteredWork:
    def test_story_with_ac_and_a_decision_heading_is_enhancement_not_adr(self) -> None:
        """The #1108 regression: chartered work is not an unmade decision."""
        proposal = classify("Wire reasoning effort to complexity score", _ISSUE_1108_BODY, [])

        assert proposal.classification is Classification.ENHANCEMENT
        assert proposal.kept_as_todo_draft is False

    def test_explicit_adr_label_still_wins_over_acceptance_criteria(self) -> None:
        proposal = classify("Wire reasoning effort", _ISSUE_1108_BODY, ["adr-candidate"])

        assert proposal.classification is Classification.ADR_CANDIDATE

    def test_adr_marked_title_still_wins_over_acceptance_criteria(self) -> None:
        proposal = classify("ADR: routing effort axis", _ISSUE_1108_BODY, [])

        assert proposal.classification is Classification.ADR_CANDIDATE

    def test_decision_heading_without_acceptance_criteria_is_still_adr_shaped(self) -> None:
        body = "## Decision\n\nPick between blocklist and invariant enforcement.\n"

        proposal = classify("Enforcement approach", body, [])

        assert proposal.classification is Classification.ADR_CANDIDATE
