"""
Tests for the public coordinator API in backend.neural_mesh.integration.

Covers:
- set_neural_mesh_coordinator / get_neural_mesh_coordinator round-trip
- mark_neural_mesh_initialized toggle
- Fallback to neural_mesh_coordinator._coordinator when integration singleton is None
- Skips stopped coordinator (when _running=False)
- _is_agent_set_registered / _mark_agent_set_registered idempotency
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest


class TestCoordinatorAPI:
    """Tests for the canonical coordinator accessor and helpers."""

    def setup_method(self):
        import backend.neural_mesh.integration as mod
        mod._neural_mesh_coordinator = None
        mod._initialized = False
        mod._production_agents_registered = {}

    # ── set / get round-trip ────────────────────────────────────────────

    def test_set_coordinator_makes_it_retrievable(self):
        import backend.neural_mesh.integration as mod

        fake = MagicMock(name="FakeCoordinator")
        mod.set_neural_mesh_coordinator(fake)
        assert mod.get_neural_mesh_coordinator() is fake

    def test_set_coordinator_none_clears(self):
        import backend.neural_mesh.integration as mod

        fake = MagicMock(name="FakeCoordinator")
        mod.set_neural_mesh_coordinator(fake)
        assert mod.get_neural_mesh_coordinator() is fake

        mod.set_neural_mesh_coordinator(None)
        # With integration singleton None, fallback path runs.
        # If the fallback also returns None, we get None.
        assert mod.get_neural_mesh_coordinator() is None

    # ── mark_neural_mesh_initialized ────────────────────────────────────

    def test_mark_neural_mesh_initialized_toggles(self):
        import backend.neural_mesh.integration as mod

        assert mod._initialized is False
        mod.mark_neural_mesh_initialized(True)
        assert mod._initialized is True
        mod.mark_neural_mesh_initialized(False)
        assert mod._initialized is False

    def test_mark_neural_mesh_initialized_default_true(self):
        import backend.neural_mesh.integration as mod

        mod.mark_neural_mesh_initialized()
        assert mod._initialized is True

    # ── fallback to coordinator module ──────────────────────────────────

    def test_fallback_to_coordinator_module(self, monkeypatch):
        """When integration singleton is None, fall back to
        neural_mesh.neural_mesh_coordinator._coordinator if it is running."""
        import backend.neural_mesh.integration as mod

        # Create a fake coordinator module with a running coordinator
        fake_coordinator = MagicMock(name="FallbackCoordinator")
        fake_coordinator._running = True

        fake_module = types.ModuleType("neural_mesh.neural_mesh_coordinator")
        fake_module._coordinator = fake_coordinator

        monkeypatch.setitem(sys.modules, "neural_mesh.neural_mesh_coordinator", fake_module)

        # Integration singleton is None (set in setup_method)
        assert mod._neural_mesh_coordinator is None
        result = mod.get_neural_mesh_coordinator()
        assert result is fake_coordinator

    def test_skips_stopped_coordinator(self, monkeypatch):
        """When _running is False, the fallback coordinator is skipped."""
        import backend.neural_mesh.integration as mod

        fake_coordinator = MagicMock(name="StoppedCoordinator")
        fake_coordinator._running = False

        fake_module = types.ModuleType("neural_mesh.neural_mesh_coordinator")
        fake_module._coordinator = fake_coordinator

        monkeypatch.setitem(sys.modules, "neural_mesh.neural_mesh_coordinator", fake_module)

        assert mod._neural_mesh_coordinator is None
        result = mod.get_neural_mesh_coordinator()
        assert result is None

    def test_fallback_skips_none_coordinator(self, monkeypatch):
        """When _coordinator is None in the coordinator module, returns None."""
        import backend.neural_mesh.integration as mod

        fake_module = types.ModuleType("neural_mesh.neural_mesh_coordinator")
        fake_module._coordinator = None

        monkeypatch.setitem(sys.modules, "neural_mesh.neural_mesh_coordinator", fake_module)

        result = mod.get_neural_mesh_coordinator()
        assert result is None

    def test_fallback_import_error_returns_none(self, monkeypatch):
        """When import of coordinator module fails, returns None gracefully."""
        import backend.neural_mesh.integration as mod

        # Remove the module from sys.modules to force ImportError
        monkeypatch.delitem(sys.modules, "neural_mesh.neural_mesh_coordinator", raising=False)
        # Also ensure it can't be found via import machinery
        monkeypatch.setitem(sys.modules, "neural_mesh.neural_mesh_coordinator", None)

        result = mod.get_neural_mesh_coordinator()
        assert result is None

    # ── agent set registration ──────────────────────────────────────────

    def test_is_agent_set_registered_empty(self):
        import backend.neural_mesh.integration as mod

        assert mod._is_agent_set_registered("coord-1", {"agent_a"}) is False

    def test_mark_and_check_agent_set(self):
        import backend.neural_mesh.integration as mod

        mod._mark_agent_set_registered("coord-1", {"agent_a", "agent_b"})
        assert mod._is_agent_set_registered("coord-1", {"agent_a"}) is True
        assert mod._is_agent_set_registered("coord-1", {"agent_b"}) is True
        assert mod._is_agent_set_registered("coord-1", {"agent_a", "agent_b"}) is True

    def test_agent_set_idempotent(self):
        """Registering same agents twice does not duplicate or error."""
        import backend.neural_mesh.integration as mod

        mod._mark_agent_set_registered("coord-1", {"agent_a"})
        mod._mark_agent_set_registered("coord-1", {"agent_a"})
        assert mod._is_agent_set_registered("coord-1", {"agent_a"}) is True
        # Internal storage is a frozenset, so no duplicates
        assert mod._production_agents_registered["coord-1"] == frozenset({"agent_a"})

    def test_agent_set_accumulates(self):
        """Multiple registrations accumulate agents."""
        import backend.neural_mesh.integration as mod

        mod._mark_agent_set_registered("coord-1", {"agent_a"})
        mod._mark_agent_set_registered("coord-1", {"agent_b", "agent_c"})
        assert mod._is_agent_set_registered("coord-1", {"agent_a", "agent_b", "agent_c"}) is True

    def test_agent_set_different_coordinators_isolated(self):
        """Different coordinator IDs have separate agent sets."""
        import backend.neural_mesh.integration as mod

        mod._mark_agent_set_registered("coord-1", {"agent_a"})
        mod._mark_agent_set_registered("coord-2", {"agent_b"})

        assert mod._is_agent_set_registered("coord-1", {"agent_a"}) is True
        assert mod._is_agent_set_registered("coord-1", {"agent_b"}) is False
        assert mod._is_agent_set_registered("coord-2", {"agent_b"}) is True
        assert mod._is_agent_set_registered("coord-2", {"agent_a"}) is False

    def test_agent_superset_not_registered(self):
        """A superset of registered agents is not considered registered."""
        import backend.neural_mesh.integration as mod

        mod._mark_agent_set_registered("coord-1", {"agent_a"})
        assert mod._is_agent_set_registered("coord-1", {"agent_a", "agent_b"}) is False
