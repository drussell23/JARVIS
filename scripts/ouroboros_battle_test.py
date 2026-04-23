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
import time
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


def _reap_zombies() -> int:
    """Detect and reap any lingering ouroboros_battle_test.py processes.

    A terminal disconnect or crashed session can leave the battle test
    running in the background, where it continues to burn API budget,
    compete for the intake router lock, and race this new session on
    git branches. This reaper scans for zombies at startup and kills
    them cleanly (SIGTERM, then SIGKILL after 3s) before we boot.

    Only reaps processes:
      • whose cmdline contains ``ouroboros_battle_test.py``
      • owned by the current UID
      • that are not this process

    Returns the number of zombies reaped.
    """
    try:
        import psutil  # type: ignore[import-untyped]
    except ImportError:
        return 0  # Silently skip; psutil is in requirements.txt but not hard-required

    my_pid = os.getpid()
    my_ppid = os.getppid() if hasattr(os, "getppid") else None
    my_uid = os.getuid() if hasattr(os, "getuid") else None

    def _is_battle_test_proc(cmdline: list) -> bool:
        """Strict match: a python interpreter running our script path.

        We require:
          • the first argv is a python-family executable (python/python3/pythonX),
          • and some argv ends with ``ouroboros_battle_test.py`` as a path segment.

        Substring matching is too loose — a shell or editor whose buffer
        contains the literal filename would otherwise be reaped.
        """
        if not cmdline:
            return False
        exe = Path(str(cmdline[0])).name.lower()
        if not exe.startswith("python"):
            return False
        for arg in cmdline[1:]:
            # Match on trailing path segment so `/abs/path/ouroboros_battle_test.py`
            # and `scripts/ouroboros_battle_test.py` both qualify, but `-c "... ouroboros_battle_test.py ..."`
            # embedded in a code string does NOT (that lives in a single argv together
            # with surrounding code, not as a clean path).
            tail = Path(str(arg)).name
            if tail == "ouroboros_battle_test.py":
                return True
        return False

    victims: list = []
    for proc in psutil.process_iter(["pid", "ppid", "cmdline", "uids", "create_time"]):
        try:
            pid = proc.info["pid"]
            if pid == my_pid or pid == my_ppid:
                continue
            cmdline = proc.info.get("cmdline") or []
            if not _is_battle_test_proc(cmdline):
                continue
            if my_uid is not None:
                uids = proc.info.get("uids")
                if uids is not None and getattr(uids, "real", my_uid) != my_uid:
                    continue
            victims.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not victims:
        return 0

    print(f"\n{_BOLD}{_YELLOW}  Zombie Reaper{_RESET}")
    print(f"{_DIM}  {'─' * 52}{_RESET}")
    for p in victims:
        try:
            age_s = time.time() - p.create_time()
            m, s = int(age_s) // 60, int(age_s) % 60
            print(
                f"  {_YELLOW}→{_RESET} reaping PID {p.pid} "
                f"{_DIM}(age {m}m{s:02d}s){_RESET}"
            )
            p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            print(f"  {_DIM}  skipped PID {p.pid}: {type(exc).__name__}{_RESET}")

    # Wait up to 3s for graceful shutdown, then SIGKILL holdouts.
    try:
        alive = psutil.wait_procs(victims, timeout=3.0)[1]
    except Exception:
        alive = victims
    for p in alive:
        try:
            p.kill()
            print(f"  {_RED}→{_RESET} SIGKILL PID {p.pid} {_DIM}(ignored SIGTERM){_RESET}")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    count = len(victims)
    plural = "s" if count != 1 else ""
    print(f"  {_GREEN}✓ reaped {count} zombie{plural}{_RESET}\n")
    return count


def _cleanup_stale_router_lock() -> None:
    """Remove a stale ``.jarvis/intake_router.lock`` left by a crashed session.

    The lock file carries ``{"pid": ..., "ts": ...}`` metadata. If the PID
    is dead (or the file is corrupt), the intake router would already clean
    it on startup — but doing it here first avoids a noisy retry and makes
    the reaper banner tell the whole story in one place.
    """
    lock_path = _PROJECT_ROOT / ".jarvis" / "intake_router.lock"
    if not lock_path.exists():
        return
    try:
        import json as _json
        data = _json.loads(lock_path.read_text() or "{}")
    except (ValueError, OSError):
        try:
            lock_path.unlink()
            print(f"  {_DIM}  cleaned corrupt intake_router.lock{_RESET}")
        except OSError:
            pass
        return
    pid = int(data.get("pid", 0) or 0)
    if pid <= 0:
        return
    try:
        os.kill(pid, 0)  # existence probe — no signal delivered
        # PID is alive; leave the lock alone (router will error loudly if it's us).
    except ProcessLookupError:
        try:
            lock_path.unlink()
            print(
                f"  {_DIM}  cleaned stale intake_router.lock "
                f"(dead PID {pid}){_RESET}"
            )
        except OSError:
            pass
    except PermissionError:
        pass  # Different user — leave it alone


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
        "--max-wall-seconds",
        type=float,
        default=float(os.environ.get("OUROBOROS_BATTLE_MAX_WALL_SECONDS", "0")),
        metavar="SEC",
        help=(
            "Hard wall-clock ceiling on total session duration — fires stop_reason=wall_clock_cap "
            "when exceeded. 0 or unset = disabled (legacy behavior). Graduation soaks MUST set "
            "this (e.g. 2400 = 40 min) to guarantee deterministic termination when provider "
            "retry storms defeat --idle-timeout. Env: OUROBOROS_BATTLE_MAX_WALL_SECONDS."
        ),
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
    # Zombie reaper — kill lingering battle tests from prior sessions
    # before they race us on API budget, git branches, and the intake
    # router lock. Opt-out with JARVIS_BATTLE_REAP_ZOMBIES=false.
    # ------------------------------------------------------------------
    if os.environ.get("JARVIS_BATTLE_REAP_ZOMBIES", "true").lower() not in ("false", "0", "no", "off"):
        _reap_zombies()
        _cleanup_stale_router_lock()

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
        "markdown_it",  # rich.markdown transitive — per-token "entering fence/list/..." spam at DEBUG
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
        max_wall_seconds_s=args.max_wall_seconds or None,
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
    interrupted = False
    try:
        loop.run_until_complete(harness.run())
    except KeyboardInterrupt:
        interrupted = True
        print(f"\n{_YELLOW}Interrupted — shutting down gracefully...{_RESET}")
    finally:
        # Shutdown hygiene (Python 3.9+): drain pending async generators
        # and thread-pool executor tasks before closing the loop. Without
        # this, background asyncio.to_thread / run_in_executor callbacks
        # can race loop.close() and raise "RuntimeError: Event loop is
        # closed" during otherwise-clean session exit. See
        # memory/project_async_shutdown_race_triage.md for the full
        # traceback + root cause analysis.
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        try:
            loop.run_until_complete(loop.shutdown_default_executor())
        except Exception:
            pass
        loop.close()

    # ------------------------------------------------------------------
    # Hot-reload restart respawn (Manifesto §6)
    # ------------------------------------------------------------------
    # If the harness's stop_reason starts with "restart_pending:", the
    # ModuleHotReloader queued a restart because O+V self-modified a
    # quarantined or unsafe-to-reload module. Re-exec this same script
    # with identical argv so the new code is loaded fresh from disk.
    #
    # JARVIS_RESTART_GENERATION (private env var) tracks the depth of the
    # respawn chain to prevent infinite loops if the same self-mod keeps
    # tripping. Capped at JARVIS_RESTART_MAX (default 5).
    if not interrupted and getattr(harness, "stop_reason", "").startswith("restart_pending:"):
        max_restarts = int(os.environ.get("JARVIS_RESTART_MAX", "5"))
        gen = int(os.environ.get("JARVIS_RESTART_GENERATION", "0"))
        if gen >= max_restarts:
            print(
                f"\n{_YELLOW}[respawn] restart cap reached "
                f"(JARVIS_RESTART_GENERATION={gen} >= JARVIS_RESTART_MAX={max_restarts}); "
                f"exiting normally instead of re-execing.{_RESET}"
            )
            sys.exit(0)
        print(
            f"\n{_YELLOW}[respawn] {harness.stop_reason} — "
            f"re-execing battle test (generation {gen + 1}/{max_restarts}){_RESET}"
        )
        os.environ["JARVIS_RESTART_GENERATION"] = str(gen + 1)
        # os.execv replaces this process — code after this line is unreachable.
        # argv[0] is the interpreter, argv[1] is this script, then the original CLI flags.
        os.execv(sys.executable, [sys.executable, *sys.argv])


if __name__ == "__main__":
    main()
