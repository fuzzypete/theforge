"""
hello-forge example app.

A minimal Python module that the forge pipeline will extend.
The dev agent will add features on top of this scaffold.
"""


def greet(name=None):
    """Return a greeting string."""
    if name:
        return f"Hello, {name}!"
    return "Hello, world!"
