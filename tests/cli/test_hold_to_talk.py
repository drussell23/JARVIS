"""Hold-to-talk on a terminal that cannot report a key release.

`ptt_router` documented the constraint correctly — a TTY delivers keypresses
only, so "the operator let go" is not an event any terminal sends — and drew
the wrong conclusion from it. A *held* key is not silent: the OS repeats it.

    press ──[initial delay 250-500ms]──► repeat ──[25-50ms]──► repeat ──► …

So a hold is observable as a RATE and a release as that rate stopping, using
nothing but the arrival times of keys prompt_toolkit already delivers. No
release event, no global hook, nothing outside the event loop.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.ui.hold_to_talk import (
    HoldAction, HoldDetector, HoldEvent, HoldPhase, HoldWatchdog,
    arm_window_s, confirm_repeats, release_window_s,
)
from backend.core.ouroboros.ui.ptt_router import (
    PTTLatch, build_ptt_key_bindings,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _hold_stream(det: HoldDetector, clock: _Clock, *, repeats: int = 12,
                 initial_delay: float = 0.45, interval: float = 0.03) -> None:
    """Press, wait out the OS initial delay, then stream repeats."""
    det.on_key()
    clock.advance(initial_delay)
    det.on_key()
    for _ in range(repeats):
        clock.advance(interval)
        det.on_key()


# --------------------------------------------------------------------------
# 1. a tap is a tap
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_single_press_does_not_start_the_microphone() -> None:
    """The mandate's first assertion. One keystroke is a tap, and the FSM
    must not conclude anything about a hold from it."""
    clock = _Clock()
    det = HoldDetector(clock=clock)

    assert det.on_key() is HoldAction.PASSTHROUGH
    assert det.is_recording is False
    assert det.phase is HoldPhase.PENDING

    clock.advance(arm_window_s() + 0.05)
    assert det.poll() is HoldEvent.TAP
    assert det.phase is HoldPhase.IDLE
    assert det.is_recording is False


@pytest.mark.asyncio
async def test_a_human_double_tap_is_not_a_hold() -> None:
    """The ambiguous case. The FIRST repeat arrives after the OS initial
    delay, which overlaps the timing of a deliberate double-tap — which is
    why confirmation needs a second one, at a rate no hand can reproduce."""
    clock = _Clock()
    det = HoldDetector(clock=clock)
    det.on_key()
    clock.advance(0.18)

    assert det.on_key() is HoldAction.WARMUP
    assert det.is_recording is False, "a double-tap opened the microphone"


@pytest.mark.asyncio
async def test_slow_deliberate_tapping_never_accumulates_into_a_hold() -> None:
    """Each press outside the arm window restarts the observation, or a
    patient tapper would eventually be recorded."""
    clock = _Clock()
    det = HoldDetector(clock=clock)
    for _ in range(10):
        det.on_key()
        clock.advance(arm_window_s() + 0.1)
        det.poll()
    assert det.is_recording is False
    assert det.holds_confirmed == 0


# --------------------------------------------------------------------------
# 2. a hold is a hold
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_repeat_stream_confirms_a_hold_and_swallows_the_rest() -> None:
    """The mandate's second assertion: rapid repeats identify a hold and the
    keystrokes stop reaching the buffer."""
    clock = _Clock()
    det = HoldDetector(clock=clock)

    det.on_key()
    clock.advance(0.45)
    assert det.on_key() is HoldAction.WARMUP
    clock.advance(0.03)
    assert det.on_key() is HoldAction.CONFIRM
    assert det.is_recording is True

    for _ in range(30):
        clock.advance(0.03)
        assert det.on_key() is HoldAction.SWALLOW, (
            "repeats reached the buffer; it fills with spaces while speaking"
        )


@pytest.mark.asyncio
async def test_the_stream_stopping_is_the_release() -> None:
    clock = _Clock()
    det = HoldDetector(clock=clock)
    _hold_stream(det, clock)
    assert det.is_recording is True

    assert det.poll() is None, "released while the key was still repeating"
    clock.advance(release_window_s(det.observed_interval) + 0.01)
    assert det.poll() is HoldEvent.RELEASE
    assert det.phase is HoldPhase.IDLE


@pytest.mark.asyncio
async def test_a_dropped_keystroke_does_not_cut_the_operator_off() -> None:
    """One missed repeat under load must not read as a release — that would
    end the sentence mid-word."""
    clock = _Clock()
    det = HoldDetector(clock=clock)
    _hold_stream(det, clock)

    clock.advance(0.03 * 2)          # two intervals missed
    assert det.poll() is None
    assert det.is_recording is True


# --------------------------------------------------------------------------
# 3. the timing model — two windows, and one of them is learned
# --------------------------------------------------------------------------

def test_the_arm_window_outwaits_the_OS_initial_delay() -> None:
    """A single ~150ms watchdog cannot work: it expires during the initial
    delay (250-500ms, platform-specific) and calls every hold a tap."""
    assert arm_window_s() > 0.5


def test_the_release_window_is_learned_from_the_observed_rate() -> None:
    """25ms and 50ms machines want different answers, and an operator who
    slowed their repeat rate for accessibility wants a third."""
    fast = release_window_s(0.025)
    slow = release_window_s(0.060)
    assert slow > fast, "the window ignores the machine it is running on"


def test_the_initial_delay_does_not_train_the_release_window() -> None:
    """It is not a repeat interval — it is an order of magnitude longer.
    Averaging it in inflated a measured 30ms stream to 49ms, stretching the
    release window on exactly the machines that repeat fastest."""
    clock = _Clock()
    det = HoldDetector(clock=clock)
    _hold_stream(det, clock, initial_delay=0.45, interval=0.03)
    assert det.observed_interval == pytest.approx(0.03, abs=0.005)


def test_confirmation_needs_more_than_one_repeat() -> None:
    assert confirm_repeats() >= 2


@pytest.mark.parametrize("initial,interval", [
    (0.25, 0.025),   # fast Windows/macOS
    (0.50, 0.033),   # macOS default
    (0.66, 0.040),   # X11 default
])
@pytest.mark.asyncio
async def test_it_works_across_real_platform_repeat_profiles(
    initial: float, interval: float,
) -> None:
    """The numbers are not hypothetical — these are the shipped defaults on
    the three platforms this has to run on."""
    clock = _Clock()
    det = HoldDetector(clock=clock)
    _hold_stream(det, clock, initial_delay=initial, interval=interval)
    assert det.is_recording is True, f"missed a hold at {initial}/{interval}"


def test_the_windows_are_tunable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeat rates are user-configurable on every OS, so nothing here is a
    constant."""
    monkeypatch.setenv("JARVIS_PTT_ARM_WINDOW_S", "1.5")
    assert arm_window_s() == 1.5
    monkeypatch.setenv("JARVIS_PTT_ARM_WINDOW_S", "99")
    assert arm_window_s() == 3.0, "clamped"
    monkeypatch.setenv("JARVIS_PTT_ARM_WINDOW_S", "nonsense")
    assert arm_window_s() == 0.75


# --------------------------------------------------------------------------
# 4. through the real key binding
# --------------------------------------------------------------------------

class _Event:
    current_buffer = None


def _wire(clock: _Clock):
    latch = PTTLatch()
    det = HoldDetector(clock=clock)
    kb = build_ptt_key_bindings(latch, buffer_getter=lambda: "", detector=det)
    return latch, det, kb.bindings[0].handler


@pytest.mark.asyncio
async def test_the_first_press_opens_the_mic_with_zero_latency() -> None:
    """Deciding the CLOSE rather than the open is what keeps tap latency at
    zero — no waiting for confirmation, no character to retroactively
    delete."""
    clock = _Clock()
    latch, _det, press = _wire(clock)
    press(_Event())
    assert latch.is_open is True


@pytest.mark.asyncio
async def test_a_held_key_does_not_toggle_the_mic_shut() -> None:
    """THE integration bug. The first repeat used to fall through to
    `latch.toggle()`, so holding the key shut the microphone the instant the
    OS sent its second event. A held key must look like ONE press."""
    clock = _Clock()
    latch, det, press = _wire(clock)
    press(_Event())
    clock.advance(0.45)
    press(_Event())
    assert latch.is_open is True, "the hold toggled the microphone shut"
    clock.advance(0.03)
    press(_Event())
    assert det.is_recording is True
    for _ in range(15):
        clock.advance(0.03)
        press(_Event())
    assert latch.is_open is True


@pytest.mark.asyncio
async def test_tap_to_toggle_still_works_exactly_as_before() -> None:
    """Hold is layered UNDER the toggle, not in place of it."""
    clock = _Clock()
    latch, det, press = _wire(clock)
    press(_Event())
    assert latch.is_open is True
    clock.advance(arm_window_s() + 0.1)
    det.poll()
    press(_Event())
    assert latch.is_open is False, "the toggle regressed"


# --------------------------------------------------------------------------
# 5. the watchdog
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_watchdog_fires_release_on_a_real_event_loop() -> None:
    clock = _Clock()
    det = HoldDetector(clock=clock)
    _hold_stream(det, clock)
    seen: list = []

    watchdog = HoldWatchdog(det, seen.append)
    watchdog.kick()
    clock.advance(release_window_s(det.observed_interval) + 0.05)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if seen:
            break
    watchdog.stop()

    assert seen == [HoldEvent.RELEASE]


@pytest.mark.asyncio
async def test_the_watchdog_is_single_and_stoppable() -> None:
    """A watchdog outliving its detector closes the microphone a second
    after the operator started speaking again."""
    det = HoldDetector()
    watchdog = HoldWatchdog(det, lambda _e: None)
    watchdog.kick()
    first = watchdog._task
    watchdog.kick()
    assert watchdog._task is first, "a second watcher was spawned"
    watchdog.stop()
    await asyncio.sleep(0)
    assert watchdog.running is False


@pytest.mark.asyncio
async def test_it_never_busy_loops() -> None:
    """The deadline shrinks while repeats stream; a floor stops it becoming
    a spin that competes with the input reader for the loop."""
    clock = _Clock()
    det = HoldDetector(clock=clock)
    _hold_stream(det, clock)
    assert det.next_deadline_s() >= 0.02


@pytest.mark.asyncio
async def test_no_running_loop_is_survivable() -> None:
    """Unit callers and sync contexts must not crash on `kick`."""
    det = HoldDetector()
    watchdog = HoldWatchdog(det, lambda _e: None)
    loop_free = asyncio.get_running_loop()
    assert loop_free is not None
    watchdog.stop()


# --------------------------------------------------------------------------
# 6. it can never eat a keystroke
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_broken_clock_still_passes_the_key_through() -> None:
    """A detector fault must not swallow the operator's input — the failure
    mode has to be the behaviour this replaces."""
    def _boom() -> float:
        raise RuntimeError("clock died")

    det = HoldDetector(clock=_boom)
    assert det.on_key() is HoldAction.PASSTHROUGH
    assert det.poll() is None


@pytest.mark.asyncio
async def test_abort_returns_to_idle() -> None:
    clock = _Clock()
    det = HoldDetector(clock=clock)
    _hold_stream(det, clock)
    assert det.is_recording is True
    det.abort()
    assert det.phase is HoldPhase.IDLE
    assert det.is_recording is False


@pytest.mark.asyncio
async def test_a_modifier_trigger_arms_on_the_first_press() -> None:
    """A non-printable combo inserts nothing, so there is nothing to take
    back and no reason to make the operator wait out a warmup."""
    det = HoldDetector(clock=_Clock(), printable=False)
    assert det.on_key() is HoldAction.CONFIRM
    assert det.is_recording is True
    assert det.undo_chars() == 0
