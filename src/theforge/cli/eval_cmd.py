"""forge eval-preflight subcommand — evaluate candidate preflight models."""

from __future__ import annotations

import sys
from pathlib import Path

from theforge.cli.shared import _find_config, load_config_checked
from theforge.config import load_config
from theforge.config.model_identity import PHASE_PREFLIGHT
from theforge.config.profiles import iter_config_profiles


def _infer_profile(
    model: str,
    budget_usd: float,
    timeout_seconds: int,
) -> object:
    """Build a minimal ModelProfile for a candidate model.

    Transport inference rules (from ModelProfile.mode: "api" if provider else "cli"):
      - "claude-*"      → CLI runner, cli="claude"
      - "gpt-*"         → API runner, provider="openai"
      - "deepseek-*"    → API runner, provider="deepseek"
      - "gemini-*"      → API runner, provider="google"
      - other           → CLI runner, cli=model (best-effort)

    The preflight phase uses allowed_tools for file exploration; use a broad
    default set that matches the existing preflight_profile convention.
    """
    from theforge.config.types import ModelProfile

    DEFAULT_TOOLS = (
        "Read",
        "Glob",
        "Grep",
        "Bash",
        "WebSearch",
        "WebFetch",
    )

    if model.startswith("claude-"):
        return ModelProfile(
            name=f"eval-{model}",
            model=model,
            budget_usd=budget_usd,
            timeout_seconds=timeout_seconds,
            allowed_tools=DEFAULT_TOOLS,
            cli="claude",
            phase=PHASE_PREFLIGHT,
        )
    elif model.startswith("gpt-"):
        return ModelProfile(
            name=f"eval-{model}",
            model=model,
            budget_usd=budget_usd,
            timeout_seconds=timeout_seconds,
            allowed_tools=DEFAULT_TOOLS,
            provider="openai",
            phase=PHASE_PREFLIGHT,
        )
    elif model.startswith("deepseek-"):
        return ModelProfile(
            name=f"eval-{model}",
            model=model,
            budget_usd=budget_usd,
            timeout_seconds=timeout_seconds,
            allowed_tools=DEFAULT_TOOLS,
            provider="deepseek",
            phase=PHASE_PREFLIGHT,
        )
    elif model.startswith("gemini-"):
        return ModelProfile(
            name=f"eval-{model}",
            model=model,
            budget_usd=budget_usd,
            timeout_seconds=timeout_seconds,
            allowed_tools=DEFAULT_TOOLS,
            provider="google",
            phase=PHASE_PREFLIGHT,
        )
    else:
        # Best-effort: treat as CLI with model name as cli identifier
        return ModelProfile(
            name=f"eval-{model}",
            model=model,
            budget_usd=budget_usd,
            timeout_seconds=timeout_seconds,
            allowed_tools=DEFAULT_TOOLS,
            cli=model,
            phase=PHASE_PREFLIGHT,
        )


def cmd_eval_preflight(args: object) -> int:
    """Run the preflight model evaluation harness."""
    from theforge.eval.golden_set import load_golden_set
    from theforge.eval.preflight_harness import run_harness
    from theforge.eval.preflight_report import generate_report, generate_report_json

    # ── Config ──────────────────────────────────────────────────────────────
    config_path_str = getattr(args, "config", None)
    if config_path_str:
        config_path = Path(config_path_str)
    else:
        config_path = _find_config()
    if config_path is None:
        print("[eval-preflight] No forge.yaml found", file=sys.stderr)
        return 1

    config = load_config_checked(
        config_path,
        loader=load_config,
        emit_startup_auth_warnings=False,
    )

    # ── Golden set ───────────────────────────────────────────────────────────
    golden_set_path_str = getattr(args, "golden_set", None)
    if golden_set_path_str:
        golden_set_path = Path(golden_set_path_str)
    else:
        golden_set_path = config_path.parent / "tests" / "eval" / "golden_stories.yaml"

    if not golden_set_path.exists():
        print(
            f"[eval-preflight] Golden set not found: {golden_set_path}",
            file=sys.stderr,
        )
        return 1

    import warnings

    with warnings.catch_warnings(record=True) as caught_warnings:
        warnings.simplefilter("always")
        golden = load_golden_set(golden_set_path)

    if caught_warnings:
        for w in caught_warnings:
            print(f"[eval-preflight] warning: {w.message}", file=sys.stderr)

    print(f"[eval-preflight] Loaded {len(golden)} golden stories from {golden_set_path}")

    # ── Model profiles ───────────────────────────────────────────────────────
    # Inherit budget and timeout from the existing preflight_profile as defaults.
    default_budget = config.preflight_profile.budget_usd
    default_timeout = config.preflight_profile.timeout_seconds

    models_arg = getattr(args, "models", None)
    if models_arg:
        model_names = [m.strip() for m in models_arg.split(",") if m.strip()]
    else:
        # Default candidate shortlist per spec AC: GPT-5.4, Claude Sonnet, DeepSeek V4 Pro
        model_names = ["gpt-5.4", "claude-sonnet-4-5", "deepseek-v4-pro"]

    profiles = [_infer_profile(m, default_budget, default_timeout) for m in model_names]
    print(f"[eval-preflight] Evaluating {len(profiles)} model(s): {', '.join(model_names)}")

    # ── Working directory ────────────────────────────────────────────────────
    working_dir_str = getattr(args, "working_dir", None)
    if working_dir_str:
        working_dir = Path(working_dir_str)
    else:
        working_dir = config.project_root

    # ── Run harness ──────────────────────────────────────────────────────────
    print(
        f"[eval-preflight] Running {len(golden) * len(profiles)} total evaluations "
        f"({len(golden)} stories × {len(profiles)} models)...\n"
    )
    all_metrics = run_harness(golden, profiles, working_dir, config)

    # ── Report ───────────────────────────────────────────────────────────────
    output_format = getattr(args, "output_format", "text")
    if output_format == "json":
        print(generate_report_json(all_metrics))
    else:
        print(generate_report(all_metrics))

    return 0


def _load_checked_config(args: object):
    config_path_str = getattr(args, "config", None)
    if config_path_str:
        config_path = Path(config_path_str)
    else:
        config_path = _find_config()
    if config_path is None:
        print("[forge] No forge.yaml found", file=sys.stderr)
        return None
    return load_config_checked(
        config_path,
        loader=load_config,
        emit_startup_auth_warnings=False,
    )


def _select_semantic_profile(config, requested_name: str | None):
    if requested_name is None:
        return config.preflight_profile
    matches = [
        profile
        for _role, profile in iter_config_profiles(config)
        if profile.name == requested_name
    ]
    if not matches:
        raise ValueError(
            f"semantic profile {requested_name!r} was not found in the configured profiles"
        )
    if len(matches) > 1:
        raise ValueError(
            f"semantic profile name {requested_name!r} is ambiguous; "
            "choose a unique configured profile name"
        )
    return matches[0]


def _render_semantic_record(record) -> str:
    lines = [
        f"status={record.outcome or 'EVALUATION_FAILED'}",
        f"input_digest={record.input_digest}",
        f"model_id={record.model_id}",
        f"prompt_contract_version={record.prompt_contract_version}",
        f"cache_hit={str(record.cache_hit).lower()}",
    ]
    if record.status == "evaluation_failed":
        lines.append(f"failure_detail={record.failure_detail or 'unknown failure'}")
        if record.raw_output_path:
            lines.append(f"raw_output_retained_at={record.raw_output_path}")
        elif record.raw_output:
            lines.append("raw_output_retained_in_record=true")
        return "\n".join(lines)
    if record.outcome == "NO_FINDINGS":
        return "\n".join(lines)
    for finding in record.findings:
        severity = f"[{finding.severity}] " if finding.severity else ""
        lines.append(f"- {severity}{finding.summary} ({finding.finding_digest})")
    return "\n".join(lines)


def cmd_review_semantic(args: object) -> int:
    """Run the audit-only semantic evaluator for one GitHub issue."""
    from theforge.eval.semantic_prompt import PROMPT_CONTRACT_VERSION
    from theforge.eval.semantic_runner import (
        SemanticBaselineRequiredError,
        review_issue_semantically,
    )

    config = _load_checked_config(args)
    if config is None:
        return 1

    try:
        profile = _select_semantic_profile(config, getattr(args, "profile", None))
        baseline_ids = tuple(getattr(args, "baseline_defect_ids", ()) or ())
        freeze_empty = bool(getattr(args, "freeze_empty_baseline", False))
        if not baseline_ids and freeze_empty:
            baseline_input: tuple[str, ...] | None = ()
        elif baseline_ids:
            baseline_input = baseline_ids
        else:
            baseline_input = None
        result = review_issue_semantically(
            issue_number=int(getattr(args, "issue_number")),
            project_root=config.project_root,
            secrets=config.secrets,
            profile=profile,
            prompt_contract_version=(
                getattr(args, "prompt_contract_version", None) or PROMPT_CONTRACT_VERSION
            ),
            baseline_defect_ids=baseline_input,
        )
    except SemanticBaselineRequiredError as exc:
        print(
            f"[review-semantic] {exc}. Freeze it with --freeze-empty-baseline or "
            f"--baseline-defect-id DEFECT_ID.",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(f"[review-semantic] {exc}", file=sys.stderr)
        return 1

    if result.baseline_created:
        defect_ids = ", ".join(result.baseline.defect_ids) or "(empty)"
        print(f"baseline_frozen_at={result.baseline.frozen_at} baseline_defect_ids={defect_ids}")
    print(_render_semantic_record(result.record))
    return 0


def _resolve_ratification_decisions(record, accepted: list[str], rejected: list[str]):
    """Validate operator accept/reject choices against one evaluation's findings.

    The operator decides every concern the evaluation raised, or the command
    refuses: a partial ratification would leave admission reading undecided
    model output, which is exactly what ADR-0009 clause 6 rules out.
    """
    from theforge.eval.semantic_storage import SemanticConcernDecision  # noqa: PLC0415
    from theforge.eval.semantic_types import (  # noqa: PLC0415
        DECISION_ACCEPTED,
        DECISION_REJECTED,
    )

    findings = set(record.finding_digests())
    accepted_set = set(accepted)
    rejected_set = set(rejected)

    both = accepted_set & rejected_set
    if both:
        raise ValueError(f"finding(s) both accepted and rejected: {sorted(both)}")
    unknown = (accepted_set | rejected_set) - findings
    if unknown:
        raise ValueError(f"finding digest(s) not raised by this evaluation: {sorted(unknown)}")
    undecided = findings - accepted_set - rejected_set
    if undecided:
        raise ValueError(
            f"every raised concern must be accepted or rejected; undecided: {sorted(undecided)}"
        )

    return tuple(
        SemanticConcernDecision(
            finding_digest=digest,
            decision=DECISION_ACCEPTED if digest in accepted_set else DECISION_REJECTED,
        )
        for digest in sorted(findings)
    )


def cmd_ratify_semantic(args: object) -> int:
    """Record an operator ratification of one evaluated document revision."""
    from theforge.eval.semantic_input import build_semantic_evaluation_input  # noqa: PLC0415
    from theforge.eval.semantic_readiness import (  # noqa: PLC0415
        derive_semantic_readiness,
    )
    from theforge.eval.semantic_runner import load_semantic_issue  # noqa: PLC0415
    from theforge.eval.semantic_storage import (  # noqa: PLC0415
        SemanticRatificationRecord,
        SemanticReviewStore,
        utc_now_iso,
    )

    config = _load_checked_config(args)
    if config is None:
        return 1

    store = SemanticReviewStore(config.project_root)
    try:
        issue = load_semantic_issue(
            issue_number=int(getattr(args, "issue_number")),
            project_root=config.project_root,
        )
        evaluation_input = build_semantic_evaluation_input(
            title=issue.title,
            body=issue.body,
            labels=issue.labels,
        )
        digest = getattr(args, "input_digest", None) or evaluation_input.input_digest
        record = store.latest_successful_record(issue_ref=issue.issue_ref, input_digest=digest)
        if record is None:
            raise ValueError(
                f"no successful semantic evaluation is recorded for {issue.issue_ref} "
                f"at revision {digest}; run `forge review-semantic` first"
            )
        accepted = list(getattr(args, "accept", ()) or ())
        rejected = list(getattr(args, "reject", ()) or ())
        if getattr(args, "reject_all", False):
            rejected = [found for found in record.finding_digests() if found not in set(accepted)]
        decisions = _resolve_ratification_decisions(record, accepted, rejected)
        ratification = SemanticRatificationRecord(
            issue_ref=issue.issue_ref,
            input_digest=digest,
            model_id=record.model_id,
            prompt_contract_version=record.prompt_contract_version,
            ratified_at=utc_now_iso(),
            decisions=decisions,
        )
        store.append_ratification(ratification)
    except Exception as exc:  # noqa: BLE001
        print(f"[ratify-semantic] {exc}", file=sys.stderr)
        return 1

    readiness = derive_semantic_readiness(
        issue_ref=issue.issue_ref,
        title=issue.title,
        body=issue.body,
        labels=issue.labels,
        store=store,
    )
    print(f"ratified_revision={digest}")
    print(f"ratified_at={ratification.ratified_at}")
    print(f"accepted={len(ratification.accepted_digests())} of {len(decisions)}")
    print(f"current_revision={readiness.input_digest}")
    print(f"semantic_requirement={readiness.requirement}")
    print(f"semantic_state={readiness.state}")
    print(f"detail={readiness.detail}")
    return 0


def cmd_semantic_report(args: object) -> int:
    """Report semantic-evaluation corpus metrics from recorded audits."""
    from theforge.eval.semantic_report import (
        build_semantic_corpus_report,
        load_semantic_corpus,
        render_semantic_corpus_report,
        render_semantic_corpus_report_json,
    )
    from theforge.eval.semantic_storage import SemanticReviewStore

    config = _load_checked_config(args)
    if config is None:
        return 1

    try:
        corpus = load_semantic_corpus(Path(getattr(args, "corpus")))
        report = build_semantic_corpus_report(
            corpus,
            store=SemanticReviewStore(config.project_root),
        )
    except Exception as exc:
        print(f"[semantic-report] {exc}", file=sys.stderr)
        return 1

    if getattr(args, "output_format", "text") == "json":
        print(render_semantic_corpus_report_json(report))
    else:
        print(render_semantic_corpus_report(report))
    return 0


def register_parser(subparsers: object) -> None:
    """Register the 'eval-preflight' subcommand parser."""
    p = subparsers.add_parser(
        "eval-preflight",
        help="Evaluate candidate preflight models against a golden story set",
    )
    p.add_argument(
        "--golden-set",
        dest="golden_set",
        metavar="PATH",
        help="Path to golden_stories.yaml (default: tests/eval/golden_stories.yaml)",
    )
    p.add_argument(
        "--models",
        metavar="MODEL[,MODEL...]",
        help=(
            "Comma-separated model identifiers to evaluate. "
            "Default: gpt-5.4,claude-sonnet-4-5,deepseek-v4-pro"
        ),
    )
    p.add_argument(
        "--working-dir",
        dest="working_dir",
        metavar="PATH",
        help="Working directory for agent invocations (default: project_root from forge.yaml)",
    )
    p.add_argument(
        "--output-format",
        dest="output_format",
        choices=["text", "json"],
        default="text",
        help="Report format: 'text' (markdown table) or 'json' (default: text)",
    )
    p.add_argument(
        "--config",
        metavar="PATH",
        help="Path to forge.yaml (default: auto-detect)",
    )

    review = subparsers.add_parser(
        "review-semantic",
        help="Run the audit-only semantic evaluator for one GitHub issue",
    )
    review.add_argument("issue_number", metavar="ISSUE")
    review.add_argument(
        "--profile",
        metavar="NAME",
        help="Configured ModelProfile name to use (default: preflight profile)",
    )
    review.add_argument(
        "--prompt-contract-version",
        dest="prompt_contract_version",
        metavar="VERSION",
        help="Prompt contract version override (default: semantic-review.v1)",
    )
    review.add_argument(
        "--baseline-defect-id",
        dest="baseline_defect_ids",
        action="append",
        default=[],
        metavar="DEFECT_ID",
        help="Freeze the human baseline with this defect id (repeatable)",
    )
    review.add_argument(
        "--freeze-empty-baseline",
        action="store_true",
        help="Freeze an empty human baseline before revealing evaluator output",
    )
    review.add_argument(
        "--config",
        metavar="PATH",
        help="Path to forge.yaml (default: auto-detect)",
    )

    ratify = subparsers.add_parser(
        "ratify-semantic",
        help="Record an operator ratification of an evaluated document revision",
    )
    ratify.add_argument("issue_number", metavar="ISSUE")
    ratify.add_argument(
        "--accept",
        action="append",
        default=[],
        metavar="FINDING_DIGEST",
        help="Accept this raised concern; it withholds readiness for this revision",
    )
    ratify.add_argument(
        "--reject",
        action="append",
        default=[],
        metavar="FINDING_DIGEST",
        help="Reject this raised concern; it passes without further challenge",
    )
    ratify.add_argument(
        "--reject-all",
        dest="reject_all",
        action="store_true",
        help="Reject every concern not explicitly accepted",
    )
    ratify.add_argument(
        "--input-digest",
        dest="input_digest",
        metavar="DIGEST",
        help="Ratify this revision instead of the issue's current one",
    )
    ratify.add_argument(
        "--config",
        metavar="PATH",
        help="Path to forge.yaml (default: auto-detect)",
    )

    report = subparsers.add_parser(
        "semantic-report",
        help="Report corpus metrics from semantic-review audit records",
    )
    report.add_argument("corpus", metavar="PATH", help="Path to a semantic corpus YAML file")
    report.add_argument(
        "--output-format",
        dest="output_format",
        choices=["text", "json"],
        default="text",
        help="Report format: 'text' or 'json' (default: text)",
    )
    report.add_argument(
        "--config",
        metavar="PATH",
        help="Path to forge.yaml (default: auto-detect)",
    )
