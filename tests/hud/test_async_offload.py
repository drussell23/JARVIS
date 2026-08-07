"""
The loop must stay responsive while heavy boot work happens.

These tests are written against the measured failure, not against the helper's
API. From ``~/Library/Logs/JARVIS/loop-stalls.log`` on 2026-08-06, the main
thread was found inside ``importlib._bootstrap._lock_unlock_module`` for 12.38s
while a background thread held the module lock — availability 34%, worst stall
16.33s, and every downstream symptom ("still loading my voice recognition",
"dropped a reply that waited 14.1s") followed from it.

So the load-bearing assertion is a *liveness* one: a heartbeat coroutine keeps
ticking while the offloaded work runs. A test that only checked the return
value would pass against the very code that caused the stall.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import types

import pytest

from backend.core import async_offload
from backend.core.async_offload import (
    ENV_ENABLED,
    ENV_IMPORT_WORKERS,
    call_off_loop,
    import_off_loop,
    offload_enabled,
)


@pytest.fixture(autouse=True)
def _fresh_pools():
    """Each test gets its own pools so worker counts don't leak between them."""
    async_offload._reset_after_fork()
    yield
    async_offload._shutdown_pools(wait=False)
    async_offload._reset_after_fork()


# ── Liveness: the actual defect ──────────────────────────────────────────────
async def _heartbeat(stop: asyncio.Event, ticks: list) -> None:
    while not stop.is_set():
        ticks.append(time.perf_counter())
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_loop_keeps_ticking_during_a_slow_blocking_call():
    """
    The regression, stated directly.

    A 0.5s blocking call is a miniature of the 12.38s import. If it runs on the
    loop thread the heartbeat records no ticks for its whole duration, which is
    precisely what "JARVIS could not respond to anything during that window"
    meant.
    """
    stop = asyncio.Event()
    ticks: list = []
    beat = asyncio.create_task(_heartbeat(stop, ticks))
    await asyncio.sleep(0.02)

    before = len(ticks)
    await call_off_loop(time.sleep, 0.5)
    during = len(ticks) - before

    stop.set()
    await beat

    # ~50 ticks are possible; anything above a handful proves the loop ran.
    assert during >= 10, (
        f"loop only ticked {during} times across a 0.5s offloaded call — "
        "the work is still on the loop thread"
    )


@pytest.mark.asyncio
async def test_blocking_call_on_the_loop_would_fail_this_same_assertion():
    """
    Pins the test's own sensitivity.

    If this control ever passes, the liveness test above has stopped measuring
    anything and both must be re-read.
    """
    stop = asyncio.Event()
    ticks: list = []
    beat = asyncio.create_task(_heartbeat(stop, ticks))
    await asyncio.sleep(0.02)

    before = len(ticks)
    time.sleep(0.5)                      # deliberately ON the loop
    during = len(ticks) - before

    stop.set()
    await beat

    assert during < 10, (
        "a synchronous sleep on the loop produced heartbeats — the harness is "
        "not measuring loop occupancy"
    )


@pytest.mark.asyncio
async def test_import_runs_off_the_loop_thread():
    """The import must not execute on the thread running the loop."""
    loop_thread = threading.get_ident()
    seen: dict = {}

    name = "_jarvis_offload_probe_thread"
    module = types.ModuleType(name)

    real_import = async_offload.importlib.import_module

    def _record(dotted):
        if dotted == name:
            seen["thread"] = threading.get_ident()
            sys.modules[name] = module
            return module
        return real_import(dotted)

    async_offload.importlib.import_module = _record
    try:
        got = await import_off_loop(name)
    finally:
        async_offload.importlib.import_module = real_import
        sys.modules.pop(name, None)

    assert got is module
    assert seen["thread"] != loop_thread, "import ran on the event loop thread"


# ── Shape parity with `from X import a, b` ───────────────────────────────────
@pytest.mark.asyncio
async def test_returns_module_attribute_or_tuple():
    """A call site converts without changing shape, or conversions introduce bugs."""
    mod = await import_off_loop("json")
    assert mod.__name__ == "json"

    dumps = await import_off_loop("json", "dumps")
    assert dumps({"a": 1}) == '{"a": 1}'

    dumps2, loads = await import_off_loop("json", "dumps", "loads")
    assert loads(dumps2({"a": 1})) == {"a": 1}


@pytest.mark.asyncio
async def test_missing_attribute_still_raises_attributeerror():
    """Failure modes must not change shape either."""
    with pytest.raises(AttributeError):
        await import_off_loop("json", "no_such_symbol")


@pytest.mark.asyncio
async def test_import_error_propagates():
    with pytest.raises(ImportError):
        await import_off_loop("jarvis_module_that_does_not_exist_xyz")


# ── The half-built module trap ───────────────────────────────────────────────
def test_module_mid_initialization_is_not_served_from_the_fast_path():
    """
    ``sys.modules`` membership is not proof a module is usable.

    A module being imported by another thread is already in ``sys.modules`` with
    a partially populated namespace — returning it hands out missing attributes.
    ``__spec__._initializing`` is the flag the import system itself uses, and is
    why a plain ``in sys.modules`` check would be wrong here. This is the exact
    concurrent-import state the stall dumps captured.
    """
    name = "_jarvis_offload_probe_initializing"
    module = types.ModuleType(name)
    spec = types.SimpleNamespace(_initializing=True)
    module.__spec__ = spec
    sys.modules[name] = module
    try:
        assert async_offload._already_usable(name) is None, \
            "a half-initialized module was served from the fast path"
        spec._initializing = False
        assert async_offload._already_usable(name) is module
    finally:
        sys.modules.pop(name, None)


@pytest.mark.asyncio
async def test_already_imported_module_skips_the_thread_hop():
    """The common case must cost nothing — no dispatch for a resolved module."""
    await import_off_loop("json")
    calls: list = []
    real = async_offload.importlib.import_module
    async_offload.importlib.import_module = lambda d: (calls.append(d), real(d))[1]
    try:
        got = await import_off_loop("json", "dumps")
    finally:
        async_offload.importlib.import_module = real
    assert got is not None
    assert calls == [], "an already-imported module was re-dispatched to a thread"


# ── Fail-open: this helper may never be the reason boot fails ────────────────
@pytest.mark.asyncio
async def test_disabled_master_switch_still_imports(monkeypatch):
    monkeypatch.setenv(ENV_ENABLED, "false")
    assert offload_enabled() is False
    dumps = await import_off_loop("json", "dumps")
    assert dumps({"a": 1}) == '{"a": 1}'


@pytest.mark.asyncio
async def test_unavailable_pool_degrades_to_inline(monkeypatch):
    """No threads left is a reason to be slow, never a reason to fail."""
    monkeypatch.setattr(async_offload, "_get_pool", lambda kind: None)
    dumps = await import_off_loop("json", "dumps")
    assert dumps({"a": 1}) == '{"a": 1}'
    assert await call_off_loop(lambda: 7) == 7


@pytest.mark.parametrize("value", ["0", "-4", "", "not-a-number", "abc"])
def test_malformed_worker_count_never_sizes_a_pool_at_zero(monkeypatch, value):
    """
    A zero-worker pool accepts submissions and never runs them.

    That converts a stall into a permanent hang, so a malformed knob must fall
    back to the default rather than be honoured.
    """
    monkeypatch.setenv(ENV_IMPORT_WORKERS, value)
    assert async_offload._positive_int(ENV_IMPORT_WORKERS, 1) == 1


@pytest.mark.asyncio
async def test_reentrant_call_from_an_offload_thread_runs_inline():
    """
    With a single import worker, a worker that re-dispatched onto its own pool
    and waited would deadlock. Re-entrancy must resolve inline.
    """
    async_offload._local.owned = True
    try:
        assert async_offload._on_offload_thread() is True
        dumps = await import_off_loop("json", "dumps")
        assert dumps({"a": 1}) == '{"a": 1}'
        assert await call_off_loop(lambda: 11) == 11
    finally:
        async_offload._local.owned = False


@pytest.mark.asyncio
async def test_call_off_loop_propagates_exceptions():
    """Offloading must not swallow a failure — that would hide the next defect."""
    def _boom():
        raise ValueError("propagated")

    with pytest.raises(ValueError, match="propagated"):
        await call_off_loop(_boom)


@pytest.mark.asyncio
async def test_call_off_loop_forwards_arguments():
    assert await call_off_loop(lambda a, b=0: a + b, 3, b=4) == 7


def test_fork_reset_drops_inherited_pools():
    """
    A forked child inherits pool objects whose worker threads did not survive;
    submitting to one blocks forever. The reset must clear both.
    """
    async_offload._get_pool("import")
    async_offload._get_pool("call")
    assert async_offload._IMPORT_POOL is not None
    assert async_offload._CALL_POOL is not None

    async_offload._reset_after_fork()

    assert async_offload._IMPORT_POOL is None
    assert async_offload._CALL_POOL is None


# ── Wiring: an unused seam fixes nothing ─────────────────────────────────────
def test_measured_stall_sites_actually_use_the_seam():
    """
    Guards the wired-but-inert trap.

    Each path below is a stack the StallSampler caught holding the loop. If a
    later edit reverts one to a plain import, the stall returns silently and
    every test above still passes.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    sites = {
        "backend/core/ouroboros/governance/hud_governance_boot.py": 2,  # dumps 1 + 2
        "backend/main.py": 1,
        "backend/intelligence/cloud_database_adapter.py": 1,            # dump 3
    }

    for rel, expected in sites.items():
        tree = ast.parse((root / rel).read_text())
        used = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("import_off_loop", "call_off_loop")
        )
        assert used >= expected, (
            f"{rel} calls the offload seam {used}x, expected >= {expected} — "
            "a measured stall site was reverted to blocking-on-the-loop"
        )
