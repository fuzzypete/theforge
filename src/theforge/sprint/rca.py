"""Sprint RCA engine — pure classification of non-DONE stories from local artifacts.

The engine is a **deterministic mapping** from on-disk artifacts to a single
``sprint-rca.yaml`` at the sprint log root:

    sprint-summary.yaml + per-story audit.yaml + sprint/story logs
        ──▶  build_sprint_rca()  ──▶  sprint-rca.yaml

It depends only on files under a sprint's log directory — never on runtime
state, renderers, or an LLM. The same pure function powers eager generation on
sprint completion, the on-demand ``forge rca`` verb, and tests.

Classification is **mechanical first**: pattern scans over logs/captured agent
output, audit-field lookups, and summary-field correlation. Every rule carries a
stable ``rule_id`` so evidence can cite the rule that fired and operators can
grep the taxonomy. The residual class ``unknown_needs_rca`` covers stories no
mechanical rule matched — they never silently drop; LLM-assisted classification
(``forge diagnose``) is reserved for that residual and is intentionally out of
this pure engine.

Each story entry carries a *primary* failure class plus explicit *contributing
factors* — a real failure usually has one root cause and one or more amplifiers,
each with a different fix path.

``RULES`` below is the single discoverable location for the classifier's rule
set. Grep it to see everything the mechanical classifier knows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

SCHEMA_VERSION = 1
# Version of the classifier RULES themselves. Bump whenever a rule change can
# alter conclusions for the same inputs. It is stamped into every artifact so a
# regeneration with an improved rule set is a *visible, versioned* re-analysis
# (schema_version stays 1) rather than a silent rewrite of historical judgement:
# an operator can tell whether two RCA files for one sprint were produced by the
# same rule set by comparing this field.
RULESET_VERSION = 2
RCA_FILENAME = "sprint-rca.yaml"

# Outcomes that mean the story landed / succeeded. These stay accounted for in
# sprint-summary.yaml; the RCA file is the recovery surface for everything else.
DONE_OUTCOMES = frozenset({"DONE", "ALREADY_DONE"})

# Residual class assigned when no mechanical primary rule matches a story.
UNKNOWN_CLASS = "unknown_needs_rca"

# Drop reason string a re-exec launch guard records for a worktree that belongs
# to a prior generation's *unfinished* story (stranded sprint state) rather than
# a genuine fresh collision. Matched as a literal here — the engine is a pure
# function over on-disk artifacts and deliberately avoids importing the
# collision/launch-guard modules (which pull in subprocess/lock machinery). Keep
# this in sync with ``launch_guard.REASON_STRANDED_WORKTREE``.
_STRANDED_WORKTREE_REASON = "stranded-prior-generation-worktree"

_EXCERPT_MAX_LEN = 240
# Cap per-file text reads so a runaway log cannot blow up classification.
_MAX_FILE_BYTES = 512 * 1024

# YAML/JSON keys whose values are numeric telemetry (cost, duration, token
# counts) — never provider or error prose. Lines assigning these keys are
# stripped from wholesale-scanned files before pattern matching so a coincidental
# digit run inside a float (e.g. ``cost_usd: 0.6659942999999999`` which contains
# the substring ``429``) can never fire a provider/HTTP rule. Matching is on the
# field *name*, so a real error message that merely mentions cost is untouched.
_TELEMETRY_KEYS: tuple[str, ...] = (
    "cost",
    "cost_usd",
    "total_cost",
    "total_cost_usd",
    "duration",
    "duration_s",
    "duration_ms",
    "elapsed",
    "elapsed_s",
    "latency",
    "latency_ms",
    "tokens",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
    "price",
    "price_usd",
)
_TELEMETRY_LINE_RE = re.compile(
    r"^\s*[\"']?(?:" + "|".join(re.escape(k) for k in _TELEMETRY_KEYS) + r")[\"']?\s*[:=]",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RcaRule:
    """One mechanical classifier rule.

    ``rule_id`` is stable and greppable; evidence cites it. ``role`` is
    ``"primary"`` (can be a story's root cause), ``"contributing"`` (an
    amplifier that never stands alone), or ``"informational"`` (baseline
    evidence that never affects classification).
    """

    rule_id: str
    failure_class: str
    role: str
    description: str
    # Case-insensitive patterns scanned against text sources; any hit fires the
    # rule. Alphabetic patterns match as plain substrings. Purely-numeric
    # patterns (HTTP status codes such as ``429``) match only as a standalone
    # token — anchored with digit boundaries so they can never fire inside a
    # longer number such as the fractional digits of a cost/duration float (see
    # ``_pattern_matches``). Empty for "signal" rules whose detection is
    # field-derived (implemented in ``_signal_rule_hits``) rather than
    # pattern-based.
    patterns: tuple[str, ...] = ()


# ── Classifier rule set (single discoverable location) ────────────────────────
#
# Text rules match substrings against captured agent output and logs. Signal
# rules (empty ``patterns``) are field-derived from the summary/audit and are
# detected in ``_signal_rule_hits`` — they are listed here so every rule_id the
# engine can emit is inspectable in one place.
RULES: tuple[RcaRule, ...] = (
    # ── primary: provider quota / rate limit exhaustion ──────────────────────
    RcaRule(
        rule_id="provider_usage_limit",
        failure_class="provider_quota",
        role="primary",
        description="Provider reported a usage/quota/rate limit in captured output.",
        patterns=(
            "usage limit",
            "current quota",
            "quota limit",
            "insufficient_quota",
            "free tier limits have been reached",
            "rate limit",
            "resource exhausted",
            "resource_exhausted",
            "spend limit",
            "429",
            "overloaded",
        ),
    ),
    # ── primary: worker / agent timeout ──────────────────────────────────────
    RcaRule(
        rule_id="worker_thread_timeout",
        failure_class="worker_timeout",
        role="primary",
        description="Sprint worker thread exceeded its per-story wall-clock budget.",
        patterns=("worker thread timed out after",),
    ),
    RcaRule(
        rule_id="agent_timeout",
        failure_class="worker_timeout",
        role="primary",
        description="An agent invocation exceeded its timeout.",
        patterns=("timeout: agent exceeded",),
    ),
    # ── primary: intake shape drop ───────────────────────────────────────────
    RcaRule(
        rule_id="intake_dropped_after_fix",
        failure_class="intake_shape",
        role="primary",
        description="Issue dropped after an auto-fix; intake gate still failing.",
        patterns=("dropped_after_fix", "dropped after fix"),
    ),
    RcaRule(
        rule_id="intake_dropped_shape",
        failure_class="intake_shape",
        role="primary",
        description="Issue dropped at the shape gate before any work ran.",
        patterns=("dropped_shape", "dropped shape"),
    ),
    # ── primary: signal rules (field-derived) ────────────────────────────────
    RcaRule(
        rule_id="merge_failed",
        failure_class="merge_failed",
        role="primary",
        description="Story reached merge but the merge itself failed.",
    ),
    RcaRule(
        rule_id="merge_arming_failed",
        failure_class="merge_arming_failed",
        role="primary",
        description="PR is fine but arming auto-merge failed (branch protection).",
    ),
    RcaRule(
        rule_id="review_changes_requested",
        failure_class="review_rejected",
        role="primary",
        description="Story escalated/failed with a REQUEST_CHANGES review verdict.",
    ),
    RcaRule(
        rule_id="dev_handoff_no_gate_evidence",
        failure_class="dev_gate_evidence_missing",
        role="primary",
        description=(
            "Terminal dev iteration handed off a completion claim without gate "
            "PASS evidence; the latest commit was never reviewed."
        ),
    ),
    RcaRule(
        rule_id="operator_action_required",
        failure_class="operator_action",
        role="primary",
        description="Deliverable is a human action no dev agent can perform.",
    ),
    RcaRule(
        rule_id="sprint_state_stranded",
        failure_class="sprint_state_stranded",
        role="primary",
        description=(
            "Worktree belongs to a prior generation's unfinished story — "
            "recoverable stranded sprint state, not a fresh launch collision."
        ),
    ),
    RcaRule(
        rule_id="launch_guard_dropped",
        failure_class="launch_collision",
        role="primary",
        description="Story dropped/preserved by the launch guard (worktree/lock).",
    ),
    RcaRule(
        rule_id="dependency_blocked",
        failure_class="dependency_skip",
        role="primary",
        description="Story skipped because an unmet dependency blocked launch.",
    ),
    RcaRule(
        rule_id="iteration_budget_exhausted",
        failure_class="iteration_exhaustion",
        role="primary",
        description="Dev or review hit its iteration limit and the story failed.",
    ),
    # ── contributing factors (amplifiers) ────────────────────────────────────
    RcaRule(
        rule_id="pending_decision_auto_rejected",
        failure_class="operator_gate_timeout",
        role="contributing",
        description="An operator decision gate timed out and auto-escalated.",
        patterns=("pending decision timed out after",),
    ),
    RcaRule(
        rule_id="provider_fallback_not_applied",
        failure_class="fallback_not_applied",
        role="contributing",
        description="A configured provider fallback did not apply on the failure.",
        patterns=("fallback not applied", "no fallback", "fallback unavailable"),
    ),
    RcaRule(
        rule_id="dev_iteration_limit_hit",
        failure_class="dev_iteration_limit",
        role="contributing",
        description="Dev exhausted its iteration budget.",
    ),
    RcaRule(
        rule_id="review_iteration_limit_hit",
        failure_class="review_iteration_limit",
        role="contributing",
        description="Review exhausted its iteration budget.",
    ),
    # ── informational baseline (never classifies) ────────────────────────────
    RcaRule(
        rule_id="captured_outcome",
        failure_class="captured_outcome",
        role="informational",
        description="Baseline evidence recording the story's terminal outcome.",
    ),
)

RULES_BY_ID: dict[str, RcaRule] = {rule.rule_id: rule for rule in RULES}

# Order in which competing primary classes win. Earlier = more specific /
# more actionable, so it is chosen as the primary_failure_class.
_PRIMARY_PRIORITY: tuple[str, ...] = (
    "provider_quota",
    "worker_timeout",
    "intake_shape",
    "merge_failed",
    "merge_arming_failed",
    # A terminal dev-handoff gate-evidence failure supersedes any earlier review
    # verdict: the latest commit was never reviewed, so it must outrank
    # review_rejected when both would otherwise fire.
    "dev_gate_evidence_missing",
    "review_rejected",
    "operator_action",
    "sprint_state_stranded",
    "launch_collision",
    "dependency_skip",
    "iteration_exhaustion",
)


@dataclass(frozen=True)
class _TextSource:
    """A relative source path plus its text content, for pattern scanning."""

    source: str
    text: str
    kind: str


# ── Public engine surface ─────────────────────────────────────────────────────


def has_non_done_stories(summary: dict) -> bool:
    """Return True when any story in a loaded sprint summary finished non-DONE."""
    for story in summary.get("stories", []) or []:
        if not isinstance(story, dict):
            continue
        outcome = str(story.get("outcome") or "").upper()
        if outcome not in DONE_OUTCOMES:
            return True
    return False


def build_sprint_rca(summary_path: Path, *, generated_at: str | None = None) -> dict | None:
    """Build the sprint-rca.yaml payload from a specific sprint summary file.

    Pure function over on-disk artifacts: reads exactly ``summary_path`` (the
    caller resolves *which* summary — the legacy ``sprint-summary.yaml`` pointer
    or the durable run-keyed ``run-<id>-summary.yaml`` for a specific run) plus
    the per-story ``audit.yaml`` and logs beneath its directory. Returns the RCA
    mapping, or ``None`` when there is no summary or when every story landed
    (nothing to analyse). ``generated_at`` defaults to the summary's
    ``finished_at`` so the mapping stays deterministic from disk.
    """
    summary = _load_yaml(summary_path)
    if not isinstance(summary, dict):
        return None

    stories = [s for s in (summary.get("stories") or []) if isinstance(s, dict)]
    non_done = [s for s in stories if str(s.get("outcome") or "").upper() not in DONE_OUTCOMES]
    if not non_done:
        return None

    sprint_log_dir = summary_path.parent
    sprint_block = summary.get("sprint") if isinstance(summary.get("sprint"), dict) else {}
    run_id = sprint_block.get("run_id")
    if generated_at is None:
        generated_at = sprint_block.get("finished_at")

    logs_root = sprint_log_dir.parent
    story_entries: dict[str, dict] = {}
    for story in non_done:
        slug = str(story.get("slug") or "").strip()
        if not slug:
            continue
        story_entries[slug] = _classify_story(
            story, summary_path, sprint_log_dir, logs_root, run_id
        )

    payload = {
        "schema_version": SCHEMA_VERSION,
        "ruleset_version": RULESET_VERSION,
        "sprint_run_id": run_id,
        "generated_at": generated_at,
        "generator": "mechanical",
        "stories": story_entries,
    }

    # Attach the shape-gate skip classification context (issue #1453) so an
    # operator reading the RCA sees whether the sprint was affected by gate
    # friction and how often. Sourced from the summary the writer already
    # persisted, so the RCA stays a pure function over on-disk artifacts and the
    # ``--check`` reproducibility guard still holds.
    skip_block = summary.get("shape_gate_skips")
    if isinstance(skip_block, dict) and skip_block:
        payload["shape_gate_skips"] = skip_block

    return payload


def write_sprint_rca(
    sprint_log_dir: Path,
    *,
    summary_path: Path | None = None,
    generated_at: str | None = None,
    overwrite: bool = True,
    write_pointer: bool = True,
) -> Path | None:
    """Build and write the sprint RCA artifact(s) to the sprint log root.

    ``summary_path`` selects which summary to analyse; it defaults to the legacy
    ``sprint-summary.yaml`` pointer in ``sprint_log_dir``. The RCA is written to
    a durable run-keyed file ``run-<run_id>-sprint-rca.yaml`` (mirroring how
    summaries/audits keep a per-run canonical copy that a later same-name run
    cannot overwrite), and — when ``write_pointer`` is True — also to the
    ``sprint-rca.yaml`` latest pointer. Regenerating an *older* run must not
    clobber the latest pointer, so the on-demand verb passes
    ``write_pointer=False`` for historical runs.

    Returns the primary written path (the pointer when written, else the
    run-keyed file), or ``None`` when there was nothing to write. When
    ``overwrite`` is False and the pointer already exists, it is left untouched.
    """
    if summary_path is None:
        summary_path = sprint_log_dir / "sprint-summary.yaml"

    pointer_path = sprint_log_dir / RCA_FILENAME
    if write_pointer and pointer_path.exists() and not overwrite:
        return pointer_path

    payload = build_sprint_rca(summary_path, generated_at=generated_at)
    if payload is None:
        return None

    sprint_log_dir.mkdir(parents=True, exist_ok=True)
    run_id = payload.get("sprint_run_id")

    written: list[Path] = []
    if run_id:
        run_keyed = sprint_log_dir / f"run-{run_id}-sprint-rca.yaml"
        _dump_yaml(run_keyed, payload)
        written.append(run_keyed)
    if write_pointer:
        _dump_yaml(pointer_path, payload)
        written.insert(0, pointer_path)

    return written[0] if written else None


def _dump_yaml(path: Path, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(payload, f, default_flow_style=False, sort_keys=False)


def artifact_matches(
    summary_path: Path,
    artifact_path: Path,
    *,
    generated_at: str | None = None,
) -> bool | None:
    """Reproducibility check: does regenerating from ``summary_path`` reproduce
    the persisted ``artifact_path`` byte-for-content?

    Because the RCA file is a *regenerable derived artifact* (never committed —
    it lives under gitignored ``.forge/``), the guard against silent divergence
    is detectability: an operator or CI can compare a stored artifact against a
    fresh generation and see whether the current rule set still produces it.

    Returns ``True`` when they match, ``False`` when they diverge, and ``None``
    when there is nothing to compare (no summary, no non-DONE stories, or no
    persisted artifact).
    """
    fresh = build_sprint_rca(summary_path, generated_at=generated_at)
    if fresh is None:
        return None
    existing = _load_yaml(artifact_path)
    if not isinstance(existing, dict):
        return None
    return fresh == existing


def read_sprint_rca(sprint_log_dir: Path) -> dict | None:
    """Return the parsed ``sprint-rca.yaml`` for a sprint, or ``None``.

    Status/rendering surfaces read the persisted artifact rather than
    re-running classification at display time.
    """
    data = _load_yaml(sprint_log_dir / RCA_FILENAME)
    return data if isinstance(data, dict) else None


# ── Classification internals ──────────────────────────────────────────────────


def _classify_story(
    story: dict,
    summary_path: Path,
    sprint_log_dir: Path,
    logs_root: Path,
    run_id: object,
) -> dict:
    """Classify one non-DONE story into a full RCA entry."""
    slug = str(story.get("slug") or "").strip()
    outcome = str(story.get("outcome") or "").upper()

    audit = _load_yaml(sprint_log_dir / slug / "audit.yaml")
    audit = audit if isinstance(audit, dict) else {}

    text_sources = _collect_text_sources(
        story, slug, audit, summary_path, sprint_log_dir, logs_root, run_id
    )

    # (rule_id, source, excerpt, matched_pattern, source_kind) tuples in
    # deterministic order.
    hits: list[tuple[str, str, str, str | None, str]] = []
    hits.extend(_text_rule_hits(text_sources))
    hits.extend(_signal_rule_hits(story, audit, summary_path, sprint_log_dir, logs_root))

    # The story's explicitly-captured terminal error (summary + audit). A concrete
    # captured cause takes precedence over an *ambiguous* pattern hit (e.g. a bare
    # "429"): the incidental match is still recorded as evidence but must not drive
    # primary classification or operator remediation when it is uncorroborated.
    captured_error_text = _captured_error_text(story, audit)
    structured_run_text = _structured_run_text(story, audit)

    # Baseline evidence so an entry is never evidence-empty (AC: unknown stories
    # surface at least the captured outcome). Cite the *resolved* summary file
    # (e.g. run-<id>-summary.yaml for a historical run), never the legacy pointer.
    summary_source = _rel(summary_path, logs_root)
    error = _nonempty(story.get("error"))
    baseline_excerpt = f"outcome={outcome or 'UNKNOWN'}"
    if error:
        baseline_excerpt += f"; {error}"

    # Deduplicate by rule_id, keeping first (most authoritative) evidence.
    seen_rules: set[str] = set()
    evidence: list[dict] = []
    structured_primary_classes: list[str] = []
    text_primary_classes: list[str] = []
    contributing_classes: list[str] = []
    for rule_id, source, excerpt, matched_pattern, source_kind in hits:
        if rule_id in seen_rules:
            continue
        rule = RULES_BY_ID.get(rule_id)
        if rule is None:
            continue
        seen_rules.add(rule_id)
        evidence.append({"source": source, "rule_id": rule_id, "excerpt": excerpt})
        if rule.role == "primary":
            if _is_ambiguous_primary(
                rule,
                matched_pattern,
                captured_error_text,
                structured_run_text,
                source_kind,
            ):
                # Uncorroborated ambiguous hit: keep the evidence for the trace but
                # do not let it classify — the captured non-provider outcome wins.
                continue
            if source_kind == "structured":
                structured_primary_classes.append(rule.failure_class)
            else:
                text_primary_classes.append(rule.failure_class)
        elif rule.role == "contributing":
            contributing_classes.append(rule.failure_class)

    # Always append the baseline outcome evidence last.
    evidence.append(
        {
            "source": summary_source,
            "rule_id": "captured_outcome",
            "excerpt": _truncate(baseline_excerpt),
        }
    )

    primary = _select_primary(structured_primary_classes, text_primary_classes)
    if primary is None:
        primary = UNKNOWN_CLASS

    # Contributing factors: unique, in rule-declaration order, minus whichever
    # class was elevated to primary.
    contributing = _dedupe_ordered(contributing_classes)

    partial_value = _detect_partial_value(story, audit)
    actions = _recommend_actions(primary, contributing, story)

    return {
        "primary_failure_class": primary,
        "contributing_factors": contributing,
        "evidence": evidence,
        "partial_value": partial_value,
        "recommended_next_actions": actions,
    }


def _collect_text_sources(
    story: dict,
    slug: str,
    audit: dict,
    summary_path: Path,
    sprint_log_dir: Path,
    logs_root: Path,
    run_id: object,
) -> list[_TextSource]:
    """Gather (relative-path, text) sources to scan for this story.

    Sources are the story's error/detail from the resolved summary, its
    per-story audit error/message, every text file under its ``<slug>/`` log
    subdir, and the lines of the sprint run log that reference this story (by
    slug or #number).
    """
    sources: list[_TextSource] = []
    summary_rel = _rel(summary_path, logs_root)
    audit_rel = _rel(sprint_log_dir / slug / "audit.yaml", logs_root)

    # Per-story audit error/message — where the runner records terminal-failure
    # detail (e.g. "Worker thread timed out after 3600s") on the CoordinatorResult.
    if isinstance(audit, dict) and audit:
        outcome_block = audit.get("outcome") if isinstance(audit.get("outcome"), dict) else {}
        audit_text_parts = [
            str(audit.get("error") or ""),
            str(outcome_block.get("message") or ""),
            str(outcome_block.get("error_type") or ""),
        ]
        audit_text = "\n".join(p for p in audit_text_parts if p)
        if audit_text.strip():
            sources.append(_TextSource(audit_rel, audit_text, "structured"))

    summary_text_parts = [
        str(story.get("error") or ""),
        str(story.get("drop_reason") or ""),
        str(story.get("error_type") or ""),
        str(story.get("outcome_code") or ""),
        yaml.safe_dump(story.get("detail")) if story.get("detail") else "",
        yaml.safe_dump(story.get("intake")) if story.get("intake") else "",
    ]
    summary_text = "\n".join(p for p in summary_text_parts if p)
    if summary_text.strip():
        sources.append(_TextSource(summary_rel, summary_text, "structured"))

    # Per-story log directory: scan every readable text file (dev/review
    # iteration logs, captured agent output yaml, etc.).
    story_dir = sprint_log_dir / slug
    if story_dir.is_dir():
        for path in sorted(story_dir.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".log", ".txt", ".yaml", ".yml", ".md", ".json"}:
                continue
            if path.name == "audit.yaml":
                continue
            # Strip numeric telemetry lines (cost/duration/token values) so a
            # coincidental digit run inside a float cannot feed context-free
            # pattern matching — e.g. `cost_usd: 0.6659942999999999` containing
            # the substring `429`. Error/message prose is untouched.
            text = _strip_telemetry(_read_text(path))
            if text:
                sources.append(
                    _TextSource(_rel(path, logs_root), text, _text_source_kind_for_path(path))
                )

    # Sprint run log: attribute only lines that reference this story so we never
    # fabricate cross-story attribution from a shared log.
    run_log = _find_run_log(sprint_log_dir, run_id)
    if run_log is not None:
        refs = _story_references(slug)
        matched_lines = [
            line
            for line in _strip_telemetry(_read_text(run_log)).splitlines()
            if any(ref in line for ref in refs)
        ]
        if matched_lines:
            sources.append(_TextSource(_rel(run_log, logs_root), "\n".join(matched_lines), "log"))

    return sources


def _pattern_matches(pattern: str, lowered: str) -> bool:
    """Return True when *pattern* is present in already-lowercased *lowered*.

    Purely-numeric patterns (HTTP status codes such as ``429``) are matched with
    digit boundaries so they fire only as a standalone token — never inside a
    longer number such as the fractional digits of a cost/duration float. All
    other (alphabetic) patterns keep plain substring semantics.
    """
    if pattern.isdigit():
        return re.search(rf"(?<!\d){re.escape(pattern)}(?!\d)", lowered) is not None
    return pattern in lowered


def _text_rule_hits(sources: list[_TextSource]) -> list[tuple[str, str, str, str | None, str]]:
    """Fire every text rule whose pattern appears in any source.

    Each hit carries the concrete pattern that matched so classification can tell
    an unambiguous provider phrase (``usage limit``) apart from an ambiguous bare
    status code (``429``) when deciding precedence.
    """
    hits: list[tuple[str, str, str, str | None, str]] = []
    for rule in RULES:
        if not rule.patterns:
            continue
        for src in sources:
            lowered = src.text.lower()
            matched = [p for p in rule.patterns if _pattern_matches(p, lowered)]
            if not matched:
                continue
            # Prefer an unambiguous (non-numeric) pattern so a source that also
            # contains a strong provider phrase (e.g. "HTTP 429 overloaded") is
            # not reduced to its bare status code — otherwise the ambiguity guard
            # would wrongly demote a genuinely-corroborated provider-limit hit.
            matched_pattern = next((p for p in matched if not p.isdigit()), matched[0])
            excerpt = _first_matching_line(src.text, rule.patterns)
            hits.append((rule.rule_id, src.source, excerpt, matched_pattern, src.kind))
            break  # one evidence per rule is enough
    return hits


def _signal_rule_hits(
    story: dict,
    audit: dict,
    summary_path: Path,
    sprint_log_dir: Path,
    logs_root: Path,
) -> list[tuple[str, str, str, str | None, str]]:
    """Fire field-derived (signal) rules from summary/audit structured fields.

    Signal hits carry ``None`` as the matched pattern — they are field-derived,
    not pattern-derived, so the ambiguity-precedence check never applies to them.
    """
    hits: list[tuple[str, str, str]] = []
    summary_source = _rel(summary_path, logs_root)
    audit_source = _rel(sprint_log_dir / str(story.get("slug") or "") / "audit.yaml", logs_root)
    outcome = str(story.get("outcome") or "").upper()
    error = _nonempty(story.get("error"))

    def _outcome_excerpt(suffix: str = "") -> str:
        tail = f"; {error}" if error else ""
        return _truncate(f"outcome={outcome}{suffix}{tail}")

    if outcome == "MERGE_FAILED":
        hits.append(("merge_failed", summary_source, _outcome_excerpt()))
    if outcome == "MERGE_ARMING_FAILED":
        hits.append(("merge_arming_failed", summary_source, _outcome_excerpt()))
    if outcome == "OPERATOR_ACTION":
        hits.append(("operator_action_required", summary_source, _outcome_excerpt()))
    drop_reason = _nonempty(story.get("drop_reason"))
    if drop_reason == _STRANDED_WORKTREE_REASON:
        # A prior-generation worktree left unfinished sprint state — a distinct,
        # recoverable class that must not be flattened into a fresh collision.
        hits.append(
            (
                "sprint_state_stranded",
                summary_source,
                _truncate(f"outcome={outcome}; stranded prior-generation sprint state"),
            )
        )
    elif outcome in {"DROPPED", "PRESERVED"} or drop_reason:
        drop = drop_reason or error or "launch-guard drop"
        hits.append(
            ("launch_guard_dropped", summary_source, _truncate(f"outcome={outcome}; {drop}"))
        )
    if outcome == "SKIPPED":
        deps = list(story.get("depends_on") or [])
        if deps:
            hits.append(
                (
                    "dependency_blocked",
                    summary_source,
                    _truncate(f"skipped; unmet dependencies: {', '.join(str(d) for d in deps)}"),
                )
            )

    # Review verdict — from the per-story audit reviews (or summary verdict).
    verdict = _last_review_verdict(story, audit)
    # A dev iteration that terminates by handing off a completion claim without
    # gate PASS evidence (HANDOFF_NO_GATE_EVIDENCE) is the *terminal* failure: it
    # ran after — and was never re-reviewed by — any earlier review cycle. Do not
    # let a stale REQUEST_CHANGES from an earlier, now-superseded commit
    # masquerade as the terminal cause; classify the gate-evidence handoff
    # instead and make the un-reviewed latest commit explicit.
    terminal_gate = _terminal_dev_gate_result(audit)
    unreviewed_handoff = terminal_gate == "HANDOFF_NO_GATE_EVIDENCE" and outcome in {
        "ESCALATE",
        "ESCALATED",
        "FAILED",
    }
    if unreviewed_handoff:
        stale = (
            " (stale review REQUEST_CHANGES applied to an earlier, now-superseded commit)"
            if verdict == "REQUEST_CHANGES"
            else ""
        )
        hits.append(
            (
                "dev_handoff_no_gate_evidence",
                audit_source if audit else summary_source,
                _truncate(
                    "terminal dev iteration handed off without gate PASS evidence "
                    "(HANDOFF_NO_GATE_EVIDENCE); latest commit was not reviewed"
                    f"{stale}; outcome={outcome}"
                ),
            )
        )
    elif verdict == "REQUEST_CHANGES" and outcome in {"ESCALATE", "ESCALATED", "FAILED"}:
        hits.append(
            (
                "review_changes_requested",
                audit_source if audit else summary_source,
                _truncate(f"final review verdict REQUEST_CHANGES; outcome={outcome}"),
            )
        )

    # Iteration-limit signals from the per-story summary/audit iteration_usage.
    usage = story.get("iteration_usage")
    if not isinstance(usage, dict):
        iteration_block = audit.get("iterations") if isinstance(audit, dict) else None
        usage = iteration_block.get("usage_summary") if isinstance(iteration_block, dict) else None
    dev_hit = _hit_limit(usage, "dev")
    review_hit = _hit_limit(usage, "review")
    if dev_hit:
        hits.append(
            ("dev_iteration_limit_hit", summary_source, _truncate("dev iteration limit reached"))
        )
    if review_hit:
        hits.append(
            (
                "review_iteration_limit_hit",
                summary_source,
                _truncate("review iteration limit reached"),
            )
        )
    # Elevate iteration exhaustion to a primary cause only when the story failed
    # and nothing else classified it (checked at selection time via priority).
    if (dev_hit or review_hit) and outcome in {"FAILED", "ESCALATE", "ESCALATED"}:
        hits.append(
            (
                "iteration_budget_exhausted",
                summary_source,
                _truncate("iteration budget exhausted before completion"),
            )
        )

    return [(rule_id, source, excerpt, None, "structured") for rule_id, source, excerpt in hits]


def _select_primary(
    structured_primary_classes: list[str], text_primary_classes: list[str]
) -> str | None:
    """Choose the winning primary failure class by declared priority.

    Structured fields from the current run outrank broad text scans. Text scans
    remain valuable fallback evidence, but they must not override the run's own
    recorded terminal state.
    """
    present = set(structured_primary_classes)
    for cls in _PRIMARY_PRIORITY:
        if cls in present:
            return cls
    if structured_primary_classes:
        return structured_primary_classes[0]
    present = set(text_primary_classes)
    for cls in _PRIMARY_PRIORITY:
        if cls in present:
            return cls
    # A primary rule fired but its class is not in the priority list — return
    # the first seen so we never lose a real classification.
    return text_primary_classes[0] if text_primary_classes else None


def _detect_partial_value(story: dict, audit: dict) -> list[str]:
    """Surface mechanically-detectable partial value produced before failure."""
    values: list[str] = []

    dev_invocations = 0
    workspace_path: str | None = None
    branch: str | None = None
    if isinstance(audit, dict):
        cost = audit.get("cost")
        if isinstance(cost, dict):
            dev_invocations = int(cost.get("dev_invocations") or 0)
        iterations = audit.get("iterations")
        if not dev_invocations and isinstance(iterations, dict):
            dev_invocations = int(iterations.get("dev_iterations") or 0)
        workspace = audit.get("workspace")
        if isinstance(workspace, dict):
            workspace_path = _nonempty(workspace.get("path"))
            branch = _nonempty(workspace.get("branch"))

    if dev_invocations > 0:
        values.append(f"dev produced {dev_invocations} iteration(s) of work")
    outcome = str(story.get("outcome") or "").upper()
    if outcome in {"ESCALATE", "ESCALATED", "PRESERVED"} and workspace_path:
        detail = f"workspace preserved at {workspace_path}"
        if branch:
            detail += f" (branch {branch})"
        values.append(detail)
    return values


def _recommend_actions(primary: str, contributing: list[str], story: dict) -> list[str]:
    """Map primary class + contributing factors to actionable next steps."""
    ref = _story_ref(story)
    actions: list[str] = []

    diagnose_ref = _issue_number(story) or ref
    base = {
        "provider_quota": (
            f"wait for quota reset or switch the provider/model, then re-sprint {ref}"
        ),
        "worker_timeout": (
            f"inspect the worker log for the phase {ref} was in at timeout; "
            "split the story or raise the worker timeout, then re-run"
        ),
        "intake_shape": f"reshape the {ref} issue body to satisfy the intake gate, then re-run",
        "merge_failed": f"resolve the merge conflict for {ref} and re-run the merge",
        "merge_arming_failed": (
            f"configure branch protection so auto-merge can arm, or merge {ref} manually"
        ),
        "review_rejected": (
            f"inspect the escalated {ref} worktree and address the review findings, then re-run"
        ),
        "dev_gate_evidence_missing": (
            f"inspect the escalated {ref} worktree: the dev handed off claiming completion "
            "without gate PASS evidence and the latest commit was never reviewed — re-run the "
            "gate (or re-sprint) before trusting any earlier review verdict"
        ),
        "operator_action": f"perform the operator action described in {ref} (no dev agent can)",
        "launch_collision": (f"clear the active worktree/lock blocking {ref}, then re-sprint it"),
        "sprint_state_stranded": (
            f"re-resume/reconcile the sprint so {ref}'s prior-generation state is "
            "recovered — do NOT clear the worktree and re-sprint fresh, which would "
            "discard partial work"
        ),
        "dependency_skip": _dependency_action(story, ref),
        "iteration_exhaustion": (
            f"raise the iteration budget for {ref} or narrow its scope, then re-run"
        ),
        UNKNOWN_CLASS: (
            f"run 'forge diagnose --issue {diagnose_ref}' for LLM-assisted root cause"
        ),
    }
    actions.append(base.get(primary, f"investigate {ref} manually"))

    if "fallback_not_applied" in contributing:
        actions.append("wire the provider fallback so the next failure recovers automatically")
    if "operator_gate_timeout" in contributing:
        actions.append("shorten or auto-resolve the operator decision gate that timed out")
    if "dev_iteration_limit" in contributing and primary != "iteration_exhaustion":
        actions.append("raise the dev iteration budget or narrow the story scope")
    if "review_iteration_limit" in contributing and primary != "iteration_exhaustion":
        actions.append("raise the review iteration budget or reduce review churn")

    return actions


def _dependency_action(story: dict, ref: str) -> str:
    deps = [str(d) for d in (story.get("depends_on") or [])]
    if deps:
        return f"land blocking dependencies ({', '.join(deps)}) then re-sprint {ref}"
    return f"resolve the blocker preventing {ref} from launching, then re-sprint it"


# ── Small helpers ─────────────────────────────────────────────────────────────


def _load_yaml(path: Path) -> object:
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception:
        return None


def _read_text(path: Path) -> str:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            with open(path, "rb") as fb:
                fb.seek(-_MAX_FILE_BYTES, 2)
                return fb.read().decode("utf-8", errors="replace")
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _find_run_log(sprint_log_dir: Path, run_id: object) -> Path | None:
    """Return the sprint run log for *this* run only.

    When the run id is known we use exactly ``run-<run_id>.log`` and never fall
    back to a sibling run's log: scanning another run's log would attribute the
    wrong run's lines to this run's stories — cross-run contamination that a
    historical ``forge rca`` (whose own run log may be gone while a later
    same-name run's log remains) would otherwise hit. The glob fallback applies
    only when the run id is unknown, where there is no specific run to confuse.
    """
    if run_id:
        candidate = sprint_log_dir / f"run-{run_id}.log"
        return candidate if candidate.is_file() else None
    matches = sorted(sprint_log_dir.glob("run-*.log"))
    return matches[0] if matches else None


def _story_references(slug: str) -> list[str]:
    """Tokens that identify a story's lines in the shared sprint run log."""
    refs = [slug]
    num = _issue_number_from_slug(slug)
    if num:
        refs.append(f"#{num}")
    return refs


def _issue_number_from_slug(slug: str) -> str | None:
    # slugs look like "issue-1324"; extract the trailing number.
    tail = slug.rsplit("-", 1)[-1] if "-" in slug else slug
    return tail if tail.isdigit() else None


def _issue_number(story: dict) -> str | None:
    slug = str(story.get("slug") or "")
    num = _issue_number_from_slug(slug)
    if num:
        return num
    path = str(story.get("path") or "")
    if path.startswith("Issue #"):
        candidate = path.split("#", 1)[1].strip()
        if candidate.isdigit():
            return candidate
    return None


def _story_ref(story: dict) -> str:
    num = _issue_number(story)
    if num:
        return f"#{num}"
    return str(story.get("slug") or story.get("path") or "the story")


def _first_matching_line(text: str, patterns: tuple[str, ...]) -> str:
    for line in text.splitlines():
        lowered = line.lower()
        if any(_pattern_matches(p, lowered) for p in patterns):
            return _truncate(line.strip())
    return _truncate(text.strip())


def _strip_telemetry(text: str) -> str:
    """Drop numeric-telemetry key/value lines (cost/duration/token counts) from
    scannable text so their digit runs never feed context-free pattern matching.

    Matching is on the field *name* at line start, so prose that merely mentions
    cost (e.g. an error message) is preserved.
    """
    if not text:
        return text
    return "\n".join(line for line in text.splitlines() if not _TELEMETRY_LINE_RE.match(line))


def _captured_error_text(story: dict, audit: dict) -> str:
    """Lowercased concatenation of the story's explicitly-captured terminal error.

    Sourced from the summary ``error`` and the per-story audit ``error`` /
    ``outcome.message`` — the fields that carry the runner's real terminal-failure
    detail. Used to let a concrete captured cause outrank an ambiguous pattern hit.
    """
    parts = [_nonempty(story.get("error")), _nonempty(audit.get("error"))]
    outcome_block = audit.get("outcome") if isinstance(audit.get("outcome"), dict) else {}
    parts.append(_nonempty(outcome_block.get("message")))
    return "\n".join(p for p in parts if p).lower()


def _structured_run_text(story: dict, audit: dict) -> str:
    """Structured current-run fields that can corroborate or veto text hits."""
    parts = [
        _nonempty(story.get("error")),
        _nonempty(story.get("drop_reason")),
        _nonempty(story.get("error_type")),
        _nonempty(story.get("outcome_code")),
        _nonempty(story.get("preflight")),
        _nonempty(story.get("preflight_original_verdict")),
    ]
    intake = story.get("intake") if isinstance(story.get("intake"), dict) else {}
    parts.extend(
        [
            _nonempty(intake.get("kind")),
            _nonempty(intake.get("problem")),
        ]
    )
    if isinstance(audit, dict):
        outcome_block = audit.get("outcome") if isinstance(audit.get("outcome"), dict) else {}
        preflight_block = (
            audit.get("preflight") if isinstance(audit.get("preflight"), dict) else {}
        )
        parts.extend(
            [
                _nonempty(audit.get("error")),
                _nonempty(outcome_block.get("message")),
                _nonempty(outcome_block.get("error_type")),
                _nonempty(preflight_block.get("verdict")),
                _nonempty(preflight_block.get("original_verdict")),
                _nonempty(preflight_block.get("failure_action")),
            ]
        )
    return "\n".join(p for p in parts if p).lower()


def _is_ambiguous_primary(
    rule: RcaRule,
    matched_pattern: str | None,
    captured_error_text: str,
    structured_run_text: str,
    source_kind: str,
) -> bool:
    """Return True when a primary text-rule fired only on an ambiguous token.

    An ambiguous match is a bare numeric status code (e.g. ``429``). Such a hit
    must not drive primary classification when the story already carries a
    concrete captured terminal error that does *not* itself corroborate the rule
    with an unambiguous phrase — the captured non-provider outcome takes
    precedence. A genuine unambiguous phrase (``usage limit``) is never demoted,
    and when there is no captured error to contradict it a standalone status code
    still classifies.
    """
    if matched_pattern is None:
        return False
    if rule.failure_class == "intake_shape":
        if source_kind == "structured":
            return False
        if not structured_run_text:
            return True
        return not any(_pattern_matches(pattern, structured_run_text) for pattern in rule.patterns)
    if not matched_pattern.isdigit():
        return False
    if not captured_error_text:
        return False
    strong_patterns = [p for p in rule.patterns if not p.isdigit()]
    if any(_pattern_matches(p, captured_error_text) for p in strong_patterns):
        return False
    return True


def _text_source_kind_for_path(path: Path) -> str:
    """Classify scanned files by how trustworthy they are as primary evidence."""
    lowered = path.name.lower()
    if lowered.endswith(".md"):
        return "authored"
    if lowered.startswith("preflight"):
        return "authored"
    return "log"


def _truncate(text: str) -> str:
    text = text.strip()
    if len(text) > _EXCERPT_MAX_LEN:
        return text[: _EXCERPT_MAX_LEN - 1] + "…"
    return text


def _nonempty(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _dedupe_ordered(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _hit_limit(usage: object, kind: str) -> bool:
    if not isinstance(usage, dict):
        return False
    block = usage.get(kind)
    if isinstance(block, dict):
        return bool(block.get("hit_limit"))
    return False


def _terminal_dev_gate_result(audit: dict) -> str | None:
    """Gate result of the last recorded dev iteration, if any.

    The terminal dev iteration is the most recent thing that happened in the dev
    loop; its ``gate_result`` is the authoritative terminal outcome regardless of
    any earlier review verdict. Sourced from the per-story audit's
    ``iterations.dev_loop`` (coordinator audit), where each entry carries the
    ``gate_result`` recorded when that iteration finished.
    """
    if not isinstance(audit, dict):
        return None
    iterations = audit.get("iterations")
    dev_loop = iterations.get("dev_loop") if isinstance(iterations, dict) else None
    if isinstance(dev_loop, list) and dev_loop:
        last = dev_loop[-1]
        if isinstance(last, dict):
            return _nonempty(last.get("gate_result"))
    return None


def _last_review_verdict(story: dict, audit: dict) -> str | None:
    verdict = _nonempty(story.get("verdict"))
    if verdict:
        return verdict.upper()
    if isinstance(audit, dict):
        reviews = audit.get("reviews")
        if isinstance(reviews, list) and reviews:
            last = reviews[-1]
            if isinstance(last, dict):
                raw = _nonempty(last.get("verdict"))
                if raw:
                    return raw.upper()
    return None
