"""Domain taxonomy — the fixed vocabulary of story-domain tags.

Preflight tags every story with zero or more *domain* tags describing the kind
of work it involves (``[react, css]``, ``[api, database]``, ``[cli, config]``).
Domain is the *horizontal* axis of routing — complexity says how hard a story
is, domain says what kind of work it is — so the adaptive router can prefer a
model whose per-domain track record is strong for the story's domains
(issue #155).

This is a pure-data, stdlib-only module (project convention 4: keep pure-data
types in low-dependency modules). It defines:

- :data:`DOMAIN_TAXONOMY` — the closed set of allowed tags. Tags are selected
  from this taxonomy, never invented as free text — an unknown tag is dropped
  by :func:`validate_domains`, mirroring how the structured-output parsers in
  ``coordinator/preflight.py`` reject values outside their enum.
- :data:`DOMAIN_DESCRIPTIONS` — one-line intended-usage doc per tag. This is
  the single source of truth the preflight prompt enumerates from, so the
  prompt's advertised vocabulary can never drift from what the parser accepts.
- :func:`normalize_domain` / :func:`validate_domains` — normalization and
  admission helpers.

ADR-0006 alignment: current-run domain tags are routing-safe only as structured
preflight telemetry (bucket A, "slug/domain tags recorded by preflight").
Historical per-domain performance is routing-safe only after aggregation and
clause-2 admissibility checks — see ``model_profiles_read_model.get_dev_domain_signal``.
"""

from __future__ import annotations

# ── The fixed taxonomy ─────────────────────────────────────────────────
#
# Each tag names a *kind of work / subject matter*, deliberately distinct from
# work_type (feature/refactor/mechanical/bug), which names the SHAPE of the
# change. A story may carry several tags (a React form backed by an API touches
# both ``frontend`` and ``api``). Keep this set small and stable: it is a
# schema-versioned routing input under ADR-0006 clause 2.5, and every added tag
# starts cold (no history) until runs accumulate under it.
DOMAIN_DESCRIPTIONS: dict[str, str] = {
    "frontend": "Client-side UI structure, components, rendering, browser behavior.",
    "react": "React-specific component work — hooks, JSX, component state.",
    "css": "Stylesheets, layout, visual styling, responsive design.",
    "api": "HTTP/RPC API endpoints, request/response handling, routing/controllers.",
    "backend": "Server-side business logic and services not specific to the API surface.",
    "database": "Schema, queries, migrations, ORM/persistence layers.",
    "cli": "Command-line interfaces, argument parsing, terminal output/UX.",
    "config": "Configuration files, settings, environment/flag handling.",
    "testing": "Test authoring, fixtures, test harness or framework work.",
    "docs": "Documentation, markdown, reference material, docstrings/comments.",
    "infra": "Deployment, containers, cloud resources, provisioning.",
    "ci": "CI/CD pipelines, build automation, release tooling.",
    "concurrency": "Threads, async, parallelism, locking, cancellation, scheduling.",
    "parsing": "Parsers, serialization, format/protocol encoding and decoding.",
    "security": "Auth, secrets, crypto, permissions, input trust boundaries.",
    "networking": "Sockets, transport protocols, connection handling, retries.",
    "data-processing": "ETL, aggregation, numeric/data pipelines, transforms.",
    "algorithms": "Algorithmic or computational logic — data structures, math.",
}

# The closed admission set. ``validate_domains`` drops anything not in here.
DOMAIN_TAXONOMY: frozenset[str] = frozenset(DOMAIN_DESCRIPTIONS)


def normalize_domain(tag: object) -> str | None:
    """Normalize one raw tag to a canonical taxonomy tag, or ``None``.

    Lowercases and trims surrounding whitespace, and folds interchangeable word
    separators (spaces / underscores → hyphen) so ``"data_processing"`` and
    ``"Data Processing"`` both resolve to ``"data-processing"``. Returns ``None``
    when the result is not in :data:`DOMAIN_TAXONOMY` — unknown tags are dropped,
    never guessed at.
    """
    if not isinstance(tag, str):
        return None
    norm = tag.strip().lower().replace("_", "-").replace(" ", "-")
    return norm if norm in DOMAIN_TAXONOMY else None


def validate_domains(raw: object) -> list[str]:
    """Coerce a raw ``domains`` value into a clean, ordered, de-duplicated list.

    Accepts the structured-output list the preflight agent emits. Non-list
    inputs (absent field, scalar, malformed) yield ``[]``. Each element is
    normalized via :func:`normalize_domain`; unknown tags are dropped. Order of
    first appearance is preserved and duplicates are removed so the recorded
    list is deterministic.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        norm = normalize_domain(item)
        if norm is not None and norm not in out:
            out.append(norm)
    return out


def taxonomy_prompt_lines() -> str:
    """Render the taxonomy as ``- tag: description`` lines for the preflight prompt.

    The prompt enumerates the allowed vocabulary from this single source so the
    advertised tags and the accepted tags cannot drift apart.
    """
    return "\n".join(f"        - `{tag}`: {desc}" for tag, desc in DOMAIN_DESCRIPTIONS.items())
