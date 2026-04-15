from src.theforge.assignment import assign_models
from src.theforge.config import AgentDef, AssignmentConfig

def test_budget_downgrade():
    agents = [
        AgentDef("haiku", "anthropic", "haiku", 1.0, 300, "cheap"),
        AgentDef("sonnet", "anthropic", "sonnet", 500.0, 900, "mid"),
        AgentDef("opus", "anthropic", "opus", 800.0, 1200, "strong"),
    ]
    cfg = AssignmentConfig(
        min_reviewers=1,
        max_reviewers=1,
        budget_per_story_usd=100.0,
        prefer_cross_provider=False,
    )
    decision = assign_models(agents, cfg, "medium")
    print(f"Dev model: {decision.dev.name}")

test_budget_downgrade()
