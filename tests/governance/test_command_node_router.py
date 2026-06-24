"""Tests for the Command Node write-router -- the biometric-gated
`/authorize-elevation` write-path.

Gated OFF by default; loopback-or-TLS; no static credentials; bounded
body; fail-CLOSED. We inject fake voice/approve/resolve fns -- no real
audio / ECAPA / governance machinery.
"""
from __future__ import annotations

import base64
import json

import pytest

from backend.core.ouroboros.governance.command_node import command_node_router as cnr
from backend.core.ouroboros.governance.command_node import (
    biometric_auth_middleware as mw,
)


def _make_request(method, path, *, headers=None, body=None):
    raw = json.dumps(body).encode("utf-8") if body is not None else b""
    return _make_request_raw(method, path, headers=headers, raw=raw)


def _make_request_raw(method, path, *, headers=None, raw=b"", match_info=None):
    import asyncio
    from unittest.mock import MagicMock
    from aiohttp.test_utils import make_mocked_request
    from aiohttp import streams

    protocol = MagicMock()
    protocol._reading_paused = False
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    stream = streams.StreamReader(protocol=protocol, limit=2 ** 24, loop=loop)
    stream.feed_data(raw)
    stream.feed_eof()
    hdrs = dict(headers or {})
    if raw:
        hdrs.setdefault("Content-Length", str(len(raw)))
    req = make_mocked_request(
        method, path, headers=hdrs, payload=stream,
        match_info=match_info or {},
    )
    return req


def _challenge_request(pr_id, ast_mutation_id, *, headers=None,
                       blast_radius_hash=None):
    q = f"?ast_mutation_id={ast_mutation_id}"
    if blast_radius_hash:
        q += f"&blast_radius_hash={blast_radius_hash}"
    return _make_request_raw(
        "GET", f"/command-node/elevation/{pr_id}/challenge{q}",
        headers=headers, match_info={"pr_id": pr_id},
    )


# --- route mounting -------------------------------------------------------


def test_register_routes_mounts_two_endpoints():
    from aiohttp import web

    app = web.Application()
    cnr.CommandNodeRouter().register_routes(app)
    paths = {
        r.resource.canonical for r in app.router.routes()
        if r.resource is not None
    }
    assert any("/command-node/elevation/" in p for p in paths)
    assert "/command-node/authorize-elevation" in paths


# --- gated-OFF ------------------------------------------------------------


def test_challenge_disabled_returns_404(monkeypatch):
    monkeypatch.delenv("JARVIS_COMMAND_NODE_AUTH_ENABLED", raising=False)
    assert mw.is_command_node_auth_enabled() is False
    router = cnr.CommandNodeRouter()
    req = _challenge_request("PR-1", "ast-1")
    resp = _run(router._handle_challenge(req))
    assert resp.status == 404


def test_authorize_disabled_returns_404(monkeypatch):
    monkeypatch.setenv("JARVIS_COMMAND_NODE_AUTH_ENABLED", "false")
    router = cnr.CommandNodeRouter()
    req = _make_request(
        "POST", "/command-node/authorize-elevation",
        body={"pr_id": "PR-1", "nonce": "ab" * 32, "ast_mutation_id": "ast-1",
              "audio_b64": base64.b64encode(b"x").decode(), "sample_rate": 16000},
    )
    resp = _run(router._handle_authorize(req))
    assert resp.status == 404


# --- static-credential rejection ------------------------------------------


def test_static_credential_rejected(monkeypatch):
    monkeypatch.setenv("JARVIS_COMMAND_NODE_AUTH_ENABLED", "true")
    router = cnr.CommandNodeRouter()
    req = _challenge_request(
        "PR-1", "ast-1", headers={"Authorization": "Bearer some-token"},
    )
    resp = _run(router._handle_challenge(req))
    assert resp.status == 400
    assert b"static_credential_rejected" in resp.body


# --- loopback / TLS bind --------------------------------------------------


def test_assert_loopback_accepts_loopback():
    cnr.assert_loopback_or_tls("127.0.0.1")
    cnr.assert_loopback_or_tls("::1")
    cnr.assert_loopback_or_tls("localhost")


def test_assert_loopback_rejects_wildcard(monkeypatch):
    monkeypatch.delenv("JARVIS_COMMAND_NODE_ALLOW_TLS_BIND", raising=False)
    with pytest.raises(ValueError):
        cnr.assert_loopback_or_tls("0.0.0.0")


def test_assert_loopback_allows_tls_optin(monkeypatch):
    monkeypatch.setenv("JARVIS_COMMAND_NODE_ALLOW_TLS_BIND", "true")
    cnr.assert_loopback_or_tls("0.0.0.0")  # no raise


# --- end-to-end via injected fns ------------------------------------------


def test_challenge_then_authorize_body_pr(monkeypatch):
    monkeypatch.setenv("JARVIS_COMMAND_NODE_AUTH_ENABLED", "true")
    approve_calls = []

    async def _verify(audio, sample_rate):  # noqa: ARG001
        return {"authenticated": True, "score": 0.95, "antispoof_ok": True,
                "liveness_ok": True, "voiceprint_id": "owner"}

    async def _approve(*, pr_id, ast_mutation_id):  # noqa: ARG001
        approve_calls.append((pr_id, ast_mutation_id))

    router = cnr.CommandNodeRouter(
        voice_verify_fn=_verify,
        approve_fn=_approve,
        resolve_target_repo_fn=lambda pr: "jarvis",
        # Phase 3: inject a passing ASR phrase-match (NO real Whisper in tests).
        phrase_match_fn=lambda: True,
    )
    # 1. challenge
    creq = _challenge_request("PR-7", "ast-7")
    cresp = _run(router._handle_challenge(creq))
    assert cresp.status == 200
    challenge = json.loads(cresp.body)["challenge"]
    nonce = challenge["nonce"]
    assert challenge["phrase"]

    # 2. authorize
    areq = _make_request(
        "POST", "/command-node/authorize-elevation",
        body={"pr_id": "PR-7", "nonce": nonce, "ast_mutation_id": "ast-7",
              "audio_b64": base64.b64encode(b"audio").decode(),
              "sample_rate": 16000},
    )
    aresp = _run(router._handle_authorize(areq))
    assert aresp.status == 200
    out = json.loads(aresp.body)
    assert out["decision"] == "AUTHORIZED"
    assert approve_calls == [("PR-7", "ast-7")]


def test_authorize_immutable_orange_rejected_403(monkeypatch):
    monkeypatch.setenv("JARVIS_COMMAND_NODE_AUTH_ENABLED", "true")

    async def _verify(audio, sample_rate):  # noqa: ARG001
        return {"authenticated": True, "score": 1.0, "antispoof_ok": True,
                "liveness_ok": True, "voiceprint_id": "owner"}

    router = cnr.CommandNodeRouter(
        voice_verify_fn=_verify,
        approve_fn=lambda **k: None,
        resolve_target_repo_fn=lambda pr: "prime",
        # Phase 3: a passing phrase-match -- proves Immutable Orange rejects
        # even when BOTH biometric + phrase-match pass (THE LAW composes AFTER).
        phrase_match_fn=lambda: True,
    )
    creq = _challenge_request("PR-X", "ast-x")
    challenge = json.loads(_run(router._handle_challenge(creq)).body)["challenge"]
    areq = _make_request(
        "POST", "/command-node/authorize-elevation",
        body={"pr_id": "PR-X", "nonce": challenge["nonce"],
              "ast_mutation_id": "ast-x",
              "audio_b64": base64.b64encode(b"audio").decode(),
              "sample_rate": 16000},
    )
    aresp = _run(router._handle_authorize(areq))
    assert aresp.status == 403
    out = json.loads(aresp.body)
    assert out["decision"] == "REJECTED"
    assert "immutable_orange" in out["reason"]


def test_malformed_json_rejected(monkeypatch):
    monkeypatch.setenv("JARVIS_COMMAND_NODE_AUTH_ENABLED", "true")
    router = cnr.CommandNodeRouter()
    req = _make_request_raw(
        "POST", "/command-node/authorize-elevation",
        headers={"Content-Length": "11"}, raw=b"not json{{{",
    )
    resp = _run(router._handle_authorize(req))
    assert resp.status == 400
    assert b"malformed_json" in resp.body


def _run(coro):
    import asyncio

    return asyncio.run(coro)
