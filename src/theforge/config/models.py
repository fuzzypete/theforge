"""Model registry, ModelInfo, and model-related helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .types import ApiFallbackConfig, AssignmentConfig, ModelProfile


@dataclass(frozen=True)
class ModelInfo:
    """Built-in metadata for a known model."""

    cli: str  # "claude", "codex", "gemini"
    model: str  # model identifier for the CLI
    tier: str  # "fast" or "strong"
    capability: int  # relative capability score (1-10)
    cost_rank: int  # 1=cheap, 2=moderate, 3=expensive
    dev_capable: bool = True  # False for models whose CLI doesn't support dev tools


@dataclass(frozen=True)
class AgentDef:
    """An agent available in the pool for adaptive model assignment."""

    name: str
    provider: str | None  # "anthropic", "openai", etc. — None for CLI agents
    model: str
    budget_usd: float
    timeout_seconds: int
    tier: str  # "cheap" | "mid" | "strong"
    cli: str | None = None  # "claude", "codex", etc. — set for CLI agents
    api_fallback: ApiFallbackConfig | None = None
    strengths: tuple[str, ...] = ()

    def to_model_profile(self, *, allowed_tools: tuple[str, ...] = ()) -> ModelProfile:
        """Convert to a ModelProfile for use in coordinator config."""
        return ModelProfile(
            name=self.name,
            cli=self.cli,
            provider=self.provider,
            model=self.model,
            budget_usd=self.budget_usd,
            timeout_seconds=self.timeout_seconds,
            allowed_tools=allowed_tools,
            api_fallback=self.api_fallback,
        )


MODEL_REGISTRY: dict[str, ModelInfo] = {
    "claude/sonnet": ModelInfo(
        cli="claude", model="sonnet", tier="fast", capability=7, cost_rank=1
    ),
    "claude/opus": ModelInfo(
        cli="claude", model="opus", tier="strong", capability=10, cost_rank=3
    ),
    "openai/gpt-5.4": ModelInfo(
        cli="codex", model="gpt-5.4", tier="strong", capability=9, cost_rank=2
    ),
    "google/gemini-2.5-pro": ModelInfo(
        cli="gemini",
        model="gemini-2.5-pro",
        tier="strong",
        capability=8,
        cost_rank=2,
        dev_capable=False,
    ),
    # Local models via ollama/vllm — route through the OpenAI adapter using base_url
    "openai/codestral": ModelInfo(
        cli="codex", model="codestral", tier="fast", capability=7, cost_rank=1
    ),
    "openai/deepseek-coder": ModelInfo(
        cli="codex", model="deepseek-coder", tier="fast", capability=7, cost_rank=1
    ),
    "openai/llama3.1": ModelInfo(
        cli="codex", model="llama3.1", tier="fast", capability=6, cost_rank=1
    ),
    "openai/qwen2.5-coder": ModelInfo(
        cli="codex", model="qwen2.5-coder", tier="fast", capability=7, cost_rank=1
    ),
}

# Maps provider prefix → CLI name
_PROVIDER_CLI_MAP: dict[str, str] = {
    "claude": "claude",
    "openai": "codex",
    "google": "gemini",
}


def _resolve_model_info(model_key: str) -> ModelInfo:
    """Resolve a model key to ModelInfo; unknown models get sensible defaults."""
    if model_key in MODEL_REGISTRY:
        return MODEL_REGISTRY[model_key]
    parts = model_key.split("/", 1)
    cli = _PROVIDER_CLI_MAP.get(parts[0], parts[0]) if len(parts) == 2 else model_key
    model = parts[1] if len(parts) == 2 else model_key
    return ModelInfo(cli=cli, model=model, tier="strong", capability=5, cost_rank=2)


def _planner_candidate_models(agents: list[AgentDef]) -> set[str]:
    """Return model names the adaptive planner can select at runtime.

    Mirrors assign_models planner selection logic (PHASE_TIER["plan"]):
      LOW → mid tier (fallback: highest-budget agent if no mid agents)
      MEDIUM/HIGH → strong tier (fallback: highest-budget agent if no strong agents)

    Auth filtering is intentionally omitted: auth state can change at runtime,
    so the static check only considers structural availability.
    """
    if not agents:
        return set()

    # PHASE_TIER["plan"] = {"LOW": "mid", "MEDIUM": "strong", "HIGH": "strong"}
    planner_tiers = {"LOW": "mid", "MEDIUM": "strong", "HIGH": "strong"}
    highest_budget = sorted(agents, key=lambda a: -a.budget_usd)[0]

    candidate_models: set[str] = set()
    for tier in planner_tiers.values():
        tier_agents = sorted([a for a in agents if a.tier == tier], key=lambda a: a.budget_usd)
        if tier_agents:
            candidate_models.add(tier_agents[0].model)
        else:
            # Mirror assign_models fallback: pick highest-budget agent
            candidate_models.add(highest_budget.model)

    return candidate_models


def _parse_agents(agents_raw: list[Any]) -> list[AgentDef]:
    """Parse agents pool from raw YAML list."""
    agents_list: list[AgentDef] = []
    _VALID_TIERS = {"cheap", "mid", "strong"}
    for agent_data in agents_raw:
        if not isinstance(agent_data, dict):
            raise ValueError(f"Each 'agents' entry must be a dict, got {type(agent_data)}")
        agent_name = agent_data.get("name")
        if not agent_name:
            raise ValueError("Each 'agents' entry must have a 'name' field")
        agent_tier = str(agent_data.get("tier", "mid"))
        if agent_tier not in _VALID_TIERS:
            raise ValueError(
                f"Agent {agent_name!r}: tier must be one of {sorted(_VALID_TIERS)}, "
                f"got {agent_tier!r}"
            )
        agent_cli = agent_data.get("cli")
        agent_provider = agent_data.get("provider")
        if not agent_cli and not agent_provider:
            agent_provider = "anthropic"  # default for backward compat
        agents_list.append(
            AgentDef(
                name=str(agent_name),
                cli=str(agent_cli) if agent_cli else None,
                provider=str(agent_provider) if agent_provider else None,
                model=str(agent_data.get("model", "sonnet")),
                budget_usd=float(agent_data.get("budget_usd", 1.0)),
                timeout_seconds=int(agent_data.get("timeout_seconds", 300)),
                tier=agent_tier,
                strengths=tuple(agent_data.get("strengths", [])),
            )
        )
    return agents_list


def _parse_assignment(assignment_raw: dict[str, Any]) -> AssignmentConfig:
    """Parse assignment config from raw YAML dict."""
    return AssignmentConfig(
        enabled=bool(assignment_raw.get("enabled", False)),
        min_reviewers=int(assignment_raw.get("min_reviewers", 1)),
        max_reviewers=int(assignment_raw.get("max_reviewers", 3)),
        prefer_cross_provider=bool(assignment_raw.get("prefer_cross_provider", True)),
        budget_per_story_usd=float(assignment_raw.get("budget_per_story_usd", 15.0)),
        escalation_memory=bool(assignment_raw.get("escalation_memory", True)),
    )
