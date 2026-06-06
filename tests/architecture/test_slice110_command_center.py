"""Slice 110 — Native Command Center: Observability API Gateway.

Deterministic integration matrix for the bus→WebSocket bridge + the FastAPI
router. NO browser, NO npm, NO live bus required: we drive the gateway helpers
directly + via FastAPI's TestClient, and assert the typed frame contract.

MARQUEE (Phase 4 state-synchronization): a simulated FSM ``ContainmentBreach``
lifecycle event propagates through the bridge and arrives on a connected WS
client as a high-severity ``containment_breach`` frame with the reverse-engineered
breach vector. (The visual render at localhost:3000 is the operator's local
verification; this proves the data path that feeds it.)
"""

from __future__ import annotations

import asyncio

import pytest

from backend.api import observability_gateway as OG


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeWS:
    """Records send_json frames; mimics the Starlette WebSocket surface the
    BroadcastConnectionManager uses (accept / send_json)."""

    def __init__(self):
        self.frames = []
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def send_json(self, message):
        self.frames.append(message)


class _FakeEvent:
    def __init__(self, payload):
        self.payload = payload


def _frame_kinds(ws):
    return [f.get("kind") for f in ws.frames]


# ===========================================================================
# Pure frame derivation (no FastAPI, no bus)
# ===========================================================================


class TestFrameDerivation:
    def test_make_frame_envelope(self):
        f = OG.make_frame(OG.KIND_TELEMETRY, op_id="op-1", payload={"x": 1})
        assert f["kind"] == "telemetry"
        assert f["op_id"] == "op-1"
        assert f["schema_version"] == OG.GATEWAY_FRAME_SCHEMA_VERSION
        assert f["payload"] == {"x": 1}
        assert isinstance(f["ts"], float)

    def test_lifecycle_post_apply_yields_why_and_telemetry(self):
        frames = OG.frames_for_lifecycle("post_apply", "op-2",
                                         {"phase": "APPLY", "confidence": 0.9})
        kinds = [f["kind"] for f in frames]
        assert OG.KIND_WHY_SNAPSHOT in kinds
        assert OG.KIND_TELEMETRY in kinds
        # No breach frame for a clean apply.
        assert OG.KIND_CONTAINMENT_BREACH not in kinds
        # Telemetry carries the gauge fields the dashboard renders.
        tele = next(f for f in frames if f["kind"] == OG.KIND_TELEMETRY)
        assert tele["payload"]["confidence_aura"] == "high"
        assert "shannon_entropy" in tele["payload"]
        assert "decision_prior_distribution" in tele["payload"]

    def test_lifecycle_breach_yields_containment_frame_with_vector(self):
        frames = OG.frames_for_lifecycle(
            "post_failure", "op-3",
            {"phase": "VERIFY", "containment_breach": True,
             "net": "EGRESS_BLOCKED", "reason": "network egress attempt"},
        )
        kinds = [f["kind"] for f in frames]
        assert OG.KIND_CONTAINMENT_BREACH in kinds
        breach = next(f for f in frames if f["kind"] == OG.KIND_CONTAINMENT_BREACH)
        assert breach["payload"]["severity"] == "high"
        assert breach["payload"]["vector"].get("net") == "EGRESS_BLOCKED"

    def test_breach_vector_inferred_from_reason(self):
        v = OG._extract_breach_vector({"reason": "filesystem write to /etc blocked"})
        assert v["inferred"] == "filesystem_violation"
        v2 = OG._extract_breach_vector({"reason": "infinite loop SIGKILL timeout"})
        assert v2["inferred"] == "timeout_or_signal_kill"

    def test_causality_graph_shape(self, monkeypatch):
        # Seed the Slice-109 ledger via its public publish path.
        from backend.core.ouroboros.governance import cognitive_observability as CO
        from backend.core.ouroboros.governance import ide_observability_stream as S
        monkeypatch.setattr(S, "publish_task_event", lambda *a, **k: None)
        monkeypatch.setenv("JARVIS_COGNITIVE_OBSERVABILITY_ENABLED", "1")
        CO.publish_why_snapshot(kind="post_apply", op_id="cz-1",
                                payload={"phase": "APPLY", "target_files": ["a.py"]})
        g = OG.build_causality_graph(30)
        assert isinstance(g["nodes"], list) and isinstance(g["edges"], list)
        ids = {n["id"] for n in g["nodes"]}
        assert "cz-1" in ids
        assert "file::a.py" in ids
        assert any(e["type"] == "touches" for e in g["edges"])


# ===========================================================================
# The bus → WS bridge (MARQUEE state synchronization)
# ===========================================================================


class TestBusBridge:
    @pytest.mark.asyncio
    async def test_containment_breach_propagates_to_ws_client(self):
        ws = _FakeWS()
        await OG.observability_manager.connect(ws)
        try:
            ev = _FakeEvent({
                "lifecycle_kind": "post_failure", "op_id": "breach-op",
                "phase": "VERIFY", "containment_breach": True,
                "net": "EGRESS_BLOCKED", "reason": "container egress attempt",
            })
            await OG._on_lifecycle_to_ws(ev)
            kinds = _frame_kinds(ws)
            # The high-severity breach frame reached the client...
            assert OG.KIND_CONTAINMENT_BREACH in kinds
            breach = next(f for f in ws.frames if f["kind"] == OG.KIND_CONTAINMENT_BREACH)
            assert breach["op_id"] == "breach-op"
            assert breach["payload"]["vector"].get("net") == "EGRESS_BLOCKED"
            # ...alongside the why-snapshot + telemetry frames.
            assert OG.KIND_WHY_SNAPSHOT in kinds
            assert OG.KIND_TELEMETRY in kinds
        finally:
            await OG.observability_manager.disconnect(ws)

    @pytest.mark.asyncio
    async def test_clean_apply_propagates_without_breach_frame(self):
        ws = _FakeWS()
        await OG.observability_manager.connect(ws)
        try:
            ev = _FakeEvent({"lifecycle_kind": "post_apply", "op_id": "ok-op",
                             "phase": "APPLY", "confidence": 0.95})
            await OG._on_lifecycle_to_ws(ev)
            kinds = _frame_kinds(ws)
            assert OG.KIND_WHY_SNAPSHOT in kinds
            assert OG.KIND_CONTAINMENT_BREACH not in kinds
        finally:
            await OG.observability_manager.disconnect(ws)

    @pytest.mark.asyncio
    async def test_bridge_swallows_garbage_event(self):
        # A malformed event must never raise out of the bridge.
        await OG._on_lifecycle_to_ws(object())

    @pytest.mark.asyncio
    async def test_register_bridge_inert_when_gateway_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_OBSERVABILITY_GATEWAY_ENABLED", "0")
        ids = await OG.register_gateway_bridge()
        assert ids == []


# ===========================================================================
# FastAPI router (mounts into the app; REST + WS handshake)
# ===========================================================================


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(OG.build_router())
    return TestClient(app)


class TestRouter:
    def test_health_endpoint(self):
        c = _client()
        r = c.get("/api/observability/health")
        assert r.status_code == 200
        body = r.json()
        assert body["schema_version"] == OG.GATEWAY_FRAME_SCHEMA_VERSION
        assert "ws" in body and "gateway_enabled" in body

    def test_why_snapshots_endpoint(self, monkeypatch):
        from backend.core.ouroboros.governance import cognitive_observability as CO
        from backend.core.ouroboros.governance import ide_observability_stream as S
        monkeypatch.setattr(S, "publish_task_event", lambda *a, **k: None)
        monkeypatch.setenv("JARVIS_COGNITIVE_OBSERVABILITY_ENABLED", "1")
        CO.publish_why_snapshot(kind="post_apply", op_id="rest-1", payload={"confidence": 0.6})
        c = _client()
        r = c.get("/api/observability/why-snapshots?limit=10")
        assert r.status_code == 200
        body = r.json()
        assert "snapshots" in body and body["count"] >= 1

    def test_causality_endpoint(self):
        c = _client()
        r = c.get("/api/observability/causality")
        assert r.status_code == 200
        body = r.json()
        assert "nodes" in body and "edges" in body

    def test_voice_endpoint_requires_master_flag(self, monkeypatch):
        # TestClient host is loopback ("testclient") → passes the loopback gate;
        # with the voice master OFF it must refuse, never mutate.
        monkeypatch.delenv("JARVIS_KAREN_VOICE_ENABLED", raising=False)
        c = _client()
        r = c.post("/api/observability/voice/mute")
        assert r.status_code == 200
        assert r.json()["ok"] is False
        assert r.json()["error"] == "voice_disabled"

    def test_voice_unknown_action_rejected(self, monkeypatch):
        monkeypatch.setenv("JARVIS_KAREN_VOICE_ENABLED", "1")
        c = _client()
        r = c.post("/api/observability/voice/launch_nukes")
        assert r.json()["ok"] is False
        assert r.json()["error"] == "unknown_action"

    def test_ws_handshake_sends_hello_and_backlog(self):
        c = _client()
        with c.websocket_connect("/api/observability/ws") as ws:
            first = ws.receive_json()
            assert first["kind"] == OG.KIND_HELLO


# ===========================================================================
# Co-boot server (Slice 110 follow-up) — engine + gateway in one process
# ===========================================================================


class TestCoBoot:
    def test_gateway_flag_default_false(self, monkeypatch):
        monkeypatch.delenv("JARVIS_COMMAND_CENTER_GATEWAY", raising=False)
        assert OG.command_center_gateway_enabled() is False
        monkeypatch.setenv("JARVIS_COMMAND_CENTER_GATEWAY", "1")
        assert OG.command_center_gateway_enabled() is True

    def test_cors_origins_are_env_driven_no_hardcode(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FRONTEND_PORT", "4321")
        origins = OG._cors_origins()
        assert "http://localhost:4321" in origins
        assert "http://127.0.0.1:4321" in origins
        assert any("host.docker.internal:4321" in o for o in origins)

    def test_gateway_port_host_env_driven(self, monkeypatch):
        monkeypatch.setenv("JARVIS_BACKEND_PORT", "9123")
        monkeypatch.setenv("JARVIS_GATEWAY_HOST", "0.0.0.0")
        assert OG.gateway_port() == 9123
        assert OG.gateway_host() == "0.0.0.0"

    def test_build_gateway_app_mounts_router_with_cors(self):
        from fastapi.testclient import TestClient
        app = OG.build_gateway_app()
        # CORS middleware present.
        assert any("CORSMiddleware" in str(m) for m in app.user_middleware)
        # The gateway router is mounted on the standalone app.
        r = TestClient(app).get("/api/observability/health")
        assert r.status_code == 200
        assert r.json()["schema_version"] == OG.GATEWAY_FRAME_SCHEMA_VERSION

    @pytest.mark.asyncio
    async def test_serve_gateway_builds_and_serves(self, monkeypatch):
        import uvicorn
        captured = {}

        class _FakeServer:
            def __init__(self, config):
                captured["config"] = config
                self.should_exit = False
                self.served = False
            async def serve(self):
                self.served = True
                captured["server"] = self

        monkeypatch.setattr(uvicorn, "Config", lambda *a, **k: {"args": a, "kw": k})
        monkeypatch.setattr(uvicorn, "Server", _FakeServer)
        await OG.serve_gateway(port=0)
        assert captured["server"].served is True

    @pytest.mark.asyncio
    async def test_serve_gateway_graceful_on_cancel(self, monkeypatch):
        import uvicorn
        captured = {}

        class _FakeServer:
            def __init__(self, config):
                self.should_exit = False
                captured["server"] = self
            async def serve(self):
                raise asyncio.CancelledError()

        monkeypatch.setattr(uvicorn, "Config", lambda *a, **k: object())
        monkeypatch.setattr(uvicorn, "Server", _FakeServer)
        with pytest.raises(asyncio.CancelledError):
            await OG.serve_gateway(port=0)
        # On cancel it requests graceful exit.
        assert captured["server"].should_exit is True

    @pytest.mark.asyncio
    async def test_serve_gateway_swallows_missing_uvicorn(self, monkeypatch):
        # If uvicorn import fails, serve_gateway returns quietly (never raises).
        import builtins
        real_import = builtins.__import__

        def _boom(name, *a, **k):
            if name == "uvicorn":
                raise ImportError("no uvicorn")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _boom)
        await OG.serve_gateway(port=0)  # must not raise


class TestLoopbackGate:
    def test_loopback_hosts_pass(self):
        class _Req:
            class client:
                host = "127.0.0.1"
        assert OG._is_loopback(_Req()) is True

    def test_non_loopback_fails_closed(self):
        class _Req:
            class client:
                host = "10.0.0.5"
        assert OG._is_loopback(_Req()) is False

    def test_unknown_request_fails_closed(self):
        assert OG._is_loopback(object()) is False
