"""forge check-providers subcommand — exercise provider capabilities."""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from theforge.cli.provider_readiness import (
    READINESS_STATUS_READY,
    ReadinessResult,
    build_readiness_probes,
    run_readiness_probe,
)
from theforge.cli.shared import _find_config
from theforge.config import load_config

_MAX_READINESS_WORKERS = 4


def cmd_check_providers(args: object) -> int:
    """Exercise every configured provider capability forge can dispatch."""
    config_path = getattr(args, "config", None)
    if config_path:
        config_path = Path(config_path)
    else:
        config_path = _find_config()
    if config_path is None:
        print("[check-providers] No forge.yaml found", file=sys.stderr)
        return 1

    config = load_config(config_path)
    probes = build_readiness_probes(config)

    # Optional single-profile filter.
    profile_filter = getattr(args, "profile", None)
    if profile_filter:
        probes = [probe for probe in probes if probe.profile.name == profile_filter]
        if not probes:
            print(
                "[check-providers] "
                f"No provider capability probe named '{profile_filter}' found in forge.yaml",
                file=sys.stderr,
            )
            return 1

    max_workers = min(len(probes), _MAX_READINESS_WORKERS) or 1
    print(
        "[check-providers] "
        "forge.yaml: "
        f"{len(probes)} capability probe(s) found; "
        f"running up to {max_workers} at a time\n"
    )

    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(
                run_readiness_probe,
                probe,
                secrets=config.secrets,
            ): probe
            for probe in probes
        }
        for fut in as_completed(futures):
            results.append(fut.result())

    order = {
        (
            probe.role,
            probe.capability,
            probe.profile.name,
            probe.profile.model,
            probe.declared_transport_kind,
        ): i
        for i, probe in enumerate(probes)
    }
    results.sort(
        key=lambda result: order.get(
            (
                result.probe.role,
                result.probe.capability,
                result.probe.profile.name,
                result.probe.profile.model,
                result.probe.declared_transport_kind,
            ),
            999,
        )
    )

    any_failed = False
    for result in results:
        profile = result.probe.profile
        provider = profile.provider_family or "?"
        model = profile.model
        name_col = f"{profile.name:<22}"
        role_col = f"{result.probe.role:<18}"
        capability_col = f"{result.probe.capability:<16}"
        prov_col = f"{provider:<10}"
        model_col = f"{model:<28}"
        if not result.ready:
            any_failed = True
        marker = "\u2713" if result.status == READINESS_STATUS_READY else "\u2717"
        print(
            f"  {name_col} {role_col} {capability_col} {prov_col} {model_col} "
            f"{marker}  {result.status}: {result.detail}"
        )
        for detail_line in _expanded_detail_lines(result):
            print(detail_line)

    passed = sum(1 for result in results if result.status == READINESS_STATUS_READY)
    total = len(results)
    print(f"\n[check-providers] {passed}/{total} passed")
    return 1 if any_failed else 0


def register_parser(subparsers: object) -> None:
    """Register the 'check-providers' subcommand parser."""
    check_providers_parser = subparsers.add_parser(
        "check-providers",
        help="Verify configured provider capability probes in forge.yaml",
    )
    check_providers_parser.add_argument(
        "--profile",
        metavar="NAME",
        help="Test only the named provider capability probe (default: all probes)",
    )
    check_providers_parser.add_argument(
        "--config",
        help="Path to forge.yaml (default: auto-detect)",
    )


def _expanded_detail_lines(result: ReadinessResult) -> list[str]:
    if result.ready:
        return []

    lines = [f"    declared transport: {result.probe.declared_transport_kind}"]
    for attempt in result.attempts:
        lines.append(
            "    "
            f"{attempt.attempted_transport_kind:<8} "
            f"{result.probe.capability:<16} "
            f"{attempt.status}: {attempt.detail}"
        )
    if result.ready_alternate_transport_kinds:
        alternates = ", ".join(result.ready_alternate_transport_kinds)
        lines.append(f"    ready alternate transport: {alternates}")
    return lines
