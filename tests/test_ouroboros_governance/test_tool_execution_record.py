from __future__ import annotations
import pytest

class TestToolExecutionRecord:
    def test_execution_record_shape(self):
        from backend.core.ouroboros.governance.tool_executor import (
            ToolExecutionRecord, ToolExecStatus,
        )
        rec = ToolExecutionRecord(
            schema_version="tool.exec.v1",
            op_id="op-abc",
            call_id="op-abc:r0:read_file",
            round_index=0,
            tool_name="read_file",
            tool_version="1.0",
            arguments_hash="deadbeef",
            repo="jarvis",
            policy_decision="allow",
            policy_reason_code="",
            started_at_ns=1_000_000,
            ended_at_ns=2_000_000,
            duration_ms=1.0,
            output_bytes=42,
            error_class=None,
            status=ToolExecStatus.SUCCESS,
        )
        assert rec.schema_version == "tool.exec.v1"
        assert rec.call_id == "op-abc:r0:read_file"
        assert rec.status == ToolExecStatus.SUCCESS

    def test_tool_exec_status_values(self):
        from backend.core.ouroboros.governance.tool_executor import ToolExecStatus
        assert ToolExecStatus.SUCCESS.value == "success"
        assert ToolExecStatus.TIMEOUT.value == "timeout"
        assert ToolExecStatus.POLICY_DENIED.value == "policy_denied"
        assert ToolExecStatus.EXEC_ERROR.value == "exec_error"
        assert ToolExecStatus.CANCELLED.value == "cancelled"

class TestComputeArgsHash:
    def test_arguments_hash_deterministic_ordering(self):
        from backend.core.ouroboros.governance.tool_executor import _compute_args_hash
        assert _compute_args_hash({"b": 2, "a": 1}) == _compute_args_hash({"a": 1, "b": 2})

    def test_arguments_hash_is_sha256_hex(self):
        from backend.core.ouroboros.governance.tool_executor import _compute_args_hash
        result = _compute_args_hash({"path": "src/foo.py"})
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_different_args_produce_different_hash(self):
        from backend.core.ouroboros.governance.tool_executor import _compute_args_hash
        assert _compute_args_hash({"a": 1}) != _compute_args_hash({"a": 2})
