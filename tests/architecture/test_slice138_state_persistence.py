"""Slice 138 — Autonomous State Persistence (the Evidence Vault).

Continuously backs up the ``.jarvis/`` directory (dissertation_evidence.jsonl +
episodic_memory.jsonl + signed roadmap) to a remote target so the organism's
memory + evidence chain survive host-death — no manual backups.

Pluggable backend (rsync / s3 / git), gated default-FALSE, async + fail-soft
(a backup failure must never crash the soak). The shell runner is injectable so
the command-building + loop logic is tested without touching the network/disk.
"""
from __future__ import annotations

import asyncio
import os
import unittest

from backend.core.ouroboros.governance import state_persistence_daemon as SP


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestGate(unittest.TestCase):
    def setUp(self):
        os.environ.pop("JARVIS_STATE_BACKUP_ENABLED", None)

    def test_default_false(self):
        self.assertFalse(SP.state_backup_enabled())


class TestBuildCommand(unittest.TestCase):
    def test_rsync(self):
        cmds = SP.build_backup_commands("rsync", ".jarvis", "user@host:/vault")
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0][0], "rsync")
        self.assertIn("--delete", cmds[0])
        self.assertIn("user@host:/vault", cmds[0])

    def test_s3(self):
        cmds = SP.build_backup_commands("s3", ".jarvis", "s3://my-vault/jarvis")
        self.assertEqual(cmds[0][:3], ["aws", "s3", "sync"])
        self.assertIn("s3://my-vault/jarvis", cmds[0])

    def test_gcs_is_native_no_argv(self):
        # Native GCS Vault (2026-06-20): gcs is handled by the native SDK path,
        # NOT a gsutil subprocess — build_backup_commands returns [] (no CLI).
        cmds = SP.build_backup_commands(
            "gcs", ".jarvis", "gs://jarvis-473803-deployments/crucible-state",
        )
        self.assertEqual(cmds, [])

    def test_parse_gs_uri(self):
        self.assertEqual(
            SP._parse_gs_uri("gs://bucket/crucible-state/.jarvis"),
            ("bucket", "crucible-state/.jarvis"),
        )
        self.assertEqual(SP._parse_gs_uri("gs://bucket"), ("bucket", ""))
        self.assertEqual(SP._parse_gs_uri("gs://bucket/"), ("bucket", ""))
        self.assertIsNone(SP._parse_gs_uri("s3://bucket/x"))
        self.assertIsNone(SP._parse_gs_uri(""))
        self.assertIsNone(SP._parse_gs_uri("gs://"))

    def test_gcs_push_failsoft_bad_uri(self):
        # Non-gs target → False, never raises (no SDK call attempted).
        self.assertFalse(SP._gcs_push_blocking(".", "not-a-uri"))

    def test_git_is_three_step(self):
        cmds = SP.build_backup_commands("git", ".jarvis", "origin")
        self.assertEqual(len(cmds), 3)            # add → commit → push
        self.assertEqual(cmds[0][:2], ["git", "-C"])
        self.assertIn("add", cmds[0])
        self.assertIn("commit", cmds[1])
        self.assertIn("push", cmds[2])

    def test_unknown_backend_is_noop(self):
        self.assertEqual(SP.build_backup_commands("ftp", ".jarvis", "x"), [])


class TestRunOnce(unittest.TestCase):
    def setUp(self):
        os.environ["JARVIS_STATE_BACKUP_ENABLED"] = "1"
        os.environ["JARVIS_BACKUP_BACKEND"] = "rsync"
        os.environ["JARVIS_BACKUP_TARGET"] = "user@host:/vault"

    def tearDown(self):
        for k in ("JARVIS_STATE_BACKUP_ENABLED", "JARVIS_BACKUP_BACKEND",
                  "JARVIS_BACKUP_TARGET"):
            os.environ.pop(k, None)

    def test_disabled_is_noop(self):
        os.environ.pop("JARVIS_STATE_BACKUP_ENABLED", None)
        calls = []
        ok = _run(SP.run_once(runner=lambda cmd: calls.append(cmd) or 0))
        self.assertFalse(ok)
        self.assertEqual(calls, [])

    def test_runs_the_backend_command(self):
        calls = []
        async def _runner(cmd):
            calls.append(cmd)
            return 0  # exit code 0 = success
        ok = _run(SP.run_once(runner=_runner))
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "rsync")

    def test_nonzero_exit_is_failsoft(self):
        async def _runner(cmd):
            return 1  # failure
        ok = _run(SP.run_once(runner=_runner))
        self.assertFalse(ok)  # reported, not raised

    def test_runner_exception_is_failsoft(self):
        async def _runner(cmd):
            raise RuntimeError("network down")
        ok = _run(SP.run_once(runner=_runner))
        self.assertFalse(ok)  # swallowed → soak never crashes

    def test_no_target_is_noop(self):
        os.environ.pop("JARVIS_BACKUP_TARGET", None)
        ok = _run(SP.run_once(runner=lambda cmd: 0))
        self.assertFalse(ok)


class TestRunForever(unittest.TestCase):
    def setUp(self):
        os.environ["JARVIS_STATE_BACKUP_ENABLED"] = "1"
        os.environ["JARVIS_BACKUP_BACKEND"] = "rsync"
        os.environ["JARVIS_BACKUP_TARGET"] = "user@host:/vault"

    def tearDown(self):
        for k in ("JARVIS_STATE_BACKUP_ENABLED", "JARVIS_BACKUP_BACKEND",
                  "JARVIS_BACKUP_TARGET"):
            os.environ.pop(k, None)

    def test_loops_until_stopped(self):
        calls = []
        async def _runner(cmd):
            calls.append(cmd)
            return 0
        async def go():
            stop = asyncio.Event()
            task = asyncio.ensure_future(
                SP.run_forever(interval_s=0.01, runner=_runner, stop=stop))
            await asyncio.sleep(0.05)
            stop.set()
            await task
        _run(go())
        self.assertGreaterEqual(len(calls), 2)  # backed up repeatedly


if __name__ == "__main__":
    unittest.main()
