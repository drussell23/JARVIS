"""
/cost REPL dispatcher — Slice 4 of the Per-Phase Cost Drill-Down arc.
======================================================================

Operator verbs for answering *"why did this op cost $0.80?"* at
runtime:

    /cost                       session-wide phase rollup (live ops
                                + current session's historical data)
    /cost <op-id>               per-phase breakdown for one live op
                                (reads CostGovernor directly)
    /cost session <sid>         per-phase breakdown from a historical
                                session (reads summary.json via the
                                session browser)
    /cost help                  the verb surface

Authority posture
-----------------

* §1 read-only — the REPL never mutates governor state. Drill-downs
  are pure projections.
* §8 observable — every verb returns a bounded text payload matching
  the render helpers in :mod:`phase_cost`.
* The dispatcher does NOT import orchestrator / policy / iron_gate /
  risk_tier_floor / semantic_guardian / tool_executor /
  candidate_generator / change_engine — grep-pinned at graduation.
"""
from __future__ import annotations

import logging
import shlex
import textwrap
from dataclasses import dataclass
from typing import Any, List, Optional

logger = logging.getLogger("Ouroboros.CostREPL")


_COMMANDS = frozenset({"/cost"})

_HELP = textwrap.dedent(
    """
    Per-phase cost drill-down
    --------------------------
      /cost                        session-wide phase rollup
      /cost <op-id>                per-phase breakdown for a live op
      /cost session <session-id>   historical breakdown from a past session
      /cost help                   this text

    Tip: "why did this op cost $X?" -> /cost <op-id>.
    """
).strip()


@dataclass
class CostDispatchResult:
    ok: bool
    text: str
    matched: bool = True


# ---------------------------------------------------------------------------
# Governor resolution
# ---------------------------------------------------------------------------


# Module-level hook — production wiring (harness / battle_test) sets
# this to the live CostGovernor at boot. Tests that need a governor
# either inject one explicitly or call set_default_governor().
_default_governor: Optional[Any] = None


def set_default_governor(governor: Optional[Any]) -> None:
    """Install the process-wide default CostGovernor for ``/cost`` REPL
    queries. Called by the harness after it constructs the governor."""
    global _default_governor
    _default_governor = governor


def reset_default_governor() -> None:
    """Test helper — drop the default governor reference."""
    global _default_governor
    _default_governor = None


def _resolve_governor(explicit: Optional[Any]) -> Optional[Any]:
    """Return the governor to query — explicit argument beats the
    process-level default. ``None`` is valid: handlers emit a graceful
    "no governor attached" message rather than raise.
    """
    if explicit is not None:
        return explicit
    return _default_governor


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _matches(line: str) -> bool:
    if not line:
        return False
    first = line.split(None, 1)[0]
    return first in _COMMANDS


def dispatch_cost_command(
    line: str,
    *,
    governor: Optional[Any] = None,
    session_browser: Optional[Any] = None,
) -> CostDispatchResult:
    """Parse a ``/cost`` REPL line and return the rendered result.

    Tests can inject an explicit ``governor`` and/or ``session_browser``
    without touching the module singletons.
    """
    if not _matches(line):
        return CostDispatchResult(ok=False, text="", matched=False)
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return CostDispatchResult(
            ok=False, text=f"  /cost parse error: {exc}",
        )
    if not tokens:
        return CostDispatchResult(ok=False, text="", matched=False)
    args = tokens[1:]
    if not args:
        return _cost_session_rollup(governor)
    head = args[0].lower()
    if head in ("help", "?"):
        return CostDispatchResult(ok=True, text=_HELP)
    if head == "session":
        if len(args) < 2:
            return CostDispatchResult(
                ok=False, text="  /cost session <session-id>",
            )
        return _cost_historical(args[1], browser=session_browser)
    # Short form: /cost <op-id> (no verb keyword)
    return _cost_live_op(args[0], governor=governor)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _cost_live_op(
    op_id: str, *, governor: Optional[Any],
) -> CostDispatchResult:
    """Drill-down for a live op via CostGovernor.get_phase_breakdown."""
    g = _resolve_governor(governor)
    if g is None:
        return CostDispatchResult(
            ok=False,
            text="  /cost: no CostGovernor attached; try /cost session <sid>",
        )
    try:
        breakdown = g.get_phase_breakdown(op_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[CostREPL] get_phase_breakdown raised: %s", exc,
        )
        return CostDispatchResult(
            ok=False, text=f"  /cost: governor error: {exc!r}",
        )
    if breakdown is None:
        return CostDispatchResult(
            ok=False,
            text=(
                f"  /cost: no live data for {op_id}  "
                "(op may have finished — try /cost session <sid>)"
            ),
        )
    from backend.core.ouroboros.governance.phase_cost import (
        render_phase_cost_breakdown,
    )
    return CostDispatchResult(
        ok=True, text=render_phase_cost_breakdown(breakdown),
    )


def _cost_session_rollup(
    governor: Optional[Any],
) -> CostDispatchResult:
    """Session-wide rollup: sum every live op's phase breakdown."""
    g = _resolve_governor(governor)
    if g is None:
        return CostDispatchResult(
            ok=True,
            text="  (no CostGovernor attached — nothing to summarize)",
        )
    try:
        all_breakdowns = g.snapshot_all_phase_breakdowns()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[CostREPL] snapshot raised: %s", exc)
        return CostDispatchResult(
            ok=False, text=f"  /cost: governor error: {exc!r}",
        )
    if not all_breakdowns:
        return CostDispatchResult(
            ok=True, text="  (no live ops currently tracked)",
        )
    # Roll up every op's by_phase into one dict.
    total = 0.0
    rollup: dict = {}
    for op_id, b in all_breakdowns.items():
        if b is None:
            continue
        total += b.total_usd
        for phase, usd in b.by_phase.items():
            rollup[phase] = rollup.get(phase, 0.0) + usd
    lines: List[str] = [
        f"  Session cost (live ops: {len(all_breakdowns)})",
        f"    total: ${total:.4f}",
    ]
    if rollup:
        from backend.core.ouroboros.governance.phase_cost import (
            _phase_sort_key,
        )
        lines.append("    by phase:")
        for phase in sorted(rollup.keys(), key=_phase_sort_key):
            lines.append(f"      {phase:<18} ${rollup[phase]:.4f}")
    lines.append("    ops:")
    for op_id, b in all_breakdowns.items():
        if b is None or not b.has_data:
            continue
        top = b.top_phase()
        top_str = (
            f"  top={top[0]} ${top[1]:.4f}" if top else ""
        )
        lines.append(
            f"      {op_id}  ${b.total_usd:.4f}  "
            f"({b.call_count} calls){top_str}"
        )
    return CostDispatchResult(ok=True, text="\n".join(lines))


def _cost_historical(
    session_id: str, *, browser: Optional[Any],
) -> CostDispatchResult:
    """Historical per-phase drill-down from a past session's
    summary.json — read via the session browser."""
    if browser is None:
        # Late import — mirrors the governor lookup pattern.
        try:
            from backend.core.ouroboros.governance.session_browser import (
                get_default_session_browser,
            )
            browser = get_default_session_browser()
        except Exception:  # noqa: BLE001
            return CostDispatchResult(
                ok=False,
                text=(
                    "  /cost session: session browser unavailable"
                ),
            )
    try:
        rec = browser.show(session_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[CostREPL] browser.show raised: %s", exc)
        return CostDispatchResult(
            ok=False, text=f"  /cost session: browser error: {exc!r}",
        )
    if rec is None:
        return CostDispatchResult(
            ok=False,
            text=f"  /cost session: unknown session id {session_id}",
        )
    if not rec.cost_by_phase and not rec.cost_by_op_phase:
        return CostDispatchResult(
            ok=True,
            text=(
                f"  /cost session {session_id}: "
                f"no per-phase cost data recorded"
            ),
        )
    from backend.core.ouroboros.governance.phase_cost import (
        _phase_sort_key,
    )
    lines: List[str] = [f"  Session {session_id}"]
    if rec.cost_by_phase:
        total = sum(rec.cost_by_phase.values())
        lines.append(f"    total: ${total:.4f}")
        lines.append("    by phase:")
        for phase in sorted(
            rec.cost_by_phase.keys(), key=_phase_sort_key,
        ):
            lines.append(
                f"      {phase:<18} ${rec.cost_by_phase[phase]:.4f}"
            )
    if rec.cost_by_op_phase:
        lines.append("    by op:")
        for op_id, phases in rec.cost_by_op_phase.items():
            op_total = sum(phases.values())
            lines.append(
                f"      {op_id}  ${op_total:.4f}  ({len(phases)} phases)"
            )
            for phase in sorted(phases.keys(), key=_phase_sort_key):
                lines.append(
                    f"        {phase:<16} ${phases[phase]:.4f}"
                )
    return CostDispatchResult(ok=True, text="\n".join(lines))


__all__ = [
    "CostDispatchResult",
    "dispatch_cost_command",
    "reset_default_governor",
    "set_default_governor",
]
