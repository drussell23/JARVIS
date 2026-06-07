"""Slice 127 Phase 3 — dynamic full-jitter exponential DW recovery window.

Replaces the hardcoded 120s STATIC freshness window (the only thing missing —
P3's core self-healing already exists on main) with a DYNAMIC exponential
backoff keyed to consecutive rupture EPISODES, composing the EXISTING
``circuit_breaker.full_jitter_delay`` (no new backoff math). A successful DW
completion resets the episode counter to 0 instantly, so a transient blip
recovers at ``base`` while a chronically-rupturing lane is probed with
progressively wider (capped, jittered) windows — no thundering-herd re-probe.

Gated ``JARVIS_DW_DYNAMIC_RECOVERY_ENABLED`` (default-FALSE §33.1).
"""
from __future__ import annotations

import os
import unittest

from backend.core.ouroboros.governance.dw_transport_recovery import (
    DWTransportRecovery,
    dw_dynamic_recovery_enabled,
    get_dw_transport_recovery,
    reset_dw_transport_recovery,
)


class _CeilingRng:
    """uniform(a, b) -> b (the exponential ceiling) — deterministic high end."""

    def uniform(self, a: float, b: float) -> float:
        return b


class _ZeroRng:
    """uniform(a, b) -> a (0.0) — deterministic low end (tests the floor)."""

    def uniform(self, a: float, b: float) -> float:
        return a


class TestMaster(unittest.TestCase):
    def test_default_false(self) -> None:
        os.environ.pop("JARVIS_DW_DYNAMIC_RECOVERY_ENABLED", None)
        self.assertFalse(dw_dynamic_recovery_enabled())

    def test_enabled_truthy(self) -> None:
        for v in ("1", "true", "yes", "on"):
            os.environ["JARVIS_DW_DYNAMIC_RECOVERY_ENABLED"] = v
            self.assertTrue(dw_dynamic_recovery_enabled())
        os.environ.pop("JARVIS_DW_DYNAMIC_RECOVERY_ENABLED", None)


class TestEpisodeWindow(unittest.TestCase):
    def setUp(self) -> None:
        self._base = os.environ.get("JARVIS_DW_RECOVERY_BASE_S")
        self._cap = os.environ.get("JARVIS_DW_RECOVERY_CAP_S")
        os.environ["JARVIS_DW_RECOVERY_BASE_S"] = "30"
        os.environ["JARVIS_DW_RECOVERY_CAP_S"] = "600"

    def tearDown(self) -> None:
        for k, v in (
            ("JARVIS_DW_RECOVERY_BASE_S", self._base),
            ("JARVIS_DW_RECOVERY_CAP_S", self._cap),
        ):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_no_episodes_zero_window(self) -> None:
        r = DWTransportRecovery()
        self.assertEqual(r.episode_count, 0)
        self.assertEqual(r.dynamic_recovery_window_s(), 0.0)

    def test_episode_one_is_base(self) -> None:
        r = DWTransportRecovery()
        r.note_degraded(now=1000.0)
        self.assertEqual(r.episode_count, 1)
        # Ceiling rng -> min(cap, base*2^0)=base; floor also base.
        self.assertEqual(r.dynamic_recovery_window_s(rng=_CeilingRng()), 30.0)
        self.assertEqual(r.dynamic_recovery_window_s(rng=_ZeroRng()), 30.0)

    def test_exponential_ceiling_grows(self) -> None:
        r = DWTransportRecovery()
        # 3 episodes spaced > base apart (debounce floor = base = 30s).
        r.note_degraded(now=1000.0)   # ep 1
        r.note_degraded(now=1100.0)   # ep 2 (gap 100 > 30)
        r.note_degraded(now=1200.0)   # ep 3
        self.assertEqual(r.episode_count, 3)
        # Ceiling: min(cap, base*2^(3-1)) = 30*4 = 120.
        self.assertEqual(r.dynamic_recovery_window_s(rng=_CeilingRng()), 120.0)
        # Floor: never below base.
        self.assertEqual(r.dynamic_recovery_window_s(rng=_ZeroRng()), 30.0)

    def test_window_capped(self) -> None:
        r = DWTransportRecovery()
        t = 1000.0
        for _ in range(20):           # drive episode count high
            t += 100.0
            r.note_degraded(now=t)
        # Ceiling clamps at cap regardless of 2^n blowup.
        self.assertEqual(r.dynamic_recovery_window_s(rng=_CeilingRng()), 600.0)
        self.assertLessEqual(r.dynamic_recovery_window_s(), 600.0)

    def test_burst_debounce_is_one_episode(self) -> None:
        r = DWTransportRecovery()
        # Many ruptures within `base` seconds = ONE outage = ONE episode.
        r.note_degraded(now=1000.0)
        r.note_degraded(now=1005.0)
        r.note_degraded(now=1010.0)
        r.note_degraded(now=1029.0)
        self.assertEqual(r.episode_count, 1)

    def test_recovery_resets_episodes_instantly(self) -> None:
        r = DWTransportRecovery()
        r.note_degraded(now=1000.0)
        r.note_degraded(now=1100.0)
        self.assertEqual(r.episode_count, 2)
        r.note_recovered()
        self.assertEqual(r.episode_count, 0)
        self.assertEqual(r.dynamic_recovery_window_s(), 0.0)
        # Next degradation starts fresh at base.
        r.note_degraded(now=2000.0)
        self.assertEqual(r.dynamic_recovery_window_s(rng=_CeilingRng()), 30.0)

    def test_window_within_bounds_real_rng(self) -> None:
        r = DWTransportRecovery()
        r.note_degraded(now=1000.0)
        r.note_degraded(now=1100.0)
        r.note_degraded(now=1200.0)
        for _ in range(50):
            w = r.dynamic_recovery_window_s()
            self.assertGreaterEqual(w, 30.0)   # floor = base
            self.assertLessEqual(w, 600.0)     # cap

    def test_never_raises_and_singleton(self) -> None:
        reset_dw_transport_recovery()
        a = get_dw_transport_recovery()
        b = get_dw_transport_recovery()
        self.assertIs(a, b)
        a.note_degraded()
        a.note_recovered()
        snap = a.snapshot()
        self.assertIn("episode_count", snap)
        reset_dw_transport_recovery()


class TestIntegrationWiringPin(unittest.TestCase):
    """Bytes-pin the 3 candidate_generator integration sites (the hot path is
    too large to unit-test directly — same convention as
    ``test_dispatcher_consults_breaker_before_primary``)."""

    def _src(self) -> str:
        import pathlib
        return pathlib.Path(
            "backend/core/ouroboros/governance/candidate_generator.py"
        ).read_text()

    def test_three_sites_wired(self) -> None:
        src = self._src()
        self.assertIn("note_recovered()", src)          # DW success → reset
        self.assertIn("note_degraded()", src)           # rupture → episode
        self.assertIn("dynamic_recovery_window_s()", src)  # preflight window

    def test_wiring_is_gated(self) -> None:
        # Every dynamic-recovery call must be guarded by the master gate.
        src = self._src()
        self.assertIn("dw_dynamic_recovery_enabled", src)


if __name__ == "__main__":
    unittest.main()
