"""Slice 110 — Native Command Center: Observability API Gateway.

The *bridge* that was missing. The internal ``TrinityEventBus`` (Slice-101
cognitive lifecycle + Slice-109 Why-Snapshots) reasoned in private; the aiohttp
``EventChannelServer`` served SSE on its own port; the FastAPI app served the
React frontend over WebSockets via ``broadcast_router`` — but NOTHING connected
the cognitive bus to the frontend. This module is that nerve.

It is a **composing gateway, not a parallel server**:

  * It mounts as a FastAPI ``APIRouter`` into the EXISTING ``backend/main.py``
    app (alongside ``broadcast_router``), reusing the EXISTING
    ``BroadcastConnectionManager`` class for WebSocket fan-out — on its own
    dedicated channel so command-center telemetry never mixes with maintenance
    broadcasts.
  * Its bus bridge reuses the Slice-101 ``cognitive_bus`` subscriber registry
    (so it inherits the same master-gate + per-handler fault isolation) and the
    Slice-109 ``cognitive_observability`` Why-Snapshot builder (no recomputation,
    no duplicate schema).

Authority invariant (§1 sovereignty, fail-closed)
--------------------------------------------------
The gateway is **read-only over the web** for everything authority-bearing. The
ONLY write surface is the *cosmetic* Karen voice mute/unmute, routed through the
sanctioned ``karen_voice_command_router`` env seam, and it is **loopback-only**.
FSM state, governance flags, and graduation are observable but NOT mutable here —
flipping them requires the operator REPL + the Operator Commit Authority. A
browser endpoint that flipped authority would be a sovereignty leak past the
Iron Gate; this gateway never offers one.

Masters
-------
* ``JARVIS_OBSERVABILITY_GATEWAY_ENABLED`` — the REST/WS read projection.
  Default **TRUE** (read-only; the app itself is opt-in to run).
* The bus bridge self-gates on ``JARVIS_COGNITIVE_BUS_ENABLED`` (Slice 101).
* The cosmetic voice write self-gates on ``JARVIS_KAREN_VOICE_ENABLED`` (Slice
  109) AND a loopback check.

Typed frames
------------
Every WS message + REST telemetry row is a ``GatewayFrame`` — a discriminated
envelope ``{kind, op_id, ts, payload}`` where ``kind`` ∈ {``why_snapshot``,
``containment_breach``, ``telemetry``, ``causality_update``, ``terminal_line``,
``hello``}. The frontend switches on ``kind``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("jarvis.observability_gateway")

# FastAPI types are imported at MODULE level (not inside build_router) so that
# `from __future__ import annotations` string-annotations resolve against module
# globals when FastAPI introspects the endpoints. A local import would leave the
# names unresolvable and FastAPI would mis-classify `request`/`websocket` as
# query params. FastAPI is a hard dependency of this app; the guard only keeps
# the PURE helpers (frames_for_lifecycle etc.) importable in isolation.
try:
    from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
    _HAVE_FASTAPI = True
except Exception:  # noqa: BLE001
    APIRouter = Request = WebSocket = WebSocketDisconnect = None  # type: ignore
    _HAVE_FASTAPI = False

_TRUTHY = ("1", "true", "yes", "on")

GATEWAY_FRAME_SCHEMA_VERSION = "gateway_frame.v1"

# Frame kinds (discriminator).
KIND_HELLO = "hello"
KIND_WHY_SNAPSHOT = "why_snapshot"
KIND_CONTAINMENT_BREACH = "containment_breach"
KIND_TELEMETRY = "telemetry"
KIND_CAUSALITY = "causality_update"
KIND_TERMINAL = "terminal_line"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def _env_truthy(name: str, *, default: bool) -> bool:
    try:
        raw = os.environ.get(name)
        if raw is None:
            return default
        return raw.strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001
        return default


def gateway_enabled() -> bool:
    """REST/WS read-projection master — default TRUE."""
    return _env_truthy("JARVIS_OBSERVABILITY_GATEWAY_ENABLED", default=True)


# ===========================================================================
# Typed frame envelope
# ===========================================================================

try:
    from pydantic import BaseModel, Field

    class GatewayFrame(BaseModel):
        """Discriminated telemetry envelope sent to command-center clients."""

        kind: str
        op_id: str = ""
        ts: float = Field(default_factory=lambda: 0.0)
        schema_version: str = GATEWAY_FRAME_SCHEMA_VERSION
        payload: Dict[str, Any] = Field(default_factory=dict)

    _HAVE_PYDANTIC = True
except Exception:  # noqa: BLE001 — pydantic always present in this app, but stay safe
    GatewayFrame = None  # type: ignore
    _HAVE_PYDANTIC = False


def make_frame(kind: str, *, op_id: str = "", payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build a plain-dict frame (what ``manager.broadcast`` sends). PURE except
    for the wall-clock stamp. NEVER raises."""
    return {
        "kind": str(kind),
        "op_id": str(op_id or ""),
        "ts": time.time(),
        "schema_version": GATEWAY_FRAME_SCHEMA_VERSION,
        "payload": dict(payload or {}),
    }


# ===========================================================================
# Dedicated WS fan-out channel (reuses the existing manager class)
# ===========================================================================


def _build_manager() -> Any:
    """Instantiate a dedicated command-center channel from the EXISTING
    ``BroadcastConnectionManager`` (composition, not a new manager class).
    Falls back to a minimal in-module manager only if the import fails (keeps
    this module importable in isolation for tests)."""
    try:
        from backend.api.broadcast_router import BroadcastConnectionManager

        return BroadcastConnectionManager()
    except Exception:  # noqa: BLE001
        return _FallbackManager()


class _FallbackManager:
    """Minimal async WS manager — only used if broadcast_router is unimportable
    (e.g. an isolated unit test). Mirrors the broadcast manager's surface."""

    def __init__(self) -> None:
        self._conns: Dict[int, Any] = {}

    async def connect(self, websocket: Any, client_id: str = "") -> int:
        await websocket.accept()
        cid = id(websocket)
        self._conns[cid] = websocket
        return cid

    async def disconnect(self, websocket: Any) -> None:
        self._conns.pop(id(websocket), None)

    async def broadcast(self, message: Dict[str, Any]) -> int:
        n = 0
        for ws in list(self._conns.values()):
            try:
                await ws.send_json(message)
                n += 1
            except Exception:  # noqa: BLE001
                self._conns.pop(id(ws), None)
        return n

    async def send_personal(self, websocket: Any, message: Dict[str, Any]) -> bool:
        try:
            await websocket.send_json(message)
            return True
        except Exception:  # noqa: BLE001
            return False

    def get_stats(self) -> Dict[str, Any]:
        return {"active_connections": len(self._conns), "total_messages": 0, "connections": []}


# The command-center channel (separate from broadcast_router.manager).
observability_manager = _build_manager()


# ===========================================================================
# Telemetry derivation (composes Slice-109 cognitive_observability)
# ===========================================================================


def _extract_breach_vector(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort reverse-engineer the containment-breach vector from a
    lifecycle payload (the Slice-106 receipt codes). NEVER raises."""
    vector: Dict[str, Any] = {}
    try:
        for key in ("net", "etc", "root", "work", "breach_class", "breach"):
            if key in payload and payload[key] is not None:
                vector[key] = payload[key]
        reason = str(payload.get("reason") or "").lower()
        if not vector:
            if "egress" in reason or "network" in reason:
                vector["inferred"] = "network_egress"
            elif "write" in reason or "filesystem" in reason or "etc" in reason or "root" in reason:
                vector["inferred"] = "filesystem_violation"
            elif "timeout" in reason or "sigkill" in reason:
                vector["inferred"] = "timeout_or_signal_kill"
            else:
                vector["inferred"] = "unknown"
    except Exception:  # noqa: BLE001
        return {"inferred": "unknown"}
    return vector


def frames_for_lifecycle(kind: str, op_id: str, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Transform ONE cognitive-bus lifecycle event into the typed gateway frames
    the command center renders. Composes ``cognitive_observability`` for the
    Why-Snapshot + severity classification. Returns 1-3 frames (why_snapshot,
    optional containment_breach, telemetry). NEVER raises — returns [] on error.
    """
    frames: List[Dict[str, Any]] = []
    try:
        from backend.core.ouroboros.governance import cognitive_observability as CO

        snapshot = CO.build_why_snapshot(kind=kind, op_id=op_id, payload=payload)
        event_type, severity = CO.classify_severity(kind, payload)

        # 1. The Why-Snapshot frame (always).
        frames.append(make_frame(
            KIND_WHY_SNAPSHOT, op_id=op_id,
            payload={"snapshot": snapshot, "severity": severity, "event_type": event_type},
        ))

        # 2. A dedicated high-severity containment-breach frame (only on breach).
        if event_type == "cognitive.containment_breach":
            frames.append(make_frame(
                KIND_CONTAINMENT_BREACH, op_id=op_id,
                payload={
                    "vector": _extract_breach_vector(payload),
                    "reason": str(payload.get("reason") or "")[:500],
                    "severity": "high",
                },
            ))

        # 3. A compact telemetry-gauge frame (entropy + confidence band).
        why = snapshot.get("why", {}) if isinstance(snapshot, dict) else {}
        frames.append(make_frame(
            KIND_TELEMETRY, op_id=op_id,
            payload={
                "shannon_entropy": why.get("shannon_entropy"),
                "confidence_aura": why.get("confidence_aura"),
                "confidence_score": why.get("confidence_score"),
                "recursion_depth": why.get("recursion_depth"),
                "decision_prior_distribution": why.get("decision_prior_distribution", {}),
                "phase": snapshot.get("phase", "") if isinstance(snapshot, dict) else "",
                "kind": kind,
            },
        ))
    except Exception:  # noqa: BLE001
        return []
    return frames


def build_causality_graph(limit: int = 30) -> Dict[str, Any]:
    """Derive a causality graph (nodes = recent ops, edges = op→target_file
    touches) from the Slice-109 Why-Snapshot ledger. This is the AI's branching
    decision/file causality — truthful, not fabricated. NEVER raises."""
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    seen_files: Dict[str, str] = {}
    try:
        from backend.core.ouroboros.governance import cognitive_observability as CO

        snaps = CO.recent_why_snapshots(limit)
        prev_op: Optional[str] = None
        for s in snaps:
            op_id = str(s.get("op_id") or "")
            if not op_id:
                continue
            why = s.get("why", {})
            nodes.append({
                "id": op_id,
                "type": "op",
                "kind": s.get("kind", ""),
                "phase": s.get("phase", ""),
                "state": s.get("state", ""),
                "confidence_aura": why.get("confidence_aura"),
                "risk_tier": s.get("risk_tier", ""),
            })
            # Temporal causal edge: previous op → this op (decision sequence).
            if prev_op:
                edges.append({"source": prev_op, "target": op_id, "type": "sequence"})
            prev_op = op_id
            # Bipartite op → file edges (what the decision touched).
            for f in (s.get("target_files") or [])[:8]:
                fid = f"file::{f}"
                if fid not in seen_files:
                    seen_files[fid] = f
                    nodes.append({"id": fid, "type": "file", "label": str(f)})
                edges.append({"source": op_id, "target": fid, "type": "touches"})
    except Exception:  # noqa: BLE001
        return {"nodes": [], "edges": [], "diagnostic": "unavailable"}
    return {"nodes": nodes, "edges": edges, "count": len(nodes)}


# ===========================================================================
# The bus → WS bridge (the additive core; composes Slice-101 registry)
# ===========================================================================


async def _on_lifecycle_to_ws(event: Any) -> None:
    """Cognitive-bus subscriber → fan typed frames out to command-center WS
    clients. NEVER raises (the bus wrapper double-guards too)."""
    try:
        from backend.core.ouroboros.governance import cognitive_observability as CO

        kind, op_id, payload = CO._unpack(event)
        if not kind:
            return
        for frame in frames_for_lifecycle(kind, op_id, dict(payload)):
            await observability_manager.broadcast(frame)
    except Exception:  # noqa: BLE001
        logger.debug("[ObsGateway] lifecycle→WS bridge swallowed", exc_info=True)


def build_gateway_subscriber() -> Optional[Any]:
    """A ``CognitiveSubscriber`` binding the bridge to the lifecycle pattern.
    Returns ``None`` if the cognitive_bus module is unavailable."""
    try:
        from backend.core.ouroboros.governance.cognitive_bus import (
            CognitiveSubscriber,
            lifecycle_pattern,
        )
    except Exception:  # noqa: BLE001
        return None
    return CognitiveSubscriber("observability_gateway_ws", lifecycle_pattern(), _on_lifecycle_to_ws)


async def register_gateway_bridge(*, bus: Any = None) -> List[str]:
    """Boot entry: subscribe the bus→WS bridge to the cognitive bus. Composes
    ``cognitive_bus.register_cognitive_subscribers`` (inherits master-gate +
    fault isolation). Inert (returns []) when the cognitive bus is off or the
    gateway is disabled. NEVER raises."""
    if not gateway_enabled():
        return []
    sub = build_gateway_subscriber()
    if sub is None:
        return []
    try:
        from backend.core.ouroboros.governance.cognitive_bus import (
            register_cognitive_subscribers,
        )
        return await register_cognitive_subscribers([sub], bus=bus)
    except Exception:  # noqa: BLE001
        return []


# ===========================================================================
# FastAPI router (mounts into the existing app)
# ===========================================================================


def _is_loopback(request: Any) -> bool:
    """True iff the request originates from loopback. FAIL-CLOSED: unknown host
    → not loopback. NEVER raises."""
    try:
        client = getattr(request, "client", None)
        host = getattr(client, "host", None) if client is not None else None
        return str(host) in _LOOPBACK_HOSTS
    except Exception:  # noqa: BLE001
        return False


# ===========================================================================
# Co-boot server (Slice 110 follow-up) — run the gateway IN the engine process
# ===========================================================================
#
# Process-topology fix: the bus→WS bridge broadcasts to ``observability_manager``,
# a module global. For LIVE cognitive frames, the producer (the governed loop,
# which registers the bridge at GLS boot) and the gateway HTTP/WS server must
# share ONE process + event loop. ``serve_gateway`` runs a lightweight uvicorn
# server for ONLY the gateway router (it does NOT import the monolith) on the
# CURRENT running loop — co-booted by the soak harness once GLS is up. Then a
# WS client connected to this server receives the bridge's live broadcasts.


def command_center_gateway_enabled() -> bool:
    """Co-boot switch — when TRUE, the harness serves the gateway in-process so
    live cognitive frames reach the command center. §33.1 default FALSE (the
    headless evidence soak does not open a port unless asked)."""
    return _env_truthy("JARVIS_COMMAND_CENTER_GATEWAY", default=False)


def _cors_origins() -> List[str]:
    """Dev CORS allow-list for the command-center frontend. Env-driven (no
    hardcoded port): the frontend port comes from ``JARVIS_FRONTEND_PORT`` and we
    permit the standard local hostnames (incl. the Docker bridge host)."""
    port = (os.environ.get("JARVIS_FRONTEND_PORT", "3000") or "3000").strip()
    hosts = ("localhost", "127.0.0.1", "host.docker.internal")
    return [f"http://{h}:{port}" for h in hosts]


def gateway_host() -> str:
    return (os.environ.get("JARVIS_GATEWAY_HOST", "127.0.0.1") or "127.0.0.1").strip()


def gateway_port() -> int:
    try:
        return int(os.environ.get("JARVIS_BACKEND_PORT", "8000") or "8000")
    except Exception:  # noqa: BLE001
        return 8000


def build_gateway_app() -> Any:
    """A MINIMAL standalone FastAPI app hosting ONLY the observability gateway
    router + CORS for the frontend. Deliberately does NOT import the monolith
    (`backend.main`) — keeps the co-boot lightweight + fast. Requires FastAPI."""
    if not _HAVE_FASTAPI:
        raise RuntimeError("FastAPI unavailable — cannot build gateway app")
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    app = FastAPI(title="O+V Observability Gateway", docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(build_router())
    return app


async def serve_gateway(*, host: Optional[str] = None, port: Optional[int] = None,
                        log_level: str = "warning") -> None:
    """Run the gateway as a uvicorn ``Server`` on the CURRENT event loop (co-boot
    inside the engine process). Blocks until cancelled. NEVER raises out — a
    bind failure or shutdown is logged, not propagated into the soak. On
    cancellation it asks the server to exit gracefully."""
    try:
        import uvicorn
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ObsGateway] uvicorn unavailable — gateway not served: %s", exc)
        return
    h = host or gateway_host()
    p = int(port if port is not None else gateway_port())
    try:
        config = uvicorn.Config(build_gateway_app(), host=h, port=p,
                                log_level=log_level, loop="asyncio", lifespan="off")
        server = uvicorn.Server(config)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ObsGateway] gateway server config failed: %s", exc)
        return
    logger.info("[ObsGateway] command-center gateway serving on http://%s:%d (co-boot)", h, p)
    try:
        await server.serve()
    except asyncio.CancelledError:
        # Graceful shutdown on soak teardown.
        try:
            server.should_exit = True
        except Exception:  # noqa: BLE001
            pass
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ObsGateway] gateway server stopped: %s", exc)


def build_router() -> Any:
    """Construct the observability ``APIRouter`` (mounts into the existing app).
    Requires FastAPI (a hard app dependency); the module-level guarded import
    above keeps the pure helpers importable without it."""
    if not _HAVE_FASTAPI:
        raise RuntimeError("FastAPI unavailable — cannot build observability router")

    router = APIRouter(prefix="/api/observability", tags=["observability"])

    @router.get("/health")
    async def health() -> Dict[str, Any]:
        from backend.core.ouroboros.governance import cognitive_observability as CO
        try:
            from backend.core.ouroboros.governance.cognitive_bus import cognitive_bus_enabled
            bus_on = cognitive_bus_enabled()
        except Exception:  # noqa: BLE001
            bus_on = False
        return {
            "gateway_enabled": gateway_enabled(),
            "cognitive_bus_enabled": bus_on,
            "voice_enabled": CO.cognitive_voice_enabled(),
            "observability_enabled": CO.cognitive_observability_enabled(),
            "ws": observability_manager.get_stats(),
            "schema_version": GATEWAY_FRAME_SCHEMA_VERSION,
        }

    @router.get("/why-snapshots")
    async def why_snapshots(limit: int = 25) -> Dict[str, Any]:
        from backend.core.ouroboros.governance import cognitive_observability as CO
        rows = CO.recent_why_snapshots(limit)
        return {"snapshots": rows, "count": len(rows)}

    @router.get("/why-snapshot/{op_id}")
    async def why_snapshot(op_id: str) -> Dict[str, Any]:
        from backend.core.ouroboros.governance import cognitive_observability as CO
        snap = CO.why_snapshot_for_op(op_id)
        return {"op_id": op_id, "snapshot": snap, "found": snap is not None}

    @router.get("/causality")
    async def causality(limit: int = 30) -> Dict[str, Any]:
        return build_causality_graph(limit)

    @router.post("/voice/{action}")
    async def voice_control(action: str, request: Request) -> Dict[str, Any]:
        """COSMETIC-ONLY write surface: mute/unmute Karen's voice. Routed through
        the sanctioned ``karen_voice_command_router`` env seam. Loopback-only +
        gated on JARVIS_KAREN_VOICE_ENABLED. This is the ONLY mutating endpoint,
        and it touches NOTHING authority-bearing (no FSM/governance/graduation)."""
        if not _is_loopback(request):
            return {"ok": False, "error": "loopback_only", "detail": "voice control is local-only"}
        from backend.core.ouroboros.governance import cognitive_observability as CO
        if not CO.cognitive_voice_enabled():
            return {"ok": False, "error": "voice_disabled",
                    "detail": "set JARVIS_KAREN_VOICE_ENABLED=1 to enable the voice channel"}
        verb = {"mute": "karen mute", "unmute": "karen unmute",
                "verbose": "karen verbose", "normal": "karen normal"}.get(action.lower())
        if verb is None:
            return {"ok": False, "error": "unknown_action",
                    "detail": "action must be mute|unmute|verbose|normal"}
        try:
            from backend.core.ouroboros.governance.karen_voice_command_router import (
                dispatch_karen_voice_command,
            )
            result = dispatch_karen_voice_command(verb)
            return {"ok": bool(getattr(result, "handled", False)),
                    "spoken": getattr(result, "text", ""), "action": action.lower()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "dispatch_failed", "detail": str(exc)}

    @router.websocket("/ws")
    async def observability_ws(websocket: WebSocket) -> None:
        """Command-center telemetry stream. On connect, replays the recent
        Why-Snapshot backlog + the causality graph, then streams live frames
        forwarded by the bus bridge."""
        await observability_manager.connect(websocket)
        try:
            # Hello + backlog replay so a fresh client paints immediately.
            await observability_manager.send_personal(websocket, make_frame(
                KIND_HELLO, payload={"schema_version": GATEWAY_FRAME_SCHEMA_VERSION}))
            try:
                from backend.core.ouroboros.governance import cognitive_observability as CO
                for s in CO.recent_why_snapshots(25):
                    await observability_manager.send_personal(websocket, make_frame(
                        KIND_WHY_SNAPSHOT, op_id=str(s.get("op_id", "")),
                        payload={"snapshot": s, "replay": True}))
                await observability_manager.send_personal(websocket, make_frame(
                    KIND_CAUSALITY, payload=build_causality_graph(30)))
            except Exception:  # noqa: BLE001
                pass
            # Keep the socket alive; the bridge pushes live frames out-of-band.
            while True:
                # Receive is only used to detect disconnects / client pings.
                await websocket.receive_text()
        except WebSocketDisconnect:
            await observability_manager.disconnect(websocket)
        except Exception:  # noqa: BLE001
            await observability_manager.disconnect(websocket)

    return router
