"""Slice 4 tests — CompactionManifest + IDE observability + bridge."""
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

from backend.core.ouroboros.governance.context_intent import (
    ChunkCandidate,
    IntentTracker,
    PreservationScorer,
    TurnSource,
    reset_default_tracker_registry,
)
from backend.core.ouroboros.governance.context_ledger import (
    ContextLedger,
    reset_default_registry,
)
from backend.core.ouroboros.governance.context_manifest import (
    CONTEXT_MANIFEST_SCHEMA_VERSION,
    CONTEXT_OBSERVABILITY_SCHEMA_VERSION,
    CompactionManifest,
    ContextObservabilityRouter,
    PreservationReason,
    bridge_context_preservation_to_broker,
    context_observability_enabled,
    get_default_manifest_registry,
    manifest_for,
    project_record_full,
    project_record_summary,
    reset_default_manifest_registry,
)
from backend.core.ouroboros.governance.context_pins import (
    ContextPinRegistry,
    PinSource,
    reset_default_pin_registries,
)
from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_CONTEXT_COMPACTED,
    EVENT_TYPE_CONTEXT_PIN_EXPIRED,
    EVENT_TYPE_CONTEXT_PINNED,
    EVENT_TYPE_CONTEXT_UNPINNED,
    EVENT_TYPE_LEDGER_ENTRY_ADDED,
    _VALID_EVENT_TYPES,
    get_default_broker,
    reset_default_broker,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for key in list(os.environ.keys()):
        if key.startswith("JARVIS_CONTEXT_OBSERVABILITY_") or \
           key.startswith("JARVIS_CONTEXT_MANIFEST_"):
            monkeypatch.delenv(key, raising=False)
    reset_default_registry()
    reset_default_tracker_registry()
    reset_default_pin_registries()
    reset_default_manifest_registry()
    reset_default_broker()
    yield
    reset_default_registry()
    reset_default_tracker_registry()
    reset_default_pin_registries()
    reset_default_manifest_registry()
    reset_default_broker()


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


async def _resolve(handler_coro):
    resp = await handler_coro
    return resp.status, json.loads(resp.body)


def _enable_observability(monkeypatch):
    monkeypatch.setenv("JARVIS_CONTEXT_OBSERVABILITY_ENABLED", "true")


# ===========================================================================
# Schema version constants
# ===========================================================================


def test_schema_versions_stable():
    assert CONTEXT_MANIFEST_SCHEMA_VERSION == "context_manifest.v1"
    assert CONTEXT_OBSERVABILITY_SCHEMA_VERSION == "1.0"


# ===========================================================================
# Env kill switch
# ===========================================================================


def test_default_is_on_post_slice_5(monkeypatch):
    """Graduated 2026-04-21. Pure read surface, safe to default-on."""
    monkeypatch.delenv(
        "JARVIS_CONTEXT_OBSERVABILITY_ENABLED", raising=False,
    )
    assert context_observability_enabled() is True


def test_explicit_false_keeps_kill_switch(monkeypatch):
    monkeypatch.setenv("JARVIS_CONTEXT_OBSERVABILITY_ENABLED", "false")
    assert context_observability_enabled() is False


def test_explicit_true_opens(monkeypatch):
    _enable_observability(monkeypatch)
    assert context_observability_enabled() is True


# ===========================================================================
# Authority invariant
# ===========================================================================


def test_manifest_has_no_authority_imports():
    src = Path(
        "backend/core/ouroboros/governance/context_manifest.py"
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
    for mod in forbidden:
        pattern = re.compile(
            rf"^\s*(from|import)\s+[^#\n]*{re.escape(mod)}",
            re.MULTILINE,
        )
        assert not pattern.search(src), (
            f"context_manifest imports forbidden module: {mod}"
        )


# ===========================================================================
# Manifest record shape
# ===========================================================================


def _make_pres_result(op_id: str):
    """Build a PreservationResult against real components so manifest
    records are tested end-to-end."""
    tracker = IntentTracker(op_id)
    tracker.ingest_turn(
        "focus on backend/hot.py", source=TurnSource.USER,
    )
    scorer = PreservationScorer()
    cands = [
        ChunkCandidate(
            chunk_id="c-hot", text="read backend/hot.py",
            index_in_sequence=5, role="user",
        ),
        ChunkCandidate(
            chunk_id="c-noise", text="unrelated",
            index_in_sequence=0, role="assistant",
        ),
        ChunkCandidate(
            chunk_id="c-fresh", text="latest turn",
            index_in_sequence=10, role="user",
        ),
    ]
    return tracker, scorer.select_preserved(cands, tracker.current_intent(),
                                            max_chunks=2)


def test_record_pass_emits_row_per_chunk():
    tracker, result = _make_pres_result("op-1")
    manifest = CompactionManifest("op-1")
    record = manifest.record_pass(
        preservation_result=result,
        intent_snapshot=tracker.current_intent(),
    )
    # 3 input chunks → 3 rows
    assert len(record.rows) == 3
    assert record.kept_count + record.compacted_count + record.dropped_count == 3
    assert record.pass_id.startswith("mf-")


def test_record_pass_preserves_reason_codes():
    tracker, result = _make_pres_result("op-2")
    manifest = CompactionManifest("op-2")
    record = manifest.record_pass(
        preservation_result=result,
        intent_snapshot=tracker.current_intent(),
    )
    reasons = {r.reason for r in record.rows}
    # At least one reason code per section we covered
    expected_possible = {
        PreservationReason.PINNED.value,
        PreservationReason.HIGH_INTENT.value,
        PreservationReason.HIGH_STRUCTURAL.value,
        PreservationReason.RECENT.value,
        PreservationReason.BUDGET_EXHAUSTED_KEEP_RATIO.value,
        PreservationReason.BUDGET_EXHAUSTED_DROPPED.value,
    }
    assert reasons <= expected_possible


def test_record_pass_rows_sorted_by_index_in_sequence():
    tracker, result = _make_pres_result("op-3")
    manifest = CompactionManifest("op-3")
    record = manifest.record_pass(
        preservation_result=result,
        intent_snapshot=tracker.current_intent(),
    )
    indices = [r.index_in_sequence for r in record.rows]
    assert indices == sorted(indices)


def test_record_pass_stamps_schema_version():
    tracker, result = _make_pres_result("op-4")
    manifest = CompactionManifest("op-4")
    record = manifest.record_pass(
        preservation_result=result,
        intent_snapshot=tracker.current_intent(),
    )
    assert record.schema_version == CONTEXT_MANIFEST_SCHEMA_VERSION


def test_manifest_cap_truncates_oldest_records():
    manifest = CompactionManifest("op-x", max_records=3)
    for _ in range(5):
        _t, r = _make_pres_result("op-x")
        manifest.record_pass(preservation_result=r)
    assert len(manifest.all_records()) == 3


def test_get_by_pass_id_round_trips():
    manifest = CompactionManifest("op-g")
    _t, r = _make_pres_result("op-g")
    rec = manifest.record_pass(preservation_result=r)
    assert manifest.get(rec.pass_id) is not None


def test_latest_returns_newest():
    manifest = CompactionManifest("op-l")
    _t, r = _make_pres_result("op-l")
    r1 = manifest.record_pass(preservation_result=r)
    _t, r2 = _make_pres_result("op-l")
    r2_rec = manifest.record_pass(preservation_result=r2)
    assert manifest.latest().pass_id == r2_rec.pass_id


def test_projection_summary_omits_rows():
    tracker, result = _make_pres_result("op-s")
    manifest = CompactionManifest("op-s")
    record = manifest.record_pass(
        preservation_result=result,
        intent_snapshot=tracker.current_intent(),
    )
    summary = project_record_summary(record)
    assert "rows" not in summary
    assert summary["schema_version"] == CONTEXT_MANIFEST_SCHEMA_VERSION


def test_projection_full_includes_rows():
    tracker, result = _make_pres_result("op-f")
    manifest = CompactionManifest("op-f")
    record = manifest.record_pass(
        preservation_result=result,
        intent_snapshot=tracker.current_intent(),
    )
    full = project_record_full(record)
    assert "rows" in full
    assert len(full["rows"]) == len(record.rows)


# ===========================================================================
# JSON-safe inf clamp (pinned chunks)
# ===========================================================================


def test_pinned_chunk_score_serialises_finite():
    """Pinned chunks have total=math.inf which isn't JSON-safe; the
    manifest clamps to a sentinel for over-the-wire emission."""
    tracker = IntentTracker("op-j")
    scorer = PreservationScorer()
    cands = [
        ChunkCandidate(
            chunk_id="pinned", text="x",
            index_in_sequence=0, role="tool", pinned=True,
        ),
    ]
    result = scorer.select_preserved(cands, tracker.current_intent())
    manifest = CompactionManifest("op-j")
    record = manifest.record_pass(
        preservation_result=result, intent_snapshot=tracker.current_intent(),
    )
    full = project_record_full(record)
    # The row score is clamped (not infinity)
    row_score = full["rows"][0]["total_score"]
    assert row_score < 1e19
    # And JSON.dumps succeeds
    json.dumps(full)


# ===========================================================================
# Listener hooks
# ===========================================================================


def test_on_change_fires_on_record_pass():
    manifest = CompactionManifest("op-l2")
    events: List[Dict[str, Any]] = []
    manifest.on_change(events.append)
    _t, result = _make_pres_result("op-l2")
    manifest.record_pass(preservation_result=result)
    assert len(events) == 1
    assert events[0]["event_type"] == "context_compacted"
    assert "projection" in events[0]


def test_listener_exception_does_not_break_record():
    manifest = CompactionManifest("op-l3")

    def _bad(_p: Dict[str, Any]) -> None:
        raise RuntimeError("boom")

    manifest.on_change(_bad)
    _t, result = _make_pres_result("op-l3")
    manifest.record_pass(preservation_result=result)
    assert manifest.latest() is not None


# ===========================================================================
# Event type allowlist completeness (bridge → broker)
# ===========================================================================


def test_broker_allowlist_includes_all_context_event_types():
    for evt in (
        EVENT_TYPE_LEDGER_ENTRY_ADDED,
        EVENT_TYPE_CONTEXT_COMPACTED,
        EVENT_TYPE_CONTEXT_PINNED,
        EVENT_TYPE_CONTEXT_UNPINNED,
        EVENT_TYPE_CONTEXT_PIN_EXPIRED,
    ):
        assert evt in _VALID_EVENT_TYPES, (
            f"broker allowlist missing context event type: {evt}"
        )


# ===========================================================================
# Bridge — end-to-end
# ===========================================================================


@pytest.mark.asyncio
async def test_bridge_publishes_ledger_event(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
    ledger = ContextLedger("op-bridge")
    broker = get_default_broker()
    captured: List[str] = []
    original = broker.publish

    def _capture(event_type, op_id, payload=None):
        captured.append(event_type)
        return original(event_type, op_id, payload)

    broker.publish = _capture  # type: ignore[assignment]
    unsub = bridge_context_preservation_to_broker(
        ledger=ledger, broker=broker,
    )
    try:
        ledger.record_file_read(file_path="backend/a.py")
        await asyncio.sleep(0.01)
    finally:
        unsub()
        broker.publish = original  # type: ignore[assignment]
    assert "ledger_entry_added" in captured


@pytest.mark.asyncio
async def test_bridge_publishes_pin_events(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
    pins = ContextPinRegistry("op-bridge-pins")
    broker = get_default_broker()
    captured: List[str] = []
    original = broker.publish

    def _capture(event_type, op_id, payload=None):
        captured.append(event_type)
        return original(event_type, op_id, payload)

    broker.publish = _capture  # type: ignore[assignment]
    unsub = bridge_context_preservation_to_broker(
        pin_registry=pins, broker=broker,
    )
    try:
        p = pins.pin(chunk_id="c", source=PinSource.OPERATOR)
        pins.unpin(p.pin_id)
        await asyncio.sleep(0.01)
    finally:
        unsub()
        broker.publish = original  # type: ignore[assignment]
    assert "context_pinned" in captured
    assert "context_unpinned" in captured


@pytest.mark.asyncio
async def test_bridge_publishes_manifest_events(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
    manifest = CompactionManifest("op-bridge-m")
    broker = get_default_broker()
    captured: List[str] = []
    original = broker.publish

    def _capture(event_type, op_id, payload=None):
        captured.append(event_type)
        return original(event_type, op_id, payload)

    broker.publish = _capture  # type: ignore[assignment]
    unsub = bridge_context_preservation_to_broker(
        manifest=manifest, broker=broker,
    )
    try:
        _t, result = _make_pres_result("op-bridge-m")
        manifest.record_pass(preservation_result=result)
        await asyncio.sleep(0.01)
    finally:
        unsub()
        broker.publish = original  # type: ignore[assignment]
    assert "context_compacted" in captured


@pytest.mark.asyncio
async def test_bridge_silent_when_stream_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "false")
    ledger = ContextLedger("op-silent")
    broker = get_default_broker()
    captured: List[str] = []
    original = broker.publish

    def _capture(event_type, op_id, payload=None):
        captured.append(event_type)
        return original(event_type, op_id, payload)

    broker.publish = _capture  # type: ignore[assignment]
    unsub = bridge_context_preservation_to_broker(
        ledger=ledger, broker=broker,
    )
    try:
        ledger.record_file_read(file_path="x.py")
        await asyncio.sleep(0.01)
    finally:
        unsub()
        broker.publish = original  # type: ignore[assignment]
    assert captured == []


@pytest.mark.asyncio
async def test_bridge_unsub_stops_forwarding(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
    ledger = ContextLedger("op-u")
    broker = get_default_broker()
    captured: List[str] = []
    original = broker.publish

    def _capture(event_type, op_id, payload=None):
        captured.append(event_type)
        return original(event_type, op_id, payload)

    broker.publish = _capture  # type: ignore[assignment]
    unsub = bridge_context_preservation_to_broker(
        ledger=ledger, broker=broker,
    )
    ledger.record_file_read(file_path="a.py")
    await asyncio.sleep(0.01)
    unsub()
    ledger.record_file_read(file_path="b.py")
    await asyncio.sleep(0.01)
    broker.publish = original  # type: ignore[assignment]
    # Only one event captured
    assert len([c for c in captured if c == "ledger_entry_added"]) == 1


# ===========================================================================
# Endpoints
# ===========================================================================


@pytest.mark.asyncio
async def test_endpoint_disabled_returns_403(monkeypatch):
    """Kill switch: explicit =false returns 403 even post-graduation."""
    monkeypatch.setenv("JARVIS_CONTEXT_OBSERVABILITY_ENABLED", "false")
    router = ContextObservabilityRouter()
    req = _make_request("/observability/context/manifest")
    status, body = await _resolve(router._handle_manifest_index(req))
    assert status == 403


@pytest.mark.asyncio
async def test_manifest_index_empty_when_enabled(monkeypatch):
    _enable_observability(monkeypatch)
    router = ContextObservabilityRouter()
    req = _make_request("/observability/context/manifest")
    status, body = await _resolve(router._handle_manifest_index(req))
    assert status == 200
    assert body["count"] == 0
    assert body["op_ids"] == []


@pytest.mark.asyncio
async def test_manifest_index_lists_ops(monkeypatch):
    _enable_observability(monkeypatch)
    for op_id in ("op-a", "op-b"):
        _t, r = _make_pres_result(op_id)
        manifest_for(op_id).record_pass(preservation_result=r)
    router = ContextObservabilityRouter()
    req = _make_request("/observability/context/manifest")
    status, body = await _resolve(router._handle_manifest_index(req))
    assert status == 200
    assert sorted(body["op_ids"]) == ["op-a", "op-b"]


@pytest.mark.asyncio
async def test_manifest_detail_returns_records(monkeypatch):
    _enable_observability(monkeypatch)
    _t, r = _make_pres_result("op-m")
    manifest_for("op-m").record_pass(preservation_result=r)
    router = ContextObservabilityRouter()
    req = _make_request(
        "/observability/context/manifest/op-m",
        match_info={"op_id": "op-m"},
    )
    status, body = await _resolve(router._handle_manifest_detail(req))
    assert status == 200
    assert body["op_id"] == "op-m"
    assert body["count"] == 1


@pytest.mark.asyncio
async def test_manifest_detail_malformed_400(monkeypatch):
    _enable_observability(monkeypatch)
    router = ContextObservabilityRouter()
    req = _make_request(
        "/observability/context/manifest/bad%20id",
        match_info={"op_id": "bad id"},
    )
    status, body = await _resolve(router._handle_manifest_detail(req))
    assert status == 400


@pytest.mark.asyncio
async def test_manifest_detail_unknown_404(monkeypatch):
    _enable_observability(monkeypatch)
    router = ContextObservabilityRouter()
    req = _make_request(
        "/observability/context/manifest/op-nope",
        match_info={"op_id": "op-nope"},
    )
    status, body = await _resolve(router._handle_manifest_detail(req))
    assert status == 404


@pytest.mark.asyncio
async def test_ledger_endpoint_returns_summary(monkeypatch):
    _enable_observability(monkeypatch)
    from backend.core.ouroboros.governance.context_ledger import ledger_for
    ledger_for("op-l").record_file_read(file_path="x.py")
    router = ContextObservabilityRouter()
    req = _make_request(
        "/observability/context/ledger/op-l",
        match_info={"op_id": "op-l"},
    )
    status, body = await _resolve(router._handle_ledger(req))
    assert status == 200
    assert body["op_id"] == "op-l"
    assert body["summary"]["counts_by_kind"]["file_read"] == 1


@pytest.mark.asyncio
async def test_intent_endpoint_returns_snapshot(monkeypatch):
    _enable_observability(monkeypatch)
    from backend.core.ouroboros.governance.context_intent import (
        intent_tracker_for,
    )
    tracker = intent_tracker_for("op-i")
    tracker.ingest_turn("backend/x.py", source=TurnSource.USER)
    router = ContextObservabilityRouter()
    req = _make_request(
        "/observability/context/intent/op-i",
        match_info={"op_id": "op-i"},
    )
    status, body = await _resolve(router._handle_intent(req))
    assert status == 200
    assert "backend/x.py" in body["intent"].get("recent_paths", [])


@pytest.mark.asyncio
async def test_pins_endpoint_returns_active(monkeypatch):
    _enable_observability(monkeypatch)
    from backend.core.ouroboros.governance.context_pins import (
        pin_registry_for,
    )
    reg = pin_registry_for("op-p")
    reg.pin(chunk_id="c", source=PinSource.OPERATOR)
    router = ContextObservabilityRouter()
    req = _make_request(
        "/observability/context/pins/op-p",
        match_info={"op_id": "op-p"},
    )
    status, body = await _resolve(router._handle_pins(req))
    assert status == 200
    assert body["count"] == 1


@pytest.mark.asyncio
async def test_pins_endpoint_unknown_op_returns_empty(monkeypatch):
    _enable_observability(monkeypatch)
    router = ContextObservabilityRouter()
    req = _make_request(
        "/observability/context/pins/op-nonexistent",
        match_info={"op_id": "op-nonexistent"},
    )
    status, body = await _resolve(router._handle_pins(req))
    # For pins we return an empty list rather than 404 — more operator-friendly
    assert status == 200
    assert body["count"] == 0


# ===========================================================================
# Rate limit
# ===========================================================================


@pytest.mark.asyncio
async def test_rate_limit_returns_429(monkeypatch):
    _enable_observability(monkeypatch)
    monkeypatch.setenv(
        "JARVIS_CONTEXT_OBSERVABILITY_RATE_LIMIT_PER_MIN", "2",
    )
    router = ContextObservabilityRouter()
    for _ in range(2):
        req = _make_request("/observability/context/manifest")
        status, _ = await _resolve(router._handle_manifest_index(req))
        assert status == 200
    req = _make_request("/observability/context/manifest")
    status, body = await _resolve(router._handle_manifest_index(req))
    assert status == 429


# ===========================================================================
# CORS + schema_version
# ===========================================================================


@pytest.mark.asyncio
async def test_cors_allowlist_reflects_origin(monkeypatch):
    _enable_observability(monkeypatch)
    router = ContextObservabilityRouter()
    req = _make_request(
        "/observability/context/manifest",
        headers={"Origin": "http://localhost:3000"},
    )
    resp = await router._handle_manifest_index(req)
    assert resp.headers.get("Access-Control-Allow-Origin") == \
        "http://localhost:3000"


@pytest.mark.asyncio
async def test_cors_rejects_evil_origin(monkeypatch):
    _enable_observability(monkeypatch)
    router = ContextObservabilityRouter()
    req = _make_request(
        "/observability/context/manifest",
        headers={"Origin": "https://evil.example"},
    )
    resp = await router._handle_manifest_index(req)
    assert "Access-Control-Allow-Origin" not in resp.headers


@pytest.mark.asyncio
async def test_cache_control_no_store(monkeypatch):
    _enable_observability(monkeypatch)
    router = ContextObservabilityRouter()
    req = _make_request("/observability/context/manifest")
    resp = await router._handle_manifest_index(req)
    assert resp.headers.get("Cache-Control") == "no-store"


@pytest.mark.asyncio
async def test_schema_version_on_every_response(monkeypatch):
    _enable_observability(monkeypatch)
    from backend.core.ouroboros.governance.context_ledger import ledger_for
    from backend.core.ouroboros.governance.context_intent import (
        intent_tracker_for,
    )
    from backend.core.ouroboros.governance.context_pins import (
        pin_registry_for,
    )
    ledger_for("op-q").record_file_read(file_path="x.py")
    intent_tracker_for("op-q").ingest_turn(
        "x.py", source=TurnSource.USER,
    )
    pin_registry_for("op-q").pin(chunk_id="c", source=PinSource.OPERATOR)
    _t, r = _make_pres_result("op-q")
    manifest_for("op-q").record_pass(preservation_result=r)
    router = ContextObservabilityRouter()
    endpoints = [
        ("/observability/context/manifest", router._handle_manifest_index, None),
        ("/observability/context/manifest/op-q",
         router._handle_manifest_detail, {"op_id": "op-q"}),
        ("/observability/context/ledger/op-q",
         router._handle_ledger, {"op_id": "op-q"}),
        ("/observability/context/intent/op-q",
         router._handle_intent, {"op_id": "op-q"}),
        ("/observability/context/pins/op-q",
         router._handle_pins, {"op_id": "op-q"}),
    ]
    for path, handler, mi in endpoints:
        req = _make_request(path, match_info=mi)
        _, body = await _resolve(handler(req))
        assert body.get("schema_version") == \
            CONTEXT_OBSERVABILITY_SCHEMA_VERSION


# ===========================================================================
# Router registers all five routes
# ===========================================================================


def test_register_routes_wires_five_paths():
    app = web.Application()
    ContextObservabilityRouter().register_routes(app)
    paths = {r.resource.canonical for r in app.router.routes()}
    assert "/observability/context/ledger/{op_id}" in paths
    assert "/observability/context/intent/{op_id}" in paths
    assert "/observability/context/pins/{op_id}" in paths
    assert "/observability/context/manifest" in paths
    assert "/observability/context/manifest/{op_id}" in paths


# ===========================================================================
# Registry
# ===========================================================================


def test_registry_returns_singleton_per_op():
    reg = get_default_manifest_registry()
    a = reg.get_or_create("op-a")
    b = reg.get_or_create("op-a")
    assert a is b


def test_registry_rejects_empty_op_id():
    reg = get_default_manifest_registry()
    with pytest.raises(ValueError):
        reg.get_or_create("")


def test_active_op_ids_reflects_registered():
    reg = get_default_manifest_registry()
    reg.get_or_create("op-x")
    reg.get_or_create("op-y")
    assert set(reg.active_op_ids()) == {"op-x", "op-y"}
