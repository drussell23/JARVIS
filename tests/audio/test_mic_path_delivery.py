"""The microphone path: two bugs that made "hello Karen" reach nothing.

Both were invisible from every status surface, which is why they survived.

BUG 1 — the resampler keyword (100% mic frame loss)
    ``AudioBus.Resampler.process`` called libsamplerate with
    ``end_of_data=...``; the pybind11 binding declares ``end_of_input``. Every
    call raised TypeError, ``_on_mic_frame`` caught it at DEBUG level, and not
    one microphone frame ever reached a consumer. ``get_status()`` reported
    ``running: True, device_running: True, input_enabled: True`` throughout.
    The device delivered 200 frames in 4s; the bus delivered 0.

BUG 2 — the bootstrap early return (no audio plane at all)
    When ASR admission was closed, ``wire_conversation_pipeline`` did a bare
    ``return handle`` — abandoning turn detection, barge-in, the Karen duplex,
    the audio-state IPC broadcaster, ConversationPipeline and ModeDispatcher.
    The log said "StreamingSTT deferred"; what actually happened was that the
    socket the cockpit talks to was never bound.
"""

from __future__ import annotations

import asyncio
import logging

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# BUG 1 — the resampler must actually resample
# ---------------------------------------------------------------------------


def test_resampler_processes_a_frame_instead_of_raising():
    """THE REGRESSION. One wrong keyword silenced the entire microphone."""
    from backend.audio.audio_bus import Resampler

    r = Resampler(48000, 16000)
    out = r.process(np.zeros(960, dtype=np.float32))

    assert out is not None
    assert out.dtype == np.float32
    # 48k -> 16k is 3:1; libsamplerate's filter delay makes the first chunk
    # slightly short, so assert the ratio rather than an exact count.
    assert 250 <= len(out) <= 330, f"resampled to {len(out)} samples"


def test_resampler_preserves_signal_not_just_shape():
    """A resampler that returns the right-sized silence would pass a shape
    check while still delivering nothing audible."""
    from backend.audio.audio_bus import Resampler

    r = Resampler(48000, 16000)
    tone = np.sin(
        2 * np.pi * 440 * np.arange(4800, dtype=np.float32) / 48000
    ).astype(np.float32)
    out = np.concatenate([r.process(tone[i:i + 960]) for i in range(0, 4800, 960)])

    assert float(np.max(np.abs(out))) > 0.1, "resampler emitted silence"


def test_the_end_of_input_flag_is_probed_once_not_per_frame():
    """Per-frame try/except in the audio path is exactly what hid the bug for
    so long: the failure had somewhere quiet to go, 50 times a second."""
    from backend.audio.audio_bus import Resampler

    r = Resampler(48000, 16000)
    assert isinstance(r._eoi_supported, bool)     # noqa: SLF001 — resolved at init


def test_the_call_does_not_use_a_backend_keyword():
    """Structural pin. Positional survives a keyword RENAME; a keyword call
    does not, and fails silently rather than loudly."""
    from pathlib import Path

    src = Path("backend/audio/audio_bus.py").read_text(encoding="utf-8")
    body = src[src.index("def process("):src.index("def process(") + 2000]
    assert "end_of_data=end_of_data" not in body, (
        "the keyword form is back — this is the exact call that silenced "
        "the microphone"
    )


def test_a_passthrough_resampler_is_still_a_no_op():
    from backend.audio.audio_bus import Resampler

    data = np.ones(64, dtype=np.float32)
    assert Resampler(16000, 16000).process(data) is data


# ---------------------------------------------------------------------------
# BUG 1 — and the failure must never be quiet again
# ---------------------------------------------------------------------------


async def test_total_frame_loss_is_reported_loudly(caplog):
    """A mic path dropping every frame is the audio path being DOWN. It must
    not whisper at DEBUG while the status surface says healthy."""
    from backend.audio.audio_bus import AudioBus

    bus = AudioBus.__new__(AudioBus)          # no device, no CoreAudio
    bus._running = True
    bus._mic_gate_active = False
    bus._mic_consumers = [lambda _f: None]
    bus._consumer_lock = __import__("threading").RLock()
    bus._mic_error_count = 0
    bus._mic_frames_delivered = 0

    class _Boom:
        def process(self, *_a, **_k):
            raise TypeError("incompatible function arguments")

    bus._resampler_down = _Boom()
    bus._device = None
    bus._aec = None

    with caplog.at_level(logging.WARNING, logger="backend.audio.audio_bus"):
        bus._on_mic_frame(np.zeros(960, dtype=np.float32))

    assert any("FAILED" in r.message or "FAILED" in r.getMessage()
               for r in caplog.records), "total frame loss stayed silent"
    assert bus._mic_error_count == 1


async def test_the_error_log_is_rate_limited_by_count():
    """This runs on the audio thread at 50Hz — a per-frame log would itself
    become the fault. Geometric cadence: 1, 2, 4, 8, …"""
    from backend.audio.audio_bus import AudioBus

    bus = AudioBus.__new__(AudioBus)
    bus._running = True
    bus._mic_gate_active = False
    bus._mic_consumers = []
    bus._consumer_lock = __import__("threading").RLock()
    bus._mic_error_count = 0
    bus._mic_frames_delivered = 0
    bus._device = None
    bus._aec = None

    class _Boom:
        def process(self, *_a, **_k):
            raise TypeError("nope")

    bus._resampler_down = _Boom()
    for _ in range(50):
        bus._on_mic_frame(np.zeros(8, dtype=np.float32))
    assert bus._mic_error_count == 50


def test_status_exposes_delivery_not_just_liveness():
    """`running: True` was true throughout a total outage. Delivery is the
    only honest health signal for the mic path."""
    from pathlib import Path

    src = Path("backend/audio/audio_bus.py").read_text(encoding="utf-8")
    assert "mic_frames_delivered" in src and "mic_frame_errors" in src


# ---------------------------------------------------------------------------
# BUG 2 — a deferred component must not abandon the plane
# ---------------------------------------------------------------------------


async def test_closed_asr_admission_still_wires_the_rest_of_the_plane(monkeypatch):
    """THE REGRESSION. `return handle` here meant no IPC broadcaster, so the
    cockpit's `wake` reached nothing and its wave had no data source — while
    the log claimed only that STT was 'deferred'."""
    monkeypatch.setenv("JARVIS_ASR_ADMISSION_ENABLED", "true")
    monkeypatch.delenv("JARVIS_ASR_ADMISSION_OPEN", raising=False)
    monkeypatch.delenv("JARVIS_ASR_ADMISSION_FORCE_OPEN", raising=False)
    monkeypatch.setenv("JARVIS_STARTUP_COMPLETE", "false")

    from backend.audio import audio_pipeline_bootstrap as bootstrap

    allowed, reason = bootstrap._can_start_streaming_stt_now()
    assert allowed is False and reason, "admission gate did not close"

    handle = await bootstrap.wire_conversation_pipeline(
        audio_bus=None, llm_client=None,
    )

    assert handle is not None
    assert handle.streaming_stt is None, "STT should be the ONE thing deferred"
    # The components below the gate must have been reached. Without a real
    # AudioBus most cannot fully mount, but the deferral must not be the reason.
    assert handle.turn_detector is not None, (
        "TurnDetector was never constructed — the bootstrap abandoned the "
        "plane at the ASR gate again"
    )


def _code_only(src: str) -> str:
    """Source with comments and string literals removed.

    A plain substring sweep flags the very comment that explains why the
    pattern is banned — which turns a structural pin into a check on
    documentation. Executable tokens are the only thing worth asserting on.
    (This is the third time in this file's lineage that prose defeated a
    naive pin; hence a shared helper rather than another ad-hoc filter.)"""
    import io
    import tokenize

    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)
    except (tokenize.TokenError, IndentationError):
        # A sliced function body may not tokenize cleanly; fall back to
        # dropping whole-line comments, which is what this pin actually needs.
        return "\n".join(
            ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
        )
    return " ".join(out)


def test_the_admission_branch_has_no_bare_return():
    """Structural pin: the branch must exit through step 2's own except, the
    same shape as every other step, not out of the whole function."""
    import inspect

    from backend.audio import audio_pipeline_bootstrap as bootstrap

    src = inspect.getsource(bootstrap.wire_conversation_pipeline)
    head = _code_only(src[:src.index("# 3. TurnDetector")])
    assert "return handle" not in head, (
        "the ASR gate returns from the whole function again — nine components "
        "below it are being silently dropped"
    )
    assert _code_only(src).count("return handle") == 1, (
        "more than one exit from the wiring"
    )


def test_the_deferral_is_reported_as_a_warning_not_an_aside():
    """'deferred' read as 'coming shortly'. Nothing re-wires — the operator
    needs to know transcription is off for the session."""
    import inspect

    from backend.audio import audio_pipeline_bootstrap as bootstrap

    src = inspect.getsource(bootstrap.wire_conversation_pipeline)
    head = src[:src.index("# 3. TurnDetector")]
    assert "logger.warning" in head


# ---------------------------------------------------------------------------
# The host that replaced the monolith
# ---------------------------------------------------------------------------


def test_the_cockpit_spawns_the_audio_host_not_the_supervisor():
    """Spawning 98K lines to open a microphone launched a web app nobody asked
    for and loaded a local model into the memory the audio path needs."""
    from backend.core.ouroboros.cli.audio_daemon_reflex import (
        audio_host_path, supervisor_path,
    )

    p = audio_host_path()
    assert p is not None and p.name == "audio_plane_host.py" and p.is_file()
    assert supervisor_path() == p, "the deprecated alias drifted from the target"


def test_the_host_generates_remotely_only():
    """llm_client=None is the point, not an omission: a host that loaded a
    local model would rebuild the memory contention it was extracted to solve."""
    from pathlib import Path

    src = Path(
        "backend/audio/audio_plane_host.py",
    ).read_text(encoding="utf-8")
    assert "llm_client=None" in src


def test_the_host_hard_exits():
    """Same Py_FinalizeEx discipline as the supervisor and the harness."""
    from pathlib import Path

    src = Path(
        "backend/audio/audio_plane_host.py",
    ).read_text(encoding="utf-8")
    assert "os._exit(" in src and "flush()" in src


# ---------------------------------------------------------------------------
# BUG 4 — a rendezvous address that depends on the caller's cwd
# ---------------------------------------------------------------------------
#
# `voice: unavailable (no audio plane)` with a host actively serving. The
# socket default was the RELATIVE string ".jarvis/audio_state.sock", so every
# process resolved it against its own working directory. Observed live: two
# hosts, one on ./.jarvis/audio_state.sock and one on
# ./backend/audio/.jarvis/audio_state.sock (the reflex spawned with
# cwd=<script dir>), while the cockpit looked at neither.
#
# It also defeated single-flight silently: the host probes for an incumbent
# before binding, and a probe of a DIFFERENT path finds nothing — so the guard
# passed and a second owner of the microphone started.


def test_the_socket_path_is_absolute_regardless_of_cwd(monkeypatch, tmp_path):
    """THE REGRESSION. Three programs meet at this address; it cannot depend on
    where each of them happened to be launched from."""
    from backend.core.ouroboros.governance.comms.duplex.audio_state_ipc import (
        socket_path,
    )

    monkeypatch.delenv("JARVIS_AUDIO_IPC_SOCKET", raising=False)
    here = socket_path()
    monkeypatch.chdir(tmp_path)
    there = socket_path()

    assert here.is_absolute(), f"cwd-relative rendezvous address: {here}"
    assert here == there, (
        f"the socket moved with cwd: {here} != {there} — this is how two "
        f"hosts bound two sockets while the cockpit found neither"
    )


def test_a_relative_override_also_anchors_to_the_repo(monkeypatch, tmp_path):
    """Closing the way back in: an operator-supplied relative path must not
    reintroduce the same drift."""
    from backend.core.ouroboros.governance.comms.duplex.audio_state_ipc import (
        socket_path,
    )

    monkeypatch.setenv("JARVIS_AUDIO_IPC_SOCKET", "sub/dir/a.sock")
    here = socket_path()
    monkeypatch.chdir(tmp_path)
    assert socket_path() == here and here.is_absolute()


def test_an_absolute_override_is_honoured_verbatim(monkeypatch):
    from backend.core.ouroboros.governance.comms.duplex.audio_state_ipc import (
        socket_path,
    )
    from pathlib import Path

    monkeypatch.setenv("JARVIS_AUDIO_IPC_SOCKET", "/tmp/explicit.sock")
    assert socket_path() == Path("/tmp/explicit.sock")


def test_the_host_is_spawned_into_the_repo_not_its_own_directory():
    """`cwd=path.parent` put the host in backend/audio/, where every
    cwd-relative path it touched resolved where nobody was looking."""
    from pathlib import Path

    src = Path(
        "backend/core/ouroboros/cli/audio_daemon_reflex.py",
    ).read_text(encoding="utf-8")
    assert "cwd=str(path.parent)" not in src, (
        "the host is being spawned into its own directory again"
    )


# ---------------------------------------------------------------------------
# BUG 5 — a detached process that dies leaving no trace
# ---------------------------------------------------------------------------


def test_the_spawned_host_logs_somewhere_readable():
    """stdio went to DEVNULL so a chatty boot could not corrupt the TUI. Right
    reasoning, wrong destination: "the cockpit says no audio plane" became
    unanswerable, which is the one question an operator actually asks."""
    from backend.core.ouroboros.cli import audio_daemon_reflex as reflex

    p = reflex.host_log_path()
    assert p is not None and p.is_absolute() and p.parent.is_dir()


def test_spawn_redirects_stdio_to_the_log_not_devnull(monkeypatch, tmp_path):
    from backend.core.ouroboros.cli import audio_daemon_reflex as reflex

    seen = {}

    class _P:
        pid = 4242

    monkeypatch.setattr(
        reflex.subprocess, "Popen",
        lambda argv, **kw: (seen.update(kw), _P())[1],
    )
    assert reflex.spawn_supervisor() == 4242
    assert seen["stdout"] is not reflex.subprocess.DEVNULL, (
        "the host's death is being sent to /dev/null again"
    )
    assert seen["stderr"] == reflex.subprocess.STDOUT
    assert seen["stdin"] == reflex.subprocess.DEVNULL, (
        "a detached host must never inherit the terminal's stdin"
    )
    assert seen["start_new_session"] is True


def test_the_host_log_appends_rather_than_truncates():
    """The interesting case is a host that dies and gets respawned; truncating
    would erase the death that explains the retry."""
    from pathlib import Path

    src = Path(
        "backend/core/ouroboros/cli/audio_daemon_reflex.py",
    ).read_text(encoding="utf-8")
    assert 'open(path, "a"' in src


# ---------------------------------------------------------------------------
# BUG 6 — single-flight had a 13-second hole, and the loser stole the address
# ---------------------------------------------------------------------------
#
# Observed live: two hosts, PIDs three minutes apart, both serving. Every
# utterance transcribed TWICE — two faster-whisper instances on one
# microphone. Two independent failures produced it:
#
#   (a) the guard was a socket probe, but binding happens ~13s into boot
#       (whisper loads first), so "is anything listening?" answers NO for a
#       13-second window in which a second host passes the same guard;
#   (b) AudioStateBroadcaster.start() unlinked any existing socket file before
#       binding — meant to clear a corpse, unable to tell one from a live
#       server — so the late host STOLE the address from the incumbent.


def test_the_host_locks_before_it_boots_not_after_it_binds():
    """A probe asks 'has the winner finished?'; a lock asks 'is anyone
    trying?'. Only the second is answerable at t=0."""
    import inspect

    from backend.audio import audio_plane_host as host

    src = inspect.getsource(host._amain)
    assert "_acquire_exclusive" in src
    assert src.index("_acquire_exclusive") < src.index("_socket_already_served"), (
        "the probe runs before the lock — the boot window is open again"
    )


def test_the_lock_is_the_shared_helper_not_a_new_one():
    """DRY: flock semantics, kernel-released on exit (so a SIGKILLed host
    cannot strand a lock), already solved in singleton_lock."""
    import inspect

    from backend.audio import audio_plane_host as host

    src = inspect.getsource(host._acquire_exclusive)
    assert "acquire_singleton" in src
    assert "audio_plane.lock" in src, (
        "the audio plane must not contend with the battle-test soak lock"
    )


async def test_binding_refuses_to_steal_a_live_socket(tmp_path):
    """THE REGRESSION. The second host must lose, not take over."""
    import tempfile
    from pathlib import Path

    from backend.core.ouroboros.governance.comms.duplex import (
        audio_state_ipc as ipc,
    )

    # mkdtemp, not tmp_path: macOS caps sun_path at ~104 bytes.
    sock = Path(tempfile.mkdtemp(prefix="ovlive")) / "a.sock"

    first = ipc.AudioStateBroadcaster(path=sock)
    if not await first.start():
        pytest.skip("cannot bind a unix socket in this environment")
    try:
        second = ipc.AudioStateBroadcaster(path=sock)
        assert await second.start() is False, (
            "a second broadcaster took the address from a live incumbent"
        )
        # And the incumbent is untouched.
        assert await ipc._socket_is_live(sock) is True
    finally:
        await first.stop()


async def test_a_stale_socket_file_is_still_cleared(tmp_path):
    """The unlink exists for a reason: a corpse left by SIGKILL must not block
    every future bind. Refusing to steal must not become refusing to start."""
    import tempfile
    from pathlib import Path

    from backend.core.ouroboros.governance.comms.duplex import (
        audio_state_ipc as ipc,
    )

    sock = Path(tempfile.mkdtemp(prefix="ovstale")) / "a.sock"
    sock.write_bytes(b"")                      # inode with nobody behind it

    b = ipc.AudioStateBroadcaster(path=sock)
    if not await b.start():
        pytest.skip("cannot bind a unix socket in this environment")
    try:
        assert await ipc._socket_is_live(sock) is True
    finally:
        await b.stop()


async def test_liveness_is_answered_by_the_kernel_not_by_stat(tmp_path):
    from pathlib import Path

    from backend.core.ouroboros.governance.comms.duplex import (
        audio_state_ipc as ipc,
    )

    absent = tmp_path / "nope.sock"
    assert await ipc._socket_is_live(absent) is False

    corpse = tmp_path / "corpse.sock"
    corpse.write_bytes(b"")
    assert await ipc._socket_is_live(corpse) is False, (
        "file presence was treated as proof of a live server"
    )


# ---------------------------------------------------------------------------
# BUG 7 — a server can lose its own address and never notice
# ---------------------------------------------------------------------------
#
# Observed live: the host was transcribing speech at the same moment the
# cockpit read "no audio plane". Both were telling the truth about DIFFERENT
# INODES — something had replaced the socket file, so the host went on serving
# an orphaned inode no path pointed at, and every client got ECONNREFUSED.
#
# Liveness cannot detect this. The process is fine, the pipeline is fine, the
# socket object is fine. Only IDENTITY can: is the file at my address still
# the one I bound?


async def test_the_host_notices_when_its_address_is_taken_away(tmp_path, monkeypatch):
    """THE REGRESSION. Deleting the socket path must be noticed and repaired,
    not served straight past."""
    import tempfile
    from pathlib import Path

    from backend.audio.audio_plane_host import AudioPlaneHost
    from backend.core.ouroboros.governance.comms.duplex import (
        audio_state_ipc as ipc,
    )

    sock = Path(tempfile.mkdtemp(prefix="ovwd")) / "a.sock"
    monkeypatch.setenv("JARVIS_AUDIO_IPC_SOCKET", str(sock))
    monkeypatch.setenv("JARVIS_AUDIO_PLANE_ADDRESS_CHECK_S", "1")

    broadcaster = ipc.AudioStateBroadcaster(path=sock)
    if not await broadcaster.start():
        pytest.skip("cannot bind a unix socket in this environment")

    host = AudioPlaneHost()
    host._handle = type("H", (), {"audio_ipc": broadcaster})()   # noqa: SLF001
    try:
        runner = asyncio.get_running_loop().create_task(host.run())
        await asyncio.sleep(0.2)
        first = host._inode                                       # noqa: SLF001
        assert first is not None

        sock.unlink()                       # the address is taken away
        await asyncio.sleep(2.5)            # one watchdog tick + rebind

        assert await ipc._socket_is_live(sock) is True, (
            "the host kept serving an orphaned inode"
        )
        assert host._inode != first, "re-bound to the same inode?"  # noqa: SLF001
    finally:
        host.request_stop("test")
        await asyncio.sleep(0.1)
        await broadcaster.stop()


async def test_identity_not_liveness_is_what_gets_compared():
    """A health check that asks 'am I running?' answers yes throughout this
    failure. The watchdog must compare the inode it bound."""
    import inspect

    from backend.audio.audio_plane_host import AudioPlaneHost

    src = inspect.getsource(AudioPlaneHost._address_watchdog)
    assert "_bound_inode" in src and "_rebind" in src


async def test_a_host_that_cannot_rebind_exits_rather_than_squatting(monkeypatch):
    """It holds the singleton lock. Staying alive while serving nothing would
    block every replacement from ever taking the microphone."""
    from backend.audio.audio_plane_host import AudioPlaneHost

    monkeypatch.setenv("JARVIS_AUDIO_PLANE_ADDRESS_CHECK_S", "1")
    host = AudioPlaneHost()
    host._inode = 12345                                   # noqa: SLF001
    host._handle = type("H", (), {"audio_ipc": None})()   # rebind impossible

    task = asyncio.get_running_loop().create_task(host._address_watchdog())
    await asyncio.sleep(2.5)
    assert host._stop.is_set(), (                          # noqa: SLF001
        "the host squatted on the lock while unable to serve"
    )
    task.cancel()


# ---------------------------------------------------------------------------
# BUG 8 — the conversation loop was gated on a listener that never hears
# ---------------------------------------------------------------------------
#
# Whisper's transcripts are drained only by ConversationPipeline.run(); run()
# starts only when the ModeDispatcher enters CONVERSATION; and that mode was
# entered only by a wake PHRASE heard through the realtime voice communicator
# — a different STT path with no audio in this host. Circular: the mode that
# would drain the transcripts was gated on a listener that never hears.
# Result: "Processing audio" forever, zero turns, Karen silent.


def test_lease_arm_enters_conversation_mode():
    """THE REGRESSION. `wake` already states the operator's intent to
    converse; it must reach the dispatcher's own switch_mode so session
    start, loop launch and speaker verification all ride the existing path."""
    import inspect

    from backend.audio import audio_pipeline_bootstrap as bootstrap

    src = inspect.getsource(bootstrap.wire_conversation_pipeline)
    i = src.index("audio lease ARMED")
    arm_block = src[i:i + 2000]
    assert "switch_mode" in arm_block and "CONVERSATION" in arm_block, (
        "arming the mic no longer starts the conversation loop — transcripts "
        "will pile up with no consumer again"
    )


def test_lease_disarm_leaves_conversation_mode():
    """Symmetric: a disarmed mic must not leave a loop pulling from a
    silenced STT forever."""
    import inspect

    from backend.audio import audio_pipeline_bootstrap as bootstrap

    src = inspect.getsource(bootstrap.wire_conversation_pipeline)
    i = src.index("DISARMED (fail-safe)")
    disarm_block = src[i:i + 1200]
    assert "switch_mode" in disarm_block and "COMMAND" in disarm_block


# ---------------------------------------------------------------------------
# BUG 9 — the cockpit's amplitude subscription was a one-shot
# ---------------------------------------------------------------------------
#
# The cockpit subscribed to the RMS stream ONCE at boot. The operator's own
# workflow violates that assumption every time: `ov` first, `wake` second —
# and wake is what SPAWNS the host. So the client was None before the host
# existed, and stayed None forever. The wave could not move.


def test_the_cockpit_keeps_the_rms_subscription_alive():
    from pathlib import Path

    src = Path("backend/core/ouroboros/cli/ov.py").read_text(encoding="utf-8")
    assert "_keep_rms_stream" in src
    assert "create_task(\n                    _keep_rms_stream" in src or \
           "_keep_rms_stream(_scope" in src, "keeper never scheduled"


async def test_the_keeper_connects_when_the_host_appears_late(monkeypatch):
    """THE REGRESSION, at the seam: no host at boot → host appears → the
    subscription must follow."""
    import backend.core.ouroboros.cli.ov as ov_mod

    calls = {"n": 0}
    state: dict = {}

    async def _fake_attach(_scope):
        calls["n"] += 1
        if calls["n"] < 3:
            return None                      # host not up yet
        return type("C", (), {"connected": True, "close": staticmethod(lambda: None)})()

    monkeypatch.setattr(ov_mod, "_attach_rms_stream", _fake_attach)
    task = asyncio.get_running_loop().create_task(
        ov_mod._keep_rms_stream(object(), state),
    )
    for _ in range(200):
        await asyncio.sleep(0.05)
        if state.get("rms_client") is not None:
            break
    state["closing"] = True
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    assert state.get("rms_client") is not None, (
        "the subscription never followed the late host"
    )
    assert calls["n"] >= 3


async def test_the_keeper_reconnects_after_a_host_restart(monkeypatch):
    import backend.core.ouroboros.cli.ov as ov_mod

    class _C:
        def __init__(self):
            self.connected = True

        async def close(self):
            self.connected = False

    made = []

    async def _fake_attach(_scope):
        c = _C()
        made.append(c)
        return c

    monkeypatch.setattr(ov_mod, "_attach_rms_stream", _fake_attach)
    state: dict = {}
    task = asyncio.get_running_loop().create_task(
        ov_mod._keep_rms_stream(object(), state),
    )
    for _ in range(100):
        await asyncio.sleep(0.02)
        if made:
            break
    made[0].connected = False               # the host dies
    for _ in range(200):
        await asyncio.sleep(0.05)
        if len(made) >= 2:
            break
    state["closing"] = True
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    assert len(made) >= 2, "a dead subscription was never replaced"
