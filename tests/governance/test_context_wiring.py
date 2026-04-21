"""Slice 3 tests — Ledger auto-wiring bridges."""
from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from backend.core.ouroboros.governance.context_intent import (
    IntentTracker,
    reset_default_tracker_registry,
)
from backend.core.ouroboros.governance.context_ledger import (
    ContextLedger,
    reset_default_registry,
)
from backend.core.ouroboros.governance.context_pins import (
    ContextPinRegistry,
    PinSource,
    reset_default_pin_registries,
)
from backend.core.ouroboros.governance.context_wiring import (
    ChunkIdResolver,
    attach_preservation_wiring,
    bridge_ledger_to_pins,
    bridge_ledger_to_tracker,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_default_registry()
    reset_default_tracker_registry()
    reset_default_pin_registries()
    yield
    reset_default_registry()
    reset_default_tracker_registry()
    reset_default_pin_registries()


# ===========================================================================
# bridge_ledger_to_tracker — feeds every entry kind
# ===========================================================================


def test_tracker_bridge_feeds_file_read_entries():
    ledger = ContextLedger("op-1")
    tracker = IntentTracker("op-1")
    bridge_ledger_to_tracker(ledger=ledger, tracker=tracker)
    ledger.record_file_read(file_path="backend/auth.py")
    assert "backend/auth.py" in tracker.current_intent().recent_paths


def test_tracker_bridge_feeds_error_entries():
    ledger = ContextLedger("op-1")
    tracker = IntentTracker("op-1")
    bridge_ledger_to_tracker(ledger=ledger, tracker=tracker)
    ledger.record_error(
        error_class="ImportError",
        message="Traceback: ImportError",
        where="backend/api.py:12",
    )
    intent = tracker.current_intent()
    assert "importerror" in intent.recent_error_terms
    # 'backend/api.py:12' or 'backend/api.py' matches the path extractor
    assert any(
        p.startswith("backend/api.py")
        for p in intent.recent_paths
    )


def test_tracker_bridge_feeds_question_entries():
    ledger = ContextLedger("op-1")
    tracker = IntentTracker("op-1")
    bridge_ledger_to_tracker(ledger=ledger, tracker=tracker)
    ledger.record_question(
        question="rename FooService?",
        related_paths=("backend/foo.py",),
        related_tools=("edit_file",),
    )
    intent = tracker.current_intent()
    assert "backend/foo.py" in intent.recent_paths
    assert "edit_file" in intent.recent_tools


def test_tracker_bridge_feeds_decision_entries():
    ledger = ContextLedger("op-1")
    tracker = IntentTracker("op-1")
    bridge_ledger_to_tracker(ledger=ledger, tracker=tracker)
    ledger.record_decision(
        decision_type="plan_approval",
        outcome="approved",
        approved_paths=("backend/core/",),
    )
    assert "backend/core/" in tracker.current_intent().recent_paths


def test_tracker_bridge_unsub_stops_feed():
    ledger = ContextLedger("op-1")
    tracker = IntentTracker("op-1")
    unsub = bridge_ledger_to_tracker(ledger=ledger, tracker=tracker)
    ledger.record_file_read(file_path="a.py")
    unsub()
    ledger.record_file_read(file_path="b.py")
    intent = tracker.current_intent()
    assert "a.py" in intent.recent_paths
    assert "b.py" not in intent.recent_paths


def test_tracker_bridge_swallows_exceptions():
    ledger = ContextLedger("op-1")

    class _BadTracker:
        def ingest_ledger_entry(self, _p):
            raise RuntimeError("boom")

    # Must not raise
    bridge_ledger_to_tracker(ledger=ledger, tracker=_BadTracker())
    ledger.record_file_read(file_path="x.py")


# ===========================================================================
# bridge_ledger_to_pins — trigger allowlist
# ===========================================================================


def test_pin_bridge_fires_on_open_error():
    ledger = ContextLedger("op-p1")
    pins = ContextPinRegistry("op-p1")
    bridge_ledger_to_pins(ledger=ledger, pins=pins)
    entry = ledger.record_error(
        error_class="ImportError", message="m", where="x.py",
    )
    # Auto-pin used entry_id as chunk_id by default
    assert pins.is_pinned(entry.entry_id)
    active = pins.list_active()
    assert active[0].kind == "auto_error"


def test_pin_bridge_fires_on_approved_decision():
    ledger = ContextLedger("op-p2")
    pins = ContextPinRegistry("op-p2")
    bridge_ledger_to_pins(ledger=ledger, pins=pins)
    entry = ledger.record_decision(
        decision_type="plan_approval", outcome="approved",
    )
    assert pins.is_pinned(entry.entry_id)
    active = pins.list_active()
    assert active[0].kind == "auto_decision"


def test_pin_bridge_fires_on_open_question():
    ledger = ContextLedger("op-p3")
    pins = ContextPinRegistry("op-p3")
    bridge_ledger_to_pins(ledger=ledger, pins=pins)
    entry = ledger.record_question(question="why?")
    assert pins.is_pinned(entry.entry_id)
    active = pins.list_active()
    assert active[0].kind == "auto_question"


def test_pin_bridge_ignores_file_read_and_tool_call():
    """Only the 3 trigger kinds pin. Everything else is a no-op."""
    ledger = ContextLedger("op-p4")
    pins = ContextPinRegistry("op-p4")
    bridge_ledger_to_pins(ledger=ledger, pins=pins)
    ledger.record_file_read(file_path="x.py")
    ledger.record_tool_call(tool="read_file", call_id="c1")
    assert pins.list_active() == []


def test_pin_bridge_ignores_rejected_decision():
    ledger = ContextLedger("op-p5")
    pins = ContextPinRegistry("op-p5")
    bridge_ledger_to_pins(ledger=ledger, pins=pins)
    ledger.record_decision(
        decision_type="plan_approval", outcome="rejected",
    )
    assert pins.list_active() == []


def test_pin_bridge_ignores_resolved_error():
    ledger = ContextLedger("op-p6")
    pins = ContextPinRegistry("op-p6")
    bridge_ledger_to_pins(ledger=ledger, pins=pins)
    ledger.record_error(
        error_class="X", message="m", status="resolved",
    )
    assert pins.list_active() == []


def test_pin_bridge_custom_resolver_maps_to_chunk_id():
    ledger = ContextLedger("op-p7")
    pins = ContextPinRegistry("op-p7")

    class _MyResolver:
        def resolve(self, *, entry_id, kind, projection) -> Optional[str]:
            return f"chunk-{kind}-{entry_id}"

    bridge_ledger_to_pins(
        ledger=ledger, pins=pins, resolver=_MyResolver(),
    )
    entry = ledger.record_error(
        error_class="X", message="m", where="x.py",
    )
    expected_chunk_id = f"chunk-error-{entry.entry_id}"
    assert pins.is_pinned(expected_chunk_id)


def test_pin_bridge_resolver_returning_none_skips_pin():
    ledger = ContextLedger("op-p8")
    pins = ContextPinRegistry("op-p8")

    class _NoneResolver:
        def resolve(self, **kwargs) -> Optional[str]:
            return None

    bridge_ledger_to_pins(
        ledger=ledger, pins=pins, resolver=_NoneResolver(),
    )
    ledger.record_error(error_class="X", message="m")
    assert pins.list_active() == []


def test_pin_bridge_resolver_raising_swallowed():
    ledger = ContextLedger("op-p9")
    pins = ContextPinRegistry("op-p9")

    class _RaisingResolver:
        def resolve(self, **kwargs):
            raise RuntimeError("boom")

    bridge_ledger_to_pins(
        ledger=ledger, pins=pins, resolver=_RaisingResolver(),
    )
    # Must not raise — resolver exception swallowed
    ledger.record_error(error_class="X", message="m")
    assert pins.list_active() == []


def test_pin_bridge_unsub_stops_auto_pins():
    ledger = ContextLedger("op-p10")
    pins = ContextPinRegistry("op-p10")
    unsub = bridge_ledger_to_pins(ledger=ledger, pins=pins)
    ledger.record_error(error_class="A", message="m")
    unsub()
    ledger.record_error(error_class="B", message="m")
    # Only the first pin survives
    active = pins.list_active()
    assert len(active) == 1


# ===========================================================================
# Composite attach helper
# ===========================================================================


def test_attach_preservation_wiring_feeds_tracker_and_pins():
    ledger = ContextLedger("op-both")
    tracker = IntentTracker("op-both")
    pins = ContextPinRegistry("op-both")
    attach_preservation_wiring(
        ledger=ledger, tracker=tracker, pins=pins,
    )
    ledger.record_file_read(file_path="backend/x.py")
    ledger.record_error(error_class="E", message="m", where="backend/x.py")
    # Tracker has the path
    assert "backend/x.py" in tracker.current_intent().recent_paths
    # Pins have the error entry
    active = pins.list_active()
    assert any(p.kind == "auto_error" for p in active)


def test_attach_preservation_wiring_unsub_both():
    ledger = ContextLedger("op-both2")
    tracker = IntentTracker("op-both2")
    pins = ContextPinRegistry("op-both2")
    unsub = attach_preservation_wiring(
        ledger=ledger, tracker=tracker, pins=pins,
    )
    ledger.record_file_read(file_path="a.py")
    unsub()
    ledger.record_file_read(file_path="b.py")
    ledger.record_error(error_class="E", message="m")
    # Both bridges detached
    intent = tracker.current_intent()
    assert "b.py" not in intent.recent_paths
    assert pins.list_active() == []


# ===========================================================================
# ChunkIdResolver Protocol shape
# ===========================================================================


def test_resolver_protocol_is_structural():
    class _Impl:
        def resolve(self, *, entry_id, kind, projection) -> Optional[str]:
            return entry_id

    assert isinstance(_Impl(), ChunkIdResolver)
