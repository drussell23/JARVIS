"""/governor REPL dispatcher — Slice 3 of Wave 1 #3 arc.

Operator surface for inspecting sensor budget state + memory pressure
+ emergency brake status. Consumes both primitives (Slice 1 + Slice 2).

Subcommands::

    /governor                     status (default)
    /governor status              current posture + brake + per-sensor budgets
    /governor explain             full decision detail + recent history
    /governor history [N]         last N budget decisions
    /governor reset               clear all counters (operator override)
    /governor memory              memory pressure level + fanout projection
    /governor help

Authority posture
-----------------

* §1 read-only + operator-scoped writes — ``reset`` clears counters
  (audited) but doesn't mutate authority, risk, or gate state.
* §8 observability — ``status`` + ``explain`` expose the full state
  machine. ``reset`` writes a stderr audit line (no dedicated file —
  the governor's rolling window is itself ephemeral).
* Authority-free — grep-pinned Slice 4.
"""
from __future__ import annotations

import logging
import shlex
import textwrap
import time
from dataclasses import dataclass
from typing import Any, List, Optional

logger = logging.getLogger("Ouroboros.GovernorREPL")


_COMMANDS = frozenset({"/governor"})


_HELP = textwrap.dedent(
    """
    /governor — sensor budget + memory pressure inspection
    -------------------------------------------------------
      /governor                    current status
      /governor status             posture + brake + per-sensor budgets
      /governor explain            full decision detail + recent history
      /governor history [N]        last N budget decisions (default 10)
      /governor reset              clear all counters (audited)
      /governor memory             memory pressure + fanout projection
      /governor help               this text

    Requires JARVIS_SENSOR_GOVERNOR_ENABLED=true for sensor subcommands
    and JARVIS_MEMORY_PRESSURE_GATE_ENABLED=true for memory subcommand.
    """
).strip()


@dataclass
class GovernorDispatchResult:
    ok: bool
    text: str
    matched: bool = True


def _matches(line: str) -> bool:
    if not line:
        return False
    return line.split(None, 1)[0] in _COMMANDS


def _governor_enabled() -> bool:
    try:
        from backend.core.ouroboros.governance.sensor_governor import (
            is_enabled,
        )
    except ImportError:
        return False
    return is_enabled()


def _memory_gate_enabled() -> bool:
    try:
        from backend.core.ouroboros.governance.memory_pressure_gate import (
            is_enabled,
        )
    except ImportError:
        return False
    return is_enabled()


def dispatch_governor_command(
    line: str,
    *,
    governor: Any = None,
    gate: Any = None,
) -> GovernorDispatchResult:
    """Parse ``/governor ...`` and dispatch. Tests inject collaborators."""
    if not _matches(line):
        return GovernorDispatchResult(ok=False, text="", matched=False)
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return GovernorDispatchResult(
            ok=False, text=f"  /governor parse error: {exc}",
        )
    if not tokens:
        return GovernorDispatchResult(ok=False, text="", matched=False)

    args = tokens[1:]
    head = (args[0].lower() if args else "status").strip()

    if head in ("help", "?"):
        return GovernorDispatchResult(ok=True, text=_HELP)

    # Memory subcommand has its own gate
    if head == "memory":
        if not _memory_gate_enabled():
            return GovernorDispatchResult(
                ok=False,
                text=(
                    "  /governor memory: MemoryPressureGate disabled — "
                    "set JARVIS_MEMORY_PRESSURE_GATE_ENABLED=true"
                ),
            )
        resolved_gate = gate
        if resolved_gate is None:
            try:
                from backend.core.ouroboros.governance.memory_pressure_gate import (
                    get_default_gate,
                )
                resolved_gate = get_default_gate()
            except ImportError:
                return GovernorDispatchResult(
                    ok=False, text="  /governor memory: gate unavailable",
                )
        return _render_memory(resolved_gate)

    # All other subcommands require the SensorGovernor master flag
    if not _governor_enabled():
        return GovernorDispatchResult(
            ok=False,
            text=(
                "  /governor: SensorGovernor disabled — "
                "set JARVIS_SENSOR_GOVERNOR_ENABLED=true"
            ),
        )

    resolved_gov = governor
    if resolved_gov is None:
        try:
            from backend.core.ouroboros.governance.sensor_governor import (
                ensure_seeded,
            )
            resolved_gov = ensure_seeded()
        except ImportError:
            return GovernorDispatchResult(
                ok=False, text="  /governor: governor unavailable",
            )

    if head == "status":
        return _render_status(resolved_gov)
    if head == "explain":
        return _render_explain(resolved_gov)
    if head == "history":
        limit = 10
        if len(args) >= 2:
            try:
                limit = max(1, min(512, int(args[1])))
            except (TypeError, ValueError):
                return GovernorDispatchResult(
                    ok=False,
                    text=f"  /governor history: invalid N {args[1]!r}",
                )
        return _render_history(resolved_gov, limit)
    if head == "reset":
        return _render_reset(resolved_gov)

    return GovernorDispatchResult(
        ok=False,
        text=f"  /governor: unknown subcommand {head!r}. Try /governor help.",
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _render_status(governor: Any) -> GovernorDispatchResult:
    snap = governor.snapshot()
    if not snap.get("enabled"):
        return GovernorDispatchResult(
            ok=True,
            text="  /governor: disabled (no state to show)",
        )
    lines = [
        f"  Posture: {snap.get('posture') or '(unknown)'}  "
        f"emergency_brake: {snap.get('emergency_brake')}",
        f"  Global: {snap['global']['count']}/{snap['global']['cap']} "
        f"(remaining {snap['global']['remaining']})  "
        f"window_s={snap['window_s']}",
        "",
        f"  Sensors ({len(snap['sensors'])}):",
    ]
    for s in snap["sensors"]:
        remaining = s["remaining_standard"]
        marker = "!" if remaining <= 0 else " "
        lines.append(
            f"  {marker} {s['sensor_name']:<28s} "
            f"{s['current_count']:>4d}/{s['weighted_cap_standard']:>4d}  "
            f"(base={s['base_cap_per_hour']:>3d} × "
            f"weight={s['posture_weight']:.2f})"
        )
    return GovernorDispatchResult(ok=True, text="\n".join(lines))


def _render_explain(governor: Any) -> GovernorDispatchResult:
    snap = governor.snapshot()
    if not snap.get("enabled"):
        return GovernorDispatchResult(
            ok=True,
            text="  /governor: disabled (no state to show)",
        )
    lines = [
        "  Governor state:",
        f"    schema_version     : {snap['schema_version']}",
        f"    posture            : {snap.get('posture')}",
        f"    emergency_brake    : {snap['emergency_brake']}",
        f"    window_s           : {snap['window_s']}",
        f"    decisions_count    : {snap['decisions_count']}",
    ]
    et = snap["emergency_thresholds"]
    lines.append(
        f"    brake thresholds   : cost_burn>{et['cost_burn']} OR "
        f"postmortem>{et['postmortem_rate']}  "
        f"(reduction ×{et['reduction_pct']})"
    )
    lines.append("")
    lines.append("  Sensor budgets (STANDARD urgency):")
    for s in snap["sensors"]:
        lines.append(
            f"    {s['sensor_name']:<28s} "
            f"base={s['base_cap_per_hour']:>3d} × "
            f"posture_weight={s['posture_weight']:.2f} → "
            f"cap={s['weighted_cap_standard']:>4d}  "
            f"(used {s['current_count']}, remaining {s['remaining_standard']})"
        )
    lines.append("")
    lines.append(
        f"  Global: {snap['global']['count']}/{snap['global']['cap']}  "
        f"(remaining {snap['global']['remaining']})"
    )
    return GovernorDispatchResult(ok=True, text="\n".join(lines))


def _render_history(governor: Any, limit: int) -> GovernorDispatchResult:
    recents = governor.recent_decisions(limit=limit)
    if not recents:
        return GovernorDispatchResult(
            ok=True, text="  (no recent budget decisions)",
        )
    lines = [f"  Last {len(recents)} decision(s):"]
    for d in recents:
        mark = "ALLOW" if d.allowed else "DENY "
        brake = " BRAKE" if d.emergency_brake else ""
        lines.append(
            f"    [{mark}] {d.sensor_name:<28s} "
            f"{d.urgency.value:<11s} "
            f"posture={d.posture or '-':<13s} "
            f"cap={d.weighted_cap:>4d} "
            f"used={d.current_count:>4d} "
            f"reason={d.reason_code}{brake}"
        )
    return GovernorDispatchResult(ok=True, text="\n".join(lines))


def _render_reset(governor: Any) -> GovernorDispatchResult:
    try:
        governor.reset()
    except Exception as exc:  # noqa: BLE001
        return GovernorDispatchResult(
            ok=False, text=f"  /governor reset failed: {exc!r}",
        )
    logger.warning(
        "[GovernorREPL] operator reset counters at %s", time.time(),
    )
    return GovernorDispatchResult(
        ok=True,
        text="  Governor counters cleared (rolling window reset).",
    )


def _render_memory(gate: Any) -> GovernorDispatchResult:
    snap = gate.snapshot()
    if not snap.get("enabled"):
        return GovernorDispatchResult(
            ok=True, text="  /governor memory: gate disabled",
        )
    probe = snap.get("probe", {})
    lines = [
        f"  Memory pressure: {snap['level']}",
        f"  Probe source   : {probe.get('source')}",
        f"  free_pct       : {probe.get('free_pct', 0):.1f}%",
        f"  total          : {probe.get('total_bytes', 0) // (1024**3)}GiB",
    ]
    thresholds = snap.get("thresholds", {})
    lines.append(
        f"  Thresholds     : WARN<{thresholds.get('warn_pct')}% "
        f"HIGH<{thresholds.get('high_pct')}% "
        f"CRITICAL<{thresholds.get('critical_pct')}%"
    )
    caps = snap.get("fanout_caps", {})
    lines.append(
        f"  Fanout caps    : WARN={caps.get('warn')} "
        f"HIGH={caps.get('high')} CRITICAL={caps.get('critical')}"
    )
    lines.append("")
    lines.append("  Fanout projection (can_fanout at each N):")
    for n in (1, 3, 8, 16):
        d = gate.can_fanout(n)
        lines.append(
            f"    n={n:>2d}  →  allowed={d.n_allowed}  "
            f"level={d.level.value}  reason={d.reason_code}"
        )
    return GovernorDispatchResult(ok=True, text="\n".join(lines))


__all__ = [
    "GovernorDispatchResult",
    "dispatch_governor_command",
]
