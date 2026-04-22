"""Slice 3 regression spine — /governor REPL + GET + SSE."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from backend.core.ouroboros.governance.governor_repl import (
    GovernorDispatchResult,
    dispatch_governor_command,
)
from backend.core.ouroboros.governance.memory_pressure_gate import (
    MemoryPressureGate, MemoryProbe, PressureLevel,
    reset_default_gate,
)
from backend.core.ouroboros.governance.sensor_governor import (
    SensorGovernor, Urgency,
    ensure_seeded as _gov_ensure_seeded,
    reset_default_governor,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in list(os.environ):
        if (k.startswith("JARVIS_SENSOR_GOVERNOR")
                or k.startswith("JARVIS_MEMORY_PRESSURE")
                or k.startswith("JARVIS_IDE_")
                or k.startswith("JARVIS_FLAG_REGISTRY")):
            monkeypatch.delenv(k, raising=False)
    reset_default_governor()
    reset_default_gate()
    yield
    reset_default_governor()
    reset_default_gate()


@pytest.fixture
def gov(monkeypatch) -> SensorGovernor:
    monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
    reset_default_governor()
    return _gov_ensure_seeded()


@pytest.fixture
def gate(monkeypatch) -> MemoryPressureGate:
    monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")

    def _fake_probe():
        return MemoryProbe(
            free_pct=50.0, total_bytes=16 * (1024 ** 3),
            available_bytes=8 * (1024 ** 3), source="test",
        )
    return MemoryPressureGate(probe_fn=_fake_probe)


def _make_request(query=None, match_info=None):
    return SimpleNamespace(
        remote="127.0.0.1",
        headers={"Origin": "http://localhost:1234"},
        query=query or {},
        match_info=match_info or {},
    )


# ---------------------------------------------------------------------------
# /governor REPL
# ---------------------------------------------------------------------------


class TestREPLBasics:

    def test_unknown_line_unmatched(self):
        r = dispatch_governor_command("/notgov")
        assert r.matched is False

    def test_empty_line_unmatched(self):
        r = dispatch_governor_command("")
        assert r.matched is False

    def test_help_always_works(self):
        r = dispatch_governor_command("/governor help")
        assert r.ok
        assert "/governor" in r.text

    def test_master_off_rejects_operational(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "false")
        r = dispatch_governor_command("/governor status")
        assert r.ok is False
        assert "SensorGovernor disabled" in r.text

    def test_parse_error(self):
        r = dispatch_governor_command('/governor status "unterminated')
        assert r.ok is False
        assert "parse error" in r.text

    def test_unknown_subcommand(self, gov):
        r = dispatch_governor_command("/governor frobnicate", governor=gov)
        assert r.ok is False


class TestStatus:

    def test_status_bare_alias(self, gov):
        r = dispatch_governor_command("/governor", governor=gov)
        assert r.ok
        assert "Sensors" in r.text or "Global" in r.text

    def test_status_lists_16_sensors(self, gov):
        r = dispatch_governor_command("/governor status", governor=gov)
        assert r.ok
        assert "TestFailureSensor" in r.text
        assert "OpportunityMinerSensor" in r.text

    def test_status_shows_global_counters(self, gov):
        r = dispatch_governor_command("/governor status", governor=gov)
        assert "Global:" in r.text


class TestExplain:

    def test_explain_has_thresholds(self, gov):
        r = dispatch_governor_command("/governor explain", governor=gov)
        assert r.ok
        assert "brake thresholds" in r.text or "cost_burn" in r.text

    def test_explain_per_sensor_detail(self, gov):
        r = dispatch_governor_command("/governor explain", governor=gov)
        assert "base=" in r.text
        assert "posture_weight=" in r.text


class TestHistory:

    def test_history_empty(self, gov):
        r = dispatch_governor_command("/governor history", governor=gov)
        assert r.ok
        assert "no recent" in r.text.lower() or "Last" in r.text

    def test_history_populated(self, gov):
        gov.request_budget("TestFailureSensor")
        gov.request_budget("OpportunityMinerSensor")
        r = dispatch_governor_command("/governor history", governor=gov)
        assert "TestFailureSensor" in r.text

    def test_history_invalid_n(self, gov):
        r = dispatch_governor_command("/governor history abc", governor=gov)
        assert r.ok is False


class TestReset:

    def test_reset_clears_counters(self, gov):
        gov.record_emission("TestFailureSensor")
        gov.record_emission("TestFailureSensor")
        r = dispatch_governor_command("/governor reset", governor=gov)
        assert r.ok
        d = gov.request_budget("TestFailureSensor")
        assert d.current_count == 0


class TestMemorySubcommand:

    def test_memory_off_rejects(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "false")
        r = dispatch_governor_command("/governor memory")
        assert r.ok is False
        assert "MemoryPressureGate" in r.text

    def test_memory_on_shows_level(self, gate, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        r = dispatch_governor_command("/governor memory", gate=gate)
        assert r.ok
        assert "Memory pressure" in r.text
        assert "Fanout projection" in r.text

    def test_memory_shows_fanout_projection(self, gate, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        r = dispatch_governor_command("/governor memory", gate=gate)
        # n=1, 3, 8, 16 projection lines
        assert "n= 1" in r.text
        assert "n=16" in r.text


# ---------------------------------------------------------------------------
# GET endpoints
# ---------------------------------------------------------------------------


class TestGovernorGET:

    @pytest.mark.asyncio
    async def test_403_when_governor_off(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "false")
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_governor_snapshot(_make_request())
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_200_with_enabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        reset_default_governor()
        _gov_ensure_seeded()
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_governor_snapshot(_make_request())
        assert resp.status == 200
        payload = json.loads(resp.body)
        assert payload["enabled"] is True
        assert len(payload["sensors"]) == 16

    @pytest.mark.asyncio
    async def test_history_limit(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        reset_default_governor()
        g = _gov_ensure_seeded()
        for _ in range(5):
            g.request_budget("TestFailureSensor")
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_governor_history(
            _make_request(query={"limit": "3"}),
        )
        payload = json.loads(resp.body)
        assert payload["count"] <= 3

    @pytest.mark.asyncio
    async def test_history_malformed_limit(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_governor_history(
            _make_request(query={"limit": "xyz"}),
        )
        assert resp.status == 400


class TestMemoryPressureGET:

    @pytest.mark.asyncio
    async def test_403_when_gate_off(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "false")
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_memory_pressure(_make_request())
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_200_with_probe_shape(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_memory_pressure(_make_request())
        assert resp.status == 200
        payload = json.loads(resp.body)
        assert "probe" in payload
        assert "level" in payload


# ---------------------------------------------------------------------------
# SSE events + bridges
# ---------------------------------------------------------------------------


class TestSSEEvents:

    def test_event_types_in_whitelist(self):
        from backend.core.ouroboros.governance.ide_observability_stream import (
            EVENT_TYPE_GOVERNOR_THROTTLE_APPLIED,
            EVENT_TYPE_GOVERNOR_EMERGENCY_BRAKE,
            EVENT_TYPE_MEMORY_PRESSURE_CHANGED,
            _VALID_EVENT_TYPES,
        )
        assert EVENT_TYPE_GOVERNOR_THROTTLE_APPLIED in _VALID_EVENT_TYPES
        assert EVENT_TYPE_GOVERNOR_EMERGENCY_BRAKE in _VALID_EVENT_TYPES
        assert EVENT_TYPE_MEMORY_PRESSURE_CHANGED in _VALID_EVENT_TYPES

    def test_publish_throttle_disabled_returns_none(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "false")
        from backend.core.ouroboros.governance.ide_observability_stream import (
            publish_governor_throttle_event, reset_default_broker,
        )
        reset_default_broker()

        # Fake decision
        from backend.core.ouroboros.governance.sensor_governor import (
            BudgetDecision,
        )
        d = BudgetDecision(
            allowed=False, sensor_name="X", urgency=Urgency.STANDARD,
            posture=None, weighted_cap=10, current_count=10, remaining=0,
            reason_code="governor.sensor_cap_exhausted",
        )
        assert publish_governor_throttle_event(d) is None

    def test_publish_throttle_enabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
        from backend.core.ouroboros.governance.ide_observability_stream import (
            publish_governor_throttle_event,
            get_default_broker, reset_default_broker,
        )
        reset_default_broker()
        broker = get_default_broker()

        from backend.core.ouroboros.governance.sensor_governor import (
            BudgetDecision,
        )
        d = BudgetDecision(
            allowed=False, sensor_name="TestFailure",
            urgency=Urgency.STANDARD, posture="HARDEN",
            weighted_cap=36, current_count=36, remaining=0,
            reason_code="governor.sensor_cap_exhausted",
        )
        before = broker.published_count
        eid = publish_governor_throttle_event(d)
        assert eid is not None
        assert broker.published_count == before + 1

    def test_publish_emergency_brake(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
        from backend.core.ouroboros.governance.ide_observability_stream import (
            publish_governor_emergency_brake_event,
            get_default_broker, reset_default_broker,
        )
        reset_default_broker()
        broker = get_default_broker()
        eid = publish_governor_emergency_brake_event(True, 0.95, 0.2)
        assert eid is not None

    def test_publish_memory_pressure(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
        from backend.core.ouroboros.governance.ide_observability_stream import (
            publish_memory_pressure_event,
            get_default_broker, reset_default_broker,
        )
        reset_default_broker()
        broker = get_default_broker()
        eid = publish_memory_pressure_event("ok", "warn", 25.0, "psutil")
        assert eid is not None

    def test_bridge_governor_throttle_fires_on_deny(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        from backend.core.ouroboros.governance.ide_observability_stream import (
            bridge_governor_to_broker,
            get_default_broker, reset_default_broker,
            EVENT_TYPE_GOVERNOR_THROTTLE_APPLIED,
        )
        reset_default_broker()
        broker = get_default_broker()

        gov = SensorGovernor(
            posture_fn=lambda: None, signal_bundle_fn=lambda: None,
        )
        from backend.core.ouroboros.governance.sensor_governor import (
            SensorBudgetSpec,
        )
        gov.register(SensorBudgetSpec(
            sensor_name="X", base_cap_per_hour=1,
        ))
        bridge_governor_to_broker(governor=gov)
        # First request allowed, then saturate
        gov.record_emission("X")
        before = broker.published_count
        # Now at cap → next request denies → publishes
        gov.request_budget("X")
        # Check throttle event in history
        types = [e.event_type for e in broker._history]
        assert EVENT_TYPE_GOVERNOR_THROTTLE_APPLIED in types

    def test_bridge_memory_pressure_publishes_on_transition(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
        monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
        from backend.core.ouroboros.governance.ide_observability_stream import (
            bridge_memory_pressure_to_broker,
            get_default_broker, reset_default_broker,
            EVENT_TYPE_MEMORY_PRESSURE_CHANGED,
        )
        reset_default_broker()
        broker = get_default_broker()

        # Probe that returns varying free_pct
        states = [50.0, 15.0]  # OK then HIGH
        def _probe():
            return MemoryProbe(
                free_pct=states.pop(0) if states else 15.0,
                total_bytes=16 * (1024**3),
                available_bytes=8 * (1024**3),
                source="test",
            )
        gate = MemoryPressureGate(probe_fn=_probe)
        bridge_memory_pressure_to_broker(gate=gate)
        gate.pressure()  # OK (first call — prev=None, no publish)
        gate.pressure()  # HIGH — transition publishes
        types = [e.event_type for e in broker._history]
        assert EVENT_TYPE_MEMORY_PRESSURE_CHANGED in types


# ---------------------------------------------------------------------------
# Authority invariant
# ---------------------------------------------------------------------------


class TestAuthorityInvariant:

    def test_governor_repl_authority_free(self):
        repo_root = Path(subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip())
        src = (repo_root
               / "backend/core/ouroboros/governance/governor_repl.py"
               ).read_text(encoding="utf-8")
        forbidden = (
            "orchestrator", "policy", "iron_gate", "risk_tier",
            "change_engine", "candidate_generator",
        )
        bad = []
        for line in src.splitlines():
            if line.startswith(("from ", "import ")):
                for f in forbidden:
                    if f".{f}" in line:
                        bad.append(line)
        assert not bad, f"governor_repl.py violations: {bad}"
