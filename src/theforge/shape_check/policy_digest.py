"""Deterministic digest for the shape-check policy surface.

The digest is intentionally file-granular: any edit to a listed policy source
invalidates the digest, while unlisted files are ignored by design.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

POLICY_SOURCE_FILES = (
    "_mapping.py",
    "check.py",
    "classifier.py",
    "diagnosis_spec.py",
    "heuristics.py",
    "issue_spec.py",
    "parsing.py",
    "placeholders.py",
    "types.py",
    "verdict.py",
)


def compute_policy_digest(package_dir: Path | None = None) -> str:
    """Return a stable digest for the configured shape-policy file manifest."""
    root = package_dir or Path(__file__).resolve().parent
    hasher = sha256()
    for relative_path in POLICY_SOURCE_FILES:
        hasher.update(relative_path.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update((root / relative_path).read_bytes())
        hasher.update(b"\0")
    return f"sha256:{hasher.hexdigest()}"
