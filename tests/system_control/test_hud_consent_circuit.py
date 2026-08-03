"""The consent circuit, driven end to end over a real socket.

`SecureConsent.swift` was complete. `main.py`'s `consent_verdict` branch was
complete. Between them: nothing. The IPC socket was read-only for its whole
life, `CapabilityRouter._provider` was None, and every gated capability resolved
to "no approval provider available — failing closed". A gate that always refuses
is indistinguishable from a gate that works until somebody needs what is behind
it — and what was behind it was `lock_screen`, `video.start_streaming` and every
other capability the federation had just made nameable.

The mandated scenario is `test_the_operator_is_asked_and_the_answer_executes`: a
real `asyncio` server, a real client socket, a gated call that pushes a
challenge, a verdict echoed back, and the capability running. If this passes,
the circuit is closed.

THE TESTS TO KEEP
-------------------
`test_the_challenge_carries_the_nonce`. The nonce used to be minted AFTER the
request, so the prompt went out with an empty one, `SecureConsent.Challenge`
failed closed on it, and the request vanished without the operator ever seeing
it. A perfectly secure gate that nobody could answer.

`test_no_hud_connected_denies_rather_than_parks`. Zero clients means the
question was never asked. Parking the call anyway makes an unasked question look
exactly like a human who is still thinking about it, for the whole TTL.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest

from backend.hud import consent_bridge as cb
from backend.hud import ipc_server as ipc
from backend.system_control import capability_leases as cl
from backend.system_control import capability_router as cr
from backend.system_control.capability_registry import CapabilityRegistry
from backend.system_control.capability_router import CapabilityRouter, Outcome


def _can_bind() -> bool:
    """Whether this environment lets a test open a loopback socket.

    Some sandboxes refuse every `bind()`. Skipping is honest — the alternative
    is a wall of PermissionErrors that look like the code is broken when the
    only thing that failed was permission to open a socket at all.
    """
    import socket
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


pytestmark = pytest.mark.skipif(
    not _can_bind(),
    reason="sandbox forbids binding a loopback socket; this suite drives a real one")


class _Controller:
    """Two capabilities: one gated, one not."""

    def __init__(self) -> None:
        self.locked = False
        self.streaming = False

    async def lock_screen(self) -> bool:
        """Lock the screen.

        Capability: approval_required
        """
        self.locked = True
        return True

    async def get_battery(self) -> dict:
        """Battery level.

        Capability: read-only
        """
        return {"percent": 91}

    async def start_stream(self) -> bool:
        """Start a stream.

        Capability: session-start, release=stop_stream
        """
        self.streaming = True
        return True

    async def stop_stream(self):
        """Stop a stream.

        Capability: session-end
        """
        self.streaming = False


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("JARVIS_HUD_CONSENT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CAPABILITY_ROUTER_ENABLED", "true")
    ipc._CLIENTS.clear()
    ipc._SERVER_LOOP = None
    cl.reset_lease_book()
    cr.reset_capability_router()
    yield
    ipc._CLIENTS.clear()
    ipc._SERVER_LOOP = None
    cl.reset_lease_book()
    cr.reset_capability_router()


class _Wire:
    """A real client on the other end of a real IPC server."""

    def __init__(self) -> None:
        self.server: Optional[asyncio.AbstractServer] = None
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.received: List[Dict[str, Any]] = []

    async def start(self, port: int = 0, dispatch=None) -> "_Wire":
        async def _noop(event_type: str, data: dict) -> None:
            return None

        self.server = await ipc.start_ipc_server(
            dispatch=dispatch or _noop, shutdown=asyncio.Event(), port=port)
        host, self.port = self.server.sockets[0].getsockname()[:2]
        self.reader, self.writer = await asyncio.open_connection(host, self.port)
        # The server registers the writer in its accept handler; give the loop a
        # turn so `connected_clients()` is true before anything publishes.
        for _ in range(50):
            if ipc.connected_clients():
                break
            await asyncio.sleep(0.01)
        return self

    async def next_event(self, timeout: float = 5.0) -> Dict[str, Any]:
        line = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
        msg = json.loads(line.decode())
        self.received.append(msg)
        return msg

    async def send(self, event_type: str, data: Dict[str, Any]) -> None:
        self.writer.write(
            (json.dumps({"event_type": event_type, "data": data}) + "\n").encode())
        await self.writer.drain()

    async def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


@pytest.fixture
async def wire():
    w = _Wire()
    try:
        yield w
    finally:
        await w.close()


def _router(ctl: _Controller) -> CapabilityRouter:
    return CapabilityRouter(registry=CapabilityRegistry(ctl).hydrate(),
                            target=ctl, provider=cb.HUDConsentProvider())


class TestTheMandatedScenario:
    async def test_the_operator_is_asked_and_the_answer_executes(self, wire):
        """Gate → challenge on the wire → verdict → the capability runs."""
        await wire.start()
        ctl = _Controller()
        router = _router(ctl)

        routed = await router.route("lock_screen", {})

        # 1. The turn was RELEASED, not blocked on a human.
        assert routed.outcome == Outcome.SUSPENDED.value
        assert ctl.locked is False

        # 2. The question actually reached the HUD.
        event = await wire.next_event()
        assert event["event_type"] == cb.CONSENT_REQUEST_EVENT
        challenge = event["data"]
        assert challenge["capability"] == "lock_screen"
        assert challenge["request_id"] == routed.request_id

        # 3. The HUD echoes the challenge, exactly as SecureConsent.respond does.
        resumed = await router.resume(challenge["request_id"], {
            "request_id": challenge["request_id"],
            "nonce": challenge["nonce"],
            "approved": True,
            "status": "APPROVED",
            "capability": challenge["capability"],
        })

        assert resumed.outcome == Outcome.EXECUTED.value, resumed.detail
        assert ctl.locked is True


class TestTheChallenge:
    async def test_the_challenge_carries_the_nonce(self, wire):
        """Minted BEFORE the ask, or the prompt fails closed on the Swift side."""
        await wire.start()
        router = _router(_Controller())

        routed = await router.route("lock_screen", {})
        challenge = (await wire.next_event())["data"]

        assert challenge["nonce"], "challenge went out with no nonce"
        assert challenge["nonce"] == routed.nonce

    async def test_it_says_when_approving_opens_a_session(self, wire):
        """Approving a continuous observer is a different decision. Say so."""
        await wire.start()
        router = _router(_Controller())

        await router.route("start_stream", {})
        challenge = (await wire.next_event())["data"]

        assert challenge["session"] == "start"
        assert "KEEP RUNNING" in challenge["detail"]

    async def test_the_detail_names_the_arguments(self, wire):
        await wire.start()
        router = _router(_Controller())
        await router.route("lock_screen", {"force": True})
        challenge = (await wire.next_event())["data"]
        assert "force" in challenge["detail"]

    async def test_a_provider_refuses_to_ask_without_a_nonce(self):
        """An unbindable verdict is replayable — better to deny than to ask."""
        sent: List[Any] = []
        provider = cb.HUDConsentProvider(
            publish=lambda e, d: (sent.append((e, d)), 1)[1])

        class _Ctx:
            op_id, capability, args, nonce, session = "x", "cap", {}, "", ""
            description = "d"

        assert await provider.request(_Ctx()) == ""
        assert sent == []


class TestFailingClosed:
    async def test_no_hud_connected_denies_rather_than_parks(self, wire):
        """An unasked question is not a pending one."""
        await wire.start()
        await wire.close()
        for _ in range(50):
            if not ipc.connected_clients():
                break
            await asyncio.sleep(0.01)
        router = _router(_Controller())

        routed = await router.route("lock_screen", {})

        assert routed.outcome == Outcome.DENIED.value
        assert routed.request_id == ""
        assert router.pending() == {}

    async def test_a_rejection_does_not_execute(self, wire):
        await wire.start()
        ctl = _Controller()
        router = _router(ctl)
        await router.route("lock_screen", {})
        challenge = (await wire.next_event())["data"]

        out = await router.resume(challenge["request_id"], {
            "nonce": challenge["nonce"], "approved": False,
            "status": "REJECTED"})

        assert out.outcome == Outcome.DENIED.value
        assert ctl.locked is False

    async def test_a_replayed_verdict_without_the_nonce_is_denied(self, wire):
        """Anything that can write to the socket must not be able to approve."""
        await wire.start()
        ctl = _Controller()
        router = _router(ctl)
        await router.route("lock_screen", {})
        challenge = (await wire.next_event())["data"]

        out = await router.resume(challenge["request_id"],
                                  {"approved": True, "status": "APPROVED"})

        assert out.outcome == Outcome.DENIED.value
        assert "nonce" in out.detail
        assert ctl.locked is False

    async def test_a_wrong_nonce_is_denied(self, wire):
        await wire.start()
        ctl = _Controller()
        router = _router(ctl)
        await router.route("lock_screen", {})
        challenge = (await wire.next_event())["data"]

        out = await router.resume(challenge["request_id"], {
            "nonce": "not-the-one", "status": "APPROVED"})

        assert out.outcome == Outcome.DENIED.value
        assert ctl.locked is False

    async def test_an_ungated_capability_never_asks(self, wire):
        await wire.start()
        router = _router(_Controller())

        routed = await router.route("get_battery", {})

        assert routed.outcome == Outcome.EXECUTED.value
        with pytest.raises(asyncio.TimeoutError):
            await wire.next_event(timeout=0.3)

    async def test_disabled_means_deny_never_allow(self, monkeypatch, wire):
        """There is deliberately no setting that ungates a gated capability."""
        await wire.start()
        monkeypatch.setenv("JARVIS_HUD_CONSENT_ENABLED", "0")
        ctl = _Controller()

        routed = await _router(ctl).route("lock_screen", {})

        assert routed.outcome == Outcome.DENIED.value
        assert ctl.locked is False


class TestThePublishPath:
    async def test_it_reaches_every_connected_hud(self, wire):
        await wire.start()
        host, port = "127.0.0.1", wire.port
        r2, w2 = await asyncio.open_connection(host, port)
        for _ in range(50):
            if ipc.connected_clients() >= 2:
                break
            await asyncio.sleep(0.01)

        reached = ipc.publish("ping", {"n": 1})

        assert reached == 2
        assert (await wire.next_event())["event_type"] == "ping"
        line = await asyncio.wait_for(r2.readline(), timeout=5)
        assert json.loads(line.decode())["event_type"] == "ping"
        w2.close()
        await w2.wait_closed()

    async def test_publishing_with_nobody_listening_reports_zero(self):
        assert ipc.publish("ping", {}) == 0

    async def test_a_departed_client_is_dropped(self, wire):
        await wire.start()
        assert ipc.connected_clients() == 1
        await wire.close()
        for _ in range(50):
            if not ipc.connected_clients():
                break
            await asyncio.sleep(0.01)
        assert ipc.connected_clients() == 0

    async def test_publish_is_callable_from_another_thread(self, wire):
        """The router asks from a per-dispatch thread, not the server loop.

        Touching a StreamWriter from a foreign loop is undefined behaviour that
        usually presents as a silently dropped write — which for a consent
        request is an operator who is never prompted.
        """
        await wire.start()
        import threading

        result: Dict[str, int] = {}

        def _from_thread() -> None:
            result["reached"] = ipc.publish("threaded", {"ok": True})

        t = threading.Thread(target=_from_thread)
        t.start()
        t.join(timeout=5)

        assert result["reached"] == 1
        event = await wire.next_event()
        assert event["event_type"] == "threaded"


class TestPrincipalAndLeases:
    async def test_a_client_id_survives_a_reconnect(self, wire):
        """A declared identity keeps its stream across a HUD restart."""
        seen: List[str] = []

        async def _dispatch(event_type: str, data: dict) -> None:
            seen.append(cl.current_principal())

        await wire.start(dispatch=_dispatch)
        await wire.send("action", {"client_id": "stable-hud"})
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0.01)

        assert seen and seen[0] == "hud:stable-hud"

    async def test_a_disconnect_starts_a_grace_window_not_a_reap(self, wire,
                                                                monkeypatch):
        monkeypatch.setenv("JARVIS_CAPABILITY_LEASE_GRACE_S", "600")
        await wire.start()
        book = cl.get_lease_book()
        book.open("cap.start", "cap.stop", owner="peer:127.0.0.1:1")
        book.note_departure("peer:127.0.0.1:1")

        await book.sweep()

        assert book.holders("cap.start") == 1
        assert "peer:127.0.0.1:1" in book.stats()["departing"]


class TestInstall:
    def test_it_wires_the_router_and_is_idempotent(self):
        router = CapabilityRouter()
        assert router._provider is None

        assert cb.install(router) is True
        first = router._provider
        assert isinstance(first, cb.HUDConsentProvider)

        assert cb.install(router) is True
        assert router._provider is first

    def test_disabled_installs_nothing(self, monkeypatch):
        monkeypatch.setenv("JARVIS_HUD_CONSENT_ENABLED", "0")
        router = CapabilityRouter()
        assert cb.install(router) is False
        assert router._provider is None
