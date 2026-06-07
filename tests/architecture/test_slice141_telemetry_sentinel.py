"""Slice 141 — The Sovereign Telemetry Failsafe (anomaly radar).

An async sentinel that fires a webhook ONLY on catastrophic anomalies — so a
12-month container is not a black box requiring manual `docker logs` polling:
  * cost spend crosses 90% of the cap,
  * consecutive provider 5xx beyond the backoff limit,
  * a REFUSED_SAFETY violation (rogue FSM state).

Gated JARVIS_TELEMETRY_SENTINEL_ENABLED default-FALSE; dispatch is async +
fail-soft (a dead webhook never perturbs the soak); per-kind cooldown prevents
alert spam. The webhook poster is injectable → tested without network.
"""
from __future__ import annotations

import asyncio
import os
import unittest

from backend.core.ouroboros.governance import telemetry_sentinel as TS


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestGate(unittest.TestCase):
    def setUp(self):
        os.environ.pop("JARVIS_TELEMETRY_SENTINEL_ENABLED", None)

    def test_default_false(self):
        self.assertFalse(TS.telemetry_sentinel_enabled())


class TestDetectors(unittest.TestCase):
    def _s(self, **kw):
        return TS.TelemetrySentinel(cooldown_s=10_000, **kw)

    def test_cost_below_threshold_no_alert(self):
        s = self._s()
        self.assertIsNone(s.note_cost(spent=400.0, cap=500.0))  # 80% < 90%

    def test_cost_at_threshold_alerts(self):
        s = self._s()
        a = s.note_cost(spent=450.0, cap=500.0)  # 90%
        self.assertIsNotNone(a)
        self.assertEqual(a.kind, TS.AnomalyKind.COST_CAP_90)

    def test_cost_cooldown_suppresses_repeat(self):
        s = self._s()
        self.assertIsNotNone(s.note_cost(spent=460.0, cap=500.0))
        self.assertIsNone(s.note_cost(spent=470.0, cap=500.0))  # within cooldown

    def test_consecutive_5xx_threshold(self):
        s = self._s(max_consec_5xx=3)
        self.assertIsNone(s.note_provider_result(500))
        self.assertIsNone(s.note_provider_result(503))
        a = s.note_provider_result(502)  # 3rd consecutive → trips
        self.assertIsNotNone(a)
        self.assertEqual(a.kind, TS.AnomalyKind.CONSECUTIVE_5XX)

    def test_success_resets_5xx_counter(self):
        s = self._s(max_consec_5xx=3)
        s.note_provider_result(500)
        s.note_provider_result(500)
        self.assertIsNone(s.note_provider_result(200))   # reset
        self.assertIsNone(s.note_provider_result(500))   # count restarts
        self.assertIsNone(s.note_provider_result(500))

    def test_4xx_does_not_trip_5xx(self):
        s = self._s(max_consec_5xx=2)
        self.assertIsNone(s.note_provider_result(429))
        self.assertIsNone(s.note_provider_result(400))

    def test_safety_refusal_alerts(self):
        s = self._s()
        a = s.note_safety_refusal("rogue FSM tried to flip a kill-switch")
        self.assertIsNotNone(a)
        self.assertEqual(a.kind, TS.AnomalyKind.REFUSED_SAFETY)


class TestDispatch(unittest.TestCase):
    def test_dispatch_posts_payload(self):
        calls = []
        async def _poster(url, payload):
            calls.append((url, payload))
            return 204
        s = TS.TelemetrySentinel(cooldown_s=0, webhook_url="https://hook.example/x")
        alert = s.note_safety_refusal("boom")
        ok = _run(s.dispatch(alert, poster=_poster))
        self.assertTrue(ok)
        self.assertEqual(calls[0][0], "https://hook.example/x")
        # Slack ("text") + Discord ("content") compatible payload.
        self.assertIn("boom", calls[0][1].get("text", ""))
        self.assertIn("boom", calls[0][1].get("content", ""))

    def test_dispatch_failsoft(self):
        async def _boom(url, payload):
            raise RuntimeError("webhook unreachable")
        s = TS.TelemetrySentinel(cooldown_s=0, webhook_url="https://hook.example/x")
        ok = _run(s.dispatch(s.note_safety_refusal("x"), poster=_boom))
        self.assertFalse(ok)  # swallowed → soak never crashes

    def test_dispatch_noop_without_url(self):
        s = TS.TelemetrySentinel(cooldown_s=0)  # no webhook_url
        ok = _run(s.dispatch(s.note_safety_refusal("x"), poster=None))
        self.assertFalse(ok)


class TestNowaitHooks(unittest.TestCase):
    def setUp(self):
        os.environ["JARVIS_TELEMETRY_SENTINEL_ENABLED"] = "1"
        os.environ["JARVIS_SENTINEL_WEBHOOK_URL"] = "https://hook.example/x"
        TS.reset_sentinel()

    def tearDown(self):
        for k in ("JARVIS_TELEMETRY_SENTINEL_ENABLED", "JARVIS_SENTINEL_WEBHOOK_URL"):
            os.environ.pop(k, None)
        TS.reset_sentinel()

    def test_nowait_gated(self):
        os.environ.pop("JARVIS_TELEMETRY_SENTINEL_ENABLED", None)
        # disabled → returns immediately, no raise
        TS.note_safety_refusal_nowait("x")

    def test_nowait_schedules_dispatch(self):
        sent = []
        async def go():
            async def _poster(url, payload):
                sent.append(payload)
                return 204
            TS.note_safety_refusal_nowait("rogue", poster=_poster)
            await asyncio.sleep(0.05)
            self.assertTrue(sent)
            self.assertIn("rogue", sent[0].get("content", ""))
        _run(go())


if __name__ == "__main__":
    unittest.main()
