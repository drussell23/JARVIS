"""Tests for IntentSignal dataclass and DedupTracker.

Validates the foundational data model for JARVIS's Intent Engine (Layer 1).
IntentSignal is a frozen dataclass carrying signal metadata, and DedupTracker
prevents duplicate signals from being processed within a configurable cooldown.
"""
from __future__ import annotations

import time
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from backend.core.ouroboros.governance.intent.signals import (
    DedupTracker,
    IntentSignal,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signal(
    *,
    source: str = "intent:test_failure",
    target_files: tuple[str, ...] = ("backend/core/foo.py",),
    repo: str = "jarvis",
    description: str = "test failed in foo",
    evidence: dict | None = None,
    confidence: float = 0.9,
    stable: bool = True,
) -> IntentSignal:
    """Factory for concise signal construction in tests."""
    if evidence is None:
        evidence = {"signature": "ValueError:foo:42"}
    return IntentSignal(
        source=source,
        target_files=target_files,
        repo=repo,
        description=description,
        evidence=evidence,
        confidence=confidence,
        stable=stable,
    )


# ---------------------------------------------------------------------------
# IntentSignal tests
# ---------------------------------------------------------------------------


class TestIntentSignalCreation:
    """test_intent_signal_creation"""

    def test_all_fields_populated(self):
        sig = _make_signal()
        assert sig.source == "intent:test_failure"
        assert sig.target_files == ("backend/core/foo.py",)
        assert sig.repo == "jarvis"
        assert sig.description == "test failed in foo"
        assert sig.evidence == {"signature": "ValueError:foo:42"}
        assert sig.confidence == 0.9
        assert sig.stable is True

    def test_auto_generated_signal_id(self):
        sig = _make_signal()
        # signal_id should start with the prefix used in generate_operation_id
        assert sig.signal_id.startswith("op-")
        assert "sig" in sig.signal_id

    def test_auto_generated_timestamp(self):
        before = datetime.now(timezone.utc)
        sig = _make_signal()
        after = datetime.now(timezone.utc)
        assert before <= sig.timestamp <= after

    def test_dedup_key_is_hex_string_16_chars(self):
        sig = _make_signal()
        key = sig.dedup_key
        assert isinstance(key, str)
        assert len(key) == 16
        # Must be valid hex
        int(key, 16)


class TestIntentSignalFrozen:
    """test_intent_signal_frozen"""

    def test_cannot_mutate_source(self):
        sig = _make_signal()
        with pytest.raises(FrozenInstanceError):
            sig.source = "intent:stack_trace"  # type: ignore[misc]

    def test_cannot_mutate_confidence(self):
        sig = _make_signal()
        with pytest.raises(FrozenInstanceError):
            sig.confidence = 0.5  # type: ignore[misc]


class TestDedupKeySameForIdenticalSignals:
    """test_dedup_key_same_for_identical_signals

    Same file + same signature = same key regardless of description/confidence.
    """

    def test_same_key_different_description(self):
        sig_a = _make_signal(description="first description", confidence=0.8)
        sig_b = _make_signal(description="totally different", confidence=0.3)
        assert sig_a.dedup_key == sig_b.dedup_key

    def test_same_key_different_source(self):
        sig_a = _make_signal(source="intent:test_failure")
        sig_b = _make_signal(source="intent:stack_trace")
        assert sig_a.dedup_key == sig_b.dedup_key


class TestDedupKeyDiffersForDifferentFiles:
    """test_dedup_key_differs_for_different_files"""

    def test_different_files_different_key(self):
        sig_a = _make_signal(target_files=("backend/core/foo.py",))
        sig_b = _make_signal(target_files=("backend/core/bar.py",))
        assert sig_a.dedup_key != sig_b.dedup_key

    def test_different_signature_different_key(self):
        sig_a = _make_signal(evidence={"signature": "ValueError:foo:42"})
        sig_b = _make_signal(evidence={"signature": "KeyError:foo:99"})
        assert sig_a.dedup_key != sig_b.dedup_key

    def test_different_repo_different_key(self):
        sig_a = _make_signal(repo="jarvis")
        sig_b = _make_signal(repo="prime")
        assert sig_a.dedup_key != sig_b.dedup_key


# ---------------------------------------------------------------------------
# DedupTracker tests
# ---------------------------------------------------------------------------


class TestDedupTrackerBlocksDuplicateWithinCooldown:
    """test_dedup_tracker_blocks_duplicate_within_cooldown"""

    def test_first_signal_is_new(self):
        tracker = DedupTracker(cooldown_s=300.0)
        sig = _make_signal()
        assert tracker.is_new(sig) is True

    def test_second_identical_signal_is_blocked(self):
        tracker = DedupTracker(cooldown_s=300.0)
        sig_a = _make_signal()
        sig_b = _make_signal()  # same dedup_key
        tracker.is_new(sig_a)
        assert tracker.is_new(sig_b) is False

    def test_different_signal_passes(self):
        tracker = DedupTracker(cooldown_s=300.0)
        sig_a = _make_signal(target_files=("a.py",))
        sig_b = _make_signal(target_files=("b.py",))
        tracker.is_new(sig_a)
        assert tracker.is_new(sig_b) is True


class TestDedupTrackerAllowsAfterCooldown:
    """test_dedup_tracker_allows_after_cooldown"""

    def test_allows_same_signal_after_cooldown_expires(self):
        tracker = DedupTracker(cooldown_s=0.0)
        sig = _make_signal()
        assert tracker.is_new(sig) is True
        time.sleep(0.01)
        assert tracker.is_new(sig) is True

    def test_clear_resets_state(self):
        tracker = DedupTracker(cooldown_s=300.0)
        sig = _make_signal()
        tracker.is_new(sig)
        assert tracker.is_new(sig) is False
        tracker.clear()
        assert tracker.is_new(sig) is True


class TestCrossSignalDedupTestFailureWinsOverStackTrace:
    """test_cross_signal_dedup_test_failure_wins_over_stack_trace

    Same file + same signature from test_failure source blocks subsequent
    stack_trace source because dedup_key is source-agnostic.
    """

    def test_test_failure_blocks_stack_trace(self):
        tracker = DedupTracker(cooldown_s=300.0)
        test_fail_sig = _make_signal(
            source="intent:test_failure",
            target_files=("backend/core/engine.py",),
            evidence={"signature": "AssertionError:engine:100"},
        )
        stack_sig = _make_signal(
            source="intent:stack_trace",
            target_files=("backend/core/engine.py",),
            evidence={"signature": "AssertionError:engine:100"},
        )
        assert tracker.is_new(test_fail_sig) is True
        assert tracker.is_new(stack_sig) is False

    def test_stack_trace_also_blocks_test_failure(self):
        """Dedup is bidirectional — whichever arrives first wins."""
        tracker = DedupTracker(cooldown_s=300.0)
        stack_sig = _make_signal(
            source="intent:stack_trace",
            target_files=("backend/core/engine.py",),
            evidence={"signature": "RuntimeError:engine:55"},
        )
        test_fail_sig = _make_signal(
            source="intent:test_failure",
            target_files=("backend/core/engine.py",),
            evidence={"signature": "RuntimeError:engine:55"},
        )
        assert tracker.is_new(stack_sig) is True
        assert tracker.is_new(test_fail_sig) is False
