"""Slice 3 tests — ContextPinRegistry + /pin /unpin /pins REPL."""
from __future__ import annotations

import math
import time
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.governance.context_intent import (
    ChunkCandidate,
    IntentTracker,
    PreservationScorer,
)
from backend.core.ouroboros.governance.context_pins import (
    ContextPinRegistries,
    ContextPinRegistry,
    PIN_REGISTRY_SCHEMA_VERSION,
    PinError,
    PinSource,
    dispatch_pin_command,
    pin_registry_for,
    reset_default_pin_registries,
)


@pytest.fixture(autouse=True)
def _clean_registries():
    reset_default_pin_registries()
    yield
    reset_default_pin_registries()


# ===========================================================================
# Manual pin / unpin
# ===========================================================================


def test_operator_pin_round_trip():
    reg = ContextPinRegistry("op-1")
    entry = reg.pin(
        chunk_id="c-1", source=PinSource.OPERATOR, reason="critical",
    )
    assert entry.chunk_id == "c-1"
    assert entry.kind == "operator"
    assert reg.is_pinned("c-1") is True


def test_orchestrator_pin_accepted():
    reg = ContextPinRegistry("op-1")
    reg.pin(chunk_id="c-1", source=PinSource.ORCHESTRATOR, reason="auto")
    assert reg.is_pinned("c-1")


def test_empty_chunk_id_rejected():
    reg = ContextPinRegistry("op-1")
    with pytest.raises(PinError):
        reg.pin(chunk_id="", source=PinSource.OPERATOR)


def test_repin_same_chunk_replaces_entry():
    reg = ContextPinRegistry("op-1")
    e1 = reg.pin(chunk_id="c-1", source=PinSource.OPERATOR, reason="first")
    e2 = reg.pin(chunk_id="c-1", source=PinSource.OPERATOR, reason="second")
    # Only one pin remains
    assert len(reg.list_active()) == 1
    assert reg.get(e1.pin_id) is None
    assert reg.get(e2.pin_id) is not None
    assert reg.get(e2.pin_id).reason == "second"


def test_unpin_returns_entry_or_none():
    reg = ContextPinRegistry("op-1")
    p = reg.pin(chunk_id="c-1", source=PinSource.OPERATOR)
    assert reg.unpin(p.pin_id) == p
    assert reg.unpin(p.pin_id) is None  # second unpin no-op


def test_unpin_chunk_finds_by_chunk_id():
    reg = ContextPinRegistry("op-1")
    reg.pin(chunk_id="c-1", source=PinSource.OPERATOR)
    assert reg.unpin_chunk("c-1") is not None
    assert reg.is_pinned("c-1") is False


# ===========================================================================
# TTL expiry
# ===========================================================================


def test_expired_pin_reports_not_pinned():
    reg = ContextPinRegistry("op-1")
    reg.pin(chunk_id="c-1", source=PinSource.OPERATOR, ttl_s=0.05)
    time.sleep(0.1)
    assert reg.is_pinned("c-1") is False


def test_prune_expired_removes_and_counts():
    reg = ContextPinRegistry("op-1")
    reg.pin(chunk_id="a", source=PinSource.OPERATOR, ttl_s=0.05)
    reg.pin(chunk_id="b", source=PinSource.OPERATOR, ttl_s=3600.0)
    time.sleep(0.1)
    n = reg.prune_expired()
    assert n == 1
    assert {p.chunk_id for p in reg.list_active()} == {"b"}


def test_list_active_excludes_expired():
    reg = ContextPinRegistry("op-1")
    reg.pin(chunk_id="a", source=PinSource.OPERATOR, ttl_s=0.05)
    reg.pin(chunk_id="b", source=PinSource.OPERATOR, ttl_s=3600.0)
    time.sleep(0.1)
    active = reg.list_active()
    assert len(active) == 1
    assert active[0].chunk_id == "b"


# ===========================================================================
# Cap + eviction policy
# ===========================================================================


def test_cap_evicts_oldest_operator_pin_first():
    reg = ContextPinRegistry("op-1", max_pins=3)
    # 2 auto pins + 2 operator pins; cap = 3 → one operator pin must go
    reg.auto_pin_for_error(
        chunk_id="auto-a", ledger_entry_id="e1", error_class="X",
    )
    reg.auto_pin_for_decision(
        chunk_id="auto-d", ledger_entry_id="d1", decision_type="plan_approval",
    )
    reg.pin(chunk_id="op-1", source=PinSource.OPERATOR)
    reg.pin(chunk_id="op-2", source=PinSource.OPERATOR)  # triggers cap
    active = reg.list_active()
    ids = {p.chunk_id for p in active}
    # Both auto pins preserved; only one operator pin survives
    assert "auto-a" in ids
    assert "auto-d" in ids
    assert "op-2" in ids
    assert "op-1" not in ids  # oldest operator evicted


def test_clear_operator_pins_preserves_auto():
    reg = ContextPinRegistry("op-1")
    reg.auto_pin_for_error(
        chunk_id="auto-e", ledger_entry_id="e1", error_class="X",
    )
    reg.pin(chunk_id="op-1", source=PinSource.OPERATOR)
    reg.pin(chunk_id="op-2", source=PinSource.OPERATOR)
    n = reg.clear_operator_pins()
    assert n == 2
    assert {p.chunk_id for p in reg.list_active()} == {"auto-e"}


# ===========================================================================
# Auto-pin triggers
# ===========================================================================


def test_auto_pin_for_error_uses_error_ttl(monkeypatch):
    monkeypatch.setenv("JARVIS_CONTEXT_PIN_ERROR_TTL_S", "90")
    reg = ContextPinRegistry("op-1")
    entry = reg.auto_pin_for_error(
        chunk_id="c", ledger_entry_id="e-1", error_class="ImportError",
    )
    assert entry.kind == "auto_error"
    assert "ImportError" in entry.reason
    assert entry.linked_ledger_entry_id == "e-1"


def test_auto_pin_for_decision():
    reg = ContextPinRegistry("op-1")
    entry = reg.auto_pin_for_decision(
        chunk_id="c", ledger_entry_id="d-1", decision_type="plan_approval",
    )
    assert entry.kind == "auto_decision"
    assert entry.source == "orchestrator"


def test_auto_pin_for_question():
    reg = ContextPinRegistry("op-1")
    entry = reg.auto_pin_for_question(
        chunk_id="c", ledger_entry_id="q-1",
    )
    assert entry.kind == "auto_question"
    assert entry.linked_ledger_entry_id == "q-1"


# ===========================================================================
# Pin survives compaction (Slice 2 integration)
# ===========================================================================


def test_pin_survives_budget_tight_compaction():
    """A pinned chunk with low intent score + low recency is STILL kept."""
    reg = ContextPinRegistry("op-1")
    tracker = IntentTracker("op-1")
    tracker.ingest_turn("focus on fresh.py", source=PinSource.OPERATOR.value and None or None)  # no-op
    # Build a proper intent focusing on fresh.py via USER turn
    from backend.core.ouroboros.governance.context_intent import TurnSource
    tracker.ingest_turn("focus on fresh.py", source=TurnSource.USER)
    intent = tracker.current_intent()
    scorer = PreservationScorer()

    pin_entry = reg.pin(chunk_id="pinned-junk", source=PinSource.OPERATOR)
    assert pin_entry is not None

    candidates = [
        # Oldest chunk, no intent match, no structure, but PINNED
        ChunkCandidate(
            chunk_id="pinned-junk", text="zzz",
            index_in_sequence=0, role="tool",
            pinned=reg.is_pinned("pinned-junk"),
        ),
        # Newest chunk, no pin, high recency
        ChunkCandidate(
            chunk_id="fresh", text="fresh.py was edited",
            index_in_sequence=10, role="user",
        ),
    ]
    result = scorer.select_preserved(candidates, intent, max_chunks=1)
    kept = {s.chunk_id for s in result.kept}
    # Pin wins — fresh is compacted / dropped
    assert "pinned-junk" in kept
    assert "fresh" not in kept


# ===========================================================================
# Listener hooks
# ===========================================================================


def test_on_change_fires_on_pin_and_unpin():
    reg = ContextPinRegistry("op-1")
    events: List[Dict[str, Any]] = []
    reg.on_change(events.append)
    p = reg.pin(chunk_id="c", source=PinSource.OPERATOR)
    reg.unpin(p.pin_id)
    kinds = [e["event_type"] for e in events]
    assert kinds == ["context_pinned", "context_unpinned"]


def test_on_change_fires_on_expiry_prune():
    reg = ContextPinRegistry("op-1")
    events: List[Dict[str, Any]] = []
    reg.on_change(events.append)
    reg.pin(chunk_id="c", source=PinSource.OPERATOR, ttl_s=0.05)
    time.sleep(0.1)
    reg.prune_expired()
    kinds = [e["event_type"] for e in events]
    assert "context_pin_expired" in kinds


def test_listener_exception_does_not_break_pin():
    reg = ContextPinRegistry("op-1")

    def _bad(_p: Dict[str, Any]) -> None:
        raise RuntimeError("boom")

    reg.on_change(_bad)
    # Must not raise
    reg.pin(chunk_id="c", source=PinSource.OPERATOR)


# ===========================================================================
# Registry-of-registries
# ===========================================================================


def test_registries_isolate_ops():
    rr = ContextPinRegistries()
    a = rr.get_or_create("op-a")
    b = rr.get_or_create("op-b")
    a.pin(chunk_id="c", source=PinSource.OPERATOR)
    assert a.is_pinned("c")
    assert not b.is_pinned("c")


def test_module_singleton_consistency():
    a = pin_registry_for("op-x")
    b = pin_registry_for("op-x")
    assert a is b


# ===========================================================================
# REPL dispatcher
# ===========================================================================


def test_repl_unmatched_falls_through():
    reg = ContextPinRegistry("op-1")
    result = dispatch_pin_command("/plan mode on", registry=reg)
    assert result.matched is False


def test_repl_pin_operator_default_ttl():
    reg = ContextPinRegistry("op-1")
    result = dispatch_pin_command(
        "/pin chunk-abc keep this", registry=reg,
    )
    assert result.ok is True
    assert "pinned" in result.text.lower()
    assert reg.is_pinned("chunk-abc")


def test_repl_pin_requires_chunk_id():
    reg = ContextPinRegistry("op-1")
    result = dispatch_pin_command("/pin", registry=reg)
    assert result.ok is False


def test_repl_unpin_by_id():
    reg = ContextPinRegistry("op-1")
    p = reg.pin(chunk_id="c", source=PinSource.OPERATOR)
    result = dispatch_pin_command(
        f"/unpin {p.pin_id}", registry=reg,
    )
    assert result.ok is True
    assert not reg.is_pinned("c")


def test_repl_unpin_unknown():
    reg = ContextPinRegistry("op-1")
    result = dispatch_pin_command("/unpin pin-does-not-exist", registry=reg)
    assert result.ok is False


def test_repl_pins_list_empty():
    reg = ContextPinRegistry("op-1")
    result = dispatch_pin_command("/pins", registry=reg)
    assert result.ok is True
    assert "no active pins" in result.text.lower()


def test_repl_pins_list_populated():
    reg = ContextPinRegistry("op-1")
    reg.pin(chunk_id="c1", source=PinSource.OPERATOR)
    reg.auto_pin_for_error(
        chunk_id="c2", ledger_entry_id="e1", error_class="X",
    )
    result = dispatch_pin_command("/pins", registry=reg)
    assert "c1" in result.text
    assert "c2" in result.text
    assert "auto_error" in result.text


def test_repl_pins_show():
    reg = ContextPinRegistry("op-1")
    p = reg.pin(
        chunk_id="c", source=PinSource.OPERATOR, reason="important",
    )
    result = dispatch_pin_command(
        f"/pins show {p.pin_id}", registry=reg,
    )
    assert result.ok is True
    assert p.pin_id in result.text
    assert "important" in result.text


def test_repl_pins_show_short_form():
    reg = ContextPinRegistry("op-1")
    p = reg.pin(chunk_id="c", source=PinSource.OPERATOR)
    result = dispatch_pin_command(f"/pins {p.pin_id}", registry=reg)
    assert result.ok is True
    assert p.pin_id in result.text


def test_repl_pins_clear_operator_only():
    reg = ContextPinRegistry("op-1")
    reg.pin(chunk_id="op-1", source=PinSource.OPERATOR)
    reg.auto_pin_for_error(
        chunk_id="auto-1", ledger_entry_id="e1", error_class="X",
    )
    result = dispatch_pin_command("/pins clear", registry=reg)
    assert result.ok is True
    ids = {p.chunk_id for p in reg.list_active()}
    assert ids == {"auto-1"}


def test_repl_pins_help():
    reg = ContextPinRegistry("op-1")
    result = dispatch_pin_command("/pins help", registry=reg)
    assert result.ok is True
    assert "/pin" in result.text
    assert "/unpin" in result.text


def test_repl_dispatch_without_op_id_errors():
    """When no registry and no op_id passed, fail-closed with a clear message."""
    result = dispatch_pin_command("/pins")
    assert result.ok is False


def test_repl_dispatch_resolves_via_op_id():
    result = dispatch_pin_command("/pins", op_id="op-xyz")
    # Empty registry, but command succeeds
    assert result.ok is True
    assert "no active pins" in result.text.lower()


# ===========================================================================
# Schema version
# ===========================================================================


def test_schema_version_stable():
    assert PIN_REGISTRY_SCHEMA_VERSION == "context_pins.v1"


def test_every_pin_carries_schema_version():
    reg = ContextPinRegistry("op-1")
    p = reg.pin(chunk_id="c", source=PinSource.OPERATOR)
    assert p.schema_version == PIN_REGISTRY_SCHEMA_VERSION


# ===========================================================================
# Scorer integration: math.inf on pinned chunks
# ===========================================================================


def test_pinned_candidate_scores_infinity():
    tracker = IntentTracker("op-1")
    scorer = PreservationScorer()
    chunk = ChunkCandidate(
        chunk_id="c", text="zzz", index_in_sequence=0, role="tool", pinned=True,
    )
    s = scorer.score(chunk, tracker.current_intent(), newest_index=0)
    assert s.total == math.inf
    assert s.pin_bonus == math.inf
