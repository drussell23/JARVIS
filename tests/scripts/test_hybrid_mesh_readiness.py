"""tests/scripts/test_hybrid_mesh_readiness.py -- Task HM-B (Hybrid Execution Mesh).

Proves the L7 SEMANTIC-READINESS poller suspends the A1 audit until the awakened
GCP 32B node's inference server returns HTTP 200 on ``/api/tags`` (the ~20GB model
is loaded into L4 VRAM) -- the gate that stops the audit from firing FAILED before
the node can serve (the exact failure of the last live run):

  - ``_await_jprime_serving`` returns True on the FIRST 200.
  - It waits (exponential backoff) while the node has no external IP yet, then
    while the inference server is still warming (503), then succeeds on 200 --
    proving it actually polls (probe called > 1).
  - On budget exhaustion it returns False WITHOUT raising (the audit proceeds;
    the ironclad HM-A teardown reaps the node).
  - A transport error from the probe is fail-soft -> treated as 'not ready'.

All GCP I/O is faked by monkeypatching ``get_compute_rest`` on the source module;
the actual HTTP GET is factored into the ``_probe_api_tags`` seam, which the tests
monkeypatch instead of touching the real network -- zero real network/spend.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

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

class _FakeCompute:
    """get_node_endpoints returns a scripted (internal, external) sequence;
    the last entry repeats once the script is exhausted."""

    def __init__(self, sequence: List[Tuple[Optional[str], Optional[str]]]) -> None:
        self._seq = list(sequence)
        self.calls = 0

    async def get_node_endpoints(
        self, name: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        self.calls += 1
        idx = min(self.calls - 1, len(self._seq) - 1)
        return self._seq[idx]


def _install_compute(
    monkeypatch: Any, sequence: List[Tuple[Optional[str], Optional[str]]],
) -> _FakeCompute:
    fake = _FakeCompute(sequence)
    monkeypatch.setattr(gcp_rest, "get_compute_rest", lambda: fake)
    return fake


def _install_probe(monkeypatch: Any, side: Any) -> List[str]:
    """Stub ``_probe_api_tags`` with a callable or a list of status codes /
    exceptions to yield in order (last repeats). Records probed URLs."""
    probed: List[str] = []

    if callable(side):
        def _fn(url: str) -> int:
            probed.append(url)
            return side(url)
    else:
        seq = list(side)

        def _fn(url: str) -> int:
            probed.append(url)
            item = seq[min(len(probed) - 1, len(seq) - 1)]
            if isinstance(item, BaseException):
                raise item
            return int(item)

    monkeypatch.setattr(_driver, "_probe_api_tags", _fn)
    return probed


@pytest.fixture(autouse=True)
def _tiny_backoff(monkeypatch: Any) -> Any:
    """Shrink backoff/cap so the polling tests are fast."""
    monkeypatch.setenv("JARVIS_HYBRID_MESH_READY_BASE_S", "0.01")
    monkeypatch.setenv("JARVIS_HYBRID_MESH_READY_CAP_S", "0.02")
    monkeypatch.setenv("JARVIS_HYBRID_MESH_READY_PROBE_TIMEOUT_S", "0.5")
    yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_serving_returns_true_on_200(monkeypatch: Any) -> None:
    _install_compute(monkeypatch, [(None, "203.0.113.9")])
    probed = _install_probe(monkeypatch, [200])

    served = asyncio.run(
        _driver._await_jprime_serving("node-a", budget_s=5.0))

    assert served is True
    assert probed == ["http://203.0.113.9:11434/api/tags"]


def test_waits_until_ip_then_200(monkeypatch: Any) -> None:
    # No external IP twice, then an IP; probe 503 once, then 200.
    fake = _install_compute(monkeypatch, [
        (None, None), (None, None), (None, "198.51.100.7"),
    ])
    probed = _install_probe(monkeypatch, [503, 200])

    served = asyncio.run(
        _driver._await_jprime_serving("node-b", budget_s=5.0))

    assert served is True
    # Polled the endpoint repeatedly (waited for IP) ...
    assert fake.calls >= 3
    # ... and probed more than once (503 -> backoff -> 200).
    assert len(probed) > 1


def test_budget_exhaustion_returns_false(monkeypatch: Any) -> None:
    # IP is present but the server is permanently warming (503) + a tiny budget.
    _install_compute(monkeypatch, [(None, "203.0.113.50")])
    _install_probe(monkeypatch, [503])

    served = asyncio.run(
        _driver._await_jprime_serving("node-c", budget_s=0.05))

    assert served is False  # never raised


def test_probe_failsoft(monkeypatch: Any) -> None:
    # Probe raises (transport error) -> treated as not-ready, loop continues to
    # budget, returns False, NEVER raises.
    _install_compute(monkeypatch, [(None, "203.0.113.77")])
    _install_probe(monkeypatch, [ConnectionRefusedError("refused")])

    served = asyncio.run(
        _driver._await_jprime_serving("node-d", budget_s=0.05))

    assert served is False


# ---------------------------------------------------------------------------
# Fix 1: soak-child wall budget
# ---------------------------------------------------------------------------

def test_failover_wall_exceeds_readiness_budget(monkeypatch: Any) -> None:
    """Fix 1: _failover_soak_wall accounts for 32B cold-start.

    enable_failover=True  -> READY_BUDGET_S + 600 >= 1500 and > 300
    enable_failover=False -> 300 (byte-identical default path)
    """
    # Default budget (900) -> wall must be 900 + 600 = 1500.
    wall_on = _driver._failover_soak_wall(True)
    assert wall_on >= 900 + 600, "failover wall must cover readiness budget + 600s margin"
    assert wall_on > 300, "failover wall must exceed the non-failover default"

    wall_off = _driver._failover_soak_wall(False)
    assert wall_off == 300, "default (no failover) wall must be byte-identical 300"


def test_failover_wall_respects_env_override(monkeypatch: Any) -> None:
    """_failover_soak_wall re-reads env at call time so monkeypatch takes effect."""
    monkeypatch.setenv("JARVIS_HYBRID_MESH_READY_BUDGET_S", "600")

    wall_on = _driver._failover_soak_wall(True)
    assert wall_on == 600 + 600  # 1200

    wall_off = _driver._failover_soak_wall(False)
    assert wall_off == 300  # unchanged
