"""forge check-providers subcommand — exercise API provider capabilities."""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from theforge.cli.provider_readiness import (
    READINESS_STATUS_READY,
    build_readiness_probes,
    run_readiness_probe,
)
from theforge.cli.shared import _find_config
from theforge.config import load_config


def cmd_check_providers(args: object) -> int:
    """Exercise every API capability forge can dispatch from forge.yaml."""
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
                f"[check-providers] No API profile named '{profile_filter}' found in forge.yaml",
                file=sys.stderr,
            )
            return 1

    print(f"[check-providers] forge.yaml: {len(probes)} capability probe(s) found\n")

    working_dir = config_path.parent

    results = []
    with ThreadPoolExecutor(max_workers=len(probes) or 1) as pool:
        futures = {
            pool.submit(
                run_readiness_probe,
                probe,
                working_dir=working_dir,
                secrets=config.secrets,
            ): probe
            for probe in probes
        }
        for fut in as_completed(futures):
            results.append(fut.result())

    order = {
        (probe.role, probe.capability, probe.profile.name, probe.profile.model): i
        for i, probe in enumerate(probes)
    }
    results.sort(
        key=lambda result: order.get(
            (
                result.probe.role,
                result.probe.capability,
                result.probe.profile.name,
                result.probe.profile.model,
            ),
            999,
        )
    )

    any_failed = False
    for result in results:
        profile = result.probe.profile
        provider = profile.provider or "?"
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

    passed = sum(1 for result in results if result.status == READINESS_STATUS_READY)
    total = len(results)
    print(f"\n[check-providers] {passed}/{total} passed")
    return 1 if any_failed else 0


def register_parser(subparsers: object) -> None:
    """Register the 'check-providers' subcommand parser."""
    check_providers_parser = subparsers.add_parser(
        "check-providers",
        help="Smoke-test all API-mode profiles in forge.yaml",
    )
    check_providers_parser.add_argument(
        "--profile",
        metavar="NAME",
        help="Test only the named profile (default: all API profiles)",
    )
    check_providers_parser.add_argument(
        "--config",
        help="Path to forge.yaml (default: auto-detect)",
    )
