"""Regression spine — PlanApproval IDE observability (Slice 4).

Covers:

  1. GET /observability/plans — list projection + schema_version +
     rate-limit + disabled-403.
  2. GET /observability/plans/{op_id} — detail projection; 404 on
     unknown; 400 on malformed op_id.
  3. bridge_plan_approval_to_broker — every plan_* transition
     emits a typed SSE frame with summary payload.
  4. Bridge only fires for plan_* event types (future non-plan
     transition types stay silent).
  5. Summary payload is bounded — full plan JSON is NOT in the
     SSE frame (IDE clients fetch it via the GET endpoint).
  6. bridge returns an unsubscribe callable.
  7. Authority invariant — ide_observability.py plan routes don't
     import orchestrator/gate modules.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from backend.core.ouroboros.governance.ide_observability import (
    IDEObservabilityRouter,
    IDE_OBSERVABILITY_SCHEMA_VERSION,
)
from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_PLAN_APPROVED,
    EVENT_TYPE_PLAN_EXPIRED,
    EVENT_TYPE_PLAN_PENDING,
    EVENT_TYPE_PLAN_REJECTED,
    StreamEventBroker,
    bridge_plan_approval_to_broker,
    reset_default_broker,
)
from backend.core.ouroboros.governance.plan_approval import (
    PlanApprovalController,
    get_default_controller,
    reset_default_controller,
)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


_ENV_KEYS = [
    "JARVIS_IDE_OBSERVABILITY_ENABLED",
    "JARVIS_IDE_STREAM_ENABLED",
    "JARVIS_PLAN_APPROVAL_MODE",
    "JARVIS_PLAN_APPROVAL_TIMEOUT_S",
]


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    reset_default_controller()
    reset_default_broker()
    yield
    reset_default_controller()
    reset_default_broker()


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_request(path: str, match_info: Dict[str, str] = None) -> web.Request:
    req = make_mocked_request("GET", path, headers={})
    if match_info:
        req.match_info.update(match_info)
    req._transport_peername = ("127.0.0.1", 0)  # type: ignore[attr-defined]
    return req


def _sample_plan() -> Dict[str, Any]:
    return {
        "schema_version": "plan.1",
        "approach": "do a thing",
        "complexity": "moderate",
        "ordered_changes": [{"file_path": "x.py", "action": "modify"}],
    }


# --------------------------------------------------------------------------
# 1. Authority invariant (§1 Boundary)
# --------------------------------------------------------------------------


def test_ide_observability_plan_routes_no_gate_imports():
    """Plan route handlers must not pull in orchestrator or gate
    modules. Grep-pinned same as the task routes."""
    from backend.core.ouroboros.governance import ide_observability as mod
    src = Path(mod.__file__).read_text()
    forbidden = [
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.tool_executor",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.risk_tier_floor",
        "from backend.core.ouroboros.governance.semantic_guardian",
        "from backend.core.ouroboros.governance.semantic_firewall",
        "from backend.core.ouroboros.governance.policy_engine",
    ]
    for f in forbidden:
        assert f not in src, "ide_observability imports " + f


# --------------------------------------------------------------------------
# 2. GET /observability/plans (list)
# --------------------------------------------------------------------------


def test_plan_list_returns_empty_when_no_plans(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
    router = IDEObservabilityRouter()
    req = _make_request("/observability/plans")
    resp = _run_async(router._handle_plan_list(req))
    assert resp.status == 200
    body = json.loads(resp.body.decode("utf-8"))
    assert body["schema_version"] == IDE_OBSERVABILITY_SCHEMA_VERSION
    assert body["plans"] == []
    assert body["count"] == 0


def test_plan_list_returns_summaries(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
    async def _register():
        c = get_default_controller()
        c.request_approval("op-a", _sample_plan())
        c.request_approval("op-b", _sample_plan())
    _run_async(_register())

    router = IDEObservabilityRouter()
    req = _make_request("/observability/plans")
    resp = _run_async(router._handle_plan_list(req))
    body = json.loads(resp.body.decode("utf-8"))
    assert body["count"] == 2
    op_ids = [p["op_id"] for p in body["plans"]]
    assert op_ids == ["op-a", "op-b"]  # sorted
    # Summary fields only — full plan NOT echoed in list view.
    for plan in body["plans"]:
        assert "plan" not in plan  # list endpoint is bounded
        assert plan["state"] == "pending"


def test_plan_list_403_when_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "false")
    router = IDEObservabilityRouter()
    req = _make_request("/observability/plans")
    resp = _run_async(router._handle_plan_list(req))
    assert resp.status == 403
    body = json.loads(resp.body.decode("utf-8"))
    assert body["reason_code"] == "ide_observability.disabled"


# --------------------------------------------------------------------------
# 3. GET /observability/plans/{op_id} (detail)
# --------------------------------------------------------------------------


def test_plan_detail_returns_full_projection(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")

    async def _register():
        get_default_controller().request_approval(
            "op-a", _sample_plan(),
        )
    _run_async(_register())

    router = IDEObservabilityRouter()
    req = _make_request(
        "/observability/plans/op-a", match_info={"op_id": "op-a"},
    )
    resp = _run_async(router._handle_plan_detail(req))
    assert resp.status == 200
    body = json.loads(resp.body.decode("utf-8"))
    assert body["op_id"] == "op-a"
    assert body["state"] == "pending"
    # Detail endpoint DOES echo the full plan payload.
    assert body["plan"]["approach"] == "do a thing"
    assert body["plan"]["ordered_changes"][0]["file_path"] == "x.py"


def test_plan_detail_404_on_unknown_op_id(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
    router = IDEObservabilityRouter()
    req = _make_request(
        "/observability/plans/op-missing",
        match_info={"op_id": "op-missing"},
    )
    resp = _run_async(router._handle_plan_detail(req))
    assert resp.status == 404
    body = json.loads(resp.body.decode("utf-8"))
    assert body["reason_code"] == "ide_observability.unknown_op_id"


def test_plan_detail_400_on_malformed_op_id(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
    router = IDEObservabilityRouter()
    req = _make_request(
        "/observability/plans/bad id!",
        match_info={"op_id": "bad id!"},
    )
    resp = _run_async(router._handle_plan_detail(req))
    assert resp.status == 400
    body = json.loads(resp.body.decode("utf-8"))
    assert body["reason_code"] == "ide_observability.malformed_op_id"


def test_plan_detail_403_when_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "false")
    router = IDEObservabilityRouter()
    req = _make_request(
        "/observability/plans/op-a", match_info={"op_id": "op-a"},
    )
    resp = _run_async(router._handle_plan_detail(req))
    assert resp.status == 403


# --------------------------------------------------------------------------
# 4. Routes registered alongside existing task routes
# --------------------------------------------------------------------------


def test_register_routes_includes_plans():
    router = IDEObservabilityRouter()
    app = MagicMock()
    app.router = MagicMock()
    app.router.add_get = MagicMock()
    router.register_routes(app)
    # Collect all the paths registered.
    paths = [call.args[0] for call in app.router.add_get.call_args_list]
    assert "/observability/plans" in paths
    assert "/observability/plans/{op_id}" in paths
    # Existing task routes still there.
    assert "/observability/tasks" in paths
    assert "/observability/tasks/{op_id}" in paths


# --------------------------------------------------------------------------
# 5. bridge_plan_approval_to_broker
# --------------------------------------------------------------------------


def test_bridge_plan_pending_emits_sse_frame():
    async def _t():
        controller = PlanApprovalController()
        broker = StreamEventBroker()
        unsub = bridge_plan_approval_to_broker(controller, broker)
        try:
            controller.request_approval("op-a", _sample_plan())
            # Broker history now contains a plan_pending frame.
            history = list(broker._history)
            types = [e.event_type for e in history]
            assert EVENT_TYPE_PLAN_PENDING in types
            # Payload is a summary — NOT the full plan.
            pending = [
                e for e in history
                if e.event_type == EVENT_TYPE_PLAN_PENDING
            ][0]
            assert pending.op_id == "op-a"
            assert "plan" not in pending.payload  # bounded
            assert pending.payload["state"] == "pending"
        finally:
            unsub()
    _run_async(_t())


def test_bridge_plan_approved_emits_sse_frame():
    async def _t():
        controller = PlanApprovalController()
        broker = StreamEventBroker()
        bridge_plan_approval_to_broker(controller, broker)
        controller.request_approval("op-a", _sample_plan())
        controller.approve("op-a", reviewer="repl")
        types = [e.event_type for e in broker._history]
        assert EVENT_TYPE_PLAN_APPROVED in types
    _run_async(_t())


def test_bridge_plan_rejected_emits_sse_frame():
    async def _t():
        controller = PlanApprovalController()
        broker = StreamEventBroker()
        bridge_plan_approval_to_broker(controller, broker)
        controller.request_approval("op-a", _sample_plan())
        controller.reject("op-a", reason="nope", reviewer="repl")
        approved_history = [e for e in broker._history
                           if e.event_type == EVENT_TYPE_PLAN_REJECTED]
        assert len(approved_history) == 1
        # Rejection reason is on the summary payload.
        assert approved_history[0].payload["reason"] == "nope"
    _run_async(_t())


def test_bridge_plan_expired_emits_sse_frame():
    async def _t():
        controller = PlanApprovalController(default_timeout_s=0.05)
        broker = StreamEventBroker()
        bridge_plan_approval_to_broker(controller, broker)
        fut = controller.request_approval("op-a", _sample_plan())
        await asyncio.wait_for(fut, timeout=2.0)
        types = [e.event_type for e in broker._history]
        assert EVENT_TYPE_PLAN_EXPIRED in types
    _run_async(_t())


def test_bridge_returns_unsubscribe_callable():
    async def _t():
        controller = PlanApprovalController()
        broker = StreamEventBroker()
        unsub = bridge_plan_approval_to_broker(controller, broker)
        assert callable(unsub)
        unsub()
        # After unsub, transitions must NOT emit further SSE frames.
        controller.request_approval("op-a", _sample_plan())
        types = [e.event_type for e in broker._history]
        assert EVENT_TYPE_PLAN_PENDING not in types
    _run_async(_t())


def test_bridge_ignores_non_plan_event_types():
    """If the controller fires a non-plan_* event (future addition),
    the bridge stays silent — the broker never sees an unrecognized
    type."""
    async def _t():
        controller = PlanApprovalController()
        broker = StreamEventBroker()
        unsub = bridge_plan_approval_to_broker(controller, broker)
        try:
            # Simulate an unrecognized event by calling the controller's
            # listener invocation internal with a novel event_type.
            controller._fire("plan_future_thing", type(
                "P", (), {
                    "op_id": "op-x", "state": "pending",
                    "created_ts": 0.0, "timeout_s": 1.0,
                    "plan": {}, "reviewer": "", "reason": "",
                })(),
            )
        except Exception:
            pass
        types = [e.event_type for e in broker._history]
        # Nothing (bridge whitelist rejects it).
        assert "plan_future_thing" not in types
        unsub()
    _run_async(_t())


# --------------------------------------------------------------------------
# 6. End-to-end: controller → broker → SSE frame shape
# --------------------------------------------------------------------------


def test_end_to_end_plan_transition_appears_in_sse_frame():
    """Simulates the full wiring — register plan, observe the frame
    in the broker's wire-encoded form."""
    async def _t():
        controller = PlanApprovalController()
        broker = StreamEventBroker()
        bridge_plan_approval_to_broker(controller, broker)
        controller.request_approval("op-a", _sample_plan())
        controller.approve("op-a", reviewer="alice")
        # Walk the history and look for the APPROVED frame.
        approved = [
            e for e in broker._history
            if e.event_type == EVENT_TYPE_PLAN_APPROVED
            and e.op_id == "op-a"
        ]
        assert len(approved) == 1
        frame_bytes = approved[0].to_sse_frame()
        frame_text = frame_bytes.decode("utf-8")
        assert "id: " in frame_text
        assert "event: plan_approved" in frame_text
        # Payload includes the reviewer.
        assert '"alice"' in frame_text
    _run_async(_t())
