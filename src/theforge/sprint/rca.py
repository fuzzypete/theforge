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
grep the taxonomy. The residual class ``unknown_needs_rca`` covers stories for
which *no mechanical signal was available at all* — they never silently drop;
LLM-assisted classification (``forge diagnose``) is reserved for that residual
and is intentionally out of this pure engine.

A story that no rule classified but whose run recorded its **own** determinate
cause code is a different situation and gets its own class, ``taxonomy_gap``
(#2292): the cause is already determined — forge generated it — so paying for an
investigation would re-derive what the run already states. What is missing is a
rule to receive it, and the entry says so, which is how the rule set grows from
the failures that actually occur.

Each story entry carries a *primary* failure class plus explicit *contributing
factors* — a real failure usually has one root cause and one or more amplifiers,
each with a different fix path.

``RULES`` below is the single discoverable location for the classifier's rule
set. Grep it to see everything the mechanical classifier knows.
"""

from __future__ import annotations

import datetime
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
RULESET_VERSION = 13
RCA_FILENAME = "sprint-rca.yaml"

# Outcomes that mean the story landed / succeeded. These stay accounted for in
# sprint-summary.yaml; the RCA file is the recovery surface for everything else.
DONE_OUTCOMES = frozenset({"DONE", "ALREADY_DONE"})

# Residual class assigned when no mechanical signal was available for a story.
UNKNOWN_CLASS = "unknown_needs_rca"

# Class assigned when no primary rule matched but the run recorded a determinate
# cause code of its own that the taxonomy has no rule for (#2292). It is a
# statement about the *rule set*, not about the story: the cause is known, the
# receiving rule is missing, and an LLM investigation would only restate what the
# run already says.
# A skip whose recorded reason no rule receives lands here too (#2312): the run
# stated why it stopped and the receiving rule is what is missing, which is the
# same state of knowledge this class already names. The entry quotes the recorded
# sentence, so an operator reads what the run said instead of acting on a class
# assembled from somewhere else.
TAXONOMY_GAP_CLASS = "taxonomy_gap"

# Class assigned when the sprint recorded that it *skipped* a story but recorded
# no reason on the row (#2373). The story's end state is not unknown — the run
# stated it — so it must not fall to ``unknown_needs_rca``, whose attached
# recommendation buys an LLM investigation to establish something the run already
# recorded. What is missing is the sentence saying why, and that is read from the
# run log, not from a paid diagnosis.
SKIP_REASON_UNRECORDED_CLASS = "skip_reason_unrecorded"

# Summary ``outcome`` values that mean the sprint recorded the story as skipped
# rather than failed. Keep in sync with ``StoryOutcome.is_skipped`` in
# ``sprint.story_state`` — this module is a pure function over on-disk artifacts
# and deliberately imports no coordinator/sprint runtime modules.
SKIPPED_OUTCOMES = frozenset({"SKIPPED", "PRESERVED", "OPERATOR_ACTION", "DECOMPOSED"})

# Per-story accounting status the coordinator records when a story's spend could
# not be measured. Keep in sync with ``coordinator.story_budget.STATUS_UNKNOWN``.
# It is accounting metadata reported *beside* the outcome and is never an input
# to classification: how a story ended and how well its cost was measured are
# separate facts (#2373).
COST_UNKNOWN_STATUS = "cost_unknown"

# ``error_type`` the coordinator stamps when a story's monetary allocation (or the
# non-review part of it a review reservation leaves) can no longer fund the work.
# Matched as a literal for the same reason as the abort code below — keep in sync
# with the ``state.error_type`` assignments in ``coordinator.engine`` and
# ``coordinator.review_pool``.
_ALLOCATION_EXHAUSTED_ERROR_TYPE = "allocation_exhausted"

# A cause code forge assigned to its own termination is lower-snake-case
# (``allocation_exhausted``, ``infrastructure_abort``); a Python exception class
# name that merely propagated into the field is not (``TimeoutError``,
# ``StoryCancelled``). Only the former is a *statement of cause* the taxonomy can
# be said to be missing a rule for.
_FORGE_CAUSE_CODE_RE = re.compile(r"[a-z][a-z0-9_]*\Z")

# Drop reason string a re-exec launch guard records for a worktree that belongs
# to a prior generation's *unfinished* story (stranded sprint state) rather than
# a genuine fresh collision. Matched as a literal here — the engine is a pure
# function over on-disk artifacts and deliberately avoids importing the
# collision/launch-guard modules (which pull in subprocess/lock machinery). Keep
# this in sync with ``launch_guard.REASON_STRANDED_WORKTREE``.
_STRANDED_WORKTREE_REASON = "stranded-prior-generation-worktree"

# ``failure_code`` the Claude runner records for an agent whose process ended
# before it emitted a terminal result event (#2427), and the forge-emitted phrase
# the dev phase writes into the story's error when it reports that ending. The
# code is read from the run's own per-iteration telemetry; the phrase is the
# fallback for a run whose telemetry is unavailable but whose recorded error
# still states the ending. Keep both in sync with
# ``agent_types.FAILURE_ENDED_WITHOUT_RESULT`` and
# ``coordinator.dev_phase.ENDED_WITHOUT_RESULT_PHRASE`` — this engine is a pure
# function over on-disk artifacts and imports no runtime modules.
_ENDED_WITHOUT_RESULT_CODE = "agent_ended_without_result"
_ENDED_WITHOUT_RESULT_PHRASE = (
    "stopped producing output and its process ended without a result event"
)
# Its sibling, and the reason the two are separate classes (#2832): this one
# names an invocation killed before it produced any output at all. There are no
# last words to quote — that is the fact — so the class carries no phrase
# fallback and is recognised only from the run's own telemetry. Keep in sync with
# ``agent_types.FAILURE_KILLED_BEFORE_OUTPUT``.
_KILLED_BEFORE_OUTPUT_CODE = "killed_before_output"
# Marker the dev phase puts before the captured agent text inside that message.
# The evidence excerpt is length-capped, so it leads with the agent's own words
# and drops the sentence wrapped around them — the words are the whole reason
# this class exists. Keep in sync with
# ``coordinator.dev_phase.LAST_SAID_MARKER``.
_LAST_SAID_MARKER = "it last said: "

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
    amplifier that never stands alone), ``"informational"`` (baseline evidence
    that never affects classification), or ``"residual"`` (applies only when no
    primary rule matched at all — it describes the state of the rule set rather
    than competing with a classification).
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


@dataclass(frozen=True)
class _SkipReasonRule:
    """Maps a reason the sprint recorded when it skipped a story to a rule.

    A skip is a decision the sprint made and wrote down in a sentence it
    controls (``_record_current_story_entry(..., error=reason)`` in
    ``sprint.runner``). That sentence — not the story's declared dependencies,
    and not text found in artifacts a *different* generation left in the story's
    log directory — is the material a skip classification is built from (#2312).
    """

    rule_id: str
    # Lowercased prefixes of the recorded reason. Prefix-matched (not substring)
    # so the reason must *begin* by stating this cause, which is how the runner
    # formats every one of them.
    prefixes: tuple[str, ...]


# Keep the prefixes in sync with the ``reason=``/``error=`` strings the runner
# records at each skip site. Order is match order.
_SKIP_REASON_RULES: tuple[_SkipReasonRule, ...] = (
    _SkipReasonRule("sprint_budget_unverifiable", ("budget unverifiable",)),
    # Before the credential rule below, which shares the "cancelled mid-flight:"
    # prefix: a story the sprint killed for its cap and one it killed for a
    # rejected credential are different causes, and the more specific prefix has
    # to be tried first or every budget halt reads as an auth outage (#2547).
    _SkipReasonRule("sprint_budget_halted_in_flight", ("cancelled mid-flight: budget",)),
    _SkipReasonRule("sprint_budget_exhausted", ("budget exhausted",)),
    # "blocked" covers both "blocked: <ref>" (unresolved external dependency) and
    # the bare "blocked" the DAG sweep records.
    _SkipReasonRule("dependency_blocked", ("blocked", "dependency failed:")),
    _SkipReasonRule(
        "agent_credential_rejected",
        ("agent credential rejected", "cancelled mid-flight:"),
    ),
    _SkipReasonRule("collision_gate_stood_down", ("collision gate stood down",)),
)


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
        description=(
            "Story skipped because a dependency blocked launch — read from the "
            "reason the sprint recorded for the skip, not inferred from the "
            "presence of a depends_on list."
        ),
    ),
    RcaRule(
        rule_id="sprint_budget_unverifiable",
        failure_class="sprint_budget_unverifiable",
        role="primary",
        description=(
            "The sprint refused to dispatch the story because it could not verify "
            "its own spend against the budget cap — some spend was unmeasured, so "
            "the measured total is a lower bound and the cap cannot be evaluated."
        ),
    ),
    RcaRule(
        rule_id="sprint_budget_exhausted",
        failure_class="sprint_budget_exhausted",
        role="primary",
        description=(
            "The sprint's measured spend met or passed the run's budget cap before "
            "the story was dispatched, so the story never started."
        ),
    ),
    RcaRule(
        rule_id="sprint_budget_halted_in_flight",
        failure_class="sprint_budget_halted_in_flight",
        role="primary",
        description=(
            "The sprint's spend met or passed the run's budget cap while this story "
            "was running, so the sprint cancelled it at its next phase boundary. "
            "Nothing judged the work — it is unfinished, not rejected."
        ),
    ),
    RcaRule(
        rule_id="agent_credential_rejected",
        failure_class="agent_auth_rejected",
        role="primary",
        description=(
            "The substrate rejected the agent credential, so the sprint stopped "
            "presenting it — the story was skipped or cancelled by that circuit "
            "breaker, not judged."
        ),
    ),
    RcaRule(
        rule_id="collision_gate_stood_down",
        failure_class="collision_stand_down",
        role="primary",
        description=(
            "The collision gate stood the story down before DEV: the files it "
            "planned to change are held by preserved work that has not landed."
        ),
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
        rule_id="dev_agent_ended_without_result",
        failure_class="agent_ended_without_result",
        role="primary",
        description=(
            "The dev agent stopped producing output and its process ended "
            "without a terminal result event. The run captured what the agent "
            "last said, so how it ended is recorded — not unknown."
        ),
    ),
    RcaRule(
        rule_id="dev_agent_killed_before_output",
        failure_class="agent_killed_before_output",
        role="primary",
        description=(
            "The dev invocation was killed before it produced any output — no "
            "stream event, no text, no usage. Nothing about the story was "
            "attempted, so this is a fact about the invocation rather than the "
            "work."
        ),
    ),
    RcaRule(
        rule_id="iteration_budget_exhausted",
        failure_class="iteration_exhaustion",
        role="primary",
        description="Dev or review hit its iteration limit and the story failed.",
    ),
    RcaRule(
        rule_id="story_allocation_exhausted",
        failure_class="allocation_exhaustion",
        role="primary",
        description=(
            "The story's monetary allocation — or the non-review part of it a "
            "review reservation leaves — could no longer fund the work, so the "
            "coordinator refused the next attempt and said so. Exhausting a "
            "budget of money is the same kind of event as exhausting a budget "
            "of iterations: an operator-set limit was reached, not a defect."
        ),
    ),
    # ── contributing factors (amplifiers) ────────────────────────────────────
    RcaRule(
        rule_id="pending_decision_auto_rejected",
        failure_class="operator_gate_timeout",
        role="contributing",
        description="An operator decision gate timed out with no decision received.",
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
    # ── residual (applies only when nothing classified the story) ────────────
    RcaRule(
        rule_id="unclassified_forge_cause_code",
        failure_class=TAXONOMY_GAP_CLASS,
        role="residual",
        description=(
            "The run recorded a determinate cause code of its own making that no "
            "rule in this taxonomy classifies. The cause was found, not missing — "
            "the rule to receive it is."
        ),
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
    # Run-level stop decisions the sprint took *about itself* before it dispatched
    # the story (#2312). They outrank dependency_skip because a story the sprint
    # never offered to a worker was not held back by its dependencies, whatever
    # its depends_on list still says.
    "agent_auth_rejected",
    "sprint_budget_unverifiable",
    # A story the sprint stopped mid-flight for its cap outranks the two
    # never-dispatched budget classes: it ran, and what ended it was the money.
    "sprint_budget_halted_in_flight",
    "sprint_budget_exhausted",
    "collision_stand_down",
    "dependency_skip",
    # A story whose dev agent could not run its toolchain because the project
    # declared no capability profile outranks the iteration exhaustion it
    # presents as (#2029): the budget was spent on iterations that could never
    # succeed, so "raise the budget" funds a repeat of the same failure. The
    # configuration gap is the cause; the exhausted budget is its symptom.
    "capability_profile_gap",
    # A story the coordinator refused to fund stopped on the money, whatever else
    # it had already spent: the iteration limit it may also have reached is not
    # what ended it, and raising that limit funds nothing (#2292). The capability
    # gap above still outranks it for the #2029 reason — a story that could never
    # build burned its allocation on attempts that could not succeed.
    "allocation_exhaustion",
    # An agent that ended without a result outranks the iteration exhaustion it
    # can present as (#2427): each iteration that ends this way consumes budget
    # without the work ever starting, so raising the limit buys more of the same
    # ending. It sits below the money and capability classes for the same reason
    # they sit above iteration exhaustion — those name a constraint that persists
    # across attempts, while this names how one attempt stopped.
    "agent_ended_without_result",
    # Above iteration exhaustion for a sharper version of the same reason
    # (#2832): an invocation killed before it produced anything never attempted
    # the story, so an iteration limit it presents as reaching describes
    # attempts that did not happen.
    "agent_killed_before_output",
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
    # Correlated once here for the same reason: the shortfall the coordinator
    # recorded supplies both the evidence excerpt and the figures the recommended
    # action names (#2292).
    allocation_shortfall = _allocation_shortfall(story, audit)

    # A skip the sprint recorded a reason for is classified from that reason and
    # from nothing else (#2312). The sprint stated why it stopped, in a sentence
    # it controls; every other signal available for a skipped story is either
    # about a different subject (a declared depends_on list the skip decision
    # never consulted) or, for a story that was never dispatched, about a
    # different run entirely (audit/log artifacts a prior generation left in the
    # story's log directory). Neither classifies the story, and neither is
    # carried as evidence for it — report surfaces quote evidence as the cause,
    # which is exactly how the wrong reason reached the operator.
    recorded_skip_reason = _recorded_skip_reason(story)
    skip_rule = _skip_reason_rule(recorded_skip_reason) if recorded_skip_reason else None

    hits: list[tuple[str, str, str, str | None, str]] = []
    hits.extend(_text_rule_hits(text_sources))
    hits.extend(
        _signal_rule_hits(
            story,
            audit,
            summary_path,
            sprint_log_dir,
            logs_root,
            capability_gap=capability_gap,
            allocation_shortfall=allocation_shortfall,
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
    audit_source = _rel(sprint_log_dir / slug / "audit.yaml", logs_root)
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
        if skip_rule is not None and rule_id != skip_rule.rule_id:
            # Dropped entirely — not even as evidence. The sprint stated why it
            # skipped this story; every other hit was matched in material the
            # skip decision never consulted (a declared depends_on list) or that
            # a *different* generation left behind (the story's log directory is
            # not rewritten for a story this run never dispatched). Carrying such
            # a hit as evidence is how the wrong reason reached the operator in
            # the first place: report surfaces quote evidence as the cause.
            continue
        seen_rules.add(rule_id)
        evidence.append({"source": source, "rule_id": rule_id, "excerpt": excerpt})
        if recorded_skip_reason is not None and source_kind != "structured":
            # The reason was recorded but no rule receives it. Other structured
            # facts of the run may still classify the story; a pattern matched in
            # scanned text may not — for a story that never ran, that text was
            # written by some other run about some other attempt.
            continue
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

    primary = _select_primary(structured_primary_classes, text_primary_classes)
    unclassified_code: str | None = None
    unclassified_skip_reason: str | None = None
    if primary is None and recorded_skip_reason is not None:
        # The sprint said why it skipped the story and no rule receives that
        # sentence. Report exactly that (#2312): naming a class from anything
        # else here would send the operator to a subject the run never named,
        # while the reason it did name sits unread in the log.
        unclassified_skip_reason = recorded_skip_reason
        evidence.append(
            {
                "source": summary_source,
                "rule_id": "unclassified_forge_cause_code",
                "excerpt": _truncate(
                    "the sprint recorded this reason for skipping the story, which no "
                    f"rule in this taxonomy classifies: {recorded_skip_reason}"
                ),
            }
        )
        primary = TAXONOMY_GAP_CLASS
    if primary is None:
        # Nothing classified the story. Before falling to the residual — which
        # tells the operator to stop reading and buy an investigation — ask
        # whether the run stated a cause of its own that this taxonomy simply has
        # no rule for (#2292). That is a gap in the rule set, and saying so is
        # both cheaper and truer than calling a stated cause unknown.
        gap = _unclassified_cause_code(story, audit, summary_source, audit_source)
        if gap is not None:
            unclassified_code, gap_source, gap_excerpt = gap
            evidence.append(
                {
                    "source": gap_source,
                    "rule_id": "unclassified_forge_cause_code",
                    "excerpt": gap_excerpt,
                }
            )
            primary = TAXONOMY_GAP_CLASS
        elif outcome in SKIPPED_OUTCOMES:
            # The sprint recorded that it skipped this story. That IS its end
            # state, recorded by the run itself, so the residual class — and the
            # paid investigation attached to it — must not claim the end state is
            # unknown (#2373). Only the reason is missing, and the run log is
            # where it is read from.
            evidence.append(
                {
                    "source": summary_source,
                    "rule_id": "skip_reason_unrecorded",
                    "excerpt": _truncate(
                        f"the sprint recorded outcome={outcome} for this story but recorded "
                        "no reason on its row; the story's end state is known and only the "
                        "reason is missing"
                    ),
                }
            )
            primary = SKIP_REASON_UNRECORDED_CLASS
        else:
            primary = UNKNOWN_CLASS

    # Evidence that supports the primary classification leads (#2312). Rendering
    # surfaces quote the first non-baseline excerpt as *the* reason a story
    # stopped, so an unrelated hit sitting first — a pattern matched in some
    # other run's leftover log — is displayed as the cause under a class it did
    # not produce. Ordering is otherwise preserved, and nothing is dropped.
    evidence = _primary_evidence_first(evidence, primary)

    # Always append the baseline outcome evidence last.
    evidence.append(
        {
            "source": summary_source,
            "rule_id": "captured_outcome",
            "excerpt": _truncate(baseline_excerpt),
        }
    )

    # Contributing factors: unique, in rule-declaration order, minus whichever
    # class was elevated to primary.
    #
    # When the engine could not classify the failure at all, its own uncertainty
    # propagates (#2031): a text-scan-derived amplifier is a match on vocabulary,
    # and asserting one confidently — plus the remediation the operator is then
    # told to go perform — on a run forge has just declared unexplained costs more
    # trust than declining to guess. Field-derived (structured) factors are the
    # run's own recorded facts, not an inference, so they still stand. A taxonomy
    # gap is the same state of knowledge — no rule classified the story — so it
    # withholds text-derived amplifiers on the same grounds, as does an
    # unrecorded skip reason (#2373).
    contributing = _dedupe_ordered(
        [
            failure_class
            for failure_class, source_kind in contributing_hits
            if primary not in {UNKNOWN_CLASS, TAXONOMY_GAP_CLASS, SKIP_REASON_UNRECORDED_CLASS}
            or source_kind == "structured"
        ]
    )

    partial_value = _detect_partial_value(story, audit)
    actions = _recommend_actions(
        primary,
        contributing,
        story,
        capability_preset=capability_gap.preset if capability_gap else None,
        capability_profile_note=capability_gap.profile_note if capability_gap else None,
        allocation_shortfall=allocation_shortfall,
        unclassified_code=unclassified_code,
        unclassified_skip_reason=unclassified_skip_reason,
    )

    entry = {
        "primary_failure_class": primary,
        "contributing_factors": contributing,
        "evidence": evidence,
        "partial_value": partial_value,
        # How well the story's spend was measured, recorded beside the outcome
        # and never folded into it (#2373).
        "cost_accounting": _cost_accounting(story),
        "recommended_next_actions": actions,
    }
    # Only present when the run spent on preparation for dev work that then
    # produced nothing — the money spent and the work attempted are different
    # facts, and the total alone reads as the story having been worked on (#2427).
    spend_shape = _spend_shape(story, audit)
    if spend_shape is not None:
        entry["spend_shape"] = spend_shape
    # Only present when the surfaces describing this one story disagree; an
    # operator reading one of them otherwise has no way to know (#2373).
    consistency = _outcome_consistency(story, audit, outcome, summary_source, audit_source)
    if consistency is not None:
        entry["outcome_consistency"] = consistency
    return entry


def _cost_accounting(story: dict) -> dict:
    """Report whether the story's spend was measured — accounting, not outcome.

    Unmeasured spend is a condition of the run's accounting, not a statement
    about how the story ended, so it is reported here rather than allowed to
    consume the outcome or the recommendation derived from it (#2373).
    """
    # Same default as ``status_reader._story_cost_usd``: an explicit ``None`` is
    # unmeasured spend, an absent key is a legacy row and not a claim either way.
    cost = story.get("cost_usd", 0.0)
    measured = isinstance(cost, (int, float)) and not isinstance(cost, bool)
    accounting: dict = {"measured": measured}
    if not measured:
        accounting["status"] = COST_UNKNOWN_STATUS
    return accounting


def _outcome_consistency(
    story: dict,
    audit: dict,
    outcome: str,
    summary_source: str,
    audit_source: str,
) -> dict | None:
    """Report disagreement between the summary row and the per-story audit.

    The sprint's recorded outcome is authoritative — it is the run's statement
    about how the story ended — while the audit's ``final_phase``/``success``
    describe how far the story got and whether the phase it reached succeeded. A
    story approved at PLAN_REVIEW and then skipped for a failed dependency
    carries both, and they read as a contradiction to an operator holding only
    one of them (#2373). Returns ``None`` when there is nothing to report.
    """
    outcome_block = audit.get("outcome") if isinstance(audit, dict) else None
    if not isinstance(outcome_block, dict) or not outcome:
        return None
    success = outcome_block.get("success")
    final_phase = _nonempty(outcome_block.get("final_phase"))
    if success is not True or outcome in DONE_OUTCOMES:
        return None
    return {
        "agrees": False,
        "summary_outcome": outcome,
        "summary_source": summary_source,
        "audit_final_phase": final_phase,
        "audit_success": True,
        "audit_source": audit_source,
        "authoritative": "summary_outcome",
        "note": _truncate(
            f"the per-story audit records a successful {final_phase or 'phase'} while the "
            f"sprint recorded outcome={outcome}; the sprint's recorded outcome is how the "
            "story ended and the audit names the phase it reached"
        ),
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
    # iteration logs, captured agent output yaml, etc.) that falls within this
    # attempt's recorded time window. The story directory persists across
    # attempts of the same story in one sprint; without attempt scoping, stale
    # artifacts from an earlier attempt can misclassify the current failure.
    story_dir = sprint_log_dir / slug
    attempt_started_at = _story_attempt_started_at(story, audit)
    attempt_run_ids = _story_attempt_run_ids(story, audit)
    if story_dir.is_dir():
        for path in sorted(story_dir.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".log", ".txt", ".yaml", ".yml", ".md", ".json"}:
                continue
            if path.name in {"audit.yaml", ".artifact-owners.yaml"}:
                continue
            if not _artifact_belongs_to_attempt(
                path,
                story_dir=story_dir,
                attempt_run_ids=attempt_run_ids,
                started_at=attempt_started_at,
            ):
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


def _story_attempt_started_at(story: dict, audit: dict) -> datetime.datetime | None:
    """Return the current attempt's recorded start time, if present.

    The summary row and per-story audit are the run's own records for this
    attempt. When either supplies a start time, use it to exclude older leftover
    files from prior attempts in the same per-story directory. Legacy fixtures
    without timing keep the historical "scan everything" behavior.
    """
    summary_started = _parse_attempt_timestamp(story.get("started_at"))
    timing = audit.get("timing") if isinstance(audit, dict) else None
    audit_started = (
        _parse_attempt_timestamp(timing.get("started_at")) if isinstance(timing, dict) else None
    )
    starts = [ts for ts in (summary_started, audit_started) if ts is not None]
    return min(starts) if starts else None


def _story_attempt_run_ids(story: dict, audit: dict) -> set[str]:
    """Return the current attempt's recorded run identities, if present."""
    ids: set[str] = set()
    candidates = [story.get("story_run_id")]
    if isinstance(audit, dict):
        candidates.append(audit.get("run_id"))
    for value in candidates:
        if isinstance(value, str):
            text = value.strip()
            if text:
                ids.add(text)
    return ids


def _parse_attempt_timestamp(value: object) -> datetime.datetime | None:
    """Parse an ISO-8601 timestamp emitted by sprint/audit records."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _artifact_belongs_to_attempt(
    path: Path,
    *,
    story_dir: Path,
    attempt_run_ids: set[str],
    started_at: datetime.datetime | None,
) -> bool:
    """Return whether *path* belongs to the current story attempt."""
    owner_run_id = _artifact_owner_run_id(story_dir, path)
    if owner_run_id is not None:
        return bool(attempt_run_ids) and owner_run_id in attempt_run_ids
    return _path_in_attempt_window(path, started_at)


def _artifact_owner_run_id(story_dir: Path, path: Path) -> str | None:
    """Return the run id recorded for *path* in the story artifact manifest."""
    manifest = _load_yaml(story_dir / ".artifact-owners.yaml")
    if not isinstance(manifest, dict):
        return None
    entries = manifest.get("artifacts")
    if not isinstance(entries, dict):
        return None
    owner = entries.get(_rel(path, story_dir))
    return owner.strip() if isinstance(owner, str) and owner.strip() else None


def _path_in_attempt_window(path: Path, started_at: datetime.datetime | None) -> bool:
    """Return whether *path* was written after the current attempt began."""
    if started_at is None:
        return True
    try:
        modified_at = datetime.datetime.fromtimestamp(path.stat().st_mtime, datetime.timezone.utc)
    except OSError:
        return False
    tolerance = datetime.timedelta(seconds=1)
    if modified_at + tolerance < started_at:
        return False
    return True


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


def _ended_without_result_evidence(
    story: dict,
    audit: dict,
    summary_source: str,
    audit_source: str,
    outcome: str,
) -> tuple[str, str, str] | None:
    """Return the hit for a dev invocation whose own record named how it ended.

    Two endings, kept apart on purpose (#2427, #2832): an agent that *ran* and
    stopped before its terminal result event (``agent_ended_without_result``,
    which quotes the agent's last words), and an invocation killed before it
    produced anything at all (``agent_killed_before_output``, which has none to
    quote). Reporting the second as the first would describe a story that never
    started as one whose agent went quiet.

    Two sources, preferred in order of authority:

    1. The run's own per-iteration telemetry — the ``runner_failure_code`` the
       runner recorded for the dev iteration. Generated by forge about its own
       execution, so it is a statement of cause rather than a match on
       vocabulary.
    2. The ending the dev phase stated in the error it recorded, for a run whose
       per-iteration telemetry is unavailable.

    Only the *terminal* dev iteration is consulted: an earlier iteration that
    ended this way and was then retried into real work did not end the story,
    and naming it would describe a recovered attempt as the cause.

    The evidence quotes the agent's own last words. The whole point of the class
    is that an operator reads why the run ended without opening the
    dev-iteration log, and without buying an investigation to rediscover a cause
    the run already stated.
    """
    if outcome in SKIPPED_OUTCOMES or outcome in DONE_OUTCOMES:
        return None

    error = _nonempty(story.get("error"))
    outcome_block = audit.get("outcome") if isinstance(audit, dict) else None
    message = _nonempty(outcome_block.get("message")) if isinstance(outcome_block, dict) else None
    stated = error or message or f"outcome={outcome}"

    if _infrastructure_failure_code(audit) == _KILLED_BEFORE_OUTPUT_CODE:
        # Read from the invocation-failure record rather than the dev-iteration
        # telemetry the sibling class below uses, because this shape has no such
        # telemetry by construction: the attempt is rolled back out of ordinary
        # dev accounting precisely because it never ran (#2832). No ``it last
        # said`` clause either — the invocation produced nothing to quote, and a
        # stand-in would blur the one distinction the class carries.
        return (
            "dev_agent_killed_before_output",
            audit_source,
            _truncate(
                "the dev invocation was killed before it produced any output; "
                "nothing about the story was attempted, so its retry allowance "
                "was not spent on this attempt"
            ),
        )

    dev_entries = _dev_loop_entries(audit)
    if dev_entries:
        terminal = dev_entries[-1]
        if _nonempty(terminal.get("runner_failure_code")) == _ENDED_WITHOUT_RESULT_CODE:
            # The captured agent text, recorded as its own field by the dev
            # phase. Quoted in preference to the sentence forge wrapped around
            # it: the excerpt is length-capped, and spending that budget on
            # forge's framing is how the one thing the operator needed became
            # the part that got cut.
            said = _nonempty(terminal.get("runner_failure_summary"))
            return (
                "dev_agent_ended_without_result",
                audit_source,
                _truncate(
                    f"the dev agent ended without a result event; {_LAST_SAID_MARKER}{said}"
                    if said
                    else stated
                ),
            )

    if _ENDED_WITHOUT_RESULT_PHRASE in (error or "").lower():
        return ("dev_agent_ended_without_result", summary_source, _truncate(stated))
    if _ENDED_WITHOUT_RESULT_PHRASE in (message or "").lower():
        return ("dev_agent_ended_without_result", audit_source, _truncate(stated))
    return None


def _infrastructure_failure_code(audit: dict) -> str | None:
    """``failure_code`` from the run's infrastructure-abort record, if it has one.

    ``agent_invocation.infrastructure_failure`` is written only when the run
    ended because no agent judgment could be obtained, so it is the run's own
    statement about its own execution — the same class of evidence as the
    per-iteration telemetry, for a shape that has none (#2832).
    """
    block = audit.get("agent_invocation") if isinstance(audit, dict) else None
    failure = block.get("infrastructure_failure") if isinstance(block, dict) else None
    return _nonempty(failure.get("failure_code")) if isinstance(failure, dict) else None


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


def _allocation_shortfall(story: dict, audit: dict) -> dict | None:
    """Return the allocation-exhausted payload the coordinator recorded, if any.

    The coordinator writes the shortfall it refused on, in a shape it owns, to
    three places that reach this engine: the per-story audit's ``cost`` block, the
    ``story_allocation`` block on the summary row, and — for a run whose payload
    did not survive — the ``allocation_exhausted`` ``error_type`` on either. The
    payload is preferred wherever present because it carries the figures as
    separate values, so the evidence and the operator action can name them
    without re-parsing the sentence the run already formatted (#2292).

    A bare ``{}`` payload is not a shortfall: the field is written unconditionally
    and is empty on every run that never exhausted anything.
    """
    candidates: list[object] = []
    cost = audit.get("cost") if isinstance(audit, dict) else None
    if isinstance(cost, dict):
        candidates.append(cost.get("allocation_exhausted"))
    allocation_block = story.get("story_allocation")
    if isinstance(allocation_block, dict):
        candidates.append(allocation_block.get("allocation_exhausted"))
    for payload in candidates:
        if isinstance(payload, dict) and payload:
            return payload
    # No payload — fall back to the run's own terminal cause code / status, which
    # still says *which* condition stopped the story even when the figures are
    # gone. Returning an empty dict keeps "exhausted, figures unavailable"
    # distinguishable from "not exhausted" (None).
    outcome_block = audit.get("outcome") if isinstance(audit, dict) else None
    codes = {
        (_nonempty(story.get("error_type")) or "").lower(),
        (_nonempty(story.get("outcome_code")) or "").lower(),
        (
            (_nonempty(outcome_block.get("error_type")) or "").lower()
            if isinstance(outcome_block, dict)
            else ""
        ),
        (
            (_nonempty(allocation_block.get("status")) or "").lower()
            if isinstance(allocation_block, dict)
            else ""
        ),
    }
    if _ALLOCATION_EXHAUSTED_ERROR_TYPE in codes:
        return {}
    return None


def _money(value: object) -> str | None:
    """Format a recorded dollar figure, or ``None`` when it is not a number."""
    try:
        return f"${float(value):.2f}"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _allocation_exhaustion_excerpt(shortfall: dict) -> str:
    """Describe the shortfall from the figures the coordinator separated out."""
    recorded_phase = _nonempty(shortfall.get("phase"))
    phase = recorded_phase or "further"
    allocation = _money(shortfall.get("allocation_usd"))
    observed = _money(shortfall.get("observed_usd"))
    allocation_clause = f" of a {allocation} allocation" if allocation else ""
    if shortfall.get("nonreview_exhausted"):
        nonreview = _money(shortfall.get("nonreview_allocation_usd"))
        reserved = _money(shortfall.get("reserved_review_usd"))
        cycles = shortfall.get("reserved_review_cycles")
        detail = (
            f"spent {observed} of the {nonreview} left for non-review work"
            if observed and nonreview
            else "the non-review balance is gone"
        )
        reserve = f"; {reserved} stays reserved for {cycles} review cycle(s)" if reserved else ""
        return _truncate(
            f"story allocation exhausted: {detail}{allocation_clause}, so no further "
            f"{phase} attempt is funded{reserve}"
        )
    planned = _money(shortfall.get("planned_usd"))
    remaining = _money(shortfall.get("remaining_usd"))
    if shortfall.get("projected"):
        need = f" needs {planned} for {phase}," if planned else ""
        left = f" {remaining} left" if remaining else ""
        return _truncate(
            f"story allocation exhausted at seating, before dev spent:{need}{left}"
            f"{allocation_clause} (projected spend {observed or 'unmeasured'})"
        )
    if planned and remaining:
        return _truncate(
            f"story allocation exhausted: {phase} needs {planned}, {remaining} left"
            f"{allocation_clause} (observed {observed or 'unmeasured'})"
        )
    refused = f"the next {recorded_phase} attempt" if recorded_phase else "further work"
    return _truncate(
        f"story allocation exhausted: the coordinator refused to fund {refused}{allocation_clause}"
    )


def _unclassified_cause_code(
    story: dict,
    audit: dict,
    summary_source: str,
    audit_source: str,
) -> tuple[str, str, str] | None:
    """Return the run's own unclassified cause code, or ``None`` (#2292).

    Only ``error_type`` is consulted, and only when it is lower-snake-case. A
    cause code forge assigned to its own termination is generated, not matched,
    which makes it the most reliable material this classifier can hold — but
    ``outcome_code`` degrades to the lowercased outcome (``failed``, ``escalate``)
    when no error type was set, and an exception class name that merely
    propagated into ``error_type`` (``TimeoutError``) names a Python type, not a
    cause. Neither is a statement of cause, so neither may be reported as one the
    taxonomy is missing a rule for.
    """
    outcome = str(story.get("outcome") or "").strip().lower()
    outcome_block = audit.get("outcome") if isinstance(audit, dict) else None
    audit_error = (
        _nonempty(outcome_block.get("error")) if isinstance(outcome_block, dict) else None
    )
    audit_code = (
        _nonempty(outcome_block.get("error_type")) if isinstance(outcome_block, dict) else None
    )
    for code, source, detail in (
        (_nonempty(story.get("error_type")), summary_source, _nonempty(story.get("error"))),
        (audit_code, audit_source, audit_error or _nonempty(story.get("error"))),
    ):
        if not code or not _FORGE_CAUSE_CODE_RE.match(code) or code.lower() == outcome:
            continue
        said = f": {detail}" if detail else ""
        return (
            code,
            source,
            _truncate(
                f"the run recorded its own terminal cause code '{code}', which no rule in "
                f"this taxonomy classifies{said}"
            ),
        )
    return None


def _recorded_skip_reason(story: dict) -> str | None:
    """Return the reason the sprint recorded when it skipped this story (#2312).

    ``None`` when the story did not finish SKIPPED, or when the sprint recorded
    no reason — a distinction that matters, because a recorded reason is a
    statement the run made about itself and its absence is not evidence of
    anything.
    """
    if str(story.get("outcome") or "").upper() != "SKIPPED":
        return None
    return _nonempty(story.get("error"))


def _skip_reason_rule(reason: str) -> _SkipReasonRule | None:
    """Return the skip rule whose recorded reason this is, or ``None``."""
    text = reason.strip().lower()
    for rule in _SKIP_REASON_RULES:
        if any(text.startswith(prefix) for prefix in rule.prefixes):
            return rule
    return None


def _signal_rule_hits(
    story: dict,
    audit: dict,
    summary_path: Path,
    sprint_log_dir: Path,
    logs_root: Path,
    capability_gap: _CapabilityGap | None = None,
    allocation_shortfall: dict | None = None,
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
        skip_reason = _recorded_skip_reason(story)
        skip_rule = _skip_reason_rule(skip_reason) if skip_reason else None
        if skip_rule is not None:
            # The sprint's own sentence, quoted. Nothing is added to it and nothing
            # is inferred around it.
            hits.append(
                (
                    skip_rule.rule_id,
                    summary_source,
                    _truncate(f"skipped; the sprint recorded: {skip_reason}"),
                    "structured",
                )
            )
        elif skip_reason is None:
            # No reason was recorded at all, so there is nothing of the run's own
            # to read. A declared dependency list is the one remaining structured
            # signal; it is reported as what it is — a declared dependency, not a
            # verified-unmet one.
            deps = list(story.get("depends_on") or [])
            if deps:
                hits.append(
                    (
                        "dependency_blocked",
                        summary_source,
                        _truncate(
                            "skipped with no recorded reason; declared dependencies: "
                            + ", ".join(str(d) for d in deps)
                        ),
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

    # An agent that ended without a result event (#2427) — read from the run's
    # own per-iteration telemetry, or from the ending the dev phase stated in the
    # error it recorded. Emitted before the iteration signals below for the same
    # reason as the allocation shortfall: both can be present and only one of
    # them ended the story.
    no_result_hit = _ended_without_result_evidence(
        story, audit, summary_source, audit_source, outcome
    )
    if no_result_hit is not None:
        hits.append((*no_result_hit, "structured"))

    # Monetary allocation exhaustion (#2292) — the coordinator's own refusal,
    # read from the shortfall payload it recorded rather than scanned for in the
    # sentence it formatted. Emitted before the iteration signals below because
    # both can be present on the same story and only one of them ended it.
    if allocation_shortfall is not None:
        allocation_source = (
            audit_source
            if isinstance(audit.get("cost"), dict)
            and (audit["cost"].get("allocation_exhausted") or {})
            else summary_source
        )
        hits.append(
            (
                "story_allocation_exhausted",
                allocation_source,
                _allocation_exhaustion_excerpt(allocation_shortfall),
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
    """Surface mechanically-detectable partial value produced before failure.

    An invocation is not an artifact: dev iterations that changed nothing are
    counted out here, because "dev produced 1 iteration(s) of work" sends an
    operator to reuse a branch holding none of it (#2427). Where the run kept no
    per-iteration record, the invocation count is all there is and stands.
    """
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

    dev_entries = _dev_loop_entries(audit)
    if dev_entries:
        dev_invocations = sum(1 for entry in dev_entries if _dev_entry_produced_work(entry))
    if dev_invocations > 0:
        values.append(f"dev produced {dev_invocations} iteration(s) of work")
    outcome = str(story.get("outcome") or "").upper()
    if outcome in {"ESCALATE", "ESCALATED", "PRESERVED"} and workspace_path:
        detail = f"workspace preserved at {workspace_path}"
        if branch:
            detail += f" (branch {branch})"
        values.append(detail)
    return values


def _dev_entry_produced_work(entry: dict) -> bool:
    """True when one recorded dev iteration left something behind.

    Files changed is the artifact; ``meaningful_progress`` is the coordinator's
    own judgment of the same iteration and is honoured where it was recorded.
    """
    try:
        changed = int(entry.get("files_changed_count") or 0)
    except (TypeError, ValueError):
        changed = 0
    return changed > 0 or entry.get("meaningful_progress") is True


def _spend_shape(story: dict, audit: dict) -> dict | None:
    """Report spend on preparation for dev work that never produced (#2427).

    What a story cost and what it attempted are different facts, and a run that
    spent most of its money on preflight/planning before a dev iteration ended
    having changed nothing reads, from the total alone, as a story that was
    worked on expensively. Reported only when the run's own records carry both
    halves — a measured total and at least one dev iteration that produced
    nothing — and never as a classification: an ordinary failure can have this
    shape too, and where it does the operator is still owed the split.
    """
    cost = audit.get("cost") if isinstance(audit, dict) else None
    if not isinstance(cost, dict):
        return None
    dev_entries = _dev_loop_entries(audit)
    if not dev_entries or any(_dev_entry_produced_work(entry) for entry in dev_entries):
        return None

    total = cost.get("total_usd")
    dev = cost.get("dev_usd")
    if not isinstance(total, (int, float)) or isinstance(total, bool):
        return None
    if not isinstance(dev, (int, float)) or isinstance(dev, bool):
        dev = 0.0
    before_dev = round(float(total) - float(dev), 6)
    if before_dev <= 0:
        return None

    return {
        "total_usd": round(float(total), 6),
        "dev_usd": round(float(dev), 6),
        "before_dev_usd": before_dev,
        "dev_iterations": len(dev_entries),
        "dev_iterations_producing_work": 0,
        "note": _truncate(
            f"${float(total):.2f} was spent on this story and ${before_dev:.2f} of it went "
            f"to preparation before dev; the {len(dev_entries)} dev iteration(s) it paid "
            "for changed no files"
        ),
    }


def _merge_failed_action(story: dict, ref: str) -> str:
    """Next step for a failed landing, branched on the evidenced cause.

    A merge can fail for reasons that call for opposite operator responses, and
    the recorded error text is the evidence that distinguishes them. Naming a
    merge conflict unconditionally (the prior behavior) is wrong whenever the PR
    was mergeable and merely red, or merely slow — issue #1946.

    A check that stopped without judging the change is a fourth case, and the
    opposite of a red one: nothing is known to be wrong with the code, so the
    recovery is to dispatch the check again — issue #2270.
    """
    error = (_nonempty(story.get("error")) or "").lower()
    if "never produced a verdict" in error:
        return (
            f"re-dispatch the required checks named in {ref}'s merge failure — they "
            "stopped without judging the change (cancelled, superseded, or interrupted), "
            "so the change itself is untested, not rejected"
        )
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


def _allocation_exhaustion_action(ref: str, shortfall: dict | None) -> str:
    """Next step for a story the coordinator refused to fund further (#2292).

    Money and iterations are the same kind of limit, so the advice has the same
    shape: raise the limit or reduce the work. What differs is *which* limit,
    and that follows from the basis the allocation was derived on — a configured
    per-story budget is a number the operator can raise, while a band-derived one
    says this story cost more than its complexity band ever has.

    Deliberately never proposes ``forge diagnose``: the coordinator stated the
    cause when it stopped, so an investigation would be paid to conclude it.
    """
    shortfall = shortfall or {}
    allocation = _money(shortfall.get("allocation_usd"))
    named = f" ({allocation} for this story)" if allocation else ""
    basis = _nonempty(shortfall.get("basis"))
    if basis == "configured_fallback":
        raise_it = (
            f"raise the configured per-story budget (models.budget_usd){named} — the "
            "allocation is the configured fallback, its complexity band having too "
            "little history to derive one"
        )
    elif basis:
        score = shortfall.get("complexity_score")
        band = f" complexity score {score}" if score is not None else " its complexity band"
        raise_it = (
            f"narrow or split {ref}, or raise its allocation{named} — the allocation is "
            f"derived from what{band} has actually cost, so the story spent beyond "
            "everything its band has ever spent"
        )
    else:
        raise_it = f"raise {ref}'s allocation{named} or narrow its scope"

    if shortfall.get("projected"):
        return (
            f"reduce the review cycles seated for {ref} (or raise its allocation), then "
            "re-run — the seating check refused before dev spent anything: the permitted "
            f"review cycles cost more than the allocation{named} leaves after the dev "
            "estimate. Nothing is wrong with the change; there is nothing to diagnose"
        )
    if shortfall.get("nonreview_exhausted"):
        reserved = _money(shortfall.get("reserved_review_usd"))
        held = (
            f" ({reserved} is still held for the review cycles it was seated with)"
            if reserved
            else ""
        )
        return (
            f"{raise_it}, then re-run — non-review spend reached everything the "
            f"allocation leaves for it{held}. This is a spending limit forge enforced "
            "deliberately, not a defect in the change: there is nothing to diagnose"
        )
    return (
        f"{raise_it}, then re-run — the story's monetary allocation ran out, so the "
        "coordinator refused to fund more work. This is an operator-set limit reached, "
        "not a defect in the change: there is nothing to diagnose"
    )


def _taxonomy_gap_action(ref: str, code: str | None, *, skip_reason: str | None = None) -> str:
    """Next step when the run stated a cause the taxonomy has no rule for (#2292).

    ``skip_reason`` is the sprint's own recorded reason for skipping the story
    when that sentence is what went unclassified (#2312). The operator is told
    the reason was not classified, and pointed at the sentence — not at a
    remediation for a class this engine could not establish.
    """
    if skip_reason is not None:
        return (
            f'read the skip reason the sprint recorded for {ref} — "{_truncate(skip_reason)}" — '
            "which no rule in this taxonomy classifies; add a classifier rule for it to the "
            "RULES set in theforge/sprint/rca.py. No remediation is recommended here: the "
            "reason is stated but its class is not established, and acting on a guessed class "
            "costs more than acting on none"
        )
    named = f"'{code}'" if code else "the cause code recorded above"
    return (
        f"add a classifier rule for {named} to the RULES set in theforge/sprint/rca.py "
        f"and file the taxonomy gap — {ref}'s cause is already determined by forge's own "
        "record of its own execution, so an LLM investigation would be paid to restate it"
    )


def _primary_evidence_first(evidence: list[dict], primary: str) -> list[dict]:
    """Stable-partition ``evidence`` so hits for the primary class come first."""
    leading = [
        item for item in evidence if _rule_failure_class(str(item.get("rule_id") or "")) == primary
    ]
    if not leading:
        return evidence
    trailing = [
        item for item in evidence if _rule_failure_class(str(item.get("rule_id") or "")) != primary
    ]
    return leading + trailing


def _rule_failure_class(rule_id: str) -> str | None:
    rule = RULES_BY_ID.get(rule_id)
    return rule.failure_class if rule is not None else None


def _recommend_actions(
    primary: str,
    contributing: list[str],
    story: dict,
    *,
    capability_preset: str | None = None,
    capability_profile_note: str | None = None,
    allocation_shortfall: dict | None = None,
    unclassified_code: str | None = None,
    unclassified_skip_reason: str | None = None,
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
        "sprint_budget_unverifiable": (
            f"{ref} was never dispatched: the sprint could not verify its spend against "
            "the budget cap because some spend was unmeasured (the sources are named in "
            "the evidence above), so it stopped rather than work against a total it "
            f"knows is a lower bound — resolve the unmeasured spend or re-sprint {ref} "
            "in a run whose cap can be evaluated"
        ),
        "sprint_budget_exhausted": (
            f"the sprint's budget cap was reached before {ref} was dispatched — raise the "
            f"budget or re-sprint {ref} in a new run; nothing about {ref}'s own work was "
            "judged"
        ),
        "sprint_budget_halted_in_flight": (
            f"the sprint's budget cap was reached while {ref} was running, so the sprint "
            f"cancelled it at its next phase boundary — raise the budget or re-sprint {ref} "
            "in a new run; its work is unfinished, not rejected, and no model judged it"
        ),
        "agent_auth_rejected": (
            f"re-authenticate the agent credential the run recorded as rejected, then "
            f"re-sprint {ref} — the credential circuit breaker stopped the story, so "
            "this is not a judgment about its work"
        ),
        "collision_stand_down": (
            f"land or clear the preserved work holding the files {ref} planned to change, "
            f"then re-sprint {ref}"
        ),
        "capability_profile_gap": _capability_gap_action(
            capability_preset, ref, capability_profile_note
        ),
        "iteration_exhaustion": (
            f"raise the iteration budget for {ref} or narrow its scope, then re-run"
        ),
        "allocation_exhaustion": _allocation_exhaustion_action(ref, allocation_shortfall),
        "agent_ended_without_result": (
            f"read the agent's last output quoted in the evidence above, then re-sprint "
            f"{ref} — its dev agent stopped producing output and its process ended without "
            "a result event, which the run recorded, so there is nothing here for a paid "
            "diagnosis to establish. Where the agent said it was waiting to be notified of "
            "something, that notification was never going to arrive on this transport"
        ),
        "agent_killed_before_output": (
            f"re-sprint {ref} — its dev invocation was killed before it produced any "
            "output, so nothing about the story was attempted and there is no agent "
            "judgment here to diagnose. The measured shape (#2832) is the CLI's stream "
            "closing without a single event, after which the runner kills it at the "
            "post-stream exit grace; look at what else was loading the host at that "
            "moment, such as a prior iteration's backgrounded work. The story's retry "
            "allowance was not spent on this attempt"
        ),
        TAXONOMY_GAP_CLASS: _taxonomy_gap_action(
            ref, unclassified_code, skip_reason=unclassified_skip_reason
        ),
        SKIP_REASON_UNRECORDED_CLASS: (
            f"read the sprint log for the line recording the skip of {ref}: the sprint "
            "skipped the story and recorded no reason on its row, so the reason — not the "
            "outcome — is what is missing. Nothing about the story's work failed and there "
            "is nothing to diagnose"
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
        # The story stopped on money, not on iterations. Raising an iteration
        # budget buys attempts the allocation will not fund (#2292).
        "allocation_exhaustion",
        # The run stopped on shared infrastructure before the story was judged,
        # so iteration-budget advice points at a lever the evidence says had no
        # bearing on the outcome.
        "shared_infrastructure",
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
    # Prefer the dependencies the sprint named in its own skip reason over the
    # story's declared depends_on list (#2312): the declared list also contains
    # dependencies that already landed, and sending an operator to a satisfied
    # dependency spends the time the real blocker needed.
    deps = _recorded_blocking_deps(story) or [str(d) for d in (story.get("depends_on") or [])]
    if deps:
        return f"land blocking dependencies ({', '.join(deps)}) then re-sprint {ref}"
    return f"resolve the blocker preventing {ref} from launching, then re-sprint it"


def _recorded_blocking_deps(story: dict) -> list[str]:
    """Dependencies named in the sprint's own recorded skip reason, if any.

    The runner formats these as ``blocked: a, b`` / ``dependency failed: a``. A
    bare ``blocked`` names none, and none are invented for it.
    """
    reason = _recorded_skip_reason(story)
    if not reason:
        return []
    lowered = reason.strip().lower()
    for prefix in ("blocked:", "dependency failed:"):
        if lowered.startswith(prefix):
            listed = reason.strip()[len(prefix) :]
            return [part.strip() for part in listed.split(",") if part.strip()]
    return []


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
