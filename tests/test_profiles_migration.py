"""Canonical-ID migration for model_profiles.yaml + assignment_history.yaml.

Covers the contract on issue #1291:

* Fragmented sonnet aliases (``claude-sonnet`` + ``sonnet-cli``) merge into
  one canonical entry (``anthropic/sonnet/cli``) with combined accumulators.
* Fragmented gpt-5.4 CLI aliases merge into one canonical entry with combined
  accumulators (CLI + API legacy keys remain distinct because transport is
  part of the canonical identity per AC 1).
* Ambiguous storage keys (role-shaped names like ``dev``, ``preflight``,
  ``gemini-reviewer``, ``codex-cli``) are flagged in the report and left in
  place rather than guessed at.
* Migration is idempotent — re-running converges to the same state.
* Behaviour-equivalence: the routing decision computed from a fragmented
  pre-migration profile fixture matches the decision computed from its
  post-migration canonical equivalent.
"""

from __future__ import annotations

from theforge.assignment import (
    AssignmentConfig,
    assign_models,
)
from theforge.config import AgentDef
from theforge.model_profiles import (
    canonical_id_for_legacy_key,
    canonical_id_from_identity,
    get_dev_success_rate,
    migrate_history_data,
    migrate_profiles_data,
)

# ── canonical_id_from_identity ──────────────────────────────────────────


def test_canonical_id_from_identity_cli():
    assert (
        canonical_id_from_identity(actual_model="sonnet", provider=None, cli="claude")
        == "anthropic/sonnet/cli"
    )


def test_canonical_id_from_identity_api():
    assert (
        canonical_id_from_identity(actual_model="gpt-5.4", provider="openai", cli=None)
        == "openai/gpt-5.4/api"
    )


def test_canonical_id_from_identity_returns_none_when_thin():
    assert canonical_id_from_identity(actual_model=None, provider=None, cli=None) is None
    assert canonical_id_from_identity(actual_model="dev", provider=None, cli=None) is None


# ── canonical_id_for_legacy_key ────────────────────────────────────────


def test_legacy_key_resolves_via_registry():
    # `claude/sonnet` is already a registry slot; canonical adds transport.
    assert canonical_id_for_legacy_key("claude/sonnet") == "anthropic/sonnet/cli"


def test_legacy_key_resolves_via_dash_prefix():
    assert canonical_id_for_legacy_key("claude-sonnet") == "anthropic/sonnet/cli"
    assert canonical_id_for_legacy_key("claude-opus") == "anthropic/opus/cli"


def test_legacy_key_resolves_via_cli_suffix():
    # `sonnet-cli` is unique once we constrain transport to cli.
    assert canonical_id_for_legacy_key("sonnet-cli") == "anthropic/sonnet/cli"


def test_legacy_key_resolves_via_provider_model_concatenation():
    assert (
        canonical_id_for_legacy_key("deepseek-deepseek-reasoner")
        == "deepseek/deepseek-reasoner/api"
    )


def test_already_canonical_passes_through():
    assert canonical_id_for_legacy_key("anthropic/sonnet/cli") == "anthropic/sonnet/cli"


def test_role_shaped_keys_are_ambiguous():
    assert canonical_id_for_legacy_key("dev") is None
    assert canonical_id_for_legacy_key("preflight") is None
    assert canonical_id_for_legacy_key("gemini-reviewer") is None
    # `codex-cli` is the codex binary name with no model attached.
    assert canonical_id_for_legacy_key("codex-cli") is None


def test_identity_metadata_takes_precedence():
    entry = {"_identity": {"provider": "anthropic", "model": "sonnet", "cli": "claude"}}
    assert canonical_id_for_legacy_key("legacy-name", entry) == "anthropic/sonnet/cli"


# ── migrate_profiles_data ──────────────────────────────────────────────


def _fragmented_sonnet_fixture() -> dict:
    return {
        "models": {
            "claude-sonnet": {
                "dev": {
                    "runs": 5,
                    "_successes": 2,
                    "_iterations_sum": 5.0,
                    "_cost_sum": 0.74,
                    "success_rate": 0.4,
                    "avg_iterations": 1.0,
                    "avg_cost_usd": 0.148,
                    "by_complexity": {
                        "small": {
                            "runs": 5,
                            "_successes": 2,
                            "_iterations_sum": 5.0,
                            "_cost_sum": 0.74,
                            "success_rate": 0.4,
                            "avg_iterations": 1.0,
                            "avg_cost_usd": 0.148,
                        }
                    },
                }
            },
            "sonnet-cli": {
                "dev": {
                    "runs": 7,
                    "_successes": 8,
                    "_iterations_sum": 9.0,
                    "_cost_sum": 0.0,
                    "success_rate": round(8 / 7, 4),
                    "avg_iterations": round(9 / 7, 4),
                    "avg_cost_usd": 0.0,
                    "by_complexity": {
                        "medium": {
                            "runs": 4,
                            "_successes": 3,
                            "_iterations_sum": 5.0,
                            "_cost_sum": 0.0,
                            "success_rate": 0.75,
                            "avg_iterations": 1.25,
                            "avg_cost_usd": 0.0,
                        },
                        "large": {
                            "runs": 3,
                            "_successes": 2,
                            "_iterations_sum": 4.0,
                            "_cost_sum": 0.0,
                            "success_rate": 0.6667,
                            "avg_iterations": 1.3333,
                            "avg_cost_usd": 0.0,
                        },
                    },
                }
            },
        }
    }


def test_migrate_merges_fragmented_sonnet_aliases():
    data = _fragmented_sonnet_fixture()
    migrated, report = migrate_profiles_data(data)

    assert "claude-sonnet" not in migrated["models"]
    assert "sonnet-cli" not in migrated["models"]
    canonical = migrated["models"]["anthropic/sonnet/cli"]
    dev = canonical["dev"]
    assert dev["runs"] == 12
    assert dev["_successes"] == 10
    assert dev["_iterations_sum"] == 14.0
    assert round(dev["_cost_sum"], 4) == 0.74
    by = dev["by_complexity"]
    assert by["small"]["runs"] == 5
    assert by["small"]["_successes"] == 2
    assert by["medium"]["runs"] == 4
    assert by["medium"]["_successes"] == 3
    assert by["large"]["runs"] == 3
    assert by["large"]["_successes"] == 2

    # Report is auditable.
    [merge] = [r for r in report if r.get("canonical_id") == "anthropic/sonnet/cli"]
    keys = {src["key"] for src in merge["merged_from"]}
    assert keys == {"claude-sonnet", "sonnet-cli"}
    assert merge["combined"]["runs"] == 12


def test_migrate_merges_fragmented_gpt54_cli_aliases():
    # Two legacy storage names that both resolve to the same CLI canonical.
    data = {
        "models": {
            "openai-gpt-5.4": {
                "dev": {
                    "runs": 4,
                    "_successes": 2,
                    "_iterations_sum": 4.0,
                    "_cost_sum": 1.0,
                    "by_complexity": {
                        "medium": {
                            "runs": 4,
                            "_successes": 2,
                            "_iterations_sum": 4.0,
                            "_cost_sum": 1.0,
                        }
                    },
                }
            },
            "gpt-5.4-cli": {
                "dev": {
                    "runs": 6,
                    "_successes": 5,
                    "_iterations_sum": 6.0,
                    "_cost_sum": 2.5,
                    "by_complexity": {
                        "large": {
                            "runs": 6,
                            "_successes": 5,
                            "_iterations_sum": 6.0,
                            "_cost_sum": 2.5,
                        }
                    },
                }
            },
        }
    }
    migrated, _report = migrate_profiles_data(data)
    canonical = migrated["models"]["openai/gpt-5.4/cli"]
    dev = canonical["dev"]
    assert dev["runs"] == 10
    assert dev["_successes"] == 7
    assert dev["by_complexity"]["medium"]["runs"] == 4
    assert dev["by_complexity"]["large"]["runs"] == 6


def test_migrate_keeps_cli_and_api_canonicals_distinct():
    # Per AC 1, transport is part of canonical identity; CLI and API of the
    # same provider+model resolve to different canonicals and must NOT merge.
    data = {
        "models": {
            "openai-gpt-5.4": {  # codex CLI canonical
                "dev": {"runs": 3, "_successes": 1, "_iterations_sum": 3.0, "_cost_sum": 0.5}
            },
            "openai-api-gpt-5.4": {  # openai API canonical
                "dev": {"runs": 5, "_successes": 4, "_iterations_sum": 5.0, "_cost_sum": 2.0}
            },
        }
    }
    migrated, _ = migrate_profiles_data(data)
    assert migrated["models"]["openai/gpt-5.4/cli"]["dev"]["runs"] == 3
    assert migrated["models"]["openai/gpt-5.4/api"]["dev"]["runs"] == 5


def test_migrate_reports_ambiguous_keys_and_leaves_them_in_place():
    data = {
        "models": {
            "dev": {"dev": {"runs": 10, "_successes": 5}},
            "preflight": {"preflight": {"runs": 3, "_cost_sum": 0.5}},
            "gemini-reviewer": {"review": {"runs": 4, "_findings_sum": 8.0, "_cost_sum": 1.0}},
            "codex-cli": {"dev": {"runs": 2, "_successes": 0}},
        }
    }
    migrated, report = migrate_profiles_data(data)
    # Ambiguous keys remain unchanged.
    for k in ("dev", "preflight", "gemini-reviewer", "codex-cli"):
        assert k in migrated["models"]
    ambiguous_keys = {r["ambiguous_key"] for r in report if "ambiguous_key" in r}
    assert ambiguous_keys == {"dev", "preflight", "gemini-reviewer", "codex-cli"}


def test_migrate_is_idempotent():
    data = _fragmented_sonnet_fixture()
    once, _ = migrate_profiles_data(data)
    twice, report_twice = migrate_profiles_data(once)
    # Second pass produces no merges (already canonical).
    assert all("canonical_id" not in r for r in report_twice)
    assert once == twice


def test_migrate_partial_data_converges():
    # One alias already merged, one still legacy — second pass merges the rest.
    data = {
        "models": {
            "anthropic/sonnet/cli": {
                "_identity": {"provider": "anthropic", "model": "sonnet", "transport": "cli"},
                "dev": {
                    "runs": 5,
                    "_successes": 2,
                    "_iterations_sum": 5.0,
                    "_cost_sum": 0.74,
                    "by_complexity": {
                        "small": {
                            "runs": 5,
                            "_successes": 2,
                            "_iterations_sum": 5.0,
                            "_cost_sum": 0.74,
                        }
                    },
                },
            },
            "sonnet-cli": {
                "dev": {
                    "runs": 7,
                    "_successes": 8,
                    "_iterations_sum": 9.0,
                    "_cost_sum": 0.0,
                    "by_complexity": {
                        "medium": {
                            "runs": 4,
                            "_successes": 3,
                            "_iterations_sum": 5.0,
                            "_cost_sum": 0.0,
                        }
                    },
                },
            },
        }
    }
    migrated, _ = migrate_profiles_data(data)
    dev = migrated["models"]["anthropic/sonnet/cli"]["dev"]
    assert dev["runs"] == 12
    assert dev["_successes"] == 10


# ── migrate_history_data ───────────────────────────────────────────────


def test_migrate_history_canonicalizes_dev_model_field():
    data = {
        "escalations": [
            {
                "story": "a",
                "complexity": "MEDIUM",
                "dev_model": "claude-sonnet",
                "outcome": "DONE",
            },  # noqa: E501
            {
                "story": "b",
                "complexity": "LARGE",
                "dev_model": "sonnet-cli",
                "outcome": "ESCALATE",
            },  # noqa: E501
            {"story": "c", "complexity": "LARGE", "dev_model": "dev", "outcome": "DONE"},
        ]
    }
    migrated, report = migrate_history_data(data)
    records = migrated["escalations"]
    assert records[0]["dev_model"] == "anthropic/sonnet/cli"
    assert records[1]["dev_model"] == "anthropic/sonnet/cli"
    # ``dev`` is ambiguous and is preserved.
    assert records[2]["dev_model"] == "dev"
    canonicalized = [r for r in report if "to" in r]
    assert len(canonicalized) == 2
    [ambiguous] = [r for r in report if "ambiguous_key" in r]
    assert ambiguous["ambiguous_key"] == "dev"


def test_migrate_history_is_idempotent():
    data = {
        "escalations": [
            {
                "story": "a",
                "complexity": "MEDIUM",
                "dev_model": "claude-sonnet",
                "outcome": "DONE",
            },  # noqa: E501
        ]
    }
    once, _ = migrate_history_data(data)
    twice, report = migrate_history_data(once)
    assert once == twice
    assert report == []


# ── Behaviour equivalence: routing decision is preserved across migration ──


def _agent_pool() -> list[AgentDef]:
    return [
        AgentDef(
            name="sonnet",
            provider="anthropic",
            model="sonnet",
            cli="claude",
            budget_usd=2.0,
            timeout_seconds=600,
            tier="mid",
        ),
        AgentDef(
            name="opus",
            provider="anthropic",
            model="opus",
            cli="claude",
            budget_usd=10.0,
            timeout_seconds=900,
            tier="strong",
        ),
        AgentDef(
            name="gpt-5.4-mini",
            provider="openai",
            model="gpt-5.4-mini",
            cli="codex",
            budget_usd=1.0,
            timeout_seconds=600,
            tier="cheap",
        ),
    ]


def test_routing_decision_matches_pre_and_post_migration():
    """Pre- and post-migration profile fixtures must produce the same dev pick.

    This is the load-bearing regression: without it a migration could pass
    syntactic checks while producing different routing decisions than the
    pre-migration data warranted.
    """
    fragmented = _fragmented_sonnet_fixture()
    canonical, _ = migrate_profiles_data(fragmented)

    # With ``claude`` CLI metadata both fixtures should report the same
    # success rate for sonnet at small complexity (combined: 2/5 == 0.4 wait
    # no, 2 + (small not present in sonnet-cli) = 2/5). Actually the small
    # bucket only exists in claude-sonnet, but the function reads across all
    # entries that resolve to the same canonical identity, so the pre and
    # post-migration fixtures must agree.
    pre = get_dev_success_rate(
        fragmented,
        "claude-sonnet",
        "small",
        actual_model="sonnet",
        cli="claude",
    )
    post = get_dev_success_rate(
        canonical,
        "claude-sonnet",
        "small",
        actual_model="sonnet",
        cli="claude",
    )
    assert pre == post

    # Routing decision check: dev model selection at the small band should be
    # identical against both fixtures (rerank-by-profile is the only thing
    # that can drift, and it must read the same combined data either way).
    cfg = AssignmentConfig(
        enabled=True,
        min_reviewers=1,
        max_reviewers=2,
        prefer_cross_provider=True,
        max_cost_per_story_usd=50.0,
        escalation_memory=False,
        adaptive_enabled=True,
    )
    agents = _agent_pool()
    decision_pre = assign_models(
        agents,
        cfg,
        complexity="small",
        complexity_score=3,
        model_profiles=fragmented,
    )
    decision_post = assign_models(
        agents,
        cfg,
        complexity="small",
        complexity_score=3,
        model_profiles=canonical,
    )
    assert decision_pre.dev.model == decision_post.dev.model
    assert decision_pre.dev.provider == decision_post.dev.provider
