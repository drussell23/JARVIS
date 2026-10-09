# [Ouroboros] Modified by Ouroboros (op=op-01a121c9-) at 2026-10-09 17:52 UTC
# Reason: `backend/api/hive_envelope.py` has no corresponding test module. CREATE `tests/test_hive_envelope.py` containing focused

"""Tests for backend/api/hive_envelope.py.

This module tests the HiveTelemetryEnvelope and related functions that convert
TrinityEventBus and IDE SSE events into a universal semantic envelope format.
"""
from __future__ import annotations
import time
from typing import Any, Dict, Mapping
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from backend.api.hive_envelope import (
    HIVE_ENV_SCHEMA,
    HiveTelemetryEnvelope,
    SensorEnvelope,
    GovernanceEnvelope,
    SwarmEnvelope,
    RoutingEnvelope,
    ActorEnvelope,
    from_trinity_event,
    from_ide_sse_event,
)


# Test data fixtures
_TEST_TS = 1700000000.0
_TEST_EVENT_ID = "test-event-id"
_TEST_TOPIC = "command.test"
_TEST_PAYLOAD = {
    "narration_text": "Test command execution",
    "op_id": "test-op-id",
    "success": True,
}
_TEST_EVENT_TYPE = "task_started"


class TestHiveEnvelopeImport:
    def test_imports(self):
        """Verify all expected imports are available."""
        assert HIVE_ENV_SCHEMA == "hive.env.1"
        assert isinstance(HiveTelemetryEnvelope, type)
        assert isinstance(SensorEnvelope, type)
        assert isinstance(GovernanceEnvelope, type)
        assert isinstance(SwarmEnvelope, type)
        assert isinstance(RoutingEnvelope, type)
        assert isinstance(ActorEnvelope, type)
        assert callable(from_trinity_event)
        assert callable(from_ide_sse_event)


class TestHiveTelemetryEnvelope:
    def test_base_envelope_creation(self):
        """Test basic HiveTelemetryEnvelope creation and to_bus_payload method."""
        envelope = HiveTelemetryEnvelope(
            actor_id="test.actor",
            subsystem="governance",
            intent="test_intent",
            action_summary="Test summary",
            trace_id="test-trace-id",
            ts=_TEST_TS,
        )

        assert envelope.actor_id == "test.actor"
        assert envelope.subsystem == "governance"
        assert envelope.intent == "test_intent"
        assert envelope.action_summary == "Test summary"
        assert envelope.trace_id == "test-trace-id"
        assert envelope.ts == _TEST_TS
        assert envelope.kind == "generic"
        assert envelope.schema_version == HIVE_ENV_SCHEMA
        assert envelope.source_fabric == "unknown"
        assert envelope.detail == {}

        payload = envelope.to_bus_payload()
        assert payload["type"] == "generic"
        assert payload["narration_text"] == "Test summary"
        assert payload["source_brain"] == "test.actor"
        assert payload["narration_priority"] == "normal"

    def test_base_envelope_with_severity(self):
        """Test HiveTelemetryEnvelope with error severity."""
        envelope = HiveTelemetryEnvelope(
            actor_id="test.actor",
            subsystem="governance",
            intent="test_intent",
            action_summary="Test summary",
            trace_id="test-trace-id",
            severity="error",
            ts=_TEST_TS,
        )

        payload = envelope.to_bus_payload()
        assert payload["narration_priority"] == "high"

    def test_base_envelope_with_detail(self):
        """Test HiveTelemetryEnvelope with detail field."""
        detail_data = {"key": "value", "number": 42}
        envelope = HiveTelemetryEnvelope(
            actor_id="test.actor",
            subsystem="governance",
            intent="test_intent",
            action_summary="Test summary",
            trace_id="test-trace-id",
            detail=detail_data,
            ts=_TEST_TS,
        )

        assert envelope.detail == detail_data


class TestSensorEnvelope:
    def test_sensor_envelope_creation(self):
        """Test SensorEnvelope creation."""
        envelope = SensorEnvelope(
            actor_id="test.sensor",
            subsystem="sensor",
            intent="test_intent",
            action_summary="Test sensor event",
            trace_id="test-trace-id",
            signal_urgency="high",
            ts=_TEST_TS,
        )

        assert envelope.kind == "sensor"
        assert envelope.signal_urgency == "high"


class TestGovernanceEnvelope:
    def test_governance_envelope_creation(self):
        """Test GovernanceEnvelope creation."""
        envelope = GovernanceEnvelope(
            actor_id="test.governance",
            subsystem="governance",
            intent="test_intent",
            action_summary="Test governance event",
            trace_id="test-trace-id",
            phase="initialization",
            risk_tier="medium",
            ts=_TEST_TS,
        )

        assert envelope.kind == "governance"
        assert envelope.phase == "initialization"
        assert envelope.risk_tier == "medium"


class TestSwarmEnvelope:
    def test_swarm_envelope_creation(self):
        """Test SwarmEnvelope creation."""
        envelope = SwarmEnvelope(
            actor_id="test.swarm",
            subsystem="swarm",
            intent="test_intent",
            action_summary="Test swarm event",
            trace_id="test-trace-id",
            lifecycle="spawned",
            ts=_TEST_TS,
        )

        assert envelope.kind == "swarm"
        assert envelope.lifecycle == "spawned"


class TestRoutingEnvelope:
    def test_routing_envelope_creation(self):
        """Test RoutingEnvelope creation."""
        envelope = RoutingEnvelope(
            actor_id="test.routing",
            subsystem="routing",
            intent="test_intent",
            action_summary="Test routing event",
            trace_id="test-trace-id",
            provider="test-provider",
            ts=_TEST_TS,
        )

        assert envelope.kind == "routing"
        assert envelope.provider == "test-provider"


class TestActorEnvelope:
    def test_actor_envelope_creation(self):
        """Test ActorEnvelope creation."""
        envelope = ActorEnvelope(
            actor_id="test.actor",
            subsystem="actor",
            intent="test_intent",
            action_summary="Test actor event",
            trace_id="test-trace-id",
            coalesced_n=5,
            span_ms=100.0,
            ts=_TEST_TS,
        )

        assert envelope.kind == "actor"
        assert envelope.coalesced_n == 5
        assert envelope.span_ms == 100.0


class TestFromTrinityEvent:

    def test_from_trinity_event_with_error(self):
        """Test from_trinity_event with error condition."""
        payload = {"narration_text": "Error occurred", "success": False}
        result = from_trinity_event(topic="command.test", payload=payload)

        assert result.severity == "error"

    def test_from_trinity_event_with_failure_verb(self):
        """Test from_trinity_event with failure verb in topic."""
        payload = {"narration_text": "Command failed"}
        result = from_trinity_event(topic="command.fail", payload=payload)

        assert result.severity == "error"

    def test_from_trinity_event_with_error_verb(self):
        """Test from_trinity_event with error verb in topic."""
        payload = {"narration_text": "Command error"}
        result = from_trinity_event(topic="command.error", payload=payload)

        assert result.severity == "error"

    def test_from_trinity_event_with_sensor_subsystem(self):
        """Test from_trinity_event with sensor subsystem."""
        payload = {"narration_text": "Sensor event", "urgency": "critical"}
        result = from_trinity_event(topic="fs.test", payload=payload)

        assert isinstance(result, SensorEnvelope)
        assert result.signal_urgency == "critical"

    def test_from_trinity_event_with_governance_subsystem(self):
        """Test from_trinity_event with governance subsystem."""
        payload = {"narration_text": "Governance event", "phase": "setup", "risk_tier": "high"}
        result = from_trinity_event(topic="command.test", payload=payload)

        assert isinstance(result, GovernanceEnvelope)
        assert result.phase == "setup"
        assert result.risk_tier == "high"

    def test_from_trinity_event_with_swarm_subsystem(self):
        """Test from_trinity_event with swarm subsystem."""
        payload = {"narration_text": "Swarm event", "lifecycle": "spawned"}
        result = from_trinity_event(topic="autonomy.test", payload=payload)

        assert isinstance(result, SwarmEnvelope)
        assert result.lifecycle == "spawned"

    def test_from_trinity_event_with_routing_subsystem(self):
        """Test from_trinity_event with routing subsystem."""
        payload = {"narration_text": "Routing event", "provider": "test-provider"}
        result = from_trinity_event(topic="tier.test", payload=payload)

        assert isinstance(result, RoutingEnvelope)
        assert result.provider == "test-provider"



class TestFromIdeSseEvent:
    def test_from_ide_sse_event_basic(self):
        """Test basic from_ide_sse_event functionality."""
        result = from_ide_sse_event(
            event_type=_TEST_EVENT_TYPE,
            op_id="test-op-id",
            payload=_TEST_PAYLOAD,
            event_id=_TEST_EVENT_ID,
            ts=_TEST_TS,
        )

        assert isinstance(result, HiveTelemetryEnvelope)
        assert result.actor_id == "ov.governance"
        assert result.subsystem == "governance"
        assert result.intent == _TEST_EVENT_TYPE
        assert result.action_summary == "Test command execution"
        assert result.trace_id == "test-op-id"
        assert result.source_fabric == "ide_sse"
        assert result.event_id == _TEST_EVENT_ID
        assert result.ts == _TEST_TS
        assert result.severity == "info"
        assert result.detail == _TEST_PAYLOAD

    def test_from_ide_sse_event_with_error(self):
        """Test from_ide_sse_event with error condition."""
        payload = {"narration_text": "Error occurred", "success": False}
        result = from_ide_sse_event(event_type="task_failed", payload=payload)

        assert result.severity == "error"

    def test_from_ide_sse_event_with_failure_verb(self):
        """Test from_ide_sse_event with failure verb in event type."""
        payload = {"narration_text": "Task failed"}
        result = from_ide_sse_event(event_type="task_failed", payload=payload)

        assert result.severity == "error"

    def test_from_ide_sse_event_with_error_verb(self):
        """Test from_ide_sse_event with error verb in event type."""
        payload = {"narration_text": "Task error"}
        result = from_ide_sse_event(event_type="task_error", payload=payload)

        assert result.severity == "error"

    def test_from_ide_sse_event_with_deadlock_verb(self):
        """Test from_ide_sse_event with deadlock verb in event type."""
        payload = {"narration_text": "Deadlock detected"}
        result = from_ide_sse_event(event_type="deadlock_detected", payload=payload)

        assert result.severity == "error"

    def test_from_ide_sse_event_with_sensor_subsystem(self):
        """Test from_ide_sse_event with sensor subsystem."""
        result = from_ide_sse_event(event_type="drift_detected", payload={})

        assert isinstance(result, SensorEnvelope)

    def test_from_ide_sse_event_with_governance_subsystem(self):
        """Test from_ide_sse_event with governance subsystem."""
        payload = {"phase": "setup", "risk_tier": "high"}
        result = from_ide_sse_event(event_type="gate_opened", payload=payload)

        assert isinstance(result, GovernanceEnvelope)
        assert result.phase == "setup"
        assert result.risk_tier == "high"

    def test_from_ide_sse_event_with_swarm_subsystem(self):
        """Test from_ide_sse_event with swarm subsystem."""
        payload = {"narration_text": "Swarm event"}
        result = from_ide_sse_event(event_type="swarm_spawned", payload=payload)

        assert isinstance(result, SwarmEnvelope)
        assert result.lifecycle == "spawned"

    def test_from_ide_sse_event_with_routing_subsystem(self):
        """Test from_ide_sse_event with routing subsystem."""
        payload = {"provider": "test-provider"}
        result = from_ide_sse_event(event_type="provider_connected", payload=payload)

        assert isinstance(result, RoutingEnvelope)
        assert result.provider == "test-provider"

