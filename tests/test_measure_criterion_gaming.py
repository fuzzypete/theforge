"""Tests for scripts/measure_criterion_gaming.py and its checked-in artifacts.

The script answers two separate questions over the diagnose audit corpus — does
a recorded fix-success criterion admit a change that removes the symptom while
leaving the confirmed cause in place, and did such a change actually land — and
renders `N / D` and `M / N` as distinct rates. Collapsing them overstates the
problem, so the counting is tested directly.

Covered here:
  - the window bounds, the explicit include-list, and the excluded-by-window count
  - every cohort clause dropping its own case into its own counter, including
    the `<dry-run:` marker and a null landing location — and never a `dry_run`
    key, which `write_diagnose_audit` does not record
  - attempt dedup: body containment wins, survives CRLF/whitespace reflow, and
    falls back to `latest_done` with the skipped run ids recorded
  - the landed-change join over real git: trailing `(#N)` stripped, and a commit
    reachable only from an unmerged feature ref NOT counted as landed
  - drift between a recorded fact and the re-derived one refusing to render
  - the checked-in report's D equalling both the adjudication row count and the
    rendered per-issue row count, so a truncated pass fails here
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "measure_criterion_gaming.py"
RECORDS = REPO_ROOT / "docs" / "plans"
ADJUDICATIONS = RECORDS / "2603-criterion-symptom-gaming.adjudications.yaml"
REPORT = RECORDS / "2603-criterion-symptom-gaming.md"

WINDOW_SINCE = "2026-09-01T00:00:00+00:00"
WINDOW_UNTIL = "2026-09-07T00:00:00+00:00"


def _load_script():
    spec = importlib.util.spec_from_file_location("measure_criterion_gaming", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mcg = _load_script()


# --------------------------------------------------------------------------
# Fixture corpora
# --------------------------------------------------------------------------


def _write_attempt(
    root: Path,
    *,
    issue: int,
    run_id: str,
    started_at: str = "2026-09-02T00:00:00+00:00",
    final_phase: str = "DONE",
    criterion: str = "the property must hold",
    cause: str = "the mechanism is missing",
    location: object = "issue #1 body updated",
    title: str = "a symptom",
) -> Path:
    audit_dir = root / ".forge" / "audits"
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / f"diagnose-issue-{issue}-{run_id}.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "kind": "diagnose",
                "run_id": run_id,
                "issue_number": issue,
                "issue_title": title,
                "started_at": started_at,
                "final_phase": final_phase,
                "landing": {"destination": "body_section", "location": location},
                "artifact": {
                    "issue_number": issue,
                    "confirmed_cause": cause,
                    "fix_success_criterion": criterion,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _cohort(root: Path, **kwargs):
    return mcg.select_cohort(
        mcg.load_attempts(root),
        since=kwargs.pop("since", WINDOW_SINCE),
        until=kwargs.pop("until", WINDOW_UNTIL),
        include_issues=kwargs.pop("include_issues", ()),
    )


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------


def test_the_window_excludes_earlier_attempts_and_counts_them(tmp_path):
    _write_attempt(tmp_path, issue=1, run_id="a", started_at="2026-09-02T00:00:00+00:00")
    _write_attempt(tmp_path, issue=2, run_id="b", started_at="2026-08-20T00:00:00+00:00")

    cohort = _cohort(tmp_path)

    assert sorted(cohort["by_issue"]) == [1]
    assert cohort["exclusions"]["excluded_by_window"] == 1
    assert cohort["excluded_by_window_issues"] == [2]


def test_the_upper_bound_excludes_attempts_after_it(tmp_path):
    _write_attempt(tmp_path, issue=1, run_id="a", started_at="2026-09-09T00:00:00+00:00")

    cohort = _cohort(tmp_path)

    assert cohort["by_issue"] == {}
    assert cohort["exclusions"]["excluded_by_window"] == 1


def test_an_issue_named_in_the_include_list_enters_despite_the_window(tmp_path):
    _write_attempt(tmp_path, issue=2595, run_id="a", started_at="2026-08-20T05:42:25+00:00")

    cohort = _cohort(tmp_path, include_issues=(2595,))

    assert sorted(cohort["by_issue"]) == [2595]
    assert cohort["exclusions"]["excluded_by_window"] == 0


# --------------------------------------------------------------------------
# Cohort clauses
# --------------------------------------------------------------------------


def test_each_cohort_clause_drops_its_case_into_its_own_counter(tmp_path):
    _write_attempt(tmp_path, issue=1, run_id="keep")
    _write_attempt(tmp_path, issue=2, run_id="failed", final_phase="FAILED")
    _write_attempt(tmp_path, issue=3, run_id="empty", criterion="   ")
    _write_attempt(tmp_path, issue=4, run_id="nolanding", location=None)
    _write_attempt(tmp_path, issue=5, run_id="dry", location="<dry-run: body_section>")
    _write_attempt(tmp_path, issue=6, run_id="path", location="/tmp/somewhere/diagnosis.md")

    cohort = _cohort(tmp_path)

    assert sorted(cohort["by_issue"]) == [1]
    assert cohort["exclusions"]["not_done"] == 1
    assert cohort["exclusions"]["empty_criterion"] == 1
    assert cohort["exclusions"]["no_landing"] == 1
    assert cohort["exclusions"]["dry_run_landing"] == 1
    assert cohort["exclusions"]["indistinguishable_landing"] == 1


def test_a_dry_run_key_is_never_consulted(tmp_path):
    """`write_diagnose_audit` records no `dry_run` key; the location is the signal."""
    path = _write_attempt(tmp_path, issue=1, run_id="a")
    data = yaml.safe_load(path.read_text())
    data["dry_run"] = True
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    cohort = _cohort(tmp_path)

    assert sorted(cohort["by_issue"]) == [1]
    source = SCRIPT.read_text(encoding="utf-8")
    assert '"dry_run"' not in source and "'dry_run'" not in source


def test_an_empty_corpus_root_fails_by_name(tmp_path):
    with pytest.raises(mcg.MeasurementError) as excinfo:
        mcg.load_attempts(tmp_path)

    assert str(tmp_path / ".forge" / "audits") in str(excinfo.value)


# --------------------------------------------------------------------------
# Attempt dedup
# --------------------------------------------------------------------------


def test_body_containment_selects_the_governing_attempt(tmp_path):
    _write_attempt(tmp_path, issue=1, run_id="old", criterion="the old property")
    _write_attempt(
        tmp_path,
        issue=1,
        run_id="new",
        started_at="2026-09-03T00:00:00+00:00",
        criterion="the new property",
    )
    attempts = _cohort(tmp_path)["by_issue"][1]

    chosen = mcg.select_attempt(attempts, "### Fix-success criterion\n\nthe old property\n")

    assert chosen["attempt"]["run_id"] == "old"
    assert chosen["selection"] == "body_contains_criterion"
    assert chosen["skipped"] == [{"run_id": "new", "reason": "criterion_not_in_issue_body"}]


def test_containment_survives_crlf_and_whitespace_reflow(tmp_path):
    _write_attempt(tmp_path, issue=1, run_id="only", criterion="the property\nmust hold")
    attempts = _cohort(tmp_path)["by_issue"][1]

    chosen = mcg.select_attempt(attempts, "prose\r\nthe property     must  hold\r\nmore prose")

    assert chosen["selection"] == "body_contains_criterion"


def test_no_containment_falls_back_to_the_latest_attempt_and_records_the_skips(tmp_path):
    _write_attempt(
        tmp_path, issue=1, run_id="first", started_at="2026-09-02T00:00:00+00:00", criterion="one"
    )
    _write_attempt(
        tmp_path, issue=1, run_id="last", started_at="2026-09-04T00:00:00+00:00", criterion="two"
    )
    attempts = _cohort(tmp_path)["by_issue"][1]

    chosen = mcg.select_attempt(attempts, "the body was reshaped and holds neither")

    assert chosen["attempt"]["run_id"] == "last"
    assert chosen["selection"] == "latest_done"
    assert chosen["skipped"] == [{"run_id": "first", "reason": "superseded_by_later_attempt"}]


def test_an_ambiguous_containment_match_falls_back_rather_than_guessing(tmp_path):
    _write_attempt(tmp_path, issue=1, run_id="a", criterion="shared text")
    _write_attempt(
        tmp_path,
        issue=1,
        run_id="b",
        started_at="2026-09-03T00:00:00+00:00",
        criterion="shared text",
    )
    attempts = _cohort(tmp_path)["by_issue"][1]

    chosen = mcg.select_attempt(attempts, "shared text")

    assert chosen["selection"] == "latest_done"


# --------------------------------------------------------------------------
# Landed-change join
# --------------------------------------------------------------------------


def test_the_join_strips_the_trailing_story_number_before_matching_titles():
    index = mcg.build_subject_index("abc123\x00fix the thing (#2944)\ndef456\x00other work")

    assert index["fix the thing"] == {"abc123"}
    assert index["other work"] == {"def456"}


def test_the_join_never_greps_for_the_issue_number():
    """`git log --grep '(#N)'` finds nothing: the trailing number is the story's."""
    source = SCRIPT.read_text(encoding="utf-8")

    assert "--grep" not in source


def test_a_unique_commit_resolves_as_the_landed_change():
    index = {"a symptom": {"sha1"}}

    assert mcg.join_landed_change("a symptom", "CLOSED", index) == {
        "landed_commit": "sha1",
        "landed_reason": None,
        "outcome": "commit_found",
    }


def test_no_commit_on_an_open_issue_is_a_proven_absence():
    result = mcg.join_landed_change("a symptom", "OPEN", {})

    assert result["outcome"] == "no_landed_change"
    assert result["landed_commit"] is None


def test_no_commit_on_a_closed_issue_is_unresolved_with_an_attributable_reason():
    result = mcg.join_landed_change("a symptom", "CLOSED", {})

    assert result["outcome"] == "unresolved"
    assert "CLOSED" in result["landed_reason"]


def test_more_than_one_matching_commit_is_unresolved():
    result = mcg.join_landed_change("a symptom", "CLOSED", {"a symptom": {"sha1", "sha2"}})

    assert result["outcome"] == "unresolved"
    assert "sha1" in result["landed_reason"] and "sha2" in result["landed_reason"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout


def test_a_commit_only_on_an_unmerged_feature_ref_is_not_a_landed_change(tmp_path):
    """`git log --all` would traverse preserved escalated worktree branches.

    This repository deliberately retains them, so a committed but never-landed
    attempt would otherwise be read as the change that landed.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("a")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "landed work (#10)")
    _git(repo, "checkout", "-b", "feat/issue-99")
    (repo / "b.txt").write_text("b")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "rejected attempt (#11)")
    _git(repo, "checkout", "main")

    index = mcg.git_subject_index(repo, ("refs/heads/main",))

    assert index["landed work"]
    assert "rejected attempt" not in index
    joined = mcg.join_landed_change("rejected attempt", "OPEN", index)
    assert joined["outcome"] == "no_landed_change"


def test_matching_no_integration_ref_at_all_is_a_named_failure(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")

    with pytest.raises(mcg.MeasurementError) as excinfo:
        mcg.git_subject_index(repo, ("refs/heads/nothing-matches-this",))

    assert "nothing-matches-this" in str(excinfo.value)


# --------------------------------------------------------------------------
# Drift and validation
# --------------------------------------------------------------------------


def _facts(**overrides) -> dict[int, dict]:
    base = {
        "run_id": "a",
        "selection": "body_contains_criterion",
        "skipped_attempts": [],
        "criterion_sha256": "hash-a",
        "criterion": "the property must hold",
        "confirmed_cause": "the mechanism is missing",
        "issue_title": "a symptom",
        "issue_state": "CLOSED",
        "issue_state_reason": "COMPLETED",
        "landed_commit": "sha1",
        "landed_reason": None,
        "join_outcome": "commit_found",
        "attempt_count": 1,
    }
    base.update(overrides)
    return {1: base}


def _row(**overrides) -> dict[int, dict]:
    base = {
        "run_id": "a",
        "selection": "body_contains_criterion",
        "criterion_sha256": "hash-a",
        "issue_title": "a symptom",
        "issue_state": "CLOSED",
        "landed_commit": "sha1",
        "admits_symptom_removing_change": True,
        "admitted_change": "skip the check for those paths",
        "admits_rationale": None,
        "landed_classification": "cause_addressing",
        "inspected_source": "sha1",
        "governance_unconfirmed": False,
        "notes": None,
    }
    base.update(overrides)
    return {1: base}


def test_a_drifted_criterion_hash_stops_the_render():
    problems = mcg.check_drift(_facts(), _row(criterion_sha256="hash-stale"))

    assert any("criterion_sha256 drifted" in p for p in problems)


def test_a_drifted_landed_sha_stops_the_render():
    problems = mcg.check_drift(_facts(), _row(landed_commit="sha-old"))

    assert any("landed_commit drifted" in p for p in problems)


def test_a_missing_adjudication_row_stops_the_render():
    problems = mcg.check_drift(_facts(), {})

    assert any("no adjudication row" in p for p in problems)


def test_an_extra_adjudication_row_stops_the_render():
    rows = _row()
    rows[2] = dict(rows[1])
    problems = mcg.check_drift(_facts(), rows)

    assert any("#2" in p and "not in the cohort" in p for p in problems)


def test_an_unadjudicated_stub_row_stops_the_render():
    problems = mcg.check_drift(_facts(), _row(admits_symptom_removing_change=None))

    assert any("unadjudicated" in p for p in problems)


def test_admitting_without_naming_the_change_stops_the_render():
    problems = mcg.check_drift(_facts(), _row(admitted_change=""))

    assert any("requires admitted_change" in p for p in problems)


def test_the_mechanical_join_constrains_the_classification():
    facts = _facts(join_outcome="no_landed_change", landed_commit=None)
    rows = _row(landed_commit=None, landed_classification="symptom_only")

    problems = mcg.cross_check_classification(facts, rows)

    assert any("proved no landed change" in p for p in problems)


def test_a_selected_run_that_is_no_longer_a_qualifying_attempt_stops_the_render(tmp_path):
    _write_attempt(tmp_path, issue=1, run_id="present")
    cohort = _cohort(tmp_path)

    with pytest.raises(mcg.MeasurementError) as excinfo:
        mcg.derive_facts(cohort, {1: {"run_id": "vanished", "selection": "latest_done"}}, {})

    assert "vanished" in str(excinfo.value)


def test_an_unknown_decision_override_is_rejected(tmp_path):
    path = tmp_path / "adj.yaml"
    path.write_text(
        yaml.safe_dump({"window": {}, "issues": {}, "decision_override": "defer"}),
        encoding="utf-8",
    )

    with pytest.raises(mcg.MeasurementError) as excinfo:
        mcg.load_adjudications(path)

    assert "decision_override" in str(excinfo.value)


# --------------------------------------------------------------------------
# Rates and decision
# --------------------------------------------------------------------------


def _rollup_rows(specs: list[tuple[bool, str, bool]]) -> tuple[dict, dict]:
    facts, rows = {}, {}
    for i, (admits, classification, unconfirmed) in enumerate(specs, start=1):
        facts[i] = _facts()[1]
        rows[i] = _row(
            admits_symptom_removing_change=admits,
            admitted_change="a change" if admits else None,
            admits_rationale=None if admits else "no such change",
            landed_classification=classification,
            governance_unconfirmed=unconfirmed,
        )[1]
    return facts, rows


def test_the_two_questions_are_counted_separately():
    facts, rows = _rollup_rows(
        [
            (True, "symptom_only", False),
            (True, "cause_addressing", False),
            (False, "cause_addressing", False),
            (False, "no_landed_change", False),
        ]
    )

    rollup = mcg.roll_up(facts, rows)

    assert (rollup["D"], rollup["N"], rollup["M"]) == (4, 2, 1)


def test_the_second_rate_is_denominated_in_n_not_in_the_resolved_subset():
    facts, rows = _rollup_rows(
        [
            (True, "symptom_only", False),
            (True, "unresolved", False),
            (True, "cause_addressing", False),
        ]
    )

    rollup = mcg.roll_up(facts, rows)

    assert rollup["N"] == 3 and rollup["M"] == 1 and rollup["U"] == 1
    assert rollup["R"] == 2  # supplemental only — never the headline denominator


def test_a_governance_unconfirmed_row_is_excluded_from_m():
    facts, rows = _rollup_rows([(True, "symptom_only", True)])

    rollup = mcg.roll_up(facts, rows)

    assert rollup["N"] == 1
    assert rollup["M"] == 0
    assert rollup["governance_unconfirmed"] == [1]


def test_the_decision_is_act_when_the_failure_mode_was_realised():
    facts, rows = _rollup_rows([(True, "symptom_only", False)])

    assert mcg.decide(mcg.roll_up(facts, rows), None)[0] == "act"


def test_the_decision_is_close_when_it_was_not():
    facts, rows = _rollup_rows([(True, "cause_addressing", False)])

    assert mcg.decide(mcg.roll_up(facts, rows), None)[0] == "close"


def test_unresolved_rows_do_not_make_a_third_outcome():
    facts, rows = _rollup_rows([(True, "unresolved", False)])

    assert mcg.decide(mcg.roll_up(facts, rows), None)[0] in mcg.DECISION_OVERRIDES


def test_an_override_replaces_the_derived_verdict():
    facts, rows = _rollup_rows([(True, "cause_addressing", False)])

    assert mcg.decide(mcg.roll_up(facts, rows), "act")[0] == "act"


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _render(specs, criteria=None):
    facts, rows = _rollup_rows(specs)
    for i, text in enumerate(criteria or [], start=1):
        facts[i] = {**facts[i], "criterion": text}
    cohort = {
        "exclusions": dict.fromkeys(
            (
                "excluded_by_window",
                "not_done",
                "empty_criterion",
                "no_landing",
                "dry_run_landing",
                "indistinguishable_landing",
            ),
            0,
        ),
        "phase_counts": {"DONE": len(specs)},
        "excluded_by_window_issues": [],
    }
    rollup = mcg.roll_up(facts, rows)
    return mcg.render_markdown(
        window={
            "since": WINDOW_SINCE,
            "until": WINDOW_UNTIL,
            "include_issues": (2595,),
            "corpus_root": "/somewhere",
            "integration_refs": ("refs/heads/main",),
            "record_date": "2026-09-06",
            "adjudications_path": "docs/plans/2603-criterion-symptom-gaming.adjudications.yaml",
        },
        cohort=cohort,
        facts=facts,
        recorded=rows,
        rollup=rollup,
        decision=mcg.decide(rollup, None),
    )


def test_the_rendered_report_states_both_rates_and_the_decision():
    rendered = _render([(True, "symptom_only", False), (False, "cause_addressing", False)])

    assert "criteria admitting a symptom-removing change:  1 / 2" in rendered
    assert "of those, where such a change actually landed: 1 / 1" in rendered
    assert "**ACT**" in rendered


def test_a_named_criterion_appears_in_the_rendered_output():
    rendered = _render(
        [(True, "cause_addressing", False)],
        criteria=["the sprint must not escalate citing artifact paths"],
    )

    assert "the sprint must not escalate citing artifact paths" in rendered


def test_an_unresolved_row_prints_the_bound_as_coverage_context_not_a_third_rate():
    rendered = _render([(True, "unresolved", False), (True, "cause_addressing", False)])

    assert "0 / 2  through  1 / 2" in rendered
    assert "**CLOSE**" in rendered


def test_a_row_that_admits_nothing_and_landed_nothing_is_not_named():
    rendered = _render([(False, "no_landed_change", False)], criteria=["a quiet criterion"])

    assert "a quiet criterion" not in rendered


# --------------------------------------------------------------------------
# The checked-in artifacts
# --------------------------------------------------------------------------


def test_the_checked_in_adjudication_file_is_complete_and_valid():
    adjudications = mcg.load_adjudications(ADJUDICATIONS)

    assert adjudications["issues"], "the adjudication file has no rows"
    for number, row in adjudications["issues"].items():
        assert not mcg._judgment_problems(number, row)


def test_the_checked_in_report_denominator_matches_its_rows():
    """A truncated adjudication pass fails here rather than shrinking D silently."""
    adjudications = mcg.load_adjudications(ADJUDICATIONS)
    report = REPORT.read_text(encoding="utf-8")
    expected = len(adjudications["issues"])

    assert f"**D = {expected}**" in report

    table_rows = [
        line for line in report.splitlines() if line.startswith("| #") and line.count("|") == 5
    ]
    assert len(table_rows) == expected


def test_the_checked_in_report_states_the_window_and_the_decision():
    report = REPORT.read_text(encoding="utf-8")

    assert WINDOW_SINCE in report
    assert "#2595" in report  # the spec's worked example is in the denominator
    assert "**CLOSE**" in report or "**ACT**" in report
