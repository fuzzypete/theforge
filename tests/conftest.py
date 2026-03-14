"""Global test fixtures and safety patches for the theforge test suite."""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _block_real_notifications():
    """Prevent any test from firing real OS or ntfy notifications."""
    with (
        patch("theforge.coordinator._notify"),
        patch("theforge.coordinator._ntfy_publish"),
        patch("theforge.sprint._notify"),
    ):
        yield
