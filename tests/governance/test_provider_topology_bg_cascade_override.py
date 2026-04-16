"""Test the JARVIS_TOPOLOGY_BG_CASCADE_ENABLED dev/verification override.

The default topology (loaded from brain_selection_policy.yaml) declares
``background`` and ``speculative`` routes with ``block_mode:
skip_and_queue`` to protect Claude compute budget under normal
operation. This env lets verification sessions override that on-demand
without editing committed YAML.
"""
from __future__ import annotations

import os

import pytest

from backend.core.ouroboros.governance import provider_topology as pt


@pytest.fixture(autouse=True)
def _reset_env_and_warned(monkeypatch):
    monkeypatch.delenv("JARVIS_TOPOLOGY_BG_CASCADE_ENABLED", raising=False)
    # Reset the "warned-once" guard so each test sees a clean slate.
    pt._BG_OVERRIDE_WARNED = False
    yield
    pt._BG_OVERRIDE_WARNED = False


def _make_topology_with_skip_and_queue():
    """Build a ProviderTopology whose ``background`` route is
    ``skip_and_queue`` — mirrors the real brain_selection_policy.yaml."""
    return pt.ProviderTopology(
        enabled=True,
        routes={
            "background": pt.RouteTopology(
                dw_allowed=False,
                dw_model=None,
                block_mode="skip_and_queue",
                reason="test",
            ),
            "immediate": pt.RouteTopology(
                dw_allowed=False,
                dw_model=None,
                block_mode="cascade_to_claude",
                reason="test",
            ),
        },
    )


def test_default_background_is_skip_and_queue():
    topo = _make_topology_with_skip_and_queue()
    assert topo.block_mode_for_route("background") == "skip_and_queue"


def test_override_flips_background_to_cascade(monkeypatch):
    monkeypatch.setenv("JARVIS_TOPOLOGY_BG_CASCADE_ENABLED", "true")
    topo = _make_topology_with_skip_and_queue()
    assert topo.block_mode_for_route("background") == "cascade_to_claude"


def test_override_preserves_already_cascade_routes(monkeypatch):
    """IMMEDIATE / COMPLEX routes should be unaffected (they're already cascading)."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_BG_CASCADE_ENABLED", "true")
    topo = _make_topology_with_skip_and_queue()
    assert topo.block_mode_for_route("immediate") == "cascade_to_claude"


def test_override_off_default_unchanged(monkeypatch):
    """With the env unset, skip_and_queue routes remain skip_and_queue."""
    monkeypatch.delenv("JARVIS_TOPOLOGY_BG_CASCADE_ENABLED", raising=False)
    topo = _make_topology_with_skip_and_queue()
    assert topo.block_mode_for_route("background") == "skip_and_queue"


def test_override_warns_once_per_process(monkeypatch, caplog):
    import logging
    monkeypatch.setenv("JARVIS_TOPOLOGY_BG_CASCADE_ENABLED", "true")
    caplog.set_level(logging.WARNING, logger="backend.core.ouroboros.governance.provider_topology")
    topo = _make_topology_with_skip_and_queue()
    # First call fires the WARN.
    topo.block_mode_for_route("background")
    first_warn_count = sum(
        1 for r in caplog.records
        if "JARVIS_TOPOLOGY_BG_CASCADE_ENABLED" in r.getMessage()
    )
    assert first_warn_count == 1, f"expected 1 WARN on first override, got {first_warn_count}"
    # Subsequent calls MUST NOT re-warn — the "warned-once" latch holds.
    for _ in range(5):
        topo.block_mode_for_route("background")
    final_warn_count = sum(
        1 for r in caplog.records
        if "JARVIS_TOPOLOGY_BG_CASCADE_ENABLED" in r.getMessage()
    )
    assert final_warn_count == 1, (
        f"warn-once latch failed — got {final_warn_count} warnings after 6 override calls"
    )


def test_env_value_variants(monkeypatch):
    topo = _make_topology_with_skip_and_queue()
    for truthy in ("true", "1", "yes", "on", "TRUE", "True"):
        monkeypatch.setenv("JARVIS_TOPOLOGY_BG_CASCADE_ENABLED", truthy)
        pt._BG_OVERRIDE_WARNED = False
        assert topo.block_mode_for_route("background") == "cascade_to_claude", truthy
    for falsey in ("false", "0", "no", "off", ""):
        monkeypatch.setenv("JARVIS_TOPOLOGY_BG_CASCADE_ENABLED", falsey)
        assert topo.block_mode_for_route("background") == "skip_and_queue", falsey


def test_disabled_topology_no_override_effect(monkeypatch):
    """Override is a no-op on a disabled topology (enabled=False short-circuits first)."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_BG_CASCADE_ENABLED", "true")
    empty = pt.ProviderTopology(enabled=False)
    # block_mode_for_route returns "cascade_to_claude" anyway when disabled —
    # not because of the override, but because that's the safe default.
    assert empty.block_mode_for_route("background") == "cascade_to_claude"
