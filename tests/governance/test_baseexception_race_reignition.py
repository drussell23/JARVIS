"""Strict exception elevation + preemptive async race — the local repro harness.

Proves, deterministically + locally (no cloud), that:
  1. GracefulStreamInterruption is a BaseException (not Exception) -> it PIERCES the
     Venom tool loop's `except Exception` blocks and reaches the checkpoint boundary.
  2. The streaming generator RACES readline vs the shutdown event -> the instant the
     signal fires it drops the I/O and yields the partial (zero-latency, not up to
     the 30s inter-token window).
  3. End-to-end: a cooperatively-interrupted dispatch writes a signed checkpoint with
     the partial to .ouroboros -- even when the intermediate layer swallows Exception.
"""
from __future__ import annotations

import asyncio
import dataclasses
import time

import pytest

import backend.core.ouroboros.governance.local_inference_director as lid
import backend.core.ouroboros.governance.cooperative_shutdown as coop
import backend.core.ouroboros.governance.fsm_checkpoint as ckpt


def _cfg(**over):
    return dataclasses.replace(lid.LocalConfig.from_env(), **over)


def _sse(content):
    import json
    return ("data: " + json.dumps({"choices": [{"delta": {"content": content}}]}) + "\n").encode()


# --- 1. Exception hierarchy elevation ---------------------------------------

def test_graceful_interruption_is_baseexception_not_exception():
    assert issubclass(lid.GracefulStreamInterruption, BaseException)
    assert not issubclass(lid.GracefulStreamInterruption, Exception)


def test_graceful_interruption_pierces_except_exception():
    """The Earmuff Bypass: a standard `except Exception` (as in the Venom tool loop)
    does NOT swallow it -- it propagates straight through."""
    swallowed = {"n": 0}

    def _tool_loop_round():
        try:
            raise lid.GracefulStreamInterruption("freeze", partial="x")
        except Exception:  # noqa: BLE001 -- mimics the tool loop's per-round guard
            swallowed["n"] += 1
            return "swallowed"

    with pytest.raises(lid.GracefulStreamInterruption):
        _tool_loop_round()
    assert swallowed["n"] == 0   # never entered the except Exception block


# --- 2. Preemptive async race (zero-latency interruption) -------------------

class _BlockingReader:
    """readline() blocks ~forever -- simulates the 32B mid-generation (slow chunk)."""
    async def readline(self):
        await asyncio.sleep(9999)
        return b""


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


def test_async_race_interrupts_blocked_read_instantly():
    coop.reset()
    client = lid.LocalPrimeClient(_cfg(num_ctx=8192), session=_Sess(_BlockingReader()))

    async def _run():
        # Fire the shutdown 0.2s in, while readline is blocked for 9999s.
        async def _fire():
            await asyncio.sleep(0.2)
            coop.request("wall_clock_cap")
        asyncio.ensure_future(_fire())
        start = time.monotonic()
        try:
            await client.complete(system="s", user="u", prompt_tokens=10, stream=True,
                                  prefill="partial so far")
            return None, 0.0
        except lid.GracefulStreamInterruption as e:
            return e.partial, time.monotonic() - start

    partial, elapsed = asyncio.run(_run())
    coop.reset()
    assert partial == "partial so far"      # buffered partial preserved
    assert elapsed < 2.0                    # interrupted at the signal, NOT after 30s/9999s


def test_race_normal_stream_still_completes():
    coop.reset()
    lines = [_sse("hi"), b"data: [DONE]\n"]
    r = type("R", (), {"i": 0, "lines": lines})()
    async def _readline(self=r):
        if self.i >= len(self.lines):
            return b""
        ln = self.lines[self.i]; self.i += 1; return ln
    r.readline = _readline
    client = lid.LocalPrimeClient(_cfg(num_ctx=8192), session=_Sess(r))
    lc = asyncio.run(client.complete(system="s", user="u", prompt_tokens=10, stream=True))
    assert lc.text == "hi"


# --- 3. End-to-end local repro: pierce + checkpoint written -----------------

def test_local_dispatch_checkpoints_on_interruption(tmp_path, monkeypatch):
    """The full local proof: a dispatch whose generation raises GSI THROUGH an
    intermediate `except Exception` (the tool-loop mimic) still writes a signed
    checkpoint (with the partial) to .ouroboros. No cloud, deterministic."""
    monkeypatch.setenv("JARVIS_JPRIME_DISPATCH_READY_ENABLED", "false")  # gate predates this test
    monkeypatch.setenv("JARVIS_CHECKPOINT_DIR", str(tmp_path / "cp"))
    monkeypatch.setenv("JARVIS_CHECKPOINT_HMAC_SECRET", "local-secret")
    import backend.core.ouroboros.governance.candidate_generator as cg
    import backend.core.ouroboros.governance.local_inference_director as lidmod
    import backend.core.ouroboros.governance.providers as provmod
    from types import SimpleNamespace
    import datetime as _dt

    class _FakeClient:
        def __init__(self, cfg, session=None, profiler=None):
            self._resume_prefill = ""
        async def warmup(self, *, timeout_s):
            return True
        async def aclose(self):
            pass

    class _FakeProvider:
        def __init__(self, client, repo_root=None, **_kw):  # tool_loop/mcp_client (venom wiring)
            pass
        async def generate(self, context, deadline):
            # Mimic the Venom tool loop swallowing Exception -- GSI (BaseException)
            # must pierce it.
            try:
                raise lidmod.GracefulStreamInterruption("freeze", partial="def foo(): retu")
            except Exception:  # noqa: BLE001
                return SimpleNamespace(candidates=("should-not-reach",))

    monkeypatch.setattr(lidmod, "LocalPrimeClient", _FakeClient)
    monkeypatch.setattr(provmod, "PrimeProvider", _FakeProvider)

    class _Stub:
        _repo_root = None
        def _remaining_seconds(self, dl):
            return 60.0
        async def _resolve_dispatch_model_name(self, ep):
            return "qwen2.5-coder:32b"
        async def _negotiate_num_ctx(self, ep):
            return 8192
        def _failover_profiler_for(self, ep, cfg):
            return lidmod.LatencyProfiler(cfg)

    dl = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=60)
    ctx = SimpleNamespace(op_id="op-frozen", phase="GENERATE", description="fix",
                          target_files=("m.py",), intake_evidence_json="", provider_route="standard")

    with pytest.raises(BaseException):  # noqa: B017 -- GSI or the converted terminal
        asyncio.run(cg.CandidateGenerator._failover_local_dispatch(_Stub(), ctx, dl, "http://n:11434"))

    pend = ckpt.list_pending(base_dir=None)
    assert any(c.op_id == "op-frozen" for c in pend)
    cp = [c for c in pend if c.op_id == "op-frozen"][0]
    assert cp.partial_completion == "def foo(): retu"   # partial preserved via checkpoint
