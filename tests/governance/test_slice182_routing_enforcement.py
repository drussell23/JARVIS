"""Slice 182 — total routing enforcement (the unblockable matrix).

The live soak proved the routing matrix was disjointed: _slice36_should_force_batch returns
True for a proper standard-route context, but the SENTINEL's per-model attempts carry an EMPTY
context.provider_route (the route is a function arg, not stamped on the frozen context) — so
force-batch never engaged downstream and every probe ruptured on RT.

Gap 1 fix: the sentinel natively obeys the predictor/warm-boot via a force-batch ContextVar —
when the stream is degraded it COMMANDS all per-model probes to batch at T=0, regardless of the
context's (empty) route attribute.
"""
from __future__ import annotations

import unittest

from backend.core.ouroboros.governance import doubleword_provider as DW


class _EmptyRouteCtx:
    """Mimics the sentinel's per-model context: route attribute is the default empty string."""
    provider_route = ""
    op_id = "diag"


class TestSentinelForceBatchOverride(unittest.TestCase):
    def tearDown(self):
        # ensure the ContextVar never leaks between tests
        try:
            DW._dw_sentinel_force_batch.set(False)
        except Exception:
            pass

    def test_empty_route_normally_does_not_force_batch(self):
        # baseline: an empty-route context can't satisfy the route gate → no force-batch
        DW._dw_sentinel_force_batch.set(False)
        self.assertFalse(DW._slice36_should_force_batch(_EmptyRouteCtx(), model_id="m"))

    def test_sentinel_override_forces_batch_despite_empty_route(self):
        # Gap 1 fix: the sentinel sets the override → force-batch True even with empty route
        tok = DW.set_sentinel_force_batch(True)
        try:
            self.assertTrue(DW._slice36_should_force_batch(_EmptyRouteCtx(), model_id="m"))
        finally:
            DW.reset_sentinel_force_batch(tok)

    def test_override_resets_cleanly(self):
        tok = DW.set_sentinel_force_batch(True)
        DW.reset_sentinel_force_batch(tok)
        self.assertFalse(DW._slice36_should_force_batch(_EmptyRouteCtx(), model_id="m"))

    def test_override_default_false(self):
        self.assertFalse(DW._dw_sentinel_force_batch.get())


class TestSentinelWiring(unittest.TestCase):
    def _gen_src(self):
        import importlib.util
        spec = importlib.util.find_spec("backend.core.ouroboros.governance.candidate_generator")
        with open(spec.origin) as fh:
            return fh.read()

    def test_sentinel_enforces_batch_when_degraded(self):
        src = self._gen_src()
        self.assertIn("set_sentinel_force_batch", src)
        self.assertIn("SENTINEL batch-enforce", src)
        # the enforcement must precede the WALK loop (rfind = the last/walk loop, not the
        # earlier register_endpoint loop) and reset cleanly per-attempt
        i_enforce = src.find("SENTINEL batch-enforce")
        i_walk = src.rfind("for model_id in ranked_models")
        self.assertTrue(0 < i_enforce < i_walk)
        self.assertIn("reset_sentinel_force_batch", src)


class TestRuptureBoundaryHedge(unittest.TestCase):
    """Gap 2 — a rupture must switch remaining probes to batch at the sentinel boundary."""

    def test_rupture_flips_remaining_probes_to_batch(self):
        import importlib.util
        spec = importlib.util.find_spec("backend.core.ouroboros.governance.candidate_generator")
        with open(spec.origin) as fh:
            src = fh.read()
        self.assertIn("rupture HEDGE at sentinel boundary", src)
        # the hedge flip must live inside the LIVE_TRANSPORT classification branch
        i_lt = src.find("failure_source is FailureSource.LIVE_TRANSPORT")
        i_hedge = src.find("rupture HEDGE at sentinel boundary")
        self.assertTrue(0 < i_lt < i_hedge)


class TestImmortalDeadlineDetachment(unittest.TestCase):
    """Gap 3 — the queue must survive a sustained outage, detached from the op's 120s deadline."""

    def test_budget_far_exceeds_op_deadline(self):
        from backend.core.ouroboros.governance.dw_immortal import immortal_max_wait_s
        self.assertGreaterEqual(immortal_max_wait_s(), 1800.0)  # ≫ the 120s generation deadline

    def test_per_attempt_window_is_fresh(self):
        from backend.core.ouroboros.governance.dw_immortal import immortal_per_attempt_window_s
        self.assertGreaterEqual(immortal_per_attempt_window_s(), 60.0)

    def test_wiring_uses_detached_budget_and_fresh_deadline(self):
        import importlib.util
        spec = importlib.util.find_spec("backend.core.ouroboros.governance.candidate_generator")
        with open(spec.origin) as fh:
            src = fh.read()
        self.assertIn("_immortal_budget_deadline", src)
        self.assertIn("immortal_max_wait_s", src)
        self.assertIn("FRESH generation window", src)
        self.assertIn("deadline-detached", src)


if __name__ == "__main__":
    unittest.main()
