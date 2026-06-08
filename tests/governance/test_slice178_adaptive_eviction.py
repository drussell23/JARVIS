"""Slice 178 — adaptive (volume-aware) eviction guard.

The Slice-177 janitor deleted purely by calendar age — risking premature forensic
deletion when disk is plentiful. This makes it STRUCTURALLY AWARE: it only runs a
destructive sweep when the storage volume crosses a scarcity threshold (default 85%).
Below that, forensic logs are left FULLY INTACT regardless of age. When scarcity IS
breached, it evicts by the age policy and emits a MAINTENANCE_EVICTION for the Discord
spine.
"""
from __future__ import annotations

import os
import time
import unittest
import tempfile

from backend.core.ouroboros.governance.artifact_janitor import (
    ArtifactJanitor,
    render_maintenance_eviction,
    emit_maintenance_eviction,
)

_DAY = 86400.0


def _touch(path, *, age_days):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"x" * 4096)
    mt = time.time() - age_days * _DAY
    os.utime(path, (mt, mt))
    return path


class TestScarcityGate(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.logs = os.path.join(self.root, ".ouroboros")
        self.now = time.time()

    def _jan(self, usage):
        return ArtifactJanitor(
            scan_dirs=[self.logs], compress_age_days=7, delete_age_days=30,
            scarcity_threshold=0.85, usage_probe=lambda: usage,
        )

    def test_below_threshold_leaves_ancient_logs_intact(self):
        ancient = _touch(os.path.join(self.logs, "old", "debug.log"), age_days=99)  # would normally be deleted
        rep = self._jan(usage=0.40).sweep(now=self.now)  # 40% — plenty of space
        self.assertFalse(rep["evicted"])
        self.assertEqual(rep["deleted"], 0)
        self.assertEqual(rep["compressed"], 0)
        self.assertTrue(os.path.exists(ancient))          # forensic log PRESERVED
        self.assertAlmostEqual(rep["usage_ratio"], 0.40)

    def test_above_threshold_evicts_by_age(self):
        old = _touch(os.path.join(self.logs, "a", "debug.log"), age_days=10)   # >7 → compress
        ancient = _touch(os.path.join(self.logs, "b", "debug.log"), age_days=45)  # >30 → delete
        rep = self._jan(usage=0.90).sweep(now=self.now)   # 90% — scarce
        self.assertTrue(rep["evicted"])
        self.assertEqual(rep["compressed"], 1)
        self.assertEqual(rep["deleted"], 1)
        self.assertFalse(os.path.exists(ancient))
        self.assertTrue(os.path.exists(old + ".gz"))

    def test_exactly_at_threshold_evicts(self):
        _touch(os.path.join(self.logs, "a", "debug.log"), age_days=45)
        rep = self._jan(usage=0.85).sweep(now=self.now)   # == threshold → evict
        self.assertTrue(rep["evicted"])

    def test_force_overrides_scarcity_gate(self):
        _touch(os.path.join(self.logs, "a", "debug.log"), age_days=45)
        rep = self._jan(usage=0.10).sweep(now=self.now, force=True)
        self.assertTrue(rep["evicted"])
        self.assertEqual(rep["deleted"], 1)

    def test_report_carries_usage_and_threshold(self):
        rep = self._jan(usage=0.50).sweep(now=self.now)
        self.assertIn("usage_ratio", rep)
        self.assertIn("scarcity_threshold", rep)
        self.assertEqual(rep["scarcity_threshold"], 0.85)


class TestDiskSensor(unittest.TestCase):
    def test_real_disk_usage_ratio_in_range(self):
        j = ArtifactJanitor(scan_dirs=["."], volume_path=".")
        r = j.disk_usage_ratio()
        self.assertGreaterEqual(r, 0.0)
        self.assertLessEqual(r, 1.0)

    def test_usage_probe_injection(self):
        j = ArtifactJanitor(scan_dirs=["."], usage_probe=lambda: 0.73)
        self.assertAlmostEqual(j.disk_usage_ratio(), 0.73)

    def test_never_raises_on_bad_volume(self):
        j = ArtifactJanitor(scan_dirs=["."], volume_path="/nonexistent/vol/xyz")
        self.assertIsInstance(j.disk_usage_ratio(), float)


class TestRender(unittest.TestCase):
    def test_eviction_message_format(self):
        msg = render_maintenance_eviction(0.85, 4_200_000_000)
        self.assertIn("85%", msg)
        self.assertIn("4.2GB", msg)
        self.assertIn("Janitor", msg)


class TestEmit(unittest.TestCase):
    def test_emit_calls_poster_with_message(self):
        captured = []
        ok = emit_maintenance_eviction(0.90, 4_200_000_000, poster=captured.append)
        self.assertTrue(ok)
        self.assertEqual(len(captured), 1)
        self.assertIn("90%", captured[0])
        self.assertIn("4.2GB", captured[0])

    def test_emit_noop_without_webhook(self):
        os.environ.pop("JARVIS_DISCORD_MAINTENANCE_WEBHOOK", None)
        os.environ.pop("JARVIS_DISCORD_SPINE_WEBHOOK", None)
        self.assertFalse(emit_maintenance_eviction(0.9, 1e9))  # no webhook → no-op, no crash


if __name__ == "__main__":
    unittest.main()
