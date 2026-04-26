"""/posture REPL dispatcher — Slice 3 of the DirectionInferrer arc.

Operator verbs for inspecting and overriding the inferred strategic
posture:

    /posture                    status (same as /posture status)
    /posture status             current posture + top 3 contributors
    /posture explain            full 12-signal contribution table
    /posture history [N]        last N readings (default 20)
    /posture signals            raw signal values (no scoring)
    /posture override <POSTURE> [--until <duration>] [--reason <text>]
    /posture clear-override     drop active override
    /posture help               verb surface

Authority posture
-----------------

* §1 read-only + human-scoped — the REPL never mutates gate state,
  risk tiers, approvals, or orchestrator FSM. ``override`` is the ONLY
  write-surface and it flows exclusively into OverrideState +
  PostureStore.append_audit. It does NOT bypass Iron Gate,
  SemanticGuardian, or risk-tier enforcement.
* §8 observability — every override writes to
  ``.jarvis/posture_audit.jsonl`` (dedicated append-only file per
  Slice 2 store design) regardless of whether the current-file write
  is later masked by a new inference cycle.
* No imports from orchestrator / policy / iron_gate / risk_tier /
  change_engine / candidate_generator / gate. Grep-pinned at Slice 4.

Rendering
---------

``status`` / ``explain`` / ``signals`` use rich tables when a TTY is
attached, fall back to flat text otherwise — same pattern as
``stream_renderer.py`` and ``diff_preview.py``. All rendering logic is
pure (no subprocess / no network); ``rich`` is a soft dependency with
``_Flat`` fallback.
"""
from __future__ import annotations

import logging
import shlex
import sys
import textwrap
import time
from dataclasses import dataclass
from typing import Any, List, Optional

logger = logging.getLogger("Ouroboros.PostureREPL")


_COMMANDS = frozenset({"/posture"})


_HELP = textwrap.dedent(
    """
    Strategic posture — inferred disposition of the organism
    --------------------------------------------------------
      /posture                         current status (alias for 'status')
      /posture status                  posture + confidence + top signals
      /posture explain                 full 12-signal contribution table
      /posture history [N]             last N readings (default 20, max 256)
      /posture signals                 raw signal values (diagnostic)
      /posture override <POSTURE>      mask inference with operator choice
          [--until <duration>] [--reason <text>]
          durations: e.g. 30m, 2h, 24h (clamped to JARVIS_POSTURE_OVERRIDE_MAX_H)
      /posture clear-override          drop active override
      /posture help                    this text

    Postures: EXPLORE, CONSOLIDATE, HARDEN, MAINTAIN
    Requires JARVIS_DIRECTION_INFERRER_ENABLED=true.
    """
).strip()


@dataclass
class PostureDispatchResult:
    ok: bool
    text: str
    matched: bool = True


# ---------------------------------------------------------------------------
# Module-level store / override-state providers — tests inject via param;
# production wires singletons from posture_observer at boot.
# ---------------------------------------------------------------------------


_default_store: Optional[Any] = None
_default_override_state: Optional[Any] = None


def set_default_store(store: Any) -> None:
    global _default_store
    _default_store = store


def set_default_override_state(override_state: Any) -> None:
    global _default_override_state
    _default_override_state = override_state


def reset_default_providers() -> None:
    global _default_store, _default_override_state
    _default_store = None
    _default_override_state = None


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _matches(line: str) -> bool:
    if not line:
        return False
    first = line.split(None, 1)[0]
    return first in _COMMANDS


def _master_enabled() -> bool:
    # Late import so this module stays authority-free — the check is
    # advisory only (REPL surface opt-out).
    try:
        from backend.core.ouroboros.governance.direction_inferrer import (
            is_enabled,
        )
    except ImportError:
        return False
    return is_enabled()


def dispatch_posture_command(
    line: str,
    *,
    store: Optional[Any] = None,
    override_state: Optional[Any] = None,
    audit_sink: Optional[Any] = None,
) -> PostureDispatchResult:
    """Parse a ``/posture`` line and dispatch.

    Tests inject collaborators explicitly; production wires defaults.
    ``audit_sink`` is optional — when ``None`` the handler falls back
    to the ``store.append_audit`` method.
    """
    if not _matches(line):
        return PostureDispatchResult(ok=False, text="", matched=False)
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return PostureDispatchResult(
            ok=False, text=f"  /posture parse error: {exc}",
        )
    if not tokens:
        return PostureDispatchResult(ok=False, text="", matched=False)
    args = tokens[1:]
    head = (args[0].lower() if args else "status")

    resolved_store = store if store is not None else _default_store
    resolved_ov = override_state if override_state is not None else _default_override_state

    if head in ("help", "?"):
        return PostureDispatchResult(ok=True, text=_HELP)
    if not _master_enabled():
        return PostureDispatchResult(
            ok=False,
            text=(
                "  /posture: DirectionInferrer disabled — set "
                "JARVIS_DIRECTION_INFERRER_ENABLED=true"
            ),
        )
    if resolved_store is None:
        return PostureDispatchResult(
            ok=False,
            text=(
                "  /posture: no PostureStore attached — call "
                "set_default_store() at boot"
            ),
        )

    if head == "status":
        return _status(resolved_store, resolved_ov)
    if head == "explain":
        return _explain(resolved_store, resolved_ov)
    if head == "history":
        limit = 20
        if len(args) >= 2:
            try:
                limit = max(1, min(256, int(args[1])))
            except (TypeError, ValueError):
                return PostureDispatchResult(
                    ok=False, text=f"  /posture history: invalid N {args[1]!r}",
                )
        return _history(resolved_store, limit)
    if head == "signals":
        return _signals(resolved_store)
    if head == "override":
        return _override(args[1:], resolved_store, resolved_ov, audit_sink)
    if head == "clear-override":
        return _clear_override(resolved_store, resolved_ov, audit_sink)
    return PostureDispatchResult(
        ok=False,
        text=f"  /posture: unknown subcommand {head!r}. Try /posture help.",
    )


# ---------------------------------------------------------------------------
# Rendering helpers — rich when available, flat fallback otherwise
# ---------------------------------------------------------------------------


def _is_tty() -> bool:
    try:
        return bool(sys.stdout.isatty())
    except Exception:  # noqa: BLE001
        return False


def _parse_duration(s: str) -> Optional[float]:
    """Parse ``30m`` / ``2h`` / ``3600s`` into seconds. Returns None on error."""
    s = s.strip().lower()
    if not s:
        return None
    unit = s[-1]
    try:
        if unit == "s":
            return float(s[:-1])
        if unit == "m":
            return float(s[:-1]) * 60.0
        if unit == "h":
            return float(s[:-1]) * 3600.0
        return float(s)  # bare number → seconds
    except (TypeError, ValueError):
        return None


def _format_since(reading_at: float) -> str:
    delta = max(0, int(time.time() - reading_at))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _render_override_banner(override_state: Optional[Any]) -> str:
    if override_state is None:
        return ""
    try:
        active = override_state.active_posture()
    except Exception:
        return ""
    if active is None:
        return ""
    try:
        snap = override_state.snapshot()
    except Exception:
        return "  ⚠ OVERRIDE ACTIVE"
    until = snap.get("until") or 0.0
    remaining = max(0, int(until - time.time()))
    reason = snap.get("reason") or "(no reason)"
    return (
        f"  ⚠ OVERRIDE ACTIVE: {active.value} "
        f"({remaining}s left, reason: {reason})"
    )


def _status(store: Any, override_state: Optional[Any]) -> PostureDispatchResult:
    reading = store.load_current()
    if reading is None:
        return PostureDispatchResult(
            ok=True,
            text="  /posture: no current reading yet — observer hasn't cycled.",
        )
    lines = [
        f"  Posture: {reading.posture.value} "
        f"(confidence {reading.confidence:.2f})",
        f"  Inferred: {_format_since(reading.inferred_at)} "
        f"(hash {reading.signal_bundle_hash})",
    ]
    meaningful = [
        c for c in reading.evidence if abs(c.contribution_score) > 1e-6
    ]
    if meaningful:
        lines.append("  Top contributors:")
        for c in meaningful[:3]:
            lines.append(
                f"    {c.signal_name:32s} "
                f"raw={c.raw_value:.2f}  contrib={c.contribution_score:+.3f}"
            )
    else:
        lines.append("  (baseline state — no strong signals)")

    banner = _render_override_banner(override_state)
    if banner:
        lines.append(banner)

    return PostureDispatchResult(ok=True, text="\n".join(lines))


def _explain(store: Any, override_state: Optional[Any]) -> PostureDispatchResult:
    reading = store.load_current()
    if reading is None:
        return PostureDispatchResult(
            ok=True, text="  /posture: no current reading to explain.",
        )

    # Try rich; fall back to flat
    try:
        from rich.console import Console
        from rich.table import Table
        from io import StringIO

        if _is_tty():
            buf = StringIO()
            console = Console(file=buf, force_terminal=False, width=100)
            table = Table(
                title=f"Posture={reading.posture.value} "
                      f"(conf={reading.confidence:.2f})",
                show_lines=False,
            )
            table.add_column("Signal", style="cyan", no_wrap=True)
            table.add_column("Raw", justify="right")
            table.add_column("Norm", justify="right")
            table.add_column("Weight", justify="right")
            table.add_column("Contrib", justify="right")
            for c in reading.evidence:
                table.add_row(
                    c.signal_name,
                    f"{c.raw_value:.3f}",
                    f"{c.normalized:.3f}",
                    f"{c.weight:+.2f}",
                    f"{c.contribution_score:+.4f}",
                )
            console.print(table)
            body = buf.getvalue()
        else:
            raise ImportError  # force flat path
    except ImportError:
        # Flat fallback
        body_lines = [
            f"  Posture={reading.posture.value} (conf={reading.confidence:.2f})",
            f"  {'Signal':<32s} {'Raw':>8s} {'Norm':>8s} {'Weight':>8s} {'Contrib':>10s}",
        ]
        for c in reading.evidence:
            body_lines.append(
                f"  {c.signal_name:<32s} "
                f"{c.raw_value:>8.3f} {c.normalized:>8.3f} "
                f"{c.weight:>+8.2f} {c.contribution_score:>+10.4f}"
            )
        body_lines.append("")
        body_lines.append("  All-posture scores:")
        for p, score in reading.all_scores:
            body_lines.append(f"    {p.value:13s} = {score:+.4f}")
        body = "\n".join(body_lines)

    arc_section = _render_arc_context_section(reading)
    if arc_section:
        body = body.rstrip() + "\n\n" + arc_section

    banner = _render_override_banner(override_state)
    if banner:
        body = body.rstrip() + "\n" + banner
    return PostureDispatchResult(ok=True, text=body)


def _render_arc_context_section(reading: Any) -> str:
    """P0.5 Slice 3 — render the arc-context block on `/posture explain`.

    Empty string when the reading carries no arc context (back-compat with
    pre-Slice-2 readings that may persist across upgrades). Always plain-
    text, never raises — observability surface for the operator to verify
    cross-session direction memory in production.
    """
    arc = getattr(reading, "arc_context", None)
    if arc is None:
        return ""

    try:
        from backend.core.ouroboros.governance.direction_inferrer import (
            arc_context_enabled,
        )
        applied = arc_context_enabled()
    except Exception:
        applied = False

    lines = ["  Arc Context (P0.5 — Cross-Session Direction Memory):"]
    lines.append(
        f"    Status: {'APPLIED to scores' if applied else 'OBSERVED ONLY (flag off)'}"
    )

    momentum = getattr(arc, "momentum", None)
    if momentum is not None and not momentum.is_empty():
        top_scopes = momentum.top_scopes(3)
        top_types = momentum.top_types(4)
        lines.append(f"    Momentum: {momentum.commit_count} commits parsed")
        if top_scopes:
            scope_str = ", ".join(f"{s}({c})" for s, c in top_scopes)
            lines.append(f"      top scopes: {scope_str}")
        if top_types:
            type_str = ", ".join(f"{t}={c}" for t, c in top_types)
            lines.append(f"      top types:  {type_str}")
    else:
        lines.append("    Momentum: (no git history available)")

    verify_ratio = getattr(arc, "lss_verify_ratio", None)
    apply_count = getattr(arc, "lss_apply_count", None)
    apply_mode = getattr(arc, "lss_apply_mode", None)
    if verify_ratio is not None or apply_count is not None:
        bits = []
        if apply_mode is not None and apply_count is not None:
            bits.append(f"apply={apply_mode}/{apply_count}")
        if verify_ratio is not None:
            bits.append(f"verify_ratio={verify_ratio:.2f}")
        lines.append(f"    LastSession: {'  '.join(bits) if bits else '(no signal)'}")
    else:
        lines.append("    LastSession: (no summary available)")

    try:
        nudges = arc.suggest_nudge()
    except Exception:
        nudges = {}
    if nudges:
        lines.append("    Per-posture score nudge (bounded ≤ 0.10):")
        for posture, nudge in sorted(
            nudges.items(), key=lambda kv: -kv[1]
        ):
            marker = "applied" if applied and nudge > 0 else "      "
            lines.append(
                f"      {posture.value:<12s}  +{nudge:.4f}  {marker}"
            )

    return "\n".join(lines)


def _history(store: Any, limit: int) -> PostureDispatchResult:
    readings = store.load_history(limit=limit)
    if not readings:
        return PostureDispatchResult(
            ok=True, text="  (no posture history yet)",
        )
    lines = [f"  Last {len(readings)} posture reading(s):"]
    for r in readings:
        lines.append(
            f"    {_format_since(r.inferred_at):>10s}  "
            f"{r.posture.value:<13s}  "
            f"conf={r.confidence:.2f}  hash={r.signal_bundle_hash}"
        )
    return PostureDispatchResult(ok=True, text="\n".join(lines))


def _signals(store: Any) -> PostureDispatchResult:
    reading = store.load_current()
    if reading is None:
        return PostureDispatchResult(
            ok=True, text="  /posture: no current reading.",
        )
    lines = ["  Current signal raw values:"]
    for c in reading.evidence:
        lines.append(f"    {c.signal_name:<32s} = {c.raw_value:.4f}")
    return PostureDispatchResult(ok=True, text="\n".join(lines))


def _override(
    args: List[str],
    store: Any,
    override_state: Optional[Any],
    audit_sink: Optional[Any],
) -> PostureDispatchResult:
    if override_state is None:
        return PostureDispatchResult(
            ok=False,
            text=(
                "  /posture override: no OverrideState attached — "
                "call set_default_override_state() at boot"
            ),
        )
    if not args:
        return PostureDispatchResult(
            ok=False,
            text="  /posture override <POSTURE> [--until <dur>] [--reason <text>]",
        )
    # Parse flags
    posture_raw = args[0]
    duration_s: Optional[float] = None
    reason = ""
    i = 1
    while i < len(args):
        tok = args[i]
        if tok == "--until" and i + 1 < len(args):
            duration_s = _parse_duration(args[i + 1])
            if duration_s is None:
                return PostureDispatchResult(
                    ok=False,
                    text=f"  /posture override: bad --until {args[i+1]!r}",
                )
            i += 2
        elif tok == "--reason" and i + 1 < len(args):
            reason = args[i + 1]
            i += 2
        else:
            return PostureDispatchResult(
                ok=False, text=f"  /posture override: unknown flag {tok!r}",
            )

    try:
        from backend.core.ouroboros.governance.posture import Posture
        posture = Posture.from_str(posture_raw)
    except (ImportError, ValueError) as exc:
        return PostureDispatchResult(
            ok=False, text=f"  /posture override: {exc}",
        )

    try:
        from backend.core.ouroboros.governance.posture_observer import (
            override_max_h,
        )
        max_s = override_max_h() * 3600
    except ImportError:
        max_s = 24 * 3600

    if duration_s is None:
        duration_s = max_s  # default to full allowed window
    if duration_s > max_s:
        clamped = True
        duration_s = float(max_s)
    else:
        clamped = False

    try:
        set_at, until = override_state.set(
            posture, duration_s=duration_s, reason=reason or "(no reason)",
        )
    except Exception as exc:  # noqa: BLE001
        return PostureDispatchResult(
            ok=False, text=f"  /posture override failed: {exc!r}",
        )

    # Emit audit record
    try:
        from backend.core.ouroboros.governance.posture_store import (
            OverrideRecord,
        )
        rec = OverrideRecord(
            event="set", posture=posture, who="user",
            at=set_at, until=until, reason=reason or "(no reason)",
        )
        sink = audit_sink if audit_sink is not None else store
        sink.append_audit(rec)
    except Exception:
        logger.debug("[PostureREPL] audit append failed", exc_info=True)

    # Best-effort SSE publish — never raises into the REPL path
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (
            publish_posture_event,
        )
        publish_posture_event(
            "override_set",
            reading=store.load_current(),
            extra={
                "override_posture": posture.value,
                "override_until": until,
                "override_reason": reason or "(no reason)",
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("[PostureREPL] SSE publish failed", exc_info=True)

    remaining = max(0, int(until - time.time()))
    msg = (
        f"  Override set: posture={posture.value} "
        f"for {remaining}s (until epoch {until:.0f})"
    )
    if clamped:
        msg += f" [clamped to max {max_s}s]"
    if reason:
        msg += f"\n  Reason: {reason}"
    return PostureDispatchResult(ok=True, text=msg)


def _clear_override(
    store: Any,
    override_state: Optional[Any],
    audit_sink: Optional[Any],
) -> PostureDispatchResult:
    if override_state is None:
        return PostureDispatchResult(
            ok=False, text="  /posture clear-override: no OverrideState attached",
        )
    try:
        active = override_state.active_posture()
    except Exception:
        active = None
    if active is None:
        return PostureDispatchResult(
            ok=True, text="  /posture: no active override to clear.",
        )
    snap = override_state.snapshot()
    override_state.clear()

    # Audit record for the clear
    try:
        from backend.core.ouroboros.governance.posture_store import (
            OverrideRecord,
        )
        rec = OverrideRecord(
            event="clear",
            posture=active,
            who="user",
            at=time.time(),
            until=snap.get("until"),
            reason=snap.get("reason", ""),
        )
        sink = audit_sink if audit_sink is not None else store
        sink.append_audit(rec)
    except Exception:
        logger.debug("[PostureREPL] clear audit append failed", exc_info=True)

    # Best-effort SSE publish — never raises into the REPL path
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (
            publish_posture_event,
        )
        publish_posture_event(
            "override_cleared",
            reading=store.load_current(),
            extra={"previous_override": active.value},
        )
    except Exception:  # noqa: BLE001
        logger.debug("[PostureREPL] SSE publish failed", exc_info=True)

    return PostureDispatchResult(
        ok=True, text=f"  Override cleared (was {active.value}).",
    )


__all__ = [
    "PostureDispatchResult",
    "dispatch_posture_command",
    "reset_default_providers",
    "set_default_override_state",
    "set_default_store",
]
