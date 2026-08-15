"""The MODE decides the surfaces; the flag is one way to assert the mode.

`converged_headless.main()` does `os.environ.setdefault("JARVIS_SERVICE_MODE",
"1")` on its way in — the flag declared the mode, and then everything
downstream keyed off the FLAG. A supervisor started as a service without that
particular spelling served a different set of surfaces than one started with
it, while being the same thing in every way that matters.

Measured: `rg -c GovernanceSSE` over the whole backend log returned 0 across
every boot, and `/api/stream/{device_id}` answered 404 on the `trinity up`
path and 200 under `--headless`.
"""
from __future__ import annotations

import pytest

from backend.api import service_surface as ss


class _State:
    pass


class _App:
    """Enough FastAPI to exercise mounting honestly: real route objects, a
    real `state`, and `include_router` that actually appends."""

    def __init__(self, routes=()):
        self.routes = [_Route(p) for p in routes]
        self.state = _State()
        self.included = []

    def include_router(self, router, **kwargs):
        self.included.append((router, kwargs))
        self.routes.extend(getattr(router, "routes", []))


class _Route:
    def __init__(self, path):
        self.path = path


class _Router:
    def __init__(self, *paths):
        self.routes = [_Route(p) for p in paths]


# ---------------------------------------------------------------------------
# The mode
# ---------------------------------------------------------------------------


class TestTheModeNotTheFlag:
    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        monkeypatch.delenv("JARVIS_SERVICE_MODE", raising=False)
        monkeypatch.delenv("JARVIS_SERVICE_SURFACE_ENABLED", raising=False)
        yield

    def test_service_mode_alone_is_enough(self, monkeypatch):
        """THE fix. `trinity up` sets this and passes no --headless; it was
        telling the truth about what it wanted and nothing was listening."""
        monkeypatch.setenv("JARVIS_SERVICE_MODE", "1")
        assert ss.service_mode_active(argv=["unified_supervisor.py"]) is True

    def test_the_flag_still_asserts_the_mode(self, monkeypatch):
        """Kept, so running the converged app by hand behaves identically."""
        assert ss.service_mode_active(
            argv=["unified_supervisor.py", "--headless"]) is True

    def test_neither_means_an_interactive_desktop_boot(self):
        assert ss.service_mode_active(argv=["unified_supervisor.py"]) is False

    def test_an_operator_can_refuse_the_surface_entirely(self, monkeypatch):
        """The escape hatch returns exactly the behaviour that shipped."""
        monkeypatch.setenv("JARVIS_SERVICE_MODE", "1")
        monkeypatch.setenv("JARVIS_SERVICE_SURFACE_ENABLED", "0")
        assert ss.service_mode_active(argv=["x", "--headless"]) is False

    def test_it_never_raises_on_a_hostile_environment(self, monkeypatch):
        class _Hostile(dict):
            def get(self, *_a, **_k):
                raise RuntimeError("environ exploded")

        monkeypatch.setattr(ss.os, "environ", _Hostile())
        assert ss.service_mode_active(argv=[]) in (True, False)


# ---------------------------------------------------------------------------
# Mounting by identity, not by path (the bug that hid all of this)
# ---------------------------------------------------------------------------


class TestMountingKeysOnRouterIdentity:
    def test_a_shared_path_no_longer_skips_the_whole_router(self):
        """THE regression. `observability_gateway` owns a `/ws`, and the
        old check (`any(route.path == '/ws')`) skipped a router that owns
        `/ws` AND the device SSE routes — so `/api/stream/*` went missing."""
        app = _App(routes=["/ws"])                    # another subsystem's
        router = _Router("/ws", "/api/stream/{device_id}")
        assert ss.include_router_once(app, router, name="unified_websocket")
        assert "/api/stream/{device_id}" in [r.path for r in app.routes]

    def test_a_collision_is_reported_rather_than_silent(self, caplog):
        """FastAPI dispatches first-match-wins, so a duplicate is inert —
        but inert and invisible is what let a router go unmounted for
        months."""
        app = _App(routes=["/ws"])
        router = _Router("/ws", "/api/stream/x")
        with caplog.at_level("INFO"):
            ss.include_router_once(app, router, name="unified_websocket")
        assert any("/ws" in r.message or "path" in r.message
                   for r in caplog.records)

    def test_collides_names_the_overlap(self):
        app = _App(routes=["/ws", "/health"])
        assert ss.collides(app, _Router("/ws", "/new")) == ("/ws",)
        assert ss.collides(app, _Router("/new")) == ()

    def test_the_same_router_is_not_mounted_twice(self):
        """Phase 2: flipping the env later must not double-register."""
        app = _App()
        router = _Router("/api/stream/x")
        assert ss.include_router_once(app, router, name="unified_websocket")
        assert not ss.include_router_once(app, router, name="unified_websocket")
        assert len(app.included) == 1

    def test_two_apps_do_not_share_a_mount_ledger(self):
        """Recorded on `app.state`, so an embedded harness or a test app
        cannot convince a second app that it is already mounted."""
        router = _Router("/api/stream/x")
        a, b = _App(), _App()
        assert ss.include_router_once(a, router, name="r")
        assert ss.include_router_once(b, router, name="r")

    def test_a_hostile_app_never_raises(self):
        class _Hostile:
            routes = []
            state = _State()

            def include_router(self, *_a, **_k):
                raise RuntimeError("no")

        assert ss.include_router_once(_Hostile(), _Router("/x"), name="r") is False


# ---------------------------------------------------------------------------
# The surface itself
# ---------------------------------------------------------------------------


class TestMountingTheSurface:
    async def test_it_is_a_no_op_outside_service_mode(self, monkeypatch):
        monkeypatch.delenv("JARVIS_SERVICE_MODE", raising=False)
        monkeypatch.setattr(ss, "service_mode_active", lambda *a, **k: False)
        assert await ss.mount_service_surface(_App()) is False

    async def test_it_reuses_the_converged_dag_rather_than_restating_it(
            self, monkeypatch):
        """A second list of subsystems here would drift from
        `converged_headless.default_subsystems()` the moment either was
        extended. The DAG is imported, not copied."""
        monkeypatch.setattr(ss, "service_mode_active", lambda *a, **k: True)
        seen = {}

        class _Orch:
            def __init__(self, subsystems):
                seen["subsystems"] = subsystems

            async def hydrate(self):
                seen["hydrated"] = True

        monkeypatch.setattr(
            "backend.api.progressive_hydration.HydrationOrchestrator", _Orch)
        from backend.api.converged_headless import default_subsystems

        assert await ss.mount_service_surface(_App()) is True
        assert seen.get("hydrated") is True
        names = [getattr(s, "name", "") for s in seen["subsystems"]]
        assert names == [getattr(s, "name", "")
                         for s in default_subsystems()], names
        assert "governance_bridge" in names

    async def test_a_failing_hydration_never_blocks_the_boot(self, monkeypatch):
        monkeypatch.setattr(ss, "service_mode_active", lambda *a, **k: True)

        class _Orch:
            def __init__(self, *_a):
                pass

            async def hydrate(self):
                raise RuntimeError("subsystem exploded")

        monkeypatch.setattr(
            "backend.api.progressive_hydration.HydrationOrchestrator", _Orch)
        assert await ss.mount_service_surface(_App()) is False


# ---------------------------------------------------------------------------
# Phase 2 — nobody watching must cost nothing
# ---------------------------------------------------------------------------


class TestNoListenerCostsNoMemory:
    """A mounted bridge with no Swift client attached must DROP, not
    accumulate. The bound is the existing ring, not a new one."""

    async def test_frames_are_dropped_not_buffered_when_no_stream_exists(
            self, monkeypatch):
        from backend.api.governance_cross_process import (
            BrokerSink, GovernanceBusConsumer,
        )
        from backend.core.ouroboros.governance.ide_observability_stream import (
            StreamEventBroker,
        )
        monkeypatch.setenv("JARVIS_GOVERNANCE_BUS_BRIDGE_ENABLED", "1")
        monkeypatch.setattr(
            "backend.core.event_stream.get_event_stream_if_initialized",
            lambda: None)                       # nobody is watching

        broker = StreamEventBroker()
        consumer = GovernanceBusConsumer(broker)
        assert await consumer.start()
        try:
            sink = BrokerSink(broker)
            for _ in range(400):
                await sink([{"command_id": "op", "narration_text": "x"}])
            for _ in range(60):
                if consumer.stats["no_stream"]:
                    break
                await _sleep()
            assert consumer.stats["no_stream"] > 0, "drops must be COUNTED"
            assert consumer.stats["broadcast"] == 0
            # The bound that matters: the broker's own ring, not growth.
            assert broker.history_size <= 512, broker.history_size
        finally:
            await consumer.stop()

    async def test_the_producer_queue_is_bounded_regardless_of_listeners(self):
        from backend.api.governance_cross_process import BrokerSink
        from backend.api.governance_sse_bridge import GovernanceSSEBridge
        from backend.core.ouroboros.governance.ide_observability_stream import (
            StreamEventBroker,
        )
        bridge = GovernanceSSEBridge(sink=BrokerSink(StreamEventBroker()))
        assert 0 < bridge._queue.maxsize < 100_000


async def _sleep():
    import asyncio
    await asyncio.sleep(0.01)
