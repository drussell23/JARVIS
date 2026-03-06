"""Tests for EnvBridge per-domain kill switches (Appendix A.13)."""
from __future__ import annotations

import os
import time
from typing import Any
from unittest import mock

import pytest

from backend.core.reactive_state.env_bridge import BridgeMode, EnvBridge
from backend.core.reactive_state.manifest import build_schema_registry
from backend.core.reactive_state.types import StateEntry


# -- Helpers ----------------------------------------------------------------


def _make_entry(key: str, value: Any, version: int = 1) -> StateEntry:
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


# -- Fixtures ---------------------------------------------------------------


@pytest.fixture()
def schema_registry():
    return build_schema_registry()


# -- TestActiveDomainsParsing -----------------------------------------------


class TestActiveDomainsParsing:
    """JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS env var parsing."""

    def test_absent_env_var_means_all_active(self, schema_registry) -> None:
        """No env var -> _active_domains is None -> all domains active."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS", None)
            bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
            assert bridge.is_domain_active("gcp.offload_active") is True
            assert bridge.is_domain_active("memory.tier") is True
            assert bridge.is_domain_active("lifecycle.startup_complete") is True

    def test_empty_env_var_means_all_active(self, schema_registry) -> None:
        """Empty string -> all domains active."""
        with mock.patch.dict(os.environ, {"JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS": ""}, clear=False):
            bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
            assert bridge.is_domain_active("gcp.offload_active") is True

    def test_specific_domains_parsed(self, schema_registry) -> None:
        """'gcp,memory' -> only gcp and memory are active."""
        with mock.patch.dict(
            os.environ,
            {"JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS": "gcp,memory"},
            clear=False,
        ):
            bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
            assert bridge.is_domain_active("gcp.offload_active") is True
            assert bridge.is_domain_active("gcp.node_ip") is True
            assert bridge.is_domain_active("memory.available_gb") is True
            assert bridge.is_domain_active("lifecycle.startup_complete") is False
            assert bridge.is_domain_active("prime.early_pid") is False
            assert bridge.is_domain_active("service.backend_minimal") is False

    def test_whitespace_stripped(self, schema_registry) -> None:
        """'  gcp , memory  ' -> parsed correctly."""
        with mock.patch.dict(
            os.environ,
            {"JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS": "  gcp , memory  "},
            clear=False,
        ):
            bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
            assert bridge.is_domain_active("gcp.offload_active") is True
            assert bridge.is_domain_active("memory.tier") is True
            assert bridge.is_domain_active("lifecycle.effective_mode") is False


# -- TestMirrorDomainFiltering ----------------------------------------------


class TestMirrorDomainFiltering:
    """mirror_to_env respects per-domain kill switches."""

    def test_mirror_skips_inactive_domain(self, schema_registry) -> None:
        """Key in inactive domain -> mirror_to_env returns False."""
        with mock.patch.dict(
            os.environ,
            {"JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS": "gcp"},
            clear=False,
        ):
            bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
            # memory domain is NOT active
            entry = _make_entry("memory.available_gb", 7.5)
            result = bridge.mirror_to_env(entry)
            assert result is False

    def test_mirror_works_for_active_domain(self, schema_registry) -> None:
        """Key in active domain -> mirror_to_env writes to env."""
        with mock.patch.dict(
            os.environ,
            {"JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS": "gcp"},
            clear=False,
        ):
            bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
            entry = _make_entry("gcp.offload_active", True)
            result = bridge.mirror_to_env(entry)
            assert result is True
            assert os.environ["JARVIS_GCP_OFFLOAD_ACTIVE"] == "true"

    def test_all_domains_active_when_no_env_var(self, schema_registry) -> None:
        """No JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS -> all keys mirror."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS", None)
            bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
            entry = _make_entry("lifecycle.startup_complete", True)
            result = bridge.mirror_to_env(entry)
            assert result is True
            assert os.environ["JARVIS_STARTUP_COMPLETE"] == "true"
