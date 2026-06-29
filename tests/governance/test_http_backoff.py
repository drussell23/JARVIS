"""Resilient API Polling -- exponential backoff + full jitter on 429/5xx.

The 429 storm came from un-backed-off diagnostic loops. The shared REST helper now
retries retryable statuses (429 + 5xx) with exponential backoff + jitter, so no
diagnostic flow crashes or locks on a transient rate-limit.
"""
from __future__ import annotations

import asyncio


async def _nosleep(*_a, **_k):
    return None

import pytest

import backend.core.ouroboros.governance.gcp_compute_rest as gcr


@pytest.mark.parametrize("status,retry", [
    (429, True), (500, True), (502, True), (503, True), (504, True),
    (200, False), (201, False), (400, False), (401, False), (404, False), (0, False),
])
def test_is_retryable_status(status, retry):
    assert gcr.is_retryable_status(status) is retry


def test_backoff_delay_is_bounded_and_grows():
    # Full-jitter: 0 <= delay <= min(cap, base*2^attempt).
    for attempt in range(0, 6):
        d = gcr._backoff_delay(attempt, base=0.5, cap=30.0)
        assert 0.0 <= d <= min(30.0, 0.5 * (2 ** attempt)) + 1e-9


def test_backoff_respects_cap():
    assert gcr._backoff_delay(20, base=0.5, cap=5.0) <= 5.0


async def test_http_request_retries_429_then_succeeds(monkeypatch):
    calls = {"n": 0}

    async def fake_once(url, *, method="GET", headers=None, body=None, timeout_s=10.0):
        calls["n"] += 1
        return (429, "rate limited") if calls["n"] < 3 else (200, "ok")

    monkeypatch.setattr(gcr, "_http_request_once", fake_once)
    monkeypatch.setattr(gcr.asyncio, "sleep", _nosleep)
    monkeypatch.setenv("JARVIS_HTTP_MAX_RETRIES", "4")

    status, text = await gcr._http_request("http://x", method="GET")
    assert status == 200 and text == "ok"
    assert calls["n"] == 3              # two 429s retried, third succeeded


async def test_http_request_gives_up_after_max_retries(monkeypatch):
    calls = {"n": 0}

    async def always_429(url, **kw):
        calls["n"] += 1
        return (429, "still limited")

    monkeypatch.setattr(gcr, "_http_request_once", always_429)
    monkeypatch.setattr(gcr.asyncio, "sleep", _nosleep)
    monkeypatch.setenv("JARVIS_HTTP_MAX_RETRIES", "2")

    status, _ = await gcr._http_request("http://x")
    assert status == 429                # surfaced, never crashed
    assert calls["n"] == 3              # initial + 2 retries


async def test_non_retryable_status_not_retried(monkeypatch):
    calls = {"n": 0}

    async def four_oh_four(url, **kw):
        calls["n"] += 1
        return (404, "nope")

    monkeypatch.setattr(gcr, "_http_request_once", four_oh_four)
    monkeypatch.setenv("JARVIS_HTTP_MAX_RETRIES", "4")
    status, _ = await gcr._http_request("http://x")
    assert status == 404 and calls["n"] == 1   # no wasted retries on a hard 404
