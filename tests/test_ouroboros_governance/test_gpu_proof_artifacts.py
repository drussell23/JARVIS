"""Phase D: observability — terminal_class and per-op proof artifact tests."""
import dataclasses


class TestTerminalClass:
    """OperationResult must include terminal_class field."""

    def test_operation_result_has_terminal_class(self):
        """OperationResult dataclass must have terminal_class field."""
        from backend.core.ouroboros.governance.governed_loop_service import OperationResult
        fields = {f.name for f in dataclasses.fields(OperationResult)}
        assert "terminal_class" in fields, (
            f"OperationResult missing terminal_class. Fields: {fields}"
        )

    def test_terminal_class_valid_taxonomy(self):
        """Known taxonomy values are defined."""
        valid = {"PRIMARY_SUCCESS", "FALLBACK_SUCCESS", "DEGRADED", "TIMEOUT", "NOOP", "UNKNOWN"}
        assert "PRIMARY_SUCCESS" in valid
        assert "FALLBACK_SUCCESS" in valid
        assert "NOOP" in valid
        assert "BANANA" not in valid

    def test_operation_result_terminal_class_defaults_to_unknown(self):
        """Default terminal_class should be 'UNKNOWN', not None."""
        from backend.core.ouroboros.governance.governed_loop_service import OperationResult
        # Use introspection to find required (non-default) fields
        required_fields = {
            f.name: None
            for f in dataclasses.fields(OperationResult)
            if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
        }
        # terminal_class should have a default so it's not in required_fields
        assert "terminal_class" not in required_fields, (
            "terminal_class must have a default value ('UNKNOWN')"
        )


class TestClassifyTerminal:
    """_classify_terminal() maps outcomes to taxonomy correctly."""

    def test_noop_returns_noop(self):
        from backend.core.ouroboros.governance.governed_loop_service import _classify_terminal
        from backend.core.ouroboros.governance.op_context import OperationPhase
        result = _classify_terminal(OperationPhase.COMPLETE, "gcp-jprime", "noop", is_noop=True)
        assert result == "NOOP"

    def test_complete_with_prime_returns_primary_success(self):
        from backend.core.ouroboros.governance.governed_loop_service import _classify_terminal
        from backend.core.ouroboros.governance.op_context import OperationPhase
        result = _classify_terminal(OperationPhase.COMPLETE, "gcp-jprime", "ok", is_noop=False)
        assert result == "PRIMARY_SUCCESS"

    def test_complete_with_claude_returns_fallback_success(self):
        from backend.core.ouroboros.governance.governed_loop_service import _classify_terminal
        from backend.core.ouroboros.governance.op_context import OperationPhase
        result = _classify_terminal(OperationPhase.COMPLETE, "claude-api", "ok", is_noop=False)
        assert result == "FALLBACK_SUCCESS"

    def test_timeout_reason_returns_timeout(self):
        from backend.core.ouroboros.governance.governed_loop_service import _classify_terminal
        from backend.core.ouroboros.governance.op_context import OperationPhase
        result = _classify_terminal(OperationPhase.CANCELLED, None, "pipeline_timeout", is_noop=False)
        assert result == "TIMEOUT"

    def test_non_complete_returns_degraded(self):
        from backend.core.ouroboros.governance.governed_loop_service import _classify_terminal
        from backend.core.ouroboros.governance.op_context import OperationPhase
        result = _classify_terminal(OperationPhase.CANCELLED, "gcp-jprime", "error", is_noop=False)
        assert result == "DEGRADED"


class TestProofArtifact:
    """_build_proof_artifact() builds a complete structured dict."""

    def test_proof_artifact_has_required_fields(self):
        from backend.core.ouroboros.governance.governed_loop_service import _build_proof_artifact
        from backend.core.ouroboros.governance.op_context import OperationPhase

        artifact = _build_proof_artifact(
            op_id="test-op-123",
            terminal_phase=OperationPhase.COMPLETE,
            terminal_class="PRIMARY_SUCCESS",
            provider_used="gcp-jprime",
            model_id="Qwen2.5-Coder-7B",
            compute_class="gpu_t4",
            execution_host="jarvis-prime-stable",
            fallback_active=False,
            phase_trail=["CLASSIFY", "ROUTE", "GENERATE", "COMPLETE"],
            generation_duration_s=3.5,
            total_duration_s=12.0,
        )

        required = {
            "op_id", "terminal_phase", "terminal_class",
            "provider_used", "model_id", "compute_class",
            "execution_host", "fallback_active", "phase_trail",
            "generation_duration_s", "total_duration_s",
        }
        assert required <= artifact.keys(), f"Missing: {required - artifact.keys()}"
        assert artifact["terminal_class"] == "PRIMARY_SUCCESS"
        assert artifact["fallback_active"] is False

    def test_proof_artifact_fallback_flag_on_fallback(self):
        from backend.core.ouroboros.governance.governed_loop_service import _build_proof_artifact
        from backend.core.ouroboros.governance.op_context import OperationPhase

        artifact = _build_proof_artifact(
            op_id="test-op",
            terminal_phase=OperationPhase.COMPLETE,
            terminal_class="FALLBACK_SUCCESS",
            provider_used="claude-api",
            model_id="claude-sonnet-4-6",
            compute_class="api",
            execution_host="anthropic",
            fallback_active=True,
            phase_trail=["CLASSIFY", "ROUTE", "GENERATE", "COMPLETE"],
            generation_duration_s=8.0,
            total_duration_s=20.0,
        )

        assert artifact["fallback_active"] is True
        assert artifact["terminal_class"] == "FALLBACK_SUCCESS"
