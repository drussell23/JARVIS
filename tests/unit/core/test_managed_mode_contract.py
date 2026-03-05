# tests/unit/core/test_managed_mode_contract.py
"""
Golden Contract Conformance Tests v1.0.0
==========================================
These tests validate the managed-mode contract shape.
IDENTICAL copies must exist in:
  - JARVIS-AI-Agent/tests/unit/core/test_managed_mode_contract.py
  - jarvis-prime/tests/test_managed_mode_contract.py
  - reactor-core/tests/test_managed_mode_contract.py

CI drift check: compare file hashes across repos.
"""
import pytest

EXPECTED_SCHEMA_VERSION = "1.0.0"

REQUIRED_HEALTH_FIELDS = {
    "liveness", "readiness", "session_id", "pid", "start_time_ns",
    "exec_fingerprint", "subsystem_role", "schema_version",
    "capability_hash", "observed_at_ns", "wall_time_utc",
}

VALID_LIVENESS = {"up", "down"}
VALID_READINESS = {"ready", "not_ready", "degraded", "draining"}

EXIT_CODE_RANGES = {
    "clean": (0,),
    "config_contract": tuple(range(100, 110)),
    "dependency": tuple(range(200, 210)),
    "runtime_fatal": tuple(range(300, 310)),
}


class TestContractShape:
    """Validates the contract field names and value domains."""

    def test_schema_version_matches(self):
        from backend.core.managed_mode import SCHEMA_VERSION
        assert SCHEMA_VERSION == EXPECTED_SCHEMA_VERSION

    def test_health_envelope_has_required_fields(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ROOT_SESSION_ID", "test-session")
        monkeypatch.setenv("JARVIS_SUBSYSTEM_ROLE", "test-role")
        from backend.core.managed_mode import build_health_envelope
        result = build_health_envelope({}, readiness="ready")
        missing = REQUIRED_HEALTH_FIELDS - set(result.keys())
        assert not missing, f"Missing required fields: {missing}"

    def test_liveness_values(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ROOT_SESSION_ID", "test")
        monkeypatch.setenv("JARVIS_SUBSYSTEM_ROLE", "test")
        from backend.core.managed_mode import build_health_envelope
        result = build_health_envelope({}, readiness="ready")
        assert result["liveness"] in VALID_LIVENESS

    def test_readiness_values(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ROOT_SESSION_ID", "test")
        monkeypatch.setenv("JARVIS_SUBSYSTEM_ROLE", "test")
        from backend.core.managed_mode import build_health_envelope
        for r in VALID_READINESS:
            result = build_health_envelope({}, readiness=r)
            assert result["readiness"] in VALID_READINESS


class TestExitCodeContract:
    """Validates exit code constants match the contract."""

    def test_clean_exit(self):
        from backend.core.managed_mode import EXIT_CLEAN
        assert EXIT_CLEAN in EXIT_CODE_RANGES["clean"]

    def test_config_error_exit(self):
        from backend.core.managed_mode import EXIT_CONFIG_ERROR, EXIT_CONTRACT_MISMATCH
        assert EXIT_CONFIG_ERROR in EXIT_CODE_RANGES["config_contract"]
        assert EXIT_CONTRACT_MISMATCH in EXIT_CODE_RANGES["config_contract"]

    def test_dependency_failure_exit(self):
        from backend.core.managed_mode import EXIT_DEPENDENCY_FAILURE
        assert EXIT_DEPENDENCY_FAILURE in EXIT_CODE_RANGES["dependency"]

    def test_runtime_fatal_exit(self):
        from backend.core.managed_mode import EXIT_RUNTIME_FATAL
        assert EXIT_RUNTIME_FATAL in EXIT_CODE_RANGES["runtime_fatal"]


class TestHMACContract:
    """Validates HMAC auth round-trip."""

    def test_build_verify_roundtrip(self):
        from backend.core.managed_mode import build_hmac_auth, verify_hmac_auth
        header = build_hmac_auth("sess-1", "secret-abc")
        assert verify_hmac_auth(header, "sess-1", "secret-abc")

    def test_session_mismatch_rejected(self):
        from backend.core.managed_mode import build_hmac_auth, verify_hmac_auth
        header = build_hmac_auth("sess-1", "secret-abc")
        assert not verify_hmac_auth(header, "sess-2", "secret-abc")
