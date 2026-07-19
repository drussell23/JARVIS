"""Tri-State IPC Audio Broker — lease negotiation + orphaned-mic spine.

Operator authorization 2026-07-18. The supervisor's ``audio_state_ipc``
transport gains an upstream lease lane; the daemon's synapse brokers
``wake`` cross-process instead of mounting hardware. The load-bearing
edge case (mandate 4, verbatim): a daemon crash mid-conversation must
disarm the supervisor's hardware locks WITHOUT hanging the supervisor —
two independent paths: broken-pipe drop-release + heartbeat expiry.

UDS tests need short socket paths (macOS sun_path) + sandbox off.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test.audio_synapse import (
    AudioVisualSynapse,
    RemoteAudioLease,
)
from backend.core.ouroboros.governance.comms.duplex.audio_state_ipc import (
    AudioStateBroadcaster,
    AudioStateClient,
)


@pytest.fixture()
def sock_dir():
    d = Path(tempfile.mkdtemp(prefix="lease-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def fast_ttl(monkeypatch):
    monkeypatch.setenv("JARVIS_AUDIO_LEASE_TTL_S", "1.0")


class _ArmSpy:
    """The supervisor's injected arm/disarm seam."""

    def __init__(self) -> None:
        self.calls: list = []
        self.armed = False

    async def __call__(self, armed: bool) -> None:
        self.armed = armed
        self.calls.append(armed)


async def _server(sock_dir, spy):
    b = AudioStateBroadcaster(
        path=sock_dir / "a.sock", on_lease_change=spy,
    )
    assert await b.start()
    return b


async def _wait_for(cond, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.05)
    return True


# ---------------------------------------------------------------------------
# (1) Lease negotiation over the EXISTING transport
# ---------------------------------------------------------------------------


class TestLeaseNegotiation:
    async def test_acquire_arms_and_grants_with_ttl(self, sock_dir, fast_ttl):
        spy = _ArmSpy()
        b = await _server(sock_dir, spy)
        frames: list = []
        c = AudioStateClient(path=sock_dir / "a.sock", on_message=frames.append)
        try:
            assert await c.connect()
            assert c.send_lease("acquire")
            assert await _wait_for(lambda: any(
                f.get("type") == "lease" for f in frames
            ))
            grant = [f for f in frames if f.get("type") == "lease"][0]
            assert grant["granted"] is True
            assert grant["ttl_s"] == pytest.approx(1.0)
            assert spy.calls == [True]
            assert b.lease_held is True
        finally:
            await c.close()
            await b.stop()

    async def test_second_client_denied_while_held(self, sock_dir, fast_ttl):
        spy = _ArmSpy()
        b = await _server(sock_dir, spy)
        f1: list = []
        f2: list = []
        c1 = AudioStateClient(path=sock_dir / "a.sock", on_message=f1.append)
        c2 = AudioStateClient(path=sock_dir / "a.sock", on_message=f2.append)
        try:
            assert await c1.connect() and await c2.connect()
            c1.send_lease("acquire")
            await _wait_for(lambda: any(f.get("type") == "lease" for f in f1))
            c2.send_lease("acquire")
            assert await _wait_for(lambda: any(
                f.get("type") == "lease" for f in f2
            ))
            deny = [f for f in f2 if f.get("type") == "lease"][0]
            assert deny["granted"] is False and deny["reason"] == "held"
            assert spy.calls == [True]          # single arm, no re-arm
        finally:
            await c1.close()
            await c2.close()
            await b.stop()

    async def test_explicit_release_disarms(self, sock_dir, fast_ttl):
        spy = _ArmSpy()
        b = await _server(sock_dir, spy)
        c = AudioStateClient(path=sock_dir / "a.sock")
        try:
            assert await c.connect()
            c.send_lease("acquire")
            await _wait_for(lambda: spy.armed)
            c.send_lease("release")
            assert await _wait_for(lambda: not spy.armed)
            assert spy.calls == [True, False]
            assert b.lease_held is False
        finally:
            await c.close()
            await b.stop()

    async def test_handshake_advertises_lease_state(self, sock_dir, fast_ttl):
        spy = _ArmSpy()
        b = await _server(sock_dir, spy)
        hs: list = []
        c1 = AudioStateClient(path=sock_dir / "a.sock")
        try:
            assert await c1.connect()
            c1.send_lease("acquire")
            await _wait_for(lambda: spy.armed)
            c2 = AudioStateClient(
                path=sock_dir / "a.sock", on_handshake=hs.append,
            )
            assert await c2.connect()
            assert hs and hs[0]["lease"]["held"] is True
            assert hs[0]["schema_version"] == "audio_ipc.v2"
            await c2.close()
        finally:
            await c1.close()
            await b.stop()


# ---------------------------------------------------------------------------
# (2) Orphaned-mic protection — MANDATE 4 VERBATIM
# ---------------------------------------------------------------------------


class TestOrphanedMicProtection:
    async def test_daemon_crash_mid_conversation_disarms_without_hang(
        self, sock_dir, fast_ttl,
    ):
        """Daemon crash mid-conversation: the client socket dies with
        NO release frame (SIGKILL shape). The supervisor must detect
        the broken pipe, disarm the hardware seam, and keep serving —
        the whole recovery bounded well under one TTL."""
        spy = _ArmSpy()
        b = await _server(sock_dir, spy)
        c = AudioStateClient(path=sock_dir / "a.sock")
        try:
            assert await c.connect()
            c.send_lease("acquire")
            assert await _wait_for(lambda: spy.armed)
            # Mid-conversation: supervisor is speaking.
            b.publish_event("TTS_GENERATING")
            # CRASH — abrupt transport death, no release, no goodbye.
            c._writer.transport.abort()
            assert await _wait_for(lambda: not spy.armed, timeout=3.0)
            assert spy.calls == [True, False]
            assert b.lease_held is False
            assert b.lease_stats["drop_releases"] == 1
            # The supervisor did NOT hang: it still serves new clients.
            c2 = AudioStateClient(path=sock_dir / "a.sock")
            assert await c2.connect()
            await c2.close()
        finally:
            await c.close()
            await b.stop()

    async def test_missed_heartbeats_expire_the_lease(
        self, sock_dir, fast_ttl,
    ):
        """Path (b): socket wedges OPEN but heartbeats stop (daemon
        event loop frozen). The monotonic watchdog expires the lease
        within ~1.5×TTL and disarms."""
        spy = _ArmSpy()
        b = await _server(sock_dir, spy)
        c = AudioStateClient(path=sock_dir / "a.sock")
        try:
            assert await c.connect()
            c.send_lease("acquire")
            assert await _wait_for(lambda: spy.armed)
            # Send NO heartbeats; socket stays open.
            assert await _wait_for(lambda: not spy.armed, timeout=4.0)
            assert b.lease_stats["expiries"] == 1
            assert b.lease_held is False
        finally:
            await c.close()
            await b.stop()

    async def test_heartbeats_keep_the_lease_alive(self, sock_dir, fast_ttl):
        spy = _ArmSpy()
        b = await _server(sock_dir, spy)
        c = AudioStateClient(path=sock_dir / "a.sock")
        try:
            assert await c.connect()
            c.send_lease("acquire")
            assert await _wait_for(lambda: spy.armed)
            for _ in range(6):                  # 1.8s > TTL, kept alive
                c.send_lease("heartbeat")
                await asyncio.sleep(0.3)
            assert spy.armed is True
            assert b.lease_stats["expiries"] == 0
            assert b.lease_stats["heartbeats"] >= 4
        finally:
            await c.close()
            await b.stop()

    async def test_wedged_disarm_callback_never_hangs_supervisor(
        self, sock_dir, fast_ttl,
    ):
        """A pathological arm/disarm callback that blocks forever is
        fused by the outer wait_for — the broadcaster loop survives."""
        async def _wedged(_armed: bool) -> None:
            await asyncio.sleep(3600)

        b = AudioStateBroadcaster(
            path=sock_dir / "a.sock", on_lease_change=_wedged,
        )
        assert await b.start()
        c = AudioStateClient(path=sock_dir / "a.sock")
        try:
            assert await c.connect()
            c.send_lease("acquire")
            # Grant still arrives (callback fuse = TTL) and the server
            # keeps accepting connections afterwards.
            await asyncio.sleep(1.5)
            c2 = AudioStateClient(path=sock_dir / "a.sock")
            assert await c2.connect()
            await c2.close()
        finally:
            await c.close()
            await b.stop()


# ---------------------------------------------------------------------------
# (3) Daemon-side broker — RemoteAudioLease + synapse composition
# ---------------------------------------------------------------------------


class TestRemoteAudioLease:
    async def test_acquire_streams_fsm_states_to_tui(
        self, sock_dir, fast_ttl, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_AUDIO_IPC_SOCKET", str(sock_dir / "a.sock"),
        )
        spy = _ArmSpy()
        b = await _server(sock_dir, spy)
        states: list = []
        lease = RemoteAudioLease(states.append)
        try:
            assert await lease.acquire()
            assert states[0] == "LISTENING"     # armed baseline
            b.publish_event("VAD_ACTIVE")
            await _wait_for(lambda: "HEARING" in states)
            b.publish_event("TTS_GENERATING")
            await _wait_for(lambda: "THINKING" in states)
            b.publish_event("AUDIO_PLAYING")
            await _wait_for(lambda: "SPEAKING" in states)
            b.publish_event("AUDIO_IDLE")
            assert await _wait_for(lambda: states[-1] == "LISTENING")
        finally:
            await lease.release()
            await b.stop()

    async def test_supervisor_absent_is_honest_failure(
        self, sock_dir, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_AUDIO_IPC_SOCKET", str(sock_dir / "missing.sock"),
        )
        states: list = []
        lease = RemoteAudioLease(states.append)
        assert await lease.acquire() is False
        assert states == []                     # caller owns UNAVAILABLE

    async def test_supervisor_death_mid_lease_reports_offline(
        self, sock_dir, fast_ttl, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_AUDIO_IPC_SOCKET", str(sock_dir / "a.sock"),
        )
        spy = _ArmSpy()
        b = await _server(sock_dir, spy)
        states: list = []
        lease = RemoteAudioLease(states.append)
        try:
            assert await lease.acquire()
            await b.stop()                      # supervisor dies
            assert await _wait_for(
                lambda: states and states[-1] == "OFFLINE", timeout=3.0,
            )
        finally:
            await lease.release()

    async def test_synapse_wake_brokers_cross_process(
        self, sock_dir, fast_ttl, monkeypatch,
    ):
        """The full daemon seam: no local handle + live supervisor →
        wake acquires the remote lease (LISTENING); sleep releases it
        (OFFLINE) and the supervisor disarms."""
        monkeypatch.setenv(
            "JARVIS_AUDIO_IPC_SOCKET", str(sock_dir / "a.sock"),
        )
        spy = _ArmSpy()
        b = await _server(sock_dir, spy)
        states: list = []
        syn = AudioVisualSynapse(states.append, handle_resolver=lambda: None)
        try:
            await syn.handle_cmd("wake")
            assert syn.armed is True
            assert states == ["LISTENING"]
            assert await _wait_for(lambda: spy.armed)
            await syn.handle_cmd("sleep")
            assert states[-1] == "OFFLINE"
            assert await _wait_for(lambda: not spy.armed, timeout=3.0)
        finally:
            await syn.stop()
            await b.stop()

    async def test_synapse_wake_broker_disabled_stays_unavailable(
        self, sock_dir, fast_ttl, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_AUDIO_IPC_SOCKET", str(sock_dir / "a.sock"),
        )
        monkeypatch.setenv("JARVIS_AUDIO_BROKER_ENABLED", "false")
        spy = _ArmSpy()
        b = await _server(sock_dir, spy)
        states: list = []
        syn = AudioVisualSynapse(states.append, handle_resolver=lambda: None)
        try:
            await syn.handle_cmd("wake")
            assert states == ["UNAVAILABLE"]
            assert spy.calls == []              # supervisor untouched
        finally:
            await syn.stop()
            await b.stop()


# ---------------------------------------------------------------------------
# (4) Lease Preemption + Push-to-Talk (operator-authorized 2026-07-18)
# ---------------------------------------------------------------------------


class TestLeasePreemption:
    async def test_force_wake_revokes_incumbent_gracefully(
        self, sock_dir, fast_ttl,
    ):
        """Second terminal asserts acquire_preempt: incumbent gets
        reason=preempted over the return channel, challenger is
        granted, and the hardware stays ARMED through the transfer
        (single continuous stream — no disarm/re-arm glitch)."""
        spy = _ArmSpy()
        b = await _server(sock_dir, spy)
        f1: list = []
        f2: list = []
        c1 = AudioStateClient(path=sock_dir / "a.sock", on_message=f1.append)
        c2 = AudioStateClient(path=sock_dir / "a.sock", on_message=f2.append)
        try:
            assert await c1.connect() and await c2.connect()
            c1.send_lease("acquire")
            await _wait_for(lambda: spy.armed)
            c2.send_lease("acquire_preempt")
            assert await _wait_for(lambda: any(
                f.get("reason") == "preempted" for f in f1
            ))
            assert await _wait_for(lambda: any(
                f.get("granted") is True for f in f2
            ))
            assert spy.calls == [True]           # armed ONCE, continuous
            assert b.lease_stats["preempts"] == 1
        finally:
            await c1.close()
            await c2.close()
            await b.stop()

    async def test_ptt_is_ephemeral_full_cycle(self, sock_dir, fast_ttl):
        """ptt_start preempts + arms; ptt_end fully releases (disarm).
        A standing lease never disarms on a stray ptt_end."""
        spy = _ArmSpy()
        b = await _server(sock_dir, spy)
        frames: list = []
        c = AudioStateClient(path=sock_dir / "a.sock", on_message=frames.append)
        try:
            assert await c.connect()
            c.send_lease("ptt_start")
            assert await _wait_for(lambda: spy.armed)
            grant = [f for f in frames if f.get("granted")][0]
            assert grant["ptt"] is True
            c.send_lease("ptt_end")
            assert await _wait_for(lambda: not spy.armed)
            assert spy.calls == [True, False]
            assert b.lease_stats["ptt_sessions"] == 1
        finally:
            await c.close()
            await b.stop()

    async def test_stray_ptt_end_never_disarms_standing_lease(
        self, sock_dir, fast_ttl,
    ):
        spy = _ArmSpy()
        b = await _server(sock_dir, spy)
        c = AudioStateClient(path=sock_dir / "a.sock")
        try:
            assert await c.connect()
            c.send_lease("acquire")              # standing, not PTT
            assert await _wait_for(lambda: spy.armed)
            c.send_lease("ptt_end")
            await asyncio.sleep(0.4)
            assert spy.armed is True             # defensive bracket
        finally:
            await c.close()
            await b.stop()

    async def test_remote_lease_maps_preempted_to_held(
        self, sock_dir, fast_ttl, monkeypatch,
    ):
        """The revoked terminal's presentation truth: HELD (the ov
        toolbar renders 'held by another terminal') and NO auto-
        re-acquire mic war."""
        monkeypatch.setenv(
            "JARVIS_AUDIO_IPC_SOCKET", str(sock_dir / "a.sock"),
        )
        spy = _ArmSpy()
        b = await _server(sock_dir, spy)
        s1: list = []
        s2: list = []
        lease1 = RemoteAudioLease(s1.append)
        lease2 = RemoteAudioLease(s2.append)
        try:
            assert await lease1.acquire()
            assert await lease2.acquire(preempt=True)
            assert await _wait_for(lambda: "HELD" in s1)
            assert lease1.active is False        # no mic war
            assert lease2.active is True
        finally:
            await lease1.release()
            await lease2.release()
            await b.stop()


# ---------------------------------------------------------------------------
# (5) TTS interruption — FLUSH / ducking
# ---------------------------------------------------------------------------


class TestFlush:
    async def test_holder_flush_invokes_seam_and_broadcasts_idle(
        self, sock_dir, fast_ttl,
    ):
        spy = _ArmSpy()
        flushes: list = []
        b = AudioStateBroadcaster(
            path=sock_dir / "a.sock", on_lease_change=spy,
            on_flush=lambda: flushes.append(1),
        )
        assert await b.start()
        frames: list = []
        c = AudioStateClient(path=sock_dir / "a.sock", on_message=frames.append)
        try:
            assert await c.connect()
            c.send_lease("acquire")
            await _wait_for(lambda: spy.armed)
            c.send_lease("flush")
            assert await _wait_for(lambda: flushes)
            assert await _wait_for(lambda: any(
                f.get("kind") == "AUDIO_IDLE" for f in frames
            ))
            assert b.lease_stats["flushes"] == 1
        finally:
            await c.close()
            await b.stop()

    async def test_bystander_flush_ignored(self, sock_dir, fast_ttl):
        spy = _ArmSpy()
        flushes: list = []
        b = AudioStateBroadcaster(
            path=sock_dir / "a.sock", on_lease_change=spy,
            on_flush=lambda: flushes.append(1),
        )
        assert await b.start()
        c1 = AudioStateClient(path=sock_dir / "a.sock")
        c2 = AudioStateClient(path=sock_dir / "a.sock")
        try:
            assert await c1.connect() and await c2.connect()
            c1.send_lease("acquire")
            await _wait_for(lambda: spy.armed)
            c2.send_lease("flush")               # NOT the holder
            await asyncio.sleep(0.4)
            assert flushes == []
        finally:
            await c1.close()
            await c2.close()
            await b.stop()

    def test_arbiter_flush_halts_everything(self):
        from backend.core.ouroboros.governance.comms.duplex.arbiter import (
            VoiceDuplexArbiter,
        )
        from backend.core.ouroboros.governance.comms.duplex.protocols import (
            Priority,
            SpeechRequest,
            VoiceState,
        )

        class _Playback:
            def __init__(self) -> None:
                self.preempts = 0

            def preempt(self) -> None:
                self.preempts += 1

            async def play(self, _text: str) -> None:
                await asyncio.sleep(3600)

        pb = _Playback()
        arb = VoiceDuplexArbiter(pb)
        arb.submit(SpeechRequest("queued line", Priority.PROACTIVE_INFO))
        arb._state = VoiceState.KAREN_SPEAKING
        arb.flush()
        assert pb.preempts == 1
        assert arb.state == VoiceState.LISTENING
        assert all(len(q) == 0 for q in arb._queues.values())

    def test_attach_ui_ducking_predicate_and_held_toolbar(self):
        from backend.core.ouroboros.cli.ov import AttachUI
        ui = AttachUI()
        for state, ducks in (
            ("LISTENING", False), ("THINKING", True),
            ("SPEAKING", True), ("OFFLINE", False),
        ):
            ui.on_audio_state(state)
            assert ui.should_flush_on_input() is ducks
        ui.on_audio_state("HELD")
        assert "held by another terminal" in ui.toolbar()
        assert ui.prompt() == "ov › "


# ---------------------------------------------------------------------------
# (6) Hardware Topology Survival — MANDATE 4 VERBATIM
# ---------------------------------------------------------------------------


class TestHardwareTopologySurvival:
    def _arbiter_with_dying_stream(self):
        from backend.core.ouroboros.governance.comms.duplex.arbiter import (
            VoiceDuplexArbiter,
        )

        class _DyingPlayback:
            def preempt(self) -> None:
                pass

            async def play(self, _text: str) -> None:
                raise OSError("CoreAudio: device vanished (-10851)")

        from backend.core.ouroboros.governance.comms.duplex.protocols import (
            ArbiterConfig,
        )
        cfg = ArbiterConfig(
            enabled=True, barge_in_enabled=True, proactive_enabled=True,
        )
        return VoiceDuplexArbiter(_DyingPlayback(), config=cfg)

    async def test_stream_read_failure_reports_fault_and_loop_survives(self):
        """Mock an audio stream failure during playback: the arbiter
        reports through on_hardware_fault, resets its FSM, and its run
        loop keeps draining — no crash, no wedge."""
        from backend.core.ouroboros.governance.comms.duplex.protocols import (
            Priority,
            SpeechRequest,
            VoiceState,
        )
        arb = self._arbiter_with_dying_stream()
        faults: list = []
        arb.on_hardware_fault = faults.append
        run = asyncio.get_running_loop().create_task(arb.run())
        try:
            arb.submit(SpeechRequest("hello", Priority.PROACTIVE_INFO))
            assert await _wait_for(lambda: faults, timeout=3.0)
            assert "vanished" in str(faults[0])
            assert arb.state == VoiceState.LISTENING   # FSM reset
            assert arb.hardware_fault_count == 1
            # Loop alive: a second submit is processed (faults again).
            arb.submit(SpeechRequest("again", Priority.PROACTIVE_INFO))
            assert await _wait_for(lambda: len(faults) == 2, timeout=3.0)
        finally:
            await arb.stop()
            run.cancel()
            try:
                await run
            except (asyncio.CancelledError, Exception):
                pass

    async def test_device_vanish_mid_lease_fails_safe_end_to_end(
        self, sock_dir, fast_ttl, monkeypatch,
    ):
        """The full survival chain: active remote lease + supervisor
        stream fault → holder revoked (reason=hw_fault), hardware seam
        disarmed, HW_FAULT broadcast, TUI renders UNAVAILABLE, and the
        supervisor's loop keeps serving new clients."""
        monkeypatch.setenv(
            "JARVIS_AUDIO_IPC_SOCKET", str(sock_dir / "a.sock"),
        )
        spy = _ArmSpy()
        b = await _server(sock_dir, spy)
        states: list = []
        lease = RemoteAudioLease(states.append)
        try:
            assert await lease.acquire()
            assert await _wait_for(lambda: spy.armed)
            # The arbiter's fault reporter fires (Bluetooth dropped):
            b.publish_hardware_fault("CoreAudio: device vanished (-10851)")
            assert await _wait_for(
                lambda: states and states[-1] == "UNAVAILABLE", timeout=3.0,
            )
            assert await _wait_for(lambda: not spy.armed, timeout=3.0)
            assert lease.active is False
            assert b.lease_stats["hw_faults"] == 1
            # Supervisor loop alive — serves a fresh client.
            c2 = AudioStateClient(path=sock_dir / "a.sock")
            assert await c2.connect()
            await c2.close()
        finally:
            await lease.release()
            await b.stop()

    def test_bootstrap_wires_fault_reporter_pin(self):
        src = (
            _REPO / "backend/audio/audio_pipeline_bootstrap.py"
        ).read_text()
        assert src.count("on_hardware_fault") >= 2   # lease + pre-mount paths
        assert "publish_hardware_fault" in src
        assert "on_flush=_on_flush" in src


_REPO = Path(__file__).resolve().parents[2]
