"""The one boundary every issue-body producer writes through.

A component that files or edits a GitHub issue body states, up front, the
lifecycle state it intends the result to occupy, hands the rendered body to
:func:`validate_issue_body`, and mutates nothing until the shared evaluation
agrees. A producer that cannot render a conforming body reports what it could
not satisfy instead of filing an object the gate will refuse.

Two declaration modes, because producers come in two shapes:

``strict`` (authoring producers)
    The producer wrote the whole body, so it owns every finding in it. The
    evaluated verdict must *equal* the declared state. ``forge report``,
    ``forge todo`` capture, the advisory-debt filer and the post-run finding
    hook are authoring producers.

non-strict (editing producers)
    The producer added a section to a body somebody else owns. It still
    declares the state it intends, but it is not answerable for refusals that
    were already there: the edit conforms when the evaluated verdict matches
    the declaration, **or** when the edit introduced no new refusal code and
    did not turn an admissible body inadmissible. ``forge shape``,
    ``forge groom``, ``forge diagnose``, ``forge todo triage`` and sprint
    intake remediation are editing producers.

Declaring nothing (``declared=None``) is available to editing producers whose
honest claim is only "I do not change the lifecycle state" — the no-new-refusal
and no-regression rules still apply, and a previous body is then required.

Stdlib-only, like the rest of :mod:`theforge.shape_check`, so a shell hook in a
repository that has none of TheForge's runtime dependencies can validate through
this same contract by running the module directly:

    python -m theforge.shape_check.producer \\
        --producer post-run-hook-finding \\
        --declared needs_operator_action \\
        --title "$title" --body-file "$body_file" \\
        --label bug --label forge-finding --label needs-triage

Exit codes: ``0`` conforming, ``2`` refused (declaration unsatisfied), ``1``
usage error. Every nonzero exit is a refusal to file — callers must fail closed.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from theforge.shape_check.check import check
from theforge.shape_check.types import Reason, ShapeVerdict
from theforge.shape_check.verdict import refusal_codes

#: Every component allowed to create or edit an issue body, mapped to a
#: one-line description of what it writes. Registration is deliberately
#: closed: a producer that is not listed here cannot validate, and a producer
#: that cannot validate must not mutate. Adding a body-writing surface means
#: adding it here and declaring what state it intends.
PRODUCERS: dict[str, str] = {
    "forge-shape": "forge shape --apply body restructure",
    "forge-groom": "forge groom readiness repair (issue body or local story file)",
    "forge-diagnose": "forge diagnose Diagnosis section landed into the issue body",
    "forge-report-create": "forge report initial bug filing in the target repository",
    "forge-report-update": "forge report publication-state body update",
    "forge-todo-create": "forge todo draft capture",
    "forge-todo-triage": "forge todo triage interactive body edit",
    "forge-advisory-finding": "advisory convention debt issue filed from a run",
    "forge-intake-autofix": "sprint intake auto-fix body rewrite (edit mode)",
    "forge-intake-reopen-context": "sprint intake shape-gate-skip reopen-context fold-in",
    "post-run-hook-finding": "post_run hook GitHub issue for a review finding",
}


def label_names(fetched: object) -> list[str]:
    """Normalize a ``gh issue view --json labels`` payload to label strings.

    Tolerates both shapes ``gh`` and TheForge's own fetch seams return — a list
    of ``{"name": ...}`` objects, or a list of plain strings — so no producer
    has to grow its own copy of this before it can validate.
    """
    names: list[str] = []
    if isinstance(fetched, list):
        for entry in fetched:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                names.append(entry["name"])
            elif isinstance(entry, str):
                names.append(entry)
    return names


class ProducerValidationError(Exception):
    """Raised when a producer's rendered body does not satisfy its declaration.

    Carries the :class:`ProducerValidation` so callers can render the same
    operator-facing report they would print on a soft refusal.
    """

    def __init__(self, validation: ProducerValidation) -> None:
        super().__init__(validation.report())
        self.validation = validation


@dataclass(frozen=True)
class ProducerValidation:
    """The answer to "may this producer write this body?"."""

    producer: str
    declared: tuple[ShapeVerdict, ...]
    actual: ShapeVerdict
    reasons: tuple[Reason, ...]
    #: Refusal codes present in the rendered body that the previous body did
    #: not have. Empty when no previous body was supplied.
    new_refusal_codes: tuple[str, ...]
    #: True when the previous body was admissible and this one is not.
    regressed_from_runnable: bool
    strict: bool
    conforms: bool

    @property
    def declared_display(self) -> str:
        if not self.declared:
            return "unchanged lifecycle state (no new refusal)"
        return " | ".join(v.value for v in self.declared)

    def report(self) -> str:
        """Operator-facing text naming exactly what could not be satisfied."""
        lines = [
            f"{self.producer}: refusing to write this issue body — "
            "it does not occupy the lifecycle state this producer declared.",
            f"  declared : {self.declared_display}",
            f"  evaluated: {self.actual.value}",
        ]
        if self.regressed_from_runnable:
            lines.append(
                "  regression: the body was admissible before this edit and would not be after."
            )
        if self.new_refusal_codes:
            lines.append(f"  introduced: {', '.join(self.new_refusal_codes)}")
        if self.reasons:
            lines.append("  findings:")
            for reason in self.reasons:
                detail = f" — {reason.detail}" if reason.detail else ""
                lines.append(f"    - [{reason.severity.value}] {reason.code}{detail}")
        return "\n".join(lines)


def _normalize_declared(
    declared: ShapeVerdict | Iterable[ShapeVerdict] | None,
) -> tuple[ShapeVerdict, ...]:
    if declared is None:
        return ()
    if isinstance(declared, ShapeVerdict):
        return (declared,)
    normalized = tuple(dict.fromkeys(declared))
    if not normalized:
        raise ValueError("declared must name at least one ShapeVerdict, or be None")
    return normalized


def compare_declaration(
    *,
    producer: str,
    declared: ShapeVerdict | Iterable[ShapeVerdict] | None,
    actual: ShapeVerdict,
    reasons: Iterable[Reason] = (),
    previous_verdict: ShapeVerdict | None = None,
    previous_reasons: Iterable[Reason] | None = None,
    strict: bool = False,
) -> ProducerValidation:
    """Compare an already-evaluated verdict against a producer's declaration.

    Separate from :func:`validate_issue_body` because one producer —
    ``forge report`` — is deliberately evaluated by the *target repository's*
    gate revision rather than this checkout's, and must still be judged against
    its declaration through the same rules.
    """
    if producer not in PRODUCERS:
        raise ValueError(
            f"unknown issue-body producer {producer!r}; "
            f"register it in shape_check.producer.PRODUCERS "
            f"(known: {', '.join(sorted(PRODUCERS))})"
        )
    declared_t = _normalize_declared(declared)
    reasons_t = tuple(reasons)
    if declared_t == () and previous_verdict is None:
        raise ValueError(
            f"{producer}: declaring no lifecycle state requires a previous body to "
            "compare against — otherwise nothing is being claimed at all"
        )

    new_codes: tuple[str, ...] = ()
    regressed = False
    if previous_verdict is not None:
        before = refusal_codes(tuple(previous_reasons or ()))
        after = refusal_codes(reasons_t)
        new_codes = tuple(sorted(after - before))
        regressed = (
            previous_verdict is ShapeVerdict.RUNNABLE and actual is not ShapeVerdict.RUNNABLE
        )

    matches_declaration = actual in declared_t
    if strict:
        conforms = matches_declaration
    else:
        conforms = matches_declaration or (
            previous_verdict is not None and not new_codes and not regressed
        )

    return ProducerValidation(
        producer=producer,
        declared=declared_t,
        actual=actual,
        reasons=reasons_t,
        new_refusal_codes=new_codes,
        regressed_from_runnable=regressed,
        strict=strict,
        conforms=conforms,
    )


def validate_issue_body(
    *,
    producer: str,
    title: str,
    body: str,
    labels: Iterable[str],
    declared: ShapeVerdict | Iterable[ShapeVerdict] | None,
    previous_body: str | None = None,
    strict: bool = False,
) -> ProducerValidation:
    """Evaluate ``body`` through the shared gate and judge it against ``declared``.

    ``previous_body`` is the body this write would replace. Supplying it turns
    on the two rules an edit is answerable for: it may not introduce a refusal
    code the body did not already carry, and it may not turn an admissible body
    inadmissible.
    """
    labels = list(labels)
    result = check(title or "", body or "", labels)
    previous_verdict: ShapeVerdict | None = None
    previous_reasons: tuple[Reason, ...] = ()
    if previous_body is not None:
        previous = check(title or "", previous_body, labels)
        previous_verdict = previous.verdict
        previous_reasons = previous.reasons
    return compare_declaration(
        producer=producer,
        declared=declared,
        actual=result.verdict,
        reasons=result.reasons,
        previous_verdict=previous_verdict,
        previous_reasons=previous_reasons,
        strict=strict,
    )


def require_conforming_body(
    *,
    producer: str,
    title: str,
    body: str,
    labels: Iterable[str],
    declared: ShapeVerdict | Iterable[ShapeVerdict] | None,
    previous_body: str | None = None,
    strict: bool = False,
) -> ProducerValidation:
    """:func:`validate_issue_body`, raising :class:`ProducerValidationError`
    when the declaration is unsatisfied. For call sites whose mutation seam is
    reached by falling through rather than by an explicit branch."""
    validation = validate_issue_body(
        producer=producer,
        title=title,
        body=body,
        labels=labels,
        declared=declared,
        previous_body=previous_body,
        strict=strict,
    )
    if not validation.conforms:
        raise ProducerValidationError(validation)
    return validation


# ── CLI ───────────────────────────────────────────────────────────────
# Shell hooks validate through this rather than reimplementing the contract.


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m theforge.shape_check.producer",
        description="Validate a rendered issue body against its producer's declared state.",
    )
    parser.add_argument("--producer", required=True, help="Registered producer id")
    parser.add_argument(
        "--declared",
        action="append",
        default=[],
        help="ShapeVerdict the producer declares (repeatable)",
    )
    parser.add_argument("--title", default="", help="Issue title")
    parser.add_argument("--body-file", dest="body_file", help="Path to the rendered body")
    parser.add_argument(
        "--body-stdin",
        dest="body_stdin",
        action="store_true",
        default=False,
        help="Read the rendered body from stdin",
    )
    parser.add_argument("--label", action="append", default=[], help="Issue label (repeatable)")
    parser.add_argument(
        "--previous-body-file",
        dest="previous_body_file",
        help="Path to the body this write would replace",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        default=False,
        help="Require the evaluated verdict to equal the declaration exactly",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if bool(args.body_file) == bool(args.body_stdin):
        print("exactly one of --body-file or --body-stdin is required", file=sys.stderr)
        return 1
    try:
        body = (
            sys.stdin.read()
            if args.body_stdin
            else Path(args.body_file).read_text(encoding="utf-8")
        )
        previous_body = (
            Path(args.previous_body_file).read_text(encoding="utf-8")
            if args.previous_body_file
            else None
        )
    except OSError as exc:
        print(f"could not read body: {exc}", file=sys.stderr)
        return 1

    try:
        declared = [ShapeVerdict(value) for value in args.declared] or None
    except ValueError as exc:
        print(f"--declared: {exc}", file=sys.stderr)
        return 1

    try:
        validation = validate_issue_body(
            producer=args.producer,
            title=args.title,
            body=body,
            labels=args.label,
            declared=declared,
            previous_body=previous_body,
            strict=args.strict,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if validation.conforms:
        return 0
    print(validation.report(), file=sys.stderr)
    return 2


__all__ = [
    "PRODUCERS",
    "ProducerValidation",
    "ProducerValidationError",
    "compare_declaration",
    "label_names",
    "main",
    "require_conforming_body",
    "validate_issue_body",
]


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    sys.exit(main())
