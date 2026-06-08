"""Slice 177 — autonomous workspace hygiene (the artifact janitor).

The organism accumulates ~20GB of untracked bloat (session logs, stray checkpoints,
caches). The .dockerignore stopped it polluting the build context, but that's a deployment
band-aid — this is the root-cause cleanup: a janitor that compresses aging logs and prunes
ancient artifacts, on a strict age policy, WITHOUT ever touching an active FSM target or any
file outside its whitelisted artifact directories.
"""
from __future__ import annotations

import gzip
import os
import time
import unittest
import tempfile

from backend.core.ouroboros.governance.artifact_janitor import ArtifactJanitor

_DAY = 86400.0


def _touch(path, *, age_days, content=b"x" * 1024):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(content)
    mt = time.time() - age_days * _DAY
    os.utime(path, (mt, mt))
    return path


class TestRotationPolicy(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.logs = os.path.join(self.root, ".ouroboros")
        self.now = time.time()

    def _janitor(self, **kw):
        return ArtifactJanitor(
            scan_dirs=[self.logs], compress_age_days=7, delete_age_days=30, **kw
        )

    def test_old_log_compressed(self):
        f = _touch(os.path.join(self.logs, "s1", "debug.log"), age_days=10)  # >7, <30
        rep = self._janitor().sweep(now=self.now)
        self.assertFalse(os.path.exists(f))                 # original gone
        self.assertTrue(os.path.exists(f + ".gz"))          # compressed
        self.assertGreaterEqual(rep["compressed"], 1)
        with gzip.open(f + ".gz", "rb") as g:               # content preserved
            self.assertEqual(g.read(), b"x" * 1024)

    def test_ancient_file_deleted(self):
        f = _touch(os.path.join(self.logs, "old", "debug.log"), age_days=45)  # >30
        rep = self._janitor().sweep(now=self.now)
        self.assertFalse(os.path.exists(f))
        self.assertFalse(os.path.exists(f + ".gz"))         # deleted, not compressed
        self.assertGreaterEqual(rep["deleted"], 1)

    def test_recent_active_file_untouched(self):
        f = _touch(os.path.join(self.logs, "live", "debug.log"), age_days=1)  # <7 → fresh
        self._janitor().sweep(now=self.now)
        self.assertTrue(os.path.exists(f))                  # the active session is SAFE
        self.assertFalse(os.path.exists(f + ".gz"))

    def test_protected_path_never_touched(self):
        f = _touch(os.path.join(self.logs, "current", "debug.log"), age_days=99)  # ancient
        # but it's the ACTIVE session → protected → must survive even though ancient
        self._janitor(protect_paths=[os.path.join(self.logs, "current")]).sweep(now=self.now)
        self.assertTrue(os.path.exists(f))

    def test_already_compressed_not_recompressed(self):
        f = _touch(os.path.join(self.logs, "s2", "old.log.gz"), age_days=10)
        rep = self._janitor().sweep(now=self.now)
        self.assertTrue(os.path.exists(f))                  # left alone (already .gz, <30d)
        self.assertEqual(rep["compressed"], 0)

    def test_never_scans_outside_whitelist(self):
        # a file OUTSIDE the scan dirs (e.g. source) is never touched even if ancient
        src = _touch(os.path.join(self.root, "backend", "core", "x.py"), age_days=99)
        self._janitor().sweep(now=self.now)
        self.assertTrue(os.path.exists(src))

    def test_disabled_by_default(self):
        import backend.core.ouroboros.governance.artifact_janitor as J
        os.environ.pop("JARVIS_ARTIFACT_JANITOR_ENABLED", None)
        self.assertFalse(J.artifact_janitor_enabled())

    def test_report_totals(self):
        _touch(os.path.join(self.logs, "a", "debug.log"), age_days=10)
        _touch(os.path.join(self.logs, "b", "debug.log"), age_days=45)
        rep = self._janitor().sweep(now=self.now)
        self.assertEqual(rep["compressed"], 1)
        self.assertEqual(rep["deleted"], 1)
        self.assertGreater(rep["freed_bytes"], 0)

    def test_never_raises_on_missing_dir(self):
        j = ArtifactJanitor(scan_dirs=["/nonexistent/path/xyz"], compress_age_days=7, delete_age_days=30)
        self.assertEqual(j.sweep(now=self.now)["compressed"], 0)  # no crash


class TestGLSWiring(unittest.TestCase):
    def _gls_src(self):
        import importlib.util
        spec = importlib.util.find_spec(
            "backend.core.ouroboros.governance.governed_loop_service"
        )
        with open(spec.origin) as fh:
            return fh.read()

    def test_janitor_wired_deferred_offthread_gated(self):
        src = self._gls_src()
        self.assertIn("artifact_janitor_enabled", src)        # gated
        self.assertIn("to_thread", src)                       # off the event loop (no GIL stall)
        self.assertIn("create_task", src)                     # deferred, doesn't block boot
        self.assertIn("protect_paths", src)                   # active session protected
        # the sweep must be guarded by the master flag
        i_gate = src.find("artifact_janitor_enabled()")
        i_task = src.find("_janitor_boot_sweep")
        self.assertTrue(0 < i_gate < i_task)


if __name__ == "__main__":
    unittest.main()
