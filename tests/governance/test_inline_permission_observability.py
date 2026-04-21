"""Slice 4 tests — IDE observability for inline permissions.

Pins:
* Kill switch: default-off, explicit true opens, explicit false keeps 403
* Authority invariant — no imports from gate/execution modules
* Four GET endpoints: prompts list / prompt detail / grants list / grant detail
* Malformed id → 400; unknown id → 404; rate limit → 429; CORS allowlist
* Every response carries ``schema_version``
* Bridge publishes 7 event types (5 prompt + 2 grant) with SANITIZED payloads
* Bridge never emits the raw grant.pattern (only pattern_preview)
* Grant events carry the _GRANTS_SENTINEL_OP_ID so clients can filter
* Bridge is disabled transparently when the broker doesn't know an event type
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_INLINE_GRANT_CREATED,
    EVENT_TYPE_INLINE_GRANT_REVOKED,
    EVENT_TYPE_INLINE_PROMPT_ALLOWED,
    EVENT_TYPE_INLINE_PROMPT_DENIED,
    EVENT_TYPE_INLINE_PROMPT_EXPIRED,
    EVENT_TYPE_INLINE_PROMPT_PAUSED,
    EVENT_TYPE_INLINE_PROMPT_PENDING,
    _VALID_EVENT_TYPES,
    reset_default_broker,
    get_default_broker,
)
from backend.core.ouroboros.governance.inline_permission import (
    InlineDecision,
)
from backend.core.ouroboros.governance.inline_permission_memory import (
    RememberedAllowStore,
    reset_stores_for_test,
)
from backend.core.ouroboros.governance.inline_permission_observability import (
    INLINE_PERMISSION_OBSERVABILITY_SCHEMA_VERSION,
    InlinePermissionObservabilityRouter,
    _GRANTS_SENTINEL_OP_ID,
    bridge_inline_permission_to_broker,
    inline_permission_observability_enabled,
)
from backend.core.ouroboros.governance.inline_permission_prompt import (
    InlinePromptController,
    InlinePromptRequest,
    ResponseKind,
    reset_default_singletons,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_env_and_state(monkeypatch):
    for key in list(os.environ.keys()):
        if key.startswith("JARVIS_IDE_INLINE_PERMISSION_OBSERVABILITY_"):
            monkeypatch.delenv(key, raising=False)
    reset_default_singletons()
    reset_stores_for_test()
    reset_default_broker()
    yield
    reset_default_singletons()
    reset_stores_for_test()
    reset_default_broker()


def _enable(monkeypatch) -> None:
    monkeypatch.setenv(
        "JARVIS_IDE_INLINE_PERMISSION_OBSERVABILITY_ENABLED", "true",
    )


def _make_request(
    path: str,
    *,
    method: str = "GET",
    headers: Dict[str, str] = None,
    match_info: Dict[str, str] = None,
    remote: str = "127.0.0.1",
) -> web.Request:
    headers = headers or {}
    req = make_mocked_request(method, path, headers=headers)
    if match_info:
        req.match_info.update(match_info)
    req._transport_peername = (remote, 0)  # type: ignore[attr-defined]
    return req


def _make_prompt_request(
    *,
    prompt_id: str = "op-1:c1:abc123",
    op_id: str = "op-1",
    tool: str = "edit_file",
    target: str = "backend/x.py",
    arg_fingerprint: str = "",
) -> InlinePromptRequest:
    from backend.core.ouroboros.governance.inline_permission import (
        InlineGateVerdict,
    )
    return InlinePromptRequest(
        prompt_id=prompt_id,
        op_id=op_id,
        call_id=f"{op_id}:r0.0:{tool}",
        tool=tool,
        arg_fingerprint=arg_fingerprint or target,
        arg_preview=(arg_fingerprint or target)[:200],
        target_path=target,
        verdict=InlineGateVerdict(
            decision=InlineDecision.ASK,
            rule_id="RULE_EDIT_OUT_OF_APPROVED",
            reason="test",
        ),
    )


async def _resolve(handler_coro):
    """Normalise aiohttp response → (status, json_dict)."""
    resp = await handler_coro
    body = resp.body  # aiohttp json_response stores raw bytes here
    return resp.status, json.loads(body)


# ===========================================================================
# Env kill switch
# ===========================================================================


def test_default_is_on_post_slice_5_graduation(monkeypatch):
    """Slice 5 (2026-04-21): graduated default `true`.

    The observability half is safe to graduate because it's a pure
    read-only view. Explicit `=false` is still the kill switch.
    """
    monkeypatch.delenv(
        "JARVIS_IDE_INLINE_PERMISSION_OBSERVABILITY_ENABLED", raising=False,
    )
    assert inline_permission_observability_enabled() is True


def test_explicit_true_opens(monkeypatch):
    _enable(monkeypatch)
    assert inline_permission_observability_enabled() is True


def test_explicit_false_keeps_closed(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_IDE_INLINE_PERMISSION_OBSERVABILITY_ENABLED", "false",
    )
    assert inline_permission_observability_enabled() is False


def test_case_insensitive(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_IDE_INLINE_PERMISSION_OBSERVABILITY_ENABLED", "TRUE",
    )
    assert inline_permission_observability_enabled() is True


# ===========================================================================
# Authority invariant — no imports from gate/execution modules
# ===========================================================================


def test_observability_does_not_import_authority_modules():
    """CRITICAL: the observability module must stay a pure view layer.

    This pin is structural — grep the module source for any import of
    authority-carrying modules. If any appears, the test fails with a
    specific module name so the reviewer knows what leaked.
    """
    src = Path(
        "backend/core/ouroboros/governance/inline_permission_observability.py"
    ).read_text()
    forbidden = [
        "orchestrator",
        "policy_engine",
        "iron_gate",
        "risk_tier_floor",
        "semantic_guardian",
        "tool_executor",
        "candidate_generator",
        "change_engine",
    ]
    violations: List[str] = []
    # Allow the module name to appear inside sanitizing comments, but
    # flag any actual ``from ... import`` or ``import ...`` reference.
    for mod in forbidden:
        pattern = re.compile(
            rf"^\s*(from|import)\s+[^#\n]*{re.escape(mod)}",
            re.MULTILINE,
        )
        if pattern.search(src):
            violations.append(mod)
    assert not violations, (
        f"inline_permission_observability.py imports forbidden authority "
        f"modules: {violations}"
    )


# ===========================================================================
# /observability/permissions/prompts — endpoint contracts
# ===========================================================================


@pytest.mark.asyncio
async def test_prompts_list_disabled_returns_403(monkeypatch):
    router = InlinePermissionObservabilityRouter()
    req = _make_request("/observability/permissions/prompts")
    status, body = await _resolve(router._handle_prompts_list(req))
    assert status == 403
    assert body["reason_code"] == "inline_permission_observability.disabled"


@pytest.mark.asyncio
async def test_prompts_list_empty_when_enabled(monkeypatch):
    _enable(monkeypatch)
    router = InlinePermissionObservabilityRouter()
    req = _make_request("/observability/permissions/prompts")
    status, body = await _resolve(router._handle_prompts_list(req))
    assert status == 200
    assert body["count"] == 0
    assert body["prompts"] == []
    assert body["schema_version"] == \
        INLINE_PERMISSION_OBSERVABILITY_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_prompts_list_returns_pending(monkeypatch):
    _enable(monkeypatch)
    from backend.core.ouroboros.governance.inline_permission_prompt import (
        get_default_controller,
    )
    ctrl = get_default_controller()
    ctrl.request(_make_prompt_request(prompt_id="op-a:c1:aaa"))
    ctrl.request(_make_prompt_request(prompt_id="op-b:c1:bbb", op_id="op-b"))
    router = InlinePermissionObservabilityRouter()
    req = _make_request("/observability/permissions/prompts")
    status, body = await _resolve(router._handle_prompts_list(req))
    assert status == 200
    assert body["count"] == 2


@pytest.mark.asyncio
async def test_prompt_detail_malformed_400(monkeypatch):
    _enable(monkeypatch)
    router = InlinePermissionObservabilityRouter()
    # Space + shell-unsafe char rejected
    req = _make_request(
        "/observability/permissions/prompts/not%20valid",
        match_info={"prompt_id": "not valid"},
    )
    status, body = await _resolve(router._handle_prompt_detail(req))
    assert status == 400
    assert "malformed_prompt_id" in body["reason_code"]


@pytest.mark.asyncio
async def test_prompt_detail_unknown_404(monkeypatch):
    _enable(monkeypatch)
    router = InlinePermissionObservabilityRouter()
    req = _make_request(
        "/observability/permissions/prompts/op-x:c1:zzz",
        match_info={"prompt_id": "op-x:c1:zzz"},
    )
    status, body = await _resolve(router._handle_prompt_detail(req))
    assert status == 404
    assert body["reason_code"] == \
        "inline_permission_observability.unknown_prompt_id"


@pytest.mark.asyncio
async def test_prompt_detail_returns_projection(monkeypatch):
    _enable(monkeypatch)
    from backend.core.ouroboros.governance.inline_permission_prompt import (
        get_default_controller,
    )
    ctrl = get_default_controller()
    ctrl.request(_make_prompt_request(prompt_id="op-a:c1:aaa"))
    router = InlinePermissionObservabilityRouter()
    req = _make_request(
        "/observability/permissions/prompts/op-a:c1:aaa",
        match_info={"prompt_id": "op-a:c1:aaa"},
    )
    status, body = await _resolve(router._handle_prompt_detail(req))
    assert status == 200
    assert body["prompt"]["prompt_id"] == "op-a:c1:aaa"
    assert body["prompt"]["state"] == "pending"


# ===========================================================================
# /observability/permissions/grants — endpoint contracts
# ===========================================================================


@pytest.mark.asyncio
async def test_grants_list_empty_ok_when_enabled(monkeypatch, tmp_path: Path):
    _enable(monkeypatch)
    monkeypatch.chdir(tmp_path)
    router = InlinePermissionObservabilityRouter()
    req = _make_request("/observability/permissions/grants")
    status, body = await _resolve(router._handle_grants_list(req))
    assert status == 200
    assert body["grants"] == []


@pytest.mark.asyncio
async def test_grants_list_returns_active(monkeypatch, tmp_path: Path):
    _enable(monkeypatch)
    monkeypatch.chdir(tmp_path)
    from backend.core.ouroboros.governance.inline_permission_memory import (
        get_store_for_repo,
    )
    store = get_store_for_repo(tmp_path)
    g = store.grant(tool="bash", pattern="make test")
    router = InlinePermissionObservabilityRouter()
    req = _make_request("/observability/permissions/grants")
    status, body = await _resolve(router._handle_grants_list(req))
    assert status == 200
    assert body["count"] == 1
    assert body["grants"][0]["grant_id"] == g.grant_id
    # CRITICAL: pattern_preview, not pattern
    assert "pattern_preview" in body["grants"][0]
    assert "pattern" not in body["grants"][0]


@pytest.mark.asyncio
async def test_grant_detail_malformed_400(monkeypatch, tmp_path: Path):
    _enable(monkeypatch)
    monkeypatch.chdir(tmp_path)
    router = InlinePermissionObservabilityRouter()
    req = _make_request(
        "/observability/permissions/grants/bad-id",
        match_info={"grant_id": "not-a-grant-id"},
    )
    status, body = await _resolve(router._handle_grant_detail(req))
    assert status == 400
    assert "malformed_grant_id" in body["reason_code"]


@pytest.mark.asyncio
async def test_grant_detail_unknown_404(monkeypatch, tmp_path: Path):
    _enable(monkeypatch)
    monkeypatch.chdir(tmp_path)
    router = InlinePermissionObservabilityRouter()
    req = _make_request(
        "/observability/permissions/grants/ga-deadbeef",
        match_info={"grant_id": "ga-deadbeef"},
    )
    status, body = await _resolve(router._handle_grant_detail(req))
    assert status == 404


@pytest.mark.asyncio
async def test_grant_detail_returns_sanitized_pattern(
    monkeypatch, tmp_path: Path,
):
    _enable(monkeypatch)
    monkeypatch.chdir(tmp_path)
    from backend.core.ouroboros.governance.inline_permission_memory import (
        get_store_for_repo,
    )
    store = get_store_for_repo(tmp_path)
    g = store.grant(
        tool="bash", pattern="make test", operator_note="trusted",
    )
    router = InlinePermissionObservabilityRouter()
    req = _make_request(
        f"/observability/permissions/grants/{g.grant_id}",
        match_info={"grant_id": g.grant_id},
    )
    status, body = await _resolve(router._handle_grant_detail(req))
    assert status == 200
    assert "pattern_preview" in body["grant"]
    assert body["grant"]["tool"] == "bash"


# ===========================================================================
# Rate limiting
# ===========================================================================


@pytest.mark.asyncio
async def test_rate_limit_kicks_in(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv(
        "JARVIS_IDE_INLINE_PERMISSION_OBSERVABILITY_RATE_LIMIT_PER_MIN", "3",
    )
    router = InlinePermissionObservabilityRouter()
    for _ in range(3):
        req = _make_request("/observability/permissions/prompts")
        status, _ = await _resolve(router._handle_prompts_list(req))
        assert status == 200
    # 4th request is capped
    req = _make_request("/observability/permissions/prompts")
    status, body = await _resolve(router._handle_prompts_list(req))
    assert status == 429
    assert body["reason_code"] == \
        "inline_permission_observability.rate_limited"


# ===========================================================================
# CORS
# ===========================================================================


@pytest.mark.asyncio
async def test_cors_allows_localhost_origin(monkeypatch):
    _enable(monkeypatch)
    router = InlinePermissionObservabilityRouter()
    req = _make_request(
        "/observability/permissions/prompts",
        headers={"Origin": "http://localhost:3000"},
    )
    resp = await router._handle_prompts_list(req)
    assert resp.headers.get("Access-Control-Allow-Origin") == \
        "http://localhost:3000"


@pytest.mark.asyncio
async def test_cors_rejects_external_origin(monkeypatch):
    _enable(monkeypatch)
    router = InlinePermissionObservabilityRouter()
    req = _make_request(
        "/observability/permissions/prompts",
        headers={"Origin": "https://evil.example"},
    )
    resp = await router._handle_prompts_list(req)
    # No ACAO header — browser blocks cross-origin access silently.
    assert "Access-Control-Allow-Origin" not in resp.headers


@pytest.mark.asyncio
async def test_cache_control_no_store_on_every_response(monkeypatch):
    _enable(monkeypatch)
    router = InlinePermissionObservabilityRouter()
    req = _make_request("/observability/permissions/prompts")
    resp = await router._handle_prompts_list(req)
    assert resp.headers.get("Cache-Control") == "no-store"


# ===========================================================================
# Event-type allowlist completeness
# ===========================================================================


def test_broker_allowlist_includes_all_inline_event_types():
    for evt in (
        EVENT_TYPE_INLINE_PROMPT_PENDING,
        EVENT_TYPE_INLINE_PROMPT_ALLOWED,
        EVENT_TYPE_INLINE_PROMPT_DENIED,
        EVENT_TYPE_INLINE_PROMPT_EXPIRED,
        EVENT_TYPE_INLINE_PROMPT_PAUSED,
        EVENT_TYPE_INLINE_GRANT_CREATED,
        EVENT_TYPE_INLINE_GRANT_REVOKED,
    ):
        assert evt in _VALID_EVENT_TYPES, (
            f"broker allowlist missing inline event type: {evt}"
        )


# ===========================================================================
# Bridge — controller → broker
# ===========================================================================


@pytest.mark.asyncio
async def test_bridge_publishes_prompt_pending_to_broker(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
    ctrl = InlinePromptController(default_timeout_s=5.0)
    broker = get_default_broker()

    published: List[Dict[str, Any]] = []
    original = broker.publish

    def _capture(event_type, op_id, payload=None):
        published.append({
            "event_type": event_type, "op_id": op_id, "payload": payload,
        })
        return original(event_type, op_id, payload)

    broker.publish = _capture  # type: ignore[assignment]

    unsub = bridge_inline_permission_to_broker(
        controller=ctrl, broker=broker,
    )
    try:
        ctrl.request(_make_prompt_request(
            prompt_id="op-a:c1:abc", op_id="op-a",
        ))
        await asyncio.sleep(0.01)
    finally:
        unsub()
        broker.publish = original  # type: ignore[assignment]

    assert any(
        p["event_type"] == "inline_prompt_pending" and p["op_id"] == "op-a"
        for p in published
    )


@pytest.mark.asyncio
async def test_bridge_publishes_all_terminal_states(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
    ctrl = InlinePromptController(default_timeout_s=0.1)
    broker = get_default_broker()
    published: List[str] = []
    original = broker.publish

    def _capture(event_type, op_id, payload=None):
        published.append(event_type)
        return original(event_type, op_id, payload)

    broker.publish = _capture  # type: ignore[assignment]
    unsub = bridge_inline_permission_to_broker(
        controller=ctrl, broker=broker,
    )
    try:
        # Allowed
        fut_a = ctrl.request(_make_prompt_request(prompt_id="op-a:c:a"))
        ctrl.allow_once("op-a:c:a", reviewer="repl")
        await fut_a
        # Denied
        fut_d = ctrl.request(_make_prompt_request(prompt_id="op-d:c:d"))
        ctrl.deny("op-d:c:d", reviewer="repl", reason="no")
        await fut_d
        # Paused
        fut_p = ctrl.request(_make_prompt_request(prompt_id="op-p:c:p"))
        ctrl.pause_op("op-p:c:p", reviewer="repl")
        await fut_p
        # Expired
        fut_e = ctrl.request(_make_prompt_request(prompt_id="op-e:c:e"))
        await fut_e
    finally:
        unsub()
        broker.publish = original  # type: ignore[assignment]

    for expected in (
        "inline_prompt_pending",
        "inline_prompt_allowed",
        "inline_prompt_denied",
        "inline_prompt_paused",
        "inline_prompt_expired",
    ):
        assert expected in published, (
            f"bridge missed emitting {expected} (got {published})"
        )


# ===========================================================================
# Bridge — store → broker
# ===========================================================================


def test_store_fires_on_grant(tmp_path: Path):
    store = RememberedAllowStore(tmp_path)
    events: List[Dict[str, Any]] = []
    store.on_change(events.append)
    g = store.grant(tool="bash", pattern="make test")
    assert any(
        e.get("event_type") == "inline_grant_created"
        and e.get("grant_id") == g.grant_id
        for e in events
    )


def test_store_fires_on_revoke(tmp_path: Path):
    store = RememberedAllowStore(tmp_path)
    g = store.grant(tool="bash", pattern="make test")
    events: List[Dict[str, Any]] = []
    store.on_change(events.append)
    store.revoke(g.grant_id)
    assert any(
        e.get("event_type") == "inline_grant_revoked"
        and e.get("grant_id") == g.grant_id
        for e in events
    )


def test_store_projection_never_emits_raw_pattern(tmp_path: Path):
    store = RememberedAllowStore(tmp_path)
    events: List[Dict[str, Any]] = []
    store.on_change(events.append)
    store.grant(tool="bash", pattern="make test")
    assert events, "must have fired at least one event"
    proj = events[0]["projection"]
    assert "pattern_preview" in proj
    assert "pattern" not in proj


@pytest.mark.asyncio
async def test_bridge_publishes_grant_events_with_sentinel_op_id(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
    ctrl = InlinePromptController(default_timeout_s=5.0)
    store = RememberedAllowStore(tmp_path)
    broker = get_default_broker()
    captured: List[Dict[str, Any]] = []
    original = broker.publish

    def _capture(event_type, op_id, payload=None):
        captured.append({
            "event_type": event_type, "op_id": op_id, "payload": payload,
        })
        return original(event_type, op_id, payload)

    broker.publish = _capture  # type: ignore[assignment]
    unsub = bridge_inline_permission_to_broker(
        controller=ctrl, store=store, broker=broker,
    )
    try:
        g = store.grant(tool="bash", pattern="make test")
        store.revoke(g.grant_id)
        await asyncio.sleep(0.01)
    finally:
        unsub()
        broker.publish = original  # type: ignore[assignment]

    created = [c for c in captured if c["event_type"] == "inline_grant_created"]
    revoked = [c for c in captured if c["event_type"] == "inline_grant_revoked"]
    assert len(created) == 1
    assert len(revoked) == 1
    assert created[0]["op_id"] == _GRANTS_SENTINEL_OP_ID
    assert revoked[0]["op_id"] == _GRANTS_SENTINEL_OP_ID
    # Sanitized projection in SSE payload
    assert "pattern_preview" in created[0]["payload"]
    assert "pattern" not in created[0]["payload"]


@pytest.mark.asyncio
async def test_bridge_is_silent_when_stream_disabled(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "false")
    ctrl = InlinePromptController(default_timeout_s=5.0)
    store = RememberedAllowStore(tmp_path)
    broker = get_default_broker()
    captured: List[str] = []
    original = broker.publish

    def _capture(event_type, op_id, payload=None):
        captured.append(event_type)
        return original(event_type, op_id, payload)

    broker.publish = _capture  # type: ignore[assignment]
    unsub = bridge_inline_permission_to_broker(
        controller=ctrl, store=store, broker=broker,
    )
    try:
        ctrl.request(_make_prompt_request(prompt_id="op-a:c:a"))
        store.grant(tool="bash", pattern="make test")
        await asyncio.sleep(0.01)
    finally:
        unsub()
        broker.publish = original  # type: ignore[assignment]
    # Stream off → bridge no-ops → no publishes
    inline_pub = [
        c for c in captured
        if c.startswith("inline_prompt") or c.startswith("inline_grant")
    ]
    assert inline_pub == []


@pytest.mark.asyncio
async def test_bridge_unsub_stops_forwarding(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
    ctrl = InlinePromptController(default_timeout_s=5.0)
    broker = get_default_broker()
    captured: List[str] = []
    original = broker.publish

    def _capture(event_type, op_id, payload=None):
        captured.append(event_type)
        return original(event_type, op_id, payload)

    broker.publish = _capture  # type: ignore[assignment]

    unsub = bridge_inline_permission_to_broker(
        controller=ctrl, broker=broker,
    )
    ctrl.request(_make_prompt_request(prompt_id="op-a:c:a"))
    await asyncio.sleep(0.01)
    unsub()
    ctrl.request(_make_prompt_request(prompt_id="op-b:c:b", op_id="op-b"))
    await asyncio.sleep(0.01)
    broker.publish = original  # type: ignore[assignment]

    # Only op-a's event should appear; op-b fired after unsub.
    op_events = [c for c in captured if c == "inline_prompt_pending"]
    assert len(op_events) == 1


# ===========================================================================
# Route registration shape
# ===========================================================================


def test_register_routes_wires_four_paths():
    app = web.Application()
    InlinePermissionObservabilityRouter().register_routes(app)
    paths = {
        r.resource.canonical
        for r in app.router.routes()
    }
    assert "/observability/permissions/prompts" in paths
    assert "/observability/permissions/prompts/{prompt_id}" in paths
    assert "/observability/permissions/grants" in paths
    assert "/observability/permissions/grants/{grant_id}" in paths


# ===========================================================================
# Prompt id regex accepts colon-separated shape
# ===========================================================================


@pytest.mark.asyncio
async def test_prompt_id_regex_accepts_canonical_shape(
    monkeypatch,
):
    _enable(monkeypatch)
    # The canonical shape is "op-xxx:cid:hex8" — colons are allowed
    router = InlinePermissionObservabilityRouter()
    req = _make_request(
        "/observability/permissions/prompts/op-abc:r0.0:bash:deadbe",
        match_info={"prompt_id": "op-abc:r0.0:bash:deadbe"},
    )
    status, body = await _resolve(router._handle_prompt_detail(req))
    # We expect 404 (not 400) — the id SHAPE is legal, the id is just
    # not in the registry.
    assert status == 404


# ===========================================================================
# Grant id regex is strict
# ===========================================================================


def test_grant_id_regex_requires_ga_prefix_and_hex():
    from backend.core.ouroboros.governance.inline_permission_observability \
        import _GRANT_ID_RE
    assert _GRANT_ID_RE.match("ga-0123456789")
    assert not _GRANT_ID_RE.match("ga-")
    assert not _GRANT_ID_RE.match("not-a-grant")
    assert not _GRANT_ID_RE.match("ga-XYZ")  # non-hex


# ===========================================================================
# schema_version on every response shape
# ===========================================================================


@pytest.mark.asyncio
async def test_every_response_carries_schema_version(monkeypatch, tmp_path: Path):
    _enable(monkeypatch)
    monkeypatch.chdir(tmp_path)
    router = InlinePermissionObservabilityRouter()
    # Enabled paths
    for path, handler, mi in [
        ("/observability/permissions/prompts",
         router._handle_prompts_list, None),
        ("/observability/permissions/prompts/op-a:c:a",
         router._handle_prompt_detail, {"prompt_id": "op-a:c:a"}),
        ("/observability/permissions/grants",
         router._handle_grants_list, None),
        ("/observability/permissions/grants/ga-0123456789",
         router._handle_grant_detail, {"grant_id": "ga-0123456789"}),
    ]:
        req = _make_request(path, match_info=mi)
        _, body = await _resolve(handler(req))
        assert body.get("schema_version") == \
            INLINE_PERMISSION_OBSERVABILITY_SCHEMA_VERSION
