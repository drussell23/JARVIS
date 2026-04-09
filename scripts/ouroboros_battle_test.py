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
import importlib.metadata as _metadata
import logging
import os
import sys
import textwrap
import warnings
from pathlib import Path

# Python 3.9 compat: patch packages_distributions before any library touches it
if not hasattr(_metadata, "packages_distributions"):
    def _packages_distributions_fallback():  # type: ignore[misc]
        """Minimal fallback for packages_distributions on Python <3.11."""
        try:
            from importlib_metadata import packages_distributions  # type: ignore[import-untyped]
            return packages_distributions()
        except Exception:
            return {}
    _metadata.packages_distributions = _packages_distributions_fallback  # type: ignore[attr-defined]

# Suppress noisy warnings that leak to terminal (urllib3, google, etc.)
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*NotOpenSSLWarning.*")
warnings.filterwarnings("ignore", message=".*urllib3.*")

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


def _replay_session(session_ref: str) -> None:
    """Replay a previous battle test session timeline.

    Parameters
    ----------
    session_ref:
        Either a session ID (e.g. ``bt-2026-04-08-143022``), a direct path
        to ``summary.json``, or ``"list"`` to show available sessions.
    """
    import json

    sessions_root = _PROJECT_ROOT / ".ouroboros" / "sessions"

    # ── List mode ──
    if session_ref.lower() == "list":
        if not sessions_root.exists():
            print(f"  {_RED}No sessions found in {sessions_root}{_RESET}")
            return
        found = sorted(sessions_root.iterdir(), reverse=True)
        if not found:
            print(f"  {_RED}No sessions found.{_RESET}")
            return
        print(f"\n{_BOLD}{_CYAN}  Available Sessions{_RESET}")
        print(f"{_DIM}  {'─' * 52}{_RESET}")
        for d in found[:20]:
            summary_path = d / "summary.json"
            if summary_path.exists():
                try:
                    data = json.loads(summary_path.read_text())
                    ops = len(data.get("operations", []))
                    cost = data.get("cost_total", 0.0)
                    dur = data.get("duration_s", 0.0)
                    m, s = int(dur) // 60, int(dur) % 60
                    stop = data.get("stop_reason", "?")
                    print(
                        f"  {_CYAN}{d.name}{_RESET}  "
                        f"{ops} ops  ${cost:.3f}  {m}m{s:02d}s  "
                        f"{_DIM}{stop}{_RESET}"
                    )
                except Exception:
                    print(f"  {_CYAN}{d.name}{_RESET}  {_DIM}(corrupt summary){_RESET}")
            else:
                print(f"  {_DIM}{d.name}  (no summary.json){_RESET}")
        print()
        return

    # ── Resolve summary.json path ──
    summary_path: Path
    if session_ref.endswith(".json") and Path(session_ref).exists():
        summary_path = Path(session_ref)
    else:
        # Try as session ID
        candidate = sessions_root / session_ref / "summary.json"
        if candidate.exists():
            summary_path = candidate
        else:
            # Try partial match
            matches = sorted(sessions_root.glob(f"*{session_ref}*"))
            if matches:
                summary_path = matches[-1] / "summary.json"
            else:
                print(f"  {_RED}Session not found: {session_ref}{_RESET}")
                print(f"  {_DIM}Use --replay list to see available sessions{_RESET}")
                return

    if not summary_path.exists():
        print(f"  {_RED}Summary not found: {summary_path}{_RESET}")
        return

    data = json.loads(summary_path.read_text())
    operations = data.get("operations", [])
    session_id = data.get("session_id", "unknown")
    duration_s = data.get("duration_s", 0.0)
    cost_total = data.get("cost_total", 0.0)
    stop_reason = data.get("stop_reason", "unknown")
    stats = data.get("stats", {})

    m, s = int(duration_s) // 60, int(duration_s) % 60

    # ── Header ──
    print(f"\n{'═' * 64}")
    print(f"  {_BOLD}{_CYAN}SESSION REPLAY{_RESET}  {session_id}")
    print(f"  {_DIM}Duration: {m}m {s:02d}s │ Cost: ${cost_total:.3f} │ Stop: {stop_reason}{_RESET}")
    print(f"  {_DIM}Attempted: {stats.get('attempted', '?')} │ "
          f"Completed: {stats.get('completed', '?')} │ "
          f"Failed: {stats.get('failed', '?')} │ "
          f"Queued: {stats.get('queued', '?')}{_RESET}")
    print(f"{'═' * 64}\n")

    if not operations:
        print(f"  {_DIM}No operations recorded in this session.{_RESET}\n")
        return

    # ── Sort by recorded_at for chronological timeline ──
    operations.sort(key=lambda o: o.get("recorded_at", 0.0))

    # Find session start time (earliest recorded_at - elapsed)
    first_ts = operations[0].get("recorded_at", 0.0)
    first_elapsed = operations[0].get("elapsed_s", 0.0)
    session_start = first_ts - first_elapsed if first_ts else 0.0

    # ── Timeline ──
    for i, op in enumerate(operations, 1):
        op_id = op.get("op_id", "?")
        short_id = op_id[:12] if len(op_id) > 12 else op_id
        status = op.get("status", "?")
        sensor = op.get("sensor", "?")
        provider = op.get("provider", "?")
        cost = op.get("cost_usd", 0.0)
        elapsed = op.get("elapsed_s", 0.0)
        technique = op.get("technique", "")
        tool_calls = op.get("tool_calls", 0)
        files_changed = op.get("files_changed", 0)
        recorded_at = op.get("recorded_at", 0.0)

        # Time offset from session start
        offset_s = recorded_at - session_start if session_start else 0.0
        om, os_ = int(offset_s) // 60, int(offset_s) % 60

        # Status icon + color
        if status == "completed":
            icon = f"{_GREEN}✅"
            status_color = _GREEN
        elif status == "failed":
            icon = f"{_RED}💀"
            status_color = _RED
        elif status == "queued":
            icon = f"{_YELLOW}⏳"
            status_color = _YELLOW
        elif status == "cancelled":
            icon = f"{_DIM}⏭️"
            status_color = _DIM
        else:
            icon = f"{_DIM}?"
            status_color = _DIM

        # Provider short name
        prov_map = {
            "doubleword-397b": "DW-397B", "doubleword": "DW-397B",
            "claude-api": "Claude", "claude": "Claude",
            "gcp-jprime": "J-Prime",
        }
        prov_short = prov_map.get(provider, provider[:10])

        print(
            f"  {_DIM}[{om:02d}:{os_:02d}]{_RESET} "
            f"{icon}{_RESET} "
            f"{_CYAN}{short_id}{_RESET}  "
            f"{status_color}{status:<10s}{_RESET}  "
            f"{sensor}"
        )

        detail_parts = []
        if prov_short:
            detail_parts.append(f"via {prov_short}")
        detail_parts.append(f"{elapsed:.1f}s")
        if cost > 0:
            detail_parts.append(f"${cost:.4f}")
        if tool_calls:
            detail_parts.append(f"{tool_calls} tools")
        if files_changed:
            detail_parts.append(f"{files_changed} files")
        if technique:
            detail_parts.append(technique)

        print(f"  {_DIM}         {'  │  '.join(detail_parts)}{_RESET}")

        # ── Check for ledger entries ──
        ledger_path = _PROJECT_ROOT / ".jarvis" / "ouroboros" / "ledger" / f"{op_id}.jsonl"
        if ledger_path.exists():
            try:
                ledger_lines = ledger_path.read_text().strip().splitlines()
                phases = []
                for ll in ledger_lines:
                    entry = json.loads(ll)
                    phase = entry.get("phase", entry.get("state", ""))
                    if phase:
                        phases.append(phase)
                if phases:
                    chain = " → ".join(phases)
                    print(f"  {_DIM}         {chain}{_RESET}")
            except Exception:
                pass

        print()

    # ── Footer ──
    top_sensors = data.get("top_sensors", [])
    if top_sensors:
        print(f"  {_BOLD}Top Sensors:{_RESET}")
        for name, count in top_sensors[:5]:
            print(f"    {name:<30s} {count} ops")
        print()

    convergence = data.get("convergence_state", "")
    if convergence:
        slope = data.get("convergence_slope", 0.0)
        r2 = data.get("convergence_r2", 0.0)
        print(
            f"  {_BOLD}Convergence:{_RESET} {convergence}  "
            f"{_DIM}(slope={slope:.4f}, R²={r2:.2f}){_RESET}"
        )
        print()

    print(f"{'═' * 64}\n")


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
    parser.add_argument(
        "--replay",
        type=str,
        default=None,
        metavar="SESSION_ID",
        help=(
            "Replay a previous session timeline instead of running live. "
            "Pass a session ID (e.g. bt-2026-04-08-143022) or a path to "
            "summary.json. Lists available sessions when set to 'list'."
        ),
    )

    args = parser.parse_args()

    # ------------------------------------------------------------------
    # Replay mode — show a previous session timeline and exit
    # ------------------------------------------------------------------
    if args.replay is not None:
        _replay_session(args.replay)
        return

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
        "aiosqlite",  # SQLite debug queries
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
