"""Best-effort secret redaction for audit records.

Applies three scrubbing passes before any audit record reaches disk:

1. Key-pattern scrub: dict keys matching secret-shaped names (secret, token,
   password, api_key, authorization, …) have their values replaced with
   "[REDACTED]".
2. Env-file value scrub: values loaded from a .env file are replaced wherever
   they appear as substrings inside string values.  Values shorter than 8
   characters are skipped to avoid replacing common innocuous strings like
   "true" or "dev".
3. Environment-dict replacement: any dict key named "environment" whose value
   is a dict is replaced with a sorted list of its keys only.

Redaction is best-effort.  Arbitrary story or plan text can still embed secrets
the scrubber cannot recognise.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

_SECRET_KEY_RE = re.compile(r"(?i)(secret|token|password|api[_-]?key|authorization)")
_MIN_SECRET_LEN = 8


def _load_env_secrets(env_file: Path) -> set[str]:
    """Parse KEY=VALUE pairs from an env file; return the set of values to scrub.

    Rules:
    - Lines starting with # are comments and are skipped.
    - Blank lines are skipped.
    - Values shorter than _MIN_SECRET_LEN chars are skipped (avoids replacing
      common non-secret strings like "true", "dev", "1").
    - Surrounding quotes (" or ') are stripped from values.
    """
    secrets: set[str] = set()
    try:
        text = env_file.read_text(encoding="utf-8")
    except OSError:
        return secrets

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        _key, _, value = line.partition("=")
        value = value.strip()
        # Strip surrounding quotes
        if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
            value = value[1:-1]
        if len(value) >= _MIN_SECRET_LEN:
            secrets.add(value)
    return secrets


def _redact_value(val: str, secrets: set[str]) -> str:
    """Replace occurrences of any known secret inside a string value."""
    for secret in secrets:
        if secret in val:
            val = "[REDACTED]"
            break
    return val


def _redact_obj(obj: Any, secrets: set[str]) -> Any:
    """Recursively walk and scrub a JSON-serialisable object."""
    if isinstance(obj, dict):
        result: dict[str, Any] = {}
        for k, v in obj.items():
            # Pass 3: environment dict → key-only list
            if k == "environment" and isinstance(v, dict):
                result[k] = sorted(v.keys())
                continue
            # Pass 1: secret-shaped key → redact value
            if _SECRET_KEY_RE.search(str(k)):
                result[k] = "[REDACTED]"
                continue
            result[k] = _redact_obj(v, secrets)
        return result
    if isinstance(obj, list):
        return [_redact_obj(item, secrets) for item in obj]
    if isinstance(obj, str):
        return _redact_value(obj, secrets)
    return obj


def redact(obj: Any, env_file: Path | None) -> Any:
    """Return a deep-copied, scrubbed version of *obj*.

    Args:
        obj: Any JSON-serialisable object (dict, list, scalar).
        env_file: Optional path to a KEY=VALUE env file whose values should be
            redacted from the output.  Missing files are silently ignored.

    Returns:
        A new object of the same type with secrets replaced by "[REDACTED]".
    """
    secrets: set[str] = set()
    if env_file is not None:
        secrets = _load_env_secrets(env_file)

    return _redact_obj(copy.deepcopy(obj), secrets)
