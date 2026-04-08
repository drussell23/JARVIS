#!/usr/bin/env python3
"""
Ouroboros Battle Test Runner
============================

Boots the full Ouroboros + Venom + Trinity Consciousness stack as a
headless daemon that autonomously detects, generates, validates, and
commits code improvements.

6-Layer Architecture:
  1. Strategic Direction  — Manifesto principles injected into every prompt
  2. Trinity Consciousness — Memory, prediction, cross-session learning
  3. Event Spine           — FileWatchGuard + TrinityEventBus, <1s detection
  4. Ouroboros Pipeline    — Governance, adaptive 3-tier routing, parallel ops
  5. Venom Agentic Loop    — read_file, bash, web_search, run_tests, L2 repair
  6. Thought Log           — Observable reasoning, signed commits

Usage::

    python3 scripts/ouroboros_battle_test.py [options]
    python3 scripts/ouroboros_battle_test.py --help

Examples::

    # Default: $0.50 budget, 600s idle timeout, verbose
    python3 scripts/ouroboros_battle_test.py -v

    # Extended session: $2.00 budget, 30 min idle
    python3 scripts/ouroboros_battle_test.py --cost-cap 2.00 --idle-timeout 1800 -v

    # Quick test: $0.10 budget, 2 min idle
    python3 scripts/ouroboros_battle_test.py --cost-cap 0.10 --idle-timeout 120 -v
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import textwrap
from pathlib import Path

# Ensure the project root is importable regardless of cwd.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ANSI color codes
_CYAN = "\033[96m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RED = "\033[91m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


# API keys that .env should always override (stale shell exports are a
# common source of 401 errors during battle test).  Everything else uses
# setdefault so explicit `env VAR=val cmd` still works for non-secret config.
_FORCE_OVERRIDE_KEYS = frozenset({
    "ANTHROPIC_API_KEY",
    "DOUBLEWORD_API_KEY",
})


def _load_env_files() -> None:
    """Load .env files from project root and backend/.

    API keys (ANTHROPIC_API_KEY, DOUBLEWORD_API_KEY) are force-overridden
    from .env so that stale shell exports don't cause silent 401 errors.
    All other variables use setdefault (shell wins).
    """
    for env_path in (_PROJECT_ROOT / ".env", _PROJECT_ROOT / "backend" / ".env"):
        if not env_path.exists():
            continue
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key in _FORCE_OVERRIDE_KEYS:
                os.environ[key] = value  # .env wins for API keys
            else:
                os.environ.setdefault(key, value)


def _check_env(key: str) -> str:
    """Check if an env var is set and return a status indicator."""
    val = os.environ.get(key, "")
    if val:
        return f"{_GREEN}ON{_RESET}"
    return f"{_DIM}OFF{_RESET}"


def _check_env_val(key: str, default: str = "") -> str:
    """Return the value of an env var or default."""
    return os.environ.get(key, default)


def _print_preflight() -> None:
    """Print a preflight checklist showing what's enabled."""
    print(f"\n{_BOLD}{_CYAN}  Preflight Checklist{_RESET}")
    print(f"{_DIM}  {'─' * 52}{_RESET}")

    checks = [
        ("Provider: DoubleWord 397B", "DOUBLEWORD_API_KEY",
         "$0.10/$0.40/M (Tier 0 PRIMARY)"),
        ("Provider: Claude Sonnet", "ANTHROPIC_API_KEY",
         "$3/$15/M (Tier 1 FALLBACK)"),
        ("Venom: Tool Loop", "JARVIS_GOVERNED_TOOL_USE_ENABLED",
         "read_file, search_code, get_callers, list_symbols"),
        ("Venom: Bash (100+ cmds)", "JARVIS_BASH_TOOL_ENABLED",
         "python, git, docker, curl, terraform..."),
        ("Venom: Web Search", "JARVIS_WEB_TOOL_ENABLED",
         "DuckDuckGo / Brave / Google CSE"),
        ("Venom: Run Tests", "JARVIS_TOOL_RUN_TESTS_ALLOWED",
         "pytest in sandbox during generation"),
        ("L2 Repair Engine", "JARVIS_L2_ENABLED",
         f"max {_check_env_val('JARVIS_L2_MAX_ITERS', '5')} iters, "
         f"{_check_env_val('JARVIS_L2_TIMEBOX_S', '120')}s timebox"),
        ("Trinity Consciousness", "JARVIS_CONSCIOUSNESS_ENABLED",
         "Memory + Prophecy + Health"),
    ]

    all_good = True
    for label, env_key, detail in checks:
        status = _check_env(env_key)
        is_on = bool(os.environ.get(env_key, ""))
        if not is_on and env_key in ("DOUBLEWORD_API_KEY", "ANTHROPIC_API_KEY"):
            all_good = False
        indicator = f"  [{status}]"
        print(f"{indicator} {label:<30s} {_DIM}{detail}{_RESET}")

    rounds = _check_env_val("JARVIS_GOVERNED_TOOL_MAX_ROUNDS", "10")
    print(f"\n{_DIM}  Tool rounds: {rounds} (deadline-based, safety ceiling){_RESET}")

    # Check at least one provider
    has_dw = bool(os.environ.get("DOUBLEWORD_API_KEY"))
    has_claude = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if not has_dw and not has_claude:
        print(f"\n  {_RED}{_BOLD}ERROR: No API keys set.{_RESET}")
        print(f"  {_RED}Export DOUBLEWORD_API_KEY or ANTHROPIC_API_KEY.{_RESET}\n")
        sys.exit(1)

    if not has_claude:
        print(f"\n  {_YELLOW}WARNING: ANTHROPIC_API_KEY not set — no Claude fallback.{_RESET}")
    if not has_dw:
        print(f"\n  {_YELLOW}WARNING: DOUBLEWORD_API_KEY not set — Claude only (expensive).{_RESET}")

    print()


def main() -> None:
    # ------------------------------------------------------------------
    # Argument parsing
    # ------------------------------------------------------------------
    parser = argparse.ArgumentParser(
        prog="ouroboros_battle_test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent(f"""\
        {_BOLD}{_CYAN}Ouroboros Battle Test Runner{_RESET}
        {_DIM}Autonomous self-developing AI session{_RESET}

        Boots the full Ouroboros + Venom + Trinity Consciousness stack.
        The organism finds work, reads code, generates Manifesto-aligned
        fixes, runs tests, iteratively converges, commits with its
        signature, and learns from outcomes. Autonomously. In parallel.

        {_BOLD}6-Layer Architecture:{_RESET}
          {_CYAN}1.{_RESET} Strategic Direction  {_DIM}Manifesto principles → every prompt{_RESET}
          {_CYAN}2.{_RESET} Trinity Consciousness {_DIM}Memory + prediction + learning{_RESET}
          {_CYAN}3.{_RESET} Event Spine           {_DIM}FileWatchGuard → TrinityEventBus → sensors{_RESET}
          {_CYAN}4.{_RESET} Ouroboros Pipeline    {_DIM}Governance + routing + parallel ops{_RESET}
          {_CYAN}5.{_RESET} Venom Agentic Loop    {_DIM}bash, web_search, run_tests, L2 repair{_RESET}
          {_CYAN}6.{_RESET} Thought Log           {_DIM}Observable reasoning + signed commits{_RESET}
        """),
        epilog=textwrap.dedent(f"""\
        {_BOLD}Examples:{_RESET}
          %(prog)s -v                          {_DIM}# Default: $0.50, 600s idle{_RESET}
          %(prog)s --cost-cap 2.00 -v          {_DIM}# Extended: $2.00 budget{_RESET}
          %(prog)s --cost-cap 0.10 -v          {_DIM}# Quick test: $0.10 budget{_RESET}

        {_BOLD}Artifacts produced:{_RESET}
          {_DIM}ouroboros/battle-test/<timestamp>    Git branch with autonomous commits
          .jarvis/ouroboros_thoughts.jsonl     Reasoning thread
          .jarvis/test_results.json            Structured test results
          .ouroboros/sessions/bt-*/             Session summary + cost tracker{_RESET}

        {_BOLD}Commit signature:{_RESET}
          {_DIM}Author: JARVIS Ouroboros <ouroboros@jarvis.local>
          Generated-By: Ouroboros + Venom + Consciousness
          Signed-off-by: JARVIS Ouroboros <ouroboros@jarvis.local>{_RESET}
        """),
    )
    parser.add_argument(
        "--cost-cap",
        type=float,
        default=float(os.environ.get("OUROBOROS_BATTLE_COST_CAP", "0.50")),
        metavar="USD",
        help="Session budget in USD (env: OUROBOROS_BATTLE_COST_CAP, default: 0.50)",
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=float(os.environ.get("OUROBOROS_BATTLE_IDLE_TIMEOUT", "600")),
        metavar="SEC",
        help="Inactivity timeout in seconds (env: OUROBOROS_BATTLE_IDLE_TIMEOUT, default: 600)",
    )
    parser.add_argument(
        "--branch-prefix",
        type=str,
        default=os.environ.get("OUROBOROS_BATTLE_BRANCH_PREFIX", "ouroboros/battle-test"),
        metavar="PREFIX",
        help="Git branch prefix (default: ouroboros/battle-test)",
    )
    parser.add_argument(
        "--repo-path",
        type=str,
        default=os.environ.get("JARVIS_REPO_PATH", str(_PROJECT_ROOT)),
        metavar="PATH",
        help="Repository root path (default: project root)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging (shows thought process).",
    )

    args = parser.parse_args()

    # ------------------------------------------------------------------
    # Load environment
    # ------------------------------------------------------------------
    _load_env_files()
    os.environ.setdefault("JARVIS_GOVERNANCE_MODE", "governed")

    # ------------------------------------------------------------------
    # Preflight checklist
    # ------------------------------------------------------------------
    _print_preflight()

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format=(
            f"{_DIM}%(asctime)s{_RESET} "
            f"[%(name)s] "
            f"%(levelname)s %(message)s"
        ),
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # Suppress noisy loggers that flood DEBUG output with internal details
    for _noisy in (
        "fsevents", "watchdog", "watchdog.observers",  # file watcher internals
        "aiohttp.access", "urllib3", "urllib3.connectionpool",  # HTTP internals
        "chromadb", "chromadb.telemetry",  # vector store internals
        "anthropic._base_client", "anthropic._client",  # Anthropic SDK request/response dumps
        "httpcore", "httpx",  # HTTP transport internals
        "asyncio",  # event loop debug
    ):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    # ------------------------------------------------------------------
    # Build config + harness
    # ------------------------------------------------------------------
    from backend.core.ouroboros.battle_test.harness import BattleTestHarness, HarnessConfig

    config = HarnessConfig(
        repo_path=Path(args.repo_path),
        cost_cap_usd=args.cost_cap,
        idle_timeout_s=args.idle_timeout,
        branch_prefix=args.branch_prefix,
    )

    harness = BattleTestHarness(config)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        harness.register_signal_handlers(loop)
    except Exception:
        pass  # Windows or unsupported platform

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    try:
        loop.run_until_complete(harness.run())
    except KeyboardInterrupt:
        print(f"\n{_YELLOW}Interrupted — shutting down gracefully...{_RESET}")
    finally:
        loop.close()


if __name__ == "__main__":
    main()
