"""
v310.0: Tests for HeartbeatWriter and validate_heartbeat.

All tests use tempfile.TemporaryDirectory for full isolation.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import pytest

from backend.core.heartbeat_writer import HeartbeatWriter, validate_heartbeat
from backend.core.time_utils import monotonic_s


# ======================================================================
# TestHeartbeatPayload — verifies the written JSON structure
# ======================================================================


class TestHeartbeatPayload:
    """Verify heartbeat payload correctness."""

    REQUIRED_FIELDS = {
        "boot_id",
        "pid",
        "ts_mono",
        "monotonic_age_ms",
        "phase",
        "loop_iteration",
        "written_at_wall",
    }

    def test_heartbeat_payload_has_required_fields(self) -> None:
        """Write once, verify all 7 fields are present and correct."""
        with tempfile.TemporaryDirectory() as td:
            hb_path = Path(td) / "heartbeat.json"
            writer = HeartbeatWriter(path=hb_path)
            writer.write(phase="loading", loop_iteration=42)

            payload = json.loads(hb_path.read_text())

            # All required fields present
            assert set(payload.keys()) == self.REQUIRED_FIELDS

            # pid matches current process
            assert payload["pid"] == os.getpid()

            # phase and loop_iteration match inputs
            assert payload["phase"] == "loading"
            assert payload["loop_iteration"] == 42

    def test_heartbeat_boot_id_is_stable(self) -> None:
        """Two writes from the same writer produce identical boot_id."""
        with tempfile.TemporaryDirectory() as td:
            hb_path = Path(td) / "heartbeat.json"
            writer = HeartbeatWriter(path=hb_path)

            writer.write(phase="loading", loop_iteration=1)
            p1 = json.loads(hb_path.read_text())

            writer.write(phase="ready", loop_iteration=2)
            p2 = json.loads(hb_path.read_text())

            assert p1["boot_id"] == p2["boot_id"]

    def test_heartbeat_monotonic_age_measures_inter_write_delta(self) -> None:
        """monotonic_age_ms measures ms since previous write, not uptime."""
        with tempfile.TemporaryDirectory() as td:
            hb_path = Path(td) / "heartbeat.json"
            writer = HeartbeatWriter(path=hb_path)

            # First write: delta from __init__ → near-zero
            writer.write(phase="loading", loop_iteration=1)
            p1 = json.loads(hb_path.read_text())

            time.sleep(0.05)  # 50ms

            # Second write: delta from first write → ~50ms
            writer.write(phase="loading", loop_iteration=2)
            p2 = json.loads(hb_path.read_text())

            # First write should have a very small delta (init → first write)
            assert p1["monotonic_age_ms"] < 50

            # Second write should capture the ~50ms sleep
            assert p2["monotonic_age_ms"] >= 40  # allow some timing slack

    def test_heartbeat_atomic_write(self) -> None:
        """100 rapid writes each produce valid JSON with correct iteration."""
        with tempfile.TemporaryDirectory() as td:
            hb_path = Path(td) / "heartbeat.json"
            writer = HeartbeatWriter(path=hb_path)

            for i in range(100):
                writer.write(phase="loading", loop_iteration=i)
                payload = json.loads(hb_path.read_text())
                assert payload["loop_iteration"] == i

    def test_heartbeat_tmp_file_cleaned_up(self) -> None:
        """After write completes, no .tmp file remains in the directory."""
        with tempfile.TemporaryDirectory() as td:
            hb_path = Path(td) / "heartbeat.json"
            writer = HeartbeatWriter(path=hb_path)
            writer.write(phase="ready", loop_iteration=1)

            remaining = list(Path(td).glob("*.tmp"))
            assert remaining == [], f"Leftover tmp files: {remaining}"


# ======================================================================
# TestHeartbeatValidation — verifies the validate_heartbeat helper
# ======================================================================


class TestHeartbeatValidation:
    """Verify validation logic for heartbeat payloads."""

    def _make_payload(self, **overrides) -> dict:
        """Build a valid payload with optional overrides."""
        base = {
            "boot_id": "test-boot-id-1234",
            "pid": os.getpid(),
            "ts_mono": monotonic_s(),
            "monotonic_age_ms": 5000,
            "phase": "ready",
            "loop_iteration": 10,
            "written_at_wall": "2026-03-05T07:15:33",
        }
        base.update(overrides)
        return base

    def test_boot_id_mismatch_detected(self) -> None:
        """boot_id mismatch is detected and reported."""
        payload = self._make_payload(boot_id="wrong-boot-id")
        result = validate_heartbeat(
            payload, expected_boot_id="correct-boot-id"
        )
        assert result["valid"] is False
        assert "boot_id" in result["reason"]

    def test_stale_heartbeat_detected(self) -> None:
        """Heartbeat with ts_mono too far in the past should be detected as stale."""
        import uuid

        boot_id = str(uuid.uuid4())
        payload = {
            "boot_id": boot_id,
            "pid": os.getpid(),
            "ts_mono": monotonic_s() - 60.0,  # 60 seconds ago
            "monotonic_age_ms": 100,
            "phase": "ready",
            "loop_iteration": 10,
        }
        result = validate_heartbeat(
            payload, expected_boot_id=boot_id, max_age_s=30.0
        )
        assert result["valid"] is False
        assert "stale" in result["reason"]

    def test_pid_mismatch_detected(self) -> None:
        """A non-existent pid is detected and reported."""
        payload = self._make_payload(pid=99999999)
        result = validate_heartbeat(payload)
        assert result["valid"] is False
        assert "pid" in result["reason"]

    def test_valid_heartbeat_accepted(self) -> None:
        """Matching boot_id and pid yields valid=True."""
        boot = "my-boot-id"
        payload = self._make_payload(boot_id=boot)
        result = validate_heartbeat(
            payload,
            expected_boot_id=boot,
            expected_pid=os.getpid(),
        )
        assert result["valid"] is True
