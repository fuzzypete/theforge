"""Domain-aware adaptive routing (issue #155).

Seam-level coverage for the preflight → profile-aggregation → assignment flow:

- taxonomy validation / normalization
- preflight ``domains`` parsing and prompt surface
- deterministic per-domain aggregation in model_profiles with clause-2 gates
- domain match as a tie/preference-only signal inside the eligible pool
- cold-start models not penalized
- routing_decision explainability when domain match influences the decision
"""

from __future__ import annotations

from pathlib import Path

from theforge.assignment import AssignmentConfig, assign_models
from theforge.config import AgentDef
from theforge.coordinator.preflight import _parse_preflight_domains
from theforge.model_profiles import (
    RunOutcome,
    apply_run,
    get_dev_domain_signal,
)
from theforge.task import TaskStory, build_preflight_prompt

# Taxonomy-module unit coverage lives in tests/test_domains.py (the source
# mirror); this file covers the preflight→aggregation→routing seam.

# ── Preflight parsing + prompt surface ─────────────────────────────────


class TestParsePreflightDomains:
    def test_parses_list(self):
        out = "```yaml\nverdict: PROCEED\ndomains: [api, database]\n```"
        assert _parse_preflight_domains(out) == ["api", "database"]

    def test_drops_unknown_tags(self):
        out = "```yaml\nverdict: PROCEED\ndomains: [api, quantum]\n```"
        assert _parse_preflight_domains(out) == ["api"]

    def test_missing_field_is_empty(self):
        out = "```yaml\nverdict: PROCEED\ncomplexity: small\n```"
        assert _parse_preflight_domains(out) == []

    def test_malformed_yaml_is_empty(self):
        assert _parse_preflight_domains("```yaml\n{{{ bad\n```") == []

    def test_no_fences_still_parses(self):
        assert _parse_preflight_domains("verdict: PROCEED\ndomains: [cli]\n") == ["cli"]


class TestPreflightPromptDomains:
    def test_prompt_advertises_taxonomy_and_field(self, tmp_path: Path):
        spec = tmp_path / "s.md"
        spec.write_text("# Test", encoding="utf-8")
        task = TaskStory(name="T", story_path=spec, slug="t")
        prompt = build_preflight_prompt(task, story_content="# Test")
        assert "Domain Classification" in prompt
        assert "domains: []" in prompt
        # Enumerated from the single-source taxonomy.
        assert "`react`" in prompt
        assert "`database`" in prompt


# ── Per-domain aggregation ─────────────────────────────────────────────


def _record(
    data: dict, model: str, success: bool, *, domains, n: int = 1, tainted: bool = False
) -> None:
    for _ in range(n):
        apply_run(
            data,
            RunOutcome(
                complexity="medium",
                dev_model=model,
                dev_success=success,
                dev_iterations=1,
                dev_cost_usd=0.1,
                domains=list(domains),
                dev_tainted=tainted,
            ),
        )


class TestPerDomainAggregation:
    def test_rate_is_deterministic_aggregation(self):
        data: dict = {"models": {}}
        _record(data, "A", True, domains=["api"], n=3)
        _record(data, "A", False, domains=["api"], n=1)
        sig = get_dev_domain_signal(data, "A", ["api"])
        assert sig["runs"] == 4
        assert sig["raw"] == 0.75
        # All 4 runs fit the recency window, so weighted == raw here; rate is the
        # recency-weighted value (never the lifetime raw).
        assert sig["weighted"] == 0.75
        assert sig["rate"] == 0.75
        assert sig["recency"] == "windowed"
        assert sig["floor"] == "pass"

    def test_sample_floor_gates_rate(self):
        data: dict = {"models": {}}
        _record(data, "A", True, domains=["api"], n=2)  # below min_runs=3
        sig = get_dev_domain_signal(data, "A", ["api"])
        assert sig["runs"] == 2
        assert sig["raw"] == 1.0
        assert sig["rate"] is None  # gated below floor
        assert sig["floor"] == "fail"

    def test_cold_start_no_domain_data_is_no_signal(self):
        data: dict = {"models": {}}
        _record(data, "A", True, domains=["api"], n=3)
        sig = get_dev_domain_signal(data, "A", ["database"])
        assert sig["runs"] == 0
        assert sig["rate"] is None
        assert sig["floor"] == "fail"

    def test_empty_domains_is_no_signal(self):
        data: dict = {"models": {}}
        _record(data, "A", True, domains=["api"], n=3)
        assert get_dev_domain_signal(data, "A", [])["rate"] is None

    def test_per_domain_slice_recorded(self):
        data: dict = {"models": {}}
        _record(data, "A", True, domains=["api"], n=3)
        _record(data, "A", False, domains=["css"], n=3)
        sig = get_dev_domain_signal(data, "A", ["api", "css"])
        # ``resolved_population`` (#2226) reports which concrete versions the
        # slice describes; this fixture records none, so it is empty rather than
        # a fabricated single-model claim.
        assert sig["by_domain"]["api"] == {
            "runs": 3,
            "raw": 1.0,
            "weighted": 1.0,
            "tainted_runs": 0,
            "resolved_population": {},
        }
        assert sig["by_domain"]["css"] == {
            "runs": 3,
            "raw": 0.0,
            "weighted": 0.0,
            "tainted_runs": 0,
            "resolved_population": {},
        }

    def test_recency_window_downweights_stale_history(self):
        # 20 early successes then 5 recent failures: lifetime raw stays high, but
        # the windowed rate drops as the stale successes fall out of the window.
        data: dict = {"models": {}}
        _record(data, "A", True, domains=["api"], n=20)
        _record(data, "A", False, domains=["api"], n=5)
        sig = get_dev_domain_signal(data, "A", ["api"])
        assert sig["runs"] == 25
        assert sig["raw"] == 0.8  # 20/25 lifetime
        # Window is the last 20 outcomes: 15 successes + 5 failures = 0.75.
        assert sig["weighted"] == 0.75
        # The admissible ranked value is the recency-weighted one, not raw.
        assert sig["rate"] == 0.75

    def test_tainted_runs_excluded_from_domain_rate(self):
        # Tainted runs don't influence routing: they are tallied but never move the rate.
        data: dict = {"models": {}}
        _record(data, "A", True, domains=["api"], n=3)
        _record(data, "A", False, domains=["api"], n=5, tainted=True)
        sig = get_dev_domain_signal(data, "A", ["api"])
        assert sig["runs"] == 3  # tainted runs excluded from the sample
        assert sig["tainted_runs"] == 5
        assert sig["raw"] == 1.0
        assert sig["weighted"] == 1.0
        assert sig["rate"] == 1.0  # unaffected by the 5 tainted failures
        assert sig["by_domain"]["api"]["tainted_runs"] == 5

    def test_all_tainted_history_is_cold_start(self):
        # A bucket whose only history is tainted carries no routing weight.
        data: dict = {"models": {}}
        _record(data, "A", True, domains=["api"], n=6, tainted=True)
        sig = get_dev_domain_signal(data, "A", ["api"])
        assert sig["runs"] == 0
        assert sig["tainted_runs"] == 6
        assert sig["rate"] is None
        assert sig["floor"] == "fail"


# ── Assignment: domain match as a tie/preference signal ────────────────


def _cfg(**kwargs) -> AssignmentConfig:
    defaults = dict(
        enabled=True,
        min_reviewers=1,
        max_reviewers=1,
        prefer_cross_provider=False,
        max_cost_per_story_usd=None,
        escalation_memory=True,
    )
    defaults.update(kwargs)
    return AssignmentConfig(**defaults)


def _mid_agent(name: str, model: str, in_cost: float, out_cost: float) -> AgentDef:
    return AgentDef(
        name=name,
        provider="anthropic",
        model=model,
        budget_usd=3.0,
        timeout_seconds=600,
        tier="mid",
        input_cost_per_mtok=in_cost,
        output_cost_per_mtok=out_cost,
    )


_SECRETS = {"ANTHROPIC_API_KEY": "k"}


class TestDomainTiebreak:
    def _tie_agents(self) -> list[AgentDef]:
        # Two mid-tier candidates tied on complexity success rate; mx is cheaper.
        return [
            _mid_agent("mx", "mx-model", 1.0, 1.0),
            _mid_agent("my", "my-model", 5.0, 5.0),
        ]

    def _tie_profiles(self) -> dict:
        data: dict = {"models": {}}
        # Both models: identical medium complexity rate 1.0 (floor passes).
        _record(data, "mx", True, domains=[], n=3)
        _record(data, "my", True, domains=["api"], n=3)  # my is strong at api
        return data

    def test_domain_breaks_tie_within_eligible_pool(self):
        agents = self._tie_agents()
        profiles = self._tie_profiles()
        # Without domains: complexity tie → cheaper mx wins on price.
        d0 = assign_models(
            agents,
            _cfg(),
            complexity="MEDIUM",
            complexity_score=5,
            model_profiles=profiles,
            secrets=_SECRETS,
        )
        assert d0.dev.model == "mx-model"
        # With the api domain: my's admissible api rate breaks the tie.
        d1 = assign_models(
            agents,
            _cfg(),
            complexity="MEDIUM",
            complexity_score=5,
            model_profiles=profiles,
            secrets=_SECRETS,
            domains=["api"],
        )
        assert d1.dev.model == "my-model"

    def test_routing_decision_records_domain_influence(self):
        agents = self._tie_agents()
        profiles = self._tie_profiles()
        d1 = assign_models(
            agents,
            _cfg(),
            complexity="MEDIUM",
            complexity_score=5,
            model_profiles=profiles,
            secrets=_SECRETS,
            domains=["api"],
        )
        dm = d1.routing_decision["dev"]["domain_match"]
        assert dm["domains"] == ["api"]
        assert dm["influenced"] is True
        assert dm["complexity_only_head"] == "mx"
        assert dm["domain_head"] == "my"
        # Matching profile slice for the selected model records the full
        # admissibility surface: sample count, floor, and raw + weighted values.
        slice_ = dm["selected_model_slice"]
        assert slice_["rate"] == 1.0
        assert slice_["runs"] == 3
        assert slice_["floor"] == "pass"
        assert slice_["raw"] == 1.0
        assert slice_["weighted"] == 1.0
        assert slice_["tainted_runs"] == 0
        # Per-candidate domain slice is attached to the pool entry too.
        pool = {e["name"]: e for e in d1.routing_decision["dev"]["candidate_pool"]}
        assert pool["my"]["signals"]["domain"]["rate"] == 1.0
        assert pool["my"]["signals"]["domain"]["weighted"] == 1.0
        assert pool["my"]["signals"]["domain"]["floor"] == "pass"

    def test_no_domains_omits_domain_match_block(self):
        agents = self._tie_agents()
        profiles = self._tie_profiles()
        d0 = assign_models(
            agents,
            _cfg(),
            complexity="MEDIUM",
            complexity_score=5,
            model_profiles=profiles,
            secrets=_SECRETS,
        )
        assert "domain_match" not in d0.routing_decision["dev"]

    def test_domain_present_but_non_influential_marked(self):
        # my has api data, mx has none, but mx has the STRICTLY higher complexity
        # rate, so the vertical axis decides and domain must not override it.
        agents = self._tie_agents()
        data: dict = {"models": {}}
        _record(data, "mx", True, domains=[], n=4)  # mx rate 1.0
        _record(data, "my", True, domains=["api"], n=2)
        _record(data, "my", False, domains=["api"], n=2)  # my rate 0.5, api rate 0.5
        d = assign_models(
            agents,
            _cfg(),
            complexity="MEDIUM",
            complexity_score=5,
            model_profiles=data,
            secrets=_SECRETS,
            domains=["api"],
        )
        # mx wins on complexity rate; domain did not change the selection.
        assert d.dev.model == "mx-model"
        dm = d.routing_decision["dev"]["domain_match"]
        assert dm["influenced"] is False

    def test_tainted_domain_history_does_not_sway_assignment(self):
        # my's ONLY api evidence is tainted, so it carries no admissible domain
        # signal; the complexity tie must fall through to price (cheaper mx wins)
        # exactly as if my had no api history at all.
        agents = self._tie_agents()  # mx cheaper, my pricier
        data: dict = {"models": {}}
        _record(data, "mx", True, domains=[], n=3)  # complexity 1.0
        _record(data, "my", True, domains=[], n=3)  # complexity 1.0 (tie)
        _record(data, "my", True, domains=["api"], n=5, tainted=True)  # tainted api only
        d = assign_models(
            agents,
            _cfg(),
            complexity="MEDIUM",
            complexity_score=5,
            model_profiles=data,
            secrets=_SECRETS,
            domains=["api"],
        )
        assert d.dev.model == "mx-model"
        dm = d.routing_decision["dev"]["domain_match"]
        assert dm["influenced"] is False
        # The tainted evidence is visible but non-influential.
        assert dm["selected_model_slice"] is None or dm["selected_model_slice"]["rate"] is None
        my_slice = {e["name"]: e for e in d.routing_decision["dev"]["candidate_pool"]}["my"]
        assert my_slice["signals"]["domain"]["tainted_runs"] == 5
        assert my_slice["signals"]["domain"]["rate"] is None


class TestColdStartNotPenalized:
    def test_no_domain_data_keeps_complexity_standing(self):
        # zed has the best complexity rate but no domain data; it must still win
        # over a domain-strong-but-weaker-complexity model.
        agents = [
            _mid_agent("zed", "zed-model", 1.0, 1.0),
            _mid_agent("dom", "dom-model", 1.0, 1.0),
        ]
        data: dict = {"models": {}}
        _record(data, "zed", True, domains=[], n=4)  # complexity rate 1.0, no domain
        _record(data, "dom", True, domains=["api"], n=2)
        _record(data, "dom", False, domains=["api"], n=2)  # complexity 0.5, api 0.5
        d = assign_models(
            agents,
            _cfg(),
            complexity="MEDIUM",
            complexity_score=5,
            model_profiles=data,
            secrets=_SECRETS,
            domains=["api"],
        )
        assert d.dev.model == "zed-model"


# ── Seam: state.preflight_domains flows into telemetry + routing ───────


class TestDomainSeam:
    def test_bridge_carries_domains_into_profiles_then_routing(self, tmp_path: Path):
        """Domains recorded on state flow through RunOutcome into per-domain
        aggregation, and the aggregated slice then moves a routing tie."""
        from theforge.config import (
            DEFAULT_DEV_PROFILE,
            DEFAULT_PREFLIGHT_PROFILE,
            DEFAULT_REVIEW_PROFILE,
            DEFAULT_VALIDATION,
            ForgeConfig,
            ModelProfile,
            RetryPolicy,
            WorkspaceConfig,
        )
        from theforge.coordinator.model_profiles_bridge import build_run_outcome
        from theforge.coordinator.state import CoordinatorState

        dev_profile = ModelProfile(
            name="my",
            cli=None,
            provider="anthropic",
            model="my-model",
            budget_usd=3.0,
            timeout_seconds=600,
            allowed_tools=DEFAULT_DEV_PROFILE.allowed_tools,
        )
        config = ForgeConfig(
            project="t",
            project_root=tmp_path,
            workspace=WorkspaceConfig(
                create_command="mkdir -p {slug}",
                path_pattern="{slug}",
                branch_pattern="forge/{slug}",
            ),
            validation=DEFAULT_VALIDATION,
            dev_profile=dev_profile,
            preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
            review_pool=[DEFAULT_REVIEW_PROFILE],
            synthesis_profile=None,
            retry=RetryPolicy(),
        )
        data: dict = {"models": {}}
        for _ in range(3):
            state = CoordinatorState()
            state.preflight_complexity = "medium"
            state.preflight_complexity_score = 5
            state.preflight_domains = ["api"]
            outcome = build_run_outcome(config, state, success=True)
            assert outcome.domains == ["api"]
            apply_run(data, outcome)

        sig = get_dev_domain_signal(
            data, "my", ["api"], actual_model="my-model", provider="anthropic"
        )
        assert sig["rate"] == 1.0
        assert sig["runs"] == 3
