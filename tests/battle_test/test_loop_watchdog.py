"""The loop watchdog's detector, including the case that motivates its shape.

The interesting assertions here are not "does it compute a median". They are:

  * a mean+stdev threshold MISSES the stall that median+MAD catches, on real
    numbers rather than by argument;
  * a sustained stall keeps firing instead of teaching the detector that
    stalling is normal;
  * a perfectly regular loop, where MAD collapses to zero, does not produce a
    verdict on every microsecond of jitter.

Every one of those is a way a latency detector silently stops detecting, which
is worse than not having one: it converts "nobody was watching" into "we
checked and it was fine".
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.core.ouroboros.battle_test.loop_watchdog import (  # noqa: E402
    LatencyWindow,
    LoopLatencyWatchdog,
)


# ---------------------------------------------------------------------------
# the window
# ---------------------------------------------------------------------------

def test_the_window_is_bounded() -> None:
    """An anomaly must age out, or the baseline never recovers."""
    w = LatencyWindow(4)
    for v in (1.0, 2.0, 3.0, 4.0, 5.0):
        w.add(v)
    assert len(w) == 4
    assert w.samples == (2.0, 3.0, 4.0, 5.0)


@pytest.mark.parametrize(
    "values, median",
    [
        ([1.0], 1.0),
        ([1.0, 3.0], 2.0),
        ([3.0, 1.0, 2.0], 2.0),
        ([], 0.0),
    ],
)
def test_median(values, median) -> None:
    w = LatencyWindow(64)
    for v in values:
        w.add(v)
    assert w.median() == pytest.approx(median)


def test_mad_is_a_sigma_equivalent() -> None:
    """Scaled by 1.4826 so "k sigmas" keeps meaning k sigmas."""
    w = LatencyWindow(64)
    for v in (1.0, 2.0, 3.0, 4.0, 5.0):
        w.add(v)
    # median 3, deviations (2,1,0,1,2) -> MAD 1 -> sigma-equivalent 1.4826
    assert w.mad_sigma() == pytest.approx(1.4826, rel=1e-4)


# ---------------------------------------------------------------------------
# THE case the design exists for
# ---------------------------------------------------------------------------

def test_stdev_masks_the_stall_that_mad_catches() -> None:
    """The empirical argument for not using mean+stdev.

    A latency window is heavy-tailed and the samples being hunted ARE the tail,
    so each stall inflates the very deviation meant to catch the next one. On
    these numbers a 6-sigma stdev threshold sits ABOVE the stall it is supposed
    to flag, while the MAD estimate is unmoved.
    """
    quiet, stall = 0.001, 0.5
    w = LatencyWindow(64)
    for _ in range(9):
        w.add(quiet)
    w.add(stall)

    stdev_threshold = w.mean() + 6.0 * w.stdev()
    mad_threshold = w.median() + 6.0 * w.mad_sigma()

    assert stall < stdev_threshold, (
        "premise broken: stdev no longer masks the stall on these numbers"
    )
    assert mad_threshold < stall, (
        "the MAD estimate was dragged by the outlier it is supposed to resist"
    )
    # And the contrast is reported rather than merely relied upon.
    assert w.stdev() > w.mad_sigma() * 10


def test_a_sustained_stall_does_not_become_the_new_normal() -> None:
    """The masking failure by the back door.

    If anomalous samples entered the baseline, a storm would raise the median
    until stalling looked ordinary and the detector fell silent — precisely
    when it is most needed.
    """
    wd = LoopLatencyWatchdog(
        interval_s=0.01, window=32, sigma=6.0, min_samples=8,
        interval_floor_multiple=0.5,
    )
    for _ in range(16):
        assert wd.observe(0.0005) is None

    fired = sum(1 for _ in range(40) if wd.observe(0.4) is not None)
    assert fired == 40, (
        f"only {fired}/40 sustained stalls were reported — the baseline "
        f"absorbed them (median now {wd.snapshot()['median_s']}s)"
    )


# ---------------------------------------------------------------------------
# the degenerate and warm-up cases
# ---------------------------------------------------------------------------

def test_no_verdict_before_there_is_a_baseline() -> None:
    """A verdict from four samples is noise with a decimal point."""
    wd = LoopLatencyWatchdog(interval_s=0.01, min_samples=20, window=64)
    for _ in range(19):
        assert wd.observe(5.0) is None, "judged during warm-up"
    assert wd.ticks == 19


def test_a_perfectly_regular_loop_does_not_cry_wolf() -> None:
    """MAD collapses to zero on identical samples, which would make every
    microsecond of jitter infinitely many sigmas. The floor — expressed as a
    fraction of the sampling interval, not as a duration — is what keeps that
    from filling the log."""
    wd = LoopLatencyWatchdog(
        interval_s=0.25, window=64, sigma=6.0, min_samples=8,
        interval_floor_multiple=0.5,
    )
    for _ in range(32):
        wd.observe(0.0)
    assert wd._window.mad_sigma() == pytest.approx(0.0)

    # Well under the floor (0.25 * 0.5 = 0.125s): jitter, not a stall.
    assert wd.observe(0.01) is None
    assert wd.observe(0.05) is None
    # Comfortably over it: a real stall, and it must survive MAD being zero.
    assert wd.observe(0.4) is not None


def test_the_floor_scales_with_the_configured_interval() -> None:
    """Derived, not declared. A watchdog sampling ten times slower tolerates
    proportionally more lateness without anyone editing a constant."""
    fast = LoopLatencyWatchdog(interval_s=0.05, interval_floor_multiple=0.5,
                               min_samples=1, window=8)
    slow = LoopLatencyWatchdog(interval_s=0.50, interval_floor_multiple=0.5,
                               min_samples=1, window=8)
    for wd in (fast, slow):
        for _ in range(8):
            wd.observe(0.0)
    assert slow.threshold() == pytest.approx(fast.threshold() * 10.0)


def test_negative_lateness_is_clamped() -> None:
    """A sleep that returns early (coarse timers, clock granularity) is not
    evidence of anything and must not skew the baseline downward."""
    wd = LoopLatencyWatchdog(interval_s=0.01, min_samples=2, window=8)
    wd.observe(-0.5)
    wd.observe(-0.5)
    assert wd.snapshot()["median_s"] == pytest.approx(0.0)
    assert wd.worst_lateness == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# against a real event loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_it_detects_a_real_blocking_call_on_the_loop_thread() -> None:
    """The whole point, end to end.

    A synchronous sleep on the loop thread is exactly the shape of the bug
    class being hunted — `run_in_terminal` suspensions, a blocking write, a C
    call holding the GIL — and it must show up as lateness without anyone
    telling the watchdog it happened.
    """
    seen: "list[dict]" = []
    wd = LoopLatencyWatchdog(
        interval_s=0.02, window=64, sigma=6.0, min_samples=10,
        interval_floor_multiple=0.5, on_anomaly=seen.append,
    )
    assert wd.start() is True
    try:
        # Let a quiet baseline establish itself.
        await asyncio.sleep(0.02 * 15)
        # Block the loop thread outright.
        time.sleep(0.5)
        await asyncio.sleep(0.05)
    finally:
        wd.stop()

    assert seen, (
        f"a 0.5s block of the loop thread went unreported: {wd.snapshot()}"
    )
    assert seen[0]["lateness_s"] >= 0.3, seen[0]
    assert wd.worst_lateness >= 0.3


@pytest.mark.asyncio
async def test_a_quiet_loop_reports_nothing() -> None:
    """The false-positive floor. A detector that fires on a healthy loop gets
    muted, and a muted detector is the state this replaces."""
    seen: "list[dict]" = []
    # The DEFAULT floor, deliberately: this test's whole job is to catch the
    # shipped configuration crying wolf. An earlier 0.5 default failed here
    # with a 12.8 ms hiccup against a 10 ms floor, on a machine doing nothing
    # more unusual than running the rest of the suite.
    wd = LoopLatencyWatchdog(
        interval_s=0.02, window=64, sigma=6.0, min_samples=10,
        on_anomaly=seen.append,
    )
    assert wd.start() is True
    try:
        await asyncio.sleep(0.02 * 30)
    finally:
        wd.stop()
    assert not seen, f"cried wolf on an idle loop: {seen[:3]}"


def test_the_floor_is_at_least_one_sampling_period() -> None:
    """Resolution, not taste.

    A sampler cannot distinguish a stall shorter than its own period from the
    jitter of its own wakeup, so a floor below one interval reports noise with
    great statistical confidence.
    """
    wd = LoopLatencyWatchdog(interval_s=0.25, min_samples=1, window=8)
    for _ in range(8):
        wd.observe(0.0)
    assert wd.threshold() >= wd.interval_s


@pytest.mark.asyncio
async def test_start_and_stop_are_idempotent_and_never_raise() -> None:
    wd = LoopLatencyWatchdog(interval_s=0.01)
    assert wd.start() is True
    assert wd.start() is True          # second call is a no-op, not an error
    wd.stop()
    wd.stop()                          # stopping twice must be safe


def test_start_without_a_running_loop_declines_rather_than_raising() -> None:
    """Diagnostics must never be the reason a process fails to boot."""
    wd = LoopLatencyWatchdog(interval_s=0.01)
    assert wd.start() is False


def test_it_is_armed_on_the_surface_that_hosts_the_freeze() -> None:
    """Wired, and wired to the RIGHT surface.

    The reported freeze belongs to the `PromptSession` + `patch_stdout` attach
    path, not the full-screen cockpit. A watchdog installed on the other one
    would pass every unit test in this file and watch a loop nobody complained
    about — the same divergence that let a slash-palette test cover a surface
    with no `run_in_terminal` in it.

    Asserted on order, not merely presence: armed AFTER the mount and BEFORE
    the prompt loop, so it covers the loop it is measuring.
    """
    import inspect
    from backend.core.ouroboros.cli import ov

    source = inspect.getsource(ov._split_plane_loop)
    assert "install_loop_watchdog" in source, (
        "the watchdog is not armed on the attach prompt surface"
    )
    assert source.index("install_loop_watchdog") < source.index(
        "patch_stdout(raw=True)"
    ), "the watchdog is armed after the prompt loop it is supposed to measure"


def test_the_dump_target_is_the_same_log_the_signal_writes_to() -> None:
    """One file, whether the stacks were asked for or produced automatically.

    An operator debugging a wedge should not have to know which of two logs to
    read, and two producers opening independent handles would interleave two
    buffers into one file.
    """
    import inspect
    from backend.core.ouroboros.battle_test import loop_watchdog

    source = inspect.getsource(loop_watchdog.install_loop_watchdog)
    assert "crash_log_handle" in source, (
        "the watchdog opens its own dump target instead of sharing the "
        "SIGUSR1 trap's descriptor"
    )


@pytest.mark.asyncio
async def test_the_dump_deadline_is_rearmed_and_cancelled() -> None:
    """The C-level timer is the wedge detector; leaving one armed after stop
    would dump the stacks of a perfectly healthy process later on."""
    import faulthandler

    wd = LoopLatencyWatchdog(interval_s=0.01, min_samples=2, window=8)
    assert wd.start() is True
    try:
        await asyncio.sleep(0.08)
        assert wd._armed is True, "the dump timer was never armed"
    finally:
        wd.stop()
    assert wd._armed is False
    # Cancelling twice must be safe; if stop() left it armed this would be the
    # only sign until an unrelated process dumped its stacks.
    faulthandler.cancel_dump_traceback_later()
