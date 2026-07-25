"""Zero-copy audio broadcast tap — the STT pipeline must never pay for the UI.

macOS CoreAudio refuses a second handle on a captured device, so the visualizer
has to observe the stream STT already owns. These tests pin the three properties
that make that safe STRUCTURALLY rather than by hope:

  (1) one injected chunk reaches BOTH the STT processor and the UI pump;
  (2) the pump reduces it to an RMS float and emits the broker event;
  (3) an observer that THROWS does not crash, block, or corrupt the STT flow.

Plus the properties that are easy to regress: zero-copy (no ``.copy()``),
non-blocking offer, and latest-wins shedding under a slow consumer.
"""

from __future__ import annotations

import math
import threading

import numpy as np
import pytest

from backend.core.ouroboros.ui.audio_pump import EVENT_AUDIO_LEVEL, AudioLevelPump
from backend.core.ouroboros.ui.audio_scope import AudioPlane, BrailleScope
from backend.voice.audio_broadcast_tap import (
    AudioBroadcastTap,
    get_default_tap,
    offer_to_default_tap,
    reset_default_tap,
)


@pytest.fixture(autouse=True)
def _clean_tap():
    reset_default_tap()
    yield
    reset_default_tap()


def _chunk(n=512, amp=0.5):
    return (np.sin(np.arange(n) / 5.0) * amp).astype(np.float32)


# ---------------------------------------------------------------------------
# (1) fan-out: STT + UI both see the chunk
# ---------------------------------------------------------------------------


def test_chunk_reaches_both_stt_and_ui_pump():
    """(1) One mic chunk, two consumers, neither starves the other."""
    stt_seen = []
    ui_seen = []

    tap = AudioBroadcastTap(sample_rate=16000)
    tap.subscribe(lambda view, sr: ui_seen.append((len(view), sr)))

    def fake_stt(chunk):
        # Mirrors the real seam: offer FIRST, then the STT work.
        tap.offer(chunk, sample_rate=16000)
        stt_seen.append(len(chunk))
        return True

    data = _chunk(512)
    assert fake_stt(data) is True
    assert tap.drain() is True

    assert stt_seen == [512], "STT did not receive its chunk"
    assert ui_seen == [(512, 16000)], "UI observer did not receive the chunk"


def test_offer_is_zero_copy_and_read_only():
    """No ``.copy()`` on a 48kHz stream, and the observer cannot mutate the
    buffer STT is about to process."""
    tap = AudioBroadcastTap()
    captured = {}
    tap.subscribe(lambda view, sr: captured.setdefault("view", view))

    src = _chunk(256)
    tap.offer(src)
    tap.drain()

    view = captured["view"]
    assert view.base is src or np.shares_memory(view, src), "tap copied the buffer"
    with pytest.raises(ValueError):
        view[0] = 1.0                      # read-only guard


def test_no_subscribers_means_the_capture_path_does_nothing():
    """Byte-identical capture when no visualizer is attached: the singleton is
    not even constructed."""
    assert offer_to_default_tap(_chunk()) is False
    tap = get_default_tap()                 # explicit construction
    assert tap.offered == 0
    assert offer_to_default_tap(_chunk()) is False, "offered with zero observers"
    tap.subscribe(lambda v, sr: None)
    assert offer_to_default_tap(_chunk()) is True


# ---------------------------------------------------------------------------
# (2) RMS float + broker event
# ---------------------------------------------------------------------------


def test_pump_computes_rms_and_emits_the_broker_event():
    """(2) The chunk becomes ONE float on an ``audio_level_changed`` event."""
    events = []
    scope = BrailleScope(width=20)
    pump = AudioLevelPump(
        scope=scope, publish=lambda et, op, p: events.append((et, p)),
    )

    tap = AudioBroadcastTap()
    tap.subscribe(lambda view, sr: pump.feed_frames(view, plane=AudioPlane.USER))

    tap.offer(_chunk(1024, amp=0.8))
    assert tap.drain() is True

    assert len(events) == 1
    et, payload = events[0]
    assert et == EVENT_AUDIO_LEVEL
    assert set(payload) == {"level", "plane"}, "raw audio leaked into the event"
    assert isinstance(payload["level"], float)
    assert 0.0 < payload["level"] <= 1.0
    assert scope.is_silent() is False


def test_rms_happens_on_the_consumer_not_the_capture_thread():
    """The capture side must perform NO arithmetic — offer() only stores a
    reference; the reduction happens in drain()."""
    computed = []
    tap = AudioBroadcastTap()
    tap.subscribe(lambda view, sr: computed.append(float(np.sqrt(np.mean(view**2)))))

    tap.offer(_chunk())
    assert computed == [], "RMS ran during offer() — that is the capture thread"
    tap.drain()
    assert len(computed) == 1, "RMS did not run on the consumer side"


def test_silence_yields_a_zero_level():
    events = []
    pump = AudioLevelPump(publish=lambda et, op, p: events.append(p["level"]))
    tap = AudioBroadcastTap()
    tap.subscribe(lambda view, sr: pump.feed_frames(view))

    tap.offer(np.zeros(512, dtype=np.float32))
    tap.drain()
    assert events == [0.0]


# ---------------------------------------------------------------------------
# (3) THE SAFETY PROPERTY: a faulting observer must not touch STT
# ---------------------------------------------------------------------------


def test_throwing_observer_does_not_interrupt_stt_flow():
    """(3) THE CORE ASSERTION: a UI observer that raises must not crash, block
    or corrupt transcription."""
    transcripts = []

    tap = AudioBroadcastTap()

    def _explode(view, sr):
        raise RuntimeError("visualizer exploded")

    tap.subscribe(_explode)

    def fake_stt(chunk):
        tap.offer(chunk)
        tap.drain()                        # the fault surfaces HERE
        transcripts.append("hello world")  # STT work continues
        return True

    for _ in range(5):
        assert fake_stt(_chunk()) is True

    assert transcripts == ["hello world"] * 5, "STT flow was interrupted"
    assert tap.observer_faults == 5, "faults were not counted"


def test_one_bad_observer_does_not_starve_a_good_one():
    good = []
    tap = AudioBroadcastTap()
    tap.subscribe(lambda v, sr: (_ for _ in ()).throw(ValueError("bad")))
    tap.subscribe(lambda v, sr: good.append(1))

    tap.offer(_chunk())
    tap.drain()

    assert good == [1], "a raising observer poisoned the others"
    assert tap.observer_faults == 1


def test_offer_never_raises_on_garbage():
    """The capture path must survive anything handed to it."""
    tap = AudioBroadcastTap()
    tap.subscribe(lambda v, sr: None)
    for bad in (None, "not audio", 42, object(), [1, 2, 3]):
        assert tap.offer(bad) in (True, False)   # never raises


def test_offer_does_not_block_when_the_consumer_holds_the_lock():
    """The capture thread must NEVER be parked by a visualizer — offer uses a
    non-blocking acquire and sheds instead of waiting."""
    tap = AudioBroadcastTap()
    tap.subscribe(lambda v, sr: None)

    tap._lock.acquire()                     # simulate a consumer mid-drain
    try:
        done = threading.Event()
        result = {}

        def _capture_thread():
            result["ok"] = tap.offer(_chunk())
            done.set()

        threading.Thread(target=_capture_thread, daemon=True).start()
        assert done.wait(timeout=1.0), "offer() BLOCKED on a held lock"
        assert result["ok"] is False
        assert tap.dropped >= 1
    finally:
        tap._lock.release()


def test_latest_wins_sheds_under_a_slow_consumer():
    """A slow UI must shed stale chunks, not accumulate them — for an amplitude
    monitor the newest sample is the only interesting one."""
    tap = AudioBroadcastTap()
    seen = []
    tap.subscribe(lambda v, sr: seen.append(float(v[0])))

    for i in range(50):                     # 50 offers, zero drains
        c = np.full(4, float(i), dtype=np.float32)
        tap.offer(c)

    assert tap.drain() is True
    assert len(seen) == 1, "mailbox accumulated instead of shedding"
    assert seen[0] == 49.0, "kept a stale chunk instead of the newest"
    assert tap.dropped == 49

    assert tap.drain() is False, "slot should be empty after a take"


def test_unsubscribe_stops_delivery():
    hits = []
    tap = AudioBroadcastTap()
    unsub = tap.subscribe(lambda v, sr: hits.append(1))
    tap.offer(_chunk()); tap.drain()
    assert hits == [1]

    unsub()
    tap.offer(_chunk()); tap.drain()
    assert hits == [1], "observer still received after unsubscribe"
    assert tap.observer_count == 0


def test_stats_report_shedding_for_diagnosis():
    tap = AudioBroadcastTap()
    tap.subscribe(lambda v, sr: None)
    for _ in range(3):
        tap.offer(_chunk())
    tap.drain()
    st = tap.stats()
    assert st["offered"] == 3 and st["drained"] == 1 and st["dropped"] == 2
    assert st["observers"] == 1


# ---------------------------------------------------------------------------
# the real capture seam
# ---------------------------------------------------------------------------


def test_streaming_processor_offers_before_any_stt_work():
    """The seam must sit at the TOP of feed_audio, before dtype conversion, so
    the visualizer never adds latency to the STT critical path."""
    import inspect

    from backend.voice.streaming_processor import StreamingAudioProcessor

    src = inspect.getsource(StreamingAudioProcessor.feed_audio)
    assert "offer_to_default_tap" in src, "capture seam is not tapped"
    assert src.index("offer_to_default_tap") < src.index("astype(np.float32)"), (
        "tap runs after STT work began"
    )
    assert src.index("offer_to_default_tap") < src.index("_create_chunks"), (
        "tap runs after chunking"
    )


def test_tap_failure_cannot_break_feed_audio(monkeypatch):
    """Even a broken tap module must leave transcription working."""
    import backend.voice.audio_broadcast_tap as tapmod

    def _explode(*a, **k):
        raise RuntimeError("tap module is broken")

    monkeypatch.setattr(tapmod, "offer_to_default_tap", _explode)

    from backend.voice.streaming_processor import StreamingAudioProcessor

    proc = StreamingAudioProcessor(
        process_callback=lambda chunk: None, sample_rate=16000,
    )
    # Must not raise despite the tap exploding.
    proc.feed_audio(_chunk(2048))
