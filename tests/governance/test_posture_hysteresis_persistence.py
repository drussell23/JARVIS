"""Q3 Slice 2 — Posture-observer hysteresis transient state regression.

Closes the two restart-induced bugs the brutal review flagged:

  Bug A (lost-on-restart):
      _last_change_at was in-memory only. On restart the observer fell
      back to ``previous.inferred_at`` as a proxy — but inferred_at
      advances on every same-posture *refresh*, so it doesn't reflect
      "last change time."

  Bug B (fictitious-fresh-change blackout):
      On cold start the observer set ``_last_change_at = now`` even when
      the session was a warm restart with stable posture. That falsely
      started a 15-min hysteresis window, blocking legitimate transitions
      for 15 min after every reboot.

Fix: a 4th store artifact ``posture_change_marker.json``, atomically
paired with ``write_current`` only on real transitions. Observer
hydrates from disk on init. Posture-mismatch invariant rejects drifted
markers (legacy or torn writes) and falls back to the legacy proxy.

Covers:

  §1   Marker round-trip + schema discipline + posture-mismatch reject
  §2   write_current(change_marker_at=None) preserves existing marker
       (same-posture refresh doesn't reset hysteresis)
  §3   Observer hydrates _last_change_at from disk on init
  §4   Bug A — restart preserves last-change-at across observer
       reincarnation when posture is stable
  §5   Bug B — restart with stable posture does NOT impose a fresh
       15-min blackout
  §6   Cold start writes the marker on first promotion (so next restart
       finds it)
  §7   Posture transition refreshes the marker; subsequent same-posture
       refreshes do NOT
  §8   Schema-mismatch / corrupt marker on disk → observer falls through
       to legacy proxy without raising
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional

from backend.core.ouroboros.governance.posture import (
    Posture,
    PostureReading,
)
from backend.core.ouroboros.governance.posture_observer import (
    PostureObserver,
    SignalCollector,
)
from backend.core.ouroboros.governance.posture_store import (
    POSTURE_STORE_SCHEMA,
    PostureStore,
)


def _make_reading(
    posture: Posture,
    *,
    confidence: float = 0.5,
    inferred_at: Optional[float] = None,
) -> PostureReading:
    """Build a minimal PostureReading for store tests. Evidence omitted
    — the marker logic doesn't read from it."""
    return PostureReading(
        posture=posture,
        confidence=confidence,
        evidence=tuple(),
        inferred_at=inferred_at if inferred_at is not None else time.time(),
        signal_bundle_hash="deadbeef" * 4,
        all_scores=tuple(),
    )


# ============================================================================
# §1 — Marker round-trip + schema + posture-mismatch
# ============================================================================


class TestMarkerRoundTrip(unittest.TestCase):
    def test_write_and_load_marker_matches_posture(self):
        with TemporaryDirectory() as td:
            store = PostureStore(Path(td))
            r = _make_reading(Posture.EXPLORE)
            store.write_current(r, change_marker_at=1000.0)
            self.assertEqual(
                store.load_change_marker_at(expected_posture=Posture.EXPLORE),
                1000.0,
            )

    def test_load_returns_none_when_marker_absent(self):
        with TemporaryDirectory() as td:
            store = PostureStore(Path(td))
            self.assertIsNone(store.load_change_marker_at())

    def test_posture_mismatch_returns_none(self):
        """Marker says EXPLORE; caller asks about CONSOLIDATE → None.
        This is the safety net for legacy or torn writes."""
        with TemporaryDirectory() as td:
            store = PostureStore(Path(td))
            r = _make_reading(Posture.EXPLORE)
            store.write_current(r, change_marker_at=500.0)
            self.assertIsNone(
                store.load_change_marker_at(
                    expected_posture=Posture.CONSOLIDATE,
                ),
            )

    def test_no_expected_posture_returns_marker(self):
        with TemporaryDirectory() as td:
            store = PostureStore(Path(td))
            r = _make_reading(Posture.HARDEN)
            store.write_current(r, change_marker_at=42.0)
            self.assertEqual(store.load_change_marker_at(), 42.0)

    def test_corrupt_marker_returns_none(self):
        with TemporaryDirectory() as td:
            store = PostureStore(Path(td))
            store.change_marker_path.parent.mkdir(parents=True, exist_ok=True)
            store.change_marker_path.write_text("{not valid json")
            self.assertIsNone(store.load_change_marker_at())

    def test_schema_mismatch_returns_none(self):
        with TemporaryDirectory() as td:
            store = PostureStore(Path(td))
            store.change_marker_path.parent.mkdir(parents=True, exist_ok=True)
            store.change_marker_path.write_text(json.dumps({
                "schema_version": "9.99",
                "posture": "EXPLORE",
                "change_marker_at": 1.0,
            }))
            self.assertIsNone(store.load_change_marker_at())


# ============================================================================
# §2 — write_current(marker=None) preserves existing marker
# ============================================================================


class TestPreserveOnSamePostureRefresh(unittest.TestCase):
    def test_refresh_without_marker_keeps_existing(self):
        with TemporaryDirectory() as td:
            store = PostureStore(Path(td))
            r1 = _make_reading(Posture.EXPLORE)
            store.write_current(r1, change_marker_at=100.0)
            # Same-posture refresh — no marker passed
            r2 = _make_reading(
                Posture.EXPLORE, confidence=0.9, inferred_at=200.0,
            )
            store.write_current(r2)
            # Marker preserved at original timestamp
            self.assertEqual(
                store.load_change_marker_at(
                    expected_posture=Posture.EXPLORE,
                ),
                100.0,
            )


# ============================================================================
# §3 — Observer hydrates _last_change_at from disk on init
# ============================================================================


class TestObserverHydration(unittest.TestCase):
    def test_hydrates_when_marker_matches_current(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            store = PostureStore(root)
            r = _make_reading(Posture.EXPLORE)
            store.write_current(r, change_marker_at=12345.0)
            observer = PostureObserver(root, store)
            self.assertEqual(observer._last_change_at, 12345.0)

    def test_hydrates_to_none_on_cold_start(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            store = PostureStore(root)
            observer = PostureObserver(root, store)
            self.assertIsNone(observer._last_change_at)

    def test_hydrates_to_none_on_posture_drift(self):
        """Marker for EXPLORE but current is CONSOLIDATE — drift, reject."""
        with TemporaryDirectory() as td:
            root = Path(td)
            store = PostureStore(root)
            store.write_current(
                _make_reading(Posture.CONSOLIDATE),
                change_marker_at=None,
            )
            store.change_marker_path.write_text(json.dumps({
                "schema_version": POSTURE_STORE_SCHEMA,
                "posture": "EXPLORE",
                "change_marker_at": 999.0,
            }))
            observer = PostureObserver(root, store)
            self.assertIsNone(observer._last_change_at)


# ============================================================================
# §4 + §5 — Bug A & Bug B: restart preserves change_at
# ============================================================================


class TestRestartPreservesHysteresisState(unittest.TestCase):
    def _spin_collector(self, observer, posture):
        """Force the observer's inferrer to return a fixed posture
        regardless of signals — we don't care about the inference path
        here, only the hysteresis state machine."""
        class _FixedInferrer:
            def infer(self, bundle, arc_context=None):
                return _make_reading(posture, confidence=0.4)
        observer._inferrer = _FixedInferrer()

    def test_bug_b_warm_restart_does_not_blackout(self):
        """A new observer with an existing current+marker on disk and
        a stable posture must NOT fictitiously reset _last_change_at to
        boot time. After a warm restart the hysteresis window is
        measured against the original change_at, not the boot."""
        with TemporaryDirectory() as td:
            root = Path(td)
            store = PostureStore(root)
            t_old = time.time() - 3 * 24 * 3600  # 3 days ago
            r_old = _make_reading(Posture.EXPLORE, inferred_at=t_old)
            store.write_current(r_old, change_marker_at=t_old)

            # --- Process restart simulation -----------------------------
            observer = PostureObserver(root, store)
            hydrated = observer._last_change_at
            assert hydrated is not None
            self.assertAlmostEqual(hydrated, t_old, delta=1.0)

    def test_bug_a_proxy_falls_through_when_marker_missing(self):
        """When the marker is unavailable (legacy observer wrote
        current without the side-car), the hysteresis check falls
        through to the legacy ``previous.inferred_at`` proxy. The
        observer must hydrate to None, not crash."""
        with TemporaryDirectory() as td:
            root = Path(td)
            store = PostureStore(root)
            r = _make_reading(Posture.EXPLORE)
            store.write_current(r)  # no marker
            observer = PostureObserver(root, store)
            self.assertIsNone(observer._last_change_at)


# ============================================================================
# §6 + §7 — Cold-start write + transition vs refresh marker behavior
# ============================================================================


class TestColdStartAndTransitions(unittest.TestCase):
    def _build_observer(self, root: Path, fixed_posture: Posture):
        store = PostureStore(root)

        class _FixedInferrer:
            def __init__(self):
                self._posture = fixed_posture

            def set_posture(self, p: Posture) -> None:
                self._posture = p

            def infer(self, bundle, arc_context=None):
                return _make_reading(self._posture, confidence=0.4)

        class _FixedCollector(SignalCollector):
            def build_bundle(self):
                from backend.core.ouroboros.governance.posture import (
                    baseline_bundle,
                )
                return baseline_bundle()

        inferrer = _FixedInferrer()
        observer = PostureObserver(
            root, store,
            inferrer=inferrer,  # type: ignore[arg-type]
            collector=_FixedCollector(root),
        )
        return observer, store, inferrer

    def test_cold_start_writes_marker_on_first_promotion(self):
        os.environ["JARVIS_DIRECTION_INFERRER_ENABLED"] = "true"
        with TemporaryDirectory() as td:
            root = Path(td)
            observer, store, _ = self._build_observer(
                root, Posture.EXPLORE,
            )
            asyncio.run(observer.run_one_cycle())
            # Marker now exists, paired with EXPLORE
            self.assertEqual(
                store.load_change_marker_at(
                    expected_posture=Posture.EXPLORE,
                ),
                observer._last_change_at,
            )
            self.assertIsNotNone(observer._last_change_at)

    def test_same_posture_refresh_preserves_marker(self):
        os.environ["JARVIS_DIRECTION_INFERRER_ENABLED"] = "true"
        with TemporaryDirectory() as td:
            root = Path(td)
            observer, store, _ = self._build_observer(
                root, Posture.EXPLORE,
            )
            asyncio.run(observer.run_one_cycle())
            t_first = observer._last_change_at
            # Wait a beat, then run another cycle with same posture
            time.sleep(0.05)
            asyncio.run(observer.run_one_cycle())
            # Marker UNCHANGED — same-posture refresh preserves it
            self.assertEqual(
                store.load_change_marker_at(
                    expected_posture=Posture.EXPLORE,
                ),
                t_first,
            )
            # In-memory state also preserved
            self.assertEqual(observer._last_change_at, t_first)

    def test_posture_transition_refreshes_marker(self):
        """High-confidence bypass forces a transition through the
        hysteresis gate so we can observe the marker rewrite."""
        os.environ["JARVIS_DIRECTION_INFERRER_ENABLED"] = "true"
        os.environ["JARVIS_POSTURE_HIGH_CONFIDENCE_BYPASS"] = "0.1"
        try:
            with TemporaryDirectory() as td:
                root = Path(td)
                observer, store, inferrer = self._build_observer(
                    root, Posture.EXPLORE,
                )
                asyncio.run(observer.run_one_cycle())
                t_first = observer._last_change_at
                # Switch posture; high-confidence bypass forces promotion
                inferrer.set_posture(Posture.CONSOLIDATE)
                time.sleep(0.05)
                asyncio.run(observer.run_one_cycle())
                # Marker NOW points at CONSOLIDATE, with a fresh ts
                t_second = store.load_change_marker_at(
                    expected_posture=Posture.CONSOLIDATE,
                )
                self.assertIsNotNone(t_second)
                assert t_second is not None and t_first is not None
                self.assertGreater(t_second, t_first)
                # And the old EXPLORE marker is gone
                self.assertIsNone(
                    store.load_change_marker_at(
                        expected_posture=Posture.EXPLORE,
                    ),
                )
        finally:
            os.environ.pop(
                "JARVIS_POSTURE_HIGH_CONFIDENCE_BYPASS", None,
            )


# ============================================================================
# §8 — Defensive: corrupt marker doesn't raise on observer init
# ============================================================================


class TestDefensiveBoot(unittest.TestCase):
    def test_corrupt_marker_does_not_crash_observer_init(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            store = PostureStore(root)
            store.write_current(
                _make_reading(Posture.EXPLORE),
            )
            store.change_marker_path.write_text("garbage{")
            # Must not raise
            observer = PostureObserver(root, store)
            self.assertIsNone(observer._last_change_at)


if __name__ == "__main__":
    unittest.main()
