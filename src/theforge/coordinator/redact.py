"""Best-effort secret redaction for audit records.

Applies three scrubbing passes before any audit record reaches disk:

1. Key-pattern scrub: dict keys matching secret-shaped names (secret, token,
   password, api_key, authorization, …) have their values replaced with
   "[REDACTED]".  Numeric values are exempt — see "Numeric values are telemetry"
   below.
2. Env-file value scrub: values loaded from a .env file are replaced wherever
   they appear as substrings inside string values.  Values shorter than 8
   characters are skipped to avoid replacing common innocuous strings like
   "true" or "dev".
3. Environment-dict replacement: any dict key named "environment" whose value
   is a dict is replaced with a sorted list of its keys only.

Redaction is best-effort.  Arbitrary story or plan text can still embed secrets
the scrubber cannot recognise.

## Numeric values are telemetry, not credentials

The key pattern classifies by *name*, but a credential is always a string: no
API key, bearer token, or password is representable as a JSON number.  A field
whose value is a bounded numeric measurement therefore carries no secret,
whatever word was used to name it — and scrubbing it destroys evidence while
protecting nothing.

This matters because per-invocation usage counts are keyed with the word
"token" (`input_tokens`, `output_tokens`, `cache_read_tokens`,
`cache_creation_tokens`).  Redacting them left audit records holding what an
invocation cost but not what it bought, so cost per unit of work was not
derivable from the substrate (#2202).

`_redact_obj` therefore retains numeric values on secret-shaped keys.  This is a
narrowing of a trust boundary, so it is guarded mechanically rather than by
comment alone: `TELEMETRY_NUMERIC_KEYS` catalogues the audit keys that depend on
the carve-out and `tests/test_coord_redact.py` asserts every one of them
survives redaction with a numeric value *and* is still scrubbed with a string
value.  A later widening of `_SECRET_KEY_RE`, or a removal of the numeric
carve-out, fails those tests instead of silently discarding telemetry again.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

_SECRET_KEY_RE = re.compile(r"(?i)(secret|token|password|api[_-]?key|authorization)")
_MIN_SECRET_LEN = 8

#: Audit keys that match `_SECRET_KEY_RE` but hold numeric telemetry, not
#: credentials.  These are the fields the numeric carve-out in `_redact_obj`
#: exists to protect; `tests/test_coord_redact.py` pins each one so a future
#: edit to the pattern or to the carve-out cannot silently start scrubbing them
#: again.  Add an entry here (and its test coverage follows automatically) when
#: a new numeric audit field lands under a secret-shaped name.
TELEMETRY_NUMERIC_KEYS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
    }
)


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
            val = val.replace(secret, "[REDACTED]")
    return val


def _carries_no_credential(val: Any) -> bool:
    """Return True when *val* cannot possibly hold a credential.

    Credentials are strings.  A numeric value — a token count, a byte count, a
    duration — is a measurement, so retaining it on a secret-shaped key leaks
    nothing while preserving evidence.  `bool` is included (it subclasses `int`
    and is equally incapable of carrying a secret); `None` is left to the normal
    redaction path so an explicitly-null credential field still reads as
    scrubbed rather than as "no credential was present".
    """
    return isinstance(val, (int, float))


def _redact_obj(obj: Any, secrets: set[str]) -> Any:
    """Recursively walk and scrub a JSON-serialisable object."""
    if isinstance(obj, dict):
        result: dict[str, Any] = {}
        for k, v in obj.items():
            # Pass 3: environment dict → key-only list
            if k == "environment" and isinstance(v, dict):
                result[k] = sorted(v.keys())
                continue
            # Pass 1: secret-shaped key → redact value, unless the value is
            # numeric and so cannot carry a credential (see module docstring).
            if _SECRET_KEY_RE.search(str(k)) and not _carries_no_credential(v):
                result[k] = "[REDACTED]"
                continue
            result[k] = _redact_obj(v, secrets)
        return result
    if isinstance(obj, list):
        return [_redact_obj(item, secrets) for item in obj]
    if isinstance(obj, tuple):
        return tuple(_redact_obj(item, secrets) for item in obj)
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
