"""Tests for the escalation advisor: taxonomy, packet, and report schema boundary.

Covers the pure-data module (``theforge.escalation_advisor``) plus the
evidence-packet builder in ``escalation_advisor_flow``. The fresh-context agent
invocation is exercised in ``test_coord_escalation_advisor.py``; here we validate
the deterministic pipeline: packet assembly → schema validation → rendering, and
the #1365 worked-example check that a well-formed report surfaces the
blocklist-vs-invariant framing as a Redirect/Elevate option.
"""

from __future__ import annotations

from pathlib import Path

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.escalation_advisor_flow import (
    _extract_acceptance_criteria,
    build_evidence_packet,
)
from theforge.coordinator.state import CoordinatorState, CycleHistory
from theforge.escalation_advisor import (
    ACTION_TAXONOMY,
    AdvisoryReport,
    EvidencePacket,
    action_disposition,
    parse_advisory_report,
    render_advisory_for_pending,
)
from theforge.review import ReviewFinding, ReviewResult
from theforge.task import TaskStory


def _config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
            base_branch="main",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        preflight_fallback_profile=None,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        log=LogConfig(enabled=False),
    )


def _finding(desc: str) -> ReviewFinding:
    return ReviewFinding(
        severity="P1",
        file="src/guard.py",
        line=10,
        observed=desc,
        suggestion=None,
    )


def _review(verdict: str, summary: str, findings: list[str]) -> ReviewResult:
    return ReviewResult(
        verdict=verdict,
        summary=summary,
        findings=[_finding(f) for f in findings],
        story_matches=False,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=[],
        raw_yaml={},
    )


# ── Taxonomy + dispositions ───────────────────────────────────────────────────


class TestTaxonomy:
    def test_taxonomy_values(self):
        assert ACTION_TAXONOMY == (
            "accept",
            "land_core_defer_edges",
            "redirect",
            "decompose",
            "elevate",
            "defer_or_abandon",
        )

    def test_dispositions(self):
        assert action_disposition("accept") == "approve"
        assert action_disposition("defer_or_abandon") == "reject"
        for named in ("land_core_defer_edges", "redirect", "decompose", "elevate"):
            assert action_disposition(named) == "named"

    def test_unknown_action_fails_closed_to_reject(self):
        assert action_disposition("merge_it_all") == "reject"
        assert action_disposition("") == "reject"


# ── Schema boundary ───────────────────────────────────────────────────────────


class TestParseAdvisoryReport:
    def _valid_block(self, recommendation: str = "redirect") -> str:
        return f"""
Prose the advisor may emit before the block is ignored.
<advisory_report>
recommendation: {recommendation}
rationale: The blocklist cannot be complete; the issue named an end-state invariant.
options:
  - action: redirect
    evidence: Cycles 1-5 each found a new git-guard bypass.
    forge_operation: re-run with constraint
    risk: The corrected framing may still be under-specified.
    consequence: Dev re-runs against the end-state invariant.
  - action: elevate
    evidence: The blocklist-vs-invariant choice is an unmade architecture decision.
    forge_operation: bump
    risk: Adds a human round-trip.
    consequence: A human picks the enforcement architecture.
</advisory_report>
"""

    def test_valid_report_parses(self):
        report = parse_advisory_report(self._valid_block())
        assert report.ok
        assert report.recommendation == "redirect"
        assert {o.action for o in report.options} == {"redirect", "elevate"}
        rec = report.recommended_option()
        assert rec is not None and rec.action == "redirect"

    def test_missing_block_is_error(self):
        report = parse_advisory_report("no structured output here")
        assert not report.ok
        assert any("no <advisory_report>" in e for e in report.parse_errors)

    def test_recommendation_outside_taxonomy_rejected(self):
        block = self._valid_block(recommendation="ship_it")
        report = parse_advisory_report(block)
        assert not report.ok
        assert any("recommendation must be one of" in e for e in report.parse_errors)

    def test_recommendation_must_be_among_options(self):
        block = """
<advisory_report>
recommendation: decompose
options:
  - action: accept
    evidence: e
    risk: r
    consequence: c
</advisory_report>
"""
        report = parse_advisory_report(block)
        assert not report.ok
        assert any("not present among the options" in e for e in report.parse_errors)

    def test_option_missing_required_field_rejected(self):
        block = """
<advisory_report>
recommendation: accept
options:
  - action: accept
    evidence: e
    risk: r
</advisory_report>
"""
        report = parse_advisory_report(block)
        assert not report.ok
        assert any("missing consequence" in e for e in report.parse_errors)

    def test_option_action_outside_taxonomy_rejected(self):
        block = """
<advisory_report>
recommendation: accept
options:
  - action: accept
    evidence: e
    risk: r
    consequence: c
  - action: rewrite_everything
    evidence: e
    risk: r
    consequence: c
</advisory_report>
"""
        report = parse_advisory_report(block)
        assert not report.ok
        assert any("action must be one of" in e for e in report.parse_errors)

    def test_malformed_yaml_is_error_not_exception(self):
        block = "<advisory_report>\nrecommendation: : : {[}\n</advisory_report>"
        report = parse_advisory_report(block)
        assert not report.ok


# ── Rendering ─────────────────────────────────────────────────────────────────


class TestRendering:
    def test_render_lists_all_options_and_marks_recommended(self):
        report = parse_advisory_report(
            """
<advisory_report>
recommendation: elevate
rationale: unmade design decision
options:
  - action: redirect
    evidence: churn evidence
    forge_operation: re-run with constraint
    risk: rr
    consequence: cc
  - action: elevate
    evidence: architecture decision
    forge_operation: bump
    risk: rr2
    consequence: cc2
</advisory_report>
"""
        )
        packet = EvidencePacket(
            story_name="git-guard",
            issue_ref="#1365",
            issue_body="body",
            acceptance_criteria=["ac1"],
            cycles=[],
            reviewer_verdicts={},
            final_verdict="REQUEST_CHANGES",
            dev_diff="",
            test_failures="",
            escalation_reason="max cycles exhausted",
        )
        text = render_advisory_for_pending(report, packet)
        assert "RECOMMENDED ACTION: Elevate" in text
        assert "Redirect" in text and "Elevate" in text
        assert "← recommended" in text
        assert "max cycles exhausted" in text


# ── Evidence packet builder ───────────────────────────────────────────────────


class TestBuildEvidencePacket:
    def test_extract_acceptance_criteria(self):
        body = (
            "# Story\n\nSome intro.\n\n"
            "## Acceptance criteria\n\n"
            "- first observable outcome\n"
            "- second observable outcome\n\n"
            "## Notes\n\n- not an AC\n"
        )
        acs = _extract_acceptance_criteria(body)
        assert acs == ["first observable outcome", "second observable outcome"]

    def test_packet_pulls_cycle_history_and_verdicts(self, tmp_path):
        config = _config(tmp_path)
        task = TaskStory(name="Guard", slug="issue-1365", github_issue=1365)
        state = CoordinatorState()
        state.story_content = (
            "Enforce the git guard.\n\n## Acceptance criteria\n\n- guard cannot be bypassed\n"
        )
        state.cycle_history = [
            CycleHistory(
                cycle=1,
                verdict="REQUEST_CHANGES",
                summary="missing pull",
                p1_findings=["pull bypass"],
            ),
            CycleHistory(
                cycle=2,
                verdict="REQUEST_CHANGES",
                summary="force-push",
                p1_findings=["+refspec bypass"],
            ),
        ]
        state.review_results = [_review("REQUEST_CHANGES", "still bypassable", ["alias bypass"])]
        state.last_cycle_reviewer_results = [("reviewer-a", state.review_results[-1])]
        state.error = "Review requested changes after 5 cycles. Max cycles (5) exhausted."

        packet = build_evidence_packet(state, task, config, tmp_path / "nonexistent")
        assert packet.issue_ref == "#1365"
        assert packet.acceptance_criteria == ["guard cannot be bypassed"]
        assert [c.cycle for c in packet.cycles] == [1, 2]
        assert packet.reviewer_verdicts == {"reviewer-a": "REQUEST_CHANGES"}
        assert "Max cycles" in packet.escalation_reason
        # Serialisable for the audit trail.
        assert packet.to_dict()["issue_ref"] == "#1365"

    def test_packet_falls_back_to_review_results_when_no_cycle_history(self, tmp_path):
        config = _config(tmp_path)
        task = TaskStory(name="Guard", slug="issue-1365", github_issue=1365)
        state = CoordinatorState()
        state.story_content = "body"
        state.review_results = [
            _review("REQUEST_CHANGES", "cycle one", ["f1"]),
            _review("REQUEST_CHANGES", "cycle two", ["f2"]),
        ]
        packet = build_evidence_packet(state, task, config, tmp_path / "nope")
        assert [c.cycle for c in packet.cycles] == [1, 2]
        assert packet.cycles[0].findings == ["f1"]


# ── Worked-example check (AC 5) ───────────────────────────────────────────────


class TestWorkedExample1365:
    """Given #1365's evidence packet, a well-formed advisory surfaces the
    invocation-blocklist-vs-end-state-invariant framing as Redirect or Elevate
    with cited evidence."""

    def _packet_1365(self) -> EvidencePacket:
        bypasses = [
            "missing `pull` in the git-guard shim",
            "`+refspec` force-push bypass",
            "inline alias bypass",
            "case-varied alias bypass",
            "`-C` worktree alias bypass",
        ]
        from theforge.escalation_advisor import CycleEvidence

        cycles = [
            CycleEvidence(
                cycle=i + 1,
                verdict="REQUEST_CHANGES",
                summary=f"reviewer found {b}",
                findings=[b],
            )
            for i, b in enumerate(bypasses)
        ]
        return EvidencePacket(
            story_name="git-guard end-state invariant",
            issue_ref="#1365",
            issue_body=(
                "Enforce that the dev phase cannot mutate git history. The issue "
                "specifies an end-state invariant at the dev-phase boundary."
            ),
            acceptance_criteria=["the dev phase boundary enforces an end-state invariant"],
            cycles=cycles,
            reviewer_verdicts={"reviewer-a": "REQUEST_CHANGES"},
            final_verdict="REQUEST_CHANGES",
            dev_diff="a git-subcommand blocklist shim",
            test_failures="",
            escalation_reason="Review requested changes after 5 cycles. Max cycles (5) exhausted.",
        )

    def test_redirect_or_elevate_surfaced_with_cited_evidence(self):
        # A representative advisor output for the #1365 packet. This validates the
        # end-to-end plumbing/schema deterministically (the LLM quality itself is
        # not asserted here — only that a well-formed report routes to the right
        # taxonomy actions with evidence citing the packet).
        advisor_output = """
<advisory_report>
recommendation: redirect
rationale: >-
  Five cycles each found a different bypass of a git-subcommand blocklist; the
  enforcement approach cannot be complete, while the issue already named a
  winnable end-state invariant.
options:
  - action: redirect
    evidence: >-
      Cycles 1-5 each surfaced a new bypass (missing pull, +refspec force-push,
      inline/case-varied/-C alias variants) — whack-a-mole on an unbounded space.
    forge_operation: re-run with constraint (enforce the end-state invariant)
    risk: The invariant framing must be specified precisely or dev drifts again.
    consequence: Dev re-runs against the end-state invariant, not the blocklist.
  - action: elevate
    evidence: >-
      Choosing an end-state invariant over a subcommand blocklist is an unmade
      architecture decision the issue implies but never fixed.
    forge_operation: bump (route the enforcement-architecture choice to a human)
    risk: Adds a human round-trip before code changes.
    consequence: A human fixes the enforcement architecture; the story re-enters.
</advisory_report>
"""
        report = parse_advisory_report(advisor_output)
        assert report.ok
        assert report.recommendation in ("redirect", "elevate")
        actions = {o.action for o in report.options}
        assert "redirect" in actions or "elevate" in actions
        rec_opt = report.recommended_option()
        assert rec_opt is not None
        # Evidence cites the churn pattern from the packet.
        assert "bypass" in rec_opt.evidence.lower() or "invariant" in rec_opt.evidence.lower()

    def test_report_is_advisory_report_dataclass(self):
        report = AdvisoryReport(
            recommendation="elevate",
            rationale="x",
            options=[],
            parse_errors=[],
        )
        assert report.ok

    def test_prompt_construction_exercises_1365_packet(self):
        # Exercise the #1365 evidence packet through prompt construction (not just
        # a prewritten report): the fresh-advisor prompt must carry the full
        # taxonomy AND the per-cycle churn signal so a fresh model can surface the
        # blocklist-vs-invariant framing.
        from theforge.task.advisor_prompts import build_advisor_prompt

        packet = self._packet_1365()
        prompt = build_advisor_prompt(packet)

        # The constrained taxonomy is presented to the advisor.
        for action in ACTION_TAXONOMY:
            assert f"`{action}`" in prompt
        # The churn pattern (each cycle's bypass finding) is in the packet section.
        assert "force-push" in prompt
        assert "case-varied alias" in prompt
        assert "Cycle 5" in prompt
        # The end-state invariant framing from the issue body is present.
        assert "end-state invariant" in prompt
        # The required constrained-output contract is spelled out.
        assert "recommendation" in prompt
        assert "<advisory_report>" in prompt


# ── Topology-walk evidence (#2372) ────────────────────────────────────────────


_TOPOLOGY_SIGNAL = {
    "pattern": "topology_walk",
    "seed_anchor": "unpriced_dispatch",
    "cycles": [1, 2, 3],
    "trajectory_cycle": 3,
    "review_cycle": 3,
    "sequence": [
        {
            "cycle": 1,
            "file": "src/routing/dispatch.py",
            "line": 10,
            "description": "unpriced_dispatch: seated primaries",
        },
        {
            "cycle": 2,
            "file": "src/routing/fallback.py",
            "line": 22,
            "description": "unpriced_dispatch: fallback_models",
        },
        {
            "cycle": 3,
            "file": "src/routing/transport.py",
            "line": 44,
            "description": "unpriced_dispatch: transport_fallback",
        },
    ],
    "rationale": "each cycle resolved its predecessor and found a new sibling",
}


class TestTopologySignalReachesTheAdvisor:
    """A pre-ceiling escalation must tell the advisor WHY it happened early.

    Without the signal the advisor sees a short cycle history and a story that
    stopped for no stated reason — the very framing question it exists to answer
    would be invisible.
    """

    def test_packet_carries_the_signal_from_state(self, tmp_path):
        config = _config(tmp_path)
        task = TaskStory(name="Pricing", slug="issue-2372", github_issue=2372)
        state = CoordinatorState()
        state.story_content = "body\n\n## Acceptance criteria\n\n- every dispatch is priced\n"
        state.review_results = [_review("REQUEST_CHANGES", "still unpriced", ["transport"])]
        state.review_topology_signal = dict(_TOPOLOGY_SIGNAL)

        packet = build_evidence_packet(state, task, config, tmp_path / "nonexistent")
        assert packet.topology_signal is not None
        assert packet.topology_signal["seed_anchor"] == "unpriced_dispatch"
        # Serialised for the audit trail alongside the rest of the packet.
        assert packet.to_dict()["topology_signal"]["cycles"] == [1, 2, 3]

    def test_packet_signal_is_none_when_no_walk_was_detected(self, tmp_path):
        config = _config(tmp_path)
        task = TaskStory(name="Pricing", slug="issue-2372", github_issue=2372)
        state = CoordinatorState()
        state.story_content = "body"
        packet = build_evidence_packet(state, task, config, tmp_path / "nope")
        assert packet.topology_signal is None
        assert packet.to_dict()["topology_signal"] is None

    def _packet(self, signal):
        from theforge.escalation_advisor import CycleEvidence

        return EvidencePacket(
            story_name="price every dispatch",
            issue_ref="#2372",
            issue_body="Every dispatch path must be priced.",
            acceptance_criteria=["every dispatch is priced"],
            cycles=[
                CycleEvidence(
                    cycle=i,
                    verdict="REQUEST_CHANGES",
                    summary=f"cycle {i}",
                    findings=[f"finding {i}"],
                )
                for i in (1, 2, 3)
            ],
            reviewer_verdicts={"reviewer-a": "REQUEST_CHANGES"},
            final_verdict="REQUEST_CHANGES",
            dev_diff="",
            test_failures="",
            escalation_reason="Topology walk detected at review cycle 3 of 5.",
            topology_signal=signal,
        )

    def test_prompt_renders_the_detected_pattern_and_its_sequence(self):
        from theforge.task.advisor_prompts import build_advisor_prompt

        prompt = build_advisor_prompt(self._packet(dict(_TOPOLOGY_SIGNAL)))

        assert "TOPOLOGY WALK" in prompt
        assert "unpriced_dispatch" in prompt
        # Every location in the sequence is named, so the advisor reasons about
        # the surface being inventoried rather than the latest finding.
        assert "src/routing/dispatch.py:10" in prompt
        assert "src/routing/fallback.py:22" in prompt
        assert "src/routing/transport.py:44" in prompt
        # The cycle history section is intact — this evidence is additive.
        assert "Review cycle history" in prompt
        assert "Cycle 3" in prompt

    def test_prompt_does_not_claim_cycles_were_exhausted_on_an_early_escalation(self):
        from theforge.task.advisor_prompts import build_advisor_prompt

        early = build_advisor_prompt(self._packet(dict(_TOPOLOGY_SIGNAL)))
        # The false premise — that the budget ran out — must not be stated.
        assert "the review cycles were exhausted" not in early
        assert "BEFORE its review cycles were exhausted" in early
        assert "Review cycles remain available" in early

        # The ceiling-triggered escalation keeps its original framing.
        exhausted = build_advisor_prompt(self._packet(None))
        assert "the review cycles were exhausted" in exhausted
        assert "TOPOLOGY WALK" not in exhausted
