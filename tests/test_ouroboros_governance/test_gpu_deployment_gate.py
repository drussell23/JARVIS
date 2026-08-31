"""A generation must not start while a hot-swap is landing.

The other half of reactor's gpu_lease. Without it, `ollama create` can
replace the blob under a model a live generation is streaming from.

The polarity here is deliberately OPPOSITE to the reactor side and the
tests pin both directions: gpu_lease defers unless proven free (a false
"free" OOMs a live soak), while this gate proceeds unless proven blocked
(a false "blocked" would halt the organism whenever the lock subsystem
hiccups).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from backend.core.ouroboros.governance import gpu_deployment_gate as gate

_ENV_MASTER = "JARVIS_GPU_DEPLOYMENT_GATE_ENABLED"


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV_MASTER, raising=False)
    monkeypatch.delenv("JARVIS_GPU_DEPLOYMENT_LOCK_NAME", raising=False)


def _stub_manager(monkeypatch: pytest.MonkeyPatch, status: Optional[Dict[str, Any]],
                  *, raises: bool = False) -> Dict[str, Any]:
    """Patch the lazily-imported lock manager. Records the queried name."""
    seen: Dict[str, Any] = {}

    class _Mgr:
        async def get_lock_status(self, name: str):
            seen["name"] = name
            if raises:
                raise RuntimeError("lock subsystem down")
            return status

    async def _get_lock_manager(*a: Any, **k: Any) -> _Mgr:
        return _Mgr()

    import sys
    from types import ModuleType

    mod = ModuleType("backend.core.distributed_lock_manager")
    mod.get_lock_manager = _get_lock_manager  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules, "backend.core.distributed_lock_manager", mod,
    )
    return seen


_LIVE_LOCK = {
    "owner": "reactor-core-991",
    "is_expired": False,
    "is_stale": False,
    "owner_alive": True,
    "time_remaining": 42.0,
}


# ---------------------------------------------------------------------------
# Default off
# ---------------------------------------------------------------------------


def test_gate_defaults_off() -> None:
    assert gate.gate_enabled() is False


@pytest.mark.asyncio
async def test_disabled_never_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_manager(monkeypatch, _LIVE_LOCK)
    blocked, reason = await gate.deployment_in_progress()
    assert blocked is False
    assert reason == "gate_disabled"


# ---------------------------------------------------------------------------
# Blocks only on positive evidence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_deployment_lock_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_ENV_MASTER, "true")
    _stub_manager(monkeypatch, _LIVE_LOCK)
    blocked, reason = await gate.deployment_in_progress()
    assert blocked is True
    assert "deployment in progress" in reason
    assert "reactor-core-991" in reason


@pytest.mark.asyncio
async def test_no_lock_does_not_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV_MASTER, "true")
    _stub_manager(monkeypatch, None)
    assert await gate.deployment_in_progress() == (False, "no_deployment_lock")


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"is_expired": True}, "lock_expired"),
        ({"is_stale": True}, "lock_stale"),
        ({"owner_alive": False}, "owner_dead"),
    ],
)
@pytest.mark.asyncio
async def test_dead_or_expired_locks_do_not_block(
    monkeypatch: pytest.MonkeyPatch, mutation: dict, expected: str,
) -> None:
    """A crashed deployer must not wedge the loop forever."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    _stub_manager(monkeypatch, {**_LIVE_LOCK, **mutation})
    blocked, reason = await gate.deployment_in_progress()
    assert blocked is False
    assert reason == expected


# ---------------------------------------------------------------------------
# Fail OPEN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_failure_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A false 'blocked' would halt the organism. Cheaper to proceed."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    _stub_manager(monkeypatch, None, raises=True)
    blocked, reason = await gate.deployment_in_progress()
    assert blocked is False
    assert reason.startswith("probe_failed:")


@pytest.mark.asyncio
async def test_missing_lock_manager_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    monkeypatch.setitem(
        sys.modules, "backend.core.distributed_lock_manager", None,
    )
    monkeypatch.setenv(_ENV_MASTER, "true")
    blocked, _ = await gate.deployment_in_progress()
    assert blocked is False


# ---------------------------------------------------------------------------
# The cross-repo contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queries_the_shared_lock_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two repos cannot import each other, so the NAME is the
    contract. Changing it on one side alone silently removes the
    exclusion, which is why it is pinned here literally."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    seen = _stub_manager(monkeypatch, None)
    await gate.deployment_in_progress()
    assert seen["name"] == "trinity_gpu_vram"
    assert gate.GPU_LOCK_NAME == "trinity_gpu_vram"


@pytest.mark.asyncio
async def test_lock_name_is_overridable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv("JARVIS_GPU_DEPLOYMENT_LOCK_NAME", "other_lock")
    seen = _stub_manager(monkeypatch, None)
    await gate.deployment_in_progress()
    assert seen["name"] == "other_lock"


def test_flags_registered() -> None:
    seen: list = []

    class _Reg:
        def bulk_register(self, specs: Any, override: bool = False) -> None:
            seen.extend(specs)

    assert gate.register_flags(_Reg()) == 2
    assert {s.name for s in seen} == {
        _ENV_MASTER, "JARVIS_GPU_DEPLOYMENT_LOCK_NAME",
    }
    assert next(s for s in seen if s.name == _ENV_MASTER).default is False
