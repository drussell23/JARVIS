"""
TrajectoryBuilder + Renderer + Stream + REPL — Slices 2/3/4 bundled.
=====================================================================

The composition + rendering + streaming + operator-surface layer for
the one-glance trajectory view.

Four tightly-coupled primitives:

* :class:`TrajectoryBuilder` — composes a :class:`TrajectoryFrame`
  from a set of injectable typed suppliers. Fake-friendly for tests;
  wires to real Ouroboros state in production.
* :class:`TrajectoryRenderer` — formats a frame to a target surface
  (REPL single-line / REPL expanded / IDE JSON / SSE compact).
* :class:`TrajectoryStream` — broadcasts frames to listeners on an
  explicit "emit" call; also supports "emit if changed" semantics to
  avoid flooding subscribers.
* :func:`dispatch_trajectory_command` — ``/trajectory`` REPL
  dispatcher.

Boundary discipline
-------------------

* §1 — suppliers return data, they DON'T carry authority. The
  builder never calls into the orchestrator; it reads.
* §5 — deterministic composition.
* §7 — supplier raising / returning junk → frame field becomes the
  "unknown" sentinel. Never crashes.
* §8 — every emitted frame carries a monotonic sequence and goes
  through listener hooks.
"""
from __future__ import annotations

import enum
import json
import logging
import shlex
import textwrap
import threading
import time
from dataclasses import dataclass, field
from typing import (
    Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Protocol, Tuple,
    runtime_checkable,
)

from backend.core.ouroboros.governance.trajectory_frame import (
    Confidence,
    TRAJECTORY_FRAME_SCHEMA_VERSION,
    TrajectoryFrame,
    TrajectoryPhase,
    idle_frame,
    phase_from_raw,
)

logger = logging.getLogger("Ouroboros.TrajectoryView")


TRAJECTORY_VIEW_SCHEMA_VERSION: str = "trajectory_view.v1"


# ---------------------------------------------------------------------------
# Supplier protocols — inject real-system adapters in production; fakes in tests
# ---------------------------------------------------------------------------


@runtime_checkable
class OpStateSupplier(Protocol):
    """Tells the builder which op is active right now."""

    def current_op(self) -> Optional[Dict[str, Any]]:
        """Return ``{"op_id", "raw_phase", "subject", "started_at_ts",
        "target_paths", "active_tools", "trigger_source",
        "trigger_reason", "is_blocked", "blocked_reason", "next_step"}``
        or ``None`` if idle."""
        ...


@runtime_checkable
class CostSupplier(Protocol):
    def cost_snapshot(self, op_id: str) -> Optional[Dict[str, Any]]:
        """``{"spent_usd": float, "budget_usd": Optional[float]}`` or None."""
        ...


@runtime_checkable
class EtaSupplier(Protocol):
    def eta_for(self, op_id: str) -> Optional[Dict[str, Any]]:
        """``{"eta_seconds": float, "deadline_at_ts": float, "confidence": float}``
        or None."""
        ...


@runtime_checkable
class SensorTriggerSupplier(Protocol):
    def trigger_for(self, op_id: str) -> Optional[Dict[str, Any]]:
        """``{"source": str, "reason": str}`` or None."""
        ...


# A default no-op supplier set for tests / degraded environments.

class _NullOpState:
    def current_op(self) -> Optional[Dict[str, Any]]:
        return None


class _NullCost:
    def cost_snapshot(self, op_id: str) -> Optional[Dict[str, Any]]:
        _ = op_id
        return None


class _NullEta:
    def eta_for(self, op_id: str) -> Optional[Dict[str, Any]]:
        _ = op_id
        return None


class _NullSensor:
    def trigger_for(self, op_id: str) -> Optional[Dict[str, Any]]:
        _ = op_id
        return None


# ---------------------------------------------------------------------------
# TrajectoryBuilder
# ---------------------------------------------------------------------------


class TrajectoryBuilder:
    """Composes :class:`TrajectoryFrame` from injectable suppliers.

    Every supplier may return ``None`` or raise — the builder absorbs
    failures and emits a well-formed frame with "unknown" sentinels.
    Thread-safe: construction of a frame is a read-only walk.
    """

    def __init__(
        self,
        *,
        op_state: Optional[OpStateSupplier] = None,
        cost: Optional[CostSupplier] = None,
        eta: Optional[EtaSupplier] = None,
        sensor_trigger: Optional[SensorTriggerSupplier] = None,
    ) -> None:
        self._op_state = op_state or _NullOpState()
        self._cost = cost or _NullCost()
        self._eta = eta or _NullEta()
        self._sensor = sensor_trigger or _NullSensor()
        self._lock = threading.Lock()
        self._next_sequence = 1

    # --- public API -----------------------------------------------------

    def build(
        self,
        *,
        now_ts: Optional[float] = None,
    ) -> TrajectoryFrame:
        """Compose a frame describing the current moment."""
        ts = now_ts if now_ts is not None else time.time()
        seq = self._next_seq()

        op_info = self._safe_call(
            self._op_state.current_op, default=None,
        )
        if not op_info:
            return idle_frame(sequence=seq, now_ts=ts)

        op_id = str(op_info.get("op_id", "") or "")
        if not op_id:
            return idle_frame(sequence=seq, now_ts=ts)

        raw_phase = str(op_info.get("raw_phase", "") or "")
        phase = phase_from_raw(raw_phase)
        subject = str(op_info.get("subject", "") or "")
        target_paths = _as_string_tuple(op_info.get("target_paths"))
        active_tools = _as_string_tuple(op_info.get("active_tools"))
        started_at_ts = _as_float(op_info.get("started_at_ts"))
        is_blocked = bool(op_info.get("is_blocked", False))
        blocked_reason = str(op_info.get("blocked_reason", "") or "")
        next_step = str(op_info.get("next_step", "") or "")
        trigger_source = str(op_info.get("trigger_source", "") or "")
        trigger_reason = str(op_info.get("trigger_reason", "") or "")

        # Sensor supplier may override trigger_source / trigger_reason
        trig = self._safe_call(
            lambda: self._sensor.trigger_for(op_id), default=None,
        )
        if trig:
            if not trigger_source:
                trigger_source = str(trig.get("source", "") or "")
            if not trigger_reason:
                trigger_reason = str(trig.get("reason", "") or "")

        # Cost
        cost_info = self._safe_call(
            lambda: self._cost.cost_snapshot(op_id), default=None,
        )
        if cost_info:
            cost_spent = _as_float(cost_info.get("spent_usd"))
            cost_budget = cost_info.get("budget_usd")
            if cost_budget is not None:
                cost_budget = _as_float(cost_budget)
        else:
            cost_spent = 0.0
            cost_budget = None

        # ETA
        eta_info = self._safe_call(
            lambda: self._eta.eta_for(op_id), default=None,
        )
        if eta_info:
            eta_seconds = eta_info.get("eta_seconds")
            if eta_seconds is not None:
                eta_seconds = _as_float(eta_seconds)
            deadline_ts = eta_info.get("deadline_at_ts")
            confidence = eta_info.get("confidence")
            if confidence is not None:
                confidence = _as_float(confidence)
        else:
            eta_seconds = None
            deadline_ts = None
            confidence = None

        return TrajectoryFrame(
            sequence=seq,
            snapshot_at_iso=_iso_for(ts),
            snapshot_at_ts=ts,
            op_id=op_id,
            phase=phase,
            raw_phase=raw_phase,
            subject=subject,
            target_paths=target_paths,
            active_tools=active_tools,
            trigger_source=trigger_source,
            trigger_reason=trigger_reason,
            started_at_iso=_iso_for(started_at_ts) if started_at_ts else "",
            started_at_ts=started_at_ts,
            eta_seconds=eta_seconds,
            deadline_at_iso=(
                _iso_for(float(deadline_ts)) if deadline_ts else ""
            ),
            cost_spent_usd=cost_spent,
            cost_budget_usd=cost_budget,
            next_step=next_step,
            confidence=confidence,
            is_idle=False,
            is_blocked=is_blocked,
            blocked_reason=blocked_reason,
        )

    # --- helpers --------------------------------------------------------

    def _next_seq(self) -> int:
        with self._lock:
            seq = self._next_sequence
            self._next_sequence += 1
        return seq

    @staticmethod
    def _safe_call(fn: Callable[[], Any], *, default: Any) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[TrajectoryBuilder] supplier raised: %s", exc,
            )
            return default


def _as_string_tuple(value: Any) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value if v is not None)
    if isinstance(value, str):
        return (value,)
    return ()


def _as_float(value: Any) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _iso_for(ts: float) -> str:
    from datetime import datetime, timezone
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).replace(
            microsecond=0,
        ).isoformat()
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# TrajectoryRenderer — surface-specific rendering
# ---------------------------------------------------------------------------


class TrajectorySurface(str, enum.Enum):
    REPL_COMPACT = "repl_compact"   # one-line status-bar style
    REPL_EXPANDED = "repl_expanded"  # multi-line narrative + key/value
    IDE_JSON = "ide_json"           # full projection as JSON
    SSE = "sse"                      # compact projection (stream payload)
    PLAIN = "plain"                  # plain text narrative only


class TrajectoryRenderer:
    """Pure-code renderer for one frame across several surfaces."""

    def render(
        self,
        frame: TrajectoryFrame,
        *,
        surface: TrajectorySurface = TrajectorySurface.REPL_COMPACT,
    ) -> str:
        if surface is TrajectorySurface.REPL_COMPACT:
            return frame.one_line_summary()
        if surface is TrajectorySurface.PLAIN:
            return frame.narrative()
        if surface is TrajectorySurface.REPL_EXPANDED:
            return self._render_expanded(frame)
        if surface is TrajectorySurface.IDE_JSON:
            return json.dumps(
                frame.project(), indent=2, sort_keys=True, default=str,
            )
        if surface is TrajectorySurface.SSE:
            return json.dumps(
                self._sse_projection(frame), sort_keys=True, default=str,
            )
        return frame.one_line_summary()

    # --- REPL expanded --------------------------------------------------

    def _render_expanded(self, f: TrajectoryFrame) -> str:
        lines: List[str] = []
        lines.append(f.narrative())
        if not f.has_op:
            return "\n".join(lines)
        # Key/value block
        lines.append("")
        lines.append(f"  op_id       : {f.op_id}")
        lines.append(f"  phase       : {f.phase.value} (raw={f.raw_phase!r})")
        if f.subject:
            lines.append(f"  subject     : {f.subject}")
        if f.target_paths:
            lines.append(f"  paths       : {', '.join(f.target_paths)}")
        if f.active_tools:
            lines.append(f"  tools       : {', '.join(f.active_tools)}")
        if f.trigger_source:
            lines.append(
                f"  trigger     : {f.trigger_source} — {f.trigger_reason}"
            )
        if f.started_at_iso:
            lines.append(f"  started     : {f.started_at_iso}")
        if f.eta_seconds is not None:
            lines.append(f"  eta_seconds : {f.eta_seconds:.0f}")
        if f.deadline_at_iso:
            lines.append(f"  deadline    : {f.deadline_at_iso}")
        if f.cost_spent_usd or f.cost_budget_usd is not None:
            lines.append(
                f"  cost        : ${f.cost_spent_usd:.3f}"
                + (f" / ${f.cost_budget_usd:.2f}"
                   if f.cost_budget_usd is not None else "")
            )
        if f.confidence is not None:
            lines.append(
                f"  confidence  : {f.confidence:.2f} ({f.confidence_band.value})"
            )
        if f.is_blocked:
            lines.append(f"  BLOCKED     : {f.blocked_reason}")
        if f.next_step:
            lines.append(f"  next_step   : {f.next_step}")
        return "\n".join(lines)

    # --- SSE projection (smaller than full project()) -------------------

    def _sse_projection(self, f: TrajectoryFrame) -> Dict[str, Any]:
        return {
            "schema_version": TRAJECTORY_VIEW_SCHEMA_VERSION,
            "sequence": f.sequence,
            "snapshot_at_iso": f.snapshot_at_iso,
            "op_id": f.op_id,
            "phase": f.phase.value,
            "trigger_source": f.trigger_source,
            "eta_seconds": f.eta_seconds,
            "cost_spent_usd": f.cost_spent_usd,
            "cost_budget_usd": f.cost_budget_usd,
            "is_idle": f.is_idle,
            "is_blocked": f.is_blocked,
            "one_line_summary": f.one_line_summary(),
        }


# ---------------------------------------------------------------------------
# TrajectoryStream — listener hook broadcaster
# ---------------------------------------------------------------------------


TrajectoryListener = Callable[[TrajectoryFrame], None]


class TrajectoryStream:
    """Broadcast frames to subscribers.

    Two emission modes:
      * :meth:`emit` — unconditional.
      * :meth:`emit_if_changed` — compare the new frame to the last
        frame (excluding ``sequence`` and ``snapshot_at_*``); emit
        only when a presentation-relevant field differs.

    Listener exceptions are swallowed; one bad listener can't block
    the rest.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._listeners: List[TrajectoryListener] = []
        self._last_emitted: Optional[TrajectoryFrame] = None
        self._emits_total = 0

    def subscribe(self, listener: TrajectoryListener) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def _unsub() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return _unsub

    def emit(self, frame: TrajectoryFrame) -> None:
        with self._lock:
            self._last_emitted = frame
            self._emits_total += 1
            listeners = list(self._listeners)
        for l in listeners:
            try:
                l(frame)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[TrajectoryStream] listener raised: %s", exc,
                )

    def emit_if_changed(self, frame: TrajectoryFrame) -> bool:
        """Emit only when presentation-relevant fields differ.

        Returns True if emitted, False if suppressed as a duplicate.
        """
        with self._lock:
            last = self._last_emitted
        if last is not None and _frames_presentation_equal(last, frame):
            return False
        self.emit(frame)
        return True

    @property
    def last_emitted(self) -> Optional[TrajectoryFrame]:
        with self._lock:
            return self._last_emitted

    @property
    def emits_total(self) -> int:
        with self._lock:
            return self._emits_total

    def reset(self) -> None:
        with self._lock:
            self._listeners.clear()
            self._last_emitted = None
            self._emits_total = 0


def _frames_presentation_equal(
    a: TrajectoryFrame, b: TrajectoryFrame,
) -> bool:
    """Equality excluding sequence + snapshot timestamp.

    Trajectory streams suppress duplicates so subscribers don't see
    a noisy heartbeat just because the clock ticked. Two frames are
    "the same picture" when every operator-visible field matches.
    """
    return (
        a.op_id == b.op_id
        and a.phase is b.phase
        and a.target_paths == b.target_paths
        and a.active_tools == b.active_tools
        and a.trigger_source == b.trigger_source
        and a.trigger_reason == b.trigger_reason
        and a.eta_seconds == b.eta_seconds
        and a.cost_spent_usd == b.cost_spent_usd
        and a.cost_budget_usd == b.cost_budget_usd
        and a.is_idle == b.is_idle
        and a.is_blocked == b.is_blocked
        and a.blocked_reason == b.blocked_reason
        and a.next_step == b.next_step
        and a.confidence == b.confidence
    )


# ---------------------------------------------------------------------------
# Module singletons — optional; callers can construct their own
# ---------------------------------------------------------------------------


_default_builder: Optional[TrajectoryBuilder] = None
_default_renderer: Optional[TrajectoryRenderer] = None
_default_stream: Optional[TrajectoryStream] = None
_singleton_lock = threading.Lock()


def get_default_builder() -> TrajectoryBuilder:
    global _default_builder
    with _singleton_lock:
        if _default_builder is None:
            _default_builder = TrajectoryBuilder()
        return _default_builder


def get_default_renderer() -> TrajectoryRenderer:
    global _default_renderer
    with _singleton_lock:
        if _default_renderer is None:
            _default_renderer = TrajectoryRenderer()
        return _default_renderer


def get_default_stream() -> TrajectoryStream:
    global _default_stream
    with _singleton_lock:
        if _default_stream is None:
            _default_stream = TrajectoryStream()
        return _default_stream


def reset_default_trajectory_singletons() -> None:
    global _default_builder, _default_renderer, _default_stream
    with _singleton_lock:
        if _default_stream is not None:
            _default_stream.reset()
        _default_builder = None
        _default_renderer = None
        _default_stream = None


def set_default_suppliers(
    *,
    op_state: Optional[OpStateSupplier] = None,
    cost: Optional[CostSupplier] = None,
    eta: Optional[EtaSupplier] = None,
    sensor_trigger: Optional[SensorTriggerSupplier] = None,
) -> TrajectoryBuilder:
    """Replace the default builder with one wired to new suppliers.

    Returns the new builder so callers can keep a handle. The
    replaced builder's sequence counter restarts at 1.
    """
    global _default_builder
    with _singleton_lock:
        _default_builder = TrajectoryBuilder(
            op_state=op_state, cost=cost, eta=eta,
            sensor_trigger=sensor_trigger,
        )
        return _default_builder


# ---------------------------------------------------------------------------
# /trajectory REPL dispatcher
# ---------------------------------------------------------------------------


@dataclass
class TrajectoryDispatchResult:
    ok: bool
    text: str
    matched: bool = True


_TRAJECTORY_HELP = textwrap.dedent(
    """
    Operator trajectory view
    ------------------------
      /trajectory                    — one-line summary
      /trajectory status             — same as above
      /trajectory expanded           — multi-line narrative + fields
      /trajectory json               — full JSON projection
      /trajectory sse                — compact SSE-style projection
      /trajectory watch [interval]   — (reserved — Slice 5 live stream)
      /trajectory help               — this text
    """
).strip()


_COMMANDS = frozenset({"/trajectory"})


def _matches(line: str) -> bool:
    if not line:
        return False
    first = line.split(None, 1)[0]
    return first in _COMMANDS


def dispatch_trajectory_command(
    line: str,
    *,
    builder: Optional[TrajectoryBuilder] = None,
    renderer: Optional[TrajectoryRenderer] = None,
) -> TrajectoryDispatchResult:
    """One-call dispatcher for ``/trajectory`` REPL subcommands."""
    if not _matches(line):
        return TrajectoryDispatchResult(ok=False, text="", matched=False)
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return TrajectoryDispatchResult(
            ok=False, text=f"  /trajectory parse error: {exc}",
        )
    if not tokens:
        return TrajectoryDispatchResult(ok=False, text="", matched=False)
    b = builder or get_default_builder()
    r = renderer or get_default_renderer()
    args = tokens[1:]
    subcmd = args[0] if args else "status"
    if subcmd == "help":
        return TrajectoryDispatchResult(ok=True, text=_TRAJECTORY_HELP)
    if subcmd == "watch":
        # Reserved for Slice 5 — report intent + point operator at
        # the stream module. Non-mutating.
        return TrajectoryDispatchResult(
            ok=True,
            text=(
                "  /trajectory watch: subscribe to TrajectoryStream "
                "listener hooks for live frames (Slice 5 wires the "
                "default SerpentFlow/IDE paths)."
            ),
        )
    surface = _surface_for_subcmd(subcmd)
    if surface is None:
        return TrajectoryDispatchResult(
            ok=False,
            text=f"  /trajectory: unknown subcommand {subcmd!r}",
        )
    frame = b.build()
    text = r.render(frame, surface=surface)
    return TrajectoryDispatchResult(ok=True, text=text)


def _surface_for_subcmd(cmd: str) -> Optional[TrajectorySurface]:
    table: Dict[str, TrajectorySurface] = {
        "status":   TrajectorySurface.REPL_COMPACT,
        "summary":  TrajectorySurface.REPL_COMPACT,
        "expanded": TrajectorySurface.REPL_EXPANDED,
        "full":     TrajectorySurface.REPL_EXPANDED,
        "json":     TrajectorySurface.IDE_JSON,
        "ide":      TrajectorySurface.IDE_JSON,
        "sse":      TrajectorySurface.SSE,
        "plain":    TrajectorySurface.PLAIN,
    }
    return table.get(cmd)


__all__ = [
    "TRAJECTORY_VIEW_SCHEMA_VERSION",
    "CostSupplier",
    "EtaSupplier",
    "OpStateSupplier",
    "SensorTriggerSupplier",
    "TrajectoryBuilder",
    "TrajectoryDispatchResult",
    "TrajectoryListener",
    "TrajectoryRenderer",
    "TrajectoryStream",
    "TrajectorySurface",
    "dispatch_trajectory_command",
    "get_default_builder",
    "get_default_renderer",
    "get_default_stream",
    "reset_default_trajectory_singletons",
    "set_default_suppliers",
]

_ = (Confidence, FrozenSet, field, TrajectoryPhase)  # silence unused-import guards
