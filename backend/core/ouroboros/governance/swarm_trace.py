"""Deterministic Swarm Trace — a JSONL post-flight artifact, not async stdout.

When the Sentinel auto-launches the swarm we are NOT at the terminal, and
interleaved async ``stdout`` from N concurrent sub-agents is unreadable. This
emits a deterministic **JSON Lines** trace (``swarm_trace.jsonl``) — one record
per lifecycle event, each a complete self-describing JSON object — so a swarm
run is auditable line-by-line after the fact (``jq`` / pandas-friendly).

Records the mandated per-sub-agent fields across Fan-Out → Fan-In:
``ts`` (monotonic + wall), ``ttft_s``, ``itl_s``, AST node boundaries
(``node_start_line`` / ``node_end_line`` / ``symbol``), and the AIMD
``concurrency`` integer in force when the agent was dispatched.

DRY: reuses the exact ``op.terminal.#``-style bus-subscribe pattern of
``StrategyOutcomeLogger`` (#70021/#70022) — ``attach_to_bus`` subscribes a
handler that translates bus events into trace lines. It is NOT a separate
logging framework: the typed ``record_*`` helpers are thin wrappers over one
append-only ``emit``. Never raises on the trace path (telemetry never perturbs
the swarm).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger("Ouroboros.SwarmTrace")

PHASE_FAN_OUT = "fan_out"
PHASE_TOKEN = "token"
PHASE_FAN_IN = "fan_in"
PHASE_AIMD = "aimd"


def default_trace_path() -> str:
    return os.environ.get("JARVIS_SWARM_TRACE_PATH", "swarm_trace.jsonl")


class SwarmTracer:
    """Append-only JSONL swarm-lifecycle tracer. Deterministic + self-describing.

    Direct API (the swarm/interceptor call these) + a bus-subscriber
    (``attach_to_bus``) for when lifecycle events are published on the
    TrinityEventBus. Both funnel through one ``emit``."""

    def __init__(self, path: Optional[str] = None, *, op_id: str = "") -> None:
        self._path = path or default_trace_path()
        self._op_id = op_id
        self._sub_id: Optional[str] = None
        self._seq = 0

    # -- the single append-only sink -------------------------------------

    def emit(self, phase: str, **fields: Any) -> None:
        """Write ONE JSON Lines record. Never raises."""
        try:
            self._seq += 1
            record = {
                "seq": self._seq,
                "phase": phase,
                "op_id": fields.pop("op_id", self._op_id),
                "ts_mono": fields.pop("ts_mono", None) if "ts_mono" in fields else time.monotonic(),
                "ts_wall": fields.pop("ts_wall", None) if "ts_wall" in fields else time.time(),
                **fields,
            }
            line = json.dumps(record, sort_keys=True, default=str)
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:  # noqa: BLE001 — a trace write never perturbs the swarm
            logger.debug("[SwarmTrace] emit failed", exc_info=True)

    # -- typed lifecycle helpers -----------------------------------------

    def record_fan_out(
        self, *, sub_agent: str, symbol: str, node_start_line: int,
        node_end_line: int, concurrency: int, **extra: Any,
    ) -> None:
        """A sub-agent was dispatched onto its AST node at the current AIMD limit."""
        self.emit(
            PHASE_FAN_OUT, sub_agent=sub_agent, symbol=symbol,
            node_start_line=int(node_start_line), node_end_line=int(node_end_line),
            concurrency=int(concurrency), **extra,
        )

    def record_token(
        self, *, sub_agent: str, ttft_s: Optional[float] = None,
        itl_s: Optional[float] = None, **extra: Any,
    ) -> None:
        """A generation-integrity datapoint for a sub-agent (TTFT / rolling ITL)."""
        self.emit(
            PHASE_TOKEN, sub_agent=sub_agent,
            ttft_s=(round(ttft_s, 4) if ttft_s is not None else None),
            itl_s=(round(itl_s, 4) if itl_s is not None else None), **extra,
        )

    def record_fan_in(
        self, *, sub_agent: str, symbol: str, converged: bool,
        concurrency: int, **extra: Any,
    ) -> None:
        """A sub-agent's node landed (converged) or was isolated (unconverged)."""
        self.emit(
            PHASE_FAN_IN, sub_agent=sub_agent, symbol=symbol,
            converged=bool(converged), concurrency=int(concurrency), **extra,
        )

    def record_aimd(self, *, event: str, concurrency: int, **extra: Any) -> None:
        """An AIMD scale event (increase / transient-fault downscale / floor)."""
        self.emit(PHASE_AIMD, event=event, concurrency=int(concurrency), **extra)

    # -- bus surface (DRY: same pattern as StrategyOutcomeLogger) --------

    async def on_event(self, event: Any) -> None:
        """Bus handler — translate a swarm-lifecycle bus event into a trace line."""
        try:
            payload = getattr(event, "payload", None) or {}
            topic = getattr(event, "topic", "") or payload.get("phase", "event")
            phase = topic.split(".")[-1] if isinstance(topic, str) else "event"
            self.emit(phase, **{k: v for k, v in payload.items() if k != "phase"})
        except Exception:  # noqa: BLE001
            logger.debug("[SwarmTrace] on_event failed", exc_info=True)

    async def attach_to_bus(self, bus: Any, *, pattern: str = "swarm.#") -> Optional[str]:
        if bus is None or self._sub_id is not None:
            return self._sub_id
        try:
            self._sub_id = await bus.subscribe(pattern, self.on_event)
            return self._sub_id
        except Exception:  # noqa: BLE001
            return None


__all__ = [
    "PHASE_AIMD",
    "PHASE_FAN_IN",
    "PHASE_FAN_OUT",
    "PHASE_TOKEN",
    "SwarmTracer",
    "default_trace_path",
]
