from __future__ import annotations

import asyncio
import time

import pytest


@pytest.mark.asyncio
async def test_resolve_scoped_targets_does_not_block_loop(tmp_path, monkeypatch):
    """The Run #14 tombstone: main thread wedged in pathlib.resolve inside
    _resolve_scoped_targets. Prove the loop keeps ticking (<250ms gaps)
    while resolution runs against a slow filesystem walk."""
    from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (
        TestFailureSensor,
    )
    sensor = TestFailureSensor.__new__(TestFailureSensor)
    monkeypatch.setattr(sensor, "_repo_root", lambda: tmp_path, raising=False)

    real_resolve = __import__("pathlib").Path.resolve

    def slow_resolve(self, *a, **k):
        time.sleep(0.4)  # simulated deep _joinrealpath walk
        return real_resolve(self, *a, **k)

    monkeypatch.setattr("pathlib.Path.resolve", slow_resolve)

    gaps: list[float] = []

    async def ticker():
        prev = time.monotonic()
        while True:
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - prev)
            prev = now

    t = asyncio.ensure_future(ticker())
    try:
        await sensor._resolve_scoped_targets("backend/some/module.py")
    finally:
        t.cancel()
    assert gaps, "ticker never ran"
    assert max(gaps) < 0.25, f"loop starved: max gap {max(gaps):.3f}s"


@pytest.mark.asyncio
async def test_resolver_failure_degrades_to_none(monkeypatch, tmp_path):
    """Offload failure -> same neutral value the sync path returns (None),
    never an exception (Global Constraint: fail-soft parity)."""
    from backend.core.ouroboros.governance.intake.sensors import test_failure_sensor as tfs
    sensor = tfs.TestFailureSensor.__new__(tfs.TestFailureSensor)
    monkeypatch.setattr(sensor, "_repo_root", lambda: tmp_path, raising=False)

    async def broken_offload(fn, *a, **k):
        raise RuntimeError("executor down")

    monkeypatch.setattr(tfs, "_offload_fs", broken_offload, raising=False)
    assert await sensor._resolve_scoped_targets("backend/x.py") is None


# ---------------------------------------------------------------------------
# cooperative_fs_io master-off degrade: byte-identical resolution
#
# Global Constraint: JARVIS_COOPERATIVE_FS_IO_ENABLED=false must degrade to
# the pre-slice on-loop synchronous behavior VERBATIM — including the
# PRIMARY TestRunner.resolve_affected_tests mapper. Reviewer-reproduced
# Critical: master-off makes offload() run the sync worker INLINE on the
# loop thread, where its asyncio.run() raises "cannot be called from a
# running event loop" and is swallowed — silently killing the primary AST
# resolver while the mirror-dir/tier-3 fallbacks mask the loss.
# ---------------------------------------------------------------------------


def _build_ast_import_repo(tmp_path):
    """repo/foo.py + repo/tests/test_uses_foo.py (imports foo).

    ONLY the primary mapper (TestRunner Strategy 3, AST-import) can map
    foo.py -> test_uses_foo.py in this layout:
    * Strategy 0: foo.py is not a test file.
    * Strategy 1/2: no test_foo.py / test_foo_*.py anywhere.
    * Mirror-dir fallback: the nearest sibling tests/ IS the repo test
      root, which _mirror_tests_dir_sync excludes by contract.
    * Tier-3: looks only for test_foo.py — absent.
    So fallback survival cannot mask a dead primary.
    """
    (tmp_path / "foo.py").write_text("x = 1\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    test = tests_dir / "test_uses_foo.py"
    test.write_text(
        "import foo\n\n\ndef test_uses_foo():\n    assert foo.x == 1\n",
        encoding="utf-8",
    )
    return test


def _resolved(paths):
    import pathlib
    return sorted(str(pathlib.Path(p).resolve()) for p in paths)


@pytest.mark.asyncio
async def test_resolver_master_off_runs_primary_mapper(tmp_path, monkeypatch):
    """JARVIS_COOPERATIVE_FS_IO_ENABLED=false -> the PRIMARY AST resolver
    must still run (natively on-loop — the inline degrade), producing the
    Strategy-3 target that no fallback can find."""
    from backend.core.ouroboros.governance.intake.sensors import (
        test_failure_sensor as tfs,
    )
    expected = _build_ast_import_repo(tmp_path)
    monkeypatch.setenv("JARVIS_COOPERATIVE_FS_IO_ENABLED", "false")

    sensor = tfs.TestFailureSensor.__new__(tfs.TestFailureSensor)
    monkeypatch.setattr(sensor, "_repo_root", lambda: tmp_path, raising=False)

    targets = await sensor._resolve_scoped_targets("foo.py")
    assert targets is not None, (
        "master-off must degrade byte-identical — primary AST resolver "
        "silently dead (Run #14 re-review Critical)"
    )
    assert _resolved(targets) == _resolved([expected])


@pytest.mark.asyncio
async def test_resolver_master_off_parity_with_master_on(tmp_path, monkeypatch):
    """Same inputs, both flag states, identical outputs (reviewer's parity pin)."""
    from backend.core.ouroboros.governance.intake.sensors import (
        test_failure_sensor as tfs,
    )
    expected = _build_ast_import_repo(tmp_path)

    sensor = tfs.TestFailureSensor.__new__(tfs.TestFailureSensor)
    monkeypatch.setattr(sensor, "_repo_root", lambda: tmp_path, raising=False)

    monkeypatch.setenv("JARVIS_COOPERATIVE_FS_IO_ENABLED", "true")
    on = await sensor._resolve_scoped_targets("foo.py")

    monkeypatch.setenv("JARVIS_COOPERATIVE_FS_IO_ENABLED", "false")
    off = await sensor._resolve_scoped_targets("foo.py")

    assert on is not None and off is not None
    assert _resolved(on) == _resolved(off) == _resolved([expected])


def test_watcher_derates_when_event_lane_armed(monkeypatch):
    """Gap #4 Strangler Fig: with the event-primary TestFailure lane armed,
    the legacy poller must not run whole-suite sweeps.

    NOTE: the brief's draft assumed the env var name
    ``JARVIS_TESTFAILURE_FS_EVENTS_ENABLED``. The real name (verified via
    ``grep -n fs_events_enabled backend/.../test_failure_sensor.py``) is
    ``JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED`` — used here and everywhere
    else in this module.
    """
    from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher
    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", "true")
    monkeypatch.delenv("JARVIS_INTENT_POLL_WHEN_EVENT_PRIMARY", raising=False)
    w = TestWatcher.__new__(TestWatcher)
    assert w._event_primary_derate() is True


def test_watcher_polls_when_event_lane_off(monkeypatch):
    from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher
    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", "false")
    w = TestWatcher.__new__(TestWatcher)
    assert w._event_primary_derate() is False


def test_watcher_escape_hatch(monkeypatch):
    """Operators can force legacy polling even under event-primary."""
    from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher
    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_INTENT_POLL_WHEN_EVENT_PRIMARY", "true")
    w = TestWatcher.__new__(TestWatcher)
    assert w._event_primary_derate() is False


# ---------------------------------------------------------------------------
# Production path: TestFailureSensor._poll_loop derate (scope extension)
#
# TestWatcher.start() is NOT the wired production poll loop —
# TestFailureSensor._poll_loop is (intake_layer_service.py builds the sensor
# and never awaits watcher.start()). The Run #15 gate needs the derate on
# THIS path: with the event lane armed and the escape hatch unset, the
# whole-suite poll_once() sweep must be fully SKIPPED each cycle, not merely
# interval-demoted. Harness mirrors the graduated
# test_test_failure_sensor_fs_events.py captured-sleep pattern.
# ---------------------------------------------------------------------------


class _SpyRouter:
    def __init__(self) -> None:
        self.envelopes: list = []

    async def ingest(self, envelope) -> str:
        self.envelopes.append(envelope)
        return "enqueued"


class _StubWatcher:
    """Minimal TestWatcher surface: poll_once + poll_interval_s + stop.

    Mirrors the REAL TestWatcher contract as consumed by
    TestFailureSensor._poll_loop (feedback_fakes_must_mirror_real_contract).
    """

    def __init__(self, poll_interval_s: float = 30.0) -> None:
        self.poll_interval_s = poll_interval_s
        self.poll_calls = 0
        self.stopped = False

    async def poll_once(self) -> list:
        self.poll_calls += 1
        return []

    def stop(self) -> None:
        self.stopped = True


async def _drive_one_poll_cycle(sensor, tfm, monkeypatch) -> list:
    """Run _poll_loop until its first inter-cycle sleep; return captured delays."""
    captured: list = []

    async def _capture_sleep(delay: float) -> None:
        captured.append(delay)
        sensor._running = False
        raise asyncio.CancelledError()

    monkeypatch.setattr(tfm.asyncio, "sleep", _capture_sleep)
    sensor._running = True
    try:
        await sensor._poll_loop()
    except asyncio.CancelledError:
        pass
    return captured


@pytest.mark.asyncio
async def test_sensor_poll_loop_skips_sweep_when_event_lane_armed(monkeypatch):
    """Event lane armed + escape hatch unset -> poll_once NEVER called;
    the cycle still sleeps the fallback interval (no busy-spin)."""
    from backend.core.ouroboros.governance.intake.sensors import (
        test_failure_sensor as tfm,
    )
    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", "true")
    monkeypatch.delenv("JARVIS_INTENT_POLL_WHEN_EVENT_PRIMARY", raising=False)
    monkeypatch.setattr(tfm, "_TEST_FAILURE_FALLBACK_INTERVAL_S", 600.0)

    watcher = _StubWatcher(poll_interval_s=30.0)
    sensor = tfm.TestFailureSensor(
        repo="jarvis", router=_SpyRouter(), test_watcher=watcher,
    )
    captured = await _drive_one_poll_cycle(sensor, tfm, monkeypatch)

    assert watcher.poll_calls == 0, (
        "event-primary lane armed: the whole-suite sweep must be fully "
        f"skipped, but poll_once ran {watcher.poll_calls}x"
    )
    assert captured == [600.0], (
        f"derated cycle must still sleep the fallback interval, got {captured!r}"
    )


@pytest.mark.asyncio
async def test_sensor_poll_loop_escape_hatch_forces_sweep(monkeypatch):
    """Escape hatch set -> poll_once IS called; interval demotion (600s)
    still applies because the event lane remains armed."""
    from backend.core.ouroboros.governance.intake.sensors import (
        test_failure_sensor as tfm,
    )
    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_INTENT_POLL_WHEN_EVENT_PRIMARY", "true")
    monkeypatch.setattr(tfm, "_TEST_FAILURE_FALLBACK_INTERVAL_S", 600.0)

    watcher = _StubWatcher(poll_interval_s=30.0)
    sensor = tfm.TestFailureSensor(
        repo="jarvis", router=_SpyRouter(), test_watcher=watcher,
    )
    captured = await _drive_one_poll_cycle(sensor, tfm, monkeypatch)

    assert watcher.poll_calls == 1, (
        f"escape hatch must force the sweep, poll_once ran {watcher.poll_calls}x"
    )
    assert captured == [600.0], (
        f"forced sweep under armed lane keeps the demoted interval, got {captured!r}"
    )


@pytest.mark.asyncio
async def test_sensor_poll_loop_polls_at_legacy_interval_when_lane_off(monkeypatch):
    """Event lane off -> legacy behavior byte-identical: poll_once called,
    watcher's own interval (30s) used."""
    from backend.core.ouroboros.governance.intake.sensors import (
        test_failure_sensor as tfm,
    )
    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", "false")
    monkeypatch.delenv("JARVIS_INTENT_POLL_WHEN_EVENT_PRIMARY", raising=False)

    watcher = _StubWatcher(poll_interval_s=30.0)
    sensor = tfm.TestFailureSensor(
        repo="jarvis", router=_SpyRouter(), test_watcher=watcher,
    )
    captured = await _drive_one_poll_cycle(sensor, tfm, monkeypatch)

    assert watcher.poll_calls == 1, (
        f"lane off: legacy poll must run, poll_once ran {watcher.poll_calls}x"
    )
    assert captured == [30.0], (
        f"lane off must keep the watcher's legacy interval, got {captured!r}"
    )
