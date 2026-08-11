"""A model identifier is a claim about the provider, and it can stop being true (#2352).

The catalog, the alias map and the accounting table each named a DeepSeek
identifier the provider had retired. None of them could notice: the name kept
resolving upstream, so runs completed with exit 0 and real token counts while the
capability, tier and price recorded against it described a model that was no
longer there. The estimate those runs recorded was several times the rate the
provider actually charges, and it was stamped ``estimated`` like any other.

These tests pin the four properties that make that non-repeatable:

- a retired identifier is unreachable by routing and, when a configuration still
  names it, resolves to an *explanation* rather than to a model or to "unknown";
- the served identifiers carry the rate card in force, including the cache-hit
  tier DeepSeek bills separately, and that tier actually reaches the estimator;
- the reasoning mode the routing band claims is requested at invocation — on
  every path a run takes, not just the one that happened to be wired;
- an identifier nothing has checked recently is *reported* from configuration,
  before a run spends against it.
"""

from __future__ import annotations

import inspect
import json
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from theforge.agent_types import (
    COST_ESTIMATED,
    COST_ESTIMATED_UNCONFIRMED,
    COST_PROVENANCE_VALUES,
    COST_UNKNOWN,
    AgentResult,
    ModelUsage,
)
from theforge.config import load_config
from theforge.config.model_catalog import (
    parse_definition,
    resolve_packaged,
    unconfirmed_identities,
)
from theforge.config.model_identity import (
    IDENTITY_VERIFICATION_MAX_AGE_DAYS,
    IdentityVerification,
)
from theforge.config.models import (
    AGENT_REGISTRY,
    RETIRED_MODEL_REGISTRY,
    AgentDef,
    resolve_agent_spec,
)
from theforge.config.types import ModelProfile
from theforge.runners.adapters.deepseek import _run_deepseek, deepseek_request_kwargs
from theforge.runners.adapters.openai import _read_chat_usage
from theforge.runners.api import _estimated_provenance
from theforge.runners.schema_utils import (
    PRICING_TABLE,
    ToolCallRequest,
    _estimate_cost,
    pricing_for,
    rate_card_confirmed,
)

RETIRED = ("deepseek/deepseek-reasoner/api", "deepseek/deepseek-chat/api")
SERVED = ("deepseek/deepseek-v4-pro/api", "deepseek/deepseek-v4-flash/api")


def _base_definition(**overrides) -> dict:
    entry = {
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "transport": {"kind": "api"},
        "routing": {"tier": "strong", "capability": 9, "cost_rank": 1},
    }
    entry.update(overrides)
    return entry


def _deepseek_profile(**overrides) -> ModelProfile:
    fields = {
        "name": "deepseek-reviewer",
        "provider": "deepseek",
        "cli": None,
        "model": "deepseek-v4-pro",
        "budget_usd": 1.0,
        "timeout_seconds": 300,
        "allowed_tools": (),
    }
    fields.update(overrides)
    return ModelProfile(**fields)


# ── Retired identifiers are unroutable and self-explaining ────────────────


class TestRetiredIdentifiers:
    def test_retired_names_are_absent_from_the_routable_registry(self):
        """Nothing that selects a model can reach one."""
        for key in RETIRED:
            assert key not in AGENT_REGISTRY
            assert key in RETIRED_MODEL_REGISTRY

    def test_resolving_a_retired_name_explains_the_retirement(self):
        """The failure names what the provider did, not merely that lookup failed.

        "Unknown model" reads as a typo and sends an operator looking for the
        misspelling. The whole reason a retirement is *declared* rather than the
        entry deleted is that the operator needs the other message.
        """
        with pytest.raises(ValueError) as excinfo:
            resolve_agent_spec("deepseek/deepseek-reasoner/api")
        message = str(excinfo.value)
        assert "retired" in message
        assert "deepseek-v4-pro" in message  # names the replacement
        assert "not in AGENT_REGISTRY" not in message

    def test_the_legacy_alias_no_longer_masks_the_retirement(self):
        """The un-suffixed spelling reaches the same explanation.

        The alias map was one of the three places the retired name resolved
        cleanly. Keeping the alias is deliberate — it is what routes the old
        spelling to the explanation instead of to "unknown model" — but it must
        not resolve to a *model*.
        """
        for alias in ("deepseek/deepseek-reasoner", "deepseek/deepseek-chat"):
            with pytest.raises(ValueError, match="retired"):
                resolve_agent_spec(alias)

    def test_a_retired_name_in_models_enabled_fails_configuration_load(self, tmp_path):
        """Discoverable when the operator can still act on it, not after a run."""
        path = tmp_path / "forge.yaml"
        path.write_text(
            yaml.dump({"models": {"enabled": ["deepseek/deepseek-reasoner"]}}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="retired"):
            load_config(path)

    def test_a_retired_entry_carries_no_price(self):
        """A replaced rate card must not be able to price anything.

        The retired entries used to carry the figures that produced the wrong
        estimate. Dropping the price rather than keeping it "for reference" is
        what stops it from being read by anything downstream.
        """
        for key in RETIRED:
            spec = RETIRED_MODEL_REGISTRY[key]
            assert spec.input_cost_per_mtok is None
            assert spec.output_cost_per_mtok is None
            assert spec.pricing_provenance is None
            assert pricing_for(spec.provider, spec.model) is None

    def test_the_accounting_table_no_longer_carries_deepseek_at_all(self):
        """The third hand-maintained copy of the rate card is gone.

        It held four rows, two commented as aliases of a generation two releases
        old, at the superseded rates. Rates now live on the catalog entry beside
        the identity and verification date they were recorded with.
        """
        assert not [key for key in PRICING_TABLE if key[0] == "deepseek"]

    def test_a_retirement_must_state_a_reason(self):
        """A withdrawn name with no stated reason tells an operator nothing."""
        entry = _base_definition(identity={"status": "retired"})
        with pytest.raises(ValueError, match="retired_reason"):
            parse_definition(entry, where="models[0]")


# ── The served identifiers carry the rate card in force ───────────────────


class TestServedIdentifiers:
    def test_both_served_entries_declare_a_checked_identifier(self):
        for key in SERVED:
            spec = AGENT_REGISTRY[key]
            assert spec.identity.verified_on is not None
            assert spec.identity.verified_against

    @pytest.mark.parametrize(
        "key,expected",
        [
            ("deepseek/deepseek-v4-pro/api", (0.435, 0.87, 0.003625)),
            ("deepseek/deepseek-v4-flash/api", (0.14, 0.28, 0.0028)),
        ],
    )
    def test_served_entries_carry_the_published_rates_including_the_cache_tier(
        self, key, expected
    ):
        spec = AGENT_REGISTRY[key]
        assert (
            spec.input_cost_per_mtok,
            spec.output_cost_per_mtok,
            spec.cached_input_cost_per_mtok,
        ) == expected
        assert spec.pricing_provenance == spec.model

    def test_the_estimator_reads_the_catalog_rate_card(self):
        rates = pricing_for("deepseek", "deepseek-v4-pro")
        assert rates is not None
        assert rates.input_per_mtok == 0.435
        assert rates.cached_input_per_mtok == 0.003625

    def test_a_cache_hit_is_billed_at_the_provider_rate_not_a_flat_discount(self):
        """The tier is ~2% of uncached, which no fixed fraction approximates.

        Pricing it as 10%-of-input (the generic assumption) would overstate the
        cached half by roughly five times.
        """
        one_million_all_cached = _estimate_cost(
            "deepseek",
            "deepseek-v4-pro",
            input_tokens=1_000_000,
            output_tokens=0,
            cached_input_tokens=1_000_000,
        )
        assert one_million_all_cached == pytest.approx(0.003625)
        generic_discount = 0.435 * 0.1
        assert one_million_all_cached < generic_discount

    def test_a_provider_without_a_declared_cache_tier_keeps_the_generic_discount(self):
        """Unchanged behaviour for everyone else — this is an addition, not a swap."""
        cost = _estimate_cost(
            "openai",
            "gpt-5.4",
            input_tokens=1_000_000,
            output_tokens=0,
            cached_input_tokens=1_000_000,
        )
        assert cost == pytest.approx(1.25 * 0.1)

    def test_the_recorded_estimate_matches_the_rate_card_in_force(self):
        """The shape of the original defect, priced against the current card.

        The two runs that surfaced this recorded ≈ $3.73 for ~6.4M input / ~78k
        output, off the superseded rates *and* with the cache tier neither
        requested nor accounted for. Both halves of the correction are asserted
        because either alone leaves most of the error in place: the rate card
        move is worth ~25%, and reading the cache tier is worth the rest.
        """
        recorded_before = 3.727
        rates_only = _estimate_cost(
            "deepseek", "deepseek-v4-pro", input_tokens=6_400_000, output_tokens=78_000
        )
        assert rates_only is not None
        assert rates_only < recorded_before

        # A review re-reads a largely identical prompt each turn, so most of its
        # input is a cache hit — which is the tier the old model had no place for.
        with_cache = _estimate_cost(
            "deepseek",
            "deepseek-v4-pro",
            input_tokens=6_400_000,
            output_tokens=78_000,
            cached_input_tokens=6_000_000,
        )
        assert with_cache is not None
        assert with_cache < recorded_before / 5

    def test_a_local_endpoints_zero_does_not_leak_into_the_rate_map(self):
        """0.00 is true of the endpoint, not of the model name.

        ``openai/codestral/api`` is priced 0.00 because it is served locally. The
        same name reached over a vendor API is billed, so the catalog figure must
        not answer for it.
        """
        assert pricing_for("openai", "codestral") is None


# ── Verification ages out ─────────────────────────────────────────────────


class TestIdentityVerificationWindow:
    def test_a_recent_check_confirms_the_identifier(self):
        today = date(2026, 8, 10)
        identity = IdentityVerification(
            verified_against="provider model list", verified_on=today - timedelta(days=1)
        )
        assert identity.confirmed_on(today) is True

    def test_a_check_older_than_the_window_stops_confirming_it(self):
        """Evidence is about the day it was gathered, not forever.

        Without this, an entry marked verified stays verified through a later
        retirement — the original failure mode with an extra field on it.
        """
        today = date(2026, 8, 10)
        identity = IdentityVerification(
            verified_against="provider model list",
            verified_on=today - timedelta(days=IDENTITY_VERIFICATION_MAX_AGE_DAYS + 1),
        )
        assert identity.confirmed_on(today) is False
        assert "over the" in identity.describe(today)

    def test_an_entry_that_declares_nothing_is_unconfirmed_but_still_loads(self):
        """Absence degrades the claim; it does not break the catalog.

        Every entry that predates this block declares no check, and failing the
        whole load on that would stop forge starting rather than surface a defect.
        """
        resolved = resolve_packaged(
            parse_definition(_base_definition(), where="models[0]"), where="models[0]"
        )
        assert resolved.spec.identity.confirmed_on(date(2026, 8, 10)) is False
        assert "never checked" in resolved.spec.identity.describe(date(2026, 8, 10))

    def test_unconfirmed_entries_are_reportable_from_the_registry(self):
        reported = dict(unconfirmed_identities(AGENT_REGISTRY, today=date(2026, 8, 10)))
        # The DeepSeek entries were checked on 2026-08-10, so they are not in it;
        # the shorthands that declare nothing are.
        for key in SERVED:
            assert key not in reported
        assert "anthropic/sonnet/cli" in reported

    def test_check_config_reports_an_unconfirmed_identifier(self, tmp_path, capsys):
        """Reported where configuration is read, before a run can spend on it."""
        import argparse

        from theforge.cli.check_config import cmd_check_config

        path = tmp_path / "forge.yaml"
        path.write_text(
            yaml.dump({"models": {"enabled": ["anthropic/sonnet/cli"]}}), encoding="utf-8"
        )
        cmd_check_config(argparse.Namespace(config=str(path), verbose=False))
        out = capsys.readouterr().out
        assert "upstream identifier not confirmed" in out
        assert "anthropic/sonnet/cli" in out


# ── Schema validation at the config integrity boundary ────────────────────


class TestCatalogSchemaValidation:
    @pytest.mark.parametrize(
        "block,expected",
        [
            ({"status": "sunsetting"}, "must be one of"),
            ({"verified_on": "last tuesday"}, "ISO date"),
            ({"checked": True}, "only supports"),
        ],
    )
    def test_a_malformed_identity_block_is_a_load_error(self, block, expected):
        with pytest.raises(ValueError, match=expected):
            parse_definition(_base_definition(identity=block), where="models[0]")

    @pytest.mark.parametrize(
        "block,expected",
        [
            ({"reasoning_mode": "maybe"}, "must be one of"),
            ({"reasoning": "enabled"}, "only supports"),
        ],
    )
    def test_a_malformed_invocation_block_is_a_load_error(self, block, expected):
        with pytest.raises(ValueError, match=expected):
            parse_definition(_base_definition(invocation=block), where="models[0]")

    def test_a_negative_cache_rate_is_a_load_error(self):
        entry = _base_definition(
            cost={
                "input_per_mtok": 1.0,
                "output_per_mtok": 2.0,
                "cached_input_per_mtok": -0.1,
                "pricing_provenance": "deepseek-v4-pro",
            }
        )
        with pytest.raises(ValueError, match="cached_input_per_mtok"):
            parse_definition(entry, where="models[0]")


# ── The declared mode is the mode that gets requested ─────────────────────


class TestReasoningModeReachesTheProvider:
    def test_the_catalog_declares_the_mode_the_band_was_recorded_about(self):
        assert AGENT_REGISTRY["deepseek/deepseek-v4-pro/api"].reasoning_mode == "enabled"

    def test_the_mode_survives_the_whole_projection_to_a_profile(self):
        """AgentSpec → ModelInfo → AgentDef → ModelProfile.

        The adapter reads a ModelProfile, and the pool is the only route a routed
        model takes to get there. A field that stops anywhere along this chain is
        a declaration the invocation never sees.
        """
        from theforge.config.models import _spec_to_model_info

        spec = AGENT_REGISTRY["deepseek/deepseek-v4-pro/api"]
        info = _spec_to_model_info("deepseek/deepseek-v4-pro/api", spec)
        assert info.reasoning_mode == "enabled"
        agent = AgentDef(
            name="deepseek",
            provider="deepseek",
            model=info.model,
            budget_usd=1.0,
            timeout_seconds=300,
            tier="strong",
            reasoning_mode=info.reasoning_mode,
        )
        assert agent.to_model_profile().reasoning_mode == "enabled"

    def test_request_kwargs_carry_the_mode_and_the_routed_effort(self):
        profile = _deepseek_profile(reasoning_mode="enabled", reasoning_effort="high")
        assert deepseek_request_kwargs(profile) == {
            "extra_body": {"thinking": {"type": "enabled", "reasoning_effort": "max"}}
        }

    def test_the_control_is_shaped_as_a_kwarg_the_real_sdk_accepts(self):
        """Bound against the installed SDK's actual signature, not a mock's.

        ``thinking`` is DeepSeek's extension to the request body, not an OpenAI
        SDK parameter, and ``create()`` declares an explicit parameter list with
        no ``**kwargs``. Passing it top-level raises TypeError in the client
        before any request is sent — so the mode is never requested and the call
        never happens. A MagicMock accepts anything and cannot see that, which is
        why this binds the kwargs to the real signature instead.
        """
        openai = pytest.importorskip("openai")
        create = openai.resources.chat.completions.Completions.create
        signature = inspect.signature(create)
        assert not [p for p in signature.parameters.values() if p.kind is p.VAR_KEYWORD], (
            "SDK grew **kwargs; this guard needs rethinking"
        )

        profile = _deepseek_profile(reasoning_mode="enabled", reasoning_effort="high")
        request = {
            "model": profile.model,
            "messages": [{"role": "user", "content": "go"}],
            **deepseek_request_kwargs(profile),
        }
        signature.bind_partial(None, **request)  # raises TypeError on an unknown kwarg
        assert request["extra_body"]["thinking"]["type"] == "enabled"

    def test_forges_effort_vocabulary_is_mapped_not_passed_through(self):
        """DeepSeek has no "medium"; sending one is not a no-op.

        Either it 400s or the provider remaps it, in which case the effort forge
        recorded in the audit is not the effort that ran.
        """
        profile = _deepseek_profile(reasoning_mode="enabled", reasoning_effort="medium")
        thinking = deepseek_request_kwargs(profile)["extra_body"]["thinking"]
        assert thinking["reasoning_effort"] == "high"

    def test_a_profile_declaring_nothing_sends_nothing(self):
        """No mode is invented for a profile built outside the registry path."""
        assert deepseek_request_kwargs(_deepseek_profile()) == {}

    def test_the_single_shot_path_forwards_the_thinking_block(self):
        profile = _deepseek_profile(reasoning_mode="enabled", reasoning_effort="low")
        client = MagicMock()
        response = MagicMock()
        response.choices[0].message.content = json.dumps(
            {
                "verdict": "APPROVE",
                "summary": "ok",
                "findings": [],
                "story_compliance": {"matches_spec": True, "mismatches": []},
                "test_coverage": {"adequate": True, "gaps": []},
            }
        )
        response.usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
        response.model_dump.return_value = {}
        client.chat.completions.create.return_value = response

        with patch("theforge.runners.adapters.deepseek._deepseek_client", return_value=client):
            result = _run_deepseek("review this", profile)

        assert result.success
        kwargs = client.chat.completions.create.call_args[1]
        assert kwargs["extra_body"] == {"thinking": {"type": "enabled", "reasoning_effort": "low"}}

    def test_the_tool_loop_path_forwards_the_thinking_block(self):
        """The loop is the path a routed reviewer actually takes.

        It builds its adapter directly rather than through the DeepSeek wrapper,
        so wiring the wrapper alone would leave every real review call without
        the mode its band was recorded about.
        """
        from theforge.runners.api import _run_loop_deepseek

        profile = _deepseek_profile(
            reasoning_mode="enabled", reasoning_effort="high", allowed_tools=("read_file",)
        )
        client = MagicMock()
        response = MagicMock()
        response.choices[0].message.content = "done"
        response.choices[0].message.tool_calls = []
        response.usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
        client.chat.completions.create.return_value = response

        with patch("theforge.runners.api._deepseek_client", return_value=client):
            _run_loop_deepseek("review this", profile, working_dir=None)

        kwargs = client.chat.completions.create.call_args[1]
        assert kwargs["extra_body"] == {"thinking": {"type": "enabled", "reasoning_effort": "max"}}

    def test_the_finalizer_path_forwards_the_thinking_block(self):
        """Finalization is still an invocation of the model the role was filled with."""
        from theforge.runners.finalizers import _make_deepseek_finalizer

        profile = _deepseek_profile(
            reasoning_mode="enabled", reasoning_effort="low", phase="review"
        )
        client = MagicMock()
        response = MagicMock()
        response.choices[0].message.content = "{}"
        response.usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
        client.chat.completions.create.return_value = response

        finalizer = _make_deepseek_finalizer(profile, None, client=client)
        finalizer([{"role": "user", "content": "go"}])

        kwargs = client.chat.completions.create.call_args[1]
        assert kwargs["extra_body"] == {"thinking": {"type": "enabled", "reasoning_effort": "low"}}


# ── A retirement cannot be undone from the project layer ──────────────────


class TestRetiredIdentifiersCannotBeRedeclared:
    """The retirement has to be a property of the *identifier*, not of one file.

    Declaring it only on the shipped entry left the whole point defeatable by a
    project: redeclare the same name with your own routing and cost, and it is
    selectable again — now priced off figures an operator wrote for a model the
    provider no longer serves, which is strictly worse than the original defect.
    """

    def test_an_inline_models_enabled_mapping_cannot_revive_a_retired_name(self, tmp_path):
        path = tmp_path / "forge.yaml"
        path.write_text(
            yaml.dump(
                {
                    "models": {
                        "enabled": [
                            {
                                "provider": "deepseek",
                                "model": "deepseek-reasoner",
                                "transport": {"kind": "api"},
                                "routing": {
                                    "tier": "strong",
                                    "capability": 9,
                                    "cost_rank": 2,
                                    "cost_rank_basis": "declared-policy",
                                },
                                "cost": {"input_per_mtok": 0.55, "output_per_mtok": 2.19},
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="retired"):
            load_config(path)

    def test_a_models_custom_declaration_cannot_revive_a_retired_name(self, tmp_path):
        path = tmp_path / "forge.yaml"
        path.write_text(
            yaml.dump(
                {
                    "models": {
                        "enabled": ["anthropic/sonnet/cli"],
                        "custom": {
                            "deepseek/deepseek-reasoner/api": {
                                "provider": "deepseek",
                                "model": "deepseek-reasoner",
                                "tier": "strong",
                                "input_cost_per_mtok": 0.55,
                                "output_cost_per_mtok": 2.19,
                            }
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="retired"):
            load_config(path)

    def test_the_refusal_names_the_identifier_and_the_reason(self):
        """Both project surfaces resolve through one guard, so both say the same thing."""
        from theforge.config.model_catalog import resolve_project

        defn = parse_definition(
            _base_definition(model="deepseek-reasoner"), where="models.custom.x"
        )
        with pytest.raises(ValueError) as excinfo:
            resolve_project(defn, where="models.custom.x", builtin=None)
        message = str(excinfo.value)
        assert "deepseek/deepseek-reasoner/api" in message
        assert "deepseek-v4-pro" in message  # the replacement, from the retirement reason

    def test_a_served_identifier_still_overlays_normally(self):
        """The guard is scoped to retired identities and nothing else."""
        from theforge.config.model_catalog import resolve_project

        defn = parse_definition(
            _base_definition(cost={"input_per_mtok": 9.0, "output_per_mtok": 9.0}),
            where="models.custom.x",
        )
        resolved = resolve_project(
            defn, where="models.custom.x", builtin=AGENT_REGISTRY["deepseek/deepseek-v4-pro/api"]
        )
        assert resolved.spec.input_cost_per_mtok == 9.0


# ── Reasoning content survives the turn without being replayed ────────────


class TestReasoningContentHandling:
    def test_the_adapter_captures_reasoning_content(self):
        """DeepSeek returns the chain of thought beside ``content``, not inside it."""
        from theforge.runners.adapters.openai import _make_openai_chat_adapter

        client = MagicMock()
        response = MagicMock()
        response.choices[0].message.content = "calling a tool"
        response.choices[0].message.tool_calls = []
        response.choices[0].message.reasoning_content = "let me check the file first"
        response.usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
        client.chat.completions.create.return_value = response

        adapter = _make_openai_chat_adapter(
            _deepseek_profile(reasoning_mode="enabled"), None, client=client
        )
        turn = adapter([{"role": "user", "content": "go"}], [])
        assert turn.reasoning_content == "let me check the file first"

    def test_a_provider_that_reports_no_reasoning_leaves_it_none(self):
        from theforge.runners.adapters.openai import _make_openai_chat_adapter

        client = MagicMock()
        response = MagicMock()
        response.choices[0].message.content = "done"
        response.choices[0].message.tool_calls = []
        response.choices[0].message.reasoning_content = None
        response.usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
        client.chat.completions.create.return_value = response

        adapter = _make_openai_chat_adapter(_deepseek_profile(), None, client=client)
        assert adapter([{"role": "user", "content": "go"}], []).reasoning_content is None

    def test_the_assistant_turn_keeps_its_text_and_its_reasoning(self):
        """The loop hardcoded ``content: None``, erasing what the model said.

        A model that explains itself and then calls a tool had that sentence
        dropped from its own history on the next turn, alongside the reasoning.
        """
        from theforge.runners.api import AgentLoopManager

        manager = AgentLoopManager.__new__(AgentLoopManager)
        messages = manager._append_tool_results(
            [{"role": "user", "content": "go"}],
            [ToolCallRequest(id="c1", name="read_file", arguments={"path": "a.py"})],
            [{"id": "c1", "content": "ok"}],
            content="I will read the file",
            reasoning_content="the file is the likely source",
        )
        assistant = messages[1]
        assert assistant["content"] == "I will read the file"
        assert assistant["reasoning_content"] == "the file is the likely source"

    def test_reasoning_content_round_trips_onto_the_outgoing_assistant_message(self):
        """DeepSeek requires it back once tools are in play.

        Per the provider's thinking-mode guide: with tool calls the intermediate
        assistant's ``reasoning_content`` "must participate in the context
        concatenation and must be passed back to the API in all subsequent user
        interaction turns", and omitting it returns 400.
        """
        from theforge.runners.adapters.openai import _translate_messages_openai_chat

        translated = _translate_messages_openai_chat(
            [
                {
                    "role": "assistant",
                    "content": "I will read the file",
                    "reasoning_content": "the file is the likely source",
                    "tool_calls": [
                        ToolCallRequest(id="c1", name="read_file", arguments={"path": "a.py"})
                    ],
                }
            ]
        )
        assert translated[0]["content"] == "I will read the file"
        assert translated[0]["reasoning_content"] == "the file is the likely source"
        assert translated[0]["tool_calls"][0]["id"] == "c1"

    def test_a_provider_that_reports_no_reasoning_sends_no_such_key(self):
        """The replay is data-driven, so nothing changes for OpenAI.

        OpenAI's Chat Completions never returns ``reasoning_content``, so an
        OpenAI history never carries it and its request bodies are byte-identical
        to before. That is what lets the round-trip live in shared code without a
        branch on provider.
        """
        from theforge.runners.adapters.openai import _translate_messages_openai_chat

        translated = _translate_messages_openai_chat(
            [
                {
                    "role": "assistant",
                    "content": "calling a tool",
                    "tool_calls": [
                        ToolCallRequest(id="c1", name="read_file", arguments={"path": "a.py"})
                    ],
                }
            ]
        )
        assert "reasoning_content" not in translated[0]

    def test_a_thinking_mode_tool_call_turn_round_trips_into_a_second_request(self):
        """Two provider calls, so the second one's body is the thing under test.

        This is the shape that 400s in production: the first turn thinks and
        calls a tool, and the continuation is rejected unless it carries the
        reasoning back. A single-call test cannot see it.
        """
        from theforge.runners.api import _run_loop_deepseek

        profile = _deepseek_profile(
            reasoning_mode="enabled", reasoning_effort="high", allowed_tools=("read_file",)
        )
        first = MagicMock()
        first.choices[0].message.content = "I will read the file"
        first.choices[0].message.reasoning_content = "the file is the likely source"
        tool_call = MagicMock()
        tool_call.id = "c1"
        tool_call.function.name = "read_file"
        tool_call.function.arguments = json.dumps({"path": "a.py"})
        first.choices[0].message.tool_calls = [tool_call]
        first.usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)

        second = MagicMock()
        second.choices[0].message.content = "done"
        second.choices[0].message.tool_calls = []
        second.choices[0].message.reasoning_content = None
        second.usage = SimpleNamespace(prompt_tokens=20, completion_tokens=6)

        client = MagicMock()
        client.chat.completions.create.side_effect = [first, second]

        with patch("theforge.runners.api._deepseek_client", return_value=client):
            _run_loop_deepseek("review this", profile, working_dir=None)

        assert client.chat.completions.create.call_count == 2
        follow_up = client.chat.completions.create.call_args_list[1][1]
        # The mode is still requested on the follow-up turn, in SDK-legal shape.
        assert follow_up["extra_body"] == {
            "thinking": {"type": "enabled", "reasoning_effort": "max"}
        }
        assistant = [m for m in follow_up["messages"] if m["role"] == "assistant"][0]
        assert assistant["content"] == "I will read the file"
        assert assistant["reasoning_content"] == "the file is the likely source"
        assert assistant["tool_calls"][0]["id"] == "c1"

    def test_the_finalizer_also_replays_the_reasoning_it_was_handed(self):
        """The finalizer replays the *whole* history, so it 400s on the same rule.

        It is the second DeepSeek call site that concatenates a prior tool-call
        turn, and it runs at the end of every review that ran out of iterations
        or time — exactly when the history is longest and a rejection costs most.
        """
        from theforge.runners.finalizers import _make_deepseek_finalizer

        profile = _deepseek_profile(
            reasoning_mode="enabled", reasoning_effort="low", phase="review"
        )
        client = MagicMock()
        response = MagicMock()
        response.choices[0].message.content = "{}"
        response.usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
        client.chat.completions.create.return_value = response

        finalizer = _make_deepseek_finalizer(profile, None, client=client)
        finalizer(
            [
                {"role": "user", "content": "go"},
                {
                    "role": "assistant",
                    "content": "I will read the file",
                    "reasoning_content": "the file is the likely source",
                    "tool_calls": [
                        ToolCallRequest(id="c1", name="read_file", arguments={"path": "a.py"})
                    ],
                },
                {"role": "tool_results", "results": [{"id": "c1", "content": "ok"}]},
            ]
        )

        sent = client.chat.completions.create.call_args[1]["messages"]
        assistant = [m for m in sent if m["role"] == "assistant"][0]
        assert assistant["reasoning_content"] == "the file is the likely source"


# ── An estimate says what rate card it rests on ───────────────────────────


class TestCostProvenanceDistinguishesTheRateCard:
    def test_provenance_values_include_the_unconfirmed_estimate(self):
        assert COST_ESTIMATED_UNCONFIRMED in COST_PROVENANCE_VALUES

    def test_a_confirmed_rate_card_yields_a_plain_estimate(self):
        assert rate_card_confirmed("deepseek", "deepseek-v4-pro") is True
        assert _estimated_provenance("deepseek", "deepseek-v4-pro") == COST_ESTIMATED

    def test_a_pricing_table_rate_is_never_confirmed(self):
        """That table records no identity and no date.

        It is how the superseded DeepSeek rates survived two revisions of the
        provider's published pricing without anything noticing.
        """
        assert rate_card_confirmed("openai", "gpt-4o") is False
        assert _estimated_provenance("openai", "gpt-4o") == COST_ESTIMATED_UNCONFIRMED

    def test_an_aged_out_check_stops_confirming_the_rate_card(self):
        stale = date(2026, 8, 10) + timedelta(days=IDENTITY_VERIFICATION_MAX_AGE_DAYS + 1)
        assert rate_card_confirmed("deepseek", "deepseek-v4-pro", today=stale) is False

    def test_the_stamp_records_which_kind_of_estimate_each_component_is(self):
        """A result can span models, so the claim is made per component."""
        from theforge.runners.api import _stamp_api_cost_provenance

        result = AgentResult(
            success=True,
            output="ok",
            session_id=None,
            cost_usd=0.5,
            exit_code=0,
            raw={},
            model_used="deepseek-v4-pro",
            model_usage=(
                ModelUsage(
                    model="deepseek-v4-pro",
                    input_tokens=10,
                    output_tokens=5,
                    cache_read_tokens=0,
                    cache_creation_tokens=0,
                    cost_usd=0.5,
                ),
            ),
        )
        stamped = _stamp_api_cost_provenance(result, "deepseek")
        assert stamped.model_usage[0].cost_provenance == COST_ESTIMATED
        assert stamped.cost_provenance == COST_ESTIMATED

    def test_an_unconfirmed_component_makes_the_whole_invocation_unconfirmed(self):
        """The invocation-level statement is the weakest of its components'."""
        from theforge.runners.api import _stamp_api_cost_provenance

        def _usage(model: str) -> ModelUsage:
            return ModelUsage(
                model=model,
                input_tokens=10,
                output_tokens=5,
                cache_read_tokens=0,
                cache_creation_tokens=0,
                cost_usd=0.25,
            )

        result = AgentResult(
            success=True,
            output="ok",
            session_id=None,
            cost_usd=0.5,
            exit_code=0,
            raw={},
            model_used="deepseek-v4-pro",
            model_usage=(_usage("deepseek-v4-pro"), _usage("some-unlisted-model")),
        )
        stamped = _stamp_api_cost_provenance(result, "deepseek")
        assert [u.cost_provenance for u in stamped.model_usage] == [
            COST_ESTIMATED,
            COST_ESTIMATED_UNCONFIRMED,
        ]
        assert stamped.cost_provenance == COST_ESTIMATED_UNCONFIRMED

    def test_a_none_cost_still_records_unknown(self):
        """Nothing observed means nothing to characterize — unchanged."""
        from theforge.runners.api import _stamp_api_cost_provenance

        result = AgentResult(
            success=False,
            output="boom",
            session_id=None,
            cost_usd=None,
            exit_code=1,
            raw={},
        )
        assert _stamp_api_cost_provenance(result, "deepseek").cost_provenance == COST_UNKNOWN


# ── The provider's own usage report is read, not discarded ────────────────


class TestUsageExtraction:
    def test_deepseeks_cache_and_reasoning_counts_are_read(self):
        usage = SimpleNamespace(
            prompt_tokens=1000,
            completion_tokens=300,
            prompt_cache_hit_tokens=800,
            prompt_cache_miss_tokens=200,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=120),
        )
        parsed = _read_chat_usage(usage)
        assert parsed.cache_read_tokens == 800
        assert parsed.thinking_tokens == 120

    def test_reasoning_tokens_are_split_out_of_the_completion_count(self):
        """``completion_tokens`` already includes reasoning on this wire format.

        ``_estimate_cost`` bills ``output_tokens + thinking_tokens``, so carrying
        both through unchanged would bill the reasoning twice.
        """
        usage = SimpleNamespace(
            prompt_tokens=0,
            completion_tokens=300,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=120),
        )
        parsed = _read_chat_usage(usage)
        assert parsed.output_tokens == 180
        assert parsed.output_tokens + parsed.thinking_tokens == 300

    def test_openais_cached_tokens_spelling_is_also_read(self):
        usage = SimpleNamespace(
            prompt_tokens=1000,
            completion_tokens=10,
            prompt_tokens_details=SimpleNamespace(cached_tokens=640),
        )
        assert _read_chat_usage(usage).cache_read_tokens == 640

    def test_a_usage_object_reporting_neither_yields_zeroes(self):
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
        parsed = _read_chat_usage(usage)
        assert (parsed.cache_read_tokens, parsed.thinking_tokens) == (0, 0)
        assert (parsed.input_tokens, parsed.output_tokens) == (10, 5)

    def test_the_accumulated_cache_count_reaches_the_estimator(self):
        """Accumulating cache reads and then not pricing them was the gap.

        The loop recorded ``cache_read_tokens`` and passed only input/output to
        ``_estimate_cost``, so every cached token was billed at the uncached rate.
        """
        from theforge.runners.api import _UsageAccumulator

        accumulator = _UsageAccumulator(input_tokens=1_000_000, cache_read_tokens=1_000_000)
        usage = accumulator.to_model_usage("deepseek-v4-pro", "deepseek")
        assert usage.cost_usd == pytest.approx(0.003625)

    def test_a_provider_reporting_cache_reads_outside_input_is_left_alone(self):
        """Anthropic's ``input_tokens`` already excludes its cache reads.

        Handing that count to the estimator would subtract real uncached input
        from the bill, so the adjustment is scoped to the providers whose input
        count contains the cache hits.
        """
        from theforge.runners.api import _UsageAccumulator

        accumulator = _UsageAccumulator(input_tokens=1_000_000, cache_read_tokens=1_000_000)
        usage = accumulator.to_model_usage("claude-sonnet-4-6", "anthropic")
        assert usage.cost_usd == pytest.approx(3.00)
