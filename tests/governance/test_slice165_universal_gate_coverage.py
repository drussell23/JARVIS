"""Slice 165 — universal governance floor at the decision boundary.

A trivial/IMMEDIATE op could route to the APPROVE decision with an un-floored tier
(the live soak: it lands SAFE_AUTO and auto-applies, skipping the operator's
MIN_RISK_TIER posture → no approval card). The auto-apply-vs-approve decision in
slice4b is the AUTHORITATIVE enforcement point. apply_floor_to_risk_tier re-asserts
the single-source floor on the RiskTier there, so NO classification path can route
around the governance floor. Composes apply_floor_to_name (fail-closed, Slice 163);
consolidates the name<->RiskTier mapping previously duplicated across gate sites.
"""
from __future__ import annotations

import os
import unittest

from backend.core.ouroboros.governance.risk_tier_floor import apply_floor_to_risk_tier
from backend.core.ouroboros.governance.risk_engine import RiskTier


class TestUniversalFloor(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("JARVIS_MIN_RISK_TIER", None)

    def test_safe_auto_floored_to_approval(self):
        os.environ["JARVIS_MIN_RISK_TIER"] = "approval_required"
        self.assertIs(apply_floor_to_risk_tier(RiskTier.SAFE_AUTO), RiskTier.APPROVAL_REQUIRED)

    def test_notify_apply_floored_to_approval(self):
        os.environ["JARVIS_MIN_RISK_TIER"] = "approval_required"
        self.assertIs(apply_floor_to_risk_tier(RiskTier.NOTIFY_APPLY), RiskTier.APPROVAL_REQUIRED)

    def test_no_config_unchanged(self):
        os.environ.pop("JARVIS_MIN_RISK_TIER", None)
        self.assertIs(apply_floor_to_risk_tier(RiskTier.SAFE_AUTO), RiskTier.SAFE_AUTO)

    def test_floor_never_lowers_a_higher_tier(self):
        os.environ["JARVIS_MIN_RISK_TIER"] = "notify_apply"
        self.assertIs(apply_floor_to_risk_tier(RiskTier.APPROVAL_REQUIRED), RiskTier.APPROVAL_REQUIRED)

    def test_never_raises_on_garbage(self):
        os.environ["JARVIS_MIN_RISK_TIER"] = "approval_required"
        # passing a non-RiskTier must fail-soft (return input) not raise
        sentinel = object()
        self.assertIs(apply_floor_to_risk_tier(sentinel), sentinel)


class TestSlice4bEnforcement(unittest.TestCase):
    def test_slice4b_re_asserts_floor_at_decision_boundary(self):
        import backend.core.ouroboros.governance.phase_runners.slice4b_runner as S4B
        src = open(S4B.__file__).read()
        self.assertIn("apply_floor_to_risk_tier", src)
        # the re-assertion must come before the APPROVAL_REQUIRED decision
        i_floor = src.find("apply_floor_to_risk_tier")
        i_decision = src.find("if risk_tier is RiskTier.APPROVAL_REQUIRED")
        self.assertTrue(0 < i_floor < i_decision)


if __name__ == "__main__":
    unittest.main()
