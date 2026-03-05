"""Tests for HealthVerdict enum and _ping_health_endpoint refactor.

Validates:
1. HealthVerdict enum exists with correct values
2. _ping_health_endpoint references HealthVerdict and does NOT return bare True/False
"""

import ast
import textwrap
from enum import Enum

import pytest


class TestHealthVerdictEnum:
    """Verify HealthVerdict enum exists with correct members and values."""

    def test_enum_importable(self):
        from backend.core.gcp_vm_manager import HealthVerdict
        assert issubclass(HealthVerdict, Enum)

    def test_enum_members(self):
        from backend.core.gcp_vm_manager import HealthVerdict
        assert HealthVerdict.READY.value == "ready"
        assert HealthVerdict.ALIVE_NOT_READY.value == "alive_not_ready"
        assert HealthVerdict.UNREACHABLE.value == "unreachable"
        assert HealthVerdict.UNHEALTHY.value == "unhealthy"

    def test_enum_has_exactly_four_members(self):
        from backend.core.gcp_vm_manager import HealthVerdict
        assert len(HealthVerdict) == 4


class TestPingHealthEndpointAST:
    """AST-based tests: _ping_health_endpoint references HealthVerdict,
    does NOT contain 'return True,' or 'return False,' patterns."""

    @pytest.fixture(autouse=True)
    def _parse_source(self):
        """Parse gcp_vm_manager.py and extract _ping_health_endpoint body."""
        import inspect
        import backend.core.gcp_vm_manager as mod

        cls = None
        # Find the class that owns _ping_health_endpoint
        for name, obj in inspect.getmembers(mod, inspect.isclass):
            if hasattr(obj, "_ping_health_endpoint"):
                cls = obj
                break
        assert cls is not None, "_ping_health_endpoint not found on any class"

        source = inspect.getsource(cls._ping_health_endpoint)
        # Dedent so ast.parse works on extracted method source
        source = textwrap.dedent(source)
        self.tree = ast.parse(source)
        self.source = source

    def test_references_health_verdict(self):
        """_ping_health_endpoint must reference HealthVerdict in its body."""
        names = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Attribute):
                if isinstance(node.value, ast.Name) and node.value.id == "HealthVerdict":
                    names.add(node.attr)
            elif isinstance(node, ast.Name):
                names.add(node.id)
        assert "HealthVerdict" in names or len(names & {"READY", "ALIVE_NOT_READY", "UNREACHABLE", "UNHEALTHY"}) > 0, (
            "_ping_health_endpoint does not reference HealthVerdict"
        )

    def test_no_bare_return_true(self):
        """_ping_health_endpoint must NOT contain 'return True,' pattern."""
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
                elts = node.value.elts
                if elts and isinstance(elts[0], ast.Constant) and elts[0].value is True:
                    pytest.fail(
                        "_ping_health_endpoint contains 'return True, ...' — "
                        "must use HealthVerdict instead"
                    )

    def test_no_bare_return_false(self):
        """_ping_health_endpoint must NOT contain 'return False,' pattern."""
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
                elts = node.value.elts
                if elts and isinstance(elts[0], ast.Constant) and elts[0].value is False:
                    pytest.fail(
                        "_ping_health_endpoint contains 'return False, ...' — "
                        "must use HealthVerdict instead"
                    )


class TestContractHashCheck:
    def test_startup_script_computes_contract_hash(self):
        """Startup script must compute and include contract_hash."""
        from backend.core.gcp_vm_manager import VMManagerConfig, GCPVMManager
        mgr = GCPVMManager.__new__(GCPVMManager)
        mgr.config = VMManagerConfig()
        script = mgr._generate_golden_startup_script()
        assert "CONTRACT_HASH=" in script
        assert "contract_hash" in script

    def test_ping_health_logs_contract_mismatch(self):
        """AST check: _ping_health_endpoint must reference contract_hash."""
        import ast
        from pathlib import Path
        src = Path("backend/core/gcp_vm_manager.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ping_health_endpoint":
                func_src = ast.get_source_segment(src, node)
                assert "contract_hash" in func_src, \
                    "_ping_health_endpoint must check contract_hash"
                break


class TestReadinessHysteresis:
    def test_config_has_hysteresis_fields(self):
        """VMManagerConfig must have hysteresis configuration."""
        from backend.core.gcp_vm_manager import VMManagerConfig
        config = VMManagerConfig()
        assert hasattr(config, "readiness_hysteresis_up")
        assert hasattr(config, "readiness_hysteresis_down")
        assert config.readiness_hysteresis_up >= 2
        assert config.readiness_hysteresis_down >= 1

    def test_hysteresis_default_values(self):
        """Default hysteresis: up=3, down=2."""
        from backend.core.gcp_vm_manager import VMManagerConfig
        config = VMManagerConfig()
        assert config.readiness_hysteresis_up == 3
        assert config.readiness_hysteresis_down == 2


class TestTimeoutPolicyTiering:
    def test_timeout_profiles_exist(self):
        """VMManagerConfig must support GCP_TIMEOUT_PROFILE."""
        from backend.core.gcp_vm_manager import VMManagerConfig, TIMEOUT_PROFILES
        assert "dev" in TIMEOUT_PROFILES
        assert "staging" in TIMEOUT_PROFILES
        assert "production" in TIMEOUT_PROFILES
        assert "golden_image" in TIMEOUT_PROFILES

    def test_timeout_profile_values(self):
        from backend.core.gcp_vm_manager import TIMEOUT_PROFILES
        assert TIMEOUT_PROFILES["dev"] == 30.0
        assert TIMEOUT_PROFILES["staging"] == 60.0
        assert TIMEOUT_PROFILES["production"] == 90.0
        assert TIMEOUT_PROFILES["golden_image"] == 120.0

    def test_explicit_timeout_overrides_profile(self):
        """Explicit GCP_SERVICE_HEALTH_TIMEOUT overrides profile."""
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {
            "GCP_TIMEOUT_PROFILE": "dev",
            "GCP_SERVICE_HEALTH_TIMEOUT": "200.0",
        }):
            from backend.core.gcp_vm_manager import VMManagerConfig
            config = VMManagerConfig()
            assert config.service_health_timeout == 200.0


class TestCorrelationIdPropagation:
    def test_ping_health_sends_correlation_header(self):
        """AST check: _ping_health_endpoint must send X-Correlation-ID."""
        import ast
        from pathlib import Path
        src = Path("backend/core/gcp_vm_manager.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ping_health_endpoint":
                func_src = ast.get_source_segment(src, node)
                assert "X-Correlation-ID" in func_src or "correlation_id" in func_src, \
                    "_ping_health_endpoint must propagate correlation ID"
                break
