"""IntakeLayer → conception bridge wiring (Gap 3 live-soak fix).

The DreamEngine that owns blueprint production is attached to the GLS as a
ConsciousnessBridge (gls._consciousness_bridge._consciousness._dream), not a
direct gls._consciousness handle — the first live soak proved the bridge went
idle ("no DreamEngine reachable") because resolution only tried the direct
handle. These tests pin the canonical handle-chain resolution + the arming
path so the event source can never silently detach again.
"""
from __future__ import annotations

import tempfile

from backend.core.ouroboros.governance.intake.intake_layer_service import (
    IntakeLayerConfig,
    IntakeLayerService,
)


class _FakeDream:
    def __init__(self):
        self.observers = []

    def register_blueprint_observer(self, cb):
        self.observers.append(cb)

    def get_blueprints(self, top_n=5):
        return []


class _Consciousness:
    def __init__(self, dream):
        self._dream = dream


class _ConsciousnessBridge:
    def __init__(self, consciousness):
        self._consciousness = consciousness


def _svc(gls):
    with tempfile.TemporaryDirectory() as tmp:
        return IntakeLayerService(
            gls=gls, config=IntakeLayerConfig(project_root=tmp), say_fn=None,
        )


class _GLSViaBridge:
    """Mirrors the real battle-test wiring."""
    def __init__(self, dream):
        self._consciousness_bridge = _ConsciousnessBridge(_Consciousness(dream))


class _GLSDirect:
    def __init__(self, dream):
        self._consciousness = _Consciousness(dream)


def test_resolves_dream_via_consciousness_bridge_chain():
    d = _FakeDream()
    assert _svc(_GLSViaBridge(d))._resolve_dream_engine() is d


def test_resolves_dream_via_direct_consciousness_handle():
    d = _FakeDream()
    assert _svc(_GLSDirect(d))._resolve_dream_engine() is d


def test_resolves_none_when_unreachable():
    assert _svc(object())._resolve_dream_engine() is None


def test_resolves_none_when_dream_lacks_observer_hook():
    class _NoHook:
        pass

    class _GLS:
        def __init__(self):
            self._consciousness = _Consciousness(_NoHook())

    assert _svc(_GLS())._resolve_dream_engine() is None


def test_arming_registers_observer_when_enabled(monkeypatch):
    monkeypatch.setenv("JARVIS_CONCEPTION_BRIDGE_ENABLED", "true")
    import backend.core.ouroboros.governance.conception_proposal_bridge as cpb
    cpb.reset_bridge_for_tests()
    d = _FakeDream()
    svc = _svc(_GLSViaBridge(d))

    class _Router:
        async def ingest(self, e):
            return "enqueued"

    svc._start_conception_bridge(_Router())
    assert len(d.observers) == 1        # the event source is now connected


def test_arming_is_inert_when_master_off(monkeypatch):
    monkeypatch.setenv("JARVIS_CONCEPTION_BRIDGE_ENABLED", "false")
    d = _FakeDream()
    svc = _svc(_GLSViaBridge(d))
    svc._start_conception_bridge(object())
    assert d.observers == []             # default-off → no wiring


def test_arming_never_raises_on_bad_gls(monkeypatch):
    monkeypatch.setenv("JARVIS_CONCEPTION_BRIDGE_ENABLED", "true")
    svc = _svc(object())
    svc._start_conception_bridge(object())   # no dream reachable → idle, no raise
