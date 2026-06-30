"""tests/scripts/test_hybrid_mesh_teardown.py -- Task HM-A (Hybrid Execution Mesh).

Proves the LOCAL A1 driver owns the GCP failover node's networking + an
UNBREAKABLE teardown so a real GPU node can NEVER be orphaned:

  - ``_reap_failover_resources`` deletes BOTH the node AND its ephemeral /32
    firewall, is IDEMPOTENT (clears the registry -> second call is a no-op), and
    is FAIL-SOFT (a delete that raises does not abort the rest of the teardown).
  - ``_open_failover_firewall`` reuses the existing GCP primitives
    (``resolve_local_public_ip`` + ``create_firewall_rule``) to open a /32 rule
    for THIS host's public IP -- never ``0.0.0.0/0``.
  - When the public IP cannot be resolved the firewall is NOT opened, but the
    node stays registered for teardown.
  - The SIGTERM/SIGINT handler path reaps the failover resources.

All GCP I/O is faked via a recorder injected by monkeypatching ``get_compute_rest``
and ``resolve_local_public_ip`` on the source module -- zero real network/spend.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

# ---------------------------------------------------------------------------
# Path bootstrap -- scripts are not packages; add repo root + scripts + backend
# ---------------------------------------------------------------------------
_REPO_ROOT = str((Path(__file__).parent.parent.parent).resolve())
_SCRIPTS_DIR = str((Path(__file__).parent.parent.parent / "scripts").resolve())

for _p in (_REPO_ROOT, _SCRIPTS_DIR, os.path.join(_REPO_ROOT, "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_script(name: str) -> Any:
    """Cache-first script load so monkeypatch targets the SAME module object the
    driver's own ``_load_module`` returns."""
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(_SCRIPTS_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, "Cannot load %s from %s" % (name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_driver = _load_script("isomorphic_a1_local")

# The source module whose symbols the driver imports at call-time; monkeypatch
# here so the lazy ``from ... import get_compute_rest`` picks up the fake.
import backend.core.ouroboros.governance.gcp_compute_rest as gcp_rest  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _Recorder:
    """Records every GCP REST call the teardown / firewall-open makes."""

    def __init__(self, *, instance_raises: bool = False) -> None:
        self.deleted_instances: List[Optional[str]] = []
        self.deleted_zones: List[Optional[str]] = []
        self.deleted_firewalls: List[str] = []
        self.created_firewalls: List[Dict[str, Any]] = []
        self._instance_raises = instance_raises

    async def delete_instance(
        self, name: Optional[str] = None, *, zone: Optional[str] = None
    ) -> Tuple[bool, str]:
        if self._instance_raises:
            raise RuntimeError("boom: instance delete blew up")
        self.deleted_instances.append(name)
        self.deleted_zones.append(zone)
        return (True, "deleted:200")

    async def delete_firewall_rule(self, name: str) -> Tuple[bool, str]:
        self.deleted_firewalls.append(name)
        return (True, "deleted:200")

    async def create_firewall_rule(
        self, *, name: str, source_ip: str, port: int = 11434,
    ) -> Tuple[bool, str]:
        self.created_firewalls.append(
            {"name": name, "source_ip": source_ip, "port": port})
        return (True, "created:200")


_TEST_ZONES = "us-central1-a,us-central1-b,us-central1-c"


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    """Every test starts + ends with an empty failover registry, and pins a
    deterministic 3-zone fallback chain so the multi-zonal reap sweep is
    predictable (the reap deletes the node in EVERY candidate zone)."""
    _driver._ACTIVE_FAILOVER_RESOURCES.clear()
    _prev = os.environ.get("JARVIS_GCP_ZONE_FALLBACK")
    os.environ["JARVIS_GCP_ZONE_FALLBACK"] = _TEST_ZONES
    yield
    if _prev is None:
        os.environ.pop("JARVIS_GCP_ZONE_FALLBACK", None)
    else:
        os.environ["JARVIS_GCP_ZONE_FALLBACK"] = _prev
    _driver._ACTIVE_FAILOVER_RESOURCES.clear()


def _install_recorder(
    monkeypatch: Any, recorder: _Recorder, *, ip: Optional[str] = "203.0.113.7",
) -> None:
    monkeypatch.setattr(gcp_rest, "get_compute_rest", lambda: recorder)

    async def _fake_resolve(*_a: Any, **_k: Any) -> Optional[str]:
        return ip

    monkeypatch.setattr(gcp_rest, "resolve_local_public_ip", _fake_resolve)


# ---------------------------------------------------------------------------
# Reap tests
# ---------------------------------------------------------------------------

def test_reap_deletes_node_and_firewall(monkeypatch: Any) -> None:
    rec = _Recorder()
    _install_recorder(monkeypatch, rec)
    _driver._ACTIVE_FAILOVER_RESOURCES.append({
        "node": "jarvis-prime-failover", "zone": "us-central1-a",
        "project": "proj", "fw_rule": "jarvis-ephemeral-failover-allow",
    })

    _driver._reap_failover_resources()

    # The node is deleted in EVERY candidate zone (zone-aware reap -- a node that
    # landed in a fallback zone is never orphaned). 404-idempotent on empty zones.
    assert set(rec.deleted_instances) == {"jarvis-prime-failover"}
    assert rec.deleted_zones == ["us-central1-a", "us-central1-b", "us-central1-c"]
    assert rec.deleted_firewalls == ["jarvis-ephemeral-failover-allow"]
    # Registry cleared -> a second reap is a no-op.
    assert _driver._ACTIVE_FAILOVER_RESOURCES == []


def test_reap_idempotent(monkeypatch: Any) -> None:
    rec = _Recorder()
    _install_recorder(monkeypatch, rec)
    _driver._ACTIVE_FAILOVER_RESOURCES.append({
        "node": "node-1", "zone": None, "project": None, "fw_rule": "fw-1",
    })

    _driver._reap_failover_resources()
    _driver._reap_failover_resources()  # second call must be a pure no-op

    # First reap swept all 3 zones once; the second was a pure no-op (not 6).
    assert set(rec.deleted_instances) == {"node-1"}
    assert len(rec.deleted_instances) == 3
    assert rec.deleted_firewalls == ["fw-1"]


def test_reap_failsoft(monkeypatch: Any) -> None:
    # delete_instance raises -> reap MUST still return cleanly and STILL attempt
    # the firewall delete (gather(..., return_exceptions=True)).
    rec = _Recorder(instance_raises=True)
    _install_recorder(monkeypatch, rec)
    _driver._ACTIVE_FAILOVER_RESOURCES.append({
        "node": "node-x", "zone": None, "project": None, "fw_rule": "fw-x",
    })

    _driver._reap_failover_resources()  # must NOT raise

    assert rec.deleted_firewalls == ["fw-x"]  # firewall delete still attempted
    assert _driver._ACTIVE_FAILOVER_RESOURCES == []


def test_reap_empty_registry_is_noop(monkeypatch: Any) -> None:
    rec = _Recorder()
    _install_recorder(monkeypatch, rec)
    # Registry empty (default-OFF path) -> no client touched at all.
    _driver._reap_failover_resources()
    assert rec.deleted_instances == []
    assert rec.deleted_firewalls == []


# ---------------------------------------------------------------------------
# Firewall-open tests
# ---------------------------------------------------------------------------

def test_firewall_opens_with_slash32_source(monkeypatch: Any) -> None:
    rec = _Recorder()
    _install_recorder(monkeypatch, rec, ip="203.0.113.7")

    ok = asyncio.run(_driver._open_failover_firewall("fw-mesh"))

    # Fix 3: _open_failover_firewall returns the fw_name on success (not bool).
    assert ok == "fw-mesh"
    assert rec.created_firewalls == [
        {"name": "fw-mesh", "source_ip": "203.0.113.7", "port": 11434},
    ]


def test_fw_rule_none_when_not_opened(monkeypatch: Any) -> None:
    """Fix 3: when resolve_local_public_ip returns None the firewall is NOT opened,
    but the node is STILL registered for teardown (orphan safety)."""
    rec = _Recorder()
    _install_recorder(monkeypatch, rec, ip=None)  # cannot resolve public IP

    env: Dict[str, str] = {}
    asyncio.run(_driver._arm_failover_mesh(env))

    # Node must be registered (orphan safety never weakened).
    assert len(_driver._ACTIVE_FAILOVER_RESOURCES) == 1
    assert _driver._ACTIVE_FAILOVER_RESOURCES[0]["node"]
    # fw_rule must be None because the firewall was never actually opened.
    assert _driver._ACTIVE_FAILOVER_RESOURCES[0]["fw_rule"] is None
    # No firewall rule was created.
    assert rec.created_firewalls == []


def test_no_public_ip_skips_open_but_keeps_teardown(monkeypatch: Any) -> None:
    rec = _Recorder()
    _install_recorder(monkeypatch, rec, ip=None)  # public IP unresolvable

    env: Dict[str, str] = {}
    asyncio.run(_driver._arm_failover_mesh(env))

    # Firewall NOT opened ...
    assert rec.created_firewalls == []
    # ... but the node IS registered for teardown (and the mesh env was armed).
    assert len(_driver._ACTIVE_FAILOVER_RESOURCES) == 1
    assert _driver._ACTIVE_FAILOVER_RESOURCES[0]["node"]
    assert env["JARVIS_FAILOVER_HYBRID_MESH"] == "true"
    assert env["JARVIS_FAILOVER_INFERENCE_BIND_ENABLED"] == "true"
    # The driver owns the firewall -> it must NOT delegate to the organism.
    assert "JARVIS_FAILOVER_EPHEMERAL_FW_ENABLED" not in env


def test_arm_registers_then_opens(monkeypatch: Any) -> None:
    rec = _Recorder()
    _install_recorder(monkeypatch, rec, ip="198.51.100.4")
    monkeypatch.setenv("JARVIS_FAILOVER_NODE_NAME", "node-custom")
    monkeypatch.setenv("JARVIS_FAILOVER_FW_RULE_NAME", "fw-custom")

    env: Dict[str, str] = {}
    asyncio.run(_driver._arm_failover_mesh(env))

    assert _driver._ACTIVE_FAILOVER_RESOURCES[0]["node"] == "node-custom"
    assert _driver._ACTIVE_FAILOVER_RESOURCES[0]["fw_rule"] == "fw-custom"
    assert rec.created_firewalls == [
        {"name": "fw-custom", "source_ip": "198.51.100.4", "port": 11434},
    ]


# ---------------------------------------------------------------------------
# Fix B: _arm_failover_mesh arms an L4-capable zone list + on-demand-on-stockout
# ---------------------------------------------------------------------------

def test_arm_sets_l4_zone_list_and_ondemand(monkeypatch: Any) -> None:
    """The mesh arming seeds the 3 L4-capable us-central1 zones + on-demand flag,
    so the live ignition can actually land a node (drops non-L4 zones whose 400
    halts the chain; Spot-stockout falls through to on-demand in the quota'd zone)."""
    rec = _Recorder()
    _install_recorder(monkeypatch, rec, ip="198.51.100.4")

    env: Dict[str, str] = {}
    asyncio.run(_driver._arm_failover_mesh(env))

    assert env["JARVIS_GCP_ZONE_FALLBACK"] == "us-central1-a,us-central1-b,us-central1-c"
    assert env["JARVIS_FAILOVER_ONDEMAND_ON_STOCKOUT"] == "true"


def test_arm_respects_operator_zone_and_ondemand_overrides(monkeypatch: Any) -> None:
    """setdefault: an operator-preset value in env (from os.environ via compose_env)
    is NOT overridden by the mesh defaults."""
    rec = _Recorder()
    _install_recorder(monkeypatch, rec, ip="198.51.100.4")

    env: Dict[str, str] = {
        "JARVIS_GCP_ZONE_FALLBACK": "us-east1-b,us-east1-c",
        "JARVIS_FAILOVER_ONDEMAND_ON_STOCKOUT": "false",
    }
    asyncio.run(_driver._arm_failover_mesh(env))

    assert env["JARVIS_GCP_ZONE_FALLBACK"] == "us-east1-b,us-east1-c"
    assert env["JARVIS_FAILOVER_ONDEMAND_ON_STOCKOUT"] == "false"


# ---------------------------------------------------------------------------
# Signal-handler teardown
# ---------------------------------------------------------------------------

def test_signal_handler_reaps(monkeypatch: Any) -> None:
    reaped: List[bool] = []
    monkeypatch.setattr(
        _driver, "_reap_failover_resources", lambda: reaped.append(True))

    captured: Dict[int, Any] = {}

    def _fake_signal(sig: int, handler: Any) -> Any:
        captured[sig] = handler
        return None

    # Capture the installed handler, neuter the re-raise so the test process
    # survives, and stub chaos-revert (none active anyway).
    monkeypatch.setattr(_driver.signal, "signal", _fake_signal)
    monkeypatch.setattr(_driver.os, "kill", lambda *_a, **_k: None)

    _driver._install_revert_signal_handlers()

    handler = captured.get(_driver.signal.SIGTERM)
    assert handler is not None, "SIGTERM handler must be installed"
    handler(_driver.signal.SIGTERM, None)  # simulate SIGTERM delivery

    assert reaped == [True], "signal handler must reap failover resources"
