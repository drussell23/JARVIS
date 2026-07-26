"""AdaptiveInputManager — bind the microphone the operator is near.

The constraint, measured on this machine across the investigation:

    known-good speech (transcribes)    crest 15.8dB   modulation 0.472
    the lid array at seating distance  crest 27.9dB   modulation 0.208

Crest is a ratio, so gain cannot move it: raising input volume 40 -> 85 lifted
level by 16dB and left crest identical. Only proximity changes it. These tests
pin the state machine that acts on that, and — more importantly — the paths
where it must refuse to act or must retreat.

Every collaborator is injected, so nothing here opens CoreAudio.
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np
import pytest

from backend.audio import adaptive_input as ai
from backend.audio.acoustic_quality import QualitySample


@pytest.fixture(autouse=True)
def _armed(monkeypatch: pytest.MonkeyPatch):
    """The manager is OFF by default (it can open a Continuity handshake and
    tear down a contended stream). Tests opt in explicitly."""
    monkeypatch.setenv("JARVIS_ADAPTIVE_INPUT", "1")


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _SD:
    """Minimal sounddevice stand-in: two inputs and one output."""

    def __init__(self) -> None:
        self.devices = [
            {"name": "MacBook Pro Microphone", "max_input_channels": 1},
            {"name": "Derek J. Russell Microphone", "max_input_channels": 1},
            {"name": "MacBook Pro Speakers", "max_input_channels": 0},
        ]

    def query_devices(self):
        return self.devices


def _far() -> QualitySample:
    """The lid array at seating distance — the measured failing profile."""
    return QualitySample(modulation=0.208, crest_db=27.9, rms=0.019, peak=0.84)


def _near() -> QualitySample:
    """A microphone close to the mouth — the measured passing profile."""
    return QualitySample(modulation=0.472, crest_db=15.8, rms=0.108, peak=0.67)


class _Bus:
    """Records rebinds; can be told to refuse specific devices."""

    def __init__(self, refuse: Optional[set] = None) -> None:
        self.calls: List[Optional[int]] = []
        self.refuse = refuse or set()
        self.bound: Optional[int] = 0

    async def rebind(self, index: Optional[int]) -> bool:
        self.calls.append(index)
        if index in self.refuse:
            return False
        self.bound = index
        return True


def _manager(bus: _Bus, clock: _Clock) -> ai.AdaptiveInputManager:
    def probe_factory(_index: int, seconds: float) -> np.ndarray:
        # Returned audio is irrelevant — from_audio is stubbed per test where
        # the score matters. This only proves a capture was attempted.
        return np.zeros(int(16000 * seconds), dtype=np.float32)

    m = ai.AdaptiveInputManager(
        rebind=bus.rebind, probe_factory=probe_factory, sd=_SD(), clock=clock,
    )
    m.note_builtin(0)
    return m


# --------------------------------------------------------------------------
# 1. arming
# --------------------------------------------------------------------------

def test_a_single_bad_utterance_does_not_arm() -> None:
    m = _manager(_Bus(), _Clock())
    m.observe(_far())
    assert not m.armed, "one cough must not trigger a device swap"


def test_sustained_high_crest_arms_the_manager() -> None:
    m = _manager(_Bus(), _Clock())
    for _ in range(ai.DEGRADED_RUN):
        m.observe(_far())
    assert m.armed


def test_good_capture_resets_the_run() -> None:
    m = _manager(_Bus(), _Clock())
    m.observe(_far())
    m.observe(_far())
    m.observe(_near())          # crest 15.8 — under the trigger
    m.observe(_far())
    assert not m.armed


# --------------------------------------------------------------------------
# 2. the rebind (requirement 1)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_high_crest_triggers_a_rebind_to_the_better_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """~28dB crest on the incumbent, a near-field challenger available ->
    best_device() picks it and the stream is re-bound."""
    bus, clock = _Bus(), _Clock()
    m = _manager(bus, clock)
    monkeypatch.setattr(
        ai.QualitySample, "from_audio",
        staticmethod(lambda *a, **k: _near()),
    )

    for _ in range(ai.DEGRADED_RUN):
        m.observe(_far())
    assert m.armed

    assert await m.on_speech() is True
    assert bus.calls == [1], "expected a rebind to the near-field device"
    assert bus.bound == 1
    assert m.rebinds == 1


@pytest.mark.asyncio
async def test_no_swap_when_nothing_beats_the_incumbent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'Stay' is a real answer — a rebind costs an utterance."""
    bus, clock = _Bus(), _Clock()
    m = _manager(bus, clock)
    monkeypatch.setattr(
        ai.QualitySample, "from_audio",
        staticmethod(lambda *a, **k: _far()),      # challenger is no better
    )
    for _ in range(ai.DEGRADED_RUN):
        m.observe(_far())

    assert await m.on_speech() is False
    assert bus.calls == []


@pytest.mark.asyncio
async def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_ADAPTIVE_INPUT", raising=False)
    bus = _Bus()
    m = _manager(bus, _Clock())
    for _ in range(ai.DEGRADED_RUN):
        m.observe(_far())
    assert await m.on_speech() is False
    assert bus.calls == []


@pytest.mark.asyncio
async def test_cooldown_prevents_thrashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus, clock = _Bus(), _Clock()
    m = _manager(bus, clock)
    monkeypatch.setattr(
        ai.QualitySample, "from_audio", staticmethod(lambda *a, **k: _near()),
    )
    for _ in range(ai.DEGRADED_RUN):
        m.observe(_far())
    assert await m.on_speech() is True

    for _ in range(ai.DEGRADED_RUN):
        m.observe(_far())
    clock.t += ai.COOLDOWN_S / 2
    assert await m.on_speech() is False, "swapped again inside the cooldown"
    assert len(bus.calls) == 1


# --------------------------------------------------------------------------
# 3. the circuit breaker (requirement 2)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_continuity_dropout_falls_back_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Continuity mic that stops delivering frames must return us to the
    built-in array — no exception, no wedged daemon."""
    bus, clock = _Bus(), _Clock()
    m = _manager(bus, clock)
    monkeypatch.setattr(
        ai.QualitySample, "from_audio", staticmethod(lambda *a, **k: _near()),
    )
    for _ in range(ai.DEGRADED_RUN):
        m.observe(_far())
    assert await m.on_speech() is True
    assert bus.bound == 1

    # The device goes silent: no capture frames for longer than the gap bound.
    for strike in range(ai.STARVE_STRIKES):
        clock.t += (ai.STARVATION_MS / 1000.0) * 2
        fell_back = await m.check_liveness()
    assert fell_back is True
    assert bus.bound == 0, "did not return to the built-in array"
    assert m.fallbacks == 1


@pytest.mark.asyncio
async def test_a_recovering_device_is_not_dropped_on_one_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One late buffer is a hiccup, not a dropout."""
    bus, clock = _Bus(), _Clock()
    m = _manager(bus, clock)
    monkeypatch.setattr(
        ai.QualitySample, "from_audio", staticmethod(lambda *a, **k: _near()),
    )
    for _ in range(ai.DEGRADED_RUN):
        m.observe(_far())
    await m.on_speech()

    clock.t += (ai.STARVATION_MS / 1000.0) * 2
    assert await m.check_liveness() is False      # strike 1 of 2
    m.note_capture_frame()                        # frames resume
    assert await m.check_liveness() is False
    assert bus.bound == 1, "dropped a device that recovered"


@pytest.mark.asyncio
async def test_a_failed_rebind_leaves_us_on_the_incumbent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus, clock = _Bus(refuse={1}), _Clock()
    m = _manager(bus, clock)
    monkeypatch.setattr(
        ai.QualitySample, "from_audio", staticmethod(lambda *a, **k: _near()),
    )
    for _ in range(ai.DEGRADED_RUN):
        m.observe(_far())

    assert await m.on_speech() is False
    assert bus.bound == 0
    assert m.rebinds == 0


@pytest.mark.asyncio
async def test_a_benched_device_is_never_offered_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise the manager oscillates onto a broken mic forever."""
    bus, clock = _Bus(refuse={1}), _Clock()
    m = _manager(bus, clock)
    monkeypatch.setattr(
        ai.QualitySample, "from_audio", staticmethod(lambda *a, **k: _near()),
    )
    for _ in range(ai.DEGRADED_RUN):
        m.observe(_far())
    await m.on_speech()                      # fails, benches device 1

    clock.t += ai.COOLDOWN_S * 2
    for _ in range(ai.DEGRADED_RUN):
        m.observe(_far())
    await m.on_speech()
    assert bus.calls.count(1) == 1, "retried a benched device"


@pytest.mark.asyncio
async def test_fallback_never_raises_even_if_the_builtin_refuses() -> None:
    """The last-resort path. If even the built-in will not start we must
    still return control cleanly — the daemon may not wedge."""
    class _Hostile(_Bus):
        async def rebind(self, index):
            self.calls.append(index)
            raise OSError("CoreAudio is gone")

    bus = _Hostile()
    m = _manager(bus, _Clock())
    assert await m.fall_back("test") is True


@pytest.mark.asyncio
async def test_observe_never_raises_on_garbage() -> None:
    m = _manager(_Bus(), _Clock())
    m.observe(None)                     # type: ignore[arg-type]
    m.observe("not a sample")           # type: ignore[arg-type]
    assert not m.armed


# --------------------------------------------------------------------------
# 4. DRY — scoring is acoustic_quality's, not a second copy
# --------------------------------------------------------------------------

def test_scoring_is_delegated_not_reimplemented() -> None:
    import inspect

    src = inspect.getsource(ai)
    assert "rank_devices" in src and "best_device" in src
    # No second scoring formula: the weights live in QualitySample.sqi alone.
    assert "0.45 *" not in src, "sqi weights duplicated into the manager"


def test_quality_sample_from_audio_uses_the_forensics_ring() -> None:
    """The one constructor both instruments share, so an incident file and a
    device score can never disagree about the same audio."""
    rate = 16000
    t = np.arange(int(2.0 * rate)) / rate
    env = 0.5 * (1.0 + np.sin(2 * np.pi * 4.0 * t))
    audio = (0.2 * env * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)

    s = QualitySample.from_audio(audio, rate)
    assert s.modulation > 0.0
    assert s.crest_db > 0.0
    assert 0.0 <= s.sqi <= 1.0


def test_from_audio_never_raises_on_empty_or_garbage() -> None:
    assert QualitySample.from_audio(np.zeros(0, np.float32), 16000).sqi >= 0.0
    assert QualitySample.from_audio(None, 16000).sqi >= 0.0
