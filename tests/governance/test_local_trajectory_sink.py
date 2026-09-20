"""The preference pair survives Reactor-Core not being there.

Reactor-Core is ``http://localhost:8090`` -- a LOCAL service, not a cloud
endpoint. Nothing in this path was ever going off-host. On a machine that
does not run it, the client's health check returns False and every
``stream_experience`` returns False in 0.0001s without touching a socket.

So the offline story was already SAFE. It was not USEFUL: the pair -- a
candidate that failed validation beside the one that fixed it, the richest
training signal this system produces -- was scored, discarded and forgotten.
Arming the emitter against an absent service bought a log line.

Measured costs these tests pin: the client's health ladder took **10.61s**
to conclude a dead localhost:8090 was dead; a TCP connect answers in 0.25s.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from backend.core.ouroboros.governance.local_trajectory_sink import (
    corpus_path,
    corpus_stats,
    endpoint_reachable,
    probe_timeout_s,
    sink_enabled,
    write_pair,
)

EVENT = {
    "event_type": "correction",
    "assistant_output": "x = 2\n",
    "original_response": "x = 1\n",
    "metadata": {"op_id": "t1"},
}


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dead_port_is_unreachable_fast():
    """The whole point: milliseconds, not the 10.61s ladder."""
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    assert await endpoint_reachable("http://localhost:1", timeout_s=0.25) is False
    assert loop.time() - t0 < 2.0


@pytest.mark.asyncio
async def test_a_listening_port_is_reachable():
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert await endpoint_reachable(f"http://127.0.0.1:{port}") is True
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["", "not a url", "http://", "://x", None])
async def test_malformed_urls_are_unreachable_not_fatal(url):
    assert await endpoint_reachable(url or "") is False


@pytest.mark.asyncio
async def test_unresolvable_host_is_unreachable():
    assert await endpoint_reachable(
        "http://no-such-host.invalid:8090", timeout_s=0.5,
    ) is False


def test_probe_timeout_is_configurable(monkeypatch):
    monkeypatch.setenv("JARVIS_REACTOR_PROBE_TIMEOUT_S", "1.5")
    assert probe_timeout_s() == 1.5


def test_nonsense_probe_timeout_falls_back(monkeypatch):
    monkeypatch.setenv("JARVIS_REACTOR_PROBE_TIMEOUT_S", "banana")
    assert probe_timeout_s() > 0


# ---------------------------------------------------------------------------
# Keeping the pair
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pair_is_written(tmp_path):
    target = tmp_path / "dpo.jsonl"
    result = await write_pair(EVENT, path=target)
    assert result.written is True
    row = json.loads(target.read_text().splitlines()[0])
    assert row["event"]["metadata"]["op_id"] == "t1"


@pytest.mark.asyncio
async def test_pairs_append_rather_than_replace(tmp_path):
    target = tmp_path / "dpo.jsonl"
    for i in range(5):
        await write_pair({**EVENT, "n": i}, path=target)
    assert corpus_stats(target)["rows"] == 5


@pytest.mark.asyncio
async def test_corpus_is_owner_only(tmp_path):
    """It holds candidate source. A corpus is not less sensitive for being
    local."""
    target = tmp_path / "dpo.jsonl"
    await write_pair(EVENT, path=target)
    assert oct(target.stat().st_mode & 0o777) == "0o600"


@pytest.mark.asyncio
async def test_unserialisable_payload_is_reported_not_raised(tmp_path):
    result = await write_pair({"obj": object()}, path=tmp_path / "dpo.jsonl")
    assert result.written is True or result.reason


@pytest.mark.asyncio
async def test_disabled_sink_keeps_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_LOCAL_TRAJECTORY_SINK_ENABLED", "false")
    result = await write_pair(EVENT, path=tmp_path / "dpo.jsonl")
    assert result.written is False
    assert result.reason == "sink_disabled"
    assert sink_enabled() is False


@pytest.mark.asyncio
async def test_unwritable_path_degrades_rather_than_raising(tmp_path):
    target = tmp_path / "nope"
    target.mkdir()
    result = await write_pair(EVENT, path=target)
    assert result.written is False


@pytest.mark.asyncio
async def test_corpus_rotates_when_it_outgrows_its_ceiling(tmp_path, monkeypatch):
    """An append-only corpus on a multi-day soak outgrows the disk."""
    monkeypatch.setenv("JARVIS_DPO_CORPUS_ROTATE_BYTES", "400")
    monkeypatch.setenv("JARVIS_DPO_CORPUS_KEEP", "2")
    target = tmp_path / "dpo.jsonl"
    for i in range(40):
        await write_pair({**EVENT, "pad": "x" * 100, "n": i}, path=target)
    assert (tmp_path / "dpo.jsonl.1").exists()


# ---------------------------------------------------------------------------
# Stats, for deciding whether there is yet enough to train on
# ---------------------------------------------------------------------------


def test_stats_on_a_missing_corpus_are_zero(tmp_path):
    stats = corpus_stats(tmp_path / "absent.jsonl")
    assert stats["exists"] is False
    assert stats["rows"] == 0


@pytest.mark.asyncio
async def test_stats_count_rows_and_bytes(tmp_path):
    target = tmp_path / "dpo.jsonl"
    await write_pair(EVENT, path=target)
    stats = corpus_stats(target)
    assert stats["rows"] == 1
    assert stats["bytes"] > 0


def test_corpus_path_is_configurable(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DPO_CORPUS_PATH", str(tmp_path / "x.jsonl"))
    assert corpus_path() == tmp_path / "x.jsonl"


def test_corpus_defaults_beside_the_other_ledgers(monkeypatch):
    monkeypatch.delenv("JARVIS_DPO_CORPUS_PATH", raising=False)
    assert ".ouroboros" in str(corpus_path())


# ---------------------------------------------------------------------------
# The emitter path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emitter_keeps_the_pair_when_nothing_is_listening(tmp_path, monkeypatch):
    """The behaviour this module exists for: a dead endpoint must not cost
    the training signal."""
    monkeypatch.setenv("JARVIS_DPO_CORPUS_PATH", str(tmp_path / "dpo.jsonl"))
    monkeypatch.setenv("REACTOR_CORE_API_URL", "http://localhost:1")

    from backend.core.ouroboros.governance.repair_trajectory_emitter import (
        RepairTrajectoryEmitter,
    )
    assert await RepairTrajectoryEmitter()._send(dict(EVENT)) is True
    assert corpus_stats(tmp_path / "dpo.jsonl")["rows"] == 1


@pytest.mark.asyncio
async def test_secrets_are_scrubbed_before_they_reach_the_corpus(tmp_path, monkeypatch):
    """The redactor runs before the sink, so a secret that would not be
    SENT is also not STORED."""
    monkeypatch.setenv("JARVIS_DPO_CORPUS_PATH", str(tmp_path / "dpo.jsonl"))
    monkeypatch.setenv("REACTOR_CORE_API_URL", "http://localhost:1")

    from backend.core.ouroboros.governance.repair_trajectory_emitter import (
        RepairTrajectoryEmitter,
    )
    await RepairTrajectoryEmitter()._send({
        **EVENT, "assistant_output": 'API_KEY = "sk-aaaaaaaaaaaaaaaaaaaa"\n',
    })
    body = (tmp_path / "dpo.jsonl").read_text()
    assert "sk-aaaa" not in body
    assert "REDACTED" in body
