"""Slice 2B-i: passthrough endpoint allowlist tests.

For each of the 5 allowlisted DW non-LLM endpoints:
  - POST /v1/files (multipart)
  - POST /v1/batches (JSON)
  - GET /v1/batches/{batch_id} (path param)
  - GET /v1/files/{file_id}/content (path param, possibly large body)
  - GET /v1/models

Verify:
  - Credential injection works (upstream sees real DW key)
  - JARVIS-side auth/lease headers are stripped
  - Multipart bodies pass through byte-identically (POST /v1/files)
  - Query strings preserved
  - Path templates substituted with concrete values
  - Session token required (401 missing, 403 invalid)
  - Unknown /v1/* paths return 404
  - Wrong method on allowed path returns 405
  - No credentials appear in log records
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import AsyncGenerator, List, Tuple

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from backend.core.ouroboros.aegis.budget_state_machine import (
    BudgetCaps,
    ImmutableBudgetStateMachine,
)
from backend.core.ouroboros.aegis.daemon import build_app
from backend.core.ouroboros.aegis.upstream_registry import (
    ENV_AEGIS_UPSTREAM_DOUBLEWORD_URL,
)


_PSK = "passthrough-test-psk-bbbbbbbbbbbbbbbbbbbb"
_STUB_DW_KEY = "stub-dw-key-very-secret-aaaaa"


def _budget(tmp_path: Path) -> ImmutableBudgetStateMachine:
    caps = BudgetCaps(
        session_cap_usd=1.0, hourly_burn_cap_usd=1.0,
        route_caps_usd={"STANDARD": 0.5, "IMMEDIATE": 0.5},
        overrun_multiplier=1.5,
    )
    return ImmutableBudgetStateMachine(caps=caps, wal_path=tmp_path / "wal.jsonl")


class _Recorder:
    def __init__(self) -> None:
        self.received: List[dict] = []


def _make_dw_passthrough_stub(recorder: _Recorder) -> web.Application:
    """Stub upstream for the 5 allowlisted DW endpoints."""
    app = web.Application()

    async def _record(request: web.Request) -> dict:
        # Read body raw (don't json-decode — multipart preservation test).
        body = await request.read()
        return {
            "method": request.method,
            "path": str(request.path),
            "query_string": request.query_string,
            "content_type": request.headers.get("Content-Type", ""),
            "auth_header": request.headers.get("Authorization", ""),
            "has_jarvis_lease": (
                "x-jarvis-lease" in {h.lower() for h in request.headers.keys()}
            ),
            "has_jarvis_session": (
                "x-jarvis-session" in {h.lower() for h in request.headers.keys()}
            ),
            "body_len": len(body),
            "body_preview": body[:64],  # for byte-identity checks
        }

    async def files_handler(request: web.Request) -> web.Response:
        recorder.received.append(await _record(request))
        return web.json_response(
            {"id": "file_abc", "object": "file", "purpose": "batch"}
        )

    async def batches_create_handler(request: web.Request) -> web.Response:
        recorder.received.append(await _record(request))
        return web.json_response(
            {"id": "batch_xyz", "status": "validating"}
        )

    async def batches_poll_handler(request: web.Request) -> web.Response:
        rec = await _record(request)
        rec["matched_id"] = request.match_info.get("batch_id")
        recorder.received.append(rec)
        bid = request.match_info["batch_id"]
        return web.json_response({"id": bid, "status": "completed"})

    async def files_content_handler(request: web.Request) -> web.Response:
        rec = await _record(request)
        rec["matched_id"] = request.match_info.get("file_id")
        recorder.received.append(rec)
        # Return JSONL content (multi-line bytes payload).
        body = b'{"id":"r1","resp":"a"}\n{"id":"r2","resp":"b"}\n'
        return web.Response(body=body, content_type="application/jsonl")

    async def models_handler(request: web.Request) -> web.Response:
        recorder.received.append(await _record(request))
        return web.json_response(
            {"data": [{"id": "Qwen/Qwen3.5-397B-A17B-FP8"}]}
        )

    app.router.add_post("/v1/files", files_handler)
    app.router.add_post("/v1/batches", batches_create_handler)
    app.router.add_get("/v1/batches/{batch_id}", batches_poll_handler)
    app.router.add_get("/v1/files/{file_id}/content", files_content_handler)
    app.router.add_get("/v1/models", models_handler)
    return app


@pytest_asyncio.fixture
async def stack(
    tmp_path, monkeypatch,
) -> AsyncGenerator[Tuple[TestClient, _Recorder, ImmutableBudgetStateMachine], None]:
    recorder = _Recorder()
    stub = _make_dw_passthrough_stub(recorder)
    stub_server = TestServer(stub)
    await stub_server.start_server()
    stub_url = f"http://{stub_server.host}:{stub_server.port}"
    monkeypatch.setenv(ENV_AEGIS_UPSTREAM_DOUBLEWORD_URL, stub_url)
    monkeypatch.setenv("DOUBLEWORD_API_KEY", _STUB_DW_KEY)

    budget = _budget(tmp_path)
    app = build_app(
        budget=budget, bootstrap_psk=_PSK,
        lease_ttl_s=300, session_ttl_s=300, forwarding_enabled=True,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, recorder, budget
    finally:
        await client.close()
        await stub_server.close()


async def _session_token(client: TestClient) -> str:
    resp = await client.post(
        "/session/establish",
        headers={"Authorization": f"Bearer {_PSK}"},
    )
    body = await resp.json()
    return body["session_token"]


# ---------------------------------------------------------------------------
# POST /v1/files (multipart byte-identity)
# ---------------------------------------------------------------------------


async def test_files_multipart_credential_injected(stack):
    client, recorder, _ = stack
    session = await _session_token(client)

    # Real-world DW batch input shape: multipart with field 'file' + 'purpose'.
    # We send raw multipart bytes so Aegis must preserve them verbatim.
    multipart_body = (
        b"--BOUNDARY\r\n"
        b'Content-Disposition: form-data; name="file"; filename="batch.jsonl"\r\n'
        b"Content-Type: application/jsonl\r\n\r\n"
        b'{"custom_id":"req1","method":"POST","url":"/v1/chat/completions"}\n'
        b"\r\n--BOUNDARY\r\n"
        b'Content-Disposition: form-data; name="purpose"\r\n\r\n'
        b"batch\r\n--BOUNDARY--\r\n"
    )

    resp = await client.post(
        "/v1/files",
        headers={
            "Authorization": f"Bearer {session}",
            "Content-Type": "multipart/form-data; boundary=BOUNDARY",
        },
        data=multipart_body,
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["id"] == "file_abc"

    # Upstream stub saw the multipart body byte-for-byte + the real DW key.
    assert len(recorder.received) == 1
    rec = recorder.received[0]
    assert rec["method"] == "POST"
    assert rec["path"] == "/v1/files"
    assert rec["auth_header"] == f"Bearer {_STUB_DW_KEY}"
    assert rec["has_jarvis_session"] is False
    assert rec["has_jarvis_lease"] is False
    assert rec["body_len"] == len(multipart_body)
    assert rec["body_preview"] == multipart_body[:64]
    assert "multipart/form-data" in rec["content_type"]


# ---------------------------------------------------------------------------
# POST /v1/batches
# ---------------------------------------------------------------------------


async def test_batches_create_credential_injected(stack):
    client, recorder, _ = stack
    session = await _session_token(client)
    resp = await client.post(
        "/v1/batches",
        headers={
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
        json={
            "input_file_id": "file_abc",
            "endpoint": "/v1/chat/completions",
            "completion_window": "1h",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["id"] == "batch_xyz"

    rec = recorder.received[0]
    assert rec["method"] == "POST"
    assert rec["auth_header"] == f"Bearer {_STUB_DW_KEY}"


# ---------------------------------------------------------------------------
# GET /v1/batches/{batch_id} — path param substitution
# ---------------------------------------------------------------------------


async def test_batches_poll_path_param_forwarded(stack):
    client, recorder, _ = stack
    session = await _session_token(client)
    resp = await client.get(
        "/v1/batches/batch-deadbeef-1234",
        headers={"Authorization": f"Bearer {session}"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["id"] == "batch-deadbeef-1234"
    rec = recorder.received[0]
    assert rec["path"] == "/v1/batches/batch-deadbeef-1234"
    assert rec["matched_id"] == "batch-deadbeef-1234"
    assert rec["method"] == "GET"


async def test_batches_poll_query_string_preserved(stack):
    client, recorder, _ = stack
    session = await _session_token(client)
    resp = await client.get(
        "/v1/batches/b1?include=metadata&v=2",
        headers={"Authorization": f"Bearer {session}"},
    )
    assert resp.status == 200
    rec = recorder.received[0]
    assert rec["query_string"] == "include=metadata&v=2"


# ---------------------------------------------------------------------------
# GET /v1/files/{file_id}/content
# ---------------------------------------------------------------------------


async def test_files_content_byte_identity(stack):
    client, recorder, _ = stack
    session = await _session_token(client)
    resp = await client.get(
        "/v1/files/file_out_xyz/content",
        headers={"Authorization": f"Bearer {session}"},
    )
    assert resp.status == 200
    body = await resp.read()
    expected = b'{"id":"r1","resp":"a"}\n{"id":"r2","resp":"b"}\n'
    assert body == expected
    rec = recorder.received[0]
    assert rec["matched_id"] == "file_out_xyz"


# ---------------------------------------------------------------------------
# GET /v1/models
# ---------------------------------------------------------------------------


async def test_models_works(stack):
    client, recorder, _ = stack
    session = await _session_token(client)
    resp = await client.get(
        "/v1/models",
        headers={"Authorization": f"Bearer {session}"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["data"][0]["id"] == "Qwen/Qwen3.5-397B-A17B-FP8"
    rec = recorder.received[0]
    assert rec["auth_header"] == f"Bearer {_STUB_DW_KEY}"


# ---------------------------------------------------------------------------
# Auth gates (negative)
# ---------------------------------------------------------------------------


async def test_passthrough_missing_bearer_returns_401(stack):
    client, _, _ = stack
    resp = await client.get("/v1/models")
    assert resp.status == 401


async def test_passthrough_bogus_session_returns_403(stack):
    client, _, _ = stack
    resp = await client.get(
        "/v1/models",
        headers={"Authorization": "Bearer not.a.real.session"},
    )
    assert resp.status == 403


# ---------------------------------------------------------------------------
# Closed allowlist — no open proxy
# ---------------------------------------------------------------------------


async def test_unregistered_v1_path_returns_404(stack):
    """Anything not in the upstream_registry allowlist must 404 —
    Aegis is NOT an open proxy."""
    client, _, _ = stack
    session = await _session_token(client)
    resp = await client.get(
        "/v1/totally-fake-endpoint",
        headers={"Authorization": f"Bearer {session}"},
    )
    assert resp.status == 404


async def test_wrong_method_returns_405(stack):
    """POST /v1/models would be wrong method (registered as GET).
    aiohttp returns 405 Method Not Allowed."""
    client, _, _ = stack
    session = await _session_token(client)
    resp = await client.post(
        "/v1/models",
        headers={"Authorization": f"Bearer {session}"},
    )
    assert resp.status == 405


# ---------------------------------------------------------------------------
# Budget isolation — passthrough must NOT touch the budget
# ---------------------------------------------------------------------------


async def test_passthrough_does_not_affect_budget(stack):
    """Per binding directive: no token-cost reconciliation unless the
    endpoint returns usage. Passthrough endpoints have NO budget impact."""
    client, _, budget = stack
    session = await _session_token(client)
    snap_before = budget.snapshot()

    # Hit every passthrough endpoint.
    await client.get("/v1/models",
                     headers={"Authorization": f"Bearer {session}"})
    await client.get("/v1/batches/b1",
                     headers={"Authorization": f"Bearer {session}"})
    await client.get("/v1/files/f1/content",
                     headers={"Authorization": f"Bearer {session}"})

    snap_after = budget.snapshot()
    assert snap_after["session_debit_usd"] == snap_before["session_debit_usd"]
    assert snap_after["open_reserve_count"] == snap_before["open_reserve_count"]


# ---------------------------------------------------------------------------
# Credential / multipart body never logged
# ---------------------------------------------------------------------------


async def test_credential_never_in_logs(stack, caplog):
    client, _, _ = stack
    session = await _session_token(client)
    caplog.set_level(logging.DEBUG, logger="backend.core.ouroboros.aegis.passthrough")

    await client.get("/v1/models",
                     headers={"Authorization": f"Bearer {session}"})

    # The DW credential must NEVER appear in any log record (message
    # body or extras). Same check for the PSK.
    for record in caplog.records:
        text = record.getMessage()
        assert _STUB_DW_KEY not in text, (
            f"credential leaked in log: {text!r}"
        )
        assert _PSK not in text, f"PSK leaked in log: {text!r}"


async def test_multipart_body_never_in_logs(stack, caplog):
    client, _, _ = stack
    session = await _session_token(client)
    caplog.set_level(logging.DEBUG, logger="backend.core.ouroboros.aegis.passthrough")

    sentinel = "MULTIPART_BODY_SENTINEL_VALUE_DO_NOT_LEAK"
    body = (
        b"--BOUND\r\n"
        b'Content-Disposition: form-data; name="file"; filename="x.jsonl"\r\n'
        b"Content-Type: application/jsonl\r\n\r\n"
        + sentinel.encode("utf-8") + b"\r\n--BOUND--\r\n"
    )
    await client.post(
        "/v1/files",
        headers={
            "Authorization": f"Bearer {session}",
            "Content-Type": "multipart/form-data; boundary=BOUND",
        },
        data=body,
    )

    for record in caplog.records:
        text = record.getMessage()
        assert sentinel not in text, (
            f"multipart body content leaked in log: {text!r}"
        )
