# tests/unit/core/test_gcp_lifecycle_schema.py
"""Tests for GCP lifecycle canonical schema."""
import json
import pytest

from backend.core.gcp_lifecycle_schema import (
    State, Event, HealthCategory, DisconnectReason,
    validate_state, validate_event, validate_health_category,
)


class TestStateEnum:
    def test_all_states_are_str_enum(self):
        for s in State:
            assert isinstance(s, str)
            assert isinstance(s.value, str)

    def test_primary_lifecycle_states_exist(self):
        required = ["idle", "triggering", "provisioning", "booting",
                     "handshaking", "active", "cooling_down", "stopping"]
        for val in required:
            assert State(val) is not None

    def test_auxiliary_states_exist(self):
        for val in ["lost", "failed", "degraded"]:
            assert State(val) is not None

    def test_state_serializes_to_json(self):
        payload = {"state": State.ACTIVE}
        dumped = json.dumps(payload)
        assert '"active"' in dumped

    def test_state_round_trips_json(self):
        original = State.PROVISIONING
        dumped = json.dumps({"s": original})
        loaded = json.loads(dumped)
        assert State(loaded["s"]) is State.PROVISIONING


class TestEventEnum:
    def test_pressure_events_exist(self):
        assert Event.PRESSURE_TRIGGERED.value == "pressure_triggered"
        assert Event.PRESSURE_COOLED.value == "pressure_cooled"

    def test_budget_events_exist(self):
        for name in ["budget_check", "budget_approved", "budget_denied",
                      "budget_exhausted_runtime", "budget_released"]:
            assert Event(name) is not None

    def test_vm_events_exist(self):
        for name in ["provision_requested", "vm_create_accepted",
                      "vm_create_already_exists", "vm_create_failed",
                      "vm_ready", "vm_stopped", "spot_preempted"]:
            assert Event(name) is not None

    def test_health_events_exist(self):
        for name in ["health_probe_ok", "health_probe_degraded",
                      "health_probe_timeout", "health_unreachable_consecutive",
                      "handshake_started", "handshake_succeeded",
                      "handshake_failed", "boot_deadline_exceeded"]:
            assert Event(name) is not None

    def test_control_events_exist(self):
        for name in ["lease_lost", "session_shutdown",
                      "manual_force_local", "manual_force_cloud", "fatal_error"]:
            assert Event(name) is not None


class TestHealthCategory:
    def test_all_categories(self):
        expected = ["healthy", "contract_mismatch", "dependency_degraded",
                     "service_degraded", "unreachable", "timeout", "unknown"]
        for val in expected:
            assert HealthCategory(val) is not None


class TestDisconnectReason:
    def test_all_reasons(self):
        expected = ["timeout", "write_error", "eof", "protocol_error",
                     "lease_lost", "server_shutdown", "client_shutdown"]
        for val in expected:
            assert DisconnectReason(val) is not None


class TestValidation:
    def test_validate_state_accepts_valid(self):
        assert validate_state("active") == State.ACTIVE

    def test_validate_state_rejects_unknown(self):
        with pytest.raises(ValueError, match="Unknown state"):
            validate_state("running")

    def test_validate_event_accepts_valid(self):
        assert validate_event("vm_ready") == Event.VM_READY

    def test_validate_event_rejects_unknown(self):
        with pytest.raises(ValueError, match="Unknown event"):
            validate_event("vm_started")

    def test_validate_health_category_accepts_valid(self):
        assert validate_health_category("healthy") == HealthCategory.HEALTHY

    def test_validate_health_category_rejects_unknown(self):
        with pytest.raises(ValueError, match="Unknown health category"):
            validate_health_category("good")
