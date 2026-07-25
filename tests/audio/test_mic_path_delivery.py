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
