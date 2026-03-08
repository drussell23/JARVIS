"""tests/governance/comms/test_ops_logger.py"""
import time
import pytest
from pathlib import Path
from unittest.mock import MagicMock


def _make_comm_message(msg_type, op_id="op-001", payload=None):
    from backend.core.ouroboros.governance.comm_protocol import CommMessage, MessageType
    return CommMessage(
        msg_type=MessageType[msg_type] if isinstance(msg_type, str) else msg_type,
        op_id=op_id,
        seq=1,
        causal_parent_seq=None,
        payload=payload or {},
        timestamp=time.time(),
    )


class TestOpsLoggerSend:
    @pytest.mark.asyncio
    async def test_writes_intent_to_log_file(self, tmp_path):
        from backend.core.ouroboros.governance.comms.ops_logger import OpsLogger

        logger = OpsLogger(log_dir=tmp_path)
        msg = _make_comm_message("INTENT", payload={
            "goal": "fix test_edge_case",
            "target_files": ["tests/test_utils.py"],
            "risk_tier": "SAFE_AUTO",
        })
        await logger.send(msg)

        log_files = list(tmp_path.glob("*.log"))
        assert len(log_files) == 1
        content = log_files[0].read_text()
        assert "INTENT" in content
        assert "op-001" in content
        assert "test_utils.py" in content

    @pytest.mark.asyncio
    async def test_writes_decision_to_log_file(self, tmp_path):
        from backend.core.ouroboros.governance.comms.ops_logger import OpsLogger

        logger = OpsLogger(log_dir=tmp_path)
        msg = _make_comm_message("DECISION", payload={
            "outcome": "applied",
            "reason_code": "tests_pass",
        })
        await logger.send(msg)

        log_files = list(tmp_path.glob("*.log"))
        content = log_files[0].read_text()
        assert "DECISION" in content
        assert "applied" in content

    @pytest.mark.asyncio
    async def test_appends_multiple_entries(self, tmp_path):
        from backend.core.ouroboros.governance.comms.ops_logger import OpsLogger

        logger = OpsLogger(log_dir=tmp_path)
        msg1 = _make_comm_message("INTENT", op_id="op-001", payload={"goal": "fix a"})
        msg2 = _make_comm_message("DECISION", op_id="op-001", payload={"outcome": "applied"})
        await logger.send(msg1)
        await logger.send(msg2)

        log_files = list(tmp_path.glob("*.log"))
        assert len(log_files) == 1  # same day = same file
        content = log_files[0].read_text()
        assert "INTENT" in content
        assert "DECISION" in content


class TestOpsLoggerFormat:
    @pytest.mark.asyncio
    async def test_log_entry_has_timestamp(self, tmp_path):
        from backend.core.ouroboros.governance.comms.ops_logger import OpsLogger

        logger = OpsLogger(log_dir=tmp_path)
        msg = _make_comm_message("INTENT", payload={"goal": "test"})
        await logger.send(msg)

        content = list(tmp_path.glob("*.log"))[0].read_text()
        # Should have a timestamp like [2026-03-07 14:23:01]
        assert "[20" in content  # starts with year


class TestOpsLoggerRetention:
    @pytest.mark.asyncio
    async def test_cleanup_old_logs(self, tmp_path):
        from backend.core.ouroboros.governance.comms.ops_logger import OpsLogger
        import os

        # Create a fake old log file
        old_log = tmp_path / "2020-01-01-ops.log"
        old_log.write_text("old data")
        # Set mtime to the past
        old_time = time.time() - (40 * 86400)  # 40 days ago
        os.utime(old_log, (old_time, old_time))

        logger = OpsLogger(log_dir=tmp_path, retention_days=30)
        await logger.cleanup_old_logs()

        assert not old_log.exists()

    @pytest.mark.asyncio
    async def test_keeps_recent_logs(self, tmp_path):
        from backend.core.ouroboros.governance.comms.ops_logger import OpsLogger

        recent_log = tmp_path / "2026-03-07-ops.log"
        recent_log.write_text("recent data")

        logger = OpsLogger(log_dir=tmp_path, retention_days=30)
        await logger.cleanup_old_logs()

        assert recent_log.exists()


class TestOpsLoggerFailure:
    @pytest.mark.asyncio
    async def test_write_failure_does_not_propagate(self, tmp_path):
        from backend.core.ouroboros.governance.comms.ops_logger import OpsLogger

        # Point to a non-writable directory
        logger = OpsLogger(log_dir=tmp_path / "nonexistent" / "nested")
        msg = _make_comm_message("INTENT", payload={"goal": "test"})
        await logger.send(msg)  # should not raise
