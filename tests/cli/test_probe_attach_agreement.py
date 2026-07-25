"""The ignition probe must never vouch for more than attach can consume.

The contradiction this closes, from a live session:

    ⏺ organism already waking — waiting for it to serve
    ⏺ organism live — attaching
    no organism awake — nothing to attach to.

`await_socket` probed with `deep=True` and got "live"; the attach that
followed immediately called the same daemon dead. The probe demanded ONE
BYTE; `CockpitAttachClient._connect_once` demands a COMPLETE LINE that PARSES
as JSON, and anything else lands in its except-clause as "dead" — which fails
fast with no retry.

`ov_doctor` already states the law: no probe may be weaker than the contract
it vouches for. These tests enforce it by construction — every server
behaviour is fed to BOTH consumers and their verdicts must agree. A future
edit that deepens one without the other fails here rather than in a terminal.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test.cockpit_attach import CockpitAttachClient
from backend.core.ouroboros.cli.thin_client import probe_socket


def _sock() -> Path:
    # mkdtemp, not tmp_path: macOS caps sun_path at ~104 bytes and pytest's
    # per-test directory names blow past it.
    return Path(tempfile.mkdtemp(prefix="ovpa")) / "a.sock"


async def _serve(path: Path, behaviour):
    """Start a bridge-shaped server whose first frame is `behaviour`."""
    async def _handler(reader, writer):
        try:
            await behaviour(writer)
        except Exception:  # noqa: BLE001
            pass

    try:
        return await asyncio.start_unix_server(_handler, path=str(path))
    except (PermissionError, OSError) as exc:
        pytest.skip(f"cannot bind a unix socket here: {exc}")


async def _both(path: Path):
    """(probe verdict, attach succeeded) for the same live server."""
    verdict = await probe_socket(path, timeout=1.0, deep=True)
    client = CockpitAttachClient(path=path)
    try:
        ok = await asyncio.wait_for(client.connect(), timeout=6.0)
    except asyncio.TimeoutError:
        ok = False
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass
    return verdict, ok


# ---------------------------------------------------------------------------
# Agreement across every first-frame behaviour
# ---------------------------------------------------------------------------


async def test_a_complete_hydration_frame_satisfies_both():
    """Positive control. Without it, every agreement test below could pass
    simply because the probe always said 'not live'."""
    path = _sock()

    async def _good(writer):
        writer.write((json.dumps({"type": "hydration", "audio": {}}) + "\n").encode())
        await writer.drain()
        await asyncio.sleep(5)

    server = await _serve(path, _good)
    try:
        verdict, ok = await _both(path)
        assert verdict == "live" and ok is True
    finally:
        server.close()


async def test_a_partial_frame_is_not_vouched_for():
    """THE REGRESSION. Bytes without a newline satisfied `read(1)` and then
    failed the attach's `readline()` — the exact live contradiction."""
    path = _sock()

    async def _partial(writer):
        writer.write(b'{"type": "hydra')       # no newline, ever
        await writer.drain()
        await asyncio.sleep(5)

    server = await _serve(path, _partial)
    try:
        verdict, ok = await _both(path)
        assert ok is False, "attach unexpectedly succeeded — retune the test"
        assert verdict != "live", (
            "the probe vouched 'live' for a daemon the attach calls dead — "
            "this is the exact contradiction the operator saw"
        )
    finally:
        server.close()


async def test_an_unparseable_frame_is_not_vouched_for():
    """A complete line that isn't JSON lands in the attach's except-clause as
    'dead'. The probe must not call that live."""
    path = _sock()

    async def _garbage(writer):
        writer.write(b"not json at all\n")
        await writer.drain()
        await asyncio.sleep(5)

    server = await _serve(path, _garbage)
    try:
        verdict, ok = await _both(path)
        assert ok is False
        assert verdict != "live"
    finally:
        server.close()


async def test_silence_is_booting_for_both():
    """Accepted but never served: a boot-starved loop. Neither may call it
    live, and the probe must say 'booting' (wait) rather than 'stale'."""
    path = _sock()

    async def _silent(writer):
        await asyncio.sleep(5)

    server = await _serve(path, _silent)
    try:
        verdict, ok = await _both(path)
        assert verdict == "booting", f"got {verdict!r}"
        assert ok is False
    finally:
        server.close()


async def test_immediate_eof_is_never_reported_live():
    path = _sock()

    async def _hangup(writer):
        writer.close()

    server = await _serve(path, _hangup)
    try:
        verdict, ok = await _both(path)
        assert verdict != "live" and ok is False
    finally:
        server.close()


# ---------------------------------------------------------------------------
# The dangerous direction
# ---------------------------------------------------------------------------


async def test_a_live_but_broken_bridge_is_never_called_stale():
    """"stale" authorises UNLINKING the socket, and the bridge binds ONCE at
    boot — cleaning a live organism's socket makes it permanently
    unattachable (the 2026-07-23 class). A misbehaving server must be waited
    on, never cleaned."""
    path = _sock()

    async def _garbage(writer):
        writer.write(b"}{\n")
        await writer.drain()
        await asyncio.sleep(5)

    server = await _serve(path, _garbage)
    try:
        assert await probe_socket(path, timeout=1.0, deep=True) != "stale"
    finally:
        server.close()


async def test_a_refused_socket_is_stale_not_booting(tmp_path):
    """The opposite error: waiting forever on a corpse. Only a kernel refusal
    proves nobody is home."""
    corpse = tmp_path / "corpse.sock"
    corpse.write_bytes(b"")
    assert await probe_socket(corpse, timeout=0.5, deep=True) == "stale"


async def test_absent_is_absent(tmp_path):
    assert await probe_socket(tmp_path / "nope.sock", deep=True) == "absent"


# ---------------------------------------------------------------------------
# Structural pin
# ---------------------------------------------------------------------------


def test_the_deep_probe_parses_rather_than_peeking():
    """A byte-peek cannot detect a partial or malformed frame. If this
    reverts, the probe silently starts over-promising again."""
    import inspect

    src = inspect.getsource(probe_socket)
    assert "readline()" in src, "the deep probe stopped reading a full frame"
    assert "json.loads" in src, "the deep probe stopped parsing the frame"
    assert "r.read(1)" not in src, "the one-byte peek is back"
