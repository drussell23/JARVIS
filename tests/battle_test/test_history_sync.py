"""Distributed history sync — Terminal A types, Terminal B's Up recalls.

The bulletproof contract, end to end over a REAL UDS bridge (not mocks of
the transport — the transport IS the feature):

  * an ``input`` frame from client A reaches the daemon's input sink AND
    fans out as a ``history_append`` to client B;
  * the ORIGINATOR is excluded — A never receives its own echo;
  * the daemon injects memory-only: its history singleton recalls the
    line while the shared history FILE gains nothing (the one disk write
    belongs to the originator's own buffer);
  * client-local lines (``send_history``) take the same route;
  * receivers' ``inject_history_entry`` is memory-only, deduped, and
    blank-refusing.

UDS binds are blocked by some sandboxes — the suite skips there rather
than reporting a false red (same policy as test_ov_rms_stream).
"""
from __future__ import annotations

import asyncio
import os
import socket
import tempfile
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test import history_sync as hs
from backend.core.ouroboros.battle_test import repl_completion as rc


def _uds_dir() -> str:
    """A SHORT socket dir — macOS caps sun_path at 104 bytes and pytest
    tmp_path routinely blows it."""
    return tempfile.mkdtemp(prefix="hsync-", dir="/tmp")


def _can_bind_uds() -> bool:
    try:
        d = _uds_dir()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.bind(os.path.join(d, "probe.sock"))
            return True
        finally:
            s.close()
    except (PermissionError, OSError):
        return False


# --------------------------------------------------------------------------
# 1. injection unit contracts
# --------------------------------------------------------------------------

@pytest.fixture()
def isolated_history(tmp_path, monkeypatch):
    path = tmp_path / "hist"
    monkeypatch.setenv(rc.HISTORY_PATH_ENV_VAR, str(path))
    rc.reset_history_cache_for_tests()
    yield path
    rc.reset_history_cache_for_tests()


def test_inject_is_memory_only(isolated_history) -> None:
    pytest.importorskip("prompt_toolkit")
    hist = rc.build_history()
    assert hs.inject_history_entry("remote-line", history=hist)
    assert hist.get_strings() == ["remote-line"]
    # the ONE disk write belongs to the originator — none happened here
    assert (
        not isolated_history.exists()
        or "remote-line" not in isolated_history.read_text()
    )


def test_inject_dedupes_and_refuses_blanks(isolated_history) -> None:
    pytest.importorskip("prompt_toolkit")
    hist = rc.build_history()
    assert hs.inject_history_entry("x", history=hist)
    assert not hs.inject_history_entry("x", history=hist)
    assert not hs.inject_history_entry("   ", history=hist)
    assert hist.get_strings() == ["x"]


def test_inject_master_flag_off(isolated_history, monkeypatch) -> None:
    pytest.importorskip("prompt_toolkit")
    monkeypatch.setenv(hs.MASTER_FLAG_ENV_VAR, "false")
    hist = rc.build_history()
    assert not hs.inject_history_entry("x", history=hist)
    assert hist.get_strings() == []


def test_injected_entry_reaches_a_fresh_buffer(isolated_history) -> None:
    """The prompt_toolkit lifecycle contract the whole design leans on:
    ``History.load()`` re-yields ``_loaded_strings``, so a buffer built
    (or reset+repainted) after injection recalls the remote line."""
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.buffer import Buffer

    async def scenario() -> str:
        hist = rc.build_history()
        hs.inject_history_entry("typed-in-terminal-A", history=hist)
        buf = Buffer(history=hist)
        buf.load_history_if_not_yet_loaded()
        await asyncio.sleep(0.05)
        buf.history_backward()
        return buf.text

    assert asyncio.run(scenario()) == "typed-in-terminal-A"


# --------------------------------------------------------------------------
# 2. the wire — A → daemon → B, originator excluded, one disk write
# --------------------------------------------------------------------------

async def _wait_for(cond, timeout: float = 3.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while not cond():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("condition not met before timeout")
        await asyncio.sleep(0.01)


@pytest.mark.skipif(not _can_bind_uds(),
                    reason="cannot bind a unix socket in this environment")
def test_history_append_routes_a_to_daemon_to_b(
    isolated_history,
) -> None:
    pytest.importorskip("prompt_toolkit")
    from backend.core.ouroboros.battle_test.cockpit_attach import (
        CockpitAttachBridge,
        CockpitAttachClient,
    )

    sock = Path(_uds_dir()) / "bridge.sock"
    inputs: list = []
    a_got: list = []
    b_got: list = []

    async def scenario() -> None:
        bridge = CockpitAttachBridge(
            on_input=lambda text, session=None: inputs.append(
                (text, session),
            ),
            path=sock,
        )
        assert await bridge.start()
        try:
            client_a = CockpitAttachClient(
                path=sock, on_history_append=a_got.append,
            )
            client_b = CockpitAttachClient(
                path=sock, on_history_append=b_got.append,
            )
            assert await client_a.connect()
            assert await client_b.connect()

            # -- input frame from A fans out to B, never back to A
            assert client_a.send_input("deploy the swarm")
            await _wait_for(lambda: b_got == ["deploy the swarm"])
            await _wait_for(lambda: inputs)
            assert inputs[0] == ("deploy the swarm", client_a.session_id)
            assert a_got == []

            # -- a client-LOCAL line (send_history) takes the same route
            assert client_a.send_history("/deck 12")
            await _wait_for(lambda: "/deck 12" in b_got)
            assert a_got == []

            # -- daemon injected memory-only: singleton recalls, file
            #    gained nothing (the one disk write is the originator's)
            daemon_hist = rc.build_history()
            await _wait_for(
                lambda: "deploy the swarm" in daemon_hist.get_strings(),
            )
            assert "/deck 12" in daemon_hist.get_strings()
            on_disk = (
                isolated_history.read_text()
                if isolated_history.exists() else ""
            )
            assert "deploy the swarm" not in on_disk
            assert "/deck 12" not in on_disk

            # -- daemon-terminal lines broadcast to EVERY client
            bridge.publish_history_append("/posture", origin_session=None)
            await _wait_for(
                lambda: "/posture" in a_got and "/posture" in b_got,
            )
            await client_a.close()
            await client_b.close()
        finally:
            await bridge.stop()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# 3. the daemon-terminal seam + surface wiring pins
# --------------------------------------------------------------------------

def test_daemon_dispatch_fans_out_before_handling() -> None:
    import inspect
    from backend.core.ouroboros.battle_test import serpent_flow
    src = inspect.getsource(serpent_flow.SerpentREPL._dispatch_repl_command)
    assert src.index("history_fanout") < src.index("Built-in commands")


def test_harness_installs_the_fanout_beside_the_markup_mirror() -> None:
    from pathlib import Path as _P
    import backend.core.ouroboros.battle_test.harness as harness
    src = _P(harness.__file__).read_text()
    assert "sf.history_fanout = bridge.publish_history_append" in src


def test_attach_client_mounts_the_injector() -> None:
    from pathlib import Path as _P
    import backend.core.ouroboros.cli.ov as ov
    src = _P(ov.__file__).read_text()
    assert "on_history_append=_build_history_injector()" in src
    # client-local verbs report upstream on every handled branch
    assert src.count("_report_local_history(client, text)") >= 3
