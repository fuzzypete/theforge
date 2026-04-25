import sys
from pathlib import Path
from theforge.config import AgentDef, AssignmentConfig, ModelProfile
from theforge.coordinator.state import CoordinatorState
from theforge.coordinator.preflight import _apply_preflight_config
from tests.coord_test_helpers import _make_config
from dataclasses import replace

def main():
    tmp_path = Path(".")
    config = _make_config(tmp_path)
    
    agents = [
        AgentDef(name="cheap-agent", model="cheap-model", tier="cheap", budget_usd=1.0, provider="anthropic", cli="claude", timeout_seconds=300),
        AgentDef(name="mid-agent", model="mid-model", tier="mid", budget_usd=5.0, provider="anthropic", cli="claude", timeout_seconds=300),
        AgentDef(name="strong-agent", model="strong-model", tier="strong", budget_usd=10.0, provider="anthropic", cli="claude", timeout_seconds=300),
    ]
    
    assignment = AssignmentConfig(
        enabled=True,
        adaptive_enabled=True,
        budget_per_story_usd=10.0,
    )
    
    # Explicit overrides
    plan_model = "expensive-planner"
    new_plan = replace(config.plan, model=plan_model)
    
    review_pool = [
        ModelProfile(name="expensive-reviewer", model="expensive-reviewer", budget_usd=50.0, provider="anthropic", cli="claude", timeout_seconds=300, allowed_tools=())
    ]
    config = replace(config, plan=new_plan, plan_model_is_default=False, review_pool=review_pool, review_pool_is_default=False, agents=agents, assignment=assignment)
    
    state = CoordinatorState()
    state.preflight_complexity = "medium"
    state.preflight_complexity_score = 5
    
    config = _apply_preflight_config(config, state)
    
    audit = state.complexity_routing_audit
    print("Audit Planner:", audit["assignments"]["planner"])
    print("Audit Code Reviewers:", audit["assignments"]["code_reviewers"])
    print("Audit Budget Total:", audit["budget"]["initial_total_usd"])
    print("Actual Planner:", config.plan.model)
    print("Actual Code Reviewers:", [p.model for p in config.review_pool])

if __name__ == "__main__":
    main()
