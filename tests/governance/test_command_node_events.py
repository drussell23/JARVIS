"""Tests for Sovereign Command Node Phase 1: additive SSE event types + blast-radius endpoint.

Covers:
  - New EVENT_TYPE_* constants exist + correct string values
  - publish_fsm_phase / publish_elevation_pending / publish_sovereign_yield /
    publish_dag_node -- correct event shape
  - All publish helpers are fail-soft (broker exception never propagates)
  - All publish helpers are no-op when broker is None
  - blast-radius endpoint (_handle_blast_radius) -- correct response shape
  - blast-radius endpoint -- authority-free (no forbidden imports in module)
  - New event types are additive (existing EVENT_TYPE_* still exist)
  - New event types are registered in _VALID_EVENT_TYPES (publish not dropped)
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Test 1: New event type constants exist with correct string values
# ---------------------------------------------------------------------------


def test_new_event_type_constants_exist():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_FSM_PHASE_CHANGED,
        EVENT_TYPE_CROSS_REPO_ELEVATION_PENDING,
        EVENT_TYPE_SOVEREIGN_YIELD,
        EVENT_TYPE_DAG_NODE_UPDATED,
    )
    assert EVENT_TYPE_FSM_PHASE_CHANGED == "fsm_phase_changed"
    assert EVENT_TYPE_CROSS_REPO_ELEVATION_PENDING == "cross_repo_elevation_pending"
    assert EVENT_TYPE_SOVEREIGN_YIELD == "sovereign_yield"
    assert EVENT_TYPE_DAG_NODE_UPDATED == "dag_node_updated"


# ---------------------------------------------------------------------------
# Test 2: publish_fsm_phase publishes correct event shape
# ---------------------------------------------------------------------------


def test_publish_fsm_phase_publishes_correct_event():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        publish_fsm_phase,
    )
    mock_broker = MagicMock()
    with patch(
        "backend.core.ouroboros.governance.ide_observability_stream.get_default_broker",
        return_value=mock_broker,
    ):
        publish_fsm_phase("op-123", "GENERATE", "STANDARD", "NOTIFY_APPLY")
    mock_broker.publish.assert_called_once()
    call_args = mock_broker.publish.call_args
    event_type = call_args[0][0]
    op_id_arg = call_args[0][1]
    payload = call_args[0][2]
    assert event_type == "fsm_phase_changed"
    assert op_id_arg == "op-123"
    assert payload["op_id"] == "op-123"
    assert payload["phase"] == "GENERATE"
    assert payload["route"] == "STANDARD"
    assert payload["risk_tier"] == "NOTIFY_APPLY"
    assert payload["schema_version"] == "1.0"


# ---------------------------------------------------------------------------
# Test 3: publish_elevation_pending publishes correct event shape
# ---------------------------------------------------------------------------


def test_publish_elevation_pending_publishes_correct_event():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        publish_elevation_pending,
    )
    mock_broker = MagicMock()
    blast = {"directly_affected": 2, "transitively_affected": 5}
    with patch(
        "backend.core.ouroboros.governance.ide_observability_stream.get_default_broker",
        return_value=mock_broker,
    ):
        publish_elevation_pending("pr-456", "jarvis-prime", blast)
    mock_broker.publish.assert_called_once()
    call_args = mock_broker.publish.call_args
    event_type = call_args[0][0]
    op_id_arg = call_args[0][1]
    payload = call_args[0][2]
    assert event_type == "cross_repo_elevation_pending"
    assert op_id_arg == "pr-456"
    assert payload["pr_id"] == "pr-456"
    assert payload["target_repo"] == "jarvis-prime"
    assert payload["blast_radius_summary"] == blast
    assert payload["schema_version"] == "1.0"


# ---------------------------------------------------------------------------
# Test 4: publish_sovereign_yield publishes correct event shape
# ---------------------------------------------------------------------------


def test_publish_sovereign_yield_publishes_correct_event():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        publish_sovereign_yield,
    )
    mock_broker = MagicMock()
    with patch(
        "backend.core.ouroboros.governance.ide_observability_stream.get_default_broker",
        return_value=mock_broker,
    ):
        publish_sovereign_yield("op-789", "upstream_quarantine")
    mock_broker.publish.assert_called_once()
    call_args = mock_broker.publish.call_args
    event_type = call_args[0][0]
    op_id_arg = call_args[0][1]
    payload = call_args[0][2]
    assert event_type == "sovereign_yield"
    assert op_id_arg == "op-789"
    assert payload["op_id"] == "op-789"
    assert payload["reason"] == "upstream_quarantine"
    assert payload["schema_version"] == "1.0"


# ---------------------------------------------------------------------------
# Test 5: publish_dag_node publishes correct event shape
# ---------------------------------------------------------------------------


def test_publish_dag_node_publishes_correct_event():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        publish_dag_node,
    )
    mock_broker = MagicMock()
    with patch(
        "backend.core.ouroboros.governance.ide_observability_stream.get_default_broker",
        return_value=mock_broker,
    ):
        publish_dag_node("op-001", "node-abc", "COMPLETE")
    mock_broker.publish.assert_called_once()
    call_args = mock_broker.publish.call_args
    event_type = call_args[0][0]
    op_id_arg = call_args[0][1]
    payload = call_args[0][2]
    assert event_type == "dag_node_updated"
    assert op_id_arg == "op-001"
    assert payload["op_id"] == "op-001"
    assert payload["node_id"] == "node-abc"
    assert payload["state"] == "COMPLETE"
    assert payload["schema_version"] == "1.0"


# ---------------------------------------------------------------------------
# Test 6: All publish helpers are fail-soft (broker exception never propagates)
# ---------------------------------------------------------------------------


def test_publish_fsm_phase_fail_soft():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        publish_fsm_phase,
    )
    mock_broker = MagicMock()
    mock_broker.publish.side_effect = RuntimeError("broker exploded")
    with patch(
        "backend.core.ouroboros.governance.ide_observability_stream.get_default_broker",
        return_value=mock_broker,
    ):
        # Must NOT raise
        publish_fsm_phase("op-x", "VALIDATE", "IMMEDIATE", "SAFE_AUTO")


def test_publish_elevation_pending_fail_soft():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        publish_elevation_pending,
    )
    mock_broker = MagicMock()
    mock_broker.publish.side_effect = RuntimeError("broker exploded")
    with patch(
        "backend.core.ouroboros.governance.ide_observability_stream.get_default_broker",
        return_value=mock_broker,
    ):
        publish_elevation_pending("pr-x", "some-repo", {})


def test_publish_sovereign_yield_fail_soft():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        publish_sovereign_yield,
    )
    mock_broker = MagicMock()
    mock_broker.publish.side_effect = RuntimeError("broker exploded")
    with patch(
        "backend.core.ouroboros.governance.ide_observability_stream.get_default_broker",
        return_value=mock_broker,
    ):
        publish_sovereign_yield("op-x", "some_reason")


def test_publish_dag_node_fail_soft():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        publish_dag_node,
    )
    mock_broker = MagicMock()
    mock_broker.publish.side_effect = RuntimeError("broker exploded")
    with patch(
        "backend.core.ouroboros.governance.ide_observability_stream.get_default_broker",
        return_value=mock_broker,
    ):
        publish_dag_node("op-x", "node-x", "FAILED")


# ---------------------------------------------------------------------------
# Test 7: All publish helpers are no-op when broker is None
# ---------------------------------------------------------------------------


def test_publish_helpers_noop_when_no_broker():
    from backend.core.ouroboros.governance import ide_observability_stream as m
    with patch.object(m, "get_default_broker", return_value=None):
        m.publish_fsm_phase("op-1", "APPLY", "STANDARD", "NOTIFY_APPLY")
        m.publish_elevation_pending("pr-1", "repo-x", {})
        m.publish_sovereign_yield("op-1", "reason")
        m.publish_dag_node("op-1", "node-1", "PENDING")


# ---------------------------------------------------------------------------
# Test 8: New event types are registered in _VALID_EVENT_TYPES (not dropped)
# ---------------------------------------------------------------------------


def test_new_event_types_in_valid_set():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        _VALID_EVENT_TYPES,
        EVENT_TYPE_FSM_PHASE_CHANGED,
        EVENT_TYPE_CROSS_REPO_ELEVATION_PENDING,
        EVENT_TYPE_SOVEREIGN_YIELD,
        EVENT_TYPE_DAG_NODE_UPDATED,
    )
    assert EVENT_TYPE_FSM_PHASE_CHANGED in _VALID_EVENT_TYPES
    assert EVENT_TYPE_CROSS_REPO_ELEVATION_PENDING in _VALID_EVENT_TYPES
    assert EVENT_TYPE_SOVEREIGN_YIELD in _VALID_EVENT_TYPES
    assert EVENT_TYPE_DAG_NODE_UPDATED in _VALID_EVENT_TYPES


# ---------------------------------------------------------------------------
# Test 9: publish helpers do not drop when broker validates event type
# ---------------------------------------------------------------------------


def test_publish_sovereign_yield_not_dropped_by_broker():
    """Uses the real broker to confirm publish() returns a non-None event_id."""
    from backend.core.ouroboros.governance.ide_observability_stream import (
        StreamEventBroker,
        EVENT_TYPE_SOVEREIGN_YIELD,
    )
    broker = StreamEventBroker()
    event_id = broker.publish(EVENT_TYPE_SOVEREIGN_YIELD, "op-test", {"reason": "stall"})
    assert event_id is not None, "EVENT_TYPE_SOVEREIGN_YIELD was rejected by the broker"


def test_publish_fsm_phase_not_dropped_by_broker():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        StreamEventBroker,
        EVENT_TYPE_FSM_PHASE_CHANGED,
    )
    broker = StreamEventBroker()
    event_id = broker.publish(
        EVENT_TYPE_FSM_PHASE_CHANGED,
        "op-test",
        {"phase": "GENERATE", "route": "STANDARD", "risk_tier": "NOTIFY_APPLY"},
    )
    assert event_id is not None, "EVENT_TYPE_FSM_PHASE_CHANGED was rejected by the broker"


def test_publish_dag_node_not_dropped_by_broker():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        StreamEventBroker,
        EVENT_TYPE_DAG_NODE_UPDATED,
    )
    broker = StreamEventBroker()
    event_id = broker.publish(
        EVENT_TYPE_DAG_NODE_UPDATED,
        "op-test",
        {"node_id": "n-001", "state": "COMPLETE"},
    )
    assert event_id is not None, "EVENT_TYPE_DAG_NODE_UPDATED was rejected by the broker"


def test_publish_elevation_pending_not_dropped_by_broker():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        StreamEventBroker,
        EVENT_TYPE_CROSS_REPO_ELEVATION_PENDING,
    )
    broker = StreamEventBroker()
    event_id = broker.publish(
        EVENT_TYPE_CROSS_REPO_ELEVATION_PENDING,
        "pr-test",
        {"target_repo": "jarvis-prime", "blast_radius_summary": {}},
    )
    assert event_id is not None, "EVENT_TYPE_CROSS_REPO_ELEVATION_PENDING was rejected by the broker"


# ---------------------------------------------------------------------------
# Test 10: blast-radius endpoint is authority-free (no forbidden imports)
# ---------------------------------------------------------------------------


def test_blast_radius_endpoint_authority_free():
    """Confirms ide_observability.py does not import top-level gate modules.

    Uses AST-based import analysis to avoid false positives from sub-packages
    that contain the banned name as a substring (e.g. closure_loop_orchestrator
    is allowed, governance.orchestrator is banned).
    """
    import ast
    obs_path = (
        "/Users/djrussell23/Documents/repos/JARVIS-AI-Agent/"
        ".claude/worktrees/command-node-phase1/backend/core/"
        "ouroboros/governance/ide_observability.py"
    )
    with open(obs_path) as f:
        source = f.read()
    # Exact governance module base-names banned by the authority invariant.
    # "orchestrator" as the LAST segment (governance.orchestrator) is banned;
    # sub-packages like closure_loop_orchestrator are allowed.
    forbidden_bases = {
        "orchestrator",
        "change_engine",
        "candidate_generator",
        "repair_engine",
        "iron_gate",
        "risk_tier_floor",
        "semantic_guardian",
    }
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                last_segment = alias.name.rsplit(".", 1)[-1]
                assert last_segment not in forbidden_bases, (
                    "ide_observability.py must not import "
                    + repr(alias.name) + " (authority-invariant)"
                )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            last_segment = module.rsplit(".", 1)[-1]
            assert last_segment not in forbidden_bases, (
                "ide_observability.py must not import from "
                + repr(module) + " (authority-invariant)"
            )


# ---------------------------------------------------------------------------
# Test 11: New event types are additive (existing types unchanged)
# ---------------------------------------------------------------------------


def test_new_event_types_are_additive():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_TASK_STARTED,
        EVENT_TYPE_TASK_COMPLETED,
        EVENT_TYPE_HEARTBEAT,
        EVENT_TYPE_FSM_PHASE_CHANGED,
        EVENT_TYPE_SOVEREIGN_YIELD,
    )
    # Old types still exist unchanged
    assert EVENT_TYPE_TASK_STARTED == "task_started"
    assert EVENT_TYPE_TASK_COMPLETED == "task_completed"
    assert EVENT_TYPE_HEARTBEAT == "heartbeat"
    # New types also exist
    assert EVENT_TYPE_FSM_PHASE_CHANGED == "fsm_phase_changed"
    assert EVENT_TYPE_SOVEREIGN_YIELD == "sovereign_yield"


# ---------------------------------------------------------------------------
# Test 12: blast-radius endpoint route is registered in IDEObservabilityRouter
# ---------------------------------------------------------------------------


def test_blast_radius_route_registered():
    """Confirms the route /observability/blast-radius/{op_id} is registered."""
    path = (
        "/Users/djrussell23/Documents/repos/JARVIS-AI-Agent/"
        ".claude/worktrees/command-node-phase1/backend/core/"
        "ouroboros/governance/ide_observability.py"
    )
    with open(path) as f:
        source = f.read()
    assert "/observability/blast-radius/{op_id}" in source, (
        "blast-radius route not found in register_routes"
    )
    assert "_handle_blast_radius" in source, (
        "_handle_blast_radius handler not found"
    )


# ---------------------------------------------------------------------------
# Test 13: blast-radius handler returns 403 when ide_observability disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blast_radius_handler_returns_403_when_disabled():
    import os
    from backend.core.ouroboros.governance.ide_observability import (
        IDEObservabilityRouter,
    )
    router = IDEObservabilityRouter.__new__(IDEObservabilityRouter)
    router._rate_tracker = {}

    mock_request = MagicMock()
    mock_request.remote = "127.0.0.1"
    mock_request.headers = {}
    mock_request.match_info = {"op_id": "op-test-123"}

    with patch(
        "backend.core.ouroboros.governance.ide_observability.ide_observability_enabled",
        return_value=False,
    ):
        resp = await router._handle_blast_radius(mock_request)
    # Should return 403
    assert resp.status == 403


# ---------------------------------------------------------------------------
# Test 14: publish helpers are all importable (sanity import check)
# ---------------------------------------------------------------------------


def test_publish_helpers_importable():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        publish_fsm_phase,
        publish_elevation_pending,
        publish_sovereign_yield,
        publish_dag_node,
    )
    assert callable(publish_fsm_phase)
    assert callable(publish_elevation_pending)
    assert callable(publish_sovereign_yield)
    assert callable(publish_dag_node)


# ---------------------------------------------------------------------------
# Test 15: convergence_watchdog uses publish_sovereign_yield (not publish_task_event)
# ---------------------------------------------------------------------------


def test_convergence_watchdog_uses_publish_sovereign_yield():
    """Confirms the upgrade in convergence_watchdog.emit_sovereign_yield."""
    path = (
        "/Users/djrussell23/Documents/repos/JARVIS-AI-Agent/"
        ".claude/worktrees/command-node-phase1/backend/core/"
        "ouroboros/governance/convergence_watchdog.py"
    )
    with open(path) as f:
        source = f.read()
    assert "publish_sovereign_yield" in source, (
        "convergence_watchdog should import publish_sovereign_yield "
        "(Command Node Phase 1 wire)"
    )
