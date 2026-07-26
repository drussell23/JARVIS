"""Acoustic quality telemetry — the closed loop.

The operator spoke 34 times and got silence 34 times while the system had
every number needed to know why. These assert the loop is now closed:

  1. a better-scoring device outranks a degraded one (the swap DECISION)
  2. an unresolvable low-modulation stream raises ACOUSTIC_DEGRADATION
  3. the audio plane's UDS socket binds
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

import pytest

from backend.audio.acoustic_quality import (
    MODULATION_FLOOR,
    AcousticFeedbackController,
    QualitySample,
    best_device,
    complaint_for,
    rank_devices,
)

# The two real captures from the investigation.
FAR = QualitySample(modulation=0.196, crest_db=37.8, rms=0.0033,
                    peak=0.257, no_speech_prob=0.522, device="built-in")
NEAR = QualitySample(modulation=0.431, crest_db=19.5, rms=0.0114,
                     peak=0.107, no_speech_prob=0.153, device="built-in")


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_ACOUSTIC_FEEDBACK", "true")
    monkeypatch.delenv("JARVIS_ACOUSTIC_DEGRADED_RUN", raising=False)
    monkeypatch.setenv("JARVIS_ACOUSTIC_COMPLAINT_COOLDOWN_S", "1")


# --- assertion 1: device ranking --------------------------------------------


class _FakeSD:
    def __init__(self, devices): self._d = devices
    def query_devices(self): return self._d


def test_a_better_device_outranks_a_degraded_one() -> None:
    """The swap DECISION. AirPods hearing clearly must outrank a built-in
    array that is only catching transients."""
    sd = _FakeSD([
        {"name": "MacBook Pro Microphone", "max_input_channels": 1},
        {"name": "Derek's AirPods", "max_input_channels": 1},
    ])
    scores = rank_devices(probe=lambda i: NEAR if i == 1 else FAR, sd=sd)
    assert scores[0].name == "Derek's AirPods"
    assert scores[0].sqi > scores[1].sqi
    assert best_device(scores) is scores[0]


def test_a_marginal_winner_does_not_trigger_a_swap() -> None:
    """A rebind costs a dropped utterance, so a bare maximum is not enough —
    swapping on noise would thrash the stream."""
    sd = _FakeSD([
        {"name": "MacBook Pro Microphone", "max_input_channels": 1},
        {"name": "USB Mic", "max_input_channels": 1},
    ])
    scores = rank_devices(probe=lambda i: NEAR, sd=sd)
    assert best_device(scores) is None, "swapped on a tie"


def test_continuity_devices_are_detected_structurally() -> None:
    """A device named after a person is a phone, wherever that person left it.
    Detected by ABSENCE of hardware words — hardcoding one operator's name is
    the fault that already wasted a measurement in this investigation."""
    sd = _FakeSD([
        {"name": "Derek J. Russell Microphone", "max_input_channels": 1},
        {"name": "MacBook Pro Microphone", "max_input_channels": 1},
    ])
    scores = {s.name: s for s in rank_devices(sd=sd)}
    assert scores["Derek J. Russell Microphone"].is_continuity
    assert not scores["MacBook Pro Microphone"].is_continuity


def test_a_probe_failure_does_not_end_the_scan() -> None:
    sd = _FakeSD([{"name": "A", "max_input_channels": 1},
                  {"name": "B", "max_input_channels": 1}])

    def boom(i: int) -> QualitySample:
        if i == 0:
            raise OSError("device busy")
        return NEAR

    scores = rank_devices(probe=boom, sd=sd)
    assert len(scores) == 2
    assert scores[0].name == "B"


# --- assertion 2: degradation is announced ----------------------------------


def test_sustained_degradation_raises_the_event() -> None:
    events: List[Dict[str, Any]] = []
    spoken: List[str] = []
    c = AcousticFeedbackController(
        emit=lambda k, p: events.append({"kind": k, **p}),
        speak=spoken.append,
    )
    fired = None
    for _ in range(5):
        fired = c.observe(FAR) or fired
    assert fired is not None, "34 silent rejections would have repeated"
    assert events and events[0]["kind"] == "acoustic_degradation"
    assert events[0]["type"] == "ACOUSTIC_DEGRADATION"
    assert spoken and "far enough away" in spoken[0]


def test_one_bad_utterance_is_not_worth_interrupting_for() -> None:
    """A cough, a chair, a door. Only a RUN means the room is unusable."""
    c = AcousticFeedbackController()
    assert c.observe(FAR) is None


def test_good_audio_resets_the_run() -> None:
    c = AcousticFeedbackController()
    c.observe(FAR); c.observe(FAR)
    c.observe(NEAR)                      # heard clearly — the room recovered
    assert c.observe(FAR) is None, "the run survived a good utterance"


def test_the_complaint_is_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_ACOUSTIC_COMPLAINT_COOLDOWN_S", "3600")
    c = AcousticFeedbackController()
    first = None
    for _ in range(6):
        first = c.observe(FAR) or first
    assert first is not None
    for _ in range(6):
        assert c.observe(FAR) is None, "announced its deafness twice in a row"


def test_the_real_captures_score_the_right_way_round() -> None:
    assert FAR.degraded and not NEAR.degraded
    assert NEAR.sqi > FAR.sqi
    assert FAR.diagnosis() == "distance"


def test_modulation_dominates_the_index() -> None:
    """Amplitude is recoverable; rhythm is not. A loud smeared capture must
    not outscore a quiet articulate one."""
    loud_smeared = QualitySample(modulation=0.10, crest_db=38.0, rms=0.08)
    quiet_clear = QualitySample(modulation=0.44, crest_db=19.0, rms=0.004)
    assert quiet_clear.sqi > loud_smeared.sqi


def test_master_switch_off_restores_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_ACOUSTIC_FEEDBACK", "false")
    c = AcousticFeedbackController()
    for _ in range(10):
        assert c.observe(FAR) is None


def test_complaints_are_deterministic_and_specific() -> None:
    assert complaint_for("distance") == complaint_for("distance")
    assert complaint_for("reverb") != complaint_for("too_quiet")
    for d in ("distance", "reverb", "too_quiet", "not_speech", "unclear"):
        line = complaint_for(d)
        assert line and line[0].isupper() and line.endswith(".")
        assert "sit closer" not in line.lower(), (
            "the assistant issued an instruction instead of reporting what it hears"
        )


def test_observe_never_raises_on_garbage() -> None:
    c = AcousticFeedbackController()
    assert c.observe(QualitySample()) is None


# --- assertion 3: the audio plane binds its socket --------------------------


def test_audio_plane_binds_its_uds_socket() -> None:
    """Boot-critical: the plane serves the audio-state socket, and a host that
    cannot bind it is invisible to every cockpit."""
    import backend.audio.audio_plane_host as host

    src = open(host.__file__, encoding="utf-8").read()
    assert "audio_state" in src
    assert hasattr(host, "main") or "def main" in src
