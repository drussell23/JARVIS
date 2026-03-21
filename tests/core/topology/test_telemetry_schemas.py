"""Tests for topology telemetry schema registration."""
from backend.core.telemetry_contract import V1_EVENT_SCHEMAS, TelemetryEnvelope
from backend.core.topology.telemetry_schemas import (
    HARDWARE_SCHEMA,
    PROACTIVE_DRIVE_SCHEMA,
    build_hardware_payload,
    build_drive_tick_payload,
)
from backend.core.topology.hardware_env import ComputeTier, HardwareEnvironmentState


class TestSchemaRegistration:
    def test_hardware_schema_in_v1_list(self):
        assert HARDWARE_SCHEMA in V1_EVENT_SCHEMAS

    def test_proactive_drive_schema_in_v1_list(self):
        assert PROACTIVE_DRIVE_SCHEMA in V1_EVENT_SCHEMAS

    def test_schema_format(self):
        assert HARDWARE_SCHEMA == "lifecycle.hardware@1.0.0"
        assert PROACTIVE_DRIVE_SCHEMA == "reasoning.proactive_drive@1.0.0"


class TestPayloadBuilders:
    def test_build_hardware_payload(self):
        hw = HardwareEnvironmentState(
            os_family="darwin", cpu_logical_cores=8, ram_total_mb=16384,
            ram_available_mb=8192, compute_tier=ComputeTier.LOCAL_CPU, gpu=None,
            hostname="test", python_version="3.11.0",
            max_parallel_inference_tasks=4, max_shadow_harness_workers=4,
        )
        payload = build_hardware_payload(hw)
        assert payload["os_family"] == "darwin"
        assert payload["compute_tier"] == "local_cpu"
        assert payload["cpu_logical_cores"] == 8
        assert payload["gpu_name"] is None

    def test_build_drive_tick_payload(self):
        payload = build_drive_tick_payload(
            state="MEASURING",
            reason="jarvis: L=0.142 < threshold=30.000",
            target_name=None,
            target_domain=None,
        )
        assert payload["state"] == "MEASURING"
        assert payload["reason"] == "jarvis: L=0.142 < threshold=30.000"
        assert payload["target_name"] is None

    def test_build_drive_tick_with_target(self):
        payload = build_drive_tick_payload(
            state="EXPLORING",
            reason="Eligible",
            target_name="parse_parquet",
            target_domain="data_io",
        )
        assert payload["target_name"] == "parse_parquet"
        assert payload["target_domain"] == "data_io"

    def test_hardware_envelope_creates(self):
        hw = HardwareEnvironmentState(
            os_family="darwin", cpu_logical_cores=8, ram_total_mb=16384,
            ram_available_mb=8192, compute_tier=ComputeTier.LOCAL_CPU, gpu=None,
            hostname="test", python_version="3.11.0",
            max_parallel_inference_tasks=4, max_shadow_harness_workers=4,
        )
        payload = build_hardware_payload(hw)
        envelope = TelemetryEnvelope.create(
            event_schema=HARDWARE_SCHEMA,
            source="proactive_drive_service",
            trace_id="test-trace",
            span_id="test-span",
            partition_key="lifecycle",
            payload=payload,
        )
        assert envelope.event_schema == HARDWARE_SCHEMA
        assert envelope.payload["compute_tier"] == "local_cpu"
