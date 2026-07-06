"""Vector observation path — streak semantics under aborted runs + FS
confirmation re-run.

Live-fire a1-brain-20260706-014931: the injected vector's FAILED was
observed exactly once (scoped fs.changed run, rc=1). Every full-suite run
died rc=3 (a diagnostic script sys.exit'ing at import — fixed separately),
so the poll path never carried the vector's failure again and the
2-consecutive-runs stability gate could never complete. Two structural
gaps, each pinned here:

1. NO-INFORMATION RUNS: pytest rc 2/3/4/5 (interrupted / INTERNALERROR /
   usage error / no tests collected) means the run carries NO evidence
   about any test. ``poll_once`` must skip the cycle and preserve streaks
   — exactly like the existing rc=-1 timeout guard. Before this fix an
   aborted sweep RESET every streak (absence-by-abort read as passing).
2. FS CONFIRMATION RE-RUN: an fs.changed-scoped detection that observes a
   NEW failure (streak 1) re-runs the same scoped targets once,
   immediately — a deterministic failure reaches the stability gate in
   seconds instead of waiting for the 600s poll fallback (which the A1
   soak wall outlives).
"""
from __future__ import annotations

import asyncio
from typing import Any, List, Optional, Sequence, Tuple

import pytest

from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher


def _watcher(tmp_path) -> TestWatcher:
    return TestWatcher(
        repo="jarvis",
        test_dir=str(tmp_path / "tests"),
        repo_path=str(tmp_path),
    )


class _ScriptedPytest:
    """Deterministic run_pytest stand-in: pops (output, exit_code) pairs."""

    def __init__(self, results: List[Tuple[str, int]]) -> None:
        self.results = list(results)
        self.calls: List[Optional[Sequence[str]]] = []

    async def __call__(
        self, target_paths: Optional[Sequence[str]] = None
    ) -> Tuple[str, int]:
        self.calls.append(target_paths)
        return self.results.pop(0)


_FAILED_LINE = (
    "FAILED tests/governance/a1_ignition_vector/test_leaf_predicates.py::"
    "test_clamp01 - assert 0 == 1"
)


# ---------------------------------------------------------------------------
# 1. No-information exit codes preserve streaks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rc", [2, 3, 4, 5])
def test_aborted_run_preserves_streaks(tmp_path, rc, monkeypatch):
    w = _watcher(tmp_path)
    scripted = _ScriptedPytest([
        (_FAILED_LINE + "\n", 1),       # observation 1 -> streak 1
        ("INTERNALERROR> boom\n", rc),  # aborted run -> NO information
        (_FAILED_LINE + "\n", 1),       # observation 2 -> streak 2 -> emit
    ])
    monkeypatch.setattr(w, "run_pytest", scripted)

    async def _drive():
        s1 = await w.poll_once()
        s2 = await w.poll_once()
        s3 = await w.poll_once()
        return s1, s2, s3

    s1, s2, s3 = asyncio.run(_drive())
    assert s1 == [], "first observation must not emit (streak 1)"
    assert s2 == [], "aborted run must not emit"
    assert s3, (
        "rc=%d between two genuine failures RESET the streak — "
        "absence-by-abort was read as passing (the a1-brain-20260706-"
        "014931 starvation class)" % rc
    )


def test_green_run_still_resets_streaks(tmp_path, monkeypatch):
    """rc=0 is REAL information — the legacy reset semantics stay intact."""
    w = _watcher(tmp_path)
    scripted = _ScriptedPytest([
        (_FAILED_LINE + "\n", 1),
        ("all passed\n", 0),            # genuine green -> reset
        (_FAILED_LINE + "\n", 1),       # streak restarts at 1 -> no emit
    ])
    monkeypatch.setattr(w, "run_pytest", scripted)

    async def _drive():
        await w.poll_once()
        await w.poll_once()
        return await w.poll_once()

    assert asyncio.run(_drive()) == []


# ---------------------------------------------------------------------------
# 2. FS-scoped detection confirms immediately
# ---------------------------------------------------------------------------


def test_fs_scoped_red_confirms_immediately(tmp_path, monkeypatch):
    """A scoped fs.changed run observing a NEW failure re-runs the same
    targets once — emission within seconds, no 600s poll dependency."""
    from backend.core.ouroboros.governance.intake.sensors import (
        test_failure_sensor as tfs,
    )

    w = _watcher(tmp_path)
    scripted = _ScriptedPytest([
        (_FAILED_LINE + "\n", 1),   # scoped run 1 -> streak 1
        (_FAILED_LINE + "\n", 1),   # confirmation  -> streak 2 -> emit
    ])
    monkeypatch.setattr(w, "run_pytest", scripted)

    emitted: List[Any] = []

    sensor = tfs.TestFailureSensor.__new__(tfs.TestFailureSensor)
    sensor._watcher = w

    async def _handle(signals):
        emitted.extend(signals)

    sensor.handle_signals = _handle  # type: ignore[method-assign]

    targets = ["tests/governance/a1_ignition_vector/test_leaf_predicates.py"]
    asyncio.run(sensor._run_scoped_with_confirmation(targets))

    assert len(scripted.calls) == 2, "confirmation re-run did not fire"
    assert scripted.calls[0] == targets and scripted.calls[1] == targets
    assert emitted, "deterministic RED must emit after the confirmation run"


def test_fs_confirmation_skipped_when_first_run_green(tmp_path, monkeypatch):
    from backend.core.ouroboros.governance.intake.sensors import (
        test_failure_sensor as tfs,
    )

    w = _watcher(tmp_path)
    scripted = _ScriptedPytest([("all passed\n", 0)])
    monkeypatch.setattr(w, "run_pytest", scripted)

    sensor = tfs.TestFailureSensor.__new__(tfs.TestFailureSensor)
    sensor._watcher = w

    async def _handle(signals):  # pragma: no cover - must not fire
        raise AssertionError("no signals expected")

    sensor.handle_signals = _handle  # type: ignore[method-assign]

    asyncio.run(sensor._run_scoped_with_confirmation(["tests/x_test.py"]))
    assert len(scripted.calls) == 1, "green first run must not re-run"


def test_fs_confirmation_env_kill_switch(tmp_path, monkeypatch):
    from backend.core.ouroboros.governance.intake.sensors import (
        test_failure_sensor as tfs,
    )

    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_CONFIRM_ENABLED", "false")
    w = _watcher(tmp_path)
    scripted = _ScriptedPytest([(_FAILED_LINE + "\n", 1)])
    monkeypatch.setattr(w, "run_pytest", scripted)

    sensor = tfs.TestFailureSensor.__new__(tfs.TestFailureSensor)
    sensor._watcher = w

    async def _handle(signals):
        pass

    sensor.handle_signals = _handle  # type: ignore[method-assign]

    asyncio.run(sensor._run_scoped_with_confirmation(["tests/x_test.py"]))
    assert len(scripted.calls) == 1, "kill switch must disable the re-run"
