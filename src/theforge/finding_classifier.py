"""Deterministic finding classification for multi-cycle review convergence.

All logic is pure Python — no LLM calls.

Fingerprinting uses sha256(severity + "|" + (file or "") + "|" + normalized_tokens).
Matching uses file equality + Jaccard token overlap ≥ JACCARD_THRESHOLD (0.5).

# CALIBRATION NOTE: The Jaccard threshold of 0.5 was chosen as a reasonable default.
# It may need tuning depending on how verbose reviewer descriptions tend to be.
# A lower threshold (e.g. 0.3) catches more near-matches but risks false positives.
# A higher threshold (e.g. 0.7) is more conservative but may miss paraphrased duplicates.
JACCARD_THRESHOLD = 0.5

# "Near changed code" is interpreted as same-file only (not line-level proximity).
# This keeps the classification deterministic without parsing diff hunks.
# If line-level proximity is required in future, parse git diff unified output.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .coordinator.state import CoordinatorState, FindingRecord
    from .review import ReviewFinding, ReviewResult

JACCARD_THRESHOLD = 0.5


def _normalize_tokens(text: str) -> frozenset[str]:
    """Lowercase, strip punctuation, split into tokens."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return frozenset(t for t in text.split() if len(t) > 2)


def _fingerprint(
    severity: str, file: str | None, description: str, line: int | None = None
) -> str:
    """Stable fingerprint: sha256 prefix of severity + file + line + normalized description tokens.

    Line is included so that one reviewer reporting the same description at distant lines
    produces separate buckets.  Reports with line=None share a bucket (line omitted from key).
    """
    tokens = sorted(_normalize_tokens(description))
    line_part = str(line) if line is not None else ""
    raw = f"{severity}|{file or ''}|{line_part}|{' '.join(tokens)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _matches_prior(
    finding: ReviewFinding,
    prior: FindingRecord,
    threshold: float = JACCARD_THRESHOLD,
) -> bool:
    """Return True if finding matches prior record (same file + same severity + token overlap)."""
    if finding.file != prior.file:
        return False
    if finding.severity != prior.severity:
        return False
    tokens_new = _normalize_tokens(finding.description)
    tokens_prior = _normalize_tokens(prior.description)
    return _jaccard(tokens_new, tokens_prior) >= threshold


def _matches_prior_agnostic(
    finding: ReviewFinding,
    prior: FindingRecord,
    threshold: float = JACCARD_THRESHOLD,
) -> bool:
    """Return True if finding matches prior record ignoring severity (same file + token overlap).

    Used to detect severity downgrades between cycles (e.g. P1 → P2).
    """
    if finding.file != prior.file:
        return False
    tokens_new = _normalize_tokens(finding.description)
    tokens_prior = _normalize_tokens(prior.description)
    return _jaccard(tokens_new, tokens_prior) >= threshold


def _get_changed_files(workspace_path: Path, prev_commit: str | None) -> frozenset[str]:
    """Return set of file paths changed since prev_commit via git diff --name-only.

    Returns empty set on error, missing workspace, or no prev_commit.
    Interprets 'near changed code' as same-file only (not line-level proximity).
    """
    if not prev_commit or not workspace_path or not workspace_path.exists():
        return frozenset()
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{prev_commit}..HEAD"],
            cwd=str(workspace_path),
            capture_output=True,
            timeout=15,
        )
        if result.returncode != 0:
            return frozenset()
        lines = result.stdout.decode("utf-8", errors="replace").splitlines()
        return frozenset(line.strip() for line in lines if line.strip())
    except (subprocess.TimeoutExpired, OSError):
        return frozenset()


def update_finding_registry(
    state: CoordinatorState,
    cycle_results: list[tuple[str, ReviewResult]],
    workspace_path: Path,
    cycle_num: int,
    prev_commit: str | None = None,
) -> list[FindingRecord]:
    """Classify findings from this review cycle and update state.finding_registry.

    Called after every review cycle (including cycle 1).
    Cycle 1: all findings recorded as net_new (no prior registry to compare against).
    Cycle 2+: compare against prior registry, correlate with changed files.

    Args:
        state: Mutable coordinator state (finding_registry mutated in-place).
        cycle_results: List of (reviewer_profile_name, ReviewResult) for this cycle.
        workspace_path: Path to the worktree (for git diff).
        cycle_num: Current review cycle number (1-indexed).
        prev_commit: Commit hash of the start of the latest dev iteration (for git diff).
                     Pass None to skip changed-file correlation.

    Returns:
        List of FindingRecord objects classified this cycle (all severities).
        Callers use this list for disposition-gated exit criteria.
    """
    from .coordinator.state import FindingRecord  # avoid circular at module level

    changed_files = _get_changed_files(workspace_path, prev_commit)

    # Gather all P1 findings per reviewer (for corroboration detection)
    # Structure: {fingerprint -> list of (reviewer_name, finding)}
    fingerprint_to_reports: dict[str, list[tuple[str, ReviewFinding]]] = defaultdict(list)

    all_findings: list[tuple[str, ReviewFinding]] = []  # (reviewer, finding)
    for reviewer_name, review_result in cycle_results:
        for finding in review_result.findings:
            all_findings.append((reviewer_name, finding))
            fp = _fingerprint(finding.severity, finding.file, finding.description, finding.line)
            fingerprint_to_reports[fp].append((reviewer_name, finding))

    def _should_merge(
        reports_a: list[tuple[str, ReviewFinding]],
        reports_b: list[tuple[str, ReviewFinding]],
    ) -> bool:
        comparable_a = [
            finding
            for _, finding in reports_a
            if finding.file is not None and finding.line is not None
        ]
        comparable_b = [
            finding
            for _, finding in reports_b
            if finding.file is not None and finding.line is not None
        ]
        if not comparable_a or not comparable_b:
            return False
        for finding_a in comparable_a:
            for finding_b in comparable_b:
                if finding_a.severity != finding_b.severity:
                    return False
                if finding_a.file != finding_b.file:
                    return False
                if abs(finding_a.line - finding_b.line) > 3:
                    return False
        return True

    merged_reports: dict[str, list[tuple[str, ReviewFinding]]] = {
        fp: list(reports) for fp, reports in fingerprint_to_reports.items()
    }
    fingerprint_keys = list(merged_reports)
    for idx, fp_a in enumerate(fingerprint_keys):
        if fp_a not in merged_reports:
            continue
        for fp_b in fingerprint_keys[idx + 1 :]:
            if fp_b not in merged_reports:
                continue
            if not _should_merge(merged_reports[fp_a], merged_reports[fp_b]):
                continue
            if len({reviewer for reviewer, _ in merged_reports[fp_a]}) < len(
                {reviewer for reviewer, _ in merged_reports[fp_b]}
            ):
                fp_a, fp_b = fp_b, fp_a
            merged_reports[fp_a].extend(merged_reports.pop(fp_b))

    classified_this_cycle: list[FindingRecord] = []

    # Snapshot the registry before this cycle's processing so that records inserted
    # during the loop (for new findings) don't falsely match later buckets from the
    # same cycle (e.g. same reviewer reporting the same description at distant lines).
    prior_registry = list(state.finding_registry)

    for fp, reports in merged_reports.items():
        # Use the first report as representative
        first_reviewer, first_finding = reports[0]

        # Look for match in prior registry (snapshot taken before this cycle's inserts).
        # First pass: exact match (same severity).
        # Second pass: severity-agnostic match to detect P1 → P2 downgrades.
        prior_match: FindingRecord | None = None
        is_severity_downgrade = False
        for record in prior_registry:
            if _matches_prior(first_finding, record):
                prior_match = record
                break
        if prior_match is None:
            for record in prior_registry:
                if _matches_prior_agnostic(first_finding, record):
                    # Only treat as downgrade when severity decreased (P1 → P2)
                    if record.severity == "P1" and first_finding.severity == "P2":
                        prior_match = record
                        is_severity_downgrade = True
                    break

        in_changed_files = first_finding.file in changed_files if first_finding.file else False
        num_reporters = len({r for r, _ in reports})  # unique reviewer count

        if prior_match is not None:
            # Finding existed in a prior cycle
            disposition: str
            if is_severity_downgrade:
                # P1 finding was reported as P2 this cycle — record the downgrade
                disposition = "downgraded"
            elif prior_match.disposition in (
                "unresolved",
                "net_new",
                "corroborated_new",
                "regression",
                "ac_blocking",
            ):
                disposition = "unresolved"
            elif prior_match.disposition == "fixed":
                # Was marked fixed but reappeared — treat as regression
                disposition = "regression"
            else:
                disposition = "unresolved"

            # Update existing record
            prior_match.cycle_last_seen = cycle_num
            prior_match.disposition = disposition  # type: ignore[assignment]
            classified_this_cycle.append(prior_match)
        else:
            # New finding this cycle
            if in_changed_files:
                disposition = "regression"
            elif num_reporters >= 2:
                disposition = "corroborated_new"
            else:
                disposition = "net_new"

            record = FindingRecord(
                finding_id=fp,
                cycle_first_seen=cycle_num,
                cycle_last_seen=cycle_num,
                file=first_finding.file,
                line=first_finding.line,
                severity=first_finding.severity,
                description=first_finding.description,
                reporter=first_reviewer,
                disposition=disposition,  # type: ignore[arg-type]
            )
            state.finding_registry.append(record)
            classified_this_cycle.append(record)

    # Mark any prior P1 findings NOT seen this cycle as fixed
    for record in state.finding_registry:
        if record.cycle_last_seen < cycle_num and record.severity == "P1":
            if record.disposition in (
                "unresolved",
                "net_new",
                "corroborated_new",
                "regression",
                "ac_blocking",
            ):
                record.disposition = "fixed"  # type: ignore[assignment]

    return classified_this_cycle


def has_blocking_p1(classified: list[FindingRecord]) -> bool:
    """Return True if any P1 finding has a blocking disposition.

    Blocking dispositions: unresolved, regression, corroborated_new, ac_blocking.
    Non-blocking: net_new (single reviewer, latent, not in changed files).
    """
    blocking = {"unresolved", "regression", "corroborated_new", "ac_blocking"}
    return any(r.severity == "P1" and r.disposition in blocking for r in classified)


def net_new_p1s(classified: list[FindingRecord]) -> list[FindingRecord]:
    """Return P1 findings with net_new disposition (non-blocking, for audit trail)."""
    return [r for r in classified if r.severity == "P1" and r.disposition == "net_new"]
