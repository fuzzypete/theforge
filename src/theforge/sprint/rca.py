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

from theforge.config.sandbox_capabilities import SandboxCapabilityError, get_preset

SCHEMA_VERSION = 1
# Version of the classifier RULES themselves. Bump whenever a rule change can
# alter conclusions for the same inputs. It is stamped into every artifact so a
# regeneration with an improved rule set is a *visible, versioned* re-analysis
# (schema_version stays 1) rather than a silent rewrite of historical judgement:
# an operator can tell whether two RCA files for one sprint were produced by the
# same rule set by comparing this field.
RULESET_VERSION = 9
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

# ``error_type``/``outcome_code`` a run carries when it terminated because of the
# substrate rather than a judgment about the story. Matched as a literal for the
# same reason as above — keep in sync with
# ``coordinator.agent_failure.ERROR_TYPE_INFRASTRUCTURE_ABORT``.
_INFRASTRUCTURE_ABORT_ERROR_TYPE = "infrastructure_abort"

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
    # When True, the rule's patterns are scanned only against sources forge
    # emitted about its own execution — structured summary/audit fields and run
    # or iteration logs — never against agent-authored prose (``authored`` kind:
    # markdown and preflight artifacts, which carry analysis *of the target
    # repository*). Set it on any rule whose patterns are ordinary English a
    # dev/reviewer agent could plausibly write about the project under
    # development; see ``_text_source_kind_for_path``.
    forge_emitted_only: bool = False


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
        description=(
            "Provider reported a usage/quota/rate limit — from the run's own CLI "
            "transport telemetry, or from a limit phrase in forge-emitted output."
        ),
        # "rate limit" / "overloaded" / "resource exhausted" are ordinary English an
        # agent can write while analysing the project under development, so this rule
        # carries the same exposure #2031 fixed for the fallback rule. Two guards,
        # short of deleting patterns that still catch real provider errors nothing
        # else records: agent-authored prose is out of scope for the scan
        # (``forge_emitted_only``), and forge's own per-iteration quota observation
        # fires the rule without any text at all (``_provider_quota_evidence``).
        forge_emitted_only=True,
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
    # ── primary: shared run-infrastructure abort ─────────────────────────────
    RcaRule(
        rule_id="shared_infrastructure_abort",
        failure_class="shared_infrastructure",
        role="primary",
        description=(
            "The run terminated on a failure of the substrate or of run "
            "infrastructure shared by every story (e.g. the rolling advisory "
            "artifact all workers write) — not on a judgment about this story."
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
    # ── primary: workspace base-branch divergence ────────────────────────────
    RcaRule(
        rule_id="workspace_base_divergence",
        failure_class="workspace_divergence",
        role="primary",
        description=(
            "Base branch diverged from origin (local ahead and behind) — a "
            "mechanical workspace precondition failure, not a code/logic bug."
        ),
        # Deliberately narrow to the divergence-specific phrase only. A bare
        # "workspace abort" also fires for unrelated WORKSPACE abort failures
        # (e.g. "pull failed for base branch ... ") which are not divergence and
        # must not get the rebase/reconcile remediation.
        patterns=("diverged from origin",),
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
        rule_id="configuration_changed",
        failure_class="configuration_changed",
        role="primary",
        description=(
            "The project configuration (forge.yaml) changed while the story was "
            "in flight — the run did not execute under a single configuration."
        ),
    ),
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
        rule_id="sandbox_capability_profile_missing",
        failure_class="capability_profile_gap",
        role="primary",
        description=(
            "The run resolved no sandbox capability profile while the story "
            "reported a toolchain/service denial a forge-owned preset grants — "
            "the project never opted into the capability the failure needed."
        ),
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
        description=(
            "A configured provider fallback did not apply on the failure — "
            "field-derived from the run's own transport telemetry "
            "(transport_fallback_reason set with transport_fallback_fired false)."
        ),
        # Deliberately NOT pattern-based (#2031). The prior patterns were ordinary
        # English ("no fallback", "fallback unavailable") matched as unanchored
        # substrings against every text source, which includes agent-authored prose
        # *about the project under development*. That fired the rule on a preflight
        # analysis of a target repo's own error handling and sent the operator to
        # wire a forge subsystem that had never been involved. A runtime cause code
        # may only be assigned from something forge emits about its own execution,
        # so detection moved to ``_fallback_not_applied_evidence``.
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
    # A substrate/shared-infrastructure abort outranks every story-level class
    # below it: the run made no statement about the story, so classifying it by
    # what the story was doing at the time sends the operator to the wrong
    # subject entirely (#2107). It sits below provider_quota only because that
    # class names a *specific* substrate cause with its own remediation.
    "shared_infrastructure",
    "worker_timeout",
    "workspace_divergence",
    "intake_shape",
    # A configuration change mid-flight outranks the merge failure it presents
    # as: "uncommitted forge.yaml in the project root" IS the configuration
    # change, and remediating it as a merge problem sends the operator to the
    # PR — the one place the cause is not (#2056).
    "configuration_changed",
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
    # A story whose dev agent could not run its toolchain because the project
    # declared no capability profile outranks the iteration exhaustion it
    # presents as (#2029): the budget was spent on iterations that could never
    # succeed, so "raise the budget" funds a repeat of the same failure. The
    # configuration gap is the cause; the exhausted budget is its symptom.
    "capability_profile_gap",
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
    # Correlated once here (not inside the signal pass) so the matched preset is
    # available to both the evidence and the recommended action.
    capability_gap = _capability_profile_gap_evidence(audit, text_sources)

    hits: list[tuple[str, str, str, str | None, str]] = []
    hits.extend(_text_rule_hits(text_sources))
    hits.extend(
        _signal_rule_hits(
            story, audit, summary_path, sprint_log_dir, logs_root, capability_gap=capability_gap
        )
    )

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
    # (failure_class, source_kind) so an unclassifiable story can drop the
    # contributing factors that rest on nothing but a text scan.
    contributing_hits: list[tuple[str, str]] = []
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
            contributing_hits.append((rule.failure_class, source_kind))

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
    #
    # When the engine could not classify the failure at all, its own uncertainty
    # propagates (#2031): a text-scan-derived amplifier is a match on vocabulary,
    # and asserting one confidently — plus the remediation the operator is then
    # told to go perform — on a run forge has just declared unexplained costs more
    # trust than declining to guess. Field-derived (structured) factors are the
    # run's own recorded facts, not an inference, so they still stand.
    contributing = _dedupe_ordered(
        [
            failure_class
            for failure_class, source_kind in contributing_hits
            if primary != UNKNOWN_CLASS or source_kind == "structured"
        ]
    )

    partial_value = _detect_partial_value(story, audit)
    actions = _recommend_actions(
        primary,
        contributing,
        story,
        capability_preset=capability_gap.preset if capability_gap else None,
        capability_profile_note=capability_gap.profile_note if capability_gap else None,
    )

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
            if rule.forge_emitted_only and src.kind == "authored":
                # Agent-authored prose describes the work being attempted, not the
                # run attempting it — it can never assign this rule's cause code.
                continue
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


# Words that, alongside a forge.yaml mention in a merge failure, identify the
# error as "the configuration file was edited" rather than an ordinary conflict.
_DIRTY_CONFIG_MARKERS: tuple[str, ...] = ("uncommitted", "unstaged", "dirty", "local changes")


def _configuration_change_evidence(
    audit: dict,
    outcome: str,
    error: str | None,
    summary_source: str,
    audit_source: str,
) -> tuple[str, str, str] | None:
    """Return a ``configuration_changed`` hit when the run's config moved (#2056).

    Two evidence sources, preferred in order of authority:

    1. The run's own recorded provenance (``configuration.changed_during_run``) —
       a mechanical statement by the run about itself, available for every run
       whose record carries configuration identity.
    2. A ``MERGE_FAILED`` whose error names a dirty ``forge.yaml``. The
       configuration change is stated verbatim in that error; classifying it as a
       merge failure spends the operator's attention on the PR, which is the one
       place the cause is not.
    """
    configuration = audit.get("configuration") if isinstance(audit, dict) else None
    if isinstance(configuration, dict) and configuration.get("changed_during_run") is True:
        start = configuration.get("source_sha256") or "?"
        finish = configuration.get("source_sha256_at_finish") or "?"
        source_path = configuration.get("source_path") or "forge.yaml"
        return (
            "configuration_changed",
            audit_source,
            _truncate(
                f"configuration changed while the run was in flight: {source_path} "
                f"{start} -> {finish}; outcome={outcome}"
            ),
        )

    error_lower = (error or "").lower()
    if (
        outcome == "MERGE_FAILED"
        and "forge.yaml" in error_lower
        and any(marker in error_lower for marker in _DIRTY_CONFIG_MARKERS)
    ):
        return (
            "configuration_changed",
            summary_source,
            _truncate(
                f"outcome={outcome}; the project configuration was modified during the "
                f"sprint: {error}"
            ),
        )
    return None


def _dev_loop_entries(audit: dict) -> list[dict]:
    """The per-dev-iteration telemetry records the coordinator audit persisted."""
    iterations = audit.get("iterations") if isinstance(audit, dict) else None
    dev_loop = iterations.get("dev_loop") if isinstance(iterations, dict) else None
    if not isinstance(dev_loop, list):
        return []
    return [entry for entry in dev_loop if isinstance(entry, dict)]


# ── Sandbox capability-profile gap (#2029) ────────────────────────────────────
#
# Forge owns the capability presets (``config.sandbox_capabilities``) and records
# the resolved capability set in every story's audit. When a project selects no
# preset, that record reads ``profile: null`` with empty grants — and if the dev
# agent then reports the exact toolchain denial a preset exists to remove, forge
# is holding the symptom and the configuration that explains it at the same time.
#
# The symptom vocabulary lives here rather than in the preset table: a preset
# declares what it *grants* (paths, mach services), which is not the text a
# failing toolchain prints. Keys must name real forge-owned presets — asserted by
# the test suite against ``sandbox_capabilities.preset_names()``.
_CAPABILITY_PRESET_SYMPTOMS: dict[str, tuple[str, ...]] = {
    "xcode": (
        "coresimulator",
        "core simulator",
        "simulator service",
        "simulator runtime",
        "simctl",
        "xcodebuild",
        "xcodegen",
        "swift build",
        "swiftpm",
        "deriveddata",
        "library/developer",
        "com.apple.dt.xcode",
        "org.swift.swiftpm",
    ),
}

# A symptom token alone is just the name of a tool the story legitimately uses.
# It only evidences a *capability* gap when the same line also states that the
# resource was refused — so both must co-occur on one line.
_CAPABILITY_DENIAL_MARKERS: tuple[str, ...] = (
    "operation not permitted",
    "permission denied",
    "not permitted",
    "unreachable",
    "connection invalid",
    "connection interrupted",
    "connection refused",
    "could not connect",
    "sandbox denied",
)


@dataclass(frozen=True)
class _CapabilityGap:
    """A run whose capability payloads missed the matched preset's grants."""

    preset: str
    source: str
    source_kind: str
    profile_note: str
    excerpt: str


def _sandbox_capability_payloads(audit: dict) -> list[dict]:
    """Every resolved capability record the story's audit carries.

    Both the run-level ``workspace.sandbox_capabilities`` and the per-iteration
    ``iterations.dev_loop[*].sandbox_capabilities`` payloads written by
    ``coordinator.audit`` — a story is only treated as ungranted when *every*
    record agrees, so a run that widened containment for even one iteration is
    never reported as having had none.
    """
    payloads: list[dict] = []
    workspace = audit.get("workspace") if isinstance(audit, dict) else None
    if isinstance(workspace, dict) and isinstance(workspace.get("sandbox_capabilities"), dict):
        payloads.append(workspace["sandbox_capabilities"])
    for entry in _dev_loop_entries(audit):
        payload = entry.get("sandbox_capabilities")
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def _capability_root_suffix(template: str) -> tuple[str, ...]:
    """Return the path suffix a preset template must match in an audit payload."""
    if template.startswith("~/"):
        template = template[2:]
    return Path(template).parts


def _capability_root_matches(actual: str, template: str) -> bool:
    """True when a resolved write root satisfies a preset template."""
    actual_parts = Path(actual).parts
    template_parts = _capability_root_suffix(template)
    if len(actual_parts) < len(template_parts):
        return False
    return actual_parts[-len(template_parts) :] == template_parts


def _capability_payload_grants_preset(payload: dict, preset_name: str) -> bool:
    """True when one recorded capability payload fully grants a preset."""
    try:
        preset = get_preset(preset_name)
    except SandboxCapabilityError:
        return False

    write_roots = payload.get("write_roots")
    mach_services = payload.get("mach_services")
    if not isinstance(write_roots, list) or not isinstance(mach_services, list):
        return False

    actual_roots = [str(root) for root in write_roots if _nonempty(root)]
    actual_services = {str(service) for service in mach_services if _nonempty(service)}

    for template in preset.write_roots:
        if not any(_capability_root_matches(actual, template) for actual in actual_roots):
            return False
    for service in preset.mach_services:
        if service not in actual_services:
            return False
    return True


def _capability_profile_note(payloads: list[dict]) -> str:
    """Summarise the recorded profile state for capability-gap evidence."""
    profiles = [
        _nonempty(payload.get("profile"))
        for payload in payloads
        if isinstance(payload, dict) and _nonempty(payload.get("profile"))
    ]
    if not profiles:
        return "sandbox.capability_profile unset"
    profile = profiles[0]
    if len({p for p in profiles}) == 1:
        return f"sandbox.capability_profile={profile!r}"
    unique = ", ".join(repr(p) for p in sorted(set(profiles)))
    return f"sandbox.capability_profile values {unique}"


def _capability_symptom_hit(sources: list[_TextSource]) -> tuple[str, str, str, str] | None:
    """Find the first line naming a preset's toolchain *and* its refusal.

    Agent-authored prose is out of scope for the same reason as #2031: it
    describes the project under development, not the run developing it, so it
    can never assign a runtime cause code.
    """
    for preset in sorted(_CAPABILITY_PRESET_SYMPTOMS):
        symptoms = _CAPABILITY_PRESET_SYMPTOMS[preset]
        for src in sources:
            if src.kind == "authored":
                continue
            for line in src.text.splitlines():
                lowered = line.lower()
                if not any(symptom in lowered for symptom in symptoms):
                    continue
                if not any(marker in lowered for marker in _CAPABILITY_DENIAL_MARKERS):
                    continue
                return preset, src.source, src.kind, _truncate(line.strip())
    return None


def _capability_profile_gap_evidence(
    audit: dict, sources: list[_TextSource]
) -> _CapabilityGap | None:
    """Correlate a missing/incomplete capability payload with the matching denial.

    Neither half classifies alone: most runs legitimately resolve no profile, and
    a toolchain error under a fully granted preset is a different problem with a
    different fix. Together they are the configuration gap forge already held
    both halves of and reported as an iteration-budget problem (#2029).
    """
    payloads = _sandbox_capability_payloads(audit)
    if not payloads:
        return None
    hit = _capability_symptom_hit(sources)
    if hit is None:
        return None
    preset, source, source_kind, excerpt = hit
    if any(_capability_payload_grants_preset(payload, preset) for payload in payloads):
        return None
    return _CapabilityGap(
        preset=preset,
        source=source,
        source_kind=source_kind,
        profile_note=_capability_profile_note(payloads),
        excerpt=excerpt,
    )


def _provider_quota_evidence(audit: dict, audit_source: str) -> tuple[str, str, str] | None:
    """Return a ``provider_usage_limit`` hit from the run's own quota observation.

    ``cli_quota_error_observed`` is forge's classification of *its own* CLI
    invocation — set from the transport's exit code or its captured stderr/stdout
    by ``runners.cli._classify_cli_fallback_decision`` — and the coordinator audit
    records it per dev iteration. That makes it a statement about the run, unlike
    the rule's English patterns.

    Restricted to the *terminal* dev iteration with no fallback fired: an earlier
    quota blip that a fallback absorbed, or that a later iteration recovered from,
    is not what the story died of, and provider_quota is the highest-priority
    primary class — promoting a survived blip to root cause would send the
    operator to wait for a quota reset that was never the blocker.
    """
    entries = _dev_loop_entries(audit)
    if not entries:
        return None
    last = entries[-1]
    if not last.get("cli_quota_error_observed") or last.get("transport_fallback_fired"):
        return None
    iteration = last.get("iteration")
    where = f"dev iteration {iteration}" if iteration is not None else "the terminal dev iteration"
    reason = _nonempty(last.get("transport_fallback_reason"))
    detail = f" ({reason})" if reason else ""
    return (
        "provider_usage_limit",
        audit_source,
        _truncate(
            f"the run's own transport telemetry recorded a provider quota/rate-limit "
            f"error on {where}{detail}, with no fallback transport applied"
        ),
    )


def _fallback_not_applied_evidence(audit: dict, audit_source: str) -> tuple[str, str, str] | None:
    """Return a ``provider_fallback_not_applied`` hit from the run's own telemetry.

    The authoritative statement that a provider fallback did not apply is made by
    forge itself, per dev iteration, in ``iterations.dev_loop``:
    ``transport_fallback_reason`` is set only when the CLI transport failed with a
    fallback-eligible error (quota/capacity/model-not-found), and
    ``transport_fallback_fired`` records whether a fallback transport actually ran.
    Reason set + fired false is exactly "forge classified the failure as one a
    fallback should have covered, and no fallback was applied" — the fallback
    machinery reporting on itself, never vocabulary borrowed from agent prose.

    (Reason set + fired true is a fallback that *did* apply — including one that
    applied and then failed — so it is deliberately not a hit.)
    """
    for entry in _dev_loop_entries(audit):
        reason = _nonempty(entry.get("transport_fallback_reason"))
        if not reason or entry.get("transport_fallback_fired"):
            continue
        iteration = entry.get("iteration")
        where = f"dev iteration {iteration}" if iteration is not None else "a dev iteration"
        transport = _nonempty(entry.get("transport_used")) or "cli"
        return (
            "provider_fallback_not_applied",
            audit_source,
            _truncate(
                f"{where} failed with a fallback-eligible transport error ({reason}) but no "
                f"provider fallback was applied (transport_fallback_fired=false, "
                f"transport_used={transport})"
            ),
        )
    return None


def _infrastructure_abort_evidence(
    story: dict,
    audit: dict,
    summary_source: str,
    audit_source: str,
) -> tuple[str, str, str] | None:
    """Return a ``shared_infrastructure_abort`` hit from the run's own record.

    Fires only on the run's *terminal* classification of itself — the
    ``infrastructure_abort`` error_type/outcome_code, or the structured
    ``agent_invocation.infrastructure_failure`` cause. The
    ``shared_infrastructure_failures`` ledger is deliberately not sufficient on
    its own: a non-fatal shared-resource failure (e.g. one lost advisory
    artifact update) is recorded there on runs that then failed review or the
    gate for entirely unrelated reasons, and promoting it to primary would
    reproduce #2107's misattribution with the subject reversed. When a terminal
    signal *has* fired, the ledger supplies the concrete component and path.

    Without this rule a substrate abort falls through to ``unknown_needs_rca``
    and the operator is told to run ``forge diagnose`` on a story that never
    failed — exactly the misattribution #2107 reports.
    """
    agent_block = audit.get("agent_invocation") if isinstance(audit, dict) else None
    cause = agent_block.get("infrastructure_failure") if isinstance(agent_block, dict) else None
    outcome_block = audit.get("outcome") if isinstance(audit, dict) else None
    audit_error_type = (
        _nonempty(outcome_block.get("error_type")) if isinstance(outcome_block, dict) else None
    )
    story_error_type = _nonempty(story.get("error_type")) or _nonempty(story.get("outcome_code"))
    abort_stamped = _INFRASTRUCTURE_ABORT_ERROR_TYPE in {
        (story_error_type or "").lower(),
        (audit_error_type or "").lower(),
    }
    if not abort_stamped and not (isinstance(cause, dict) and cause):
        return None

    ledger = audit.get("shared_infrastructure_failures") if isinstance(audit, dict) else None
    head = ledger[0] if isinstance(ledger, list) and ledger else None
    first = head if isinstance(head, dict) else {}
    cause = cause if isinstance(cause, dict) else {}
    component = (
        _nonempty(cause.get("component"))
        or _nonempty(first.get("component"))
        or _nonempty(cause.get("category"))
        or "shared run infrastructure"
    )
    detail = (
        _nonempty(cause.get("message"))
        or _nonempty(first.get("error"))
        or _nonempty(story.get("error"))
        or "no agent judgment was obtained"
    )
    source = audit_source if (cause or first) else summary_source
    return (
        "shared_infrastructure_abort",
        source,
        _truncate(f"run terminated on a shared-infrastructure failure ({component}): {detail}"),
    )


def _signal_rule_hits(
    story: dict,
    audit: dict,
    summary_path: Path,
    sprint_log_dir: Path,
    logs_root: Path,
    capability_gap: _CapabilityGap | None = None,
) -> list[tuple[str, str, str, str | None, str]]:
    """Fire field-derived (signal) rules from summary/audit structured fields.

    Signal hits carry ``None`` as the matched pattern — they are field-derived,
    not pattern-derived, so the ambiguity-precedence check never applies to them.
    """
    hits: list[tuple[str, str, str, str]] = []
    summary_source = _rel(summary_path, logs_root)
    audit_source = _rel(sprint_log_dir / str(story.get("slug") or "") / "audit.yaml", logs_root)
    outcome = str(story.get("outcome") or "").upper()
    error = _nonempty(story.get("error"))

    def _outcome_excerpt(suffix: str = "") -> str:
        tail = f"; {error}" if error else ""
        return _truncate(f"outcome={outcome}{suffix}{tail}")

    # Configuration change (#2056) — checked before the merge signals so the
    # evidence that names it is attached to the class that explains it. Two
    # independent sources: the run's own recorded provenance, and the dirty
    # forge.yaml that a MERGE_FAILED outcome reports as a merge problem.
    config_change = _configuration_change_evidence(
        audit, outcome, error, summary_source, audit_source
    )
    if config_change is not None:
        hits.append((*config_change, "structured"))

    # Shared-infrastructure abort (#2107) — field-derived from the run's own
    # recorded error_type/outcome_code and the structured cause the runner or
    # coordinator persisted. Never a text scan: "infrastructure" is ordinary
    # English, and only forge's own classification of its own execution may
    # assign this class.
    infra_hit = _infrastructure_abort_evidence(story, audit, summary_source, audit_source)
    if infra_hit is not None:
        hits.append((*infra_hit, "structured"))

    # Provider quota / fallback classification from the run's own transport
    # telemetry rather than scanned for in prose (#2031).
    quota_hit = _provider_quota_evidence(audit, audit_source)
    if quota_hit is not None:
        hits.append((*quota_hit, "structured"))
    fallback_hit = _fallback_not_applied_evidence(audit, audit_source)
    if fallback_hit is not None:
        hits.append((*fallback_hit, "structured"))

    # Sandbox capability-profile gap (#2029) — the run's own resolved capability
    # record, whether absent or missing the matched preset's required grants,
    # correlated with a reported denial of a resource a forge-owned preset
    # grants. Detected in
    # ``_capability_profile_gap_evidence`` so the matched preset can also name
    # itself in the recommended action.
    if capability_gap is not None:
        hits.append(
            (
                "sandbox_capability_profile_missing",
                capability_gap.source,
                _truncate(
                    f"{capability_gap.profile_note}; recorded capability payloads did not "
                    f"grant the '{capability_gap.preset}' preset's required write roots or "
                    f"mach services; reported denial the '{capability_gap.preset}' preset "
                    f"grants: {capability_gap.excerpt}"
                ),
                capability_gap.source_kind,
            )
        )

    if outcome == "MERGE_FAILED":
        hits.append(("merge_failed", summary_source, _outcome_excerpt(), "structured"))
    if outcome == "MERGE_ARMING_FAILED":
        hits.append(("merge_arming_failed", summary_source, _outcome_excerpt(), "structured"))
    if outcome == "OPERATOR_ACTION":
        hits.append(("operator_action_required", summary_source, _outcome_excerpt(), "structured"))
    drop_reason = _nonempty(story.get("drop_reason"))
    if drop_reason == _STRANDED_WORKTREE_REASON:
        # A prior-generation worktree left unfinished sprint state — a distinct,
        # recoverable class that must not be flattened into a fresh collision.
        hits.append(
            (
                "sprint_state_stranded",
                summary_source,
                _truncate(f"outcome={outcome}; stranded prior-generation sprint state"),
                "structured",
            )
        )
    elif outcome in {"DROPPED", "PRESERVED"} or drop_reason:
        drop = drop_reason or error or "launch-guard drop"
        hits.append(
            (
                "launch_guard_dropped",
                summary_source,
                _truncate(f"outcome={outcome}; {drop}"),
                "structured",
            )
        )
    if outcome == "SKIPPED":
        deps = list(story.get("depends_on") or [])
        if deps:
            hits.append(
                (
                    "dependency_blocked",
                    summary_source,
                    _truncate(f"skipped; unmet dependencies: {', '.join(str(d) for d in deps)}"),
                    "structured",
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
                "structured",
            )
        )
    elif verdict == "REQUEST_CHANGES" and outcome in {"ESCALATE", "ESCALATED", "FAILED"}:
        hits.append(
            (
                "review_changes_requested",
                audit_source if audit else summary_source,
                _truncate(f"final review verdict REQUEST_CHANGES; outcome={outcome}"),
                "structured",
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
            (
                "dev_iteration_limit_hit",
                summary_source,
                _truncate("dev iteration limit reached"),
                "structured",
            )
        )
    if review_hit:
        hits.append(
            (
                "review_iteration_limit_hit",
                summary_source,
                _truncate("review iteration limit reached"),
                "structured",
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
                "structured",
            )
        )

    return [
        (rule_id, source, excerpt, None, source_kind)
        for rule_id, source, excerpt, source_kind in hits
    ]


def _select_primary(
    structured_primary_classes: list[str], text_primary_classes: list[str]
) -> str | None:
    """Choose the winning primary failure class by declared priority.

    Structured fields from the current run outrank broad text scans. Text scans
    remain valuable fallback evidence, but they must not override the run's own
    recorded terminal state.

    ``capability_profile_gap`` is the single exception: its structured half (the
    run's own resolved, empty capability payload) is always present, and only
    the denial half is carried as a log line, so a text-sourced hit still
    represents structured run state. It is promoted into the structured pool and
    then competes on the declared priority list like any other class — it does
    not jump ahead of a higher-priority class such as ``merge_failed``.
    """
    present = set(structured_primary_classes)
    if "capability_profile_gap" in text_primary_classes:
        present.add("capability_profile_gap")
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


def _merge_failed_action(story: dict, ref: str) -> str:
    """Next step for a failed landing, branched on the evidenced cause.

    A merge can fail for reasons that call for opposite operator responses, and
    the recorded error text is the evidence that distinguishes them. Naming a
    merge conflict unconditionally (the prior behavior) is wrong whenever the PR
    was mergeable and merely red, or merely slow — issue #1946.
    """
    error = (_nonempty(story.get("error")) or "").lower()
    if "required checks failed" in error:
        return (
            f"fix the required checks named in {ref}'s merge failure, then re-run the "
            "gate and land it — the PR was abandoned decided-red, not blocked by a "
            "conflict or a wait budget"
        )
    if "timed out" in error:
        return (
            f"inspect {ref}'s queued PR: its required checks were still pending when the "
            "merge wait expired — raise merge_wait_timeout_seconds or land it manually "
            "once the checks settle"
        )
    if "conflict" in error:
        return f"resolve the merge conflict for {ref} and re-run the merge"
    return f"inspect {ref}'s PR for the merge failure cause recorded above, then re-run the merge"


def _launch_collision_action(story: dict, ref: str) -> str:
    """Next step for a launch-guard drop, gated on what the worktree holds.

    "Clear the worktree" is destructive advice, and a drop is not evidence that
    the worktree is disposable: in #2079 the colliding worktrees held this
    sprint's own unmerged commits, and following the advice would have destroyed
    them. So the clearing advice is emitted only when the absence of unmerged
    work was positively established — an unknown answer gets the preserving
    advice, exactly as a known-nonzero one does.
    """
    commits = story.get("unmerged_commits")
    determined = story.get("unmerged_work_determined")
    dirty = story.get("worktree_dirty")
    branch = _nonempty(story.get("branch"))
    worktree = _nonempty(story.get("worktree"))
    where = f" (branch {branch})" if branch else ""

    has_commits = isinstance(commits, int) and not isinstance(commits, bool) and commits > 0
    if has_commits or dirty is True:
        held = f"{commits} unmerged commit(s)" if has_commits else "uncommitted changes"
        location = f" at {worktree}" if worktree else ""
        return (
            f"preserve the worktree{location} blocking {ref}{where}: it holds {held} — "
            "recover that branch (push it, or land/rebase it) BEFORE clearing anything, "
            "and do not re-sprint the story until the work is safe"
        )
    if determined is True:
        return f"clear the active worktree/lock blocking {ref}, then re-sprint it"
    return (
        f"inspect the worktree/lock blocking {ref}{where} before clearing it — whether it "
        "holds unmerged work was never established, so treat it as if it does "
        "(git log/status in the worktree, then recover or clear deliberately)"
    )


def _capability_gap_action(preset: str | None, ref: str, profile_note: str | None = None) -> str:
    """Next step for a capability-profile gap: name the setting and the preset.

    Deliberately does not mention the iteration budget. The budget was spent on
    iterations that could not have succeeded, so raising it funds a repeat of the
    same failure (#2029).
    """
    named = f"'{preset}'" if preset else "the preset matching the failing toolchain"
    profile_clause = f"{profile_note}; " if profile_note else ""
    return (
        f"set sandbox.capability_profile: {preset or '<preset>'} in the project's forge.yaml "
        f"and re-sprint {ref} — {profile_clause}recorded capability payloads did not "
        f"grant the {named} preset's required write roots or mach services, so the "
        "dev agent could not build or verify its own work"
    )


def _recommend_actions(
    primary: str,
    contributing: list[str],
    story: dict,
    *,
    capability_preset: str | None = None,
    capability_profile_note: str | None = None,
) -> list[str]:
    """Map primary class + contributing factors to actionable next steps."""
    ref = _story_ref(story)
    actions: list[str] = []

    diagnose_ref = _issue_number(story) or ref
    base = {
        "provider_quota": (
            f"wait for quota reset or switch the provider/model, then re-sprint {ref}"
        ),
        "shared_infrastructure": (
            f"repair the shared run infrastructure named in the evidence above, then "
            f"re-run {ref} — the run aborted on the substrate, so this is not a "
            f"judgment about {ref}'s work: check its branch before re-sprinting, the "
            "commits it produced may already be complete"
        ),
        "worker_timeout": (
            f"inspect the worker log for the phase {ref} was in at timeout; "
            "split the story or raise the worker timeout, then re-run"
        ),
        "workspace_divergence": (
            f"resolve the base branch divergence (rebase/reconcile local vs origin), "
            f"then re-sprint {ref} — this is a mechanical workspace precondition failure, "
            "not a code defect requiring LLM diagnosis"
        ),
        "intake_shape": f"reshape the {ref} issue body to satisfy the intake gate, then re-run",
        "configuration_changed": (
            f"inspect the forge.yaml change recorded above (git log -p -- forge.yaml), commit "
            f"or revert it, then re-run {ref} under the intended configuration — the "
            "configuration changed while the story was in flight, so this is not a PR merge "
            "problem and the PR is not where the cause is"
        ),
        "merge_failed": _merge_failed_action(story, ref),
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
        "launch_collision": _launch_collision_action(story, ref),
        "sprint_state_stranded": (
            f"re-resume/reconcile the sprint so {ref}'s prior-generation state is "
            "recovered — do NOT clear the worktree and re-sprint fresh, which would "
            "discard partial work"
        ),
        "dependency_skip": _dependency_action(story, ref),
        "capability_profile_gap": _capability_gap_action(
            capability_preset, ref, capability_profile_note
        ),
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
    # A story that reached the merge queue produced a reviewed, PR-worthy commit;
    # its landing failed for a post-review reason. Iteration-budget advice there
    # names a cause the evidence contradicts (issue #1946).
    iteration_advice_applies = primary not in {
        "iteration_exhaustion",
        # The iteration limit is how a capability gap *presents*; the budget was
        # never the constraint, so budget advice here would restate the wrong
        # cause the class exists to replace (#2029).
        "capability_profile_gap",
        "merge_failed",
        # Same reasoning as merge_failed: the story produced a landable commit and
        # failed for a reason the iteration budget did not cause (#2056).
        "configuration_changed",
    }
    if "dev_iteration_limit" in contributing and iteration_advice_applies:
        actions.append("raise the dev iteration budget or narrow the story scope")
    if "review_iteration_limit" in contributing and iteration_advice_applies:
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
