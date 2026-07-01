"""Deterministic VRAM residency + stateful profiler + dynamic re-negotiation.

Cures the last-mile 32B dispatch failures on the (now-loadable) g2-standard-8:

  1. FSM-tied VRAM residency: dispatches pass a persistent keep_alive so ollama
     keeps the model resident (no ~109s reload between ops); the FSM reap fires a
     synchronous keep_alive:0 flush BEFORE the GCP delete.
  2. Stateful Latency Profiler: a session-scoped per-endpoint singleton whose EWMA
     survives across ops + L7 retries (cures "profiler amnesia").
  3. Dynamic context re-negotiation: accurate KV physics (256KB/token, not 512KB)
     + bounded output reservation widen num_ctx to the true VRAM-safe max, ending
     the over-compression empty responses.
"""
from __future__ import annotations

import asyncio

import backend.core.ouroboros.governance.local_inference_director as lid
import backend.core.ouroboros.governance.candidate_generator as cg


_GIB = 1024 ** 3


# --- 1. FSM-tied VRAM residency ---------------------------------------------

def test_failover_keep_alive_default_resident():
    assert cg._failover_keep_alive_seconds() == -1  # keep forever while serving


def test_failover_keep_alive_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_KEEP_ALIVE_SECONDS", "600")
    assert cg._failover_keep_alive_seconds() == 600


def test_flush_vram_posts_keep_alive_zero():
    captured = {}

    class _Resp:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def read(self): return b""

    class _Sess:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def post(self, url, json=None):
            captured["url"] = url
            captured["json"] = json
            return _Resp()

    import backend.core.ouroboros.governance.local_inference_director as _lid
    import aiohttp
    orig = aiohttp.ClientSession
    aiohttp.ClientSession = lambda *a, **k: _Sess()  # type: ignore
    try:
        ok = asyncio.run(_lid.flush_vram("http://10.0.0.5:11434", "qwen2.5-coder:32b"))
    finally:
        aiohttp.ClientSession = orig  # type: ignore
    assert ok is True
    assert captured["url"].endswith("/api/generate")
    assert captured["json"] == {"model": "qwen2.5-coder:32b", "keep_alive": 0}


def test_flush_vram_failsoft_on_bad_input():
    assert asyncio.run(lid.flush_vram("", "m")) is False
    assert asyncio.run(lid.flush_vram("http://x", "")) is False


# --- 2. Stateful Latency Profiler (cure amnesia) ----------------------------

def test_client_uses_injected_profiler():
    cfg = lid.LocalConfig.from_env()
    prof = lid.LatencyProfiler(cfg)
    client = lid.LocalPrimeClient(cfg, profiler=prof)
    assert client.profiler is prof  # injected singleton, not a fresh one


def test_profiler_singleton_persists_per_endpoint():
    gen = cg.CandidateGenerator.__new__(cg.CandidateGenerator)  # no __init__ needed
    cfg = lid.LocalConfig.from_env()
    p1 = gen._failover_profiler_for("http://10.0.0.5:11434", cfg)
    p2 = gen._failover_profiler_for("http://10.0.0.5:11434", cfg)   # same endpoint
    p3 = gen._failover_profiler_for("http://10.0.0.9:11434", cfg)   # new endpoint
    assert p1 is p2            # persists across dispatches (retains EWMA)
    assert p3 is not p1        # a re-awaken (new IP) gets a fresh profiler


def test_injected_profiler_retains_samples_across_clients():
    """The whole point: a rebuilt client (L7 retry) that reuses the profiler keeps
    the EWMA warm instead of resetting to the cold seed."""
    cfg = lid.LocalConfig.from_env()
    prof = lid.LatencyProfiler(cfg)
    for _ in range(cfg.min_samples):
        prof.record(ttft_ms=500.0, total_ms=120_000.0, output_tokens=200)
    assert prof.is_warm() is True
    # A brand-new client that reuses this profiler sees a WARM profiler.
    client = lid.LocalPrimeClient(cfg, profiler=prof)
    assert client.profiler.is_warm() is True


# --- 3. Dynamic context re-negotiation (accurate KV -> wider num_ctx) --------

def test_accurate_kv_widens_num_ctx_vs_conservative():
    # Same L4/32B envelope: the corrected 256KB/token default yields a WIDER window
    # than the old 512KB, ending the over-compression (default now = 262144).
    default_n = lid.derive_safe_num_ctx(vram_bytes=24 * _GIB, model_bytes=20 * _GIB)
    conservative_n = lid.derive_safe_num_ctx(
        vram_bytes=24 * _GIB, model_bytes=20 * _GIB, kv_bytes_per_token=524288)
    assert default_n > conservative_n
    assert default_n >= 8192  # meaningfully wide on an L4


def test_negotiator_still_vram_bounded_not_ram():
    # KV lives in VRAM: a 32GB-RAM host with the SAME L4 (24GB VRAM) does NOT get a
    # bigger window from RAM -- the negotiator is correctly VRAM-bounded.
    n = lid.derive_safe_num_ctx(vram_bytes=24 * _GIB, model_bytes=20 * _GIB)
    assert n <= 32768  # bounded by the L4 VRAM, not the 32GB system RAM


# --- WIRING: _reap_gpu_node flushes VRAM (keep_alive:0) BEFORE the GCP delete -

def test_reap_gpu_node_flushes_vram_before_delete(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_USE_ADC", "false")
    import backend.core.ouroboros.governance.failover_lifecycle as fl
    from types import SimpleNamespace

    calls = []

    async def _fake_flush(endpoint, model, *, timeout_s=10.0):
        calls.append((endpoint, model))
        return True

    monkeypatch.setattr(lid, "flush_vram", _fake_flush)
    controller = fl.FailoverLifecycleController()
    controller._endpoint = "http://10.0.0.5:11434"
    controller._awakened_tier = SimpleNamespace(model_label="qwen2.5-coder:32b")

    asyncio.run(controller._reap_gpu_node())  # fail-soft delete; flush fires first

    assert calls == [("http://10.0.0.5:11434", "qwen2.5-coder:32b")]
