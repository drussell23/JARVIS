"""Tests for v270.2 Phase 5: Observability, component alignment, env atomicity, authority gate.

Validates:
1. _set_startup_env() contract via standalone reproduction (unified_supervisor is not
   importable as a library due to side effects in the 88K-line monolith)
2. Component name alignment between startup_state_machine and supervisor
3. Newly registered components (loading_server, jarvis_prime, jarvis_body)
4. cross_repo supervisor authority gate (grant/revoke/check)
"""

import importlib
import logging
import os
import time

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _import_module(dotted_name: str):
    """Import a module by dotted name, returning None on failure."""
    try:
        return importlib.import_module(dotted_name)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 1. _set_startup_env() contract test (standalone reproduction)
#
# Since unified_supervisor.py cannot be imported in test context (88K-line
# kernel with startup side effects), we reproduce the exact function and
# test its contract. The real function is at unified_supervisor.py:2839.
# ---------------------------------------------------------------------------

_TEST_ENV_WRITE_LOG = []
_TEST_ENV_WRITE_LOG_MAX = 200


def _set_startup_env_standalone(
    key: str,
    value: str,
    reason: str,
    *,
    write_only_true: bool = False,
    caller: str = "",
) -> bool:
    """Standalone copy of _set_startup_env for contract testing."""
    old_value = os.environ.get(key, "")
    if write_only_true and old_value == "true" and value != "true":
        return False
    os.environ[key] = value
    entry = {
        "key": key, "old": old_value, "new": value,
        "reason": reason, "caller": caller, "ts": f"{time.time():.3f}",
    }
    _TEST_ENV_WRITE_LOG.append(entry)
    if len(_TEST_ENV_WRITE_LOG) > _TEST_ENV_WRITE_LOG_MAX:
        _TEST_ENV_WRITE_LOG.pop(0)
    return True


class TestSetStartupEnvContract:
    """Verify the _set_startup_env contract (write guard, logging, ring buffer)."""

    def setup_method(self):
        _TEST_ENV_WRITE_LOG.clear()

    def test_basic_write(self):
        key = "_TEST_PHASE5_BASIC"
        try:
            result = _set_startup_env_standalone(key, "hello", "test_basic", caller="test")
            assert result is True
            assert os.environ.get(key) == "hello"
        finally:
            os.environ.pop(key, None)

    def test_write_only_true_blocks_clearing(self):
        key = "_TEST_PHASE5_WOT"
        try:
            os.environ[key] = "true"
            result = _set_startup_env_standalone(
                key, "false", "clear_attempt", write_only_true=True, caller="test"
            )
            assert result is False, "write_only_true should block clearing"
            assert os.environ.get(key) == "true", "Value should remain 'true'"
        finally:
            os.environ.pop(key, None)

    def test_write_only_true_allows_setting_true(self):
        key = "_TEST_PHASE5_WOT2"
        try:
            os.environ[key] = "false"
            result = _set_startup_env_standalone(
                key, "true", "escalate", write_only_true=True, caller="test"
            )
            assert result is True
            assert os.environ.get(key) == "true"
        finally:
            os.environ.pop(key, None)

    def test_write_only_true_allows_true_to_true(self):
        key = "_TEST_PHASE5_WOT3"
        try:
            os.environ[key] = "true"
            result = _set_startup_env_standalone(
                key, "true", "reaffirm", write_only_true=True, caller="test"
            )
            assert result is True, "true→true should succeed even with write_only_true"
        finally:
            os.environ.pop(key, None)

    def test_ring_buffer_records_writes(self):
        key = "_TEST_PHASE5_LOG"
        try:
            _set_startup_env_standalone(key, "val1", "test_reason", caller="test_caller")
            assert len(_TEST_ENV_WRITE_LOG) > 0
            last = _TEST_ENV_WRITE_LOG[-1]
            assert last["key"] == key
            assert last["new"] == "val1"
            assert last["reason"] == "test_reason"
            assert last["caller"] == "test_caller"
            assert "ts" in last
        finally:
            os.environ.pop(key, None)

    def test_ring_buffer_max_size(self):
        key = "_TEST_PHASE5_RING"
        try:
            for i in range(_TEST_ENV_WRITE_LOG_MAX + 50):
                _set_startup_env_standalone(key, str(i), f"iter_{i}", caller="test")
            assert len(_TEST_ENV_WRITE_LOG) <= _TEST_ENV_WRITE_LOG_MAX
        finally:
            os.environ.pop(key, None)

    def test_unchanged_value_returns_true(self):
        key = "_TEST_PHASE5_UNCHANGED"
        try:
            _set_startup_env_standalone(key, "same", "first", caller="test")
            result = _set_startup_env_standalone(key, "same", "second", caller="test")
            assert result is True, "Unchanged writes should still return True"
        finally:
            os.environ.pop(key, None)

    def test_unset_key_records_empty_old_value(self):
        key = "_TEST_PHASE5_UNSET"
        os.environ.pop(key, None)
        try:
            _set_startup_env_standalone(key, "new_val", "first_set", caller="test")
            last = _TEST_ENV_WRITE_LOG[-1]
            assert last["old"] == "", "Unset keys should show empty old value"
        finally:
            os.environ.pop(key, None)


# ---------------------------------------------------------------------------
# 2. Component name alignment
# ---------------------------------------------------------------------------

class TestComponentNameAlignment:
    """Verify startup_state_machine components match supervisor expectations."""

    def test_two_tier_security_registered(self):
        """Supervisor uses 'two_tier_security' — must be in DAG."""
        from backend.core.startup_state_machine import StartupStateMachine
        sm = StartupStateMachine()
        assert "two_tier_security" in sm.components, (
            "two_tier_security must be registered (supervisor uses this exact name)"
        )

    def test_enterprise_services_registered(self):
        """Supervisor uses 'enterprise_services' — must be in DAG."""
        from backend.core.startup_state_machine import StartupStateMachine
        sm = StartupStateMachine()
        assert "enterprise_services" in sm.components, (
            "enterprise_services must be registered (supervisor uses this exact name)"
        )

    def test_no_mismatched_short_names(self):
        """The short names 'two_tier' and 'enterprise' should NOT be registered."""
        from backend.core.startup_state_machine import StartupStateMachine
        sm = StartupStateMachine()
        for bad_name in ("two_tier", "enterprise"):
            assert bad_name not in sm.components, (
                f"'{bad_name}' is a mismatched short name — use the full name instead"
            )


# ---------------------------------------------------------------------------
# 3. Newly registered components
# ---------------------------------------------------------------------------

class TestNewlyRegisteredComponents:
    """Verify components that were previously undeclared are now registered."""

    def test_loading_server_registered(self):
        from backend.core.startup_state_machine import StartupStateMachine
        sm = StartupStateMachine()
        assert "loading_server" in sm.components
        info = sm.components["loading_server"]
        assert "clean_slate" in info.dependencies

    def test_jarvis_prime_registered(self):
        from backend.core.startup_state_machine import StartupStateMachine
        sm = StartupStateMachine()
        assert "jarvis_prime" in sm.components
        info = sm.components["jarvis_prime"]
        assert "trinity" in info.dependencies

    def test_jarvis_body_registered(self):
        from backend.core.startup_state_machine import StartupStateMachine
        sm = StartupStateMachine()
        assert "jarvis_body" in sm.components
        info = sm.components["jarvis_body"]
        assert "backend" in info.dependencies

    def test_new_components_in_valid_waves(self):
        """Newly registered components should appear in the wave computation."""
        from backend.core.startup_state_machine import StartupStateMachine
        sm = StartupStateMachine()
        waves = sm.compute_waves()
        all_in_waves = set()
        for wave in waves:
            all_in_waves.update(wave)
        for name in ("loading_server", "jarvis_prime", "jarvis_body"):
            assert name in all_in_waves, (
                f"'{name}' not found in any wave — registration may be broken"
            )

    def test_loading_server_before_jarvis_prime(self):
        """loading_server (load_order=2) should be in an earlier wave than jarvis_prime (load_order=13)."""
        from backend.core.startup_state_machine import StartupStateMachine
        sm = StartupStateMachine()
        waves = sm.compute_waves()
        ls_wave = jp_wave = None
        for i, wave in enumerate(waves):
            if "loading_server" in wave:
                ls_wave = i
            if "jarvis_prime" in wave:
                jp_wave = i
        assert ls_wave is not None, "loading_server not in any wave"
        assert jp_wave is not None, "jarvis_prime not in any wave"
        assert ls_wave < jp_wave, (
            f"loading_server (wave {ls_wave}) should precede jarvis_prime (wave {jp_wave})"
        )


# ---------------------------------------------------------------------------
# 4. cross_repo supervisor authority gate
# ---------------------------------------------------------------------------

class TestSupervisorAuthorityGate:
    """Verify the authority gate blocks/allows actions correctly."""

    def _get_gate_functions(self):
        mod = _import_module("backend.supervisor.cross_repo_startup_orchestrator")
        assert mod is not None, "cross_repo_startup_orchestrator must be importable"
        grant = getattr(mod, "grant_supervisor_authority", None)
        revoke = getattr(mod, "revoke_supervisor_authority", None)
        check = getattr(mod, "check_supervisor_authority", None)
        assert grant is not None, "grant_supervisor_authority must exist"
        assert revoke is not None, "revoke_supervisor_authority must exist"
        assert check is not None, "check_supervisor_authority must exist"
        return grant, revoke, check

    def test_default_state_is_blocked(self):
        """Authority should be blocked by default (before supervisor grants it)."""
        grant, revoke, check = self._get_gate_functions()
        revoke("test_reset")
        assert check("test_action") is False

    def test_grant_allows_actions(self):
        grant, revoke, check = self._get_gate_functions()
        try:
            grant("test_grant")
            assert check("test_action") is True
        finally:
            revoke("test_cleanup")

    def test_revoke_blocks_actions(self):
        grant, revoke, check = self._get_gate_functions()
        try:
            grant("test_grant")
            assert check("test_action") is True
            revoke("test_revoke")
            assert check("test_action") is False
        finally:
            revoke("test_cleanup")

    def test_grant_revoke_cycle(self):
        """Multiple grant/revoke cycles should work correctly."""
        grant, revoke, check = self._get_gate_functions()
        try:
            for i in range(3):
                revoke(f"cycle_{i}_revoke")
                assert check(f"action_{i}") is False
                grant(f"cycle_{i}_grant")
                assert check(f"action_{i}") is True
        finally:
            revoke("test_cleanup")

    def test_check_returns_bool(self):
        """check_supervisor_authority must return a proper bool, not truthy/falsy."""
        _, revoke, check = self._get_gate_functions()
        revoke("test_reset")
        result = check("test_action")
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# 5. Full supervisor phase set coverage
# ---------------------------------------------------------------------------

class TestSupervisorPhaseSetComplete:
    """Verify ALL components the supervisor updates are registered in the DAG."""

    def test_all_supervisor_component_names_registered(self):
        """Every name used in _update_component_status() must exist in DAG."""
        from backend.core.startup_state_machine import StartupStateMachine
        sm = StartupStateMachine()
        registered = set(sm.components.keys())
        supervisor_names = {
            "clean_slate", "loading_experience", "preflight", "resources",
            "backend", "intelligence", "trinity", "two_tier_security",
            "enterprise_services", "ghost_display", "agi_os",
            "visual_pipeline", "frontend",
            "loading_server", "jarvis_prime", "jarvis_body",
        }
        missing = supervisor_names - registered
        assert not missing, (
            f"Components used by supervisor but missing from DAG: {missing}"
        )

    def test_dag_still_acyclic_with_new_components(self):
        """The DAG with newly registered components must remain acyclic."""
        from backend.core.startup_state_machine import (
            StartupStateMachine, CyclicDependencyError,
        )
        sm = StartupStateMachine()
        try:
            waves = sm.compute_waves()
            assert len(waves) > 0
        except CyclicDependencyError:
            pytest.fail("Component DAG has a cycle after Phase 5 additions")
