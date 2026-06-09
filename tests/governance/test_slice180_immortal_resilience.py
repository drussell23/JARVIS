"""Slice 180 — the Immortal Execution Layer (total vendor resilience).

The soak proved it: when ALL DW surfaces degrade and Claude is disabled, the op hits
`all_providers_exhausted:fallback_skipped:no_fallback_configured` and is DELETED. A sovereign
organism must not bleed tasks. This adds three resilience primitives:
  1. QUEUE, don't exhaust — when there's no fallback, backoff-retry DW until it recovers.
  2. Intra-request HEDGE — a live_transport rupture retries the SAME request over batch.
  3. Kevlar batch net — resilient batch polling + re-submit on transient 5xx.
"""
from __future__ import annotations

import unittest

from backend.core.ouroboros.governance.dw_immortal import (
    immortal_queue_enabled,
    immortal_backoff_s,
    immortal_should_retry,
    hedge_to_batch_on_rupture,
    batch_should_retry,
    immortal_retry,
)


class TestBackoff(unittest.TestCase):
    def test_exponential_and_capped(self):
        self.assertEqual(immortal_backoff_s(0, base=2.0, cap=60.0), 2.0)
        self.assertEqual(immortal_backoff_s(1, base=2.0, cap=60.0), 4.0)
        self.assertEqual(immortal_backoff_s(2, base=2.0, cap=60.0), 8.0)
        self.assertEqual(immortal_backoff_s(10, base=2.0, cap=60.0), 60.0)  # capped


class TestQueueDecision(unittest.TestCase):
    def test_retry_when_no_fallback_and_budget(self):
        # DW exhausted, Claude unavailable, deadline ahead → QUEUE (retry)
        self.assertTrue(immortal_should_retry(
            deadline=1000.0, now=100.0, claude_available=False, attempt=0, max_attempts=20))

    def test_no_retry_when_fallback_exists(self):
        # Claude available → let the normal cascade handle it (not the queue's job)
        self.assertFalse(immortal_should_retry(
            deadline=1000.0, now=100.0, claude_available=True, attempt=0, max_attempts=20))

    def test_no_retry_past_deadline(self):
        self.assertFalse(immortal_should_retry(
            deadline=100.0, now=500.0, claude_available=False, attempt=0, max_attempts=20))

    def test_no_retry_past_max_attempts(self):
        self.assertFalse(immortal_should_retry(
            deadline=1e9, now=0.0, claude_available=False, attempt=20, max_attempts=20))


class TestHedge(unittest.TestCase):
    def test_rupture_hedges_to_batch(self):
        self.assertTrue(hedge_to_batch_on_rupture("live_transport"))
        self.assertTrue(hedge_to_batch_on_rupture("live_transport:RuntimeError"))

    def test_non_transport_does_not_hedge(self):
        self.assertFalse(hedge_to_batch_on_rupture("live_http_429"))
        self.assertFalse(hedge_to_batch_on_rupture("live_parse_error"))


class TestBatchKevlar(unittest.TestCase):
    def test_transient_5xx_retries(self):
        self.assertTrue(batch_should_retry(503, attempt=0, max_retries=3))
        self.assertTrue(batch_should_retry(500, attempt=2, max_retries=3))

    def test_4xx_does_not_retry(self):
        self.assertFalse(batch_should_retry(400, attempt=0, max_retries=3))
        self.assertFalse(batch_should_retry(404, attempt=0, max_retries=3))

    def test_exhausted_retries_stop(self):
        self.assertFalse(batch_should_retry(503, attempt=3, max_retries=3))


class TestImmortalRetryRecovers(unittest.IsolatedAsyncioTestCase):
    async def test_op_survives_total_outage_and_recovers(self):
        # synthetic total DW outage: fails 3× (vendor down) then succeeds (vendor restored)
        calls = {"n": 0}

        async def attempt():
            calls["n"] += 1
            if calls["n"] <= 3:
                raise RuntimeError("live_transport:RuntimeError")  # DW down
            return "RECOVERED"

        clock = {"t": 0.0}
        slept = []

        async def fake_sleep(s):
            slept.append(s)
            clock["t"] += s

        result = await immortal_retry(
            attempt,
            deadline_fn=lambda: 10000.0,
            now_fn=lambda: clock["t"],
            sleep_fn=fake_sleep,
            claude_available=False,
            max_attempts=20,
            base_backoff=2.0,
            cap_backoff=60.0,
        )
        self.assertEqual(result, "RECOVERED")          # the op SURVIVED + recovered
        self.assertEqual(calls["n"], 4)                # 3 failures + 1 success
        self.assertEqual(slept, [2.0, 4.0, 8.0])       # exponential backoff between retries

    async def test_raises_after_budget_when_never_recovers(self):
        async def always_fail():
            raise RuntimeError("live_transport:RuntimeError")

        clock = {"t": 0.0}

        async def fast_sleep(s):
            clock["t"] += s

        with self.assertRaises(RuntimeError):
            await immortal_retry(
                always_fail,
                deadline_fn=lambda: 30.0,        # tight deadline → budget runs out
                now_fn=lambda: clock["t"],
                sleep_fn=fast_sleep,
                claude_available=False,
                max_attempts=20, base_backoff=2.0, cap_backoff=60.0,
            )


class TestGating(unittest.TestCase):
    def test_enabled_default_true(self):
        import os
        os.environ.pop("JARVIS_DW_IMMORTAL_QUEUE_ENABLED", None)
        self.assertTrue(immortal_queue_enabled())


class TestSentinelWiring(unittest.TestCase):
    def test_exhaustion_path_queues_instead_of_raising(self):
        import importlib.util
        spec = importlib.util.find_spec(
            "backend.core.ouroboros.governance.candidate_generator"
        )
        with open(spec.origin) as fh:
            src = fh.read()
        # the immortal queue-retry must precede the no-fallback raise
        self.assertIn("immortal_should_retry", src)
        self.assertIn("_immortal_attempt", src)
        i_retry = src.find("[Immortal] DW exhausted")
        i_raise = src.find("sentinel_dispatch_no_fallback")
        self.assertTrue(0 < i_retry < i_raise)


if __name__ == "__main__":
    unittest.main()
