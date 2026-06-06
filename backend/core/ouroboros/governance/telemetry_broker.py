"""Slice 114 — cross-process telemetry broker (gateway decoupling).

Slices 110–113 co-booted the FastAPI gateway INSIDE the engine's event loop, so
the command center shared the loop's fate: the Oracle freeze (fixed by Slice
112) was the worst, but ChromaDB / EmbeddingService / consciousness C-extension
boots still starve the loop ~57 s, and the gateway times out with it. A UI that
shares the engine loop can never be "100 % responsive."

This module severs that coupling. The gateway runs in its OWN OS process (its
OWN event loop), and telemetry crosses the boundary through a bounded,
non-blocking ``multiprocessing.Queue``:

    ENGINE process                         GATEWAY process (isolated)
    ──────────────                         ──────────────────────────
    bridge handler                         uvicorn(observability gateway)
      └─ publish_frame(q, frame)  ──Q──▶     └─ _drain(): q.get → manager.broadcast
         (put_nowait, drop-oldest;             (blocking get OFF its own loop;
          NEVER blocks the FSM loop)            engine freezes can't touch it)

Result: the engine loop can freeze for any reason (vector loads, GIL-heavy ops)
and the gateway process keeps serving WS/REST with zero timeouts — it only ever
*consumes* a queue. No external dependency (stdlib ``multiprocessing`` only),
no lock the engine can contend on (the queue is the only shared primitive).

Master ``JARVIS_GATEWAY_DECOUPLED_ENABLED`` — §33.1 default-FALSE (the co-boot
path stays the default until this is soak-proven). NEVER raises into the FSM.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import queue as _queue
from typing import Any, Optional

logger = logging.getLogger("ouroboros.telemetry_broker")

_TRUTHY = ("1", "true", "yes", "on")
_ENV_MASTER = "JARVIS_GATEWAY_DECOUPLED_ENABLED"
_ENV_QUEUE_MAX = "JARVIS_TELEMETRY_QUEUE_MAX"

# Shutdown sentinel pushed onto the queue to stop the gateway-process drain.
_STOP = "__telemetry_stop__"


def gateway_decoupled_enabled() -> bool:
    """§33.1 master — default FALSE. NEVER raises."""
    try:
        raw = os.environ.get(_ENV_MASTER)
        return bool(raw) and raw.strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001
        return False


def _queue_max() -> int:
    try:
        return max(16, int(os.environ.get(_ENV_QUEUE_MAX, "512")))
    except Exception:  # noqa: BLE001
        return 512


def make_telemetry_queue() -> Any:
    """Create the bounded cross-process telemetry queue (spawn context — matches
    the Slice-112 Oracle IPC + safe on macOS)."""
    return multiprocessing.get_context("spawn").Queue(maxsize=_queue_max())


def publish_frame(q: Any, frame: Any) -> bool:
    """ENGINE-SIDE: hand a telemetry frame to the gateway process. NON-BLOCKING
    and drop-oldest on a full queue — the FSM event loop must NEVER block on
    telemetry, so a slow/dead gateway can only ever cost us the oldest frame,
    never a stall. Returns True iff the frame was enqueued. NEVER raises."""
    if q is None:
        return False
    try:
        q.put_nowait(frame)
        return True
    except _queue.Full:
        # Drop the oldest frame to make room — recency beats completeness for a
        # live dashboard. Best-effort; never blocks.
        try:
            q.get_nowait()
        except Exception:  # noqa: BLE001
            pass
        try:
            q.put_nowait(frame)
            return True
        except Exception:  # noqa: BLE001
            return False
    except Exception:  # noqa: BLE001
        return False


def stop_gateway_queue(q: Any) -> None:
    """Push the drain-stop sentinel (best-effort)."""
    try:
        if q is not None:
            q.put_nowait(_STOP)
    except Exception:  # noqa: BLE001
        pass


# ===========================================================================
# Gateway process (isolated) — drains the queue + serves WS/REST
# ===========================================================================


async def drain_queue_to_manager(q: Any, manager: Any, *, _stop_after: Optional[int] = None) -> int:
    """GATEWAY-SIDE drain loop: pull frames off the cross-process queue and fan
    them to the local WS manager. The blocking ``q.get`` runs in a worker thread
    (``run_in_executor``) so the gateway's OWN event loop is never blocked
    either. Stops on the ``_STOP`` sentinel / EOF. ``_stop_after`` bounds it for
    tests. Returns the number of frames broadcast. NEVER raises out."""
    loop = asyncio.get_running_loop()
    delivered = 0
    while True:
        try:
            frame = await loop.run_in_executor(None, q.get)
        except (EOFError, OSError):
            return delivered
        except asyncio.CancelledError:
            return delivered
        if frame == _STOP or frame is None:
            return delivered
        try:
            await manager.broadcast(frame)
            delivered += 1
        except Exception:  # noqa: BLE001 — one bad frame never kills the drain
            logger.debug("[TelemetryBroker] broadcast swallowed", exc_info=True)
        if _stop_after is not None and delivered >= _stop_after:
            return delivered


def _gateway_process_main(q: Any, host: str, port: int, log_level: str = "warning") -> None:
    """Top-level (spawn-picklable) entrypoint for the ISOLATED gateway process.
    Runs the observability gateway under uvicorn + the queue drain on its own
    fresh event loop. NEVER lets an exception escape (process exit signals the
    parent)."""
    try:
        asyncio.run(_gateway_process_async(q, host, port, log_level))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TelemetryBroker] gateway process exited: %s", exc)


async def _gateway_process_async(q: Any, host: str, port: int, log_level: str) -> None:
    import uvicorn
    from backend.api.observability_gateway import build_gateway_app, observability_manager

    app = build_gateway_app()
    drain_task = asyncio.ensure_future(drain_queue_to_manager(q, observability_manager))
    config = uvicorn.Config(app, host=host, port=int(port), log_level=log_level,
                            loop="asyncio", lifespan="off")
    server = uvicorn.Server(config)
    logger.info("[TelemetryBroker] decoupled gateway serving on http://%s:%d (own process)", host, port)
    try:
        await server.serve()
    finally:
        drain_task.cancel()
        try:
            await drain_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


def spawn_gateway_process(q: Any, host: str = "127.0.0.1", port: int = 8000) -> Any:
    """Spawn the isolated gateway process. Returns the ``Process`` handle. The
    queue ``q`` is inherited by the child at spawn. NEVER raises beyond what
    Process.start would."""
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(
        target=_gateway_process_main, args=(q, host, int(port)), daemon=True,
        name="jarvis-observability-gateway",
    )
    proc.start()
    return proc


# ===========================================================================
# Engine-side bridge — publishes lifecycle frames to the queue (decoupled mode)
# ===========================================================================


def build_queue_publishing_subscriber(q: Any) -> Optional[Any]:
    """A ``CognitiveSubscriber`` that, instead of broadcasting to an in-process
    WS manager, publishes telemetry frames to the cross-process queue. This is
    the decoupled-mode replacement for the Slice-110 in-process bridge.
    Returns ``None`` if the cognitive_bus module is unavailable."""
    try:
        from backend.core.ouroboros.governance.cognitive_bus import (
            CognitiveSubscriber,
            lifecycle_pattern,
        )
        from backend.core.ouroboros.governance import cognitive_observability as CO
        from backend.api.observability_gateway import frames_for_lifecycle
    except Exception:  # noqa: BLE001
        return None

    async def _on_lifecycle_to_queue(event: Any) -> None:
        try:
            kind, op_id, payload = CO._unpack(event)
            if not kind:
                return
            for frame in frames_for_lifecycle(kind, op_id, dict(payload)):
                publish_frame(q, frame)
        except Exception:  # noqa: BLE001 — telemetry never touches the FSM
            logger.debug("[TelemetryBroker] queue bridge swallowed", exc_info=True)

    return CognitiveSubscriber("telemetry_broker_queue", lifecycle_pattern(), _on_lifecycle_to_queue)
