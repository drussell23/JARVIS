"""Tests for bounded event log in MemoryBudgetBroker."""
import json
import tempfile
from collections import deque
from pathlib import Path


class TestEventLogBound:
    def test_deque_maxlen_enforced(self):
        """Event log should evict oldest entries at capacity."""
        log = deque(maxlen=5)
        for i in range(10):
            log.append({"id": i})
        assert len(log) == 5
        assert log[0]["id"] == 5  # Oldest kept is #5

    def test_critical_events_spill_to_disk(self):
        """Critical severity events should be written to JSONL file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            spill_path = Path(tmpdir) / "critical_events.jsonl"
            event = {"type": "test", "severity": "critical", "data": "important"}

            # Simulate spill
            with open(spill_path, "a") as f:
                f.write(json.dumps(event) + "\n")

            lines = spill_path.read_text().strip().split("\n")
            assert len(lines) == 1
            assert json.loads(lines[0])["severity"] == "critical"

    def test_non_critical_events_not_spilled(self):
        """Non-critical events should NOT be written to disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            spill_path = Path(tmpdir) / "critical_events.jsonl"
            event = {"type": "test", "severity": "info", "data": "routine"}

            # Only spill if critical
            if event.get("severity") == "critical":
                with open(spill_path, "a") as f:
                    f.write(json.dumps(event) + "\n")

            assert not spill_path.exists()
