"""RT-completion resilience wiring (2026-07-17).

Both handlers already EXISTED but were reachable only from batch/_upload_file
and probe paths, so the RT completion surface — the one the DreamEngine and
every rt_gate consumer use — had NO resilience owner:
  * dw_client_lifecycle had ZERO production callers (severed): a 502 storm
    (bt-2026-07-17-082144: "upstream_unreachable") never flushed the poisoned
    aiohttp pool.
  * a 403 never pruned the unentitled model on this path → cyclic 403s.

These pin the WIRING. They deliberately do NOT re-test backoff/TTL internals —
those belong to the modules that own them (DRY).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import backend.core.ouroboros.governance.dw_client_lifecycle as lifecycle
import backend.core.ouroboros.governance.dw_entitlement_fallback as ef
from backend.core.ouroboros.governance.doubleword_provider import DoublewordProvider


def _run(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _provider():
    p = DoublewordProvider.__new__(DoublewordProvider)
    p._client_lifecycle = None
    p.force_session_reset = AsyncMock()
    return p


# ---- 502 → the severed lifecycle owner is now driven -----------------------


def test_502_triggers_transport_pool_flush(monkeypatch):
    p = _provider()
    _run(p._handle_rt_http_failure(
        status=502, body='{"error":"upstream_unreachable"}',
        model="Qwen/Qwen3.5-397B-A17B-FP8", caller_id="dream_engine"))
    p.force_session_reset.assert_awaited_once()      # poisoned pool flushed


@pytest.mark.parametrize("status", [502, 503, 504])
def test_all_upstream_5xx_flush(monkeypatch, status):
    p = _provider()
    _run(p._handle_rt_http_failure(status=status, body="", model="m", caller_id="c"))
    p.force_session_reset.assert_awaited_once()


def test_flush_respects_the_modules_own_cooldown(monkeypatch):
    """DRY: the 60s anti-thrash cooldown is the lifecycle module's policy —
    a 502 STORM must not thrash the pool. State persists per-provider."""
    p = _provider()
    for _ in range(5):
        _run(p._handle_rt_http_failure(status=502, body="", model="m", caller_id="c"))
    assert p.force_session_reset.await_count == 1    # storm → ONE flush


def test_flush_master_switch_honored(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_TRANSPORT_FLUSH_ENABLED", "false")
    p = _provider()
    _run(p._handle_rt_http_failure(status=502, body="", model="m", caller_id="c"))
    p.force_session_reset.assert_not_called()


def test_non_5xx_does_not_flush():
    p = _provider()
    _run(p._handle_rt_http_failure(status=400, body="bad param", model="m", caller_id="c"))
    p.force_session_reset.assert_not_called()


# ---- 403 → entitlement prune (TTL-governed re-probe) ----------------------


def test_403_entitlement_blocked_prunes_catalog(monkeypatch):
    cache = MagicMock()
    monkeypatch.setattr(ef, "get_process_entitlement_cache", lambda: cache)
    monkeypatch.setattr(ef, "is_entitlement_blocked", lambda s, b: True)
    p = _provider()
    _run(p._handle_rt_http_failure(
        status=403, body="blocked by a routing rule",
        model="Qwen/Qwen3.5-35B-A3B-FP8", caller_id="dream_engine"))
    cache.reset.assert_called_once()   # next election re-derives from DW itself


def test_403_that_is_not_entitlement_does_not_prune(monkeypatch):
    """A generic 403 (not the entitlement marker) must not nuke the catalog."""
    cache = MagicMock()
    monkeypatch.setattr(ef, "get_process_entitlement_cache", lambda: cache)
    monkeypatch.setattr(ef, "is_entitlement_blocked", lambda s, b: False)
    p = _provider()
    _run(p._handle_rt_http_failure(status=403, body="forbidden", model="m", caller_id="c"))
    cache.reset.assert_not_called()


def test_403_respects_fallback_master_switch(monkeypatch):
    cache = MagicMock()
    monkeypatch.setattr(ef, "get_process_entitlement_cache", lambda: cache)
    monkeypatch.setattr(ef, "entitlement_fallback_enabled", lambda: False)
    p = _provider()
    _run(p._handle_rt_http_failure(status=403, body="blocked by a routing rule",
                                   model="m", caller_id="c"))
    cache.reset.assert_not_called()


def test_403_does_not_flush_the_pool():
    """403 != 502: an entitlement denial must not thrash the transport."""
    p = _provider()
    _run(p._handle_rt_http_failure(status=403, body="blocked by a routing rule",
                                   model="m", caller_id="c"))
    p.force_session_reset.assert_not_called()


# ---- never mask the caller's real error -----------------------------------


def test_handler_never_raises_on_broken_lifecycle(monkeypatch):
    monkeypatch.setattr(
        lifecycle, "ClientLifecycleManager",
        MagicMock(side_effect=RuntimeError("lifecycle exploded")))
    p = _provider()
    _run(p._handle_rt_http_failure(status=502, body="", model="m", caller_id="c"))


def test_handler_never_raises_on_broken_entitlement(monkeypatch):
    monkeypatch.setattr(
        ef, "get_process_entitlement_cache",
        MagicMock(side_effect=RuntimeError("cache exploded")))
    monkeypatch.setattr(ef, "is_entitlement_blocked", lambda s, b: True)
    p = _provider()
    _run(p._handle_rt_http_failure(status=403, body="blocked by a routing rule",
                                   model="m", caller_id="c"))


def test_rt_failure_handler_is_wired_into_complete_sync():
    """Structural: the RT path must actually call the handler (the whole point
    — these modules were unreachable from RT before)."""
    import inspect
    src = inspect.getsource(DoublewordProvider.complete_sync)
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert "_handle_rt_http_failure(" in code
