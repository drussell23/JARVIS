"""Deterministic L7 model resolution for the awakened J-Prime failover node.

The Phase 3c dispatch must name the model the node ACTUALLY serves (loaded in
VRAM), not the lagging FSM ``_active_model_label`` -- which is empty when the
endpoint is discovered by direct GCP query (controller not SERVING in-process),
so the dispatch fell back to the survival 7B and the node rejected it with
``KeyError('choices')``.

The source of truth is the node's own ``/api/tags`` (ollama). We query it ONCE
per endpoint and memoize per-endpoint: a new endpoint (node changed / re-awaken
at a new IP) is a natural cache miss; ``_invalidate_jprime_model_cache()`` clears
it on FSM->DORMANT. No per-dispatch network spam.
"""
from __future__ import annotations

import asyncio

import backend.core.ouroboros.governance.candidate_generator as cg


def setup_function(_fn):
    cg._invalidate_jprime_model_cache()


# --- pure parse of the /api/tags payload -----------------------------------

def test_parse_picks_largest_model_by_size():
    tags = {"models": [
        {"name": "qwen2.5-coder:3b", "size": 2_000_000_000},
        {"name": "qwen2.5-coder:32b", "size": 20_000_000_000},
    ]}
    assert cg._parse_served_model(tags) == "qwen2.5-coder:32b"


def test_parse_falls_back_to_model_key():
    tags = {"models": [{"model": "qwen2.5-coder:32b"}]}
    assert cg._parse_served_model(tags) == "qwen2.5-coder:32b"


def test_parse_empty_or_malformed_returns_none():
    assert cg._parse_served_model({}) is None
    assert cg._parse_served_model({"models": []}) is None
    assert cg._parse_served_model(None) is None
    assert cg._parse_served_model({"models": [{}]}) is None


# --- memoized resolution ----------------------------------------------------

def test_resolve_fetches_once_and_memoizes():
    calls = {"n": 0}

    async def _fetcher(endpoint):
        calls["n"] += 1
        return "qwen2.5-coder:32b"

    async def _run():
        ep = "http://10.0.0.5:11434"
        a = await cg._resolve_served_model(ep, fetcher=_fetcher)
        b = await cg._resolve_served_model(ep, fetcher=_fetcher)
        return a, b

    a, b = asyncio.run(_run())
    assert a == b == "qwen2.5-coder:32b"
    assert calls["n"] == 1  # fetched once, second call served from cache


def test_resolve_refetches_on_node_change():
    calls = {"n": 0}

    async def _fetcher(endpoint):
        calls["n"] += 1
        return "model-for-" + endpoint

    async def _run():
        a = await cg._resolve_served_model("http://10.0.0.5:11434", fetcher=_fetcher)
        b = await cg._resolve_served_model("http://10.0.0.9:11434", fetcher=_fetcher)
        return a, b

    a, b = asyncio.run(_run())
    assert a != b
    assert calls["n"] == 2  # a new endpoint (node changed) is a cache miss


def test_invalidate_clears_cache():
    calls = {"n": 0}

    async def _fetcher(endpoint):
        calls["n"] += 1
        return "qwen2.5-coder:32b"

    async def _run():
        ep = "http://10.0.0.5:11434"
        await cg._resolve_served_model(ep, fetcher=_fetcher)
        cg._invalidate_jprime_model_cache()  # FSM -> DORMANT
        await cg._resolve_served_model(ep, fetcher=_fetcher)

    asyncio.run(_run())
    assert calls["n"] == 2  # cleared -> re-fetched


def test_resolve_none_endpoint_never_fetches():
    calls = {"n": 0}

    async def _fetcher(endpoint):
        calls["n"] += 1
        return "x"

    assert asyncio.run(cg._resolve_served_model(None, fetcher=_fetcher)) is None
    assert calls["n"] == 0


def test_resolve_failsoft_does_not_cache_none():
    calls = {"n": 0}

    async def _fetcher(endpoint):
        calls["n"] += 1
        return None  # node unreachable / empty tags

    async def _run():
        ep = "http://10.0.0.5:11434"
        await cg._resolve_served_model(ep, fetcher=_fetcher)
        await cg._resolve_served_model(ep, fetcher=_fetcher)

    asyncio.run(_run())
    assert calls["n"] == 2  # a None result is not cached -> retried next dispatch


# --- method delegates to the memoized resolver, abandons _active_model_label -

def test_method_delegates_to_resolver_without_active_label():
    cg._JPRIME_SERVED_MODEL_CACHE["http://10.0.0.5:11434"] = "qwen2.5-coder:32b"

    class _Stub:
        pass

    model = asyncio.run(
        cg.CandidateGenerator._resolve_dispatch_model_name(_Stub(), "http://10.0.0.5:11434")
    )
    assert model == "qwen2.5-coder:32b"


def test_reap_gpu_node_invalidates_cache(monkeypatch):
    """WIRING: FSM teardown (_reap_gpu_node -> DORMANT) must clear the memoized
    served-model map so a fresh awaken re-queries the new node. Proves the
    invalidation has a live caller (not theater)."""
    monkeypatch.setenv("JARVIS_FAILOVER_USE_ADC", "false")
    import backend.core.ouroboros.governance.failover_lifecycle as fl

    cg._JPRIME_SERVED_MODEL_CACHE["http://10.0.0.5:11434"] = "qwen2.5-coder:32b"
    controller = fl.FailoverLifecycleController()
    asyncio.run(controller._reap_gpu_node())  # fail-soft; finally clears the cache
    assert cg._JPRIME_SERVED_MODEL_CACHE == {}
