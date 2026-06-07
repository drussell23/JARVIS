"""Slice 131 Phase 4 — the CAI Autonomous Router (FrugalGPT cascade for O+V).

"Cursor-auto for O+V": pick the cheapest CAPABLE provider tier per op, escalating
to heavyweights only when CAI signals high difficulty / low confidence — with SAI
acting as a situational cost-guard (under high system pressure, suppress
confidence-driven escalation to conserve spend).

Composition (verify-first — do NOT rebuild):
  * CAI  — ContextAwarenessIntelligence.predict_intent (cheap, sync, deterministic
           intent+confidence scorer) → the difficulty/confidence signal.
  * SAI  — SelfAwareIntelligence.get_cognitive_state → situational pressure.
  * tiers — resolved from brain_selection_policy.yaml (no hardcoded models);
            the cheap Claude tier reuses Phase-1 economic_failover_model.
  * failover — the existing economic_router (downstream, on 402/429).

Gated JARVIS_CAI_ROUTER_ENABLED default-FALSE; fail-closed (any error → None →
caller keeps the existing UrgencyRouter path). OFF must be byte-identical.
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import pathlib
import unittest

from backend.core.ouroboros.governance import cai_router as CR


@dataclasses.dataclass
class _Ctx:
    task_complexity: str = ""


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestGate(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = os.environ.get("JARVIS_CAI_ROUTER_ENABLED")
        os.environ.pop("JARVIS_CAI_ROUTER_ENABLED", None)

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("JARVIS_CAI_ROUTER_ENABLED", None)
        else:
            os.environ["JARVIS_CAI_ROUTER_ENABLED"] = self._prev

    def test_default_false(self) -> None:
        self.assertFalse(CR.cai_router_enabled())

    def test_disabled_decide_returns_none(self) -> None:
        # OFF byte-identical: caller falls back to the existing route path.
        out = _run(CR.decide("add a function", _Ctx(),
                             classifier=lambda p, c: CR.CAIClassification("high", 0.1)))
        self.assertIsNone(out)


class TestCascade(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["JARVIS_CAI_ROUTER_ENABLED"] = "1"
        CR.reset_adaptive_state()

    def tearDown(self) -> None:
        os.environ.pop("JARVIS_CAI_ROUTER_ENABLED", None)
        CR.reset_adaptive_state()

    def _decide(self, difficulty, confidence, pressure="nominal"):
        return _run(CR.decide(
            "do a thing", _Ctx(),
            classifier=lambda p, c: CR.CAIClassification(difficulty, confidence),
            sai_probe=lambda: CR.SituationalSignal(pressure),
        ))

    def test_low_difficulty_high_confidence_uses_cheapest(self) -> None:
        d = self._decide("low", 0.95)
        self.assertEqual(d.tier, "doubleword")     # cheapest rung
        self.assertFalse(d.escalated)

    def test_high_difficulty_escalates_to_heavy(self) -> None:
        d = self._decide("high", 0.95)
        self.assertEqual(d.tier, "claude_heavy")   # top rung
        self.assertTrue(d.escalated)

    def test_low_confidence_bumps_one_rung(self) -> None:
        # low difficulty but the model is unsure → escalate one rung up.
        d = self._decide("low", 0.05)
        self.assertEqual(d.tier, "claude_low_cost")
        self.assertTrue(d.escalated)

    def test_sai_high_pressure_suppresses_confidence_bump(self) -> None:
        # Same low-confidence op, but the system is under high situational
        # pressure → cost-guard keeps it on the cheapest tier.
        d = self._decide("low", 0.05, pressure="high")
        self.assertEqual(d.tier, "doubleword")
        self.assertFalse(d.escalated)

    def test_difficulty_escalation_survives_cost_guard(self) -> None:
        # The cost-guard only suppresses CONFIDENCE bumps, not genuine
        # difficulty — a hard op still gets the capable tier under pressure.
        d = self._decide("high", 0.95, pressure="high")
        self.assertEqual(d.tier, "claude_heavy")

    def test_claude_low_cost_model_resolves_from_policy(self) -> None:
        d = self._decide("medium", 0.95)
        self.assertEqual(d.tier, "claude_low_cost")
        self.assertTrue(d.model)  # resolved from brain_selection_policy.yaml

    def test_fail_closed_on_classifier_error(self) -> None:
        def _boom(p, c):
            raise RuntimeError("classifier exploded")
        out = _run(CR.decide("x", _Ctx(), classifier=_boom))
        self.assertIsNone(out)  # fail-closed → caller keeps existing path

    def test_async_classifier_and_probe_awaited(self) -> None:
        async def _acls(p, c):
            return CR.CAIClassification("high", 0.9)
        async def _asai():
            return CR.SituationalSignal("nominal")
        d = _run(CR.decide("x", _Ctx(), classifier=_acls, sai_probe=_asai))
        self.assertEqual(d.tier, "claude_heavy")

    def test_record_outcome_tunes_threshold(self) -> None:
        base = CR.confidence_threshold()
        for _ in range(20):
            CR.record_outcome(escalation_was_needed=True)
        self.assertGreater(CR.confidence_threshold(), base)  # escalate more readily


class TestRouteDecisionFusion(unittest.TestCase):
    """The gated SHADOW consult fused into RouteDecisionService.select() — it
    surfaces the CAI tier decision (advisory/observable), byte-identical when
    OFF, never overriding brain selection (enforce = documented follow-on)."""

    def setUp(self) -> None:
        from backend.core.ouroboros.governance.route_decision_service import (
            RouteDecisionService,
        )
        from backend.core.ouroboros.governance.brain_selector import BrainSelector
        self.svc = RouteDecisionService(BrainSelector())
        CR.reset_adaptive_state()

    def tearDown(self) -> None:
        os.environ.pop("JARVIS_CAI_ROUTER_ENABLED", None)
        CR.reset_adaptive_state()

    def test_advisory_none_when_disabled(self) -> None:
        os.environ.pop("JARVIS_CAI_ROUTER_ENABLED", None)
        out = _run(self.svc.cai_tier_advisory("refactor the module", "heavy_code"))
        self.assertIsNone(out)
        self.assertIsNone(self.svc._last_cai_tier)

    def test_advisory_decides_and_stores_when_enabled(self) -> None:
        os.environ["JARVIS_CAI_ROUTER_ENABLED"] = "1"
        out = _run(self.svc.cai_tier_advisory("refactor the module", "heavy_code"))
        self.assertIsNotNone(out)
        self.assertEqual(out.tier, "claude_heavy")        # high difficulty
        self.assertIs(self.svc._last_cai_tier, out)        # observable

    def test_advisory_fail_closed(self) -> None:
        os.environ["JARVIS_CAI_ROUTER_ENABLED"] = "1"
        # A None description must not raise — fail-soft to a decision or None.
        out = _run(self.svc.cai_tier_advisory(None, ""))
        self.assertTrue(out is None or isinstance(out, CR.CAIRoutingDecision))


class TestNoHardcode(unittest.TestCase):
    def test_no_claude_model_string_in_module(self) -> None:
        src = pathlib.Path(
            "backend/core/ouroboros/governance/cai_router.py"
        ).read_text()
        for banned in ("claude-haiku", "claude-sonnet", "claude-opus", "claude-3-5"):
            self.assertNotIn(banned, src)


if __name__ == "__main__":
    unittest.main()
