"""Prior-run knowledge receipts: the verifier (#2866).

What these tests pin is the *taxonomy of silences*. The instrument's value comes
entirely from never confusing "the agent said this claim was irrelevant" with
"the agent never mentioned it" with "no agent was asked", and every one of those
is a separate assertion below.
"""

from __future__ import annotations

import pytest

from theforge import knowledge_receipts as kr


def _manifest(phase: str, claims: list[dict], *, captured: bool = True) -> dict:
    prior: dict = {
        "enabled": True,
        "included": [{"run_id": "prior", "claims": claims}],
    }
    if captured:
        prior["claim_exposure"] = {"capture_version": 1}
    return {"phase": phase, "prior_run_context": prior}


def _claim(ref: str, text: str = "a claim about the projection rebuild path") -> dict:
    return {"claim_ref": ref, "claim": text, "run_id": "prior", "phase": "dev"}


def _submission(phase: str, entries, *, role: str = "dev", iteration: int = 1) -> dict:
    return kr.debrief_submission(
        phase=phase,
        agent_role=role,
        phase_iteration=iteration,
        source="test",
        payload=entries,
    )


class TestDispositionSet:
    def test_the_set_is_closed_and_covers_the_six_required_distinctions(self) -> None:
        assert kr.CLOSED_DISPOSITIONS == {
            "changed_decision",
            "prompted_verification",
            "confirmed_approach",
            "irrelevant",
            "already_known",
            "stale_or_wrong",
        }

    def test_only_the_two_influence_dispositions_assert_use(self) -> None:
        assert kr.USE_ASSERTING_DISPOSITIONS == {"changed_decision", "prompted_verification"}

    def test_the_schema_has_no_field_for_usefulness_or_confidence(self) -> None:
        """A satisfaction number that cannot fail is worse than no number."""
        normalized = kr.normalize_debrief(
            [
                {
                    "claim_ref": "r1",
                    "disposition": "irrelevant",
                    "did": "nothing",
                    "evidence": [],
                    "usefulness": 5,
                    "confidence": "high",
                    "counterfactual": "would have taken longer",
                }
            ]
        )
        assert set(normalized["entries"][0]) == {"claim_ref", "disposition", "did", "evidence"}


class TestMatchingAgainstExposure:
    def test_an_entry_naming_an_uninjected_claim_is_an_unmatched_citation(self) -> None:
        report = kr.build_receipt_report(
            context_manifests=[_manifest("dev", [_claim("r1")])],
            debriefs=[
                _submission(
                    "dev",
                    [
                        {"claim_ref": "r1", "disposition": "already_known", "evidence": []},
                        {"claim_ref": "nope", "disposition": "changed_decision", "evidence": []},
                    ],
                )
            ],
        )
        counts = report["counts"]
        assert counts["unmatched_citations"] == 1
        # Excluded from every count of use.
        assert counts[kr.OUTCOME_CORROBORATED_USE] == 0
        assert counts[kr.OUTCOME_UNCORROBORATED_USE] == 0

    def test_an_injected_claim_the_debrief_never_names_is_unaddressed(self) -> None:
        report = kr.build_receipt_report(
            context_manifests=[_manifest("dev", [_claim("r1"), _claim("r2", "second claim")])],
            debriefs=[
                _submission("dev", [{"claim_ref": "r1", "disposition": "irrelevant"}]),
            ],
        )
        assert report["counts"]["unaddressed_claims"] == 1
        assert [u["claim_ref"] for u in report["unaddressed"]] == ["r2"]
        # Unaddressed is not any disposition the agent could have chosen.
        assert report["counts"]["irrelevant"] == 1

    def test_a_claim_injected_into_another_phase_does_not_match_this_one(self) -> None:
        report = kr.build_receipt_report(
            context_manifests=[
                _manifest("plan", [_claim("r1")]),
                _manifest("dev", [_claim("r2", "dev claim")]),
            ],
            debriefs=[_submission("dev", [{"claim_ref": "r1", "disposition": "already_known"}])],
        )
        assert report["counts"]["unmatched_citations"] == 1
        # r1 was exposed to plan and never debriefed there; r2 was exposed to dev
        # and never debriefed either.
        assert sorted(u["claim_ref"] for u in report["unaddressed"]) == ["r1", "r2"]


class TestDispositionsOutsideTheSet:
    def test_an_unrecognised_disposition_is_recorded_and_excluded(self) -> None:
        report = kr.build_receipt_report(
            context_manifests=[_manifest("dev", [_claim("r1")])],
            debriefs=[_submission("dev", [{"claim_ref": "r1", "disposition": "somewhat_useful"}])],
        )
        assert report["counts"]["unrecognised_dispositions"] == 1
        for disposition in kr.NON_USE_DISPOSITIONS:
            assert report["counts"][disposition] == 0
        assert report["counts"][kr.OUTCOME_CORROBORATED_USE] == 0

    def test_an_unrecognised_disposition_is_never_mapped_onto_a_nearby_one(self) -> None:
        """'changed_the_decision' is not 'changed_decision'; the near miss is the point."""
        report = kr.build_receipt_report(
            context_manifests=[_manifest("dev", [_claim("r1")])],
            debriefs=[
                _submission(
                    "dev",
                    [
                        {
                            "claim_ref": "r1",
                            "disposition": "changed_the_decision",
                            "evidence": ["src/theforge/foo.py"],
                        }
                    ],
                )
            ],
            artifacts={"changed_files": ["src/theforge/foo.py"]},
        )
        assert report["counts"]["unrecognised_dispositions"] == 1
        assert report["counts"][kr.OUTCOME_CORROBORATED_USE] == 0

    def test_a_claim_with_an_unrecognised_disposition_is_not_also_unaddressed(self) -> None:
        report = kr.build_receipt_report(
            context_manifests=[_manifest("dev", [_claim("r1")])],
            debriefs=[_submission("dev", [{"claim_ref": "r1", "disposition": "???"}])],
        )
        assert report["counts"]["unaddressed_claims"] == 0


class TestEvidencePointerResolution:
    ARTIFACTS = {
        "changed_files": ["src/theforge/rebuild.py", "tests/test_rebuild.py"],
        "commits": ["abc1234 feat: rebuild path"],
        "plan_text": "approach\nstep 3\nadd the projection rebuild path",
        "plan_step_ids": ["1", "2", "3"],
    }

    @pytest.mark.parametrize(
        "pointer",
        [
            "src/theforge/rebuild.py",
            "rebuild.py",
            "plan §3",
            "plan step 3",
            "a commit touching the rebuild entry point",
            "the regression test",
        ],
    )
    def test_a_resolvable_pointer_yields_a_corroborated_use_claim(self, pointer: str) -> None:
        report = kr.build_receipt_report(
            context_manifests=[_manifest("dev", [_claim("r1")])],
            debriefs=[
                _submission(
                    "dev",
                    [
                        {
                            "claim_ref": "r1",
                            "disposition": "changed_decision",
                            "did": "added the rebuild path",
                            "evidence": [pointer],
                        }
                    ],
                )
            ],
            artifacts=self.ARTIFACTS,
        )
        assert report["counts"][kr.OUTCOME_CORROBORATED_USE] == 1
        assert report["counts"][kr.OUTCOME_UNCORROBORATED_USE] == 0

    @pytest.mark.parametrize(
        "evidence",
        [
            [],  # absent
            ["src/theforge/never_touched.py"],  # names a path this run did not change
            ["plan §9"],  # names a plan section that is not there
            ["it just felt right"],  # not a pointer at all
        ],
    )
    def test_an_absent_or_unresolvable_pointer_yields_an_uncorroborated_use_claim(
        self, evidence: list[str]
    ) -> None:
        report = kr.build_receipt_report(
            context_manifests=[_manifest("dev", [_claim("r1")])],
            debriefs=[
                _submission(
                    "dev",
                    [
                        {
                            "claim_ref": "r1",
                            "disposition": "prompted_verification",
                            "evidence": evidence,
                        }
                    ],
                )
            ],
            artifacts=self.ARTIFACTS,
        )
        assert report["counts"][kr.OUTCOME_UNCORROBORATED_USE] == 1
        assert report["counts"][kr.OUTCOME_CORROBORATED_USE] == 0

    def test_one_unresolvable_pointer_among_several_leaves_the_claim_uncorroborated(
        self,
    ) -> None:
        report = kr.build_receipt_report(
            context_manifests=[_manifest("dev", [_claim("r1")])],
            debriefs=[
                _submission(
                    "dev",
                    [
                        {
                            "claim_ref": "r1",
                            "disposition": "changed_decision",
                            "evidence": ["src/theforge/rebuild.py", "plan §9"],
                        }
                    ],
                )
            ],
            artifacts=self.ARTIFACTS,
        )
        assert report["counts"][kr.OUTCOME_UNCORROBORATED_USE] == 1

    def test_path_matching_is_on_whole_segments_not_substrings(self) -> None:
        resolved = kr.resolve_pointer(
            "orebuild.py", {"changed_files": ["src/theforge/rebuild.py"]}
        )
        assert resolved["resolved"] is False

    def test_free_text_never_decides_corroboration(self) -> None:
        """``did`` contributes to no count — including this one."""
        report = kr.build_receipt_report(
            context_manifests=[_manifest("dev", [_claim("r1")])],
            debriefs=[
                _submission(
                    "dev",
                    [
                        {
                            "claim_ref": "r1",
                            "disposition": "changed_decision",
                            "did": "src/theforge/rebuild.py plan §3 commit test",
                            "evidence": [],
                        }
                    ],
                )
            ],
            artifacts=self.ARTIFACTS,
        )
        assert report["counts"][kr.OUTCOME_UNCORROBORATED_USE] == 1

    def test_non_use_dispositions_need_no_pointer(self) -> None:
        report = kr.build_receipt_report(
            context_manifests=[_manifest("dev", [_claim("r1")])],
            debriefs=[_submission("dev", [{"claim_ref": "r1", "disposition": "stale_or_wrong"}])],
            artifacts=self.ARTIFACTS,
        )
        assert report["counts"]["stale_or_wrong"] == 1
        assert report["counts"][kr.OUTCOME_UNCORROBORATED_USE] == 0


class TestPhaseStatuses:
    def test_a_phase_that_received_nothing_has_nothing_to_debrief(self) -> None:
        report = kr.build_receipt_report(
            context_manifests=[_manifest("preflight", [])],
            debriefs=[],
        )
        phase = report["phases"][0]
        assert phase["status"] == kr.PHASE_NOTHING_TO_DEBRIEF
        assert report["counts"]["phases_undebriefed"] == 0
        assert report["counts"]["phases_with_injected_knowledge"] == 0

    def test_a_phase_that_received_claims_and_returned_nothing_is_undebriefed(self) -> None:
        report = kr.build_receipt_report(
            context_manifests=[_manifest("dev", [_claim("r1")])],
            debriefs=[_submission("dev", None)],
        )
        phase = report["phases"][0]
        assert phase["status"] == kr.PHASE_UNDEBRIEFED
        assert report["counts"]["phases_undebriefed"] == 1
        assert report["counts"]["unaddressed_claims"] == 1

    def test_an_empty_debrief_list_is_debriefed_with_every_claim_unaddressed(self) -> None:
        """Answering "none of them" is an answer; silence is not."""
        report = kr.build_receipt_report(
            context_manifests=[_manifest("dev", [_claim("r1")])],
            debriefs=[_submission("dev", [])],
        )
        assert report["phases"][0]["status"] == kr.PHASE_DEBRIEFED
        assert report["counts"]["unaddressed_claims"] == 1

    def test_a_malformed_debrief_is_recorded_as_malformed_not_as_a_disposition(self) -> None:
        report = kr.build_receipt_report(
            context_manifests=[_manifest("dev", [_claim("r1")])],
            debriefs=[_submission("dev", "not a list at all")],
        )
        phase = report["phases"][0]
        assert phase["status"] == kr.PHASE_UNDEBRIEFED
        assert phase["malformed_submissions"][0]["reason"].startswith("not_a_list")
        assert sum(report["counts"][d] for d in kr.NON_USE_DISPOSITIONS) == 0

    def test_a_run_predating_capture_is_uncomparable_not_zero(self) -> None:
        report = kr.build_receipt_report(
            context_manifests=[_manifest("dev", [_claim("r1")], captured=False)],
            debriefs=[],
        )
        assert report["status"] == kr.STATUS_UNCOMPARABLE
        assert report["counts"] is None


class TestMultipleReviewersInOnePhase:
    def test_each_claim_contributes_exactly_one_counted_disposition(self) -> None:
        """A pool exposes one claim set; three reviewers must not triple it."""
        claims = [_claim("r1"), _claim("r2", "second claim")]
        report = kr.build_receipt_report(
            context_manifests=[_manifest("review", claims), _manifest("review", claims)],
            debriefs=[
                _submission(
                    "review",
                    [
                        {"claim_ref": "r1", "disposition": "already_known"},
                        {"claim_ref": "r2", "disposition": "irrelevant"},
                    ],
                    role="reviewer-a",
                ),
                _submission(
                    "review",
                    [
                        {"claim_ref": "r1", "disposition": "already_known"},
                        {"claim_ref": "r2", "disposition": "irrelevant"},
                    ],
                    role="reviewer-b",
                ),
            ],
        )
        counts = report["counts"]
        assert counts["claims_injected"] == 2
        assert counts["already_known"] == 1
        assert counts["irrelevant"] == 1
        # Both raw debriefs remain visible.
        assert len(report["entries"]) == 4

    def test_a_claim_cited_by_only_one_reviewer_is_neither_unaddressed_nor_unmatched(
        self,
    ) -> None:
        claims = [_claim("r1")]
        report = kr.build_receipt_report(
            context_manifests=[_manifest("review", claims), _manifest("review", claims)],
            debriefs=[
                _submission("review", [], role="reviewer-a"),
                _submission(
                    "review",
                    [{"claim_ref": "r1", "disposition": "confirmed_approach"}],
                    role="reviewer-b",
                ),
            ],
        )
        counts = report["counts"]
        assert counts["unaddressed_claims"] == 0
        assert counts["unmatched_citations"] == 0
        assert counts["confirmed_approach"] == 1

    def test_conflicting_dispositions_never_resolve_into_a_use_count(self) -> None:
        claims = [_claim("r1")]
        report = kr.build_receipt_report(
            context_manifests=[_manifest("review", claims)],
            debriefs=[
                _submission(
                    "review",
                    [
                        {
                            "claim_ref": "r1",
                            "disposition": "changed_decision",
                            "evidence": ["src/a.py"],
                        }
                    ],
                    role="reviewer-a",
                ),
                _submission(
                    "review",
                    [{"claim_ref": "r1", "disposition": "irrelevant"}],
                    role="reviewer-b",
                ),
            ],
            artifacts={"changed_files": ["src/a.py"]},
        )
        counts = report["counts"]
        assert counts["irrelevant"] == 1
        assert counts[kr.OUTCOME_CORROBORATED_USE] == 0

    def test_the_collapse_is_deterministic_regardless_of_submission_order(self) -> None:
        claims = [_claim("r1")]
        a = _submission(
            "review", [{"claim_ref": "r1", "disposition": "already_known"}], role="reviewer-a"
        )
        b = _submission(
            "review", [{"claim_ref": "r1", "disposition": "confirmed_approach"}], role="reviewer-b"
        )
        forward = kr.build_receipt_report(
            context_manifests=[_manifest("review", claims)], debriefs=[a, b]
        )
        reverse = kr.build_receipt_report(
            context_manifests=[_manifest("review", claims)], debriefs=[b, a]
        )
        assert forward["counts"] == reverse["counts"]


class TestSpecWorkedExample:
    def test_the_story_example_produces_the_stated_result(self) -> None:
        report = kr.build_receipt_report(
            context_manifests=[
                _manifest(
                    "dev",
                    [
                        _claim("7f3a", "rebuild the projection before implementing"),
                        _claim("c91e", "the approach already taken"),
                        _claim("b204", "behaviour replaced in the current release"),
                    ],
                )
            ],
            debriefs=[
                _submission(
                    "dev",
                    [
                        {
                            "claim_ref": "7f3a",
                            "disposition": "changed_decision",
                            "did": "added the projection rebuild path to the plan",
                            "evidence": ["plan §3", "a commit touching src/rebuild.py"],
                        },
                        {
                            "claim_ref": "c91e",
                            "disposition": "already_known",
                            "did": "matched the approach I had already taken",
                        },
                        {
                            "claim_ref": "b204",
                            "disposition": "stale_or_wrong",
                            "did": "the behaviour it describes was replaced",
                        },
                    ],
                )
            ],
            artifacts={
                "changed_files": ["src/rebuild.py"],
                "plan_text": "approach\nstep 3 rebuild",
                "plan_step_ids": ["3"],
                "commits": ["abc1234"],
            },
        )
        counts = report["counts"]
        assert counts[kr.OUTCOME_CORROBORATED_USE] == 1
        assert counts["already_known"] == 1
        assert counts["stale_or_wrong"] == 1
        assert counts["unaddressed_claims"] == 0
        assert counts["unmatched_citations"] == 0


class TestExtraction:
    def test_a_top_level_debrief_is_read_out_of_bare_yaml(self) -> None:
        payload = kr.extract_knowledge_debrief(
            "plan:\n  approach: x\nknowledge_debrief:\n  - claim_ref: r1\n"
        )
        assert payload == [{"claim_ref": "r1"}]

    def test_a_top_level_debrief_is_read_out_of_a_fenced_block(self) -> None:
        payload = kr.extract_knowledge_debrief(
            "prose\n```yaml\nverdict: APPROVE\nknowledge_debrief:\n  - claim_ref: r1\n```\n"
        )
        assert payload == [{"claim_ref": "r1"}]

    def test_unparseable_output_yields_none_rather_than_raising(self) -> None:
        assert kr.extract_knowledge_debrief("::: not yaml [ }") is None
        assert kr.extract_knowledge_debrief("") is None

    def test_artifacts_are_read_from_the_record_not_the_working_tree(self) -> None:
        artifacts = kr.artifacts_from_record(
            {
                "changed_files": {"files": [{"path": "src/a.py"}, {"path": "tests/test_a.py"}]},
                "dev_handoffs": [{"handoff": {"commits": [{"sha": "abc", "message": "feat: a"}]}}],
                "phases": {
                    "plan": {
                        "plan_structured": {
                            "approach": "do the thing",
                            "steps": [{"id": 1, "description": "first", "details": "detail"}],
                        }
                    }
                },
            }
        )
        assert artifacts["changed_files"] == ["src/a.py", "tests/test_a.py"]
        assert artifacts["commits"] == ["abc feat: a"]
        assert artifacts["plan_step_ids"] == ["1"]
        assert "do the thing" in artifacts["plan_text"]
