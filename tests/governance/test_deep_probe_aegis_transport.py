"""Gap 1 -- the Deep Inference Probe must impersonate REAL DW traffic.

Hybrid soak bt-2026-06-29-055555 showed the probe getting HTTP 401: it hit
``api.doubleword.ai`` directly with the (Aegis-confiscated) DOUBLEWORD_API_KEY,
while the real DW provider routes through the Aegis proxy with a SESSION BEARER
(``dw_session_auth_header``). A probe on a different transport/auth is not a
faithful data-plane signal -- it false-degrades.

The fix: the probe resolves its transport (base URL + auth header) from the SAME
``aegis_provider_bridge`` helpers the DW CandidateGenerator/provider use -- no
hardcoded Aegis path, no hardcoded token. It perfectly impersonates real traffic.

TDD with the aegis helpers monkeypatched -- ZERO real network.
"""
from __future__ import annotations

import pytest

import backend.core.ouroboros.governance.provider_heartbeat as ph
import backend.core.ouroboros.governance.aegis_provider_bridge as apb


async def test_transport_uses_aegis_base_and_session_bearer(monkeypatch):
    monkeypatch.setattr(apb, "dw_aegis_base_url", lambda: "http://127.0.0.1:9701/v1")

    async def _sess():
        return {"Authorization": "Bearer sess-token-xyz"}
    monkeypatch.setattr(apb, "dw_session_auth_header", _sess)

    url, headers = await ph._resolve_dw_probe_transport()
    assert url == "http://127.0.0.1:9701/v1/chat/completions"
    assert headers.get("Authorization") == "Bearer sess-token-xyz"
    assert headers.get("Content-Type") == "application/json"


async def test_transport_failsoft_to_direct(monkeypatch):
    """If the aegis bridge raises, fall back to the direct DW base + no bearer
    (fail-soft -- a transport-resolve error must not crash the probe)."""
    def _boom():
        raise RuntimeError("aegis preflight corrupt")
    monkeypatch.setattr(apb, "dw_aegis_base_url", _boom)

    url, headers = await ph._resolve_dw_probe_transport()
    assert url.endswith("/chat/completions")  # direct DW fallback
    assert "doubleword.ai" in url or url.startswith("http")
    assert headers.get("Content-Type") == "application/json"


async def test_transport_session_header_error_is_failsoft(monkeypatch):
    monkeypatch.setattr(apb, "dw_aegis_base_url", lambda: "http://aegis/v1")

    async def _sess_boom():
        raise RuntimeError("session establish failed")
    monkeypatch.setattr(apb, "dw_session_auth_header", _sess_boom)

    # Resolves the URL; auth header is best-effort (empty) -- never raises.
    url, headers = await ph._resolve_dw_probe_transport()
    assert url == "http://aegis/v1/chat/completions"
    assert "Authorization" not in headers  # graceful: no bearer rather than crash


async def test_default_dispatch_calls_resolved_transport(monkeypatch):
    """The default inference dispatch POSTs to the RESOLVED (faithful) transport,
    not a hardcoded endpoint."""
    seen = {}

    async def _fake_transport():
        return ("http://aegis/v1/chat/completions", {"Authorization": "Bearer S"})
    monkeypatch.setattr(ph, "_resolve_dw_probe_transport", _fake_transport)

    def _fake_post(url, headers, payload, timeout):  # noqa: ANN001
        seen["url"] = url
        seen["auth"] = headers.get("Authorization")
        return '{"choices":[{"message":{"content":"1"}}]}'
    monkeypatch.setattr(ph, "_http_post_json", _fake_post)

    out = await ph._default_dw_inference_dispatch()
    assert out == "1"
    assert seen["url"] == "http://aegis/v1/chat/completions"
    assert seen["auth"] == "Bearer S"
