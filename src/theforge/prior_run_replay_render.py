"""Rendering helpers for ``forge prior-run-replay``."""

from __future__ import annotations

import json
from typing import Any

import yaml


def render_terminal(report: dict[str, Any]) -> str:
    aggregate = report["aggregate"]
    lines = [
        (
            "Prior-run selection replay"
            f" - {aggregate['story_count']} stories,"
            f" {aggregate['stories_with_candidates']} with >=1 candidate"
        ),
        "",
        "Qualifying signals:",
    ]
    for signal, count in aggregate["qualifying_signal_counts"].items():
        lines.append(f"  {signal}: {count}")
    lines.extend(
        [
            "",
            "Useful truncation pressure:",
            f"  candidate cap: {aggregate['candidate_cap_useful_phase_count']} phase(s)",
            f"  rendered-claim cap: {aggregate['claim_cap_useful_phase_count']} phase(s)",
        ]
    )
    for corpus in report["corpora"]:
        lines.extend(
            [
                "",
                f"Corpus {corpus['name']} ({corpus['story_count']} stories)",
                f"  root: {corpus['root']}",
            ]
        )
        for probe in corpus["fence_probes"]:
            parts = []
            for run_id, detail in probe["matched"].items():
                state = "offered" if detail["offered"] else "not offered"
                suffix = f" [{detail['reason']}]" if detail["reason"] else ""
                expanded = ""
                if detail.get("offered_in_expanded_probe") and not detail["offered"]:
                    expanded_reason = detail.get("expanded_reason") or ""
                    expanded_suffix = f" [{expanded_reason}]" if expanded_reason else ""
                    expanded = f", expanded probe: offered{expanded_suffix}"
                parts.append(f"{run_id}: {state}{suffix}{expanded}")
            lines.append(
                "  fence probe "
                f"{probe['probe_run_id']}: {'; '.join(parts)}; "
                f"co-surfaced(limit={probe['diagnostic']['selection_limit']})="
                f"{probe['co_surfaced']}; "
                f"co-surfaced(expanded)={probe['co_surfaced_in_expanded_probe']}"
            )
    return "\n".join(lines).rstrip() + "\n"


def render_yaml(report: dict[str, Any]) -> str:
    return yaml.safe_dump(report, sort_keys=False, allow_unicode=True)


def render_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2) + "\n"
