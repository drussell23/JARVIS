"""Centralized Fleet Registry -- dynamic multi-node topology.

The orchestrator can no longer assume a single SERVING endpoint. The registry
holds each node class's endpoint INDEPENDENTLY (cpu + gpu resolved/registered
separately by the Reachability Racer). The router queries endpoint_for(class)
per-op to execute the exact handoff. Reaping a class clears only that entry.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.fleet_registry import (
    FleetRegistry,
    get_fleet_registry,
    reset_fleet_registry,
)


@pytest.fixture(autouse=True)
def _fresh():
    reset_fleet_registry()
    yield
    reset_fleet_registry()


def test_register_and_resolve():
    r = FleetRegistry()
    r.register("cpu", "http://10.0.0.1:11434")
    assert r.endpoint_for("cpu") == "http://10.0.0.1:11434"


def test_classes_are_independent():
    r = FleetRegistry()
    r.register("cpu", "http://cpu:11434")
    r.register("gpu", "http://gpu:11434")
    assert r.endpoint_for("cpu") == "http://cpu:11434"
    assert r.endpoint_for("gpu") == "http://gpu:11434"


def test_unregister_one_leaves_the_other():
    r = FleetRegistry()
    r.register("cpu", "http://cpu:11434")
    r.register("gpu", "http://gpu:11434")
    r.unregister("gpu")  # reap GPU
    assert r.endpoint_for("gpu") is None
    assert r.endpoint_for("cpu") == "http://cpu:11434"  # CPU survives


def test_unknown_class_resolves_none():
    assert FleetRegistry().endpoint_for("nope") is None


def test_reregister_overwrites():
    r = FleetRegistry()
    r.register("gpu", "http://old:11434")
    r.register("gpu", "http://new:11434")
    assert r.endpoint_for("gpu") == "http://new:11434"


def test_register_empty_endpoint_is_ignored():
    r = FleetRegistry()
    r.register("cpu", "")
    assert r.endpoint_for("cpu") is None
    assert "cpu" not in r.classes()


def test_classes_and_snapshot():
    r = FleetRegistry()
    r.register("cpu", "http://cpu:11434")
    r.register("gpu", "http://gpu:11434")
    assert set(r.classes()) == {"cpu", "gpu"}
    snap = r.snapshot()
    assert snap == {"cpu": "http://cpu:11434", "gpu": "http://gpu:11434"}
    snap["cpu"] = "mutated"  # snapshot is a copy
    assert r.endpoint_for("cpu") == "http://cpu:11434"


def test_is_registered():
    r = FleetRegistry()
    assert r.is_registered("gpu") is False
    r.register("gpu", "http://gpu:11434")
    assert r.is_registered("gpu") is True


def test_singleton_accessor_is_stable():
    a = get_fleet_registry()
    a.register("cpu", "http://cpu:11434")
    b = get_fleet_registry()
    assert b.endpoint_for("cpu") == "http://cpu:11434"  # same instance
