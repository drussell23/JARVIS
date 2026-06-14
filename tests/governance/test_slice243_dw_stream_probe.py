"""Slice 243 Phase 3 — DoubleWord micro-streaming load test (stream_health_probe).

The stability gate calls ``provider.stream_health_probe()`` to prove the grid can
carry a real stream, not just answer a 200-OK /models ping. This probe reuses the
existing session/auth/lease machinery (same as ``health_probe`` + the realtime
path) but requests a tiny deterministic multi-token completion and verifies the
SSE socket delivers tokens and closes cleanly. A mid-flight rupture → False
(FLAPPING). It NEVER raises into the caller — failure is signalled as False.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import doubleword_provider as dwp
from backend.core.ouroboros.governance.doubleword_provider import DoublewordProvider


class _FakeContent:
    """Async SSE body: readline() pops lines; an Exception element ruptures."""

    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        if not self._lines:
            return b""
        item = self._lines.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeResp:
    def __init__(self, lines, status=200):
        self.status = status
        self.content = _FakeContent(lines)

    async def text(self):
        return "err"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp
        self.posted = None

    def post(self, url, **kw):
        self.posted = (url, kw)
        return self._resp


class _MockDW:
    """Minimal stand-in exercising the REAL stream_health_probe."""

    is_available = True
    _base_url = "http://dw.local/v1"
    _model = "qwen-397b"
    _last_error_status = 0

    def __init__(self, resp):
        self._session = _FakeSession(resp)

    async def _get_session(self):
        return self._session

    @staticmethod
    def _request_timeout():
        return None


# bind the real method onto the mock
_MockDW.stream_health_probe = DoublewordProvider.stream_health_probe


def _data(token):
    return ('data: {"choices":[{"delta":{"content":"%s"}}]}\n' % token).encode()


@pytest.fixture(autouse=True)
def _hermetic_aegis(monkeypatch):
    async def _auth():
        return {}

    async def _lease(**kw):
        return {}

    monkeypatch.setattr(dwp, "_aegis_dw_session_auth_header", _auth)
    monkeypatch.setattr(dwp, "_aegis_acquire_call_lease", _lease)
    monkeypatch.setattr(dwp, "_aegis_merge_lease_headers", lambda a, b: {})
    monkeypatch.delenv("JARVIS_GRID_STABILITY_MIN_TOKENS", raising=False)
    yield


class TestStreamHealthProbe:
    async def test_clean_multitoken_stream_returns_true(self):
        resp = _FakeResp([_data("ok"), _data("ya"), _data("go"), b"data: [DONE]\n", b""])
        p = _MockDW(resp)
        assert await p.stream_health_probe() is True
        # it requested a *streaming* completion
        url, kw = p._session.posted
        assert url.endswith("/chat/completions")
        assert kw["json"].get("stream") is True

    async def test_midflight_rupture_returns_false(self):
        # one token, then the socket drops mid-stream
        resp = _FakeResp([_data("ok"), ConnectionResetError("socket dropped")])
        p = _MockDW(resp)
        assert await p.stream_health_probe() is False

    async def test_non_200_returns_false(self):
        resp = _FakeResp([b""], status=503)
        p = _MockDW(resp)
        assert await p.stream_health_probe() is False

    async def test_too_few_tokens_returns_false(self, monkeypatch):
        monkeypatch.setenv("JARVIS_GRID_STABILITY_MIN_TOKENS", "3")
        resp = _FakeResp([_data("ok"), b"data: [DONE]\n", b""])  # only 1 token
        p = _MockDW(resp)
        assert await p.stream_health_probe() is False

    async def test_unavailable_provider_returns_false(self):
        resp = _FakeResp([_data("ok"), b"data: [DONE]\n", b""])
        p = _MockDW(resp)
        p.is_available = False
        assert await p.stream_health_probe() is False

    async def test_lightweight_payload_caps_max_tokens(self):
        resp = _FakeResp([_data("ok"), _data("ya"), b"data: [DONE]\n", b""])
        p = _MockDW(resp)
        await p.stream_health_probe()
        body = p._session.posted[1]["json"]
        # a micro-stream — must NOT request a heavy completion
        assert body.get("max_tokens", 9999) <= 32
