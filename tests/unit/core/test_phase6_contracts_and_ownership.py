"""Tests for v270.3 Phase 6: Versioned contracts, centralized config, async ownership.

Validates:
1. config_constants.py — canonical shared configuration values
2. startup_contracts.py — env var contract registry and validation
3. cross_repo authority gate + task tracking (from Phase 5, verified here)
4. Health endpoint schema validation
"""

import importlib
import os
import re

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
# 1. config_constants.py — Centralized Configuration Constants
# ===========================================================================

class TestConfigConstants:
    """Verify centralized config constants are importable and correct."""

    def test_module_imports(self):
        mod = _import_module("backend.core.config_constants")
        assert mod is not None, "config_constants must be importable"

    def test_port_constants_exist(self):
        from backend.core.config_constants import (
            BACKEND_PORT, FRONTEND_PORT, LOADING_SERVER_PORT,
            LOADING_SERVER_HTTP_PORT, WEBSOCKET_PORT, JARVIS_PRIME_PORT,
        )
        assert isinstance(BACKEND_PORT, int)
        assert isinstance(FRONTEND_PORT, int)
        assert isinstance(LOADING_SERVER_PORT, int)
        assert isinstance(LOADING_SERVER_HTTP_PORT, int)
        assert isinstance(WEBSOCKET_PORT, int)
        assert isinstance(JARVIS_PRIME_PORT, int)

    def test_port_defaults(self):
        """Verify default values match the canonical assignments."""
        from backend.core.config_constants import (
            BACKEND_PORT, FRONTEND_PORT, LOADING_SERVER_PORT,
            WEBSOCKET_PORT, JARVIS_PRIME_PORT,
        )
        # These defaults must match the hardcoded values throughout the codebase
        assert BACKEND_PORT == 8010
        assert FRONTEND_PORT == 3000
        assert LOADING_SERVER_PORT == 3001
        assert WEBSOCKET_PORT == 8765
        assert JARVIS_PRIME_PORT == 8001

    def test_timeout_constants_exist(self):
        from backend.core.config_constants import (
            SHUTDOWN_TIMEOUT, TRINITY_TIMEOUT, CLEAN_SLATE_TIMEOUT,
            HEALTH_CHECK_TIMEOUT, HEALTH_CHECK_INTERVAL,
            GCP_PROBE_TIMEOUT, GCP_RECOVERY_TIMEOUT,
        )
        assert isinstance(SHUTDOWN_TIMEOUT, float)
        assert isinstance(TRINITY_TIMEOUT, float)
        assert isinstance(CLEAN_SLATE_TIMEOUT, float)
        assert isinstance(HEALTH_CHECK_TIMEOUT, float)
        assert isinstance(HEALTH_CHECK_INTERVAL, float)
        assert isinstance(GCP_PROBE_TIMEOUT, float)
        assert isinstance(GCP_RECOVERY_TIMEOUT, float)

    def test_timeout_defaults(self):
        from backend.core.config_constants import (
            SHUTDOWN_TIMEOUT, TRINITY_TIMEOUT,
        )
        assert SHUTDOWN_TIMEOUT == 30.0
        assert TRINITY_TIMEOUT == 600.0

    def test_memory_constants_exist(self):
        from backend.core.config_constants import (
            SPAWN_ADMISSION_MIN_GB, PLANNED_ML_GB, MEMORY_PRESSURE_THRESHOLD,
        )
        assert isinstance(SPAWN_ADMISSION_MIN_GB, float)
        assert isinstance(PLANNED_ML_GB, float)
        assert isinstance(MEMORY_PRESSURE_THRESHOLD, float)

    def test_url_bases_constructed_from_ports(self):
        from backend.core.config_constants import (
            BACKEND_URL, FRONTEND_URL, BACKEND_PORT, FRONTEND_PORT,
        )
        assert str(BACKEND_PORT) in BACKEND_URL
        assert str(FRONTEND_PORT) in FRONTEND_URL

    def test_env_int_fallback_chain(self):
        """_env_int should try keys in order and use default if none set."""
        from backend.core.config_constants import _env_int
        # None of these test keys are set
        result = _env_int(
            "_TEST_P6_KEY1", "_TEST_P6_KEY2", "_TEST_P6_KEY3",
            default=9999
        )
        assert result == 9999

    def test_env_int_reads_primary(self):
        from backend.core.config_constants import _env_int
        key = "_TEST_P6_PRIMARY"
        try:
            os.environ[key] = "42"
            result = _env_int(key, default=0)
            assert result == 42
        finally:
            os.environ.pop(key, None)

    def test_env_int_fallback_to_secondary(self):
        from backend.core.config_constants import _env_int
        primary = "_TEST_P6_PRI"
        secondary = "_TEST_P6_SEC"
        try:
            os.environ[secondary] = "77"
            result = _env_int(primary, secondary, default=0)
            assert result == 77
        finally:
            os.environ.pop(primary, None)
            os.environ.pop(secondary, None)

    def test_env_int_handles_non_numeric(self):
        from backend.core.config_constants import _env_int
        key = "_TEST_P6_BAD"
        try:
            os.environ[key] = "not_a_number"
            result = _env_int(key, default=123)
            assert result == 123
        finally:
            os.environ.pop(key, None)


# ===========================================================================
# 2. startup_contracts.py — Contract Registry and Validation
# ===========================================================================

class TestStartupContracts:
    """Verify contract registry and validation functions."""

    def test_module_imports(self):
        mod = _import_module("backend.core.startup_contracts")
        assert mod is not None, "startup_contracts must be importable"

    def test_contract_version_defined(self):
        from backend.core.startup_contracts import CONTRACT_VERSION
        assert CONTRACT_VERSION == "1.0.0"

    def test_env_contracts_not_empty(self):
        from backend.core.startup_contracts import ENV_CONTRACTS
        assert len(ENV_CONTRACTS) >= 10, (
            f"Expected at least 10 env contracts, got {len(ENV_CONTRACTS)}"
        )

    def test_each_contract_has_required_fields(self):
        from backend.core.startup_contracts import ENV_CONTRACTS
        for c in ENV_CONTRACTS:
            assert c.canonical_name, f"Contract missing canonical_name"
            assert c.description, f"{c.canonical_name} missing description"
            assert c.value_type in ("str", "int", "float", "bool", "url"), (
                f"{c.canonical_name} has invalid value_type={c.value_type}"
            )

    def test_backend_port_contract_has_aliases(self):
        from backend.core.startup_contracts import ENV_CONTRACTS
        port_contracts = [c for c in ENV_CONTRACTS if c.canonical_name == "JARVIS_BACKEND_PORT"]
        assert len(port_contracts) == 1
        c = port_contracts[0]
        # Must have the 3 legacy aliases
        assert "BACKEND_PORT" in c.aliases
        assert "JARVIS_API_PORT" in c.aliases
        assert "JARVIS_PORT" in c.aliases

    def test_hollow_client_contract_has_aliases(self):
        from backend.core.startup_contracts import ENV_CONTRACTS
        hc = [c for c in ENV_CONTRACTS if c.canonical_name == "JARVIS_HOLLOW_CLIENT_ACTIVE"]
        assert len(hc) == 1
        c = hc[0]
        assert "JARVIS_HOLLOW_CLIENT" in c.aliases
        assert "JARVIS_HOLLOW_CLIENT_MODE" in c.aliases


class TestContractValidation:
    """Verify boot-time contract validation."""

    def test_validate_returns_list(self):
        from backend.core.startup_contracts import validate_contracts_at_boot
        result = validate_contracts_at_boot()
        assert isinstance(result, list)

    def test_validate_catches_invalid_pattern(self):
        from backend.core.startup_contracts import validate_contracts_at_boot
        key = "JARVIS_STARTUP_MEMORY_MODE"
        try:
            os.environ[key] = "INVALID_MODE_VALUE"
            warnings = validate_contracts_at_boot()
            pattern_warns = [w for w in warnings if key in w and "pattern" in w.lower()]
            assert len(pattern_warns) >= 1, (
                f"Expected pattern violation warning for {key}=INVALID_MODE_VALUE"
            )
        finally:
            os.environ.pop(key, None)

    def test_validate_catches_alias_conflict(self):
        from backend.core.startup_contracts import validate_contracts_at_boot
        canonical = "JARVIS_BACKEND_PORT"
        alias = "BACKEND_PORT"
        try:
            os.environ[canonical] = "8010"
            os.environ[alias] = "9999"  # Conflict!
            warnings = validate_contracts_at_boot()
            conflict_warns = [w for w in warnings if "conflict" in w.lower()]
            assert len(conflict_warns) >= 1, (
                f"Expected alias conflict warning for {canonical}=8010 vs {alias}=9999"
            )
        finally:
            os.environ.pop(canonical, None)
            os.environ.pop(alias, None)

    def test_validate_no_warnings_when_clean(self):
        """With no env vars set, there should be no warnings."""
        from backend.core.startup_contracts import validate_contracts_at_boot
        # Clear all contracted env vars temporarily
        keys = [
            "JARVIS_STARTUP_MEMORY_MODE", "JARVIS_BACKEND_PORT",
            "BACKEND_PORT", "JARVIS_API_PORT", "JARVIS_PORT",
        ]
        saved = {}
        for k in keys:
            if k in os.environ:
                saved[k] = os.environ.pop(k)
        try:
            warnings = validate_contracts_at_boot()
            assert len(warnings) == 0, f"Unexpected warnings: {warnings}"
        finally:
            for k, v in saved.items():
                os.environ[k] = v


class TestHealthEndpointValidation:
    """Verify health endpoint schema validation."""

    def test_valid_health_response(self):
        from backend.core.startup_contracts import validate_health_response
        data = {"status": "healthy", "bridge_health": {}}
        violations = validate_health_response("/health", data)
        assert len(violations) == 0

    def test_missing_required_field(self):
        from backend.core.startup_contracts import validate_health_response
        data = {"bridge_health": {}}  # missing "status"
        violations = validate_health_response("/health", data)
        assert any("status" in v for v in violations)

    def test_wrong_type_field(self):
        from backend.core.startup_contracts import validate_health_response
        data = {"ready": "yes"}  # should be bool, not str
        violations = validate_health_response("/health/ready", data)
        assert any("ready" in v and "bool" in v for v in violations)

    def test_valid_ready_response(self):
        from backend.core.startup_contracts import validate_health_response
        data = {"ready": True, "details": {"websocket_ready": True}}
        violations = validate_health_response("/health/ready", data)
        assert len(violations) == 0

    def test_unknown_endpoint_no_violations(self):
        from backend.core.startup_contracts import validate_health_response
        violations = validate_health_response("/unknown/endpoint", {"anything": True})
        assert len(violations) == 0

    def test_prime_health_valid(self):
        from backend.core.startup_contracts import validate_health_response
        data = {"ready_for_inference": True, "status": "healthy", "model_loaded": True}
        violations = validate_health_response("prime:/health", data)
        assert len(violations) == 0

    def test_prime_health_missing_field(self):
        from backend.core.startup_contracts import validate_health_response
        data = {"status": "healthy"}  # missing ready_for_inference
        violations = validate_health_response("prime:/health", data)
        assert any("ready_for_inference" in v for v in violations)


class TestGetCanonicalEnv:
    """Verify the canonical env var accessor with alias fallback."""

    def test_returns_canonical_value(self):
        from backend.core.startup_contracts import get_canonical_env
        key = "JARVIS_BACKEND_PORT"
        try:
            os.environ[key] = "8888"
            result = get_canonical_env(key)
            assert result == "8888"
        finally:
            os.environ.pop(key, None)

    def test_falls_back_to_alias(self):
        from backend.core.startup_contracts import get_canonical_env
        canonical = "JARVIS_BACKEND_PORT"
        alias = "BACKEND_PORT"
        # Ensure canonical is NOT set
        os.environ.pop(canonical, None)
        try:
            os.environ[alias] = "7777"
            result = get_canonical_env(canonical)
            assert result == "7777"
        finally:
            os.environ.pop(alias, None)

    def test_returns_default_when_unset(self):
        from backend.core.startup_contracts import get_canonical_env
        # Clear all backend port env vars
        for k in ("JARVIS_BACKEND_PORT", "BACKEND_PORT", "JARVIS_API_PORT", "JARVIS_PORT"):
            os.environ.pop(k, None)
        result = get_canonical_env("JARVIS_BACKEND_PORT")
        assert result == "8010"  # default from contract

    def test_non_contracted_var_falls_through(self):
        from backend.core.startup_contracts import get_canonical_env
        key = "_TEST_P6_NONCONTRACTED"
        try:
            os.environ[key] = "hello"
            result = get_canonical_env(key)
            assert result == "hello"
        finally:
            os.environ.pop(key, None)


# ===========================================================================
# 3. Async task ownership — verify tracking functions exist
# ===========================================================================

class TestAsyncTaskOwnership:
    """Verify cross_repo has proper task tracking infrastructure."""

    def test_track_background_task_exists(self):
        mod = _import_module("backend.supervisor.cross_repo_startup_orchestrator")
        assert mod is not None
        # The class should have _track_background_task
        cls = getattr(mod, "CrossRepoStartupOrchestrator", None)
        if cls is not None:
            assert hasattr(cls, "_track_background_task")

    def test_stop_gcp_vm_health_monitor_exists(self):
        """Module-level stop function must exist for shutdown cleanup."""
        mod = _import_module("backend.supervisor.cross_repo_startup_orchestrator")
        assert mod is not None
        fn = getattr(mod, "stop_gcp_vm_health_monitor", None)
        assert fn is not None and callable(fn)

    def test_authority_gate_functions_exist(self):
        """Phase 5 authority gate must still be present."""
        mod = _import_module("backend.supervisor.cross_repo_startup_orchestrator")
        assert mod is not None
        for name in ("grant_supervisor_authority", "revoke_supervisor_authority",
                      "check_supervisor_authority"):
            fn = getattr(mod, name, None)
            assert fn is not None and callable(fn), f"{name} must exist"
