"""Task 4 (ov cockpit silence, Slice 2, F2) — SemanticTriage credentialed probe.

Live run bt-2026-07-08-013911: ``WARNING [SemanticTriage] /v1/models
returned 401 after 4 attempt(s)`` while the Aegis daemon's own GET of
the same endpoint returns 200. Root cause: ``verify_model()``'s
``session.get(f"{base_url}/models", ...)`` call carried NO
``Authorization`` header at all — ``doubleword_provider._get_session()``
deliberately bakes no real bearer into the session when Aegis is
enabled (the confiscated key is injected per-call), and every other DW
call site (``_upload_file``, ``DwCatalogClient._auth_headers``,
``dw_surface_probes.probe_auth_sync``) fetches
``aegis_provider_bridge.dw_session_auth_header()`` per call — this
probe was the one outbound site that never did.

This spine proves the fix at the root (not a retry band-aid): the
probe's request now carries the SAME credentialed header the rest of
the DW stack attaches, via the SAME shared helper — verified by
spying on the header dict actually handed to ``session.get`` (no real
network).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict

import pytest


class _FakeGetResp:
    def __init__(self, status: int, json_body: Dict[str, Any] | None = None):
        self.status = status
        self._json_body = json_body or {}

    async def json(self):
        return self._json_body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeGetCtx:
    """Records every GET call (url + kwargs) and returns queued responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._responses.pop(0)


class _FakeSession:
    def __init__(self, get_ctx):
        self.get = get_ctx


class _FakeDwProvider:
    def __init__(self, session, base_url="https://dw.example/v1"):
        self._session = session
        self._base_url = base_url
        self.is_available = True
        self._model = "Qwen/Qwen3.5-397B-A17B-FP8"

    async def _get_session(self):
        return self._session

    def _request_timeout(self):
        return 30


def _make_engine(session):
    from backend.core.ouroboros.governance.semantic_triage import (
        SemanticTriageEngine,
    )
    dw = _FakeDwProvider(session)
    engine = SemanticTriageEngine(dw_provider=dw, project_root=Path("."))
    return engine


def test_verify_model_request_carries_aegis_session_bearer_header(monkeypatch):
    """The GET to /v1/models must carry the SAME credentialed header
    ``aegis_provider_bridge.dw_session_auth_header()`` produces — the
    root fix, spied without touching the network."""
    calls = []

    async def _fake_header():
        calls.append(1)
        return {"Authorization": "Bearer session-tok-abc"}

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.aegis_provider_bridge."
        "dw_session_auth_header",
        _fake_header,
    )

    get_ctx = _FakeGetCtx([_FakeGetResp(200, {"data": []})])
    session = _FakeSession(get_ctx)
    engine = _make_engine(session)

    ok = asyncio.run(engine.verify_model())

    assert len(get_ctx.calls) == 1
    _, kwargs = get_ctx.calls[0]
    assert "headers" in kwargs, (
        "verify_model()'s GET must pass a headers= kwarg (root fix for "
        "the 401 — the pre-fix call passed none at all)"
    )
    assert kwargs["headers"] == {"Authorization": "Bearer session-tok-abc"}
    assert calls, "dw_session_auth_header() must actually be invoked"
    # Empty catalog with no matching model still proceeds (unverified) —
    # not the focus of this test, just confirming no crash.
    assert ok in (True, False)


def test_verify_model_uses_same_helper_doubleword_provider_uses(monkeypatch):
    """Structural: the header-fetch helper imported by verify_model()
    must be the exact same symbol (``aegis_provider_bridge.
    dw_session_auth_header``) the rest of the DW stack (dw_catalog_
    client, dw_surface_probes, doubleword_provider._upload_file)
    already uses — one credential path, not a second bespoke one."""
    import backend.core.ouroboros.governance.aegis_provider_bridge as bridge

    sentinel_calls = []

    async def _spy():
        sentinel_calls.append("called")
        return {"Authorization": "Bearer x"}

    monkeypatch.setattr(bridge, "dw_session_auth_header", _spy)

    get_ctx = _FakeGetCtx([_FakeGetResp(200, {"data": []})])
    session = _FakeSession(get_ctx)
    engine = _make_engine(session)

    asyncio.run(engine.verify_model())

    assert sentinel_calls == ["called"], (
        "verify_model must call aegis_provider_bridge.dw_session_auth_header "
        "(monkeypatched on the module object) — confirms it resolves the "
        "SAME shared helper at call time, not a bound/cached reference"
    )


def test_verify_model_refetches_header_on_each_retry_attempt(monkeypatch):
    """A session-bearer can rotate — each retry attempt (boot-race
    401→200) must fetch a fresh header, matching the per-call pattern
    used elsewhere in the DW stack (never a stale baked-in token)."""
    fetch_count = {"n": 0}

    async def _fake_header():
        fetch_count["n"] += 1
        return {"Authorization": f"Bearer tok-{fetch_count['n']}"}

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.aegis_provider_bridge."
        "dw_session_auth_header",
        _fake_header,
    )
    monkeypatch.setenv("OUROBOROS_TRIAGE_VERIFY_BACKOFF_S", "0")

    # First attempt 401 (retryable), second attempt 200.
    get_ctx = _FakeGetCtx([
        _FakeGetResp(401, {}),
        _FakeGetResp(200, {"data": []}),
    ])
    session = _FakeSession(get_ctx)
    engine = _make_engine(session)

    asyncio.run(engine.verify_model())

    assert len(get_ctx.calls) == 2
    headers_seen = [kwargs["headers"] for _, kwargs in get_ctx.calls]
    assert headers_seen[0] != headers_seen[1], (
        "each retry attempt must fetch a fresh credential, not reuse "
        "the first attempt's (possibly stale/401'd) header"
    )
    assert fetch_count["n"] == 2
