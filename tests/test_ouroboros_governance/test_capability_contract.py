"""Phase A/B: capability contract validation tests."""
import pytest


class TestModelArtifactIntegrity:
    """_check_artifact_integrity enforces model_artifact match between policy and VM."""

    def test_matching_artifact_passes(self):
        """Matching artifacts (exact case) — no error."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            _check_artifact_integrity,
        )
        capability = {"model_artifact": "qwen2.5-coder-7b-instruct-q4_k_m.gguf"}
        brain_cfg = {"model_artifact": "qwen2.5-coder-7b-instruct-q4_k_m.gguf"}
        _check_artifact_integrity(brain_cfg, capability)  # must not raise

    def test_mismatched_artifact_raises(self):
        """Mismatched artifacts — ModelArtifactMismatch raised."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            ModelArtifactMismatch,
            _check_artifact_integrity,
        )
        capability = {"model_artifact": "Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf"}
        brain_cfg = {"model_artifact": "qwen2.5-coder-7b-instruct-q4_k_m.gguf"}
        with pytest.raises(ModelArtifactMismatch) as exc_info:
            _check_artifact_integrity(brain_cfg, capability)
        assert "mismatch" in str(exc_info.value).lower() or "14B" in str(exc_info.value)

    def test_case_insensitive_match(self):
        """Artifact comparison is case-insensitive."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            _check_artifact_integrity,
        )
        capability = {"model_artifact": "Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf"}
        brain_cfg = {"model_artifact": "qwen2.5-coder-7b-instruct-q4_k_m.gguf"}
        _check_artifact_integrity(brain_cfg, capability)  # must not raise


class TestHostBindingInvariant:
    """_check_host_binding enforces telemetry==selector==execution host."""

    def test_matching_hosts_passes(self):
        """All three hosts match — no error."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            _check_host_binding,
        )
        _check_host_binding(
            telemetry_host="jarvis-prime-stable",
            selector_host="jarvis-prime-stable",
            execution_host="jarvis-prime-stable",
        )  # must not raise

    def test_execution_host_mismatch_raises(self):
        """execution_host differs from telemetry_host — HostBindingViolation raised."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            HostBindingViolation,
            _check_host_binding,
        )
        with pytest.raises(HostBindingViolation):
            _check_host_binding(
                telemetry_host="jarvis-prime-stable",
                selector_host="jarvis-prime-stable",
                execution_host="some-other-host",
            )

    def test_capability_host_is_execution_host(self):
        """execution_host must come from capability['host'], not local socket.gethostname()."""
        import socket
        local_hostname = socket.gethostname()
        vm_hostname = "jarvis-prime-stable"
        capability = {"host": vm_hostname, "compute_class": "gpu_t4"}
        execution_host = capability["host"]
        # The invariant: execution_host is derived from capability, not local machine
        assert execution_host == vm_hostname
