"""Integrity checks on the shipped plan-advisory judgment corpus (#2112).

These do not need the audit substrate — CI has no ``.forge/audits`` — so they
check what the file can be held to on its own: the schema every judgment must
satisfy, the controlled vocabularies, key uniqueness, and that no row is
under-evidenced without saying so.
"""

from __future__ import annotations

from theforge.plan_advisory.analysis import (
    DETECTION_POINTS,
    ESCAPED,
    EVIDENCE_UNAVAILABLE,
    FINDING_CLASSES,
    RESOLVED,
    finding_key,
)
from theforge.plan_advisory.report import load_judgments

_REQUIRED = {
    "finding_key",
    "run_id",
    "slug",
    "class",
    "advisory_outcome",
    "shipped_addressed",
    "evidence",
}


def _judgments() -> list[dict]:
    return load_judgments()["judgments"]


def test_the_corpus_is_non_empty_and_annotated() -> None:
    payload = load_judgments()
    assert payload["judgments"], "the shipped corpus must carry judgments"
    assert payload.get("notes"), "the corpus must state how it was built and what it excludes"


def test_every_judgment_carries_the_required_fields() -> None:
    for row in _judgments():
        missing = _REQUIRED - set(row)
        assert not missing, f"{row.get('finding_key')} missing {sorted(missing)}"
        assert isinstance(row["shipped_addressed"], bool)


def test_classes_and_outcomes_stay_inside_the_controlled_vocabularies() -> None:
    for row in _judgments():
        assert row["class"] in FINDING_CLASSES, row
        assert row["advisory_outcome"] in (RESOLVED, ESCAPED), row
        if row["advisory_outcome"] == ESCAPED:
            assert row.get("detection_point") in DETECTION_POINTS, row


def test_finding_keys_are_unique_and_agree_with_their_run_id() -> None:
    keys = [row["finding_key"] for row in _judgments()]
    assert len(keys) == len(set(keys))
    for row in _judgments():
        assert row["finding_key"].startswith(f"{row['run_id']}:"), row


def test_finding_keys_have_the_shape_the_extractor_produces() -> None:
    # A key the extractor could never emit would silently never match, so the
    # corpus would validate as "no orphans" while covering nothing.
    for row in _judgments():
        run_id, ordinal, digest = row["finding_key"].split(":")
        assert run_id == row["run_id"]
        assert ordinal.isdigit()
        assert len(digest) == 8
        assert finding_key(run_id, int(ordinal), "x").split(":")[:2] == [run_id, ordinal]


def test_every_judgment_cites_evidence_or_says_it_has_none() -> None:
    for row in _judgments():
        evidence = str(row["evidence"]).strip()
        assert evidence, f"{row['finding_key']} has an empty evidence field"
        if evidence == EVIDENCE_UNAVAILABLE:
            continue
        assert len(evidence) > 40, f"{row['finding_key']} evidence is too thin to check"


def test_escapes_name_where_they_were_eventually_caught() -> None:
    escapes = [r for r in _judgments() if r["advisory_outcome"] == ESCAPED]
    assert escapes, "the corpus should retain at least the known #2050 escape"
    for row in escapes:
        assert row["shipped_addressed"] is False or row.get("detection_point")
