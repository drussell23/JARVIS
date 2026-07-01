"""Autonomous Context-Hardware Negotiator + Dynamic Cognitive Compression.

Solves the VRAM/KV-cache OOM (warm 32B ServerDisconnect on an L4) via software
intelligence, not hardware scaling:

  1. `derive_safe_num_ctx` mathematically derives the max safe context window from
     the MEASURED VRAM buffer (VRAM - model_size - overhead) / kv_bytes_per_token.
     No static cap -- a bigger GPU or smaller model yields a bigger window
     automatically.
  2. `accelerator_vram_bytes` maps a GCP accelerator to its VRAM (env-overridable).
  3. `fit_prompt_to_window` is the sliding-window Cognitive Compression: preserve
     the system prompt (Iron Gate rules) IN FULL + the recent tail (latest tool
     outputs), compress the older intermediate middle, GUARANTEE the result fits.
"""
from __future__ import annotations

import backend.core.ouroboros.governance.local_inference_director as lid
import backend.core.ouroboros.governance.failover_tier as ft


_GIB = 1024 ** 3


# --- Negotiator: derive safe num_ctx from measured VRAM buffer ---------------

def test_negotiator_derives_from_vram_buffer():
    # L4 24GiB, model ~20GiB, 1.5GB overhead, 512KB/token KV -> a positive window.
    n = lid.derive_safe_num_ctx(
        vram_bytes=24 * _GIB, model_bytes=20 * _GIB,
        kv_bytes_per_token=524288, overhead_bytes=1_500_000_000,
        floor=2048, ceiling=32768,
    )
    # buffer = 24GiB - 20GiB - 1.5GB ~= 2.79GB ; /512KB ~= 5300 -> clamp, /256 round
    assert 2048 <= n <= 32768
    assert n % 256 == 0
    assert 4000 <= n <= 7000  # sane L4 window for a 32B


def test_negotiator_bigger_gpu_yields_bigger_window():
    small = lid.derive_safe_num_ctx(vram_bytes=24 * _GIB, model_bytes=20 * _GIB,
                                    kv_bytes_per_token=524288, overhead_bytes=1_500_000_000,
                                    floor=2048, ceiling=131072)
    big = lid.derive_safe_num_ctx(vram_bytes=80 * _GIB, model_bytes=20 * _GIB,
                                  kv_bytes_per_token=524288, overhead_bytes=1_500_000_000,
                                  floor=2048, ceiling=131072)
    assert big > small  # adaptive: more VRAM -> more context, no code change


def test_negotiator_floors_when_no_buffer():
    # Model bigger than VRAM -> negative buffer -> floor (never negative / crash).
    n = lid.derive_safe_num_ctx(vram_bytes=16 * _GIB, model_bytes=20 * _GIB,
                                kv_bytes_per_token=524288, overhead_bytes=1_500_000_000,
                                floor=2048, ceiling=32768)
    assert n == 2048


def test_negotiator_clamps_to_ceiling():
    n = lid.derive_safe_num_ctx(vram_bytes=200 * _GIB, model_bytes=20 * _GIB,
                                kv_bytes_per_token=524288, overhead_bytes=1_500_000_000,
                                floor=2048, ceiling=8192)
    assert n == 8192


def test_negotiator_failsoft_on_bad_input():
    assert lid.derive_safe_num_ctx(vram_bytes=0, model_bytes=0,
                                   kv_bytes_per_token=1, overhead_bytes=0,
                                   floor=2048, ceiling=32768) == 2048


# --- VRAM lookup ------------------------------------------------------------

def test_vram_lookup_known_accelerators():
    assert ft.accelerator_vram_bytes("nvidia-l4") == 24 * _GIB
    assert ft.accelerator_vram_bytes("nvidia-a100") == 40 * _GIB


def test_vram_lookup_unknown_is_zero():
    assert ft.accelerator_vram_bytes("nvidia-unobtainium") == 0
    assert ft.accelerator_vram_bytes("") == 0


def test_vram_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_GPU_VRAM_GIB", "48")
    assert ft.accelerator_vram_bytes("nvidia-l4") == 48 * _GIB


# --- Cognitive Compression sliding window -----------------------------------

def test_fit_noop_when_within_window():
    sys_p = "SYSTEM RULES"
    usr = "short user payload"
    s, u, compressed = lid.fit_prompt_to_window(sys_p, usr, max_tokens=10_000)
    assert s == sys_p and u == usr and compressed is False


def test_fit_preserves_system_and_tail_compresses_middle():
    sys_p = "IRON GATE: explore >=2 before patch. " * 5
    # Build a large user payload: HEAD (task) + huge middle + TAIL (recent tools).
    head = "TASK: fix the bug in module X.\n"
    middle = "OLD TOOL RESULT LINE\n" * 5000
    tail = "\nRECENT read_file(x.py) -> def foo(): ...\n"
    usr = head + middle + tail
    s, u, compressed = lid.fit_prompt_to_window(sys_p, usr, max_tokens=1024)
    assert compressed is True
    assert s == sys_p                                   # system preserved IN FULL
    assert "TASK: fix the bug" in u                     # head preserved
    assert "RECENT read_file(x.py)" in u                # recent tail preserved
    assert "compression" in u.lower()                   # deterministic marker present
    # GUARANTEE it fits the window (system + user <= max_tokens).
    assert lid.estimate_tokens(s) + lid.estimate_tokens(u) <= 1024


def test_fit_guarantees_fit_even_when_system_dominates():
    sys_p = "S" * 40_000  # system alone exceeds the window
    usr = "U" * 40_000
    s, u, compressed = lid.fit_prompt_to_window(sys_p, usr, max_tokens=1024)
    assert s == sys_p            # never cut the system (Iron Gate rules)
    assert compressed is True
    assert lid.estimate_tokens(u) <= 1024  # user reduced to (near) nothing


def test_estimate_tokens_monotonic():
    assert lid.estimate_tokens("") == 0
    assert lid.estimate_tokens("abcd") == 1
    assert lid.estimate_tokens("x" * 400) == 100


# --- candidate_generator: /api/tags size + negotiation + L7 auto-heal --------

import asyncio  # noqa: E402
import datetime as _dt  # noqa: E402

import backend.core.ouroboros.governance.candidate_generator as cg  # noqa: E402


def test_parse_served_model_bytes_picks_largest():
    tags = {"models": [
        {"name": "qwen2.5-coder:3b", "size": 2_000_000_000},
        {"name": "qwen2.5-coder:32b", "size": 20_000_000_000},
    ]}
    assert cg._parse_served_model_bytes(tags) == 20_000_000_000
    assert cg._parse_served_model_bytes({}) == 0
    assert cg._parse_served_model_bytes(None) == 0


def test_negotiate_num_ctx_combines_vram_and_model(monkeypatch):
    async def _bytes(endpoint, **_kw):
        return 20 * (1024 ** 3)  # ~20GiB model
    monkeypatch.setattr(cg, "_resolve_served_model_bytes", _bytes)
    monkeypatch.setattr(cg, "_awakened_vram_bytes", lambda: 24 * (1024 ** 3))  # L4

    class _Stub:
        pass

    n = asyncio.run(cg.CandidateGenerator._negotiate_num_ctx(_Stub(), "http://n:11434"))
    assert n is not None and 2048 <= n <= 32768 and n % 256 == 0


def test_negotiate_num_ctx_none_when_undeterminable(monkeypatch):
    async def _zero(endpoint, **_kw):
        return 0
    monkeypatch.setattr(cg, "_resolve_served_model_bytes", _zero)
    monkeypatch.setattr(cg, "_awakened_vram_bytes", lambda: 0)

    class _Stub:
        pass

    assert asyncio.run(cg.CandidateGenerator._negotiate_num_ctx(_Stub(), "http://n:11434")) is None


def test_is_l7_recoverable_classifies_disconnects():
    import aiohttp
    assert cg._is_l7_recoverable(aiohttp.ServerDisconnectedError()) is True
    assert cg._is_l7_recoverable(ConnectionResetError()) is True
    assert cg._is_l7_recoverable(ValueError("nope")) is False


def _deadline(s=120.0):
    return _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=s)


def test_l7_autoheal_retries_then_succeeds(monkeypatch):
    """A ServerDisconnected on the warm 32B triggers re-warm + tighten + retry --
    the second attempt (tighter window) succeeds, no permanent halt."""
    monkeypatch.setenv("JARVIS_FAILOVER_L7_RECOVERY_ATTEMPTS", "2")
    import backend.core.ouroboros.governance.local_inference_director as lidmod
    import backend.core.ouroboros.governance.providers as provmod
    import aiohttp

    warmups = {"n": 0}
    ctx_windows = []

    class _FakeClient:
        def __init__(self, cfg, session=None, profiler=None):
            self.cfg = cfg
            self.profiler = profiler
            ctx_windows.append(getattr(cfg, "num_ctx", None))
        async def warmup(self, *, timeout_s):
            warmups["n"] += 1
            return True
        async def aclose(self):
            pass

    class _Result:
        candidates = ("cand",)

    class _FakeProvider:
        _calls = {"n": 0}
        def __init__(self, client, repo_root=None):
            self.client = client
        async def generate(self, context, deadline):
            _FakeProvider._calls["n"] += 1
            if _FakeProvider._calls["n"] == 1:
                raise aiohttp.ServerDisconnectedError("worker dropped")
            return _Result()

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
            return None

    res = asyncio.run(
        cg.CandidateGenerator._failover_local_dispatch(_Stub(), object(), _deadline(), "http://n:11434")
    )
    assert res is not None and getattr(res, "candidates", None)
    assert warmups["n"] >= 1                      # re-warm ping fired
    assert len(ctx_windows) >= 2                  # a second (retry) client was built
    assert ctx_windows[1] < ctx_windows[0]        # window tightened on retry


def test_l7_autoheal_exhausts_then_raises(monkeypatch):
    """If every attempt disconnects, the auto-heal exhausts and RAISES (so the
    sentinel seam seals/halts -- never cascades)."""
    monkeypatch.setenv("JARVIS_FAILOVER_L7_RECOVERY_ATTEMPTS", "1")
    import backend.core.ouroboros.governance.local_inference_director as lidmod
    import backend.core.ouroboros.governance.providers as provmod
    import aiohttp

    class _FakeClient:
        def __init__(self, cfg, session=None, profiler=None):
            pass
        async def warmup(self, *, timeout_s):
            return True
        async def aclose(self):
            pass

    class _FakeProvider:
        def __init__(self, client, repo_root=None):
            pass
        async def generate(self, context, deadline):
            raise aiohttp.ServerDisconnectedError("always down")

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
            return None

    try:
        asyncio.run(
            cg.CandidateGenerator._failover_local_dispatch(_Stub(), object(), _deadline(), "http://n:11434")
        )
        assert False, "expected the exhausted auto-heal to raise"
    except aiohttp.ServerDisconnectedError:
        pass  # exhausted -> raised -> the sentinel seam will seal (halt, no cascade)
