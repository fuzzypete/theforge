"""A provider-reported dated model version resolves like its undated form.

Issue #2311. Anthropic bills components under dated spellings
(``claude-haiku-4-5-20251001``); the catalog names the undated version
(``claude-haiku-4-5``). Resolution matched the hand-written shorthand and missed
the provider's own format, so the identity the provider *chose to send* — the
most authoritative and most common statement of what ran — routinely landed in
the ``unresolved`` bucket and evidence accrued under a key nothing could group by.

The carve-out stays: a spelling that genuinely names nothing in the catalog is
still recorded verbatim and marked unresolved. What these tests pin is that the
ordinary case no longer takes that path.
"""

from __future__ import annotations

from theforge.coordinator.agent_identity import (
    RESOLUTION_CANONICAL,
    RESOLUTION_UNRESOLVED,
    canonicalize_identity,
    entry_identity_ledger,
)
from theforge.model_profiles import canonical_id_for_legacy_key

DATED = "claude-haiku-4-5-20251001"
UNDATED = "claude-haiku-4-5"
CANONICAL = "anthropic/claude-haiku-4-5/cli"


class TestDatedVersionResolvesLikeItsUndatedForm:
    def test_the_provider_spelling_resolves_to_the_catalog_identity(self) -> None:
        assert canonical_id_for_legacy_key(DATED) == CANONICAL

    def test_dated_and_undated_group_under_one_identity(self) -> None:
        """The point of the fix: two spellings, one grouping key."""
        for dated, undated in (
            (DATED, UNDATED),
            ("claude-sonnet-5-20250101", "claude-sonnet-5"),
            ("claude-opus-5-20250101", "claude-opus-5"),
        ):
            resolved = canonical_id_for_legacy_key(dated)
            assert resolved == canonical_id_for_legacy_key(undated)
            assert resolved is not None

    def test_an_iso_dashed_release_date_normalizes_too(self) -> None:
        """OpenAI spells the same suffix ``-2024-08-06``; the hint disambiguates."""
        hint = {"transport_used": "cli"}
        assert canonical_id_for_legacy_key("gpt-5.4-2024-08-06", hint) == (
            canonical_id_for_legacy_key("gpt-5.4", hint)
        )

    def test_a_transport_suffix_survives_the_date_strip(self) -> None:
        assert canonical_id_for_legacy_key(f"{DATED}-cli") == CANONICAL

    def test_the_transport_hint_still_narrows_a_dated_spelling(self) -> None:
        """Date normalization does not bypass the ambiguity rules it feeds."""
        # ``claude-sonnet-4-6`` is CLI-only, so an API hint leaves it unresolved
        # — and the dated spelling must inherit exactly that answer, not a
        # looser one.
        api = {"transport_used": "api"}
        assert canonical_id_for_legacy_key("claude-sonnet-4-6", api) is None
        assert canonical_id_for_legacy_key("claude-sonnet-4-6-20250101", api) is None


class TestGenuinelyUnknownIdentitiesStayUnresolved:
    def test_a_version_absent_from_the_catalog_is_still_unresolved(self) -> None:
        """Stripping a date only ever *removes* precision — it invents nothing."""
        assert canonical_id_for_legacy_key("claude-opus-4-8") is None
        assert canonical_id_for_legacy_key("claude-opus-4-8-20260101") is None

    def test_a_version_number_is_not_mistaken_for_a_release_date(self) -> None:
        """``-4-6`` is part of the model name; the shorthand fold stays dead (#2226)."""
        assert canonical_id_for_legacy_key("claude-sonnet-4-6") == (
            "anthropic/claude-sonnet-4-6/cli"
        )
        # An implausible date is not a date, so nothing is stripped and the
        # unknown spelling stays unresolved rather than folding onto the version.
        assert canonical_id_for_legacy_key("claude-sonnet-5-20251301") is None
        assert canonical_id_for_legacy_key("claude-sonnet-5-99999999") is None

    def test_a_bare_date_resolves_to_nothing(self) -> None:
        assert canonical_id_for_legacy_key("20251001") is None
        assert canonical_id_for_legacy_key("-20251001") is None

    def test_an_unknown_spelling_is_recorded_verbatim_and_marked(self) -> None:
        assert canonicalize_identity("who-knows-4-1-20251001") == (
            "who-knows-4-1-20251001",
            RESOLUTION_UNRESOLVED,
        )


class TestBilledComponentLedgerSeam:
    """The renderer writes the component; the reader groups it. One namespace.

    Built with the real writer (:func:`audit_render._agent_entry`) rather than a
    hand-shaped dict, so this fails if either side of the seam drifts.
    """

    def _entry(self, billed: str) -> dict:
        from theforge.agent_types import AgentResult, ModelUsage
        from theforge.coordinator import audit_render

        result = AgentResult(
            success=True,
            output="done",
            session_id=None,
            cost_usd=1.0,
            exit_code=0,
            raw={},
            profile_name="dev",
            model_used=UNDATED,
            transport_used="cli",
            model_usage=(
                ModelUsage(
                    model=billed,
                    input_tokens=10,
                    output_tokens=20,
                    cache_read_tokens=0,
                    cache_creation_tokens=0,
                    cost_usd=0.5,
                ),
            ),
        )
        return audit_render._agent_entry(result, "dev", "dev", 12.0)

    def test_a_dated_billed_component_is_canonical_in_the_written_record(self) -> None:
        entry = self._entry(DATED)
        component = entry["ledger"]["billed_components"][0]["identity"]

        # The raw spelling the provider sent is preserved — resolution adds a
        # projection, it does not overwrite what was reported.
        assert component["raw"] == DATED
        assert component["identity"] == CANONICAL
        assert component["resolution"] == RESOLUTION_CANONICAL

    def test_the_reader_groups_the_component_with_the_invocation_identity(self) -> None:
        ledger = entry_identity_ledger(self._entry(DATED))
        assert ledger is not None
        assert ledger["billed"][0][0] == CANONICAL
        assert ledger["billed"][0][2] == RESOLUTION_CANONICAL
        # The dated component and the undated invocation now share a key.
        assert ledger["resolved"][0] == ledger["billed"][0][0]

    def test_a_pre_ledger_component_block_re_derives_the_same_identity(self) -> None:
        """A record written before the identity projection existed still reads."""
        entry = {
            "role": "dev",
            "ledger": {
                "version": 1,
                "billed_components": [
                    {"identity": {"raw": DATED, "transport": "cli"}},
                ],
            },
        }
        ledger = entry_identity_ledger(entry)
        assert ledger is not None
        assert ledger["billed"] == [(CANONICAL, "direct", RESOLUTION_CANONICAL)]
