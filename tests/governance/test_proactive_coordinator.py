"""Proactive Coordinator spine — the FSM-tick binding.

Verifies the coordinator ties gaze→queue→evict→present on one tick,
is master-gated, fail-soft on a headless desktop, and is wired onto
the GovernedLoop heartbeat cadence (not a new poll loop).
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.comms.duplex.proactive_coordinator import (
    ProactiveCrossSpaceCoordinator,
)


def _win(pid, title, w=800, h=600):
    return {
        "kCGWindowOwnerPID": pid, "kCGWindowName": title,
        "kCGWindowOwnerName": "Code", "kCGWindowIsOnscreen": True,
        "kCGWindowBounds": {"Width": w, "Height": h},
    }


class _FakeGaze:
    def __init__(self, result):
        self._result = result
        self._last_hash = "TOPO"
        self.ticks = 0
        self.focuses = []
    def tick(self, windows, explicit=False):
        self.ticks += 1
        return self._result
    def note_focus(self, sid):
        self.focuses.append(sid)
    @staticmethod
    def _insight_spaces(insight):
        return list(insight.get("affected_spaces", []))


class _FakeQueue:
    def __init__(self, idle=True):
        self.submitted = []
        self.evictions = []
        self._idle = idle
        self.depth = 0
    def submit(self, insight, spaces, dhash):
        self.submitted.append((insight, spaces, dhash))
        self.depth += 1
        return True
    def evict_stale(self, current_dhash=None):
        self.evictions.append(current_dhash)
        return 0
    def present_if_idle(self):
        if self._idle and self.submitted:
            self.depth -= 1
            return self.submitted[0][0]
        return None


class TestCoordinatorGating:
    def test_gate_off_is_noop(self, monkeypatch):
        monkeypatch.delenv("JARVIS_CROSSSPACE_PROACTIVE_ENABLED", raising=False)
        gaze = _FakeGaze({"synthesized": True, "proactive": [{"affected_spaces": [1]}]})
        c = ProactiveCrossSpaceCoordinator(gaze=gaze, queue=_FakeQueue())
        r = c.tick()
        assert r["active"] is False and r["reason"] == "gate_off"
        assert gaze.ticks == 0                       # nothing ran

    def test_full_chain_on_tick(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CROSSSPACE_PROACTIVE_ENABLED", "true")
        insight = {"description": "reconcile", "affected_spaces": [2, 4]}
        gaze = _FakeGaze({"synthesized": True, "proactive": [insight]})
        queue = _FakeQueue(idle=True)
        presented = []
        c = ProactiveCrossSpaceCoordinator(
            windows_source=lambda: {1: [_win(1, "a")]},
            present_sink=presented.append, gaze=gaze, queue=queue,
        )
        r = c.tick()
        assert gaze.ticks == 1
        assert queue.submitted and queue.submitted[0][1] == [2, 4]
        assert queue.evictions == ["TOPO"]           # dhash-keyed eviction
        assert presented == [insight]                # idle → presented
        assert r["presented"] is True

    def test_flow_holds_proposal(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CROSSSPACE_PROACTIVE_ENABLED", "true")
        insight = {"description": "x", "affected_spaces": [1]}
        gaze = _FakeGaze({"synthesized": True, "proactive": [insight]})
        queue = _FakeQueue(idle=False)               # operator typing
        presented = []
        c = ProactiveCrossSpaceCoordinator(
            present_sink=presented.append, gaze=gaze, queue=queue,
        )
        c.tick()
        assert queue.submitted                       # queued
        assert presented == []                       # but NOT presented

    def test_headless_empty_desktop_noop(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CROSSSPACE_PROACTIVE_ENABLED", "true")
        gaze = _FakeGaze({"synthesized": False, "reason": "no_real_windows"})
        c = ProactiveCrossSpaceCoordinator(
            windows_source=lambda: {}, gaze=gaze, queue=_FakeQueue(),
        )
        r = c.tick()
        assert r["active"] is True                    # ran, cleanly
        assert r["synthesized"] is False

    def test_note_focus_reaches_gaze(self):
        gaze = _FakeGaze({})
        c = ProactiveCrossSpaceCoordinator(gaze=gaze, queue=_FakeQueue())
        c.note_focus(7)
        assert gaze.focuses == [7]

    def test_coordinator_wired_on_heartbeat_pin(self):
        from pathlib import Path
        src = (
            Path(__file__).resolve().parents[2]
            / "backend/core/ouroboros/governance/governed_loop_service.py"
        ).read_text()
        # Same heartbeat cadence, not a new loop:
        hb = src[src.index("async def _chronos_heartbeat_loop"):]
        hb = hb[:hb.index("self._chronos_task")]
        assert "_proactive_coord.tick()" in hb
        assert "ProactiveCrossSpaceCoordinator" in src


# ---------------------------------------------------------------------------
# The three landed wires (2026-07-19)
# ---------------------------------------------------------------------------


class TestLandedWires:
    def test_wire1_windows_provider_shape_and_headless_safe(self):
        from backend.core.ouroboros.governance.comms.duplex.proactive_coordinator import (  # noqa: E501
            native_windows_by_space,
        )
        # On CI / non-Quartz this returns {} without raising — the
        # coordinator's headless no-op path.
        result = native_windows_by_space()
        assert isinstance(result, dict)

    def test_wire2_sink_renders_alert_through_existing_surface(self):
        from backend.core.ouroboros.governance.comms.duplex.proactive_coordinator import (  # noqa: E501
            build_proactive_sink,
        )
        alerts = []

        class _Prop:
            spaces = [2, 4]
            def summary(self): return "reconcile test with source"

        sink = build_proactive_sink(lambda **kw: alerts.append(kw))
        sink(_Prop())
        assert len(alerts) == 1
        a = alerts[0]
        assert "reconcile" in a["body"] and "[Y/n]" in a["body"]
        assert a["source"] == "cross_space"
        assert "2, 4" in a["body"]                   # spaces surfaced

    def test_wire3_approved_reconciliation_reaches_backlog(self):
        from backend.core.ouroboros.governance.comms.duplex.proactive_coordinator import (  # noqa: E501
            build_proactive_sink,
        )
        backlog = []

        class _Prop:
            spaces = [1]
            def summary(self): return "align lint config"

        sink = build_proactive_sink(
            lambda **kw: None,
            backlog_emit=lambda s, sp: backlog.append((s, sp)),
        )
        sink(_Prop())
        assert backlog == [("align lint config", [1])]

    def test_wire3_cross_space_signal_source_exists(self):
        from backend.core.ouroboros.governance.intent.signals import (
            SignalSource,
        )
        assert SignalSource.CROSS_SPACE == "cross_space"
        assert SignalSource("cross_space") is SignalSource.CROSS_SPACE

    def test_focus_derived_from_current_space_on_tick(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CROSSSPACE_PROACTIVE_ENABLED", "true")
        gaze = _FakeGaze({"synthesized": False})
        c = ProactiveCrossSpaceCoordinator(
            windows_source=lambda: {1: [_win(1, "a")], 3: [_win(2, "b")]},
            gaze=gaze, queue=_FakeQueue(),
        )
        c.tick()
        assert gaze.focuses == [1]                    # current Space stamped

    def test_gls_binds_real_providers_pin(self):
        from pathlib import Path
        src = (
            Path(__file__).resolve().parents[2]
            / "backend/core/ouroboros/governance/governed_loop_service.py"
        ).read_text()
        assert "native_windows_by_space" in src       # WIRE 1
        assert "build_proactive_sink" in src          # WIRE 2
        assert "SignalSource.CROSS_SPACE" in src       # WIRE 3
        assert "emit_proactive_alert" in src
