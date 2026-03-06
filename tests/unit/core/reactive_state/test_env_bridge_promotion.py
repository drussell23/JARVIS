"""Tests for EnvBridge promotion readiness and parity stats.

Covers ``is_promotion_ready()`` and ``parity_stats()`` methods that
delegate to the underlying ``ShadowParityLogger`` for promotion gating.
"""
from __future__ import annotations

import os
import time
from typing import Any
from unittest import mock

import pytest

from backend.core.reactive_state.env_bridge import (
    BridgeMode,
    EnvBridge,
)
from backend.core.reactive_state.manifest import build_schema_registry
from backend.core.reactive_state.types import StateEntry
from backend.core.umf.shadow_parity import ShadowParityLogger


# ── Helpers ───────────────────────────────────────────────────────────


def _make_entry(key: str, value: Any, version: int = 1) -> StateEntry:
    """Create a minimal ``StateEntry`` for testing."""
    return StateEntry(
        key=key,
        value=value,
        version=version,
        epoch=1,
        writer="test",
        origin="explicit",
        updated_at_mono=time.monotonic(),
        updated_at_unix_ms=int(time.time() * 1000),
    )


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def schema_registry():
    """Build a fresh schema registry for each test."""
    return build_schema_registry()


# ── TestPromotionReadiness ────────────────────────────────────────────


class TestPromotionReadiness:
    """EnvBridge.is_promotion_ready() delegates to parity logger."""

    def test_not_ready_insufficient_data(self, schema_registry) -> None:
        """Fresh bridge with min_comparisons=100 -> not promotion ready."""
        parity_logger = ShadowParityLogger(parity_threshold=0.999, min_comparisons=100)
        bridge = EnvBridge(
            schema_registry=schema_registry,
            initial_mode=BridgeMode.SHADOW,
            parity_logger=parity_logger,
        )
        assert bridge.is_promotion_ready() is False

    def test_ready_after_sufficient_matching(self, schema_registry) -> None:
        """10 matching comparisons with min_comparisons=10 -> promotion ready."""
        parity_logger = ShadowParityLogger(parity_threshold=0.999, min_comparisons=10)
        bridge = EnvBridge(
            schema_registry=schema_registry,
            initial_mode=BridgeMode.SHADOW,
            parity_logger=parity_logger,
        )
        entry = _make_entry("gcp.offload_active", False)
        with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "false"}, clear=False):
            for rev in range(1, 11):
                bridge.shadow_compare(entry, global_revision=rev)
        assert parity_logger.total_comparisons == 10
        assert parity_logger.mismatches == 0
        assert bridge.is_promotion_ready() is True

    def test_not_ready_too_many_mismatches(self, schema_registry) -> None:
        """10 mismatching comparisons with threshold=0.999 -> not ready."""
        parity_logger = ShadowParityLogger(parity_threshold=0.999, min_comparisons=10)
        bridge = EnvBridge(
            schema_registry=schema_registry,
            initial_mode=BridgeMode.SHADOW,
            parity_logger=parity_logger,
        )
        # store=True but env='false' -> mismatch each time
        entry = _make_entry("gcp.offload_active", True)
        with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "false"}, clear=False):
            for rev in range(1, 11):
                bridge.shadow_compare(entry, global_revision=rev)
        assert parity_logger.total_comparisons == 10
        assert parity_logger.mismatches == 10
        assert bridge.is_promotion_ready() is False


# ── TestParityStats ───────────────────────────────────────────────────


class TestParityStats:
    """EnvBridge.parity_stats() returns aggregated parity information."""

    def test_parity_ratio_starts_at_one(self, schema_registry) -> None:
        """Fresh bridge -> parity_ratio=1.0, total=0, mismatches=0."""
        parity_logger = ShadowParityLogger(parity_threshold=0.999, min_comparisons=100)
        bridge = EnvBridge(
            schema_registry=schema_registry,
            initial_mode=BridgeMode.SHADOW,
            parity_logger=parity_logger,
        )
        stats = bridge.parity_stats()
        assert stats["parity_ratio"] == 1.0
        assert stats["total_comparisons"] == 0
        assert stats["mismatches"] == 0

    def test_parity_ratio_after_mix(self, schema_registry) -> None:
        """1 match + 1 mismatch -> total=2, mismatches=1, parity_ratio ~0.5."""
        parity_logger = ShadowParityLogger(parity_threshold=0.999, min_comparisons=100)
        bridge = EnvBridge(
            schema_registry=schema_registry,
            initial_mode=BridgeMode.SHADOW,
            parity_logger=parity_logger,
        )
        # Match: store=False, env='false'
        entry_match = _make_entry("gcp.offload_active", False)
        with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "false"}, clear=False):
            bridge.shadow_compare(entry_match, global_revision=1)

        # Mismatch: store=True, env='false'
        entry_mismatch = _make_entry("gcp.offload_active", True)
        with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "false"}, clear=False):
            bridge.shadow_compare(entry_mismatch, global_revision=2)

        stats = bridge.parity_stats()
        assert stats["total_comparisons"] == 2
        assert stats["mismatches"] == 1
        assert stats["parity_ratio"] == pytest.approx(0.5)

    def test_recent_diffs_populated(self, schema_registry) -> None:
        """1 mismatch -> recent_diffs has 1 entry with correct category."""
        parity_logger = ShadowParityLogger(parity_threshold=0.999, min_comparisons=100)
        bridge = EnvBridge(
            schema_registry=schema_registry,
            initial_mode=BridgeMode.SHADOW,
            parity_logger=parity_logger,
        )
        # Mismatch: store=True, env='false'
        entry = _make_entry("gcp.offload_active", True)
        with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "false"}, clear=False):
            bridge.shadow_compare(entry, global_revision=1)

        stats = bridge.parity_stats()
        assert len(stats["recent_diffs"]) == 1
        assert stats["recent_diffs"][0]["category"] == "gcp.offload_active"
