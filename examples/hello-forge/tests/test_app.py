"""
Baseline tests for the hello-forge example app.

These tests pass before any forge run. The dev agent will add more tests
as it implements new features.
"""

from src.app import greet


def test_greet_with_name():
    assert greet("Alice") == "Hello, Alice!"


def test_greet_without_name():
    assert greet() == "Hello, world!"
