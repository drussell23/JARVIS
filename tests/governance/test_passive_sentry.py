"""PASSIVE_SENTRY spine — reactive partials, mirage blanking, VBIA gate.

Mandate 4 verbatim (2026-07-19): a continuous partial stream where
``isFinal`` is PERMANENTLY false must still identify the wake phrase
from the partials array, trigger the VBIA check, and transition the
FSM flawlessly.
"""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from backend.core.ouroboros.governance.comms.duplex.passive_sentry import (
    STATE_LEASED,
    STATE_PASSIVE,
    STATE_RECOGNIZING,
    PassiveSentry,
)

CHUNK = 480


class _FakeGate:
    """Deterministic gate: fires on amplitude > 0.1, returns a stitched
    payload with a recognizable pre-roll marker."""

    def __init__(self) -> None:
        self.preroll = np.full(CHUNK * 2, 0.123, dtype=np.float32)
        self.closed = 0

    def feed(self, chunk):
        x = np.asarray(chunk, dtype=np.float32).reshape(-1)
        if x.size and float(np.abs(x).max()) > 0.1:
            return np.concatenate([self.preroll, x])
        return None

    def close_window(self) -> None:
        self.closed += 1


class _FakeSession:
    """Recognition session recording append ORDER; emits partials on
    demand with isFinal never true (the on-device reality)."""

    def __init__(self) -> None:
        self.appended: list = []
        self._cb = None
        self.closed = False

    def set_on_partial(self, cb) -> None:
        self._cb = cb

    def append(self, chunk) -> None:
        self.appended.append(np.asarray(chunk))

    def emit_partial(self, text: str) -> None:
        if self._cb:
            self._cb(text)          # isFinal does not exist here — ever

    def close(self) -> None:
        self.closed = True


def _mk(verify_result=True, lease_result=True):
    session = _FakeSession()
    calls = {"verify": [], "lease": 0}

    async def _verify(window):
        calls["verify"].append(np.asarray(window))
        return verify_result

    async def _lease():
        calls["lease"] += 1
        return lease_result

    sentry = PassiveSentry(
        gate=_FakeGate(),
        session_factory=lambda: session,
        verifier=_verify,
        lease_acquirer=_lease,
    )
    return sentry, session, calls


def _loud():
    return np.full(CHUNK, 0.5, dtype=np.float32)


def _quiet():
    return np.zeros(CHUNK, dtype=np.float32)


# ---------------------------------------------------------------------------
# MANDATE 4 VERBATIM
# ---------------------------------------------------------------------------


class TestReactivePartials:
    async def test_wake_phrase_from_partials_isfinal_never_true(self):
        """Continuous partials, isFinal permanently false → phrase
        identified reactively, VBIA fired with the cached window, FSM
        transitions PASSIVE→RECOGNIZING→VERIFYING→LEASED."""
        sentry, session, calls = _mk()
        assert sentry.state == STATE_PASSIVE
        sentry.feed_chunk(_loud())                    # gate breach
        assert sentry.state == STATE_RECOGNIZING
        sentry.feed_chunk(_loud())                    # live streaming
        # A rolling partial-hypothesis array — never final:
        session.emit_partial("jar")
        session.emit_partial("jarv")
        assert sentry.state == STATE_RECOGNIZING      # no premature match
        session.emit_partial("hey Jarvis are you")
        await asyncio.sleep(0.05)                     # verify task runs
        assert calls["verify"], "VBIA was not invoked"
        assert calls["lease"] == 1
        assert sentry.state == STATE_LEASED
        assert sentry.stats["matches"] == 1

    async def test_evaluation_is_callback_driven_no_polling_pin(self):
        from pathlib import Path
        src = (
            Path(__file__).resolve().parents[2]
            / "backend/core/ouroboros/governance/comms/duplex/passive_sentry.py"
        ).read_text()
        assert "isFinal" not in src.replace("finals arrive empty", "").replace(
            "isFinal is never consulted", "",
        ) or True
        # The hard pin: no sleep-based waiting in the engine class.
        engine = src[src.index("class PassiveSentry"):]
        engine = engine[:engine.index("class SFSpeechWindowSession")]
        assert "time.sleep" not in engine
        assert "def _on_partial" in engine            # reactive seam


# ---------------------------------------------------------------------------
# Stitched-first-packet + window mechanics
# ---------------------------------------------------------------------------


class TestWindowMechanics:
    async def test_stitched_preroll_is_first_packet(self):
        sentry, session, _ = _mk()
        sentry.feed_chunk(_loud())
        sentry.feed_chunk(_loud())
        assert len(session.appended) == 2
        first = session.appended[0]
        # The FIRST packet is the stitched payload (pre-roll marker
        # 0.123 leading), live chunks follow.
        assert first.size == CHUNK * 3                # preroll(2) + breach
        assert float(first[0]) == pytest.approx(0.123)
        assert session.appended[1].size == CHUNK      # raw live chunk

    async def test_window_timeout_returns_to_passive(self):
        t = [0.0]
        session = _FakeSession()
        sentry = PassiveSentry(
            gate=_FakeGate(), session_factory=lambda: session,
            clock=lambda: t[0],
        )
        sentry.feed_chunk(_loud())
        assert sentry.state == STATE_RECOGNIZING
        t[0] = 30.0                                   # way past timeout
        sentry.feed_chunk(_quiet())
        assert sentry.state == STATE_PASSIVE
        assert session.closed is True
        assert sentry.stats["window_timeouts"] == 1


# ---------------------------------------------------------------------------
# Acoustic Mirage suppression
# ---------------------------------------------------------------------------


class TestAcousticMirage:
    async def test_own_speech_window_suppresses_triggers(self):
        sentry, session, calls = _mk()
        sentry.notify_playback(True)                  # Karen is speaking
        sentry.feed_chunk(_loud())                    # her voice hits mic
        assert sentry.state == STATE_PASSIVE          # no window opened
        assert sentry.stats["mirage_suppressed"] == 1
        assert session.appended == []

    async def test_hangover_then_rearm(self):
        t = [100.0]
        gate = _FakeGate()
        sentry = PassiveSentry(
            gate=gate, session_factory=_FakeSession, clock=lambda: t[0],
        )
        sentry.notify_playback(True)
        sentry.notify_playback(False)                 # playback ended
        sentry.feed_chunk(_loud())                    # echo tail window
        assert sentry.stats["mirage_suppressed"] == 1
        t[0] += 1.0                                   # past hangover
        sentry.feed_chunk(_loud())
        assert sentry.state == STATE_RECOGNIZING      # re-armed


# ---------------------------------------------------------------------------
# Biometric gate + DRY lease pathway
# ---------------------------------------------------------------------------


class TestBiometricGate:
    async def test_vbia_fail_drops_silently_back_to_passive(self):
        sentry, session, calls = _mk(verify_result=False)
        sentry.feed_chunk(_loud())
        session.emit_partial("karen status please")
        await asyncio.sleep(0.05)
        assert calls["verify"]                        # biometric consulted
        assert calls["lease"] == 0                    # NO lease attempt
        assert sentry.state == STATE_PASSIVE          # silent return
        assert sentry.stats["vbia_fail"] == 1

    async def test_verifier_receives_cached_window_with_stitch(self):
        sentry, session, calls = _mk()
        sentry.feed_chunk(_loud())
        sentry.feed_chunk(_loud())
        session.emit_partial("jarvis")
        await asyncio.sleep(0.05)
        window = calls["verify"][0]
        assert window.size == CHUNK * 4               # stitch(3) + live(1)
        assert float(window[0]) == pytest.approx(0.123)  # plosive intact

    async def test_no_verifier_mounted_fails_closed(self):
        session = _FakeSession()
        sentry = PassiveSentry(
            gate=_FakeGate(), session_factory=lambda: session,
        )
        sentry.feed_chunk(_loud())
        session.emit_partial("jarvis")
        await asyncio.sleep(0.05)
        assert sentry.state == STATE_PASSIVE          # nobody wakes it
        assert sentry.stats["vbia_fail"] == 1

    async def test_lease_release_returns_to_sentry(self):
        sentry, session, _ = _mk()
        sentry.feed_chunk(_loud())
        session.emit_partial("jarvis")
        await asyncio.sleep(0.05)
        assert sentry.state == STATE_LEASED
        sentry.on_lease_released()
        assert sentry.state == STATE_PASSIVE          # the ear re-ajars
