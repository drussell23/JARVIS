"""The Transactional Viewport Lock + Tier-1 safety suite.

The mandated contract, end to end over a REAL UDS bridge:

  * a ``PAUSE_AUTONOMY`` payload suspends the daemon's execution loop —
    a mock worker stops mutating the moment the hold lands, and stays
    stopped until ``RESUME_AUTONOMY``;
  * the hold is a REFCOUNT: two panes' menus keep the world frozen until
    the LAST one closes;
  * safe resumption on every path: explicit resume, client disconnect
    (SIGKILL'd terminal), TTL expiry (wedged holder), and a daemon that
    never answers the menu request (client-side failsafe);
  * ``autonomy_state`` broadcasts so every pane shows the freeze;
  * ``rewind_list`` answers the asking cockpit from the locked snapshot.

Plus the trust dial's cycle contract and the surface-wiring pins for the
bell, ``!`` shell mode, and Shift+Tab.
"""
from __future__ import annotations

import asyncio
import os
import socket
import tempfile
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test import rewind_menu as rm
from backend.core.ouroboros.governance import trust_repl


def _uds_dir() -> str:
    return tempfile.mkdtemp(prefix="alock-", dir="/tmp")


def _can_bind_uds() -> bool:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.bind(os.path.join(_uds_dir(), "probe.sock"))
            return True
        finally:
            s.close()
    except (PermissionError, OSError):
        return False


async def _wait_for(cond, timeout: float = 3.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while not cond():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("condition not met before timeout")
        await asyncio.sleep(0.01)


class _MockOrganism:
    """A background 'execution loop' that mutates state unless paused —
    the thing the viewport lock exists to freeze."""

    def __init__(self) -> None:
        self.paused = False
        self.mutations: list = []
        self.transitions: list = []
        self._task = None

    def on_autonomy(self, action: str) -> None:
        self.transitions.append(action)
        self.paused = action == "pause"

    async def run(self) -> None:
        while True:
            if not self.paused:
                self.mutations.append(len(self.mutations))
            await asyncio.sleep(0.005)


# --------------------------------------------------------------------------
# 1. the mandated core — PAUSE freezes the loop until RESUME
# --------------------------------------------------------------------------

@pytest.mark.skipif(not _can_bind_uds(),
                    reason="cannot bind a unix socket in this environment")
def test_pause_suspends_the_execution_loop_until_resume() -> None:
    pytest.importorskip("prompt_toolkit")
    from backend.core.ouroboros.battle_test.cockpit_attach import (
        CockpitAttachBridge,
        CockpitAttachClient,
    )

    sock = Path(_uds_dir()) / "bridge.sock"
    organism = _MockOrganism()
    a_states: list = []
    b_states: list = []

    async def scenario() -> None:
        bridge = CockpitAttachBridge(
            on_autonomy=organism.on_autonomy,
            rewind_provider=lambda n: [
                {"n": 1, "sha": "abc123def0",
                 "label": "abc123def0 [O+V] feat: x",
                 "is_ov": True, "insertions": 4, "deletions": 1},
            ][:n],
            path=sock,
        )
        assert await bridge.start()
        worker = asyncio.ensure_future(organism.run())
        try:
            a = CockpitAttachClient(
                path=sock, on_autonomy_state=a_states.append,
            )
            b = CockpitAttachClient(
                path=sock, on_autonomy_state=b_states.append,
            )
            assert await a.connect() and await b.connect()

            # -- A pauses: the loop freezes
            await _wait_for(lambda: organism.mutations)
            assert a.send_autonomy("pause")
            await _wait_for(lambda: organism.paused)
            frozen_at = len(organism.mutations)
            await asyncio.sleep(0.08)
            assert len(organism.mutations) == frozen_at, (
                "the execution loop mutated while the viewport lock was held"
            )

            # -- REFCOUNT: B also holds; A's release does not unfreeze
            assert b.send_autonomy("pause")
            await asyncio.sleep(0.02)
            assert a.send_autonomy("resume")
            await asyncio.sleep(0.08)
            assert organism.paused
            assert len(organism.mutations) == frozen_at

            # -- last hold releases: the loop flows again
            assert b.send_autonomy("resume")
            await _wait_for(lambda: not organism.paused)
            await _wait_for(
                lambda: len(organism.mutations) > frozen_at,
            )
            # exactly ONE pause + ONE resume crossed the seam (edges only)
            assert organism.transitions == ["pause", "resume"]

            # -- every pane saw the truth
            await _wait_for(lambda: len(a_states) >= 4 and len(b_states) >= 4)
            assert a_states[0]["paused"] is True
            assert a_states[-1]["paused"] is False
            assert any(s.get("holders") == 2 for s in b_states)

            # -- the locked snapshot answers the requester
            got: list = []
            a2 = CockpitAttachClient(path=sock, on_rewind_list=got.append)
            assert await a2.connect()
            assert a2.send_rewind_request(limit=5)
            await _wait_for(lambda: got)
            assert got[0]["candidates"][0]["sha"] == "abc123def0"
            await a2.close()
            await a.close()
            await b.close()
        finally:
            worker.cancel()
            await bridge.stop()

    asyncio.run(scenario())


@pytest.mark.skipif(not _can_bind_uds(),
                    reason="cannot bind a unix socket in this environment")
def test_disconnect_releases_a_dead_holders_lock() -> None:
    pytest.importorskip("prompt_toolkit")
    from backend.core.ouroboros.battle_test.cockpit_attach import (
        CockpitAttachBridge,
        CockpitAttachClient,
    )

    sock = Path(_uds_dir()) / "bridge.sock"
    organism = _MockOrganism()

    async def scenario() -> None:
        bridge = CockpitAttachBridge(
            on_autonomy=organism.on_autonomy, path=sock,
        )
        assert await bridge.start()
        try:
            a = CockpitAttachClient(path=sock)
            assert await a.connect()
            assert a.send_autonomy("pause")
            await _wait_for(lambda: organism.paused)
            # SIGKILL'd terminal: no resume frame will ever come
            await a.close()
            await _wait_for(lambda: not organism.paused)
            assert organism.transitions == ["pause", "resume"]
        finally:
            await bridge.stop()

    asyncio.run(scenario())


def test_ttl_expiry_releases_a_wedged_holder(monkeypatch) -> None:
    """No sockets needed — the refcount logic is exercised directly."""
    from backend.core.ouroboros.battle_test.cockpit_attach import (
        CockpitAttachBridge,
    )
    monkeypatch.setenv("JARVIS_AUTONOMY_LOCK_TTL_S", "5")
    transitions: list = []
    bridge = CockpitAttachBridge(on_autonomy=transitions.append)
    bridge._autonomy_ctl("pause", "wedged-pane")
    assert transitions == ["pause"]
    # time passes beyond the TTL…
    bridge._autonomy_holds["wedged-pane"] = 0.0
    # …and the next lock event sweeps the corpse and resumes
    bridge._autonomy_ctl("resume", "someone-else")
    assert transitions == ["pause", "resume"]


# --------------------------------------------------------------------------
# 2. the client half — open/deliver/close release exactly once
# --------------------------------------------------------------------------

class _FakeClient:
    def __init__(self) -> None:
        self.autonomy: list = []
        self.rewinds = 0

    def send_autonomy(self, action):
        self.autonomy.append(action)
        return True

    def send_rewind_request(self, limit=10):
        self.rewinds += 1
        return True


def test_controller_open_acquires_and_close_releases_once() -> None:
    async def scenario() -> None:
        client = _FakeClient()
        ctl = rm.RewindController(client)
        assert ctl.open()
        assert client.autonomy == ["pause"] and client.rewinds == 1
        ctl.close()
        ctl.close()   # double-close must not double-release
        assert client.autonomy == ["pause", "resume"]

    asyncio.run(scenario())


def test_controller_empty_delivery_releases() -> None:
    async def scenario() -> None:
        client = _FakeClient()
        ctl = rm.RewindController(client)
        assert ctl.open()
        ctl.deliver({"candidates": []})
        assert client.autonomy == ["pause", "resume"]

    asyncio.run(scenario())


def test_controller_failsafe_releases_when_daemon_never_answers(
    monkeypatch,
) -> None:
    monkeypatch.setenv(rm.REPLY_TIMEOUT_ENV_VAR, "1")

    async def scenario() -> None:
        client = _FakeClient()
        ctl = rm.RewindController(client)
        assert ctl.open()
        await asyncio.sleep(1.3)
        assert client.autonomy == ["pause", "resume"]
        assert not ctl.armed

    asyncio.run(scenario())


def test_rewind_completer_offers_undo_verbs_only_while_armed() -> None:
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.document import Document

    async def scenario() -> list:
        client = _FakeClient()
        ctl = rm.RewindController(client)
        comp = rm.RewindCompleter(ctl)
        assert list(comp.get_completions(Document("", 0), None)) == []
        ctl.open()
        ctl.candidates = [
            {"n": 1, "label": "abc [O+V] feat: x",
             "insertions": 4, "deletions": 1},
            {"n": 2, "label": "def [O+V] fix: y",
             "insertions": 2, "deletions": 2},
        ]
        return list(comp.get_completions(Document("", 0), None))

    completions = asyncio.run(scenario())
    assert [c.text for c in completions] == ["/undo 1", "/undo 2"]


def test_master_flag_off_takes_no_hold(monkeypatch) -> None:
    monkeypatch.setenv(rm.MASTER_FLAG_ENV_VAR, "false")
    client = _FakeClient()
    ctl = rm.RewindController(client)
    assert ctl.open() is False
    assert client.autonomy == []


# --------------------------------------------------------------------------
# 3. the trust dial
# --------------------------------------------------------------------------

@pytest.fixture()
def clean_floor(monkeypatch):
    monkeypatch.delenv("JARVIS_MIN_RISK_TIER", raising=False)
    yield
    os.environ.pop("JARVIS_MIN_RISK_TIER", None)


def test_trust_cycle_walks_the_dial(clean_floor) -> None:
    assert trust_repl.current_floor() == "safe_auto"
    trust_repl.dispatch_trust_command("/trust cycle")
    assert trust_repl.current_floor() == "notify_apply"
    trust_repl.dispatch_trust_command("/trust cycle")
    assert trust_repl.current_floor() == "approval_required"
    trust_repl.dispatch_trust_command("/trust cycle")
    assert trust_repl.current_floor() == "safe_auto"
    assert os.environ.get("JARVIS_MIN_RISK_TIER") is None


def test_trust_floor_feeds_the_real_gate(clean_floor) -> None:
    trust_repl.dispatch_trust_command("/trust notify_apply")
    from backend.core.ouroboros.governance.risk_tier_floor import _env_floor
    assert _env_floor() == "notify_apply"


def test_trust_chip_quiet_at_rest_loud_when_raised(clean_floor) -> None:
    assert trust_repl.floor_chip() == ""
    trust_repl.dispatch_trust_command("/trust approval_required")
    assert "approval_required" in trust_repl.floor_chip()


def test_status_line_carries_the_chip(clean_floor) -> None:
    import inspect
    from backend.core.ouroboros.battle_test import status_line
    src = inspect.getsource(status_line.StatusLineBuilder.render_plain)
    assert "floor_chip" in src


# --------------------------------------------------------------------------
# 4. surface wiring pins — bell, shell mode, shift+tab
# --------------------------------------------------------------------------

def _ov_src() -> str:
    import backend.core.ouroboros.cli.ov as ov
    return Path(ov.__file__).read_text()


def test_gate_arrival_rings_the_bell() -> None:
    src = _ov_src()
    block = src.split("def _on_prompt_frame")[1]
    assert "_ring_gate_bell()" in block.split("def ")[0]


def test_shell_mode_and_client_actions_are_wired() -> None:
    src = _ov_src()
    assert "_run_client_shell(ui, client, text)" in src
    assert '"app:cycleTrust", ("shift+tab",)' in src
    assert '"app:help", ("?",)' in src
    assert '"chat:externalEditor", ("ctrl+g",)' in src
    assert "install_rewind_binding" in src
    # mounted on BOTH surfaces
    assert src.count("_client_extra_bindings(ui, client)") >= 2
    assert src.count("merge_rewind_completer(") >= 2
