"""ov cockpit silence Slice 2 Task 3 -- ceremony at t0 + fatal-abort.

Covers the three surfaces the task wired:
  1. ``BattleTestHarness._start_awakening_t0`` dispatches the conductor
     task IMMEDIATELY (COCKPIT) / is a clean no-op (SOAK) -- t0-start proof.
  2. ``BattleTestHarness._run_boot_phase_region`` awaits the already-live
     ceremony task to completion before returning (so the caller's next
     step -- REPL/SerpentFlow construction in ``run()`` -- never races the
     Live region) -- await-before-REPL proof.
  3. ``BattleTestHarness._abort_awakening_on_fatal_boot_error`` -- Mandate 4
     fatal-abort: a synthetic boot error mid-ceremony requests skip, the
     ceremony task completes within the bound, and the ORIGINAL exception
     propagates unchanged (never swallowed).

Uses a hand-rolled fake conductor (mirrors the ``AwakeningConductor``
surface used by the harness: ``run()`` / ``request_skip()`` /
``typed_prefix``) rather than the real animated/plain paths -- those are
already covered by ``tests/ui/test_awakening.py``. This suite is about the
HARNESS's t0-dispatch / await / fatal-abort wiring, not the conductor's own
ceremony logic.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Iterator

import pytest

from backend.core.ouroboros.battle_test import harness as harness_module
from backend.core.ouroboros.battle_test.harness import (
    BattleTestHarness,
    HarnessConfig,
)


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_harness(tmp_path: Path) -> Iterator[BattleTestHarness]:
    session_dir = tmp_path / ".ouroboros" / "sessions" / "bt-awakening-t0-test"
    config = HarnessConfig(
        repo_path=tmp_path,
        cost_cap_usd=0.05,
        idle_timeout_s=30.0,
        session_dir=session_dir,
    )
    harness = BattleTestHarness(config)
    yield harness
    import atexit
    atexit.unregister(harness._atexit_fallback_write)


class _FakeConductor:
    """Duck-typed AwakeningConductor: hangs in ``run()`` until
    ``request_skip()`` fires -- deterministic stand-in for "a ceremony
    task that is genuinely still live" without depending on real
    animation timing or a real terminal."""

    def __init__(self) -> None:
        self.started = False
        self.skip_requested = False
        self.typed_prefix = ""
        self._stop_event: asyncio.Event = asyncio.Event()

    async def run(self) -> None:
        self.started = True
        await self._stop_event.wait()

    def request_skip(self) -> None:
        self.skip_requested = True
        self._stop_event.set()


class _StuckConductor(_FakeConductor):
    """``request_skip()`` is a no-op -- proves the fatal-abort bound is a
    real ceiling, not just a happy-path formality."""

    def request_skip(self) -> None:
        self.skip_requested = True  # observed, but never unblocks run()


def _patch_builder(monkeypatch: pytest.MonkeyPatch, conductor: object) -> None:
    monkeypatch.setattr(
        harness_module, "build_awakening_for_cockpit",
        lambda *a, **kw: conductor,
    )


def _patch_boot_phase_methods(monkeypatch: pytest.MonkeyPatch, harness: BattleTestHarness) -> None:
    """Stub every heavy boot_* method _run_boot_phase_region calls so the
    method can run standalone without booting the real 6-layer stack."""
    async def _noop(*_a, **_kw) -> None:
        return None

    async def _false(*_a, **_kw) -> bool:
        return False

    monkeypatch.setattr(harness, "_boot_git_index_guard", _noop)
    monkeypatch.setattr(harness, "boot_oracle", _noop)
    monkeypatch.setattr(harness, "boot_governance_stack", _noop)
    monkeypatch.setattr(harness, "boot_governed_loop_service", _noop)
    monkeypatch.setattr(harness, "_gate_provider_readiness_or_refuse", _false)
    monkeypatch.setattr(harness, "_boot_ledger_sovereignty_workspace", _noop)
    monkeypatch.setattr(harness, "boot_jarvis_tiers", _noop)
    monkeypatch.setattr(harness, "create_branch", lambda: _string_task())
    monkeypatch.setattr(harness, "boot_intake", _noop)
    monkeypatch.setattr(harness, "_inject_phase_9_synthetic_workload", _noop)
    harness._governed_loop_service = None
    harness._intake_boot_task = None


async def _string_task() -> str:
    return "test-branch"


# ---------------------------------------------------------------------------
# (1) t0-start proof
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_awakening_t0_dispatches_before_boot_phases_cockpit(
    monkeypatch, tmp_harness,
):
    """COCKPIT: the ceremony task is created and RUNNING (started=True)
    strictly by the time _start_awakening_t0 returns control -- i.e.
    dispatched at t0, before run() would go on to call the first heavy
    _BootPhase. Still pending (not done) -- proof the phases race it
    CONCURRENTLY rather than waiting for it first."""
    monkeypatch.setenv("JARVIS_OV_PRESENTATION", "cockpit")
    fake = _FakeConductor()
    _patch_builder(monkeypatch, fake)

    task = tmp_harness._start_awakening_t0()

    assert task is not None
    assert tmp_harness._awakening is fake
    assert tmp_harness._awakening_task is task
    await asyncio.sleep(0)  # let the task get its first scheduling slice
    assert fake.started is True   # ceremony is already running
    assert not task.done()        # still live -- races the boot phases

    # Cleanup: don't leak a pending task.
    fake.request_skip()
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_start_awakening_t0_soak_is_noop(monkeypatch, tmp_harness):
    """SOAK: no console built, no conductor, no task -- legacy boot_banner()
    path (unchanged) handles the banner instead."""
    monkeypatch.setenv("JARVIS_OV_PRESENTATION", "soak")
    built = []
    monkeypatch.setattr(
        harness_module, "build_awakening_for_cockpit",
        lambda *a, **kw: built.append(1) or None,
    )

    task = tmp_harness._start_awakening_t0()

    assert task is None
    assert tmp_harness._awakening is None
    assert tmp_harness._awakening_task is None
    assert not built  # build_awakening_for_cockpit never even called


# ---------------------------------------------------------------------------
# (2) await-before-REPL proof
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_boot_phase_region_awaits_ceremony_to_completion(
    monkeypatch, tmp_harness,
):
    """_run_boot_phase_region (called before any REPL/SerpentFlow
    construction in run()) must not return until the t0-dispatched
    ceremony task has completed -- the serialization invariant that keeps
    SerpentFlow's console output / the REPL's patch_stdout from racing the
    ceremony's Live region."""
    monkeypatch.setenv("JARVIS_OV_PRESENTATION", "cockpit")
    fake = _FakeConductor()
    _patch_builder(monkeypatch, fake)
    _patch_boot_phase_methods(monkeypatch, tmp_harness)

    task = tmp_harness._start_awakening_t0()
    assert task is not None and not task.done()

    # Simulate the ceremony finishing naturally partway through the phase
    # region (e.g. it hit ignition + is_live) by requesting skip from a
    # background coroutine shortly after the region starts.
    async def _skip_soon() -> None:
        await asyncio.sleep(0.01)
        fake.request_skip()

    asyncio.ensure_future(_skip_soon())

    gate_refused = await tmp_harness._run_boot_phase_region()

    assert gate_refused is False
    assert task.done()             # awaited to completion before returning
    assert fake.skip_requested is True


@pytest.mark.asyncio
async def test_run_boot_phase_region_soak_uses_legacy_banner(monkeypatch, tmp_harness):
    """SOAK: no ceremony task was dispatched at t0 -- _run_boot_phase_region
    must fall through to the legacy path (no _serpent_flow here, so the
    basic Rich-console fallback fires) rather than trying to await None."""
    monkeypatch.setenv("JARVIS_OV_PRESENTATION", "soak")
    _patch_boot_phase_methods(monkeypatch, tmp_harness)
    assert tmp_harness._start_awakening_t0() is None

    gate_refused = await tmp_harness._run_boot_phase_region()

    assert gate_refused is False


# ---------------------------------------------------------------------------
# (3) fatal-abort proof (Mandate 4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fatal_abort_skips_awaits_and_reraises_original_exception(
    monkeypatch, tmp_harness,
):
    """Synthetic boot error mid-ceremony (a hanging conductor that would
    never finish on its own): skip is requested, the ceremony task
    completes within the bound, and the ORIGINAL exception propagates
    unchanged -- mirrors run()'s inner try/except exactly."""
    monkeypatch.setenv("JARVIS_OV_PRESENTATION", "cockpit")
    fake = _FakeConductor()  # hangs until request_skip()
    _patch_builder(monkeypatch, fake)

    task = tmp_harness._start_awakening_t0()
    assert task is not None
    await asyncio.sleep(0)
    assert fake.started is True and not task.done()  # genuinely live

    async def _boot_region_that_blows_up() -> bool:
        raise ValueError("synthetic boot error mid-ceremony")

    with pytest.raises(ValueError, match="synthetic boot error mid-ceremony"):
        try:
            await _boot_region_that_blows_up()
        except Exception:
            await tmp_harness._abort_awakening_on_fatal_boot_error()
            raise

    assert fake.skip_requested is True
    assert task.done()


@pytest.mark.asyncio
async def test_fatal_abort_is_a_noop_when_ceremony_not_live(monkeypatch, tmp_harness):
    """SOAK / ceremony already finished: no task to skip -- the helper must
    return immediately without touching anything (nothing to swallow, no
    hang)."""
    tmp_harness._awakening = None
    tmp_harness._awakening_task = None
    await tmp_harness._abort_awakening_on_fatal_boot_error()  # must not raise


@pytest.mark.asyncio
async def test_fatal_abort_bound_is_enforced_even_if_task_never_completes(
    monkeypatch, tmp_harness,
):
    """A pathologically stuck conductor (request_skip() doesn't unblock it)
    must not hang the fatal-abort helper forever. ``asyncio.wait_for``
    cancels the awaited task on timeout (and waits for the cancellation to
    land) before raising -- the helper swallows that TimeoutError and
    returns promptly, task now cancelled/done, so the caller can still
    re-raise the ORIGINAL exception without delay."""
    monkeypatch.setattr(harness_module, "_AWAKENING_FATAL_ABORT_BOUND_S", 0.05)
    stuck = _StuckConductor()
    tmp_harness._awakening = stuck
    tmp_harness._awakening_task = asyncio.ensure_future(stuck.run())
    await asyncio.sleep(0)
    assert stuck.started is True

    start = asyncio.get_event_loop().time()
    await tmp_harness._abort_awakening_on_fatal_boot_error()  # must return promptly
    elapsed = asyncio.get_event_loop().time() - start

    assert stuck.skip_requested is True
    assert elapsed < 1.0  # bounded -- not the conductor's infinite hang
    # asyncio.wait_for cancels its awaited task on timeout and waits for
    # the cancellation to land before returning -- the task ends up
    # cancelled (== done), not merely "still pending".
    assert tmp_harness._awakening_task.done()
    assert tmp_harness._awakening_task.cancelled()
