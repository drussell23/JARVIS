"""Asynchronous Inter-Token Watchdog (Stream Breaker) + stream buffering.

Deprecates the total-duration timeout for the heavy 32B path: the model streams,
and each token chunk resets a per-chunk inter-token deadline. A model that keeps
emitting runs INDEFINITELY (total duration is not a kill condition); only a genuine
STALL (silence > inter-token timeout) trips InterTokenStall. The deltas are buffered
(and yielded to stdout) and assembled into the final response.
"""
from __future__ import annotations

import asyncio
import dataclasses

import pytest

import backend.core.ouroboros.governance.local_inference_director as lid


def _cfg(**over):
    return dataclasses.replace(lid.LocalConfig.from_env(), **over)


def _sse(content: str) -> bytes:
    import json
    return ("data: " + json.dumps({"choices": [{"delta": {"content": content}}]}) + "\n").encode()


class _Reader:
    def __init__(self, lines, gaps=None):
        self.lines = list(lines)
        self.gaps = list(gaps or [0] * len(lines))
        self.i = 0

    async def readline(self):
        if self.i >= len(self.lines):
            return b""
        g = self.gaps[self.i] if self.i < len(self.gaps) else 0
        if g:
            await asyncio.sleep(g)
        ln = self.lines[self.i]
        self.i += 1
        return ln


class _Resp:
    def __init__(self, reader):
        self.content = reader

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Sess:
    def __init__(self, reader):
        self._r = reader
        self.posted = []

    def post(self, url, **kw):
        self.posted.append(kw)
        return _Resp(self._r)

    async def close(self):
        pass


# --- SSE parser -------------------------------------------------------------

def test_parse_sse_delta_content():
    assert lid._parse_sse_delta(_sse("Hello")) == "Hello"


def test_parse_sse_delta_done_and_noise():
    assert lid._parse_sse_delta(b"data: [DONE]\n") is lid._SSE_DONE
    assert lid._parse_sse_delta(b": keep-alive\n") is None
    assert lid._parse_sse_delta(b"\n") is None
    assert lid._parse_sse_delta(b"data: {not json}\n") is None


def test_env_defaults():
    assert lid._inter_token_timeout_s() == 30.0
    assert lid._streaming_enabled() is True


# --- streaming assembles the buffered deltas --------------------------------

def test_streaming_assembles_full_text():
    lines = [_sse("Hel"), _sse("lo, "), _sse("world"), b"data: [DONE]\n"]
    prof = lid.LatencyProfiler(_cfg(num_ctx=8192))
    client = lid.LocalPrimeClient(_cfg(num_ctx=8192), session=_Sess(_Reader(lines)), profiler=prof)
    lc = asyncio.run(client.complete(system="s", user="u", prompt_tokens=10, stream=True))
    assert lc.text == "Hello, world"
    assert len(prof._total) == 1          # a REAL latency sample was recorded


def test_streaming_runs_while_emitting_no_total_cap(monkeypatch):
    """Steady slow tokens (each gap < inter-token) complete even though the TOTAL
    duration would blow a small total budget -- total duration is not the killer."""
    monkeypatch.setenv("JARVIS_LOCAL_INTER_TOKEN_TIMEOUT_S", "0.5")
    lines = [_sse("a"), _sse("b"), _sse("c"), _sse("d"), b"data: [DONE]\n"]
    gaps = [0.1, 0.1, 0.1, 0.1, 0.0]      # 0.4s total, each gap < 0.5s inter-token
    client = lid.LocalPrimeClient(_cfg(num_ctx=8192), session=_Sess(_Reader(lines, gaps)))
    lc = asyncio.run(client.complete(system="s", user="u", prompt_tokens=10, stream=True))
    assert lc.text == "abcd"


def test_inter_token_stall_trips_fast(monkeypatch):
    monkeypatch.setenv("JARVIS_LOCAL_INTER_TOKEN_TIMEOUT_S", "0.2")
    lines = [_sse("Hel"), _sse("lo")]
    gaps = [0.0, 100.0]                    # 2nd chunk silent for 100s -> stall
    import time
    client = lid.LocalPrimeClient(_cfg(num_ctx=8192), session=_Sess(_Reader(lines, gaps)))
    start = time.monotonic()
    with pytest.raises(lid.InterTokenStall):
        asyncio.run(client.complete(system="s", user="u", prompt_tokens=10, stream=True))
    assert time.monotonic() - start < 2.0   # tripped fast, not 100s


def test_complete_guarded_heavy_path_streams(monkeypatch):
    """complete_guarded on the heavy (num_ctx) path uses streaming (no total-duration
    wait_for) -- proven by a stream slower than any adaptive budget still completing."""
    monkeypatch.setenv("JARVIS_LOCAL_INTER_TOKEN_TIMEOUT_S", "0.5")
    monkeypatch.setenv("JARVIS_LOCAL_INFERENCE_TIMEOUT_SEED_MS", "10")  # tiny total seed
    lines = [_sse("x"), _sse("y"), _sse("z"), b"data: [DONE]\n"]
    gaps = [0.1, 0.1, 0.1, 0.0]           # 0.3s > 0.01s seed, but streams fine
    client = lid.LocalPrimeClient(_cfg(num_ctx=8192), session=_Sess(_Reader(lines, gaps)))
    lc = asyncio.run(client.complete_guarded(system="s", user="u", prompt_tokens=10))
    assert lc.text == "xyz"


def test_inter_token_stall_is_not_l7_recoverable():
    import backend.core.ouroboros.governance.candidate_generator as cg
    assert cg._is_l7_recoverable(lid.InterTokenStall("stall")) is False
