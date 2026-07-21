"""Unit tests for the domain taxonomy module (issue #155).

Mirror of ``src/theforge/domains.py`` — covers the closed taxonomy set, tag
normalization/validation, and the prompt-line rendering used as the single
source the preflight prompt enumerates from.
"""

from __future__ import annotations

from theforge.domains import (
    DOMAIN_DESCRIPTIONS,
    DOMAIN_TAXONOMY,
    normalize_domain,
    taxonomy_prompt_lines,
    validate_domains,
)

# ── The taxonomy set ───────────────────────────────────────────────────


class TestTaxonomySet:
    def test_taxonomy_matches_descriptions_keys(self):
        # DOMAIN_TAXONOMY is the closed admission set derived from the docs map,
        # so the two can never drift apart.
        assert DOMAIN_TAXONOMY == frozenset(DOMAIN_DESCRIPTIONS)

    def test_expected_core_tags_present(self):
        for tag in ("react", "css", "api", "database", "cli", "config"):
            assert tag in DOMAIN_TAXONOMY

    def test_every_tag_is_lowercase_and_documented(self):
        for tag, desc in DOMAIN_DESCRIPTIONS.items():
            assert tag == tag.lower()
            assert isinstance(desc, str) and desc.strip(), f"{tag} lacks a description"


# ── normalize_domain ───────────────────────────────────────────────────


class TestNormalizeDomain:
    def test_known_tags_pass_through(self):
        for tag in DOMAIN_TAXONOMY:
            assert normalize_domain(tag) == tag

    def test_case_is_folded(self):
        assert normalize_domain("React") == "react"
        assert normalize_domain("API") == "api"

    def test_separators_folded_to_hyphen(self):
        assert normalize_domain("data_processing") == "data-processing"
        assert normalize_domain("Data Processing") == "data-processing"

    def test_surrounding_whitespace_trimmed(self):
        assert normalize_domain("  cli  ") == "cli"

    def test_unknown_tag_is_none(self):
        assert normalize_domain("blockchain") is None
        assert normalize_domain("") is None

    def test_non_string_is_none(self):
        assert normalize_domain(123) is None
        assert normalize_domain(None) is None
        assert normalize_domain(["react"]) is None


# ── validate_domains ───────────────────────────────────────────────────


class TestValidateDomains:
    def test_keeps_known_drops_unknown(self):
        assert validate_domains(["react", "bogus", "css"]) == ["react", "css"]

    def test_dedups_preserving_first_appearance_order(self):
        assert validate_domains(["css", "react", "React", "css"]) == ["css", "react"]

    def test_normalizes_each_element(self):
        assert validate_domains(["Data_Processing", "API"]) == ["data-processing", "api"]

    def test_accepts_tuple(self):
        assert validate_domains(("cli", "config")) == ["cli", "config"]

    def test_non_list_inputs_are_empty(self):
        assert validate_domains(None) == []
        assert validate_domains("react") == []
        assert validate_domains(42) == []
        assert validate_domains({"react": 1}) == []

    def test_empty_list_is_empty(self):
        assert validate_domains([]) == []

    def test_all_unknown_is_empty(self):
        assert validate_domains(["quantum", "blockchain"]) == []


# ── taxonomy_prompt_lines ──────────────────────────────────────────────


class TestTaxonomyPromptLines:
    def test_renders_one_line_per_tag(self):
        lines = taxonomy_prompt_lines().splitlines()
        assert len(lines) == len(DOMAIN_DESCRIPTIONS)

    def test_each_line_names_tag_and_description(self):
        rendered = taxonomy_prompt_lines()
        for tag, desc in DOMAIN_DESCRIPTIONS.items():
            assert f"`{tag}`" in rendered
            assert desc in rendered
