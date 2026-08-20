"""Tests for the plan-advisory resolution measure (#2112).

Synthetic audit records and judgment rows throughout: the point is the matching,
aggregation and rendering logic, not the shipped corpus (whose own coverage is
asserted separately in :mod:`test_plan_advisory_corpus`).
"""

from __future__ import annotations

import json

import pytest

from theforge.plan_advisory.analysis import (
    CorpusMismatchError,
    analyze,
    extract_plan_findings,
    finding_key,
)
from theforge.plan_advisory.report import render


def _finding(description: str, *, severity: str = "P1-impl", disposition: str = "new") -> dict:
    return {
        "description": description,
        "severity": severity,
        "effective_severity": severity,
        "cycle_first_seen": 0,
        "cycle_last_seen": 0,
        "disposition": disposition,
    }


def _record(
    run_id: str,
    *,
    slug: str = "issue-1",
    final_phase: str = "DONE",
    findings: list[dict] | None = None,
    plan_review_usd: float | None = 2.0,
    total_usd: float | None = 10.0,
    decision: str = "approve",
) -> dict:
    return {
        "run_id": run_id,
        "task": {"slug": slug},
        "outcome": {"final_phase": final_phase},
        "cost": {"total_usd": total_usd},
        "plan_review": {
            "decision": decision,
            "cost_usd": plan_review_usd,
            "plan_finding_registry": findings if findings is not None else [_finding("a")],
        },
    }


def _judgment(record: dict, ordinal: int, **overrides) -> dict:
    registry = record["plan_review"]["plan_finding_registry"]
    p1s = [
        f
        for f in registry
        if str(f.get("effective_severity") or f.get("severity") or "").startswith("P1")
    ]
    row = {
        "finding_key": finding_key(record["run_id"], ordinal, p1s[ordinal]["description"]),
        "run_id": record["run_id"],
        "slug": record["task"]["slug"],
        "class": "module/placement",
        "advisory_outcome": "resolved",
        "shipped_addressed": True,
        "evidence": "commit abc1234 touches the omitted module",
    }
    row.update(overrides)
    return row


class TestExtraction:
    def test_only_p1_level_findings_are_extracted(self) -> None:
        record = _record(
            "r1",
            findings=[
                _finding("p1 plain", severity="P1"),
                _finding("p1 impl", severity="P1-impl"),
                _finding("p2 one", severity="P2"),
                _finding("p3 one", severity="P3"),
            ],
        )
        extraction = extract_plan_findings([record])
        assert [f.severity for f in extraction.findings] == ["P1", "P1-impl"]
        assert extraction.p2_findings_skipped == 2

    def test_effective_severity_wins_over_severity(self) -> None:
        raw = _finding("downgraded later", severity="P1-impl")
        raw["effective_severity"] = "P2"
        extraction = extract_plan_findings([_record("r1", findings=[raw])])
        assert extraction.findings == []

    def test_non_done_runs_are_excluded_but_counted(self) -> None:
        done = _record("r1", slug="issue-1")
        escalated = _record(
            "r2",
            slug="issue-2",
            final_phase="ESCALATE",
            findings=[_finding("a"), _finding("b")],
        )
        extraction = extract_plan_findings([done, escalated])
        assert [f.run_id for f in extraction.findings] == ["r1"]
        assert extraction.excluded_runs == [
            {"run_id": "r2", "slug": "issue-2", "final_phase": "ESCALATE", "p1_findings": 2}
        ]

    def test_records_without_a_plan_review_decision_are_skipped(self) -> None:
        record = _record("r1", decision="")
        assert extract_plan_findings([record]).findings == []

    def test_findings_fixed_in_plan_are_not_carried_to_dev(self) -> None:
        record = _record(
            "r1",
            findings=[_finding("kept"), _finding("regenerated away", disposition="fixed")],
        )
        extraction = extract_plan_findings([record])
        assert [f.carried_to_dev for f in extraction.findings] == [True, False]

    def test_finding_key_is_stable_under_whitespace_and_case(self) -> None:
        assert finding_key("r1", 0, "The  Broker\n blocks") == finding_key(
            "r1", 0, "the broker blocks"
        )

    def test_finding_key_separates_identical_descriptions_in_one_run(self) -> None:
        assert finding_key("r1", 0, "same") != finding_key("r1", 1, "same")


class TestCoverageValidation:
    def test_a_judgment_with_no_matching_audit_finding_fails_loudly(self) -> None:
        record = _record("r1")
        extraction = extract_plan_findings([record])
        row = _judgment(record, 0, finding_key="r1:0:deadbeef")
        with pytest.raises(CorpusMismatchError, match="no advisory finding carried into dev"):
            analyze(extraction, [row])

    def test_a_judgment_naming_a_plan_regenerated_finding_fails_loudly(self) -> None:
        record = _record("r1", findings=[_finding("gone", disposition="fixed")])
        extraction = extract_plan_findings([record])
        key = finding_key("r1", 0, "gone")
        with pytest.raises(CorpusMismatchError):
            analyze(extraction, [{**_judgment(record, 0), "finding_key": key}])

    def test_an_unjudged_audit_finding_is_counted_not_fatal(self) -> None:
        record = _record("r1", findings=[_finding("judged"), _finding("not judged")])
        extraction = extract_plan_findings([record])
        report = analyze(extraction, [_judgment(record, 0)])
        assert report["corpus"]["findings_extracted"] == 2
        assert report["corpus"]["findings_judged"] == 1
        assert report["corpus"]["findings_unjudged"] == 1
        assert report["corpus"]["coverage"] == 0.5
        # The unjudged finding is excluded from the denominator, not counted as
        # resolved: a rate that quietly covered half its corpus would mislead.
        assert report["overall"]["findings"] == 1
        assert [r["finding_key"] for r in report["unjudged_findings"]] == [
            finding_key("r1", 1, "not judged")
        ]

    def test_duplicate_judgments_fail_loudly(self) -> None:
        record = _record("r1")
        extraction = extract_plan_findings([record])
        row = _judgment(record, 0)
        with pytest.raises(CorpusMismatchError, match="judged more than once"):
            analyze(extraction, [row, dict(row)])

    def test_a_class_outside_the_vocabulary_fails_loudly(self) -> None:
        record = _record("r1")
        extraction = extract_plan_findings([record])
        with pytest.raises(CorpusMismatchError, match="controlled vocabulary"):
            analyze(extraction, [_judgment(record, 0, **{"class": "vibes"})])

    def test_an_unknown_advisory_outcome_fails_loudly(self) -> None:
        record = _record("r1")
        extraction = extract_plan_findings([record])
        with pytest.raises(CorpusMismatchError, match="resolved/escaped"):
            analyze(extraction, [_judgment(record, 0, advisory_outcome="partly")])

    def test_an_escape_with_an_unknown_detection_point_fails_loudly(self) -> None:
        record = _record("r1")
        extraction = extract_plan_findings([record])
        with pytest.raises(CorpusMismatchError, match="detection_point"):
            analyze(
                extraction,
                [
                    _judgment(
                        record,
                        0,
                        advisory_outcome="escaped",
                        shipped_addressed=False,
                        detection_point="someone noticed",
                    )
                ],
            )

    def test_missing_evidence_is_counted_rather_than_averaged_in_silently(self) -> None:
        record = _record("r1", findings=[_finding("a"), _finding("b")])
        extraction = extract_plan_findings([record])
        report = analyze(
            extraction,
            [_judgment(record, 0), _judgment(record, 1, evidence="  ")],
        )
        assert report["corpus"]["evidence_unavailable"] == 1
        assert report["resolved_findings"][1]["evidence"] == "evidence unavailable"


class TestAggregation:
    def test_class_breakout_math(self) -> None:
        record = _record(
            "r1",
            findings=[_finding("a"), _finding("b"), _finding("c"), _finding("d")],
        )
        extraction = extract_plan_findings([record])
        rows = [
            _judgment(record, 0),
            _judgment(record, 1),
            _judgment(
                record,
                2,
                **{"class": "unspecified mechanism"},
                advisory_outcome="escaped",
                shipped_addressed=False,
                detection_point="adopter run",
            ),
            _judgment(record, 3, **{"class": "unspecified mechanism"}),
        ]
        report = analyze(extraction, rows)
        by_class = {row["class"]: row for row in report["classes"]}
        assert by_class["module/placement"] == {
            "class": "module/placement",
            "findings": 2,
            "resolved": 2,
            "escaped": 0,
            "shipped_addressed": 2,
            "rate": 1.0,
        }
        assert by_class["unspecified mechanism"]["rate"] == 0.5
        assert report["overall"]["rate"] == 0.75

    def test_resolved_and_escaped_are_separately_enumerable(self) -> None:
        record = _record("r1", findings=[_finding("kept"), _finding("escaped one")])
        extraction = extract_plan_findings([record])
        report = analyze(
            extraction,
            [
                _judgment(record, 0),
                _judgment(
                    record,
                    1,
                    advisory_outcome="escaped",
                    shipped_addressed=False,
                    detection_point="own gate",
                    evidence="caught by the gate on iteration 2",
                ),
            ],
        )
        assert [r["description"] for r in report["resolved_findings"]] == ["kept"]
        escaped = report["escaped_findings"]
        assert [r["description"] for r in escaped] == ["escaped one"]
        assert escaped[0]["evidence"] == "caught by the gate on iteration 2"
        assert escaped[0]["detection_point"] == "own gate"

    def test_escapes_are_counted_by_detection_point(self) -> None:
        record = _record("r1", findings=[_finding("a"), _finding("b"), _finding("c")])
        extraction = extract_plan_findings([record])
        rows = [
            _judgment(
                record,
                i,
                advisory_outcome="escaped",
                shipped_addressed=False,
                detection_point=point,
            )
            for i, point in enumerate(("own gate", "adopter run", "own gate"))
        ]
        report = analyze(extraction, rows)
        assert report["escapes_by_detection_point"] == {"own gate": 2, "adopter run": 1}

    def test_advisory_resolution_and_shipped_status_are_reported_separately(self) -> None:
        # A finding dev never acted on whose remedy the shipped change carried
        # anyway is neither a clean resolution nor a clean escape; both readings
        # stay visible rather than collapsing into one number.
        record = _record("r1")
        extraction = extract_plan_findings([record])
        report = analyze(
            extraction,
            [
                _judgment(
                    record,
                    0,
                    advisory_outcome="escaped",
                    shipped_addressed=True,
                    detection_point="own code review",
                )
            ],
        )
        assert report["overall"]["resolved"] == 0
        assert report["overall"]["escaped"] == 1
        assert report["overall"]["shipped_addressed"] == 1
        assert report["overall"]["shipped_unaddressed"] == 0

    def test_plan_regenerated_findings_are_split_out_of_the_rate(self) -> None:
        record = _record(
            "r1",
            findings=[_finding("carried"), _finding("regenerated", disposition="fixed")],
        )
        extraction = extract_plan_findings([record])
        report = analyze(extraction, [_judgment(record, 0)])
        assert report["corpus"]["p1_findings_raised"] == 2
        assert report["corpus"]["findings_fixed_in_plan"] == 1
        assert report["corpus"]["findings_extracted"] == 1
        assert report["overall"]["rate"] == 1.0


class TestCost:
    def test_median_fraction_of_story_cost(self) -> None:
        records = [
            _record("r1", plan_review_usd=1.0, total_usd=10.0),
            _record("r2", plan_review_usd=2.0, total_usd=10.0),
            _record("r3", plan_review_usd=6.0, total_usd=10.0),
        ]
        report = analyze(extract_plan_findings(records), [])
        assert report["cost"]["median_plan_review_usd"] == 2.0
        assert report["cost"]["median_fraction_of_story"] == 0.2
        assert report["cost"]["runs_with_both"] == 3

    def test_missing_costs_are_omitted_and_accounted_not_imputed(self) -> None:
        records = [
            _record("r1", plan_review_usd=1.0, total_usd=10.0),
            _record("r2", plan_review_usd=None, total_usd=10.0),
            _record("r3", plan_review_usd=3.0, total_usd=None),
        ]
        report = analyze(extract_plan_findings(records), [])
        cost = report["cost"]
        assert cost["runs"] == 3
        assert cost["runs_with_plan_review_cost"] == 2
        assert cost["runs_with_both"] == 1
        assert cost["median_plan_review_usd"] == 2.0
        assert cost["median_fraction_of_story"] == 0.1
        assert cost["omitted_missing_plan_review_cost"] == 1
        assert cost["omitted_missing_total_cost"] == 1

    def test_a_zero_total_never_divides(self) -> None:
        report = analyze(
            extract_plan_findings([_record("r1", plan_review_usd=1.0, total_usd=0.0)]),
            [],
        )
        assert report["cost"]["median_fraction_of_story"] is None
        assert report["cost"]["omitted_missing_total_cost"] == 1


class TestRender:
    def _report(self) -> dict:
        record = _record("r1", findings=[_finding("kept"), _finding("got out")])
        extraction = extract_plan_findings([record])
        return analyze(
            extraction,
            [
                _judgment(record, 0),
                _judgment(
                    record,
                    1,
                    **{"class": "unspecified mechanism"},
                    advisory_outcome="escaped",
                    shipped_addressed=False,
                    detection_point="adopter run",
                    evidence="shipped later as #2077",
                ),
            ],
        )

    def test_render_carries_the_decision_surface(self) -> None:
        text = render(self._report())
        assert "module/placement" in text
        assert "unspecified mechanism" in text
        assert "escaped findings (1):" in text
        assert "resolved findings (1):" in text
        assert "caught at adopter run" in text
        assert "escapes by later detection point:" in text
        assert "plan review cost:" in text

    def test_verbose_renders_the_finding_text_and_its_evidence(self) -> None:
        plain = render(self._report())
        verbose = render(self._report(), verbose=True)
        assert "shipped later as #2077" not in plain
        assert "shipped later as #2077" in verbose
        assert "got out" in verbose

    def test_unshipped_escapes_do_not_read_as_caught(self) -> None:
        record = _record("r1")
        report = analyze(
            extract_plan_findings([record]),
            [
                _judgment(
                    record,
                    0,
                    advisory_outcome="escaped",
                    shipped_addressed=False,
                    detection_point="unshipped",
                )
            ],
        )
        text = render(report)
        assert "never caught (still latent)" in text
        assert "caught at unshipped" not in text

    def test_unjudged_findings_are_rendered_so_coverage_is_not_overread(self) -> None:
        record = _record("r1", findings=[_finding("judged"), _finding("not judged")])
        report = analyze(extract_plan_findings([record]), [_judgment(record, 0)])
        text = render(report)
        assert "1 judged findings of 2 extracted (coverage 50%)" in text
        assert "unjudged findings (1):" in text

    def test_empty_corpus_renders_without_error(self) -> None:
        report = analyze(extract_plan_findings([]), [])
        text = render(report)
        assert "(no judged findings)" in text
        assert "escaped findings (0):" in text


class TestCliDispatch:
    """``forge audits plan-advisory`` delegates rather than re-deriving."""

    def test_the_subcommand_is_registered(self) -> None:
        from theforge.cli.main import build_parser

        args = build_parser().parse_args(["audits", "plan-advisory", "--verbose"])
        assert args.audits_command == "plan-advisory"
        assert args.verbose is True

    def test_cmd_audits_delegates_to_the_report_module(self, tmp_path, monkeypatch, capsys):
        from theforge.cli import audits as audits_cli
        from theforge.plan_advisory import report as report_mod

        (tmp_path / "forge.yaml").write_text("project: {}\n", encoding="utf-8")
        seen: dict = {}

        def fake_load_report(project_root, **kwargs):
            seen["project_root"] = project_root
            return {"sentinel": True}

        def fake_render(report, *, verbose=False):
            seen["rendered"] = report
            seen["verbose"] = verbose
            return "RENDERED REPORT"

        monkeypatch.setattr(report_mod, "load_report", fake_load_report)
        monkeypatch.setattr(report_mod, "render", fake_render)

        class Args:
            audits_command = "plan-advisory"
            config = str(tmp_path / "forge.yaml")
            verbose = True

        assert audits_cli.cmd_audits(Args()) == 0
        assert seen["project_root"] == tmp_path
        assert seen["rendered"] == {"sentinel": True}
        assert seen["verbose"] is True
        assert "RENDERED REPORT" in capsys.readouterr().out

    def test_a_corpus_mismatch_surfaces_as_an_explicit_failure(
        self, tmp_path, monkeypatch, capsys
    ):
        from theforge.cli import audits as audits_cli
        from theforge.plan_advisory import report as report_mod
        from theforge.plan_advisory.analysis import CorpusMismatchError

        (tmp_path / "forge.yaml").write_text("project: {}\n", encoding="utf-8")

        def boom(project_root, **kwargs):
            raise CorpusMismatchError("2 judgments name no advisory finding")

        monkeypatch.setattr(report_mod, "load_report", boom)

        class Args:
            audits_command = "plan-advisory"
            config = str(tmp_path / "forge.yaml")
            verbose = False

        assert audits_cli.cmd_audits(Args()) == 1
        assert "judgment corpus unusable" in capsys.readouterr().err

    def test_load_judgments_rejects_a_malformed_corpus(self, tmp_path) -> None:
        from theforge.plan_advisory.report import load_judgments

        path = tmp_path / "judgments.json"
        path.write_text(json.dumps({"judgments": "not a list"}), encoding="utf-8")
        with pytest.raises(ValueError, match="expected an object with a 'judgments' list"):
            load_judgments(path)


class TestUnusableCorpus:
    """An unreadable corpus must reach the operator as a message, not a traceback.

    ``json.JSONDecodeError`` subclasses ``ValueError`` and a missing file raises
    ``OSError``, so all three shapes below used to escape the entry points'
    ``except`` clauses uncaught.
    """

    def _corpus(self, tmp_path, body: str | None):
        path = tmp_path / "judgments.json"
        if body is not None:
            path.write_text(body, encoding="utf-8")
        return path

    @pytest.mark.parametrize(
        "body, why",
        [
            (None, "missing file"),
            ("{not json at all", "corrupt json"),
            ('{"judgments": "not a list"}', "wrong shape"),
            ("[]", "not an object"),
        ],
    )
    def test_read_corpus_converts_every_unusable_shape(self, tmp_path, body, why) -> None:
        from theforge.plan_advisory.report import _read_corpus

        with pytest.raises(CorpusMismatchError, match="unusable"):
            _read_corpus(self._corpus(tmp_path, body))

    def test_the_module_entry_point_reports_rather_than_tracebacks(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        from theforge.plan_advisory import report as report_mod

        monkeypatch.setattr(report_mod, "open_readonly", lambda root: _FakeConn())
        corpus = self._corpus(tmp_path, "{not json at all")
        code = report_mod.main(["--project-root", str(tmp_path), "--judgments", str(corpus)])
        assert code == 2
        assert "judgment corpus unusable" in capsys.readouterr().err

    def test_the_cli_reports_rather_than_tracebacks(self, tmp_path, monkeypatch, capsys) -> None:
        from theforge.cli import audits as audits_cli
        from theforge.plan_advisory import report as report_mod

        (tmp_path / "forge.yaml").write_text("project: {}\n", encoding="utf-8")
        monkeypatch.setattr(report_mod, "open_readonly", lambda root: _FakeConn())
        monkeypatch.setattr(report_mod, "JUDGMENTS_PATH", self._corpus(tmp_path, "["))

        class Args:
            audits_command = "plan-advisory"
            config = str(tmp_path / "forge.yaml")
            verbose = False

        assert audits_cli.cmd_audits(Args()) == 1
        assert "judgment corpus unusable" in capsys.readouterr().err


class _FakeConn:
    """Stands in for the sqlite connection so no substrate file is needed."""

    def execute(self, *_args, **_kwargs):
        return iter(())

    def close(self) -> None:
        return None
