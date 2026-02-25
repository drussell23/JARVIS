"""Tests for v271.0 Phase 8: State authority and decision audit trail.

Validates:
1. StateDeclaration registry — frozen dataclass, concept declarations
2. validate_consistency() — divergence detection
3. DecisionLog — bounded ring buffer, thread-safety, query API
4. PrimeRouter flapping protection — cooldown enforcement
5. Integration wiring — decision_log importable, record_decision safe
"""

import asyncio
import importlib
import os
import threading
import time

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _import_module(dotted_name: str):
    try:
        return importlib.import_module(dotted_name)
    except Exception:
        return None


# ===========================================================================
# 1. StateDeclaration Registry
# ===========================================================================

class TestStateDeclaration:
    """Verify state_authority.py module structure and declarations."""

    def test_module_imports(self):
        mod = _import_module("backend.core.state_authority")
        assert mod is not None, "state_authority must be importable"

    def test_state_declarations_registry_exists(self):
        from backend.core.state_authority import STATE_DECLARATIONS
        assert isinstance(STATE_DECLARATIONS, dict)
        assert len(STATE_DECLARATIONS) >= 3, (
            f"Expected at least 3 state declarations, got {len(STATE_DECLARATIONS)}"
        )

    def test_declarations_are_frozen(self):
        from backend.core.state_authority import STATE_DECLARATIONS
        decl = STATE_DECLARATIONS["gcp_vm_readiness"]
        with pytest.raises(AttributeError):
            decl.concept = "hacked"  # type: ignore

    def test_gcp_vm_readiness_declaration(self):
        from backend.core.state_authority import get_state_declaration
        d = get_state_declaration("gcp_vm_readiness")
        assert d is not None
        assert d.authoritative_source == "supervisor._invincible_node_ready"
        assert "supervisor._invincible_node_ip" in d.secondary_sources

    def test_prime_routing_mode_declaration(self):
        from backend.core.state_authority import get_state_declaration
        d = get_state_declaration("prime_routing_mode")
        assert d is not None
        assert d.authoritative_source == "prime_router._gcp_promoted"
        assert "prime_router._gcp_host" in d.secondary_sources

    def test_startup_memory_mode_declaration(self):
        from backend.core.state_authority import get_state_declaration
        d = get_state_declaration("startup_memory_mode")
        assert d is not None
        assert d.authoritative_source == "env:JARVIS_STARTUP_MEMORY_MODE"

    def test_unknown_concept_returns_none(self):
        from backend.core.state_authority import get_state_declaration
        assert get_state_declaration("nonexistent_concept") is None


# ===========================================================================
# 2. Validate Consistency
# ===========================================================================

class TestValidateConsistency:
    """Verify consistency validation logic."""

    def test_validate_returns_list(self):
        from backend.core.state_authority import validate_consistency
        results = validate_consistency()
        assert isinstance(results, list)

    def test_validate_with_no_objects_skips_gracefully(self):
        from backend.core.state_authority import validate_consistency
        results = validate_consistency()
        # Should not crash, should skip object-dependent checks
        for r in results:
            assert r.consistent is True or len(r.divergences) > 0

    def test_validate_startup_mode_consistent(self, monkeypatch):
        from backend.core.state_authority import validate_consistency
        monkeypatch.setenv("JARVIS_STARTUP_MEMORY_MODE", "local_full")
        monkeypatch.setenv("JARVIS_STARTUP_EFFECTIVE_MODE", "local_full")
        results = validate_consistency(concept="startup_memory_mode")
        assert len(results) == 1
        assert results[0].consistent is True
        assert results[0].authoritative_value == "local_full"

    def test_validate_startup_mode_divergent(self, monkeypatch):
        from backend.core.state_authority import validate_consistency
        monkeypatch.setenv("JARVIS_STARTUP_MEMORY_MODE", "local_full")
        monkeypatch.setenv("JARVIS_STARTUP_EFFECTIVE_MODE", "cloud_only")
        results = validate_consistency(concept="startup_memory_mode")
        assert len(results) == 1
        assert results[0].consistent is False
        assert any("EFFECTIVE_MODE" in d for d in results[0].divergences)

    def test_validate_gcp_readiness_consistent(self, monkeypatch):
        """Mock supervisor with consistent state -> no divergence."""
        from backend.core.state_authority import validate_consistency
        monkeypatch.setenv("JARVIS_INVINCIBLE_NODE_IP", "10.0.0.1")
        monkeypatch.setenv("JARVIS_HOLLOW_CLIENT_ACTIVE", "true")

        class MockSupervisor:
            _invincible_node_ready = True
            _invincible_node_ip = "10.0.0.1"

        results = validate_consistency(
            concept="gcp_vm_readiness", supervisor=MockSupervisor()
        )
        assert len(results) == 1
        assert results[0].consistent is True

    def test_validate_gcp_readiness_divergent(self, monkeypatch):
        """Mock supervisor with ready=True but no env var -> divergence."""
        from backend.core.state_authority import validate_consistency
        monkeypatch.delenv("JARVIS_INVINCIBLE_NODE_IP", raising=False)
        monkeypatch.delenv("JARVIS_HOLLOW_CLIENT_ACTIVE", raising=False)

        class MockSupervisor:
            _invincible_node_ready = True
            _invincible_node_ip = "10.0.0.1"

        results = validate_consistency(
            concept="gcp_vm_readiness", supervisor=MockSupervisor()
        )
        assert len(results) == 1
        assert results[0].consistent is False
        assert any("env var not set" in d for d in results[0].divergences)

    def test_validate_prime_routing_consistent(self):
        """Mock router with consistent state."""
        from backend.core.state_authority import validate_consistency

        class MockRouter:
            _gcp_promoted = False
            _gcp_host = None

        results = validate_consistency(
            concept="prime_routing_mode", prime_router=MockRouter()
        )
        assert len(results) == 1
        assert results[0].consistent is True

    def test_validate_prime_routing_divergent(self):
        """Mock router with promoted=True but no host."""
        from backend.core.state_authority import validate_consistency

        class MockRouter:
            _gcp_promoted = True
            _gcp_host = None

        results = validate_consistency(
            concept="prime_routing_mode", prime_router=MockRouter()
        )
        assert len(results) == 1
        assert results[0].consistent is False
        assert any("_gcp_host is None" in d for d in results[0].divergences)

    def test_validate_at_boot_returns_strings(self):
        from backend.core.state_authority import validate_consistency_at_boot
        warnings = validate_consistency_at_boot()
        assert isinstance(warnings, list)
        for w in warnings:
            assert isinstance(w, str)


# ===========================================================================
# 3. DecisionLog — Ring Buffer
# ===========================================================================

class TestDecisionLog:
    """Verify decision_log.py ring buffer and query API."""

    def test_module_imports(self):
        mod = _import_module("backend.core.decision_log")
        assert mod is not None, "decision_log must be importable"

    def test_record_returns_decision_record(self):
        from backend.core.decision_log import DecisionLog, DecisionRecord
        log = DecisionLog(max_entries=10)
        rec = log.record(
            decision_type="test",
            reason="testing",
            inputs={"key": "value"},
            outcome="success",
            component="test_suite",
        )
        assert isinstance(rec, DecisionRecord)
        assert rec.decision_type == "test"
        assert rec.reason == "testing"
        assert rec.outcome == "success"
        assert rec.component == "test_suite"
        assert rec.inputs == {"key": "value"}
        assert rec.timestamp > 0

    def test_ring_buffer_bounded(self):
        from backend.core.decision_log import DecisionLog
        log = DecisionLog(max_entries=5)
        for i in range(10):
            log.record(
                decision_type="test",
                reason=f"entry_{i}",
                inputs={"i": i},
                outcome="ok",
            )
        assert log.size == 5
        # Oldest entries should be rotated out
        recent = log.get_recent(10)
        reasons = [r.reason for r in recent]
        assert "entry_0" not in reasons
        assert "entry_9" in reasons

    def test_query_by_type(self):
        from backend.core.decision_log import DecisionLog
        log = DecisionLog(max_entries=50)
        log.record("type_a", "a1", {}, "ok")
        log.record("type_b", "b1", {}, "ok")
        log.record("type_a", "a2", {}, "ok")

        results = log.query(decision_type="type_a")
        assert len(results) == 2
        assert all(r.decision_type == "type_a" for r in results)

    def test_query_by_time_range(self):
        from backend.core.decision_log import DecisionLog
        log = DecisionLog(max_entries=50)

        t1 = time.time()
        log.record("test", "early", {}, "ok")
        time.sleep(0.05)
        t2 = time.time()
        log.record("test", "late", {}, "ok")
        t3 = time.time()

        results = log.query(since=t2, until=t3)
        assert len(results) == 1
        assert results[0].reason == "late"

    def test_query_newest_first(self):
        from backend.core.decision_log import DecisionLog
        log = DecisionLog(max_entries=50)
        for i in range(5):
            log.record("test", f"entry_{i}", {}, "ok")

        results = log.query()
        assert results[0].reason == "entry_4"
        assert results[-1].reason == "entry_0"

    def test_get_counts_cumulative(self):
        from backend.core.decision_log import DecisionLog
        log = DecisionLog(max_entries=3)
        for i in range(5):
            log.record("test_type", f"entry_{i}", {}, "ok")

        counts = log.get_counts()
        # Cumulative count is 5, even though buffer only holds 3
        assert counts["test_type"] == 5

    def test_thread_safety(self):
        from backend.core.decision_log import DecisionLog
        log = DecisionLog(max_entries=1000)
        errors = []

        def writer(thread_id):
            try:
                for i in range(100):
                    log.record(
                        f"thread_{thread_id}",
                        f"entry_{i}",
                        {"thread": thread_id, "i": i},
                        "ok",
                    )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert log.size == 500
        counts = log.get_counts()
        total = sum(counts.values())
        assert total == 500

    def test_get_recent(self):
        from backend.core.decision_log import DecisionLog
        log = DecisionLog(max_entries=50)
        for i in range(10):
            log.record("test", f"entry_{i}", {}, "ok")
        recent = log.get_recent(3)
        assert len(recent) == 3
        assert recent[0].reason == "entry_9"

    def test_to_dicts(self):
        from backend.core.decision_log import DecisionLog
        log = DecisionLog(max_entries=50)
        log.record("test", "first", {"a": 1}, "ok", component="test")
        dicts = log.to_dicts()
        assert len(dicts) == 1
        assert isinstance(dicts[0], dict)
        assert dicts[0]["decision_type"] == "test"
        assert dicts[0]["inputs"] == {"a": 1}

    def test_record_decision_convenience_never_raises(self):
        from backend.core.decision_log import record_decision
        # Should not raise even with weird inputs
        result = record_decision(
            decision_type="test",
            reason="safety test",
            inputs={"x": 1},
            outcome="ok",
        )
        assert result is not None

    def test_singleton_pattern(self):
        from backend.core.decision_log import get_decision_log
        a = get_decision_log()
        b = get_decision_log()
        assert a is b


# ===========================================================================
# 4. Decision Type Constants
# ===========================================================================

class TestDecisionLogConstants:
    """Verify decision type constants exist."""

    def test_vm_termination_constant(self):
        from backend.core.decision_log import DECISION_VM_TERMINATION
        assert DECISION_VM_TERMINATION == "vm_termination"

    def test_routing_promote_constant(self):
        from backend.core.decision_log import DECISION_ROUTING_PROMOTE
        assert DECISION_ROUTING_PROMOTE == "routing_promote"

    def test_routing_demote_constant(self):
        from backend.core.decision_log import DECISION_ROUTING_DEMOTE
        assert DECISION_ROUTING_DEMOTE == "routing_demote"

    def test_mode_transition_constant(self):
        from backend.core.decision_log import DECISION_MODE_TRANSITION
        assert DECISION_MODE_TRANSITION == "mode_transition"


# ===========================================================================
# 5. PrimeRouter Flapping Protection
# ===========================================================================

class TestPrimeRouterFlappingProtection:
    """Verify promote/demote cooldown enforcement."""

    def test_cooldown_attributes_exist(self):
        from backend.core.prime_router import PrimeRouter
        r = PrimeRouter()
        assert hasattr(r, "_last_transition_time")
        assert hasattr(r, "_transition_cooldown_s")
        assert r._transition_cooldown_s > 0

    def test_check_transition_cooldown_method_exists(self):
        from backend.core.prime_router import PrimeRouter
        assert hasattr(PrimeRouter, "_check_transition_cooldown")

    def test_cooldown_allows_first_transition(self):
        from backend.core.prime_router import PrimeRouter
        r = PrimeRouter()
        assert r._check_transition_cooldown("promote") is True

    def test_cooldown_blocks_rapid_transition(self):
        from backend.core.prime_router import PrimeRouter
        r = PrimeRouter()
        r._transition_cooldown_s = 30.0
        r._last_transition_time = time.monotonic()  # Just transitioned
        assert r._check_transition_cooldown("demote") is False

    def test_cooldown_allows_after_elapsed(self):
        from backend.core.prime_router import PrimeRouter
        r = PrimeRouter()
        r._transition_cooldown_s = 0.01  # Very short cooldown for testing
        r._last_transition_time = time.monotonic()
        time.sleep(0.02)
        assert r._check_transition_cooldown("promote") is True

    def test_cooldown_env_var_override(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ROUTING_TRANSITION_COOLDOWN_S", "120.0")
        from backend.core.prime_router import PrimeRouter
        r = PrimeRouter()
        assert r._transition_cooldown_s == 120.0


# ===========================================================================
# 6. Decision Record Serialization
# ===========================================================================

class TestDecisionRecordSerialization:
    """Verify DecisionRecord to_dict()."""

    def test_to_dict_complete(self):
        from backend.core.decision_log import DecisionRecord
        rec = DecisionRecord(
            decision_type="vm_termination",
            reason="cost waste",
            inputs={"vm": "test-1", "idle": 45.2},
            outcome="terminated",
            component="gcp_vm_manager",
            metadata={"zone": "us-central1-a"},
        )
        d = rec.to_dict()
        assert d["decision_type"] == "vm_termination"
        assert d["reason"] == "cost waste"
        assert d["inputs"]["vm"] == "test-1"
        assert d["outcome"] == "terminated"
        assert d["component"] == "gcp_vm_manager"
        assert d["metadata"]["zone"] == "us-central1-a"
        assert d["timestamp"] > 0


# ===========================================================================
# 7. State Authority — ConsistencyResult
# ===========================================================================

class TestConsistencyResult:
    """Verify ConsistencyResult data model."""

    def test_auto_timestamp(self):
        from backend.core.state_authority import ConsistencyResult
        r = ConsistencyResult(
            concept="test",
            consistent=True,
            authoritative_value="ok",
            divergences=[],
        )
        assert r.checked_at > 0

    def test_divergences_list(self):
        from backend.core.state_authority import ConsistencyResult
        r = ConsistencyResult(
            concept="test",
            consistent=False,
            authoritative_value="bad",
            divergences=["source A disagrees", "source B missing"],
        )
        assert len(r.divergences) == 2


# ===========================================================================
# 8. Integration — Wiring Exists
# ===========================================================================

class TestIntegrationWiring:
    """Verify wiring in consumer files."""

    def test_supervisor_has_state_authority_call(self):
        with open("unified_supervisor.py", "r") as f:
            content = f.read()
        assert "state_authority" in content
        assert "validate_consistency_at_boot" in content

    def test_supervisor_has_decision_log_call(self):
        with open("unified_supervisor.py", "r") as f:
            content = f.read()
        assert "DECISION_MODE_TRANSITION" in content

    def test_gcp_vm_manager_has_decision_log(self):
        with open("backend/core/gcp_vm_manager.py", "r") as f:
            content = f.read()
        assert "DECISION_VM_TERMINATION" in content

    def test_prime_router_has_flapping_protection(self):
        with open("backend/core/prime_router.py", "r") as f:
            content = f.read()
        assert "_check_transition_cooldown" in content
        assert "DECISION_ROUTING_PROMOTE" in content
        assert "DECISION_ROUTING_DEMOTE" in content


# ===========================================================================
# 9. Query Edge Cases
# ===========================================================================

class TestDecisionLogEdgeCases:
    """Verify edge cases in query API."""

    def test_query_empty_log(self):
        from backend.core.decision_log import DecisionLog
        log = DecisionLog(max_entries=10)
        assert log.query() == []
        assert log.get_recent() == []
        assert log.get_counts() == {}
        assert log.to_dicts() == []
        assert log.size == 0

    def test_query_limit(self):
        from backend.core.decision_log import DecisionLog
        log = DecisionLog(max_entries=50)
        for i in range(20):
            log.record("test", f"entry_{i}", {}, "ok")
        results = log.query(limit=5)
        assert len(results) == 5

    def test_record_with_metadata(self):
        from backend.core.decision_log import DecisionLog
        log = DecisionLog(max_entries=10)
        rec = log.record(
            "test", "meta test", {}, "ok",
            metadata={"extra": "info", "count": 42},
        )
        assert rec.metadata["extra"] == "info"
        assert rec.metadata["count"] == 42
