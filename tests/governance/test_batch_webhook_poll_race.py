"""Async Batch Recovery — _await_batch_result must race webhook vs poll.

Pins the 2026-06-20 fix: a webhook that never arrives (cloud node, no ingress)
must NOT starve the op — the always-works poll path wins. DW completes batches in
~11s, so polling catches it; the prior await-webhook-first design hung the full
budget → 180s TimeoutError.
"""
from __future__ import annotations

import asyncio
import pytest

from backend.core.ouroboros.governance.doubleword_provider import DoublewordProvider


def _provider():
    # construct minimally without network — only _await_batch_result is exercised
    return DoublewordProvider.__new__(DoublewordProvider)


class _Registry:
    def __init__(self, *, hangs=False, result=None):
        self._hangs = hangs
        self._result = result
    async def wait(self, batch_id, timeout=None):
        if self._hangs:
            await asyncio.sleep(3600)  # webhook never arrives
        return self._result


@pytest.mark.asyncio
async def test_poll_wins_when_webhook_never_arrives(monkeypatch):
    """The cloud-node case: webhook hangs forever, poll returns → poll wins fast."""
    p = _provider()
    p._batch_registry = _Registry(hangs=True)
    async def _poll(batch_id, op_id="x"):
        await asyncio.sleep(0.05)
        return "out-file-from-poll"
    monkeypatch.setattr(p, "_adaptive_poll_batch", _poll)
    out = await asyncio.wait_for(p._await_batch_result("b1"), timeout=2.0)
    assert out == "out-file-from-poll"


@pytest.mark.asyncio
async def test_webhook_wins_when_faster(monkeypatch):
    p = _provider()
    p._batch_registry = _Registry(hangs=False, result="out-file-from-webhook")
    async def _poll(batch_id, op_id="x"):
        await asyncio.sleep(5)  # slow poll
        return "out-file-from-poll"
    monkeypatch.setattr(p, "_adaptive_poll_batch", _poll)
    out = await asyncio.wait_for(p._await_batch_result("b1"), timeout=2.0)
    assert out == "out-file-from-webhook"


@pytest.mark.asyncio
async def test_no_registry_just_polls(monkeypatch):
    p = _provider()
    p._batch_registry = None
    async def _poll(batch_id, op_id="x"):
        return "out-file-from-poll"
    monkeypatch.setattr(p, "_adaptive_poll_batch", _poll)
    out = await p._await_batch_result("b1")
    assert out == "out-file-from-poll"


@pytest.mark.asyncio
async def test_poll_wins_when_webhook_returns_none(monkeypatch):
    """Webhook resolves None (rejected) → poll's real result is taken, not None."""
    p = _provider()
    p._batch_registry = _Registry(hangs=False, result=None)
    async def _poll(batch_id, op_id="x"):
        await asyncio.sleep(0.05)
        return "out-file-from-poll"
    monkeypatch.setattr(p, "_adaptive_poll_batch", _poll)
    out = await asyncio.wait_for(p._await_batch_result("b1"), timeout=2.0)
    assert out == "out-file-from-poll"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
