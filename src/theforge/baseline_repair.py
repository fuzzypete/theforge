"""Read reproduced sprint baseline failures and render a repair work item."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_EXCERPT_CHARS = 2000
_PER_RUN_AUDIT_RE = re.compile(r"^run-(?P<run_id>.+)-sprint-audit\.yaml$")
_NON_OUTPUT_HEADERS = ("# ",)
_EXCERPT_ANCHORS = ("FAILED ", "AssertionError", "Traceback", "ERROR ", "FAIL ")


class BaselineRepairError(ValueError):
    """Raised when a sprint audit cannot be routed into a baseline repair."""


@dataclass(frozen=True)
class BaselineRepairEvidence:
    """Structured evidence captured from a reproduced broken-baseline audit."""

    audit_path: Path
    sprint_name: str | None
    sprint_run_id: str | None
    merge_base: str
    gate_command: str | None
    validation_profile: str | None
    validation_authority: str | None
    output_tail: str
    worktree: Path
    evidence_path: Path | None
    evidence_text: str | None
    evidence_unavailable: str | None
    failing_targets: tuple[str, ...]
    failing_target_source: str | None
    failing_target_format_recognized: bool | None

    @property
    def merge_base_short(self) -> str:
        return self.merge_base[:12]


def load_baseline_repair_evidence(audit_path: Path) -> BaselineRepairEvidence:
    """Load a reproduced broken-baseline record from ``audit_path``."""
    path = audit_path.resolve()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BaselineRepairError(f"could not read sprint audit {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise BaselineRepairError(f"sprint audit {path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise BaselineRepairError(f"sprint audit {path} is not a YAML mapping")

    sprint = raw.get("sprint")
    if not isinstance(sprint, dict):
        raise BaselineRepairError(f"sprint audit {path} has no sprint block")
    stopped_reason = _as_str(sprint.get("stopped_reason"))
    if stopped_reason != "broken_baseline":
        raise BaselineRepairError(
            f"sprint audit {path} stopped_reason is {stopped_reason!r}, not 'broken_baseline'"
        )

    baseline = raw.get("baseline_check")
    if not isinstance(baseline, dict):
        raise BaselineRepairError(f"sprint audit {path} has no baseline_check block")
    if baseline.get("passed") is not False:
        raise BaselineRepairError(
            "sprint audit "
            f"{path} baseline_check.passed is not false; no broken baseline was recorded"
        )

    failure_reproduced = baseline.get("failure_reproduced")
    if failure_reproduced is not True:
        if failure_reproduced is None:
            detail = "baseline_check.failure_reproduced is missing"
        else:
            detail = f"baseline_check.failure_reproduced is {failure_reproduced!r}"
        raise BaselineRepairError(
            f"sprint audit {path} does not record a reproduced baseline gate failure: {detail}"
        )

    merge_base = _as_str(baseline.get("merge_base"))
    if not merge_base:
        raise BaselineRepairError(f"sprint audit {path} recorded no baseline merge_base")

    worktree_raw = _as_str(baseline.get("worktree"))
    if not worktree_raw:
        raise BaselineRepairError(
            "sprint audit "
            f"{path} recorded no preserved baseline worktree for the reproduced failure"
        )
    worktree = Path(worktree_raw)
    if not worktree.is_dir():
        raise BaselineRepairError(f"preserved baseline worktree no longer exists: {worktree}")

    evidence_path_str = _as_str(baseline.get("evidence_path"))
    evidence_path = Path(evidence_path_str) if evidence_path_str else None
    evidence_text: str | None = None
    evidence_unavailable = _as_str(baseline.get("evidence_unavailable"))
    if evidence_path is not None:
        if evidence_path.is_file():
            try:
                evidence_text = evidence_path.read_text(encoding="utf-8")
            except OSError as exc:
                evidence_unavailable = (
                    evidence_unavailable
                    or f"could not read recorded evidence file {evidence_path}: {exc}"
                )
        else:
            evidence_unavailable = (
                evidence_unavailable or f"recorded evidence file no longer exists: {evidence_path}"
            )

    extraction = baseline.get("failing_target_extraction")
    extraction_source: str | None = None
    extraction_recognized: bool | None = None
    if isinstance(extraction, dict):
        extraction_source = _as_str(extraction.get("source"))
        extraction_recognized = _as_bool(extraction.get("format_recognized"))

    return BaselineRepairEvidence(
        audit_path=path,
        sprint_name=_as_str(sprint.get("name")),
        sprint_run_id=_audit_run_id(path),
        merge_base=merge_base,
        gate_command=_as_str(baseline.get("command")),
        validation_profile=_as_str(baseline.get("validation_profile")),
        validation_authority=_as_str(baseline.get("validation_authority")),
        output_tail=_as_str(baseline.get("output_tail")) or "",
        worktree=worktree,
        evidence_path=evidence_path,
        evidence_text=evidence_text,
        evidence_unavailable=evidence_unavailable,
        failing_targets=tuple(_as_str_list(baseline.get("failing_targets"))),
        failing_target_source=extraction_source,
        failing_target_format_recognized=extraction_recognized,
    )


def render_issue_title(evidence: BaselineRepairEvidence) -> str:
    """Return the GitHub issue title for a baseline repair task."""
    if evidence.failing_targets:
        first = evidence.failing_targets[0]
        if len(evidence.failing_targets) == 1:
            return f"Fix reproduced baseline gate failure in {first}"
        remaining = len(evidence.failing_targets) - 1
        return f"Fix reproduced baseline gate failure in {first} (+{remaining} more)"
    return f"Fix reproduced baseline gate failure on merge base {evidence.merge_base_short}"


def render_issue_body(
    evidence: BaselineRepairEvidence,
    *,
    excerpt_chars: int = DEFAULT_EXCERPT_CHARS,
) -> str:
    """Render a bug-shaped issue body from captured baseline evidence."""
    command = evidence.gate_command or "(gate command not recorded)"
    profile = evidence.validation_profile or "(validation profile not recorded)"
    authority = evidence.validation_authority or "(validation authority not recorded)"
    output_location = _render_output_location(evidence)
    failing_targets = _render_failing_targets(evidence)
    excerpt = _render_excerpt(evidence, excerpt_chars=excerpt_chars)
    sprint_name = evidence.sprint_name or "(sprint name not recorded)"
    sprint_run_id = evidence.sprint_run_id or "(sprint run id not recorded)"
    extraction_detail = _render_extraction_detail(evidence)
    target_lines = (
        [f"  - `{target}`" for target in evidence.failing_targets]
        if evidence.failing_targets
        else ["  - `(none extracted)`"]
    )

    return "\n".join(
        [
            "## Observed",
            "",
            (
                "A sprint aborted before any story ran because the configured baseline gate "
                f"failed again on merge base `{evidence.merge_base}`."
            ),
            "",
            "## Expected",
            "",
            (
                "The sprint merge base should pass the configured baseline gate "
                "before ordinary sprint work starts."
            ),
            "",
            "## Diagnosis",
            "",
            (
                "- **Observed symptom:** sprint audit "
                f"`{evidence.audit_path}` recorded a reproduced baseline-gate failure before any "
                "dev "
                f"work started on merge base `{evidence.merge_base}`."
            ),
            (
                "- **Evidence:** validation profile "
                f"`{profile}` ({authority}) ran `{command}`; full gate output {output_location}; "
                f"preserved reproduction worktree `{evidence.worktree}`; extracted failing "
                "targets: "
                f"{failing_targets}."
            ),
            (
                "- **Confirmed cause:** not yet identified. The reproduced gate output and "
                "preserved worktree are attached so the first dev cycle can confirm the cause "
                "in the exact failing checkout."
            ),
            (
                "- **Affected code path:** the configured baseline gate "
                f"`{command}` as reproduced at merge base `{evidence.merge_base}` in "
                f"`{evidence.worktree}`."
            ),
            (
                "- **Fix-success criterion:** the preserved worktree passes the failing target(s) "
                "named below under the configured baseline gate, and a fresh sprint entry "
                "no longer aborts on this broken baseline."
            ),
            "",
            "## Reproduction details",
            "",
            f"- Sprint audit: `{evidence.audit_path}`",
            f"- Sprint: `{sprint_name}`",
            f"- Sprint run id: `{sprint_run_id}`",
            f"- Validation profile: `{profile}`",
            f"- Validation authority: `{authority}`",
            f"- Gate command: `{command}`",
            f"- Merge base: `{evidence.merge_base}`",
            f"- Full gate output: {output_location}",
            f"- Preserved worktree: `{evidence.worktree}`",
            f"- Failing-target extraction: {extraction_detail}",
            "- Extracted failing targets:",
            *target_lines,
            "",
            "### Gate output excerpt",
            "",
            "```text",
            excerpt,
            "```",
        ]
    )


def _audit_run_id(path: Path) -> str | None:
    match = _PER_RUN_AUDIT_RE.match(path.name)
    if match is None:
        return None
    return match.group("run_id")


def _as_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _as_str(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _as_str_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        rendered = _as_str(item)
        if rendered and rendered not in out:
            out.append(rendered)
    return out


def _render_output_location(evidence: BaselineRepairEvidence) -> str:
    if evidence.evidence_path is not None and evidence.evidence_text is not None:
        return f"`{evidence.evidence_path}`"
    if evidence.evidence_path is not None and evidence.evidence_unavailable:
        return f"`{evidence.evidence_path}` (unavailable: {evidence.evidence_unavailable})"
    if evidence.evidence_path is not None:
        return f"`{evidence.evidence_path}`"
    if evidence.evidence_unavailable:
        return evidence.evidence_unavailable
    return "(not recorded)"


def _render_failing_targets(evidence: BaselineRepairEvidence) -> str:
    if evidence.failing_targets:
        return ", ".join(f"`{target}`" for target in evidence.failing_targets)
    return "none extracted"


def _render_extraction_detail(evidence: BaselineRepairEvidence) -> str:
    source = evidence.failing_target_source or "not recorded"
    recognized = evidence.failing_target_format_recognized
    if recognized is None:
        recognized_text = "not recorded"
    else:
        recognized_text = "yes" if recognized else "no"
    return f"source={source}, format_recognized={recognized_text}"


def _render_excerpt(evidence: BaselineRepairEvidence, *, excerpt_chars: int) -> str:
    raw = _evidence_body(evidence.evidence_text) or evidence.output_tail or ""
    text = raw.strip()
    if not text:
        return "(no gate output was captured)"
    if len(text) <= excerpt_chars:
        return text
    anchor = _find_excerpt_anchor(text, evidence.failing_targets)
    if anchor is not None:
        start = max(anchor - (excerpt_chars // 3), 0)
        end = min(start + excerpt_chars, len(text))
        start = max(end - excerpt_chars, 0)
        excerpt = text[start:end]
        if start > 0:
            excerpt = "...\n" + excerpt
        if end < len(text):
            excerpt = excerpt + "\n..."
        return excerpt
    head = excerpt_chars // 2
    tail = excerpt_chars - head - len("\n...\n")
    return text[:head].rstrip() + "\n...\n" + text[-tail:].lstrip()


def _evidence_body(text: str | None) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    body_start = 0
    while body_start < len(lines) and lines[body_start].startswith(_NON_OUTPUT_HEADERS):
        body_start += 1
    if body_start < len(lines) and not lines[body_start].strip():
        body_start += 1
    return "\n".join(lines[body_start:])


def _find_excerpt_anchor(text: str, failing_targets: tuple[str, ...]) -> int | None:
    for target in failing_targets:
        idx = text.find(target)
        if idx >= 0:
            return idx
    for token in _EXCERPT_ANCHORS:
        idx = text.find(token)
        if idx >= 0:
            return idx
    return None
