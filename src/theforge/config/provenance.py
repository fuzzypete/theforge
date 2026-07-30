"""Configuration identity: what configuration a run actually executed under (#2056).

A run outcome recorded without the configuration that produced it cannot be
compared against another run's outcome — the two may differ in inputs the record
never names. This module derives that identity at load time so every downstream
consumer (audit record, RCA, explain) reads a *recorded* fact rather than
re-deriving one from `git log`.

Two digests, because they answer different questions:

- ``resolved_sha256`` — the digest of the fully **resolved** configuration
  (defaults applied, profiles/overrides/registry resolved). This is the one that
  answers "did these two runs use the same configuration?", because it captures
  resolution the file alone does not determine.
- ``source_sha256`` — the digest of the ``forge.yaml`` bytes as read. This is
  what makes a *mid-flight* edit detectable: the audit emitter re-reads the same
  path when the run finishes and compares.

Stdlib-only and dependency-free by design (CONVENTIONS: pure-data types live in
low-dependency modules); it must not import :mod:`theforge.config.types`, which
imports *this* module for the :class:`ConfigProvenance` field.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Fields excluded from the resolved-configuration digest.
#
# ``secrets``  — secret *values*; a digest must never be an oracle for them, and
#                rotating a token is not a configuration change.
# ``provenance`` — the digest field itself (self-reference).
# ``project_root`` — the filesystem location the config was loaded from, not the
#                configuration. Two checkouts of one project (e.g. a worktree and
#                the parent) must agree on identity; the location is recorded
#                separately as ``source_path``.
_DIGEST_EXCLUDED_FIELDS = frozenset({"secrets", "provenance", "project_root"})

# Dotted paths (``[]`` for a list element) whose *value* is a delivery endpoint
# that doubles as a bearer credential — an ntfy topic URL is the whole secret to
# anyone who holds it. Excluding the top-level ``secrets`` dict is not enough:
# ``_parse_notifications`` resolves ``NTFY_URL`` from ``.forge/.env`` *or* the
# process environment straight into these typed fields, so a digest over them
# both fingerprints the secret and makes a rotated URL look like a different
# configuration.
#
# Redacted, not dropped: presence still participates in the digest (see
# ``_REDACTED``), so enabling or disabling a backend remains a configuration
# change while rotating its URL does not.
_REDACTED_VALUE_PATHS = frozenset(
    {
        "notifications.ntfy.url",
        "notifications.backends[].url",
    }
)

# Placeholder substituted for a redacted value. A constant, so the digest records
# "this field was set" without recording what it was set to.
_REDACTED = "<redacted>"

# Secret values shorter than this are not substring-matched against other config
# values: a two-character secret would redact half the configuration and destroy
# the digest's ability to distinguish anything. Exact matches are still redacted
# at any length.
_MIN_SUBSTRING_SECRET_LEN = 8


@dataclass(frozen=True)
class ConfigProvenance:
    """Identity of the configuration a run loaded.

    ``source_path`` is the absolute path of the ``forge.yaml`` that was read
    (``None`` for a directly-constructed config that never came from a file);
    ``source_sha256`` is the digest of its bytes at load time; ``resolved_sha256``
    is the digest of the resolved configuration those bytes produced.
    """

    source_path: str | None = None
    source_sha256: str | None = None
    resolved_sha256: str | None = None


def file_sha256(path: Path) -> str:
    """Return the SHA-256 hex digest of a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _secret_values(config: Any) -> frozenset[str]:
    """Return the loaded secret values, for scrubbing wherever they landed.

    A path allow-list only covers the leaks somebody enumerated. Every secret
    the loader resolved is known here, so any config value carrying one is
    redacted no matter which field it reached — the digest must not be a function
    of a secret, wherever it ended up.
    """
    secrets = getattr(config, "secrets", None)
    if not isinstance(secrets, dict):
        return frozenset()
    return frozenset(value for value in secrets.values() if isinstance(value, str) and value)


def _carries_secret(text: str, secret_values: frozenset[str]) -> bool:
    """True when ``text`` is, or embeds, a known secret value."""
    for secret in secret_values:
        if text == secret:
            return True
        if len(secret) >= _MIN_SUBSTRING_SECRET_LEN and secret in text:
            return True
    return False


def canonical_payload(
    value: Any,
    *,
    path: str = "",
    secret_values: frozenset[str] = frozenset(),
) -> Any:
    """Normalize a configuration value into a deterministic JSON-able structure.

    Dataclasses become field-name-keyed dicts, mappings are emitted with sorted
    keys, sequences keep their order, sets are sorted, and ``Path`` (plus any
    other opaque object) degrades to its string form. Deterministic for equal
    inputs, which is the whole property the digest rests on.

    ``path`` is the dotted position of ``value`` in the configuration tree, used
    to redact the credential-bearing endpoints in ``_REDACTED_VALUE_PATHS``;
    ``secret_values`` redacts any string carrying a loaded secret regardless of
    where it sits. Both substitute ``_REDACTED``, preserving presence.
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: canonical_payload(
                getattr(value, field.name),
                path=f"{path}.{field.name}" if path else field.name,
                secret_values=secret_values,
            )
            for field in sorted(dataclasses.fields(value), key=lambda f: f.name)
        }
    if isinstance(value, dict):
        return {
            str(key): canonical_payload(
                value[key],
                path=f"{path}.{key}" if path else str(key),
                secret_values=secret_values,
            )
            for key in sorted(value, key=str)
        }
    if isinstance(value, (list, tuple)):
        return [
            canonical_payload(item, path=f"{path}[]", secret_values=secret_values)
            for item in value
        ]
    if isinstance(value, (set, frozenset)):
        return sorted(str(item) for item in value)
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str) and value:
        if path in _REDACTED_VALUE_PATHS or _carries_secret(value, secret_values):
            return _REDACTED
        return value
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def resolved_config_payload(config: Any) -> dict[str, Any]:
    """Return the canonical payload of a resolved config, minus excluded fields.

    Secret-derived values are redacted rather than digested — see
    ``_REDACTED_VALUE_PATHS`` and ``_secret_values``.
    """
    secret_values = _secret_values(config)
    return {
        field.name: canonical_payload(
            getattr(config, field.name), path=field.name, secret_values=secret_values
        )
        for field in sorted(dataclasses.fields(config), key=lambda f: f.name)
        if field.name not in _DIGEST_EXCLUDED_FIELDS
    }


def resolved_config_sha256(config: Any) -> str:
    """Return a stable digest of the resolved configuration.

    Equivalent resolved configurations digest identically regardless of where
    they were loaded from or which secrets were present; any meaningful
    configuration difference changes the digest.
    """
    payload = json.dumps(
        resolved_config_payload(config),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_provenance(config: Any, source_path: Path | None) -> ConfigProvenance:
    """Derive the :class:`ConfigProvenance` for a freshly-loaded config.

    An unreadable source file yields a ``None`` ``source_sha256`` rather than an
    exception: configuration identity is observability, and it must never be the
    reason a run refuses to start.
    """
    source_digest: str | None = None
    resolved_path: Path | None = None
    if source_path is not None:
        # Absolute: the audit emitter re-reads this exact path at run finish,
        # possibly from a different working directory.
        try:
            resolved_path = source_path.resolve()
        except OSError:
            resolved_path = source_path
        try:
            source_digest = file_sha256(resolved_path)
        except OSError:
            source_digest = None
    return ConfigProvenance(
        source_path=str(resolved_path) if resolved_path is not None else None,
        source_sha256=source_digest,
        resolved_sha256=resolved_config_sha256(config),
    )
