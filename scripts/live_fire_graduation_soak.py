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

Operator helper (sets graduation ledger + soak + contract + DW env):
``bash scripts/run_live_fire_graduation_soak.sh [subcommand ...]``.
Example crontab fragment: ``scripts/crontab-live-fire.example``.
Installer: ``bash scripts/install_live_fire_soak_cron.sh --install``.

Three runs per day rotating through pickable flags. Per PRD §9 P9.1
estimate: ~3 flips/week × 12+ flags ≈ 4-6 weeks to fully graduated.
"""
from __future__ import annotations

import argparse
import os
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, Optional

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


def _warn_if_ledger_master_unset() -> None:
    """Phase 9 Slice 5 UX footgun — when the operator runs `queue`
    or `ready` without the ledger master flag set, every progress
    counter renders as 0/N and looks like a data bug. Print one
    clear line at the top so the all-zeros state can never be
    confused with "the cadence didn't accumulate evidence." The
    canonical wrapper script and the cron entry both export the
    flag; only direct CLI invocations skip it."""
    raw = os.environ.get(
        "JARVIS_GRADUATION_LEDGER_ENABLED", "",
    ).strip().lower()
    if raw not in ("1", "true", "yes", "on"):
        print(
            f"  {_YELLOW}!{_RESET} "
            f"{_DIM}JARVIS_GRADUATION_LEDGER_ENABLED is unset "
            f"— progress counters will read as zeros even if the "
            f"ledger has rows.\n"
            f"    Re-run via "
            f"{_CYAN}bash scripts/run_live_fire_graduation_soak.sh{_RESET}"
            f"{_DIM} OR set the flag explicitly:\n"
            f"      "
            f"{_CYAN}JARVIS_GRADUATION_LEDGER_ENABLED=true "
            f"python3 scripts/live_fire_graduation_soak.py "
            f"<subcommand>{_RESET}"
            f"{_DIM}{_RESET}"
        )


def cmd_queue(args: argparse.Namespace) -> int:
    """Render the full graduation queue."""
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        get_default_harness,
    )
    _warn_if_ledger_master_unset()
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


def cmd_ready(args: argparse.Namespace) -> int:
    """Render only the flags that are ready to flip — composes the
    existing ``GraduationLedger.eligible_flags()`` primitive so the
    operator can answer 'which flags should I flip now?' in one
    command instead of scanning the full ``queue`` output. Phase 9
    hardening 2026-05-05 — closes the operator-UX gap that wasted
    an estimated ~30s/glance × 12+ flags × 36+ soaks of cognitive
    load."""
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
        get_default_ledger,
    )
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
        get_default_harness,
    )
    _warn_if_ledger_master_unset()
    ledger = get_default_ledger()
    eligible = ledger.eligible_flags()
    rows = get_default_harness().queue_view()
    n_total = len(rows)
    n_graduated = sum(1 for r in rows if r["graduated"])
    n_ready = len(eligible)
    print(
        f"\n{_BOLD}{_CYAN}Live-Fire Ready-to-Flip Queue{_RESET}  "
        f"{_DIM}({n_total} flags total){_RESET}"
    )
    print(
        f"  {_GREEN}graduated={n_graduated}{_RESET}  "
        f"{_GREEN}{_BOLD}ready_to_flip={n_ready}{_RESET}\n"
    )
    if not eligible:
        print(
            f"  {_DIM}No flags ready to flip yet — accumulate "
            f"more clean evidence (run `bash scripts/"
            f"run_live_fire_graduation_soak.sh`).{_RESET}\n"
        )
        return 0
    rows_by_flag = {r["flag_name"]: r for r in rows}
    for flag in eligible:
        row = rows_by_flag.get(flag)
        if row is None:
            print(f"  {_GREEN}{flag}{_RESET}")
            continue
        progress = ledger.progress(flag)
        clean = progress["clean"]
        required = progress["required"]
        deps = row.get("deps_satisfied", True)
        deps_marker = (
            f"{_GREEN}deps=ok{_RESET}" if deps
            else f"{_RED}deps=BLOCKED{_RESET}"
        )
        print(
            f"  {_GREEN}{_BOLD}{flag}{_RESET}\n"
            f"    {_DIM}clean={clean}/{required}  "
            f"runner=0  {deps_marker}{_RESET}\n"
            f"    {_DIM}{row.get('description', '')}{_RESET}"
        )
    print(
        f"\n  {_BOLD}Next steps:{_RESET}\n"
        f"  {_DIM}1. Flip the flag(s) above to default-true in "
        f"flag_registry_seed.py + helper functions{_RESET}\n"
        f"  {_DIM}2. Land the change; ledger automatically marks "
        f"as graduated on next read{_RESET}\n"
    )
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
        # §3.6.2 vector #6 producer-loop wiring (2026-05-07) —
        # feed the Phase9Orchestrator interaction matrix so
        # `/phase9 partners` populates as the cadence runs.
        # Composes the SAME flag-set the harness used to build
        # the subprocess env (target flag + its graduated
        # dependencies — single source of truth via
        # ``get_dependencies``). NEVER raises into the run
        # path. Master-flag-gate decision (operator binding
        # 2026-05-07): if JARVIS_PHASE9_ORCHESTRATOR_ENABLED
        # is off, surface a structured operator-visible
        # message rather than silently no-op-ing — operators
        # who wonder why the matrix stays empty get a clear
        # diagnostic.
        _record_phase9_interaction_matrix(
            session_id=str(ev.session_id),
            target_flag=flag,
        )
    return 0 if status == HarnessStatus.OK else 5


def _record_phase9_interaction_matrix(
    *, session_id: str, target_flag: str,
) -> None:
    """§3.6.2 vector #6 producer-loop wiring. Composes the
    canonical flag-set (target + dependencies) and feeds the
    Phase9Orchestrator append-only matrix. NEVER raises.

    Master-flag-gate decision: when
    ``JARVIS_PHASE9_ORCHESTRATOR_ENABLED`` is OFF, prints an
    operator-visible diagnostic explaining that the matrix
    will not populate (rather than silently no-op-ing — which
    would leave operators wondering why ``/phase9 partners``
    stays empty). When ON, performs the append + prints a
    one-liner confirmation."""
    try:
        from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
            get_dependencies,
        )
        from backend.core.ouroboros.governance.phase9_orchestrator import (  # noqa: E501
            get_default_orchestrator,
            master_enabled as phase9_master_enabled,
        )
    except ImportError:
        # Substrate unavailable (rollback branch) —
        # operator-visible note + skip.
        print(
            f"  {_YELLOW}[phase9-matrix] substrate unavailable "
            f"(import error); session_id={session_id!r} not "
            f"recorded into interaction matrix{_RESET}"
        )
        return
    try:
        if not phase9_master_enabled():
            # OPERATOR-VISIBLE DIAGNOSTIC (operator binding
            # 2026-05-07: do not silently no-op).
            print(
                f"  {_YELLOW}[phase9-matrix] "
                f"JARVIS_PHASE9_ORCHESTRATOR_ENABLED=false → "
                f"interaction matrix NOT recorded for "
                f"session {session_id}. To populate: set "
                f"the env var to true (cron / wrapper / "
                f"operator). /phase9 partners will stay empty "
                f"until then.{_RESET}"
            )
            return
        deps = get_dependencies(target_flag) or frozenset()
        flags_enabled = (target_flag,) + tuple(sorted(deps))
        ok = get_default_orchestrator().record_session_flags(
            session_id=session_id,
            flags_enabled=flags_enabled,
        )
        if ok:
            partner_count = max(0, len(flags_enabled) - 1)
            print(
                f"  {_DIM}[phase9-matrix] recorded "
                f"session={session_id} flags="
                f"{len(flags_enabled)} "
                f"(target+{partner_count} deps){_RESET}"
            )
        else:
            print(
                f"  {_YELLOW}[phase9-matrix] record_session_"
                f"flags returned False for "
                f"session={session_id}{_RESET}"
            )
    except Exception as exc:  # noqa: BLE001 — defensive
        # NEVER raises into the run path; surface failure as
        # operator-visible non-fatal.
        print(
            f"  {_YELLOW}[phase9-matrix] non-fatal error "
            f"recording session={session_id}: {exc}{_RESET}"
        )


def cmd_status(args: argparse.Namespace) -> int:
    """One-line summary per flag + cadence health (Slice 3)."""
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
    # Cadence Slice 3 (2026-05-06) — overdue detector composing
    # manifest + health + history. Renders below the per-flag
    # queue so operators see "did the schedule fire when
    # expected?" alongside "which flags are progressing?".
    # Fail-silent on substrate unavailability.
    try:
        from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
            evaluate_cadence_status,
            render_cadence_status_block,
        )
        report = evaluate_cadence_status()
        print(render_cadence_status_block(report))
    except Exception as exc:  # noqa: BLE001 — fail-silent
        sys.stderr.write(
            f"cadence_status unavailable: "
            f"{type(exc).__name__}: {exc}\n",
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


def cmd_write_cadence_manifest(
    args: argparse.Namespace,
) -> int:
    """Write the cadence manifest at install time.

    Composes :func:`cadence_manifest.write_manifest`. Invoked
    by ``install_live_fire_soak_cron.sh`` after a successful
    ``crontab -`` (or by the launchd installer in Slice 4)
    so the schedule's interval is captured as the single
    source of truth for the overdue detector (Slice 3).

    Cadence Slice 1 — closes the cadence-observability gap
    surfaced 2026-05-06.
    """
    try:
        from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
            write_manifest,
        )
    except ImportError as exc:
        sys.stderr.write(
            f"error: cadence_manifest substrate unavailable: "
            f"{exc}\n",
        )
        return 2
    extras: Dict[str, Any] = {}
    for kv in (args.extra or []):
        if "=" not in kv:
            continue
        k, _, v = kv.partition("=")
        k = k.strip()
        if not k:
            continue
        extras[k] = v.strip()
    interval_override: Optional[int] = None
    if args.interval_hint_s is not None:
        interval_override = int(args.interval_hint_s)
    ok, detail = write_manifest(
        schedule_kind=args.kind,
        schedule_string=args.schedule,
        installer_version=args.installer_version,
        extras=extras,
        interval_hint_s=interval_override,
    )
    if not ok:
        sys.stderr.write(
            f"error: write_manifest failed: {detail}\n",
        )
        return 2
    print(f"cadence_manifest written: {detail}")
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
          {_CYAN}ready{_RESET}      list ONLY flags ready to flip (Phase 9 hardening)
          {_CYAN}evidence{_RESET}   show evidence rows for a flag
          {_CYAN}run{_RESET}        fire a single soak (next pickable, or specified)
          {_CYAN}status{_RESET}     one-line per-flag summary
          {_CYAN}pause{_RESET}      print pause-env command
          {_CYAN}resume{_RESET}     print resume-env command
        """),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("queue")
    sub.add_parser("ready")
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
    # Cadence Slice 1 (2026-05-06) — write cadence_manifest.json.
    # Invoked by install_live_fire_soak_cron.sh after successful
    # crontab install + by Slice 4 launchd installer. Single source
    # of truth for cadence interval; overdue detector (Slice 3)
    # reads this manifest, no magic numbers in detection modules.
    p_manifest = sub.add_parser(
        "write-cadence-manifest",
        help=(
            "Write .jarvis/cadence_manifest.json — installer "
            "invokes this AFTER crontab/launchd install."
        ),
    )
    p_manifest.add_argument(
        "--kind", type=str, required=True,
        choices=("cron", "launchd"),
        help="Schedule kind.",
    )
    p_manifest.add_argument(
        "--schedule", type=str, required=True,
        help=(
            "Schedule string — raw crontab line for cron, "
            "StartInterval seconds for launchd."
        ),
    )
    p_manifest.add_argument(
        "--installer-version", type=str, default="1.0",
        help="Installer version stamp.",
    )
    p_manifest.add_argument(
        "--interval-hint-s", type=int, default=None,
        help=(
            "Override the derived interval hint in seconds "
            "(rare; only when caller knows better than the "
            "cron-spec parser)."
        ),
    )
    p_manifest.add_argument(
        "--extra", type=str, action="append",
        help=(
            "Extra key=value metadata stamped on the manifest "
            "(repeatable). E.g. --extra cost_cap_usd=0.50 "
            "--extra wall_cap_s=2400."
        ),
    )
    args = parser.parse_args()
    handlers = {
        "queue": cmd_queue,
        "ready": cmd_ready,
        "evidence": cmd_evidence,
        "run": cmd_run,
        "status": cmd_status,
        "pause": cmd_pause,
        "resume": cmd_resume,
        "write-cadence-manifest": cmd_write_cadence_manifest,
    }
    handler = handlers.get(args.cmd)
    if handler is None:
        parser.error(f"unknown subcommand: {args.cmd}")
        return 2
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
