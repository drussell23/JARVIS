"""Slice 166 — injection target fidelity (enables natural-risk validation).

The generic /webhook/generic ingress hardcoded target_files=("backend/",), so an
injected op could not declare its REAL target. That blocked the natural-risk path: an
op attempting to modify the governance cage (backend/core/ouroboros/governance/) must
carry that target for the organism's own governance_boundary_gate to elevate it to
APPROVAL_REQUIRED by nature. This honors an explicit target_files (and description)
from the payload — composable injection fidelity, NOT a forced floor.
"""
from __future__ import annotations

import unittest

from backend.core.ouroboros.governance.event_channel import _generic_classification


class TestGenericClassification(unittest.TestCase):
    def test_honors_explicit_target_files(self):
        urgency, desc, targets, repo = _generic_classification(
            {"target_files": ["backend/core/ouroboros/governance/semantic_guardian.py"]},
            "intrusion", "rewrite the guardian",
        )
        self.assertEqual(targets, ("backend/core/ouroboros/governance/semantic_guardian.py",))

    def test_defaults_when_absent(self):
        _, _, targets, _ = _generic_classification({}, "s", "t")
        self.assertEqual(targets, ("backend/",))

    def test_honors_explicit_description(self):
        _, desc, _, _ = _generic_classification({"description": "custom intent"}, "s", "t")
        self.assertEqual(desc, "custom intent")

    def test_synthesizes_description_when_absent(self):
        _, desc, _, _ = _generic_classification({}, "intrusion", "do X")
        self.assertEqual(desc, "intrusion event: do X")

    def test_ignores_garbage_target_files(self):
        _, _, targets, _ = _generic_classification({"target_files": "not-a-list"}, "s", "t")
        self.assertEqual(targets, ("backend/",))


if __name__ == "__main__":
    unittest.main()
