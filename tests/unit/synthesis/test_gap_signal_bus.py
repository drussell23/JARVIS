"""
Tests for GapSignalBus and CapabilityGapEvent.

Written before implementation (TDD / Task 1).
"""
from __future__ import annotations

import asyncio
import logging

import pytest

# ---------------------------------------------------------------------------
# Import under test — will raise ModuleNotFoundError until implemented
# ---------------------------------------------------------------------------
from backend.neural_mesh.synthesis.gap_signal_bus import (
    CapabilityGapEvent,
    GapSignalBus,
    get_gap_signal_bus,
)


# ===========================================================================
# CapabilityGapEvent — domain_id
# ===========================================================================

def test_domain_id_normalised():
    """'Browser Navigation' + 'Notion' -> 'browser_navigation:notion'"""
    event = CapabilityGapEvent(
        goal="open a page",
        task_type="Browser Navigation",
        target_app="Notion",
        source="test",
    )
    assert event.domain_id == "browser_navigation:notion"


def test_domain_id_empty_app():
    """Empty target_app should fall back to 'any'."""
    event = CapabilityGapEvent(
        goal="do something visual",
        task_type="Vision Action",
        target_app="",
        source="test",
    )
    assert event.domain_id == "vision_action:any"


# ===========================================================================
# CapabilityGapEvent — dedupe_key
# ===========================================================================

def test_dedupe_key_is_hex16():
    """dedupe_key must be exactly 16 lowercase hex chars."""
    event = CapabilityGapEvent(
        goal="irrelevant",
        task_type="Web Scraping",
        target_app="Chrome",
        source="test",
    )
    key = event.dedupe_key
    assert len(key) == 16
    # valid hex: all chars in 0-9a-f
    assert all(c in "0123456789abcdef" for c in key)


def test_dedupe_key_stable():
    """Identical inputs always produce the same dedupe_key."""
    kwargs = dict(goal="g", task_type="File System", target_app="Finder", source="s")
    e1 = CapabilityGapEvent(**kwargs)
    e2 = CapabilityGapEvent(**kwargs)
    assert e1.dedupe_key == e2.dedupe_key


def test_dedupe_key_varies_by_domain():
    """Different task_type+target_app combos must produce different keys."""
    e1 = CapabilityGapEvent(goal="g", task_type="Task A", target_app="App1", source="s")
    e2 = CapabilityGapEvent(goal="g", task_type="Task B", target_app="App2", source="s")
    assert e1.dedupe_key != e2.dedupe_key


# ===========================================================================
# GapSignalBus — basic queue operations
# ===========================================================================

def test_emit_and_qsize():
    """emit() should add an event to the queue (qsize increases)."""
    bus = GapSignalBus(maxsize=10)
    assert bus.qsize() == 0

    event = CapabilityGapEvent(
        goal="open a doc",
        task_type="Document Editing",
        target_app="Word",
        source="unit_test",
    )
    bus.emit(event)
    assert bus.qsize() == 1


def test_emit_drops_on_full(caplog):
    """When the queue is full, emit() drops the event and logs a warning."""
    bus = GapSignalBus(maxsize=2)

    e1 = CapabilityGapEvent(goal="g1", task_type="T", target_app="A", source="s")
    e2 = CapabilityGapEvent(goal="g2", task_type="T", target_app="A", source="s")
    e3 = CapabilityGapEvent(goal="g3", task_type="T", target_app="A", source="s")  # overflow

    bus.emit(e1)
    bus.emit(e2)

    with caplog.at_level(logging.WARNING, logger="backend.neural_mesh.synthesis.gap_signal_bus"):
        bus.emit(e3)  # should drop + warn

    assert bus.qsize() == 2  # still 2 — overflow was dropped
    assert any("drop" in rec.message.lower() or "full" in rec.message.lower()
               for rec in caplog.records)


# ===========================================================================
# Singleton
# ===========================================================================

def test_singleton():
    """get_gap_signal_bus() must return the same instance every call."""
    a = get_gap_signal_bus()
    b = get_gap_signal_bus()
    assert a is b
