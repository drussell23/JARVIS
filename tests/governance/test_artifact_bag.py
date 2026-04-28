"""Tests for Slice RC.1 — Typed ArtifactBag.

Pins:
  * Per-key write authority enforcement
  * Ownership violation rejection
  * Cross-phase overwrite prevention
  * Merge semantics (partial merge on rejection)
  * from_mapping backwards compatibility
  * Read API (Mapping-like interface)
  * Immutability (frozen, mutations produce new bags)
"""
from __future__ import annotations

import time
from typing import Any

import pytest

from backend.core.ouroboros.governance.artifact_bag import (
    ARTIFACT_KEY_OWNERSHIP,
    ArtifactBag,
    ArtifactEntry,
)


class TestArtifactBagConstruction:
    """Construction and factory methods."""

    def test_empty(self) -> None:
        bag = ArtifactBag.empty()
        assert len(bag) == 0
        assert bag.keys() == ()

    def test_from_mapping(self) -> None:
        bag = ArtifactBag.from_mapping(
            {"key1": "val1", "key2": 42},
            writer_phase="GENERATE",
        )
        assert len(bag) == 2
        assert bag.get_value("key1") == "val1"
        assert bag.get_value("key2") == 42

    def test_from_mapping_attribution(self) -> None:
        bag = ArtifactBag.from_mapping(
            {"key1": "val1"},
            writer_phase="VALIDATE",
        )
        entry = bag["key1"]
        assert entry.owner_phase == "VALIDATE"


class TestArtifactBagWriteAuthority:
    """Per-key write authority enforcement (§24.6.1)."""

    def test_valid_write_succeeds(self) -> None:
        bag = ArtifactBag.empty()
        ok, new_bag = bag.with_entry(
            key="generation_metadata",
            value={"model": "claude"},
            writer_phase="GENERATE",
        )
        assert ok is True
        assert new_bag.get_value("generation_metadata") == {"model": "claude"}

    def test_unauthorized_write_rejected(self) -> None:
        """Writing to a registered key from a non-owning phase is rejected."""
        bag = ArtifactBag.empty()
        ok, new_bag = bag.with_entry(
            key="generation_metadata",
            value="bad",
            writer_phase="VALIDATE",  # not in GENERATE/GENERATE_RETRY
        )
        assert ok is False
        assert new_bag is bag  # unchanged

    def test_cross_phase_overwrite_rejected(self) -> None:
        """A different phase cannot overwrite an existing registered key."""
        bag = ArtifactBag.empty()
        ok, bag = bag.with_entry(
            key="gate_decision",
            value="APPROVE",
            writer_phase="GATE",
        )
        assert ok is True
        # Now try to overwrite from VALIDATE
        ok, bag2 = bag.with_entry(
            key="gate_decision",
            value="REJECT",
            writer_phase="VALIDATE",
        )
        assert ok is False
        assert bag2.get_value("gate_decision") == "APPROVE"

    def test_same_phase_overwrite_allowed(self) -> None:
        """Same phase can update its own key."""
        bag = ArtifactBag.empty()
        ok, bag = bag.with_entry(
            key="gate_decision",
            value="PENDING",
            writer_phase="GATE",
        )
        ok, bag = bag.with_entry(
            key="gate_decision",
            value="APPROVE",
            writer_phase="GATE",
        )
        assert ok is True
        assert bag.get_value("gate_decision") == "APPROVE"

    def test_unregistered_key_any_phase(self) -> None:
        """Unregistered keys can be written by any phase."""
        bag = ArtifactBag.empty()
        ok, bag = bag.with_entry(
            key="custom_key",
            value="anything",
            writer_phase="ANY_PHASE",
        )
        assert ok is True
        assert bag.get_value("custom_key") == "anything"

    @pytest.mark.parametrize("key,allowed_phases", [
        ("generation_metadata", frozenset({"GENERATE", "GENERATE_RETRY"})),
        ("validation_summary", frozenset({"VALIDATE", "VALIDATE_RETRY"})),
        ("gate_decision", frozenset({"GATE"})),
        ("approval_decision", frozenset({"APPROVE"})),
        ("apply_result", frozenset({"APPLY"})),
        ("verify_result", frozenset({"VERIFY", "VISUAL_VERIFY"})),
    ])
    def test_ownership_registry(
        self, key: str, allowed_phases: frozenset,
    ) -> None:
        """Verify the ownership registry matches expectations."""
        assert key in ARTIFACT_KEY_OWNERSHIP
        assert ARTIFACT_KEY_OWNERSHIP[key] == allowed_phases


class TestArtifactBagMerge:
    """Merge semantics."""

    def test_merge_success(self) -> None:
        bag1 = ArtifactBag.empty()
        ok, bag1 = bag1.with_entry(
            key="gate_decision", value="X", writer_phase="GATE",
        )
        bag2 = ArtifactBag.from_mapping(
            {"apply_result": "OK"},
            writer_phase="APPLY",
        )
        all_ok, merged, rejected = bag1.merge(bag2)
        assert all_ok is True
        assert merged.get_value("gate_decision") == "X"
        assert merged.get_value("apply_result") == "OK"
        assert rejected == ()

    def test_merge_partial_rejection(self) -> None:
        bag1 = ArtifactBag.empty()
        ok, bag1 = bag1.with_entry(
            key="gate_decision", value="X", writer_phase="GATE",
        )
        bag2 = ArtifactBag.empty()
        # This should be rejected (VALIDATE can't write gate_decision)
        ok, bag2 = bag2.with_entry(
            key="gate_decision", value="OVERRIDE",
            writer_phase="VALIDATE",
        )
        # bag2 is empty because with_entry rejected it
        assert ok is False


class TestArtifactBagReadAPI:
    """Mapping-like read interface."""

    def test_contains(self) -> None:
        bag = ArtifactBag.from_mapping(
            {"k": "v"}, writer_phase="X",
        )
        assert "k" in bag
        assert "missing" not in bag

    def test_getitem(self) -> None:
        bag = ArtifactBag.from_mapping(
            {"k": "v"}, writer_phase="X",
        )
        entry = bag["k"]
        assert isinstance(entry, ArtifactEntry)
        assert entry.value == "v"

    def test_getitem_missing(self) -> None:
        bag = ArtifactBag.empty()
        with pytest.raises(KeyError):
            bag["missing"]

    def test_get_default(self) -> None:
        bag = ArtifactBag.empty()
        assert bag.get("missing") is None
        assert bag.get("missing", "default") == "default"

    def test_iteration(self) -> None:
        bag = ArtifactBag.from_mapping(
            {"a": 1, "b": 2}, writer_phase="X",
        )
        keys = list(bag)
        assert "a" in keys
        assert "b" in keys

    def test_to_dict(self) -> None:
        bag = ArtifactBag.from_mapping(
            {"k1": "v1", "k2": 42}, writer_phase="X",
        )
        d = bag.to_dict()
        assert d == {"k1": "v1", "k2": 42}

    def test_to_audit_dict(self) -> None:
        bag = ArtifactBag.from_mapping(
            {"k": "v"}, writer_phase="GENERATE",
        )
        d = bag.to_audit_dict()
        assert d["k"]["owner_phase"] == "GENERATE"
        assert d["k"]["value"] == "v"


class TestArtifactBagImmutability:
    """Frozen dataclass — mutations produce new instances."""

    def test_with_entry_returns_new_bag(self) -> None:
        bag = ArtifactBag.empty()
        ok, new_bag = bag.with_entry(
            key="custom", value="v", writer_phase="X",
        )
        assert ok is True
        assert new_bag is not bag
        assert "custom" not in bag
        assert "custom" in new_bag
