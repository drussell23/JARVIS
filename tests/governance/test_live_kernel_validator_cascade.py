"""Slice 256 C.3 — CascadeFailureBreaker.on_trip: soft relief → re-eval → recover OR
shadow-gated HARD_RESTART (never auto-reboots). All deps injected → in-sandbox."""
import importlib.util as _u
import sys as _sys

import pytest

_spec = _u.spec_from_file_location(
    "live_kernel_validator",
    "backend/core/ouroboros/governance/live_kernel_validator.py",
)
lkv = _u.module_from_spec(_spec)
_sys.modules["live_kernel_validator"] = lkv
_spec.loader.exec_module(lkv)


class _ShadowTrapped:  # mimics the real cybernetic_reanimation SHADOW_TRAPPED sentinel
    def __repr__(self):
        return "<SHADOW_TRAPPED>"


def _recorder():
    calls = {"cache_clear": 0, "state_flush": 0, "reboot": 0, "emit": [], "shadow": []}
    async def cache_clear(): calls["cache_clear"] += 1
    async def state_flush(): calls["state_flush"] += 1
    async def reboot(): calls["reboot"] += 1; return "rebooted"
    async def emit(kind, payload): calls["emit"].append(kind)
    return calls, cache_clear, state_flush, reboot, emit


@pytest.mark.asyncio
async def test_soft_relief_recovers_no_reboot():
    calls, cache_clear, state_flush, reboot, emit = _recorder()
    async def pressure_ok(): return "NORMAL"
    async def shadow(desc, action): calls["shadow"].append(desc); return _ShadowTrapped()
    b = lkv.CascadeFailureBreaker()
    out = await b.on_trip(pressure_probe=pressure_ok, shadow_guard=shadow,
                          reboot_action=reboot, cache_clear=cache_clear,
                          state_flush=state_flush, emit=emit)
    assert out == "recovered"
    assert calls["cache_clear"] == 1          # soft relief ran
    assert calls["reboot"] == 0 and calls["shadow"] == []   # NO reboot path
    assert calls["state_flush"] == 0          # no flush needed
    assert "ENVIRONMENT_RECOVERED" in calls["emit"]


@pytest.mark.asyncio
async def test_still_critical_routes_hard_restart_through_shadow_guard():
    calls, cache_clear, state_flush, reboot, emit = _recorder()
    async def pressure_crit(): return "CRITICAL"
    async def shadow(desc, action): calls["shadow"].append(desc); return _ShadowTrapped()
    b = lkv.CascadeFailureBreaker()
    out = await b.on_trip(pressure_probe=pressure_crit, shadow_guard=shadow,
                          reboot_action=reboot, cache_clear=cache_clear,
                          state_flush=state_flush, emit=emit)
    assert out == "awaiting_endorsement"
    assert calls["state_flush"] == 1          # best-effort serializable flush ran
    assert "HARD_RESTART" in calls["shadow"][0]
    assert calls["reboot"] == 0               # trapped — NOT auto-executed
    assert "CRITICAL_SYSTEMIC_CASCADE" in calls["emit"]


@pytest.mark.asyncio
async def test_failed_probe_is_failsecure_not_recovered():
    calls, cache_clear, state_flush, reboot, emit = _recorder()
    async def pressure_boom(): raise RuntimeError("probe down")
    async def shadow(desc, action): calls["shadow"].append(desc); return _ShadowTrapped()
    b = lkv.CascadeFailureBreaker()
    out = await b.on_trip(pressure_probe=pressure_boom, shadow_guard=shadow,
                          reboot_action=reboot, cache_clear=cache_clear,
                          state_flush=state_flush, emit=emit)
    assert out == "awaiting_endorsement"      # unknown env → assume still-degraded
    assert calls["reboot"] == 0


@pytest.mark.asyncio
async def test_shadow_off_executes_reboot_returns_suspended():
    calls, cache_clear, state_flush, reboot, emit = _recorder()
    async def pressure_crit(): return "CRITICAL"
    async def shadow(desc, action): return await action()  # shadow OFF → executes
    b = lkv.CascadeFailureBreaker()
    out = await b.on_trip(pressure_probe=pressure_crit, shadow_guard=shadow,
                          reboot_action=reboot, cache_clear=cache_clear,
                          state_flush=state_flush, emit=emit)
    assert out == "suspended"
    assert calls["reboot"] == 1               # endorsed/shadow-off path actually reboots


@pytest.mark.asyncio
async def test_on_trip_never_raises_even_with_all_broken():
    async def boom(*a, **k): raise RuntimeError("x")
    b = lkv.CascadeFailureBreaker()
    out = await b.on_trip(pressure_probe=boom, shadow_guard=boom, reboot_action=boom,
                          cache_clear=boom, state_flush=boom, emit=boom)
    assert out in ("recovered", "awaiting_endorsement", "suspended")  # never raised
