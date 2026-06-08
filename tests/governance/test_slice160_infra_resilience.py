"""Slice 160 — The Environment-Aware Applicator.

The InfraApplicator hardcoded a single 'requirements.txt' assumption and a missing/
failed pip install was a TERMINAL FAILED that killed the op before the governance
floor. This makes it (a) environment-aware — dynamically resolve the active manifest
present in the repo root, and (b) fail-soft — an infra failure flags INFRA_WARNING and
the op continues to the approval floor for the operator to decide.

Phase-2/fail-soft helpers are pure logic → unit-tested here; the orchestrator/runner
wiring is verified live.
"""
from __future__ import annotations

import os
import pathlib
import tempfile
import unittest

from backend.core.ouroboros.governance import infrastructure_applicator as IA


def _root_with(*files: str) -> pathlib.Path:
    d = pathlib.Path(tempfile.mkdtemp())
    for f in files:
        (d / f).write_text("# manifest\n")
    return d


class TestManifestResolution(unittest.TestCase):
    def test_requested_present_wins(self):
        root = _root_with("requirements.txt", "requirements-soak.txt")
        self.assertEqual(IA._resolve_pip_manifest(root, "requirements.txt"), "requirements.txt")

    def test_falls_back_to_present_manifest_when_requested_absent(self):
        root = _root_with("requirements-soak-oracle.txt")  # requirements.txt ABSENT
        self.assertEqual(
            IA._resolve_pip_manifest(root, "requirements.txt"),
            "requirements-soak-oracle.txt",
        )

    def test_highest_priority_present_wins(self):
        root = _root_with("requirements-soak.txt", "requirements-soak-oracle.txt")
        self.assertEqual(
            IA._resolve_pip_manifest(root, "nonexistent.txt"),
            "requirements-soak-oracle.txt",  # oracle outranks soak
        )

    def test_none_when_nothing_present(self):
        root = _root_with()
        self.assertIsNone(IA._resolve_pip_manifest(root, "requirements.txt"))

    def test_priority_is_env_tunable(self):
        os.environ["JARVIS_INFRA_MANIFEST_PRIORITY"] = "requirements-soak.txt,requirements.txt"
        try:
            root = _root_with("requirements-soak.txt", "requirements-soak-oracle.txt")
            self.assertEqual(IA._resolve_pip_manifest(root, "x.txt"), "requirements-soak.txt")
        finally:
            os.environ.pop("JARVIS_INFRA_MANIFEST_PRIORITY", None)


class TestBuildPipArgvUsesResolved(unittest.TestCase):
    def test_argv_targets_resolved_manifest(self):
        root = _root_with("requirements-soak-oracle.txt")  # requested absent
        argv = IA._build_pip_argv(root, "requirements.txt")
        self.assertIn("requirements-soak-oracle.txt", argv)
        self.assertNotIn("requirements.txt", [a for a in argv if a == "requirements.txt"])


class TestFailSoft(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("JARVIS_INFRA_FAIL_SOFT_ENABLED", None)

    def test_fail_soft_default_true(self):
        os.environ.pop("JARVIS_INFRA_FAIL_SOFT_ENABLED", None)
        self.assertTrue(IA.infra_fail_soft_enabled())

    def test_fail_soft_off(self):
        os.environ["JARVIS_INFRA_FAIL_SOFT_ENABLED"] = "0"
        self.assertFalse(IA.infra_fail_soft_enabled())

    def test_summarize_failures(self):
        r = IA.InfraResult(
            success=False, command="pip install -r requirements.txt", exit_code=1,
            duration_s=0.2, stdout_tail="", stderr_tail="No such file: requirements.txt",
            file_trigger="requirements.txt",
        )
        text = IA.summarize_infra_failures([r])
        self.assertIn("requirements.txt", text)
        self.assertIn("No such file", text)


if __name__ == "__main__":
    unittest.main()
