"""Tests for backend.core.trace_envelope — TraceEnvelope v1 schema, LamportClock, and factory.

TDD: These tests are written BEFORE the implementation.
"""

import json
import os
import threading
import time
import unittest


def _make_valid_envelope_dict() -> dict:
    """Helper to create a valid envelope dict for testing."""
    return {
        "trace_id": "abc123def456",
        "span_id": "span0001",
        "event_id": "evt0001",
        "parent_span_id": None,
        "sequence": 1,
        "boot_id": "boot-uuid-1234",
        "runtime_epoch_id": "epoch-001",
        "process_id": os.getpid(),
        "node_id": "test-host",
        "ts_wall_utc": time.time(),
        "ts_mono_local": time.monotonic(),
        "repo": "jarvis",
        "component": "test_component",
        "operation": "test_op",
        "boundary_type": "internal",
        "caused_by_event_id": None,
        "idempotency_key": None,
        "producer_version": "1.0.0",
        "schema_version": 1,
        "extra": {},
    }


class TestLamportClock(unittest.TestCase):
    """LamportClock: thread-safe logical clock."""

    def test_tick_monotonic(self):
        from backend.core.trace_envelope import LamportClock

        clock = LamportClock()
        values = [clock.tick() for _ in range(10)]
        self.assertEqual(values, list(range(1, 11)))

    def test_receive_advances_past_incoming(self):
        from backend.core.trace_envelope import LamportClock

        clock = LamportClock()
        clock.tick()  # 1
        result = clock.receive(10)
        # Should be max(1, 10) + 1 = 11
        self.assertEqual(result, 11)
        self.assertEqual(clock.current, 11)

    def test_receive_when_local_ahead(self):
        from backend.core.trace_envelope import LamportClock

        clock = LamportClock()
        for _ in range(20):
            clock.tick()
        # local is 20, incoming is 5
        result = clock.receive(5)
        # max(20, 5) + 1 = 21
        self.assertEqual(result, 21)
        self.assertEqual(clock.current, 21)

    def test_thread_safety(self):
        """4 threads x 1000 ticks, all values unique, max = 4000."""
        from backend.core.trace_envelope import LamportClock

        clock = LamportClock()
        results = []
        lock = threading.Lock()

        def tick_many():
            local_results = []
            for _ in range(1000):
                local_results.append(clock.tick())
            with lock:
                results.extend(local_results)

        threads = [threading.Thread(target=tick_many) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 4000)
        self.assertEqual(len(set(results)), 4000, "All tick values must be unique")
        self.assertEqual(max(results), 4000)

    def test_current_property(self):
        from backend.core.trace_envelope import LamportClock

        clock = LamportClock()
        self.assertEqual(clock.current, 0)
        clock.tick()
        self.assertEqual(clock.current, 1)


class TestBoundaryType(unittest.TestCase):
    """BoundaryType enum values."""

    def test_all_types_are_strings(self):
        from backend.core.trace_envelope import BoundaryType

        for bt in BoundaryType:
            self.assertIsInstance(bt.value, str)

    def test_expected_types_exist(self):
        from backend.core.trace_envelope import BoundaryType

        expected = {"http", "ipc", "file_rpc", "event_bus", "subprocess", "internal"}
        actual = {bt.value for bt in BoundaryType}
        self.assertEqual(actual, expected)


class TestTraceEnvelope(unittest.TestCase):
    """TraceEnvelope frozen dataclass."""

    def test_frozen_immutable(self):
        from backend.core.trace_envelope import TraceEnvelope

        d = _make_valid_envelope_dict()
        env = TraceEnvelope.from_dict(d)
        with self.assertRaises(AttributeError):
            env.trace_id = "new_value"  # type: ignore[misc]

    def test_serialization_round_trip_dict(self):
        from backend.core.trace_envelope import TraceEnvelope

        d = _make_valid_envelope_dict()
        env = TraceEnvelope.from_dict(d)
        result = env.to_dict()
        env2 = TraceEnvelope.from_dict(result)
        self.assertEqual(env, env2)

    def test_json_round_trip(self):
        from backend.core.trace_envelope import TraceEnvelope

        d = _make_valid_envelope_dict()
        env = TraceEnvelope.from_dict(d)
        json_str = env.to_json()
        env2 = TraceEnvelope.from_json(json_str)
        self.assertEqual(env, env2)

    def test_header_round_trip(self):
        from backend.core.trace_envelope import TraceEnvelope

        d = _make_valid_envelope_dict()
        env = TraceEnvelope.from_dict(d)
        headers = env.to_headers()

        # All header keys should start with X-Trace-
        for key in headers:
            self.assertTrue(key.startswith("X-Trace-"), f"Header {key} missing X-Trace- prefix")

        env2 = TraceEnvelope.from_headers(headers)
        self.assertIsNotNone(env2)
        self.assertEqual(env.trace_id, env2.trace_id)
        self.assertEqual(env.span_id, env2.span_id)
        self.assertEqual(env.event_id, env2.event_id)
        self.assertEqual(env.sequence, env2.sequence)
        self.assertEqual(env.repo, env2.repo)
        self.assertEqual(env.component, env2.component)
        self.assertEqual(env.operation, env2.operation)
        self.assertEqual(env.boundary_type, env2.boundary_type)
        self.assertEqual(env.schema_version, env2.schema_version)

    def test_from_headers_returns_none_when_missing_trace_id(self):
        from backend.core.trace_envelope import TraceEnvelope

        headers = {"X-Trace-Span-ID": "abc"}
        result = TraceEnvelope.from_headers(headers)
        self.assertIsNone(result)

    def test_child_inherits_trace_id(self):
        from backend.core.trace_envelope import BoundaryType, TraceEnvelopeFactory

        factory = TraceEnvelopeFactory(
            repo="jarvis",
            boot_id="boot-1",
            runtime_epoch_id="epoch-1",
            node_id="host-1",
            producer_version="1.0.0",
        )
        root = factory.create_root("comp_a", "op_a")
        child = factory.create_child(root, "comp_b", "op_b")

        self.assertEqual(child.trace_id, root.trace_id)
        self.assertEqual(child.parent_span_id, root.span_id)
        self.assertNotEqual(child.span_id, root.span_id)
        self.assertNotEqual(child.event_id, root.event_id)

    def test_causality_link(self):
        from backend.core.trace_envelope import TraceEnvelopeFactory

        factory = TraceEnvelopeFactory(
            repo="jarvis",
            boot_id="boot-1",
            runtime_epoch_id="epoch-1",
            node_id="host-1",
            producer_version="1.0.0",
        )
        root = factory.create_root("comp_a", "op_a")
        child = factory.create_child(
            root, "comp_b", "op_b", caused_by_event_id=root.event_id
        )
        self.assertEqual(child.caused_by_event_id, root.event_id)

    def test_extra_fields_preserved(self):
        from backend.core.trace_envelope import TraceEnvelope

        d = _make_valid_envelope_dict()
        d["extra"] = {"custom_key": "custom_value", "count": 42}
        env = TraceEnvelope.from_dict(d)
        self.assertEqual(env.extra["custom_key"], "custom_value")
        self.assertEqual(env.extra["count"], 42)

        # Round trip preserves extra
        env2 = TraceEnvelope.from_dict(env.to_dict())
        self.assertEqual(env2.extra, {"custom_key": "custom_value", "count": 42})

    def test_unknown_fields_go_to_extra(self):
        from backend.core.trace_envelope import TraceEnvelope

        d = _make_valid_envelope_dict()
        d["unknown_field_xyz"] = "surprise"
        env = TraceEnvelope.from_dict(d)
        self.assertEqual(env.extra["unknown_field_xyz"], "surprise")

    def test_from_dict_missing_required_fields(self):
        from backend.core.trace_envelope import TraceEnvelope

        d = {"trace_id": "abc", "extra": {}}  # Missing most required fields
        with self.assertRaises(ValueError) as ctx:
            TraceEnvelope.from_dict(d)
        self.assertIn("missing required fields", str(ctx.exception))

    def test_from_headers_malformed_sequence(self):
        """Malformed numeric header values default gracefully."""
        from backend.core.trace_envelope import TraceEnvelope

        d = _make_valid_envelope_dict()
        env = TraceEnvelope.from_dict(d)
        headers = env.to_headers()
        # Corrupt the sequence header
        headers["X-Trace-Sequence"] = "not-a-number"
        env2 = TraceEnvelope.from_headers(headers)
        self.assertIsNotNone(env2)
        self.assertEqual(env2.sequence, 0)  # Falls back to 0

    def test_from_headers_invalid_boundary_type(self):
        """Invalid boundary type string defaults to internal."""
        from backend.core.trace_envelope import BoundaryType, TraceEnvelope

        d = _make_valid_envelope_dict()
        env = TraceEnvelope.from_dict(d)
        headers = env.to_headers()
        headers["X-Trace-Boundary"] = "grpc_nonexistent"
        env2 = TraceEnvelope.from_headers(headers)
        self.assertIsNotNone(env2)
        self.assertEqual(env2.boundary_type, BoundaryType.internal)

    def test_env_var_size_limit(self):
        """Serialized envelope should be under 4KB for env var transport."""
        from backend.core.trace_envelope import TraceEnvelope

        d = _make_valid_envelope_dict()
        env = TraceEnvelope.from_dict(d)
        json_str = env.to_json()
        self.assertLess(len(json_str.encode("utf-8")), 4096)

    def test_create_event_from_same_span(self):
        """create_event_from produces new event_id in same span."""
        from backend.core.trace_envelope import TraceEnvelopeFactory

        factory = TraceEnvelopeFactory(
            repo="jarvis",
            boot_id="boot-1",
            runtime_epoch_id="epoch-1",
            node_id="host-1",
            producer_version="1.0.0",
        )
        root = factory.create_root("comp_a", "op_a")
        event2 = factory.create_event_from(root)

        self.assertEqual(event2.trace_id, root.trace_id)
        self.assertEqual(event2.span_id, root.span_id)
        self.assertNotEqual(event2.event_id, root.event_id)
        self.assertGreater(event2.sequence, root.sequence)

    def test_to_dict_boundary_type_is_string(self):
        from backend.core.trace_envelope import TraceEnvelope

        d = _make_valid_envelope_dict()
        env = TraceEnvelope.from_dict(d)
        result = env.to_dict()
        self.assertIsInstance(result["boundary_type"], str)
        self.assertEqual(result["boundary_type"], "internal")


class TestTraceEnvelopeValidation(unittest.TestCase):
    """validate_envelope checks."""

    def test_rejects_empty_trace_id(self):
        from backend.core.trace_envelope import TraceEnvelope, validate_envelope

        d = _make_valid_envelope_dict()
        d["trace_id"] = ""
        env = TraceEnvelope.from_dict(d)
        errors = validate_envelope(env)
        self.assertTrue(any("trace_id" in e for e in errors))

    def test_rejects_negative_sequence(self):
        from backend.core.trace_envelope import TraceEnvelope, validate_envelope

        d = _make_valid_envelope_dict()
        d["sequence"] = 0
        env = TraceEnvelope.from_dict(d)
        errors = validate_envelope(env)
        self.assertTrue(any("sequence" in e for e in errors))

    def test_rejects_unknown_repo(self):
        from backend.core.trace_envelope import TraceEnvelope, validate_envelope

        d = _make_valid_envelope_dict()
        d["repo"] = "unknown-repo"
        env = TraceEnvelope.from_dict(d)
        errors = validate_envelope(env)
        self.assertTrue(any("repo" in e for e in errors))

    def test_detects_gross_clock_skew(self):
        from backend.core.trace_envelope import TraceEnvelope, validate_envelope

        d = _make_valid_envelope_dict()
        d["ts_wall_utc"] = time.time() + 100000  # Way in the future
        env = TraceEnvelope.from_dict(d)
        errors = validate_envelope(env)
        self.assertTrue(any("ts_wall_utc" in e or "clock" in e.lower() for e in errors))

    def test_valid_envelope_passes(self):
        from backend.core.trace_envelope import TraceEnvelope, validate_envelope

        d = _make_valid_envelope_dict()
        env = TraceEnvelope.from_dict(d)
        errors = validate_envelope(env)
        self.assertEqual(errors, [])

    def test_rejects_long_trace_id(self):
        from backend.core.trace_envelope import TraceEnvelope, validate_envelope

        d = _make_valid_envelope_dict()
        d["trace_id"] = "x" * 65
        env = TraceEnvelope.from_dict(d)
        errors = validate_envelope(env)
        self.assertTrue(any("trace_id" in e for e in errors))

    def test_rejects_negative_ts_mono(self):
        from backend.core.trace_envelope import TraceEnvelope, validate_envelope

        d = _make_valid_envelope_dict()
        d["ts_mono_local"] = -1.0
        env = TraceEnvelope.from_dict(d)
        errors = validate_envelope(env)
        self.assertTrue(any("ts_mono" in e for e in errors))

    def test_accepts_zero_ts_mono_from_wire(self):
        """ts_mono_local=0.0 is a valid sentinel for cross-boundary envelopes."""
        from backend.core.trace_envelope import TraceEnvelope, validate_envelope

        d = _make_valid_envelope_dict()
        d["ts_mono_local"] = 0.0
        env = TraceEnvelope.from_dict(d)
        errors = validate_envelope(env)
        self.assertFalse(any("ts_mono" in e for e in errors))

    def test_rejects_bad_schema_version(self):
        from backend.core.trace_envelope import TraceEnvelope, validate_envelope

        d = _make_valid_envelope_dict()
        d["schema_version"] = 0
        env = TraceEnvelope.from_dict(d)
        errors = validate_envelope(env)
        self.assertTrue(any("schema" in e.lower() for e in errors))


class TestSchemaCompatibility(unittest.TestCase):
    """check_schema_compatibility behavior."""

    def test_min_version_reject(self):
        from backend.core.trace_envelope import check_schema_compatibility

        result = check_schema_compatibility(schema_version=0, boundary_critical=False)
        self.assertFalse(result.accepted)

    def test_max_version_critical_reject(self):
        from backend.core.trace_envelope import check_schema_compatibility

        result = check_schema_compatibility(schema_version=999, boundary_critical=True)
        self.assertFalse(result.accepted)

    def test_max_version_non_critical_warn(self):
        from backend.core.trace_envelope import check_schema_compatibility

        result = check_schema_compatibility(schema_version=999, boundary_critical=False)
        self.assertTrue(result.accepted)
        self.assertIsNotNone(result.warning)

    def test_current_version_accepted(self):
        from backend.core.trace_envelope import (
            TRACE_SCHEMA_VERSION,
            check_schema_compatibility,
        )

        result = check_schema_compatibility(
            schema_version=TRACE_SCHEMA_VERSION, boundary_critical=True
        )
        self.assertTrue(result.accepted)
        self.assertIsNone(result.warning)


class TestTraceEnvelopeFactory(unittest.TestCase):
    """TraceEnvelopeFactory creates correctly structured envelopes."""

    def test_create_root_has_no_parent(self):
        from backend.core.trace_envelope import TraceEnvelopeFactory

        factory = TraceEnvelopeFactory(
            repo="jarvis",
            boot_id="boot-1",
            runtime_epoch_id="epoch-1",
            node_id="host-1",
            producer_version="1.0.0",
        )
        root = factory.create_root("auth", "login")
        self.assertIsNone(root.parent_span_id)
        self.assertIsNone(root.caused_by_event_id)
        self.assertGreater(root.sequence, 0)

    def test_create_root_ids_are_16_hex_chars(self):
        from backend.core.trace_envelope import TraceEnvelopeFactory

        factory = TraceEnvelopeFactory(
            repo="jarvis",
            boot_id="boot-1",
            runtime_epoch_id="epoch-1",
            node_id="host-1",
            producer_version="1.0.0",
        )
        root = factory.create_root("auth", "login")
        self.assertEqual(len(root.trace_id), 16)
        self.assertEqual(len(root.span_id), 16)
        self.assertEqual(len(root.event_id), 16)

    def test_factory_populates_timestamps(self):
        from backend.core.trace_envelope import TraceEnvelopeFactory

        factory = TraceEnvelopeFactory(
            repo="jarvis",
            boot_id="boot-1",
            runtime_epoch_id="epoch-1",
            node_id="host-1",
            producer_version="1.0.0",
        )
        before = time.time()
        root = factory.create_root("auth", "login")
        after = time.time()

        self.assertGreaterEqual(root.ts_wall_utc, before)
        self.assertLessEqual(root.ts_wall_utc, after)
        self.assertGreater(root.ts_mono_local, 0)

    def test_factory_sets_process_and_node(self):
        from backend.core.trace_envelope import TraceEnvelopeFactory

        factory = TraceEnvelopeFactory(
            repo="jarvis",
            boot_id="boot-1",
            runtime_epoch_id="epoch-1",
            node_id="my-host",
            producer_version="2.0.0",
        )
        root = factory.create_root("auth", "login")
        self.assertEqual(root.process_id, os.getpid())
        self.assertEqual(root.node_id, "my-host")
        self.assertEqual(root.producer_version, "2.0.0")
        self.assertEqual(root.boot_id, "boot-1")
        self.assertEqual(root.runtime_epoch_id, "epoch-1")


if __name__ == "__main__":
    unittest.main()
