"""Lossy IPC telemetry valve + client-side smoothing.

The boundary being crossed (from audio_state_ipc's own docstring): the
daemonized supervisor owns the audio hardware absolutely; `ov` is a thin client
that subscribes over a Unix socket. The visualizer therefore cannot read the
mic, and amplitude must cross as data.

Two mandated assertions:

  (1) with the client's write buffer artificially full, the capture path drops
      the RMS frame without blocking and without raising;
  (2) the client parses the extended protocol and updates the visualizer.

Plus the property that makes the valve *correct* rather than merely safe: state
events must still be DELIVERED while telemetry is dropped. A lost VAD_ACTIVE
corrupts the client's model; a lost amplitude sample costs one frame.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from backend.audio.mic_telemetry_bridge import (
    LevelSmoother,
    MicTelemetryBridge,
    parse_rms_frame,
)
from backend.core.ouroboros.governance.comms.duplex import audio_state_ipc as ipc


# ---------------------------------------------------------------------------
# fakes that mirror the REAL asyncio contracts
# ---------------------------------------------------------------------------


class _FakeTransport:
    """Mirrors the one asyncio method the valve consults."""

    def __init__(self, queued: int = 0) -> None:
        self._queued = queued

    def get_write_buffer_size(self) -> int:
        return self._queued


class _FakeWriter:
    """asyncio.StreamWriter contract: `write` NEVER blocks, it buffers."""

    def __init__(self, queued: int = 0, closing: bool = False) -> None:
        self.transport = _FakeTransport(queued)
        self.written: list = []
        self._closing = closing
        self.closed = False

    def is_closing(self) -> bool:
        return self._closing

    def write(self, data: bytes) -> None:
        self.written.append(data)
        # Real writers grow their buffer on write — model that, so a test that
        # forgets the valve would see the buffer climb.
        self.transport._queued += len(data)

    def close(self) -> None:
        self.closed = True


def _server(*writers):
    srv = ipc.AudioStateServer() if hasattr(ipc, "AudioStateServer") else None
    if srv is None:                       # class name drift — find it
        cls = next(
            v for k, v in vars(ipc).items()
            if isinstance(v, type) and hasattr(v, "publish_rms")
        )
        srv = cls()
    srv._clients = set(writers)
    return srv


def _frames(w):
    return [json.loads(d.decode()) for d in w.written]


# ---------------------------------------------------------------------------
# (1) full buffer -> drop, no block, no raise
# ---------------------------------------------------------------------------


def test_full_write_buffer_drops_the_rms_frame():
    """(1) THE VALVE: a lagging client sheds telemetry instead of queueing."""
    lagging = _FakeWriter(queued=ipc.rms_drop_watermark_bytes() + 1)
    srv = _server(lagging)

    for _ in range(50):
        srv.publish_rms(0.7, "user")       # must not block or raise

    assert lagging.written == [], "telemetry was queued behind a lagging client"
    assert srv.rms_stats["dropped"] == 50
    assert srv.rms_stats["sent"] == 0
    assert lagging.closed is False, "a lagging client must NOT be disconnected"


def test_drained_client_receives_frames():
    healthy = _FakeWriter(queued=0)
    srv = _server(healthy)
    srv.publish_rms(0.5, "user")
    assert len(healthy.written) == 1
    assert srv.rms_stats["sent"] == 1


def test_valve_reopens_when_the_client_catches_up():
    """Dropping is transient, not a latch — a recovered client resumes."""
    w = _FakeWriter(queued=ipc.rms_drop_watermark_bytes() + 10)
    srv = _server(w)
    srv.publish_rms(0.4)
    assert srv.rms_stats["dropped"] == 1

    w.transport._queued = 0                # client drained
    srv.publish_rms(0.4)
    assert srv.rms_stats["sent"] == 1


def test_one_lagging_client_does_not_starve_a_healthy_one():
    lagging = _FakeWriter(queued=ipc.rms_drop_watermark_bytes() * 2)
    healthy = _FakeWriter(queued=0)
    srv = _server(lagging, healthy)

    srv.publish_rms(0.9)

    assert healthy.written, "healthy client starved by a lagging peer"
    assert lagging.written == []


def test_state_lane_ignores_the_watermark_that_drops_telemetry():
    """THE CORRECTNESS PROPERTY: a dropped VAD_ACTIVE would corrupt the
    client's model, so the DELIVERED lane must not consult the valve.

    Compared at the broadcast layer on purpose. `publish_event` marshals
    through `_enqueue` to the bound event loop, so with no loop running it
    writes nothing — which would make this pass for the wrong reason.
    `_broadcast` is the synchronous delivered path and is what the state lane
    ultimately calls."""
    over = ipc.rms_drop_watermark_bytes() + 1

    lossy_w = _FakeWriter(queued=over)
    srv_a = _server(lossy_w)
    srv_a.publish_rms(0.8)
    assert lossy_w.written == [], "telemetry ignored the watermark"

    delivered_w = _FakeWriter(queued=over)     # identically congested
    srv_b = _server(delivered_w)
    srv_b._broadcast({"type": "event", "kind": ipc.EVENT_VAD_ACTIVE})
    kinds = [f.get("kind") for f in _frames(delivered_w)]
    assert ipc.EVENT_VAD_ACTIVE in kinds, (
        "the state lane dropped an event — a lost VAD_ACTIVE corrupts state"
    )


def test_closing_client_is_dropped_not_written_to():
    w = _FakeWriter(queued=0, closing=True)
    srv = _server(w)
    srv.publish_rms(0.5)
    assert w.written == []


def test_publish_rms_never_raises_on_garbage():
    srv = _server(_FakeWriter())
    for bad in (None, "loud", float("nan"), object()):
        srv.publish_rms(bad)               # type: ignore[arg-type]


def test_rms_is_not_in_the_state_machine_vocabulary():
    """Telemetry is a SAMPLE, not a transition. Mixing them would let a
    deliberately-dropped frame look like a missed state change."""
    assert ipc.MSG_RMS_LEVEL not in ipc.EVENT_KINDS


def test_telemetry_can_be_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_AUDIO_IPC_RMS_ENABLED", "false")
    w = _FakeWriter()
    srv = _server(w)
    srv.publish_rms(0.9)
    assert w.written == []


# ---------------------------------------------------------------------------
# (2) client parses the extended protocol and updates the visualizer
# ---------------------------------------------------------------------------


def test_client_parses_the_rms_frame():
    """(2) The extended protocol round-trips."""
    w = _FakeWriter()
    srv = _server(w)
    srv.publish_rms(0.625, "system")

    frame = _frames(w)[0]
    parsed = parse_rms_frame(frame)
    assert parsed is not None
    level, plane = parsed
    assert level == pytest.approx(0.625)
    assert plane == "system"


def test_client_ignores_non_rms_frames():
    assert parse_rms_frame({"type": "event", "kind": "VAD_ACTIVE"}) is None
    assert parse_rms_frame({}) is None
    assert parse_rms_frame(None) is None
    assert parse_rms_frame("garbage") is None


def test_parsed_frame_drives_the_braille_scope():
    """End-to-end into the visualizer the operator actually sees."""
    from backend.core.ouroboros.ui.audio_scope import AudioPlane, BrailleScope

    w = _FakeWriter()
    srv = _server(w)
    scope = BrailleScope(width=20)
    import math

    for i in range(40):
        w.transport._queued = 0            # healthy client
        srv.publish_rms(abs(math.sin(i / 3.0)), "user")
    for frame in _frames(w):
        level, plane = parse_rms_frame(frame)
        scope.set_plane(AudioPlane(plane))
        scope.push(level)

    assert scope.is_silent() is False
    assert scope.plane is AudioPlane.USER
    assert len(scope.render()) == 20


# ---------------------------------------------------------------------------
# client-side smoothing
# ---------------------------------------------------------------------------


def test_smoother_interpolates_between_sparse_samples():
    t = {"now": 0.0}
    sm = LevelSmoother(alpha=0.5, clock=lambda: t["now"])
    sm.observe(1.0)
    a = sm.value()
    b = sm.value()
    assert 0.0 < a < 1.0 and a < b <= 1.0, "no interpolation between samples"


def test_smoother_decays_to_zero_when_the_stream_dies():
    """A frozen waveform reads as 'live and loud' — the worst failure for a
    monitor. Silence must fall to baseline."""
    t = {"now": 0.0}
    sm = LevelSmoother(alpha=0.5, silence_timeout_s=0.5, clock=lambda: t["now"])
    sm.observe(1.0)
    for _ in range(5):
        sm.value()
    assert sm.value() > 0.5

    t["now"] = 10.0                        # daemon stopped sending
    for _ in range(30):
        sm.value()
    assert sm.value() == 0.0, "meter froze instead of falling to baseline"


def test_smoother_clamps_and_survives_garbage():
    sm = LevelSmoother(alpha=0.5)
    sm.observe(5.0)
    assert sm.value() <= 1.0
    sm.observe(-3.0)
    assert sm.value() >= 0.0
    sm.observe("loud")                     # type: ignore[arg-type]
    assert 0.0 <= sm.value() <= 1.0


# ---------------------------------------------------------------------------
# the audio-thread hop
# ---------------------------------------------------------------------------


class _FakeTap:
    def __init__(self):
        self.offered = []
        self._slot = None

    def offer(self, chunk, sample_rate=None):
        self.offered.append(chunk)
        self._slot = chunk
        return True

    def take(self):
        c, self._slot = self._slot, None
        return c


def test_audio_thread_hop_does_no_arithmetic():
    """AudioBus documents the mic callback as 'must be fast and non-blocking'.
    The callback hands off a reference and returns — RMS happens in drain()."""
    tap = _FakeTap()
    br = MicTelemetryBridge(server=None, tap=tap)
    frame = np.sin(np.arange(512) / 5.0).astype(np.float32)

    br.on_mic_frame(frame)

    assert br.frames_seen == 1
    assert br.published == 0, "RMS ran on the audio thread"
    assert tap.offered and tap.offered[0] is frame


def test_drain_computes_rms_and_publishes():
    class _Srv:
        def __init__(self):
            self.calls = []

        def publish_rms(self, level, plane):
            self.calls.append((level, plane))

    tap, srv = _FakeTap(), _Srv()
    br = MicTelemetryBridge(server=srv, tap=tap, clock=lambda: 0.0, fps=20.0)
    br.on_mic_frame(np.full(256, 0.5, dtype=np.float32))

    level = br.drain(plane="user")
    assert level == pytest.approx(0.5, abs=1e-3)
    assert srv.calls and srv.calls[0][1] == "user"


def test_drain_rate_caps():
    t = {"now": 0.0}
    tap = _FakeTap()
    br = MicTelemetryBridge(server=None, tap=tap, clock=lambda: t["now"], fps=20.0)

    br.on_mic_frame(np.full(64, 0.5, dtype=np.float32))
    assert br.drain() is not None          # first passes
    br.on_mic_frame(np.full(64, 0.5, dtype=np.float32))
    t["now"] = 0.001
    assert br.drain() is None              # inside the cap -> coalesced
    assert br.coalesced == 1


def test_drain_with_no_pending_frame_is_a_noop():
    br = MicTelemetryBridge(server=None, tap=_FakeTap())
    assert br.drain() is None


def test_attach_and_detach_use_the_public_audiobus_api():
    """DRY: AudioBus already exposes register/unregister_mic_consumer — the tap
    rides that, rather than modifying the read loop."""
    class _Bus:
        def __init__(self):
            self.consumers = []

        def register_mic_consumer(self, cb):
            self.consumers.append(cb)

        def unregister_mic_consumer(self, cb):
            self.consumers.remove(cb)

    bus = _Bus()
    br = MicTelemetryBridge(server=None, tap=_FakeTap())
    assert br.attach(bus) is True
    assert bus.consumers == [br.on_mic_frame]
    br.detach()
    assert bus.consumers == []


def test_a_failing_bus_never_raises():
    class _BadBus:
        def register_mic_consumer(self, cb):
            raise RuntimeError("bus is gone")

    assert MicTelemetryBridge(server=None, tap=_FakeTap()).attach(_BadBus()) is False


def test_capture_callback_survives_a_broken_tap():
    """Nothing in the telemetry path may perturb capture."""
    class _BadTap:
        def offer(self, chunk, sample_rate=None):
            raise RuntimeError("tap exploded")

    br = MicTelemetryBridge(server=None, tap=_BadTap())
    br.on_mic_frame(np.zeros(64, dtype=np.float32))   # must not raise


# ---------------------------------------------------------------------------
# adaptive watermark — derived from the socket, never a constant
# ---------------------------------------------------------------------------


def test_watermark_derives_from_the_real_socket_buffer():
    """Root-cause: ask the kernel how much buffer this socket has rather than
    guessing. AF_UNIX SO_SNDBUF varies by platform and sysctl, so any constant
    is wrong somewhere."""
    import socket as _s

    a, b = _s.socketpair(_s.AF_UNIX, _s.SOCK_STREAM)
    try:
        sndbuf = a.getsockopt(_s.SOL_SOCKET, _s.SO_SNDBUF)

        class _W:
            def get_extra_info(self, key):
                return a if key == "socket" else None

        w = _W()
        assert ipc.socket_send_buffer_bytes(w) == sndbuf
        wm = ipc.rms_drop_watermark_bytes(w)
        assert wm == pytest.approx(sndbuf * ipc.rms_watermark_multiple(), rel=0.01)
    finally:
        a.close(); b.close()


def test_watermark_scales_with_a_bigger_socket():
    """A host with larger buffers must tolerate proportionally more queueing —
    the behaviour a fixed 64KB could never have."""
    class _W:
        def __init__(self, n): self._n = n
        def get_extra_info(self, key):
            class _S:
                def getsockopt(_s2, *_a): return self._n
            return _S()

    small = ipc.rms_drop_watermark_bytes(_W(8192))
    large = ipc.rms_drop_watermark_bytes(_W(262144))
    assert large > small * 4, "watermark did not scale with socket capacity"


def test_watermark_falls_back_only_when_introspection_fails():
    class _NoSock:
        def get_extra_info(self, key):
            return None

    assert ipc.socket_send_buffer_bytes(_NoSock()) is None
    assert ipc.rms_drop_watermark_bytes(_NoSock()) >= 1024
    assert ipc.rms_drop_watermark_bytes(None) >= 1024


def test_introspection_never_raises_on_a_hostile_transport():
    class _Hostile:
        def get_extra_info(self, key):
            raise OSError("no transport")

    assert ipc.socket_send_buffer_bytes(_Hostile()) is None


# ---------------------------------------------------------------------------
# QoS signalling — edge-triggered, on the GUARANTEED lane
# ---------------------------------------------------------------------------


def test_shedding_signals_degraded_on_the_edge_not_per_frame():
    """Per-frame signalling would put 20 events/sec on the guaranteed lane —
    more traffic than the telemetry being shed, on the lane that must never
    drop. Hysteresis makes it one edge."""
    w = _FakeWriter(queued=10_000_000)
    srv = _server(w)
    srv._enqueue = lambda msg: None            # no loop in test; count edges

    for _ in range(100):
        srv.publish_rms(0.5)

    assert srv.rms_stats["dropped"] == 100
    assert srv.rms_stats["degraded_edges"] == 1, "signalled per-frame, not per-edge"
    assert srv.telemetry_degraded is True


def test_recovery_signals_once_when_the_client_catches_up():
    w = _FakeWriter(queued=10_000_000)
    srv = _server(w)
    srv._enqueue = lambda msg: None

    for _ in range(20):
        srv.publish_rms(0.5)
    assert srv.telemetry_degraded is True

    w.transport._queued = 0
    for _ in range(20):
        w.transport._queued = 0                # stay drained
        srv.publish_rms(0.5)

    assert srv.telemetry_degraded is False
    assert srv.rms_stats["recovered_edges"] == 1


def test_hysteresis_prevents_flapping_at_the_watermark():
    """A client hovering at the threshold must not strobe the indicator."""
    w = _FakeWriter(queued=0)
    srv = _server(w)
    srv._enqueue = lambda msg: None
    hyst = ipc._qos_hysteresis()

    for i in range(hyst * 4):                  # alternate shed/send every frame
        w.transport._queued = 10_000_000 if i % 2 else 0
        srv.publish_rms(0.5)

    assert srv.rms_stats["degraded_edges"] == 0, "flapped despite hysteresis"


def test_qos_events_ride_the_guaranteed_lane():
    """Transport health is a STATE transition — the cockpit must never miss the
    edge that says the meter went unreliable."""
    assert ipc.EVENT_TELEMETRY_DEGRADED in ipc.EVENT_KINDS
    assert ipc.EVENT_TELEMETRY_RECOVERED in ipc.EVENT_KINDS
    assert ipc.MSG_RMS_LEVEL not in ipc.EVENT_KINDS


# ---------------------------------------------------------------------------
# IoC binding
# ---------------------------------------------------------------------------


def test_ioc_attaches_via_the_audiobus_singleton(monkeypatch):
    """No injection into the supervisor, no edit to AudioBus — the bridge PULLS
    its dependency from the published singleton accessor."""
    from backend.audio import mic_telemetry_bridge as mtb

    class _Bus:
        def __init__(self): self.consumers = []
        def register_mic_consumer(self, cb): self.consumers.append(cb)
        def unregister_mic_consumer(self, cb): self.consumers.remove(cb)

    bus = _Bus()
    monkeypatch.setattr(
        "backend.audio.audio_bus.get_audio_bus_safe", lambda: bus, raising=False,
    )
    mtb.reset_bridge()
    try:
        br = mtb.ensure_attached(server=None)
        assert br is not None
        assert len(bus.consumers) == 1
        # Idempotent: repeat calls must not double-register.
        for _ in range(5):
            mtb.ensure_attached(server=None)
        assert len(bus.consumers) == 1
    finally:
        mtb.reset_bridge()


def test_ioc_is_inert_until_a_bus_exists(monkeypatch):
    from backend.audio import mic_telemetry_bridge as mtb

    monkeypatch.setattr(
        "backend.audio.audio_bus.get_audio_bus_safe", lambda: None, raising=False,
    )
    mtb.reset_bridge()
    assert mtb.ensure_attached() is None
    assert mtb.pump_once() is None


def test_ioc_reattaches_after_a_bus_restart(monkeypatch):
    """Self-healing: a torn-down and rebuilt bus must regain the consumer."""
    from backend.audio import mic_telemetry_bridge as mtb

    class _Bus:
        def __init__(self): self.consumers = []
        def register_mic_consumer(self, cb): self.consumers.append(cb)
        def unregister_mic_consumer(self, cb): self.consumers.remove(cb)

    first, second = _Bus(), _Bus()
    current = {"bus": first}
    monkeypatch.setattr(
        "backend.audio.audio_bus.get_audio_bus_safe",
        lambda: current["bus"], raising=False,
    )
    mtb.reset_bridge()
    try:
        mtb.ensure_attached()
        assert len(first.consumers) == 1
        current["bus"] = second               # bus restarted
        mtb.ensure_attached()
        assert len(second.consumers) == 1, "did not re-attach to the new bus"
        assert first.consumers == [], "leaked the consumer on the dead bus"
    finally:
        mtb.reset_bridge()


def test_ioc_never_raises_when_audiobus_import_fails(monkeypatch):
    from backend.audio import mic_telemetry_bridge as mtb

    def _boom():
        raise RuntimeError("audio stack unavailable")

    monkeypatch.setattr(
        "backend.audio.audio_bus.get_audio_bus_safe", _boom, raising=False,
    )
    mtb.reset_bridge()
    assert mtb.ensure_attached() is None      # must not raise
    assert mtb.pump_once() is None
