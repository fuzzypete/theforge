"""forge check-providers subcommand — smoke-test all API-mode profiles."""

from __future__ import annotations

import dataclasses
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from theforge.agent_types import AgentResult
from theforge.cli.shared import _find_config
from theforge.config import load_config
from theforge.runners.api import run_api_agent

_CHECK_PROVIDERS_PROMPT = (
    "Review this diff: -    x = 1\n+    x = 2\n\n"
    "Respond with JSON containing a 'verdict' field set to APPROVE or REQUEST_CHANGES, "
    "and a 'summary' field with a one-line explanation."
)


def cmd_check_providers(args: object) -> int:
    """Smoke-test all API-mode profiles in forge.yaml."""
    config_path = getattr(args, "config", None)
    if config_path:
        config_path = Path(config_path)
    else:
        config_path = _find_config()
    if config_path is None:
        print("[check-providers] No forge.yaml found", file=sys.stderr)
        return 1

    config = load_config(config_path)

    # Collect all unique API profiles (by name) across all config slots.
    seen: dict[str, object] = {}
    candidates = [
        config.dev_profile,
        config.preflight_profile,
        *config.review_pool,
    ]
    if config.synthesis_profile is not None:
        candidates.append(config.synthesis_profile)
    if config.plan_agent_review.enabled:
        candidates.extend(config.plan_agent_review.profiles)

    api_profiles = []
    for p in candidates:
        if p.mode == "api" and p.name not in seen:
            seen[p.name] = p
            api_profiles.append(p)

    # Optional single-profile filter.
    profile_filter = getattr(args, "profile", None)
    if profile_filter:
        api_profiles = [p for p in api_profiles if p.name == profile_filter]
        if not api_profiles:
            print(
                f"[check-providers] No API profile named '{profile_filter}' found in forge.yaml",
                file=sys.stderr,
            )
            return 1

    print(f"[check-providers] forge.yaml: {len(api_profiles)} API profile(s) found\n")

    working_dir = config_path.parent

    def _run_one(profile: object) -> tuple:
        # Use single-shot mode by ensuring allowed_tools is empty.
        single_shot = dataclasses.replace(profile, allowed_tools=())
        t0 = time.perf_counter()
        try:
            result: AgentResult = run_api_agent(
                prompt=_CHECK_PROVIDERS_PROMPT,
                profile=single_shot,
                working_dir=working_dir,
                quiet=True,
                secrets=config.secrets,
            )
            elapsed = time.perf_counter() - t0
            return (profile, elapsed, result)
        except Exception as exc:
            elapsed = time.perf_counter() - t0
            return (profile, elapsed, exc)

    results = []
    with ThreadPoolExecutor(max_workers=len(api_profiles) or 1) as pool:
        futures = {pool.submit(_run_one, p): p for p in api_profiles}
        for fut in as_completed(futures):
            results.append(fut.result())

    # Sort results to match original profile order.
    order = {p.name: i for i, p in enumerate(api_profiles)}
    results.sort(key=lambda t: order.get(t[0].name, 999))

    any_failed = False
    for profile, elapsed, outcome in results:
        provider = profile.provider or "?"
        model = profile.model
        name_col = f"{profile.name:<22}"
        prov_col = f"{provider:<10}"
        model_col = f"{model:<28}"

        is_local = profile.base_url and (
            "localhost" in profile.base_url or "127.0.0.1" in profile.base_url
        )
        local_tag = " [local]" if is_local else ""

        if isinstance(outcome, Exception):
            any_failed = True
            print(
                f"  {name_col} {prov_col} {model_col} \u2717  {type(outcome).__name__}: {outcome}"
            )
        elif not outcome.success:
            any_failed = True
            short_err = (outcome.output or "unknown error")[:120]
            print(f"  {name_col} {prov_col} {model_col} \u2717  {short_err}")
        elif outcome.structured_data is None or "verdict" not in outcome.structured_data:
            any_failed = True
            msg = "no valid verdict in structured output"
            print(f"  {name_col} {prov_col} {model_col} \u2717  {msg}")
        else:
            if is_local or outcome.cost_usd == 0.0:
                cost_str = "$0.000"
            elif outcome.cost_usd is not None:
                cost_str = f"${outcome.cost_usd:.3f}"
            else:
                cost_str = "$?.???"
            suffix = f"{elapsed:.1f}s  {cost_str}{local_tag}"
            print(f"  {name_col} {prov_col} {model_col} \u2713  {suffix}")

    passed = sum(
        1
        for _, _, o in results
        if not isinstance(o, Exception)
        and o.success
        and o.structured_data is not None
        and "verdict" in o.structured_data
    )
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
