#!/usr/bin/env python3
"""Phase 9.1 — Live-Fire Graduation Soak CLI.

Operator entry point for the cron-driven daily soak cadence that
flips 12+ default-false substrate flags from `false` → `true` via
documented 3-clean-session soak proofs.

## Subcommands

  queue                 — show pending flags + dep status + clean count
  evidence FLAG         — show all evidence rows for FLAG
  run [FLAG]            — run a single soak (next pickable, or specified)
  status                — overall progress (graduated / pending / blocked)
  pause                 — operator-pause (sets env)
  resume                — clear operator-pause

## Master flag

  JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED  — must be ``true`` for
  ``run`` to actually fork the battle-test subprocess. Default off.
  Read-only subcommands (``queue`` / ``evidence`` / ``status``) work
  regardless because they don't fire soaks.

## Composition

This script is a *thin* CLI dispatcher. The substrate lives in
``backend.core.ouroboros.governance.graduation.live_fire_soak``;
this script does parsing + rendering only.

## Cron usage

  0 */8 * * *  cd /path/to/repo && JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED=true \\
               python3 scripts/live_fire_graduation_soak.py run

Three runs per day rotating through pickable flags. Per PRD §9 P9.1
estimate: ~3 flips/week × 12+ flags ≈ 4-6 weeks to fully graduated.
"""
from __future__ import annotations

import argparse
import os
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict

# Ensure project root importable regardless of cwd (cron uses absolute path).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ANSI color codes (auto-stripped when not TTY).
def _supports_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


_USE_COLOR = _supports_color()
_RESET = "\033[0m" if _USE_COLOR else ""
_BOLD = "\033[1m" if _USE_COLOR else ""
_DIM = "\033[2m" if _USE_COLOR else ""
_GREEN = "\033[32m" if _USE_COLOR else ""
_YELLOW = "\033[33m" if _USE_COLOR else ""
_RED = "\033[31m" if _USE_COLOR else ""
_CYAN = "\033[36m" if _USE_COLOR else ""


def _format_flag_row(row: Dict[str, Any]) -> str:
    """Pretty-render one row from harness.queue_view()."""
    flag = row["flag_name"]
    progress = row["progress"]
    clean = progress.get("clean", 0)
    required = progress.get("required", 3)
    runner = progress.get("runner", 0)
    infra = progress.get("infra", 0)
    cadence = row["cadence_class"]
    if row["graduated"]:
        status_color = _GREEN
        status = "GRADUATED"
    elif not row["deps_satisfied"]:
        status_color = _DIM
        status = "BLOCKED"
    elif runner > 0:
        status_color = _RED
        status = "RUNNER-BLOCKED"
    else:
        status_color = _YELLOW
        status = "PENDING"
    deps_str = (
        ",".join(d.split("_")[-1] for d in row["deps"][:3])
        if row["deps"] else "-"
    )
    return (
        f"  {status_color}[{status:>14}]{_RESET}  "
        f"{_BOLD}{flag}{_RESET}\n"
        f"    {_DIM}clean={clean}/{required}  runner={runner}  "
        f"infra={infra}  cadence={cadence}  deps={deps_str}{_RESET}\n"
        f"    {_DIM}{row['description']}{_RESET}"
    )


def cmd_queue(args: argparse.Namespace) -> int:
    """Render the full graduation queue."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        get_default_harness,
    )
    rows = get_default_harness().queue_view()
    n_total = len(rows)
    n_graduated = sum(1 for r in rows if r["graduated"])
    n_pending = sum(
        1 for r in rows
        if not r["graduated"] and r["deps_satisfied"]
    )
    n_blocked = sum(
        1 for r in rows
        if not r["graduated"] and not r["deps_satisfied"]
    )
    print(
        f"\n{_BOLD}{_CYAN}Live-Fire Graduation Queue{_RESET}  "
        f"{_DIM}({n_total} flags){_RESET}"
    )
    print(
        f"  {_GREEN}graduated={n_graduated}{_RESET}  "
        f"{_YELLOW}pending={n_pending}{_RESET}  "
        f"{_DIM}blocked={n_blocked}{_RESET}\n"
    )
    for row in rows:
        print(_format_flag_row(row))
    print()
    return 0


def cmd_evidence(args: argparse.Namespace) -> int:
    """Show all evidence rows for one flag."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        get_default_harness,
    )
    flag = args.flag
    if not flag:
        print(f"  {_RED}--flag required{_RESET}")
        return 2
    harness = get_default_harness()
    rows = harness.evidence_for_flag(flag)
    if not rows:
        print(
            f"  {_DIM}No evidence rows for {flag} in "
            f"{harness.history_file}{_RESET}"
        )
        return 0
    print(
        f"\n{_BOLD}{_CYAN}Evidence rows for {flag}{_RESET}  "
        f"{_DIM}({len(rows)} sessions){_RESET}\n"
    )
    for r in rows:
        outcome = r.get("outcome", "?")
        outcome_color = {
            "clean": _GREEN, "runner": _RED,
            "infra": _YELLOW, "migration": _DIM,
        }.get(outcome, _DIM)
        runner_attr = "RUNNER" if r.get("runner_attributed") else "  -   "
        print(
            f"  {outcome_color}[{outcome:>9}]{_RESET}  "
            f"{r.get('finished_at_iso', '?')}  "
            f"sid={r.get('session_id', '?')}  "
            f"stop={r.get('stop_reason', '?')}  "
            f"cost=${r.get('cost_total_usd', 0):.4f}  "
            f"dur={r.get('duration_s', 0):.1f}s  "
            f"ops={r.get('ops_count', 0)}  "
            f"{_DIM}({runner_attr}){_RESET}"
        )
        notes = r.get("notes", "")
        if notes:
            print(f"    {_DIM}notes: {notes[:160]}{_RESET}")
    print()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Fire a single soak (next pickable, or specified)."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        DEFAULT_COST_CAP_USD, DEFAULT_MAX_WALL_SECONDS,
        HarnessStatus, get_default_harness, is_paused,
        is_soak_harness_enabled,
    )
    if not is_soak_harness_enabled():
        print(
            f"  {_RED}master flag JARVIS_LIVE_FIRE_GRADUATION_SOAK_"
            f"ENABLED is false — cannot run{_RESET}"
        )
        return 3
    if is_paused():
        print(f"  {_YELLOW}paused — set JARVIS_LIVE_FIRE_GRADUATION_"
              f"SOAK_PAUSED=false to resume{_RESET}")
        return 4
    harness = get_default_harness()
    flag = args.flag
    if flag is None:
        flag = harness.pick_next_flag()
        if flag is None:
            print(
                f"  {_GREEN}all flags graduated OR no flag has "
                f"satisfied deps — nothing to run{_RESET}"
            )
            return 0
        print(f"  {_DIM}picked next flag: {flag}{_RESET}")
    print(
        f"  {_BOLD}{_CYAN}Running soak{_RESET} "
        f"flag={flag} cost_cap=${args.cost_cap:.2f} "
        f"max_wall={args.max_wall_seconds}s ..."
    )
    result = harness.run_soak(
        flag_name=flag,
        cost_cap_usd=args.cost_cap,
        max_wall_seconds=args.max_wall_seconds,
        subprocess_timeout_s=args.timeout,
        recorded_by=args.recorded_by,
    )
    status = result.status
    color = (
        _GREEN if status == HarnessStatus.OK
        else _YELLOW if status.value.startswith("skipped_")
        else _RED
    )
    print(
        f"  {color}{status.value}{_RESET}  "
        f"{_DIM}{result.detail}{_RESET}"
    )
    if result.evidence is not None:
        ev = result.evidence
        print(
            f"  {_DIM}session={ev.session_id} "
            f"outcome={ev.outcome} "
            f"runner_attributed={ev.runner_attributed} "
            f"cost=${ev.cost_total_usd:.4f} "
            f"dur={ev.duration_s:.1f}s ops={ev.ops_count}{_RESET}"
        )
    return 0 if status == HarnessStatus.OK else 5


def cmd_status(args: argparse.Namespace) -> int:
    """One-line summary per flag."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        get_default_harness,
    )
    rows = get_default_harness().queue_view()
    print(f"\n{_BOLD}{_CYAN}Graduation Status{_RESET}\n")
    for row in rows:
        progress = row["progress"]
        clean = progress.get("clean", 0)
        required = progress.get("required", 3)
        if row["graduated"]:
            marker = f"{_GREEN}✓{_RESET}"
        elif not row["deps_satisfied"]:
            marker = f"{_DIM}·{_RESET}"
        elif progress.get("runner", 0) > 0:
            marker = f"{_RED}✗{_RESET}"
        else:
            marker = f"{_YELLOW}…{_RESET}"
        print(
            f"  {marker} {row['flag_name']:60s} "
            f"{_DIM}{clean}/{required}{_RESET}"
        )
    print()
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    """Print the pause-env command. We can't mutate parent shell env,
    so we tell the operator what to set."""
    print(
        f"  {_YELLOW}export JARVIS_LIVE_FIRE_GRADUATION_SOAK_PAUSED=true"
        f"{_RESET}"
    )
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    print(f"  {_GREEN}unset JARVIS_LIVE_FIRE_GRADUATION_SOAK_PAUSED{_RESET}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="live_fire_graduation_soak",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent(f"""\
        {_BOLD}{_CYAN}Live-Fire Graduation Soak Harness (Phase 9.1){_RESET}

        Automates the 3-clean-session-per-flag soak cadence that
        graduates 12+ default-false substrate flags. Composes with
        the existing graduation_ledger (CADENCE_POLICY + outcomes
        + clean-counting) and the battle-test harness (forks one
        --headless run per soak).

        {_BOLD}Subcommands:{_RESET}
          {_CYAN}queue{_RESET}      list flags with pending/blocked/graduated state
          {_CYAN}evidence{_RESET}   show evidence rows for a flag
          {_CYAN}run{_RESET}        fire a single soak (next pickable, or specified)
          {_CYAN}status{_RESET}     one-line per-flag summary
          {_CYAN}pause{_RESET}      print pause-env command
          {_CYAN}resume{_RESET}     print resume-env command
        """),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("queue")
    sub.add_parser("status")
    sub.add_parser("pause")
    sub.add_parser("resume")
    p_evidence = sub.add_parser("evidence")
    p_evidence.add_argument("flag", type=str)
    p_run = sub.add_parser("run")
    p_run.add_argument(
        "flag", type=str, nargs="?", default=None,
        help="Specific flag (default: pick-next).",
    )
    p_run.add_argument(
        "--cost-cap", type=float, default=0.50,
        help="Per-soak USD cost cap (default 0.50).",
    )
    p_run.add_argument(
        "--max-wall-seconds", type=int, default=2400,
        help="Per-soak wall-clock cap in seconds (default 2400 = 40min).",
    )
    p_run.add_argument(
        "--timeout", type=int, default=3600,
        help="Subprocess kill timeout in seconds (default 3600).",
    )
    p_run.add_argument(
        "--recorded-by", type=str, default="live_fire_soak_cli",
        help="Operator/runner identity for ledger row (default cli).",
    )
    args = parser.parse_args()
    handlers = {
        "queue": cmd_queue,
        "evidence": cmd_evidence,
        "run": cmd_run,
        "status": cmd_status,
        "pause": cmd_pause,
        "resume": cmd_resume,
    }
    handler = handlers.get(args.cmd)
    if handler is None:
        parser.error(f"unknown subcommand: {args.cmd}")
        return 2
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
