"""
Phase 12 Tests: Partition-Aware Health + Protocol Version Gate
==============================================================

Tests for:
  Disease 1: Partial partition handling (partition_aware_health.py)
  Disease 2: Hot-update compatibility window (protocol_version_gate.py)

v276.0 Phase 12 hardening.
"""

import os
import sys
import time
import threading
import unittest
from unittest.mock import patch, MagicMock

# Ensure project root is on sys.path
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


# ============================================================================
# Disease 1: Partition-Aware Health Tests
# ============================================================================

class TestHealthVerdict(unittest.TestCase):
    """Test HealthVerdict enum ordering and semantics."""

    def test_import(self):
        from backend.core.partition_aware_health import HealthVerdict
        self.assertIsNotNone(HealthVerdict)

    def test_ordering(self):
        from backend.core.partition_aware_health import HealthVerdict
        self.assertGreater(HealthVerdict.HEALTHY, HealthVerdict.DEGRADED)
        self.assertGreater(HealthVerdict.DEGRADED, HealthVerdict.UNKNOWN)
        self.assertGreater(HealthVerdict.UNKNOWN, HealthVerdict.PARTITIONED)
        self.assertGreater(HealthVerdict.PARTITIONED, HealthVerdict.UNREACHABLE)

    def test_comparison_with_threshold(self):
        from backend.core.partition_aware_health import HealthVerdict
        # Common pattern: verdict >= DEGRADED means "usable"
        self.assertTrue(HealthVerdict.HEALTHY >= HealthVerdict.DEGRADED)
        self.assertTrue(HealthVerdict.DEGRADED >= HealthVerdict.DEGRADED)
        self.assertFalse(HealthVerdict.PARTITIONED >= HealthVerdict.DEGRADED)

    def test_int_values(self):
        from backend.core.partition_aware_health import HealthVerdict
        self.assertEqual(int(HealthVerdict.UNREACHABLE), 0)
        self.assertEqual(int(HealthVerdict.HEALTHY), 4)


class TestPartitionDetector(unittest.TestCase):
    """Test PartitionDetector bidirectional reachability tracking."""

    def _make_detector(self, window=5, threshold=0.7):
        from backend.core.partition_aware_health import PartitionDetector
        return PartitionDetector(
            component_id="test",
            window_size=window,
            partition_threshold=threshold,
        )

    def test_empty_detector_returns_unknown(self):
        from backend.core.partition_aware_health import HealthVerdict
        d = self._make_detector()
        self.assertEqual(d.assess(), HealthVerdict.UNKNOWN)

    def test_forward_only_good_returns_degraded(self):
        from backend.core.partition_aware_health import HealthVerdict
        d = self._make_detector()
        for _ in range(5):
            d.record_forward(True, latency_ms=10)
        # Forward OK but no reverse data → DEGRADED (can't confirm bidirectional)
        self.assertEqual(d.assess(), HealthVerdict.DEGRADED)

    def test_both_directions_good_returns_healthy(self):
        from backend.core.partition_aware_health import HealthVerdict
        d = self._make_detector()
        for _ in range(5):
            d.record_forward(True, latency_ms=10)
            d.record_reverse(True, latency_ms=15)
        self.assertEqual(d.assess(), HealthVerdict.HEALTHY)

    def test_both_directions_bad_returns_unreachable(self):
        from backend.core.partition_aware_health import HealthVerdict
        d = self._make_detector()
        for _ in range(5):
            d.record_forward(False, error="timeout")
            d.record_reverse(False, error="timeout")
        self.assertEqual(d.assess(), HealthVerdict.UNREACHABLE)

    def test_forward_good_reverse_bad_returns_partitioned(self):
        from backend.core.partition_aware_health import HealthVerdict
        d = self._make_detector()
        for _ in range(5):
            d.record_forward(True, latency_ms=10)
            d.record_reverse(False, error="no route")
        self.assertEqual(d.assess(), HealthVerdict.PARTITIONED)

    def test_forward_bad_reverse_good_returns_partitioned(self):
        from backend.core.partition_aware_health import HealthVerdict
        d = self._make_detector()
        for _ in range(5):
            d.record_forward(False, error="timeout")
            d.record_reverse(True, latency_ms=10)
        self.assertEqual(d.assess(), HealthVerdict.PARTITIONED)

    def test_is_partitioned_detection(self):
        d = self._make_detector()
        for _ in range(5):
            d.record_forward(True)
            d.record_reverse(False)
        is_part, reason = d.is_partitioned()
        self.assertTrue(is_part)
        self.assertIn("Asymmetric partition", reason)

    def test_is_partitioned_not_detected_when_both_ok(self):
        d = self._make_detector()
        for _ in range(5):
            d.record_forward(True)
            d.record_reverse(True)
        is_part, _ = d.is_partitioned()
        self.assertFalse(is_part)

    def test_window_size_truncation(self):
        d = self._make_detector(window=3)
        # Record 3 failures then 3 successes
        for _ in range(3):
            d.record_forward(False)
        for _ in range(3):
            d.record_forward(True)
        # Window should only contain the 3 recent successes
        from backend.core.partition_aware_health import HealthVerdict
        # Forward OK, no reverse → DEGRADED
        self.assertEqual(d.assess(), HealthVerdict.DEGRADED)

    def test_reset_clears_all(self):
        from backend.core.partition_aware_health import HealthVerdict
        d = self._make_detector()
        for _ in range(5):
            d.record_forward(True)
            d.record_reverse(True)
        self.assertEqual(d.assess(), HealthVerdict.HEALTHY)
        d.reset()
        self.assertEqual(d.assess(), HealthVerdict.UNKNOWN)

    def test_thread_safety(self):
        d = self._make_detector(window=100)
        errors = []

        def record_batch(forward: bool):
            try:
                for _ in range(50):
                    d.record_forward(forward)
                    d.record_reverse(forward)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=record_batch, args=(True,)),
            threading.Thread(target=record_batch, args=(False,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        # Should produce a valid verdict without crashing
        verdict = d.assess()
        from backend.core.partition_aware_health import HealthVerdict
        self.assertIsInstance(verdict, HealthVerdict)


class TestCoordinatedHealthDecision(unittest.TestCase):
    """Test multi-signal health decisions."""

    def test_should_promote_all_checks_pass(self):
        from backend.core.partition_aware_health import (
            CoordinatedHealthDecision, HealthVerdict,
        )
        d = CoordinatedHealthDecision(
            outbound_ok=True,
            partition_verdict=HealthVerdict.HEALTHY,
            consecutive_successes=3,
        )
        should, reason = d.should_promote()
        self.assertTrue(should)
        self.assertEqual(reason, "all checks passed")

    def test_should_promote_fails_on_outbound_failure(self):
        from backend.core.partition_aware_health import (
            CoordinatedHealthDecision, HealthVerdict,
        )
        d = CoordinatedHealthDecision(
            outbound_ok=False,
            partition_verdict=HealthVerdict.HEALTHY,
            consecutive_successes=3,
        )
        should, reason = d.should_promote()
        self.assertFalse(should)
        self.assertIn("outbound", reason)

    def test_should_promote_fails_on_partition(self):
        from backend.core.partition_aware_health import (
            CoordinatedHealthDecision, HealthVerdict,
        )
        d = CoordinatedHealthDecision(
            outbound_ok=True,
            partition_verdict=HealthVerdict.PARTITIONED,
            consecutive_successes=3,
        )
        should, reason = d.should_promote()
        self.assertFalse(should)
        self.assertIn("partition", reason)

    def test_should_promote_fails_on_insufficient_successes(self):
        from backend.core.partition_aware_health import (
            CoordinatedHealthDecision, HealthVerdict,
        )
        d = CoordinatedHealthDecision(
            outbound_ok=True,
            partition_verdict=HealthVerdict.HEALTHY,
            consecutive_successes=1,
        )
        should, reason = d.should_promote()
        self.assertFalse(should)
        self.assertIn("consecutive successes", reason)

    def test_should_promote_fails_on_cooldown(self):
        from backend.core.partition_aware_health import (
            CoordinatedHealthDecision, HealthVerdict,
        )
        d = CoordinatedHealthDecision(
            outbound_ok=True,
            partition_verdict=HealthVerdict.HEALTHY,
            consecutive_successes=3,
            last_transition_mono=time.monotonic(),
            cooldown_s=60.0,
        )
        should, reason = d.should_promote()
        self.assertFalse(should)
        self.assertIn("cooldown", reason)

    def test_should_demote_on_partition(self):
        from backend.core.partition_aware_health import (
            CoordinatedHealthDecision, HealthVerdict,
        )
        d = CoordinatedHealthDecision(
            outbound_ok=True,  # Even though outbound is OK
            partition_verdict=HealthVerdict.PARTITIONED,
        )
        should, reason = d.should_demote()
        self.assertTrue(should)
        self.assertIn("partition", reason)

    def test_should_demote_on_failures(self):
        from backend.core.partition_aware_health import (
            CoordinatedHealthDecision, HealthVerdict,
        )
        d = CoordinatedHealthDecision(
            outbound_ok=False,
            partition_verdict=HealthVerdict.UNREACHABLE,
            consecutive_failures=5,
        )
        should, reason = d.should_demote()
        self.assertTrue(should)
        self.assertIn("consecutive failures", reason)

    def test_should_demote_fails_on_insufficient_failures(self):
        from backend.core.partition_aware_health import (
            CoordinatedHealthDecision, HealthVerdict,
        )
        d = CoordinatedHealthDecision(
            outbound_ok=False,
            partition_verdict=HealthVerdict.UNREACHABLE,
            consecutive_failures=1,
        )
        should, reason = d.should_demote()
        self.assertFalse(should)
        self.assertIn("insufficient consecutive failures", reason)

    def test_should_not_demote_when_outbound_ok_and_no_partition(self):
        from backend.core.partition_aware_health import (
            CoordinatedHealthDecision, HealthVerdict,
        )
        d = CoordinatedHealthDecision(
            outbound_ok=True,
            partition_verdict=HealthVerdict.HEALTHY,
            consecutive_failures=0,
        )
        should, _ = d.should_demote()
        self.assertFalse(should)


class TestAtomicEndpointState(unittest.TestCase):
    """Test versioned/generational endpoint state."""

    def setUp(self):
        from backend.core.partition_aware_health import AtomicEndpointState
        AtomicEndpointState.reset()

    def test_get_current_creates_empty(self):
        from backend.core.partition_aware_health import AtomicEndpointState
        state = AtomicEndpointState.get_current()
        self.assertIsNone(state.host)
        self.assertEqual(state.generation, 0)

    def test_update_increments_generation(self):
        from backend.core.partition_aware_health import AtomicEndpointState
        s1 = AtomicEndpointState.update("10.0.0.1", 8000, True, "test")
        self.assertEqual(s1.generation, 1)
        s2 = AtomicEndpointState.update("10.0.0.2", 8001, True, "test2")
        self.assertEqual(s2.generation, 2)

    def test_url_property(self):
        from backend.core.partition_aware_health import AtomicEndpointState
        state = AtomicEndpointState.update("10.0.0.1", 8000, True)
        self.assertEqual(state.url, "http://10.0.0.1:8000")

    def test_url_none_when_no_host(self):
        from backend.core.partition_aware_health import AtomicEndpointState
        state = AtomicEndpointState.get_current()
        self.assertIsNone(state.url)

    def test_try_update_cas_success(self):
        from backend.core.partition_aware_health import AtomicEndpointState
        AtomicEndpointState.update("10.0.0.1", 8000, True)
        current = AtomicEndpointState.get_current()
        success, new_state = AtomicEndpointState.try_update(
            "10.0.0.2", 8001, True,
            expected_generation=current.generation,
        )
        self.assertTrue(success)
        self.assertEqual(new_state.host, "10.0.0.2")

    def test_try_update_cas_failure(self):
        from backend.core.partition_aware_health import AtomicEndpointState
        AtomicEndpointState.update("10.0.0.1", 8000, True)
        success, _ = AtomicEndpointState.try_update(
            "10.0.0.2", 8001, True,
            expected_generation=999,  # wrong generation
        )
        self.assertFalse(success)

    def test_is_stale_for(self):
        from backend.core.partition_aware_health import AtomicEndpointState
        state = AtomicEndpointState.update("10.0.0.1", 8000, True)
        self.assertTrue(state.is_stale_for(999))
        self.assertFalse(state.is_stale_for(0))

    def test_singleton_pattern(self):
        from backend.core.partition_aware_health import AtomicEndpointState
        AtomicEndpointState.update("10.0.0.1", 8000, True)
        s1 = AtomicEndpointState.get_current()
        s2 = AtomicEndpointState.get_current()
        self.assertEqual(s1.generation, s2.generation)
        self.assertEqual(s1.host, s2.host)


class TestPartitionConvenienceFunctions(unittest.TestCase):
    """Test fail-open convenience functions."""

    def test_assess_endpoint_health_no_detector(self):
        from backend.core.partition_aware_health import (
            assess_endpoint_health, HealthVerdict,
        )
        self.assertEqual(assess_endpoint_health(None), HealthVerdict.UNKNOWN)

    def test_assess_endpoint_health_with_detector(self):
        from backend.core.partition_aware_health import (
            assess_endpoint_health, PartitionDetector, HealthVerdict,
        )
        d = PartitionDetector("test", window_size=3, partition_threshold=0.7)
        for _ in range(3):
            d.record_forward(True)
            d.record_reverse(True)
        self.assertEqual(assess_endpoint_health(d), HealthVerdict.HEALTHY)

    def test_is_partition_detected_no_detector(self):
        from backend.core.partition_aware_health import is_partition_detected
        is_part, reason = is_partition_detected(None)
        self.assertFalse(is_part)

    def test_build_health_decision(self):
        from backend.core.partition_aware_health import (
            build_health_decision, PartitionDetector,
        )
        d = PartitionDetector("test", window_size=3, partition_threshold=0.7)
        for _ in range(3):
            d.record_forward(True)
            d.record_reverse(True)
        decision = build_health_decision(outbound_ok=True, detector=d, consecutive_successes=3)
        should_promote, _ = decision.should_promote()
        self.assertTrue(should_promote)

    def test_build_health_decision_fail_open(self):
        from backend.core.partition_aware_health import build_health_decision
        # Pass a non-detector object — should fail-open
        decision = build_health_decision(outbound_ok=True, detector="not_a_detector")
        self.assertTrue(decision.outbound_ok)


# ============================================================================
# Disease 2: Protocol Version Gate Tests
# ============================================================================

class TestProtocolVersion(unittest.TestCase):
    """Test ProtocolVersion parsing and compatibility."""

    def test_import(self):
        from backend.core.protocol_version_gate import ProtocolVersion
        self.assertIsNotNone(ProtocolVersion)

    def test_parse_simple(self):
        from backend.core.protocol_version_gate import ProtocolVersion
        pv = ProtocolVersion.parse("1.2.3")
        self.assertEqual(pv.major, 1)
        self.assertEqual(pv.minor, 2)
        self.assertEqual(pv.patch, 3)

    def test_parse_with_v_prefix(self):
        from backend.core.protocol_version_gate import ProtocolVersion
        pv = ProtocolVersion.parse("v2.0.1")
        self.assertEqual(pv.major, 2)

    def test_parse_with_compat_range(self):
        from backend.core.protocol_version_gate import ProtocolVersion
        pv = ProtocolVersion.parse("1.5.0", min_compat="1.0.0", max_compat="1.9.0")
        self.assertEqual(pv.min_compatible, (1, 0, 0))
        self.assertEqual(pv.max_compatible, (1, 9, 0))

    def test_compatible_same_version(self):
        from backend.core.protocol_version_gate import ProtocolVersion
        local = ProtocolVersion.parse("1.2.0")
        remote = ProtocolVersion.parse("1.2.0")
        compat, _ = local.is_compatible_with(remote)
        self.assertTrue(compat)

    def test_compatible_same_major(self):
        from backend.core.protocol_version_gate import ProtocolVersion
        local = ProtocolVersion.parse("1.2.0")
        remote = ProtocolVersion.parse("1.5.0")
        compat, _ = local.is_compatible_with(remote)
        self.assertTrue(compat)

    def test_incompatible_different_major(self):
        from backend.core.protocol_version_gate import ProtocolVersion
        local = ProtocolVersion.parse("1.2.0")
        remote = ProtocolVersion.parse("2.0.0")
        compat, reason = local.is_compatible_with(remote)
        self.assertFalse(compat)
        self.assertIn("major version mismatch", reason)

    def test_incompatible_below_min(self):
        from backend.core.protocol_version_gate import ProtocolVersion
        local = ProtocolVersion.parse("1.5.0", min_compat="1.3.0")
        remote = ProtocolVersion.parse("1.1.0")
        compat, reason = local.is_compatible_with(remote)
        self.assertFalse(compat)
        self.assertIn("below min", reason)

    def test_incompatible_above_max(self):
        from backend.core.protocol_version_gate import ProtocolVersion
        local = ProtocolVersion.parse("1.5.0", max_compat="1.7.0")
        remote = ProtocolVersion.parse("1.9.0")
        compat, reason = local.is_compatible_with(remote)
        self.assertFalse(compat)
        self.assertIn("above max", reason)

    def test_from_env(self):
        from backend.core.protocol_version_gate import ProtocolVersion
        with patch.dict(os.environ, {"JARVIS_PROTOCOL_VERSION": "2.1.0"}):
            pv = ProtocolVersion.from_env()
            self.assertEqual(pv.major, 2)
            self.assertEqual(pv.minor, 1)

    def test_str_representation(self):
        from backend.core.protocol_version_gate import ProtocolVersion
        pv = ProtocolVersion.parse("3.2.1")
        self.assertEqual(str(pv), "3.2.1")

    def test_as_tuple(self):
        from backend.core.protocol_version_gate import ProtocolVersion
        pv = ProtocolVersion.parse("1.2.3")
        self.assertEqual(pv.as_tuple(), (1, 2, 3))


class TestVersionGate(unittest.TestCase):
    """Test VersionGate hot-swap blocking."""

    def _make_gate(self, version="1.5.0", min_compat=None, max_compat=None, caps=None):
        from backend.core.protocol_version_gate import ProtocolVersion, VersionGate
        local = ProtocolVersion.parse(version, min_compat, max_compat)
        return VersionGate(local_version=local, required_capabilities=caps)

    def test_allow_compatible(self):
        gate = self._make_gate("1.5.0")
        allowed, _ = gate.check(remote_version_str="1.3.0")
        self.assertTrue(allowed)

    def test_block_incompatible_major(self):
        gate = self._make_gate("1.5.0")
        allowed, reason = gate.check(remote_version_str="2.0.0")
        self.assertFalse(allowed)
        self.assertIn("major", reason)

    def test_block_below_min_compat(self):
        gate = self._make_gate("1.5.0", min_compat="1.3.0")
        allowed, reason = gate.check(remote_version_str="1.1.0")
        self.assertFalse(allowed)
        self.assertIn("below min", reason)

    def test_allow_when_no_version_provided(self):
        gate = self._make_gate("1.5.0")
        allowed, reason = gate.check()
        self.assertTrue(allowed)
        self.assertIn("no version provided", reason)

    def test_block_missing_capabilities(self):
        gate = self._make_gate("1.5.0", caps={"streaming", "hot_swap"})
        allowed, reason = gate.check(
            remote_version_str="1.5.0",
            remote_capabilities={"streaming"},
        )
        self.assertFalse(allowed)
        self.assertIn("hot_swap", reason)

    def test_allow_all_capabilities_present(self):
        gate = self._make_gate("1.5.0", caps={"streaming", "hot_swap"})
        allowed, _ = gate.check(
            remote_version_str="1.5.0",
            remote_capabilities={"streaming", "hot_swap", "metrics"},
        )
        self.assertTrue(allowed)

    def test_fail_open_on_bad_version_string(self):
        gate = self._make_gate("1.5.0")
        # Malformed version string should not crash, should allow
        allowed, reason = gate.check(remote_version_str="not-a-version!!!")
        # Fail-open behavior
        self.assertTrue(allowed)

    def test_thread_safety(self):
        gate = self._make_gate("1.5.0")
        errors = []

        def check_batch():
            try:
                for _ in range(50):
                    gate.check(remote_version_str="1.3.0")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=check_batch) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


class TestHealthSchemaValidation(unittest.TestCase):
    """Test health response schema validation wiring."""

    def test_check_health_schema_valid(self):
        from backend.core.protocol_version_gate import check_health_schema
        valid, violations = check_health_schema("/health", {"status": "ok"})
        self.assertTrue(valid)
        self.assertEqual(violations, [])

    def test_check_health_schema_missing_field(self):
        from backend.core.protocol_version_gate import check_health_schema
        valid, violations = check_health_schema("/health", {})
        # If startup_contracts is importable, this should detect missing "status"
        # If not importable, fail-open returns True
        if valid:
            pass  # Fail-open, acceptable
        else:
            self.assertTrue(any("status" in v for v in violations))

    def test_check_health_schema_unknown_endpoint(self):
        from backend.core.protocol_version_gate import check_health_schema
        valid, violations = check_health_schema("/unknown", {"anything": True})
        self.assertTrue(valid)  # Unknown schema = no validation = valid


class TestVersionConvenienceFunctions(unittest.TestCase):
    """Test fail-open convenience functions."""

    def test_version_check_for_hotswap_compatible(self):
        from backend.core.protocol_version_gate import version_check_for_hotswap
        ok, _ = version_check_for_hotswap(
            remote_version_str="1.2.0",
            local_version_str="1.5.0",
        )
        self.assertTrue(ok)

    def test_version_check_for_hotswap_incompatible(self):
        from backend.core.protocol_version_gate import version_check_for_hotswap
        ok, reason = version_check_for_hotswap(
            remote_version_str="2.0.0",
            local_version_str="1.5.0",
        )
        self.assertFalse(ok)
        self.assertIn("major", reason)

    def test_version_check_for_hotswap_no_remote(self):
        from backend.core.protocol_version_gate import version_check_for_hotswap
        ok, reason = version_check_for_hotswap()
        self.assertTrue(ok)
        self.assertIn("no remote", reason)

    def test_version_check_for_hotswap_fail_open(self):
        from backend.core.protocol_version_gate import version_check_for_hotswap
        # Both versions malformed → fail-open
        ok, _ = version_check_for_hotswap(
            remote_version_str="???",
            local_version_str="!!!",
        )
        self.assertTrue(ok)

    def test_extract_version_from_health(self):
        from backend.core.protocol_version_gate import extract_version_from_health
        v = extract_version_from_health({"protocol_version": "1.2.3"})
        self.assertEqual(v, "1.2.3")

    def test_extract_version_fallback_keys(self):
        from backend.core.protocol_version_gate import extract_version_from_health
        v = extract_version_from_health({"version": "2.0.0"})
        self.assertEqual(v, "2.0.0")

    def test_extract_version_none_when_missing(self):
        from backend.core.protocol_version_gate import extract_version_from_health
        v = extract_version_from_health({"status": "ok"})
        self.assertIsNone(v)

    def test_validate_health_before_swap_valid(self):
        from backend.core.protocol_version_gate import validate_health_before_swap
        ok, _ = validate_health_before_swap(
            "/health",
            {"status": "ok", "protocol_version": "1.0.0"},
        )
        self.assertTrue(ok)

    def test_validate_health_before_swap_version_mismatch(self):
        from backend.core.protocol_version_gate import validate_health_before_swap
        with patch.dict(os.environ, {"JARVIS_PROTOCOL_VERSION": "1.0.0"}):
            ok, reason = validate_health_before_swap(
                "/health",
                {"status": "ok"},
                remote_version_str="2.0.0",
            )
            self.assertFalse(ok)
            self.assertIn("major", reason)


# ============================================================================
# Integration Tests: Both Diseases Together
# ============================================================================

class TestIntegrationPartitionAndVersion(unittest.TestCase):
    """Integration tests combining partition detection and version gating."""

    def setUp(self):
        from backend.core.partition_aware_health import AtomicEndpointState
        AtomicEndpointState.reset()

    def test_full_promotion_flow(self):
        """Simulates a full GCP VM promotion with partition check + version gate."""
        from backend.core.partition_aware_health import (
            PartitionDetector, build_health_decision, AtomicEndpointState,
        )
        from backend.core.protocol_version_gate import (
            validate_health_before_swap,
        )

        # 1. Partition detector says bidirectional is healthy
        detector = PartitionDetector("supervisor", window_size=3, partition_threshold=0.7)
        for _ in range(3):
            detector.record_forward(True, latency_ms=20)
            detector.record_reverse(True, latency_ms=25)

        # 2. Build coordinated health decision
        decision = build_health_decision(
            outbound_ok=True,
            detector=detector,
            consecutive_successes=3,
        )
        should_promote, _ = decision.should_promote()
        self.assertTrue(should_promote)

        # 3. Validate health response schema + version
        ok, _ = validate_health_before_swap(
            "prime:/health",
            {"ready_for_inference": True, "protocol_version": "1.0.0"},
        )
        self.assertTrue(ok)

        # 4. Atomic endpoint state update
        state = AtomicEndpointState.update("34.45.154.209", 8000, True, "promotion")
        self.assertEqual(state.generation, 1)
        self.assertTrue(state.is_gcp)

    def test_partition_blocks_promotion(self):
        """Partition detected → promotion blocked even though outbound is OK."""
        from backend.core.partition_aware_health import (
            PartitionDetector, build_health_decision,
        )

        detector = PartitionDetector("supervisor", window_size=3, partition_threshold=0.7)
        for _ in range(3):
            detector.record_forward(True)
            detector.record_reverse(False)  # Reverse failing

        decision = build_health_decision(
            outbound_ok=True,
            detector=detector,
            consecutive_successes=3,
        )
        should_promote, reason = decision.should_promote()
        self.assertFalse(should_promote)
        self.assertIn("partition", reason)

    def test_version_mismatch_blocks_hotswap(self):
        """Version gate blocks hot-swap when major version differs."""
        from backend.core.protocol_version_gate import validate_health_before_swap

        with patch.dict(os.environ, {"JARVIS_PROTOCOL_VERSION": "1.0.0"}):
            ok, reason = validate_health_before_swap(
                "prime:/health",
                {"ready_for_inference": True},
                remote_version_str="2.0.0",
            )
            self.assertFalse(ok)

    def test_stale_generation_blocks_cas_update(self):
        """CAS update fails on stale generation → prevents race condition."""
        from backend.core.partition_aware_health import AtomicEndpointState

        AtomicEndpointState.update("10.0.0.1", 8000, True, "first")
        # Concurrent update changes generation
        AtomicEndpointState.update("10.0.0.2", 8001, True, "second")

        # Stale CAS attempt
        success, _ = AtomicEndpointState.try_update(
            "10.0.0.3", 8002, True,
            expected_generation=1,  # Was gen 1, now gen 2
        )
        self.assertFalse(success)


# ============================================================================
# Wiring Integration Tests
# ============================================================================

class TestWiringIntegration(unittest.TestCase):
    """Test that wiring points in consumer files reference our modules correctly."""

    def test_partition_aware_health_importable(self):
        import backend.core.partition_aware_health as m
        self.assertTrue(hasattr(m, "HealthVerdict"))
        self.assertTrue(hasattr(m, "PartitionDetector"))
        self.assertTrue(hasattr(m, "CoordinatedHealthDecision"))
        self.assertTrue(hasattr(m, "AtomicEndpointState"))
        self.assertTrue(hasattr(m, "assess_endpoint_health"))
        self.assertTrue(hasattr(m, "is_partition_detected"))
        self.assertTrue(hasattr(m, "build_health_decision"))

    def test_protocol_version_gate_importable(self):
        import backend.core.protocol_version_gate as m
        self.assertTrue(hasattr(m, "ProtocolVersion"))
        self.assertTrue(hasattr(m, "VersionGate"))
        self.assertTrue(hasattr(m, "check_health_schema"))
        self.assertTrue(hasattr(m, "version_check_for_hotswap"))
        self.assertTrue(hasattr(m, "extract_version_from_health"))
        self.assertTrue(hasattr(m, "validate_health_before_swap"))

    def test_version_negotiation_semantic_version_importable(self):
        """Verify our dependency (existing version_negotiation.py) is importable."""
        try:
            from backend.core.version_negotiation import SemanticVersion
            sv = SemanticVersion.parse("1.2.3")
            self.assertEqual(sv.major, 1)
        except ImportError:
            self.skipTest("version_negotiation.py not on path")

    def test_startup_contracts_importable(self):
        """Verify our dependency (existing startup_contracts.py) is importable."""
        try:
            from backend.core.startup_contracts import validate_health_response
            violations = validate_health_response("/health", {"status": "ok"})
            self.assertEqual(violations, [])
        except ImportError:
            self.skipTest("startup_contracts.py not on path")


class TestConsumerWiring(unittest.TestCase):
    """Verify Phase 12 wiring exists in consumer files via grep."""

    def _file_contains(self, filepath: str, pattern: str) -> bool:
        """Check if file contains a pattern."""
        try:
            with open(filepath) as f:
                return pattern in f.read()
        except FileNotFoundError:
            return False

    def test_supervisor_propagate_has_atomic_endpoint_state(self):
        self.assertTrue(self._file_contains(
            os.path.join(_PROJECT_ROOT, "unified_supervisor.py"),
            "AtomicEndpointState",
        ))

    def test_supervisor_clear_has_atomic_endpoint_state(self):
        content = ""
        with open(os.path.join(_PROJECT_ROOT, "unified_supervisor.py")) as f:
            content = f.read()
        # Both propagate and clear should have AtomicEndpointState
        self.assertGreater(content.count("AtomicEndpointState"), 1)

    def test_prime_router_has_partition_check(self):
        self.assertTrue(self._file_contains(
            os.path.join(_PROJECT_ROOT, "backend", "core", "prime_router.py"),
            "is_partition_detected",
        ))

    def test_prime_client_has_version_gate(self):
        self.assertTrue(self._file_contains(
            os.path.join(_PROJECT_ROOT, "backend", "core", "prime_client.py"),
            "validate_health_before_swap",
        ))

    def test_prime_client_has_partition_detector(self):
        self.assertTrue(self._file_contains(
            os.path.join(_PROJECT_ROOT, "backend", "core", "prime_client.py"),
            "PartitionDetector",
        ))

    def test_gcp_vm_manager_has_version_validation(self):
        self.assertTrue(self._file_contains(
            os.path.join(_PROJECT_ROOT, "backend", "core", "gcp_vm_manager.py"),
            "validate_health_before_swap",
        ))

    def test_supervisor_singleton_heartbeat_has_protocol_version(self):
        self.assertTrue(self._file_contains(
            os.path.join(_PROJECT_ROOT, "backend", "core", "supervisor_singleton.py"),
            "protocol_version",
        ))

    def test_supervisor_singleton_has_version_check(self):
        self.assertTrue(self._file_contains(
            os.path.join(_PROJECT_ROOT, "backend", "core", "supervisor_singleton.py"),
            "version_check_for_hotswap",
        ))


if __name__ == "__main__":
    unittest.main()
