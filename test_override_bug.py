from theforge.config.load import load_config
from pathlib import Path
import yaml
import os

config_dir = Path("test_config_dir")
config_dir.mkdir(exist_ok=True)
config_file = config_dir / "forge.yaml"

config_data = {
    "project": "test",
    "models": ["claude/sonnet"],
    "overrides": {
        "dev": {
            "provider": "anthropic"
        }
    }
}

with open(config_file, "w") as f:
    yaml.dump(config_data, f)

os.environ["ANTHROPIC_API_KEY"] = "test"
config = load_config(config_file)
print(f"dev profile cli: {config.dev_profile.cli}")
print(f"dev profile provider: {config.dev_profile.provider}")
print(f"dev profile transport: {config.dev_profile.transport.kind}")
