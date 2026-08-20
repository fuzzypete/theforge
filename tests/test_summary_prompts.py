"""Tests for issue #2606: the summary-generation prompt must discourage filler.

A clean, low-friction run has nothing to teach, but the prompt gave the model
no explicit criterion for that and no permission to say so — so it padded
`what_was_learned` with restatements of the diff. These tests pin the prompt
text that fixes that: a disqualification rule for restatement claims,
permission to return an empty list, an instruction to order claims
strongest-first, and a requirement that evidence descriptions explain the
causal mechanism rather than mere involvement.
"""

from __future__ import annotations

from theforge.knowledge_summary import RunAnchors
from theforge.task.summary_prompts import build_run_summary_prompt

RUN_ID = "run-abc123"


def _audit() -> dict:
    return {
        "run_id": RUN_ID,
        "task": {"name": "Retry the client", "slug": "retry-client", "github_issue": 42},
        "preflight": {"work_type": "feature", "complexity": "small", "complexity_score": 2},
        "iterations": {"dev_iterations_productive": 1, "review_cycles_total": 1},
        "cost": {"total_usd": 1.0},
        "plan_review": {"regenerated": False},
        "changed_files": {"base_ref": "aaa", "head_ref": "bbb", "files": []},
        "finding_registry": [],
        "reviews": [],
        "phases": {},
    }


def _anchors() -> RunAnchors:
    return RunAnchors(
        finding_ids=frozenset({"f-1"}),
        plan_step_ids=frozenset({"1"}),
        review_cycles=frozenset({"1"}),
        file_paths=frozenset({"src/x.py"}),
        diff_refs=frozenset({"aaa", "bbb"}),
    )


def test_prompt_disqualifies_restatement_claims():
    prompt = build_run_summary_prompt(_audit(), _anchors())
    lower = prompt.lower()
    assert "restating what changed" in lower
    assert "do not include it" in lower


def test_prompt_permits_empty_what_was_learned():
    prompt = build_run_summary_prompt(_audit(), _anchors())
    lower = prompt.lower()
    assert "empty list is a correct" in lower
    assert "manufacture claims" in lower


def test_prompt_requires_strongest_first_ordering():
    prompt = build_run_summary_prompt(_audit(), _anchors())
    assert "strongest-first" in prompt.lower()


def test_prompt_requires_causal_evidence_descriptions():
    prompt = build_run_summary_prompt(_audit(), _anchors())
    lower = prompt.lower()
    assert "causal mechanism" in lower
    assert "not just that it was involved" in lower
