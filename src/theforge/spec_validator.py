"""Backward-compatibility shim — use story_validator instead."""

from .story_validator import StoryValidationFinding as SpecValidationFinding  # noqa: F401
from .story_validator import StoryValidationResult as SpecValidationResult  # noqa: F401
from .story_validator import validate_story as validate_spec  # noqa: F401
