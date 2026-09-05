"""Tests for the shared story-run artifact attribution (#2775).

The predicate answers one question two subsystems ask: is every path in this
porcelain status block something forge itself wrote as a consequence of running?
``sprint`` asks so a publisher knows what is pending; ``coordinator`` asks so a
landing precondition does not refuse a sprint's own stories over bookkeeping no
operator can reconcile. Both must get the same answer, which is why the
predicate lives below both and is tested here rather than at either call site.
"""

from __future__ import annotations

from theforge.coordinator.landing_evidence import LANDING_EVIDENCE_RELPATH
from theforge.story_run_artifacts import (
    STORY_RUN_ARTIFACT_DIRS,
    porcelain_paths,
    story_run_artifact_dirt_only,
)


class TestStoryRunArtifactDirs:
    def test_covers_the_three_trees_the_refusals_named(self):
        """The exact directories quoted in the reported refusals."""
        assert set(STORY_RUN_ARTIFACT_DIRS) == {
            ".forge/audits/runs",
            ".forge/audits/landing",
            ".forge/knowledge/summaries",
        }

    def test_landing_evidence_tree_is_not_a_second_literal(self):
        """Derived from the constant that owns it, so the two cannot drift."""
        assert "/".join(LANDING_EVIDENCE_RELPATH) in STORY_RUN_ARTIFACT_DIRS


class TestPorcelainPaths:
    def test_reads_untracked_and_modified_entries(self):
        status = "?? .forge/audits/runs/abc.json\n M forge.yaml"
        assert porcelain_paths(status) == [".forge/audits/runs/abc.json", "forge.yaml"]

    def test_stripped_worktree_only_status_keeps_its_whole_path(self):
        """``_run_shell`` strips the block, so a fixed column slice ate a char.

        The first entry arrives as ``M path`` rather than ``" M path"``, which
        is why this splits on whitespace instead of slicing (#2598).
        """
        assert porcelain_paths("M .forge/audits/runs/abc.json") == [".forge/audits/runs/abc.json"]

    def test_rename_reports_the_destination(self):
        """What is on disk now is what a landing has to reckon with."""
        assert porcelain_paths("R  old.txt -> new.txt") == ["new.txt"]

    def test_quotes_and_trailing_slashes_are_stripped(self):
        assert porcelain_paths('?? ".forge/audits/runs/"') == [".forge/audits/runs"]

    def test_blank_and_malformed_lines_are_skipped(self):
        assert porcelain_paths("\n??\n\n?? kept.txt\n") == ["kept.txt"]


class TestStoryRunArtifactDirtOnly:
    def test_all_three_trees_together(self):
        status = "\n".join(
            (
                "?? .forge/audits/landing/817ecdc3d187.landed.json",
                "?? .forge/audits/runs/817ecdc3d187.json",
                "?? .forge/knowledge/summaries/817ecdc3d187.yaml",
            )
        )
        assert story_run_artifact_dirt_only(status) is True

    def test_operator_dirt_is_not_forge_dirt(self):
        assert story_run_artifact_dirt_only(" M forge.yaml") is False

    def test_one_operator_path_disqualifies_the_whole_block(self):
        """Mixed dirt still refuses: the operator's half is still theirs."""
        status = "?? .forge/audits/runs/abc.json\n M forge.yaml"
        assert story_run_artifact_dirt_only(status) is False

    def test_clean_status_has_nothing_to_attribute(self):
        assert story_run_artifact_dirt_only("") is False

    def test_the_tree_itself_counts_as_the_tree(self):
        assert story_run_artifact_dirt_only("?? .forge/audits/runs/") is True

    def test_a_collapsed_forge_tree_is_too_broad_to_attribute(self):
        """``?? .forge/`` names more than the artifact trees.

        Callers that need this excused ask git with ``-uall`` first; the
        predicate itself must not guess at what the collapsed entry contains.
        """
        assert story_run_artifact_dirt_only("?? .forge/") is False

    def test_a_sibling_path_sharing_a_prefix_is_not_inside_the_tree(self):
        """``.forge/audits/runs-scratch`` is not ``.forge/audits/runs``."""
        assert story_run_artifact_dirt_only("?? .forge/audits/runs-scratch/x") is False
