"""SkyLight true-Space attribution + Amortized Presentation Slot spine.

Mandate 4 verbatim (2026-07-19): a mocked SkyLight symbol resolution
failure (AttributeError on the CDLL/bundle load) → the fault-handler
catches the missing symbol, logs the degradation, falls back to PID
bucketing, and the primary loop survives.
"""
from __future__ import annotations

import logging

import pytest

from backend.core.ouroboros.governance.comms.duplex import skylight_spaces as sl
from backend.core.ouroboros.governance.comms.duplex.proposal_queue import (
    ProposalQueue,
)


@pytest.fixture(autouse=True)
def _reset_skylight():
    sl._reset_for_tests()
    yield
    sl._reset_for_tests()


class TestDynamicSymbolResolution:
    def test_symbol_resolution_failure_degrades_no_crash(
        self, monkeypatch, caplog,
    ):
        """MANDATE 4 VERBATIM: AttributeError on bundle/CDLL load →
        caught, logged, falls back to PID (None), loop survives."""
        import backend.vision.macos_space_detector as msd

        class _BoomDetector:
            def __init__(self):
                raise AttributeError(
                    "dlsym(CGSCopySpacesForWindows): symbol not found",
                )

        monkeypatch.setattr(msd, "MacOSSpaceDetector", _BoomDetector)
        with caplog.at_level(logging.WARNING, logger="Ouroboros.SkyLightSpaces"):
            result = sl.true_windows_by_space([{"kCGWindowNumber": 1}])
        assert result is None                       # → PID fallback
        assert sl.skylight_available() is False
        assert any("symbol resolution failed" in r.message
                   for r in caplog.records)         # logged degradation
        # The primary loop is unharmed — a second call is a fast no-op:
        assert sl.true_windows_by_space([{"kCGWindowNumber": 2}]) is None

    def test_private_api_unavailable_degrades(self, monkeypatch):
        import backend.vision.macos_space_detector as msd

        class _NoPrivateAPI:
            _private_api_available = False

        monkeypatch.setattr(msd, "MacOSSpaceDetector", lambda: _NoPrivateAPI())
        assert sl.true_windows_by_space([{"kCGWindowNumber": 1}]) is None
        assert sl.skylight_available() is False

    def test_resolution_failure_remembered_not_repaid(self, monkeypatch):
        import backend.vision.macos_space_detector as msd
        calls = {"n": 0}

        def _boom():
            calls["n"] += 1
            raise AttributeError("gone")

        monkeypatch.setattr(msd, "MacOSSpaceDetector", _boom)
        sl.true_windows_by_space([])
        sl.true_windows_by_space([])
        sl.true_windows_by_space([])
        assert calls["n"] == 1                       # resolved-fault cached

    def test_provider_falls_back_when_skylight_none(self, monkeypatch):
        # native_windows_by_space must still yield the PID-bucket map
        # when SkyLight returns None (the real degraded path).
        from backend.core.ouroboros.governance.comms.duplex import (
            proactive_coordinator as pc,
        )
        monkeypatch.setattr(
            "backend.core.ouroboros.governance.comms.duplex.skylight_spaces."
            "true_windows_by_space", lambda w: None,
        )
        # No Quartz on CI → {} (still no crash); on macOS → PID map.
        result = pc.native_windows_by_space()
        assert isinstance(result, dict)


class TestAmortizedPresentationSlot:
    def test_fresher_insight_evicts_older_single_slot(self):
        """The Alert Avalanche Guard: max ONE proposal; a newer one
        silently replaces the older."""
        q = ProposalQueue(idle_source=lambda: 30.0)  # idle → will present
        q.submit({"description": "old insight"}, [1], dhash="A")
        assert q.depth == 1
        q.submit({"description": "FRESH insight"}, [2], dhash="B")
        assert q.depth == 1                          # STILL one slot
        assert q.stats["slot_evicted"] == 1
        # The one held proposal is the FRESH one:
        p = q.present_if_idle()
        assert p is not None and "FRESH" in p.summary()

    def test_returning_operator_faces_one_prompt(self):
        q = ProposalQueue(idle_source=lambda: 30.0)  # idle (stepped away)
        # A long idle period generates several insights:
        for i in range(5):
            q.submit({"description": f"insight-{i}"}, [i], dhash=f"H{i}")
        assert q.depth == 1                          # never stacked
        p = q.present_if_idle()
        assert "insight-4" in p.summary()            # the newest
        assert q.present_if_idle() is None           # no avalanche behind it

    def test_identical_insight_still_deduped_not_evicted(self):
        q = ProposalQueue(idle_source=lambda: 0.0)
        q.submit({"description": "same"}, [1], dhash="A")
        assert q.submit({"description": "same"}, [1], dhash="A") is False
        assert q.stats["deduped"] == 1
        assert q.stats["slot_evicted"] == 0          # dedup ≠ eviction
        assert q.depth == 1
