"""The signals routing consults, read straight from stored profile state (#2467).

These exercise :mod:`theforge.model_profiles_read_model` — the half of the old
``model_profiles`` module that answers "what does routing see", as opposed to
:mod:`theforge.model_profiles_storage`, which answers "how does a finished run
become stored history". Every test here builds the stored shape it reads as a
plain dict and calls one signal: no ``RunOutcome`` is constructed and no
``apply_run`` is applied to produce the state, which is exactly the
independence the split buys.

Signals reached through an actual accumulation — ``build_run_outcome`` →
``apply_run`` → signal — stay in ``test_model_profiles.py``; that seam is real
behaviour and is still covered there. The ownership boundary itself is pinned by
``test_model_profiles_boundary.py``.
"""

from __future__ import annotations

from theforge.model_profiles_read_model import (
    get_dev_complexity_stats,
    get_dev_success_rate,
    get_review_signal,
)


def test_get_review_signal_cold_start_below_min_runs_is_none():
    profiles = {
        "models": {
            "openai/gpt/api": {
                "_identity": {"provider": "openai", "model": "gpt", "transport": "api"},
                "review": {
                    "_attempted_count": 3,
                    "_completed_count": 3,
                    "completion_rate": 1.0,
                    "_completion_recent": [1, 1, 1],
                },
            }
        }
    }
    signal = get_review_signal(
        profiles, "rev", min_runs=5, actual_model="gpt", provider="openai", cli=None
    )
    assert signal["floor"] == "fail"
    assert signal["rate"] is None
    assert signal["attempted"] == 3


def test_get_review_signal_above_min_runs_returns_weighted_rate():
    profiles = {
        "models": {
            "openai/gpt/api": {
                "_identity": {"provider": "openai", "model": "gpt", "transport": "api"},
                "review": {
                    "_attempted_count": 8,
                    "_completed_count": 4,
                    "completion_rate": 0.5,
                    "_completion_recent": [1, 0, 1, 0, 1, 0, 1, 0],
                },
            }
        }
    }
    signal = get_review_signal(
        profiles, "rev", min_runs=5, actual_model="gpt", provider="openai", cli=None
    )
    assert signal["floor"] == "pass"
    assert signal["raw"] == 0.5
    assert signal["rate"] is not None
    assert signal["completed"] == 4
    assert signal["attempted"] == 8


def test_get_dev_success_rate_requires_min_runs():
    profiles = {
        "models": {
            "sonnet": {
                "dev": {
                    "runs": 2,
                    "success_rate": 1.0,
                    "by_complexity": {
                        "medium": {"runs": 2, "success_rate": 1.0},
                    },
                }
            },
            "opus": {
                "dev": {
                    "runs": 10,
                    "success_rate": 0.8,
                    "by_complexity": {
                        "medium": {"runs": 10, "success_rate": 0.8},
                    },
                }
            },
        }
    }
    # sonnet has fewer runs than default min_runs=3 → None
    assert get_dev_success_rate(profiles, "sonnet", "medium") is None
    assert get_dev_success_rate(profiles, "opus", "medium") == 0.8
    # Unknown model / role → None
    assert get_dev_success_rate(profiles, "gemini", "medium") is None


def test_get_dev_success_rate_aggregates_fragmented_aliases():
    profiles = {
        "models": {
            "claude-sonnet": {
                "dev": {
                    "by_complexity": {
                        "small": {"runs": 2, "success_rate": 0.5},
                    }
                }
            },
            "sonnet-cli": {
                "dev": {
                    "by_complexity": {
                        "small": {"runs": 3, "success_rate": round(2 / 3, 4)},
                    }
                }
            },
            "openai-gpt-5.4": {
                "dev": {
                    "by_complexity": {
                        "medium": {"runs": 2, "success_rate": 0.5},
                    }
                }
            },
            "openai-api-gpt-5.4": {
                "dev": {
                    "by_complexity": {
                        "medium": {"runs": 2, "success_rate": 1.0},
                    }
                }
            },
        }
    }

    assert (
        get_dev_success_rate(
            profiles,
            "claude-sonnet",
            "small",
            actual_model="sonnet",
            cli="claude",
        )
        == 0.6
    )
    assert (
        get_dev_success_rate(
            profiles,
            "openai-gpt-5.4",
            "medium",
            actual_model="gpt-5.4",
            provider="openai",
            cli="codex",
        )
        == 0.75
    )


def test_get_dev_complexity_stats_requires_band_averages():
    profiles = {
        "models": {
            "sonnet": {
                "dev": {
                    "by_complexity": {
                        "medium": {
                            "runs": 3,
                            "avg_iterations": 2.5,
                            "avg_cost_usd": 1.25,
                        }
                    }
                }
            }
        }
    }
    assert get_dev_complexity_stats(profiles, "sonnet", "medium") == {
        "runs": 3.0,
        "avg_iterations": 2.5,
        "avg_cost_usd": 1.25,
        "cost_measured_runs": 3.0,
        "max_duration_s": 0.0,
        "duration_runs": 0.0,
        "max_killed_timeout_s": 0.0,
    }


def test_get_dev_complexity_stats_aggregates_fragmented_aliases():
    profiles = {
        "models": {
            "claude-sonnet": {
                "dev": {
                    "by_complexity": {
                        "medium": {
                            "runs": 2,
                            "avg_iterations": 3.0,
                            "avg_cost_usd": 1.0,
                        }
                    }
                }
            },
            "sonnet-cli": {
                "dev": {
                    "by_complexity": {
                        "medium": {
                            "runs": 3,
                            "avg_iterations": 5.0,
                            "avg_cost_usd": 2.0,
                        }
                    }
                }
            },
        }
    }

    assert get_dev_complexity_stats(
        profiles,
        "claude-sonnet",
        "medium",
        actual_model="sonnet",
        cli="claude",
    ) == {
        "runs": 5.0,
        "avg_iterations": 4.2,
        "avg_cost_usd": 1.6,
        "cost_measured_runs": 5.0,
        "max_duration_s": 0.0,
        "duration_runs": 0.0,
        "max_killed_timeout_s": 0.0,
    }


def test_get_dev_complexity_stats_returns_none_under_min_runs():
    profiles = {
        "models": {
            "sonnet": {
                "dev": {
                    "by_complexity": {
                        "medium": {
                            "runs": 2,
                            "avg_iterations": 2.5,
                            "avg_cost_usd": 1.25,
                        }
                    }
                }
            }
        }
    }
    assert get_dev_complexity_stats(profiles, "sonnet", "medium") is None


def test_legacy_profile_without_duration_still_yields_iteration_stats():
    """Profiles predating the duration fields must still return iteration stats
    (duration/kill floors default to 0.0, they do not void the result)."""
    profiles = {
        "models": {
            "sonnet": {
                "dev": {
                    "by_complexity": {
                        "medium": {
                            "runs": 4,
                            "avg_iterations": 3.0,
                            "avg_cost_usd": 1.0,
                        }
                    }
                }
            }
        }
    }
    stats = get_dev_complexity_stats(profiles, "sonnet", "medium", min_runs=1)
    assert stats is not None
    assert stats["avg_iterations"] == 3.0
    assert stats["max_duration_s"] == 0.0
    assert stats["duration_runs"] == 0.0
    assert stats["max_killed_timeout_s"] == 0.0
