from theforge.config.profiles import _apply_profile_overrides
from theforge.config.types import ModelProfile

base = ModelProfile(
    name="dev",
    cli="claude",
    provider=None,
    model="sonnet",
    budget_usd=10.0,
    timeout_seconds=300,
    allowed_tools=(),
)

overridden = _apply_profile_overrides(base, {"provider": "anthropic"})
print(f"cli: {overridden.cli}, provider: {overridden.provider}, transport: {overridden.transport.kind}")
