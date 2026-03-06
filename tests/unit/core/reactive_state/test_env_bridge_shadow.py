"""Tests for EnvBridge shadow comparison logic with canonical parity tracking.

Covers ``shadow_compare()``, ``_canonicalize()``, and ``_values_equal()``
across all value types, absent-env defaults, sensitive redaction, and
mode-gating (LEGACY skips, unmapped keys ignored).
"""
from __future__ import annotations

import os
import time
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


def _make_entry(key: str, value: object, version: int = 1) -> StateEntry:
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


@pytest.fixture()
def parity_logger():
    """Create a fresh ShadowParityLogger for each test."""
    return ShadowParityLogger()


@pytest.fixture()
def shadow_bridge(schema_registry, parity_logger):
    """Create an EnvBridge in SHADOW mode with a fresh parity logger."""
    return EnvBridge(
        schema_registry=schema_registry,
        initial_mode=BridgeMode.SHADOW,
        parity_logger=parity_logger,
    )


# ── TestShadowComparison ─────────────────────────────────────────────


class TestShadowComparison:
    """shadow_compare records parity between env vars and store values."""

    def test_matching_bool_records_parity(
        self, shadow_bridge: EnvBridge, parity_logger: ShadowParityLogger,
    ) -> None:
        """env='true', store=True -> 0 mismatches, 1 total."""
        entry = _make_entry("gcp.offload_active", True)
        with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "true"}, clear=False):
            shadow_bridge.shadow_compare(entry, global_revision=1)
        assert parity_logger.total_comparisons == 1
        assert parity_logger.mismatches == 0

    def test_mismatching_bool_records_mismatch(
        self, shadow_bridge: EnvBridge, parity_logger: ShadowParityLogger,
    ) -> None:
        """env='false', store=True -> 1 mismatch."""
        entry = _make_entry("gcp.offload_active", True)
        with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "false"}, clear=False):
            shadow_bridge.shadow_compare(entry, global_revision=2)
        assert parity_logger.total_comparisons == 1
        assert parity_logger.mismatches == 1

    def test_matching_int(
        self, shadow_bridge: EnvBridge, parity_logger: ShadowParityLogger,
    ) -> None:
        """env='8000', store=8000 -> 0 mismatches."""
        entry = _make_entry("gcp.node_port", 8000)
        with mock.patch.dict(os.environ, {"JARVIS_GCP_NODE_PORT": "8000"}, clear=False):
            shadow_bridge.shadow_compare(entry, global_revision=3)
        assert parity_logger.total_comparisons == 1
        assert parity_logger.mismatches == 0

    def test_matching_float(
        self, shadow_bridge: EnvBridge, parity_logger: ShadowParityLogger,
    ) -> None:
        """env='7.5', store=7.5 -> 0 mismatches."""
        entry = _make_entry("memory.available_gb", 7.5)
        with mock.patch.dict(os.environ, {"JARVIS_MEMORY_AVAILABLE_GB": "7.5"}, clear=False):
            shadow_bridge.shadow_compare(entry, global_revision=4)
        assert parity_logger.total_comparisons == 1
        assert parity_logger.mismatches == 0

    def test_absent_env_uses_schema_default(
        self, shadow_bridge: EnvBridge, parity_logger: ShadowParityLogger,
    ) -> None:
        """No env var, store=False (default for gcp.offload_active) -> 0 mismatches."""
        entry = _make_entry("gcp.offload_active", False)
        # Ensure the env var is absent
        env = {k: v for k, v in os.environ.items() if k != "JARVIS_GCP_OFFLOAD_ACTIVE"}
        with mock.patch.dict(os.environ, env, clear=True):
            shadow_bridge.shadow_compare(entry, global_revision=5)
        assert parity_logger.total_comparisons == 1
        assert parity_logger.mismatches == 0

    def test_absent_env_mismatch_with_non_default(
        self, shadow_bridge: EnvBridge, parity_logger: ShadowParityLogger,
    ) -> None:
        """No env var, store=True (non-default for gcp.offload_active) -> 1 mismatch."""
        entry = _make_entry("gcp.offload_active", True)
        env = {k: v for k, v in os.environ.items() if k != "JARVIS_GCP_OFFLOAD_ACTIVE"}
        with mock.patch.dict(os.environ, env, clear=True):
            shadow_bridge.shadow_compare(entry, global_revision=6)
        assert parity_logger.total_comparisons == 1
        assert parity_logger.mismatches == 1

    def test_unmapped_key_is_ignored(
        self, shadow_bridge: EnvBridge, parity_logger: ShadowParityLogger,
    ) -> None:
        """unknown.key -> 0 total comparisons (silently ignored)."""
        entry = _make_entry("unknown.key", "whatever")
        shadow_bridge.shadow_compare(entry, global_revision=7)
        assert parity_logger.total_comparisons == 0

    def test_legacy_mode_skips_comparison(
        self, schema_registry, parity_logger: ShadowParityLogger,
    ) -> None:
        """LEGACY mode -> 0 total comparisons (no-op)."""
        bridge = EnvBridge(
            schema_registry=schema_registry,
            initial_mode=BridgeMode.LEGACY,
            parity_logger=parity_logger,
        )
        entry = _make_entry("gcp.offload_active", True)
        with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "true"}, clear=False):
            bridge.shadow_compare(entry, global_revision=8)
        assert parity_logger.total_comparisons == 0

    def test_nullable_int_absent_matches_none(
        self, shadow_bridge: EnvBridge, parity_logger: ShadowParityLogger,
    ) -> None:
        """No env var for prime.early_pid, store=None -> 0 mismatches."""
        entry = _make_entry("prime.early_pid", None)
        env = {k: v for k, v in os.environ.items() if k != "JARVIS_PRIME_EARLY_PID"}
        with mock.patch.dict(os.environ, env, clear=True):
            shadow_bridge.shadow_compare(entry, global_revision=9)
        assert parity_logger.total_comparisons == 1
        assert parity_logger.mismatches == 0


# ── TestShadowCompareEnum ────────────────────────────────────────────


class TestShadowCompareEnum:
    """shadow_compare handles enum-typed keys correctly."""

    def test_matching_enum(
        self, shadow_bridge: EnvBridge, parity_logger: ShadowParityLogger,
    ) -> None:
        """env='cloud_first', store='cloud_first' -> 0 mismatches."""
        entry = _make_entry("lifecycle.effective_mode", "cloud_first")
        with mock.patch.dict(os.environ, {"JARVIS_EFFECTIVE_MODE": "cloud_first"}, clear=False):
            shadow_bridge.shadow_compare(entry, global_revision=10)
        assert parity_logger.total_comparisons == 1
        assert parity_logger.mismatches == 0

    def test_enum_whitespace_stripped(
        self, shadow_bridge: EnvBridge, parity_logger: ShadowParityLogger,
    ) -> None:
        """env='  cloud_first  ', store='cloud_first' -> 0 mismatches."""
        entry = _make_entry("lifecycle.effective_mode", "cloud_first")
        with mock.patch.dict(os.environ, {"JARVIS_EFFECTIVE_MODE": "  cloud_first  "}, clear=False):
            shadow_bridge.shadow_compare(entry, global_revision=11)
        assert parity_logger.total_comparisons == 1
        assert parity_logger.mismatches == 0
