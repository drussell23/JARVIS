"""Sentry bootstrap spine — deferred capture, biometric gate, total gate.

Mandate 4 verbatim (2026-07-19): a device-busy hardware capture
failure at supervisor init must leave orchestration loops alive,
transition to DEFERRED_CAPTURE gracefully, and retry the audio plane —
no fatal panic.
"""
from __future__ import annotations

import asyncio
import errno
from pathlib import Path

import numpy as np
import pytest

from backend.core.ouroboros.governance.comms.duplex.sentry_bootstrap import (
    STATE_ACTIVE,
    STATE_DEFERRED_CAPTURE,
    STATE_FAILED,
    BiometricGateAdapter,
    DeferredCaptureAllocator,
    classify_capture_error,
    mount_passive_sentry,
)

_REPO = Path(__file__).resolve().parents[2]


class TestCaptureClassification:
    def test_posix_busy_codes_are_retryable(self):
        e = OSError(errno.EBUSY, "Device busy")
        assert classify_capture_error(e) == "busy"
        assert classify_capture_error(
            OSError(errno.EACCES, "not permitted"),
        ) == "busy"

    def test_genuine_faults_never_retried(self):
        assert classify_capture_error(ValueError("bad rate")) == "fault"
        assert classify_capture_error(
            OSError(errno.ENOENT, "no device"),
        ) == "fault"


class TestDeferredCapture:
    async def test_device_busy_defers_then_recovers_no_panic(self):
        """MANDATE 4 VERBATIM: busy at init → DEFERRED_CAPTURE, the
        loop keeps running, retry succeeds, state → ACTIVE."""
        attempts = {"n": 0}

        def _opener():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise OSError(errno.EBUSY, "Device busy (media link)")
            return "capture-handle"

        alloc = DeferredCaptureAllocator(
            _opener, retry_interval_s=0.05, max_retries=10,
        )
        assert alloc.ensure() == STATE_DEFERRED_CAPTURE   # graceful
        # The primary loop is provably alive — we're awaiting on it:
        for _ in range(60):
            if alloc.state == STATE_ACTIVE:
                break
            await asyncio.sleep(0.05)
        assert alloc.state == STATE_ACTIVE
        assert alloc.handle == "capture-handle"
        assert alloc.stats["busy"] == 2
        await alloc.stop()

    async def test_real_fault_fails_permanently_no_blind_retry(self):
        def _opener():
            raise ValueError("format mismatch — a real bug")

        alloc = DeferredCaptureAllocator(_opener, retry_interval_s=0.05)
        assert alloc.ensure() == STATE_FAILED
        await asyncio.sleep(0.2)
        assert alloc.stats["attempts"] == 1               # never retried
        await alloc.stop()

    async def test_retry_budget_bounded(self):
        def _opener():
            raise OSError(errno.EBUSY, "busy forever")

        alloc = DeferredCaptureAllocator(
            _opener, retry_interval_s=0.02, max_retries=3,
        )
        alloc.ensure()
        for _ in range(60):
            if alloc.state == STATE_FAILED:
                break
            await asyncio.sleep(0.02)
        assert alloc.state == STATE_FAILED
        assert alloc.stats["attempts"] == 4               # 1 + 3 retries
        await alloc.stop()


class TestBiometricGate:
    async def test_clear_pass_and_clear_fail(self, monkeypatch):
        monkeypatch.setenv("JARVIS_TIER1_VBIA_THRESHOLD", "0.70")
        monkeypatch.setenv("JARVIS_SENTRY_VBIA_BOUNDARY_MARGIN", "0.05")

        async def _hi(_w): return 0.9, True
        async def _lo(_w): return 0.2, True
        assert await BiometricGateAdapter(_hi).verify(np.ones(100)) is True
        assert await BiometricGateAdapter(_lo).verify(np.ones(100)) is False

    async def test_boundary_band_invokes_pava_drift(self, monkeypatch):
        monkeypatch.setenv("JARVIS_TIER1_VBIA_THRESHOLD", "0.70")
        monkeypatch.setenv("JARVIS_SENTRY_VBIA_BOUNDARY_MARGIN", "0.05")
        calls = {"n": 0}

        async def _scorer(w):
            calls["n"] += 1
            if calls["n"] == 1:
                return 0.71, True          # boundary → PAVA engages
            return 0.72 + 0.01 * calls["n"], True   # non-degrading trend

        gate = BiometricGateAdapter(_scorer)
        assert await gate.verify(np.ones(48000, dtype=np.float32)) is True
        assert gate.stats["pava_evals"] == 1
        assert gate.stats["pava_pass"] == 1
        assert calls["n"] == 4                            # 1 + 3 frames

    async def test_boundary_with_degrading_drift_fails(self, monkeypatch):
        monkeypatch.setenv("JARVIS_TIER1_VBIA_THRESHOLD", "0.70")
        monkeypatch.setenv("JARVIS_SENTRY_VBIA_BOUNDARY_MARGIN", "0.05")
        seq = iter([(0.71, True), (0.8, True), (0.6, True), (0.5, True)])

        async def _scorer(_w):
            return next(seq)

        gate = BiometricGateAdapter(_scorer)
        assert await gate.verify(np.ones(48000, dtype=np.float32)) is False
        assert gate.stats["pava_fail"] == 1

    async def test_scorer_error_fails_closed(self):
        async def _boom(_w):
            raise RuntimeError("biometric stack offline")

        assert await BiometricGateAdapter(_boom).verify(np.ones(10)) is False

    def test_sentry_inverts_permissive_bypass_pin(self):
        src = (
            _REPO / "backend/core/ouroboros/governance/comms/duplex/"
            "sentry_bootstrap.py"
        ).read_text()
        assert "AuthResult.PASSED" in src
        assert "fail closed" in src.lower() or "fail CLOSED" in src


class TestTotalGate:
    def test_gate_down_means_zero_instantiation(self, monkeypatch):
        monkeypatch.delenv("JARVIS_PASSIVE_SENTRY_ENABLED", raising=False)
        touched = {"broadcaster": 0, "mic": 0}

        class _B:
            def __getattr__(self, name):
                touched["broadcaster"] += 1
                raise AttributeError(name)

        result = mount_passive_sentry(
            broadcaster=_B(),
            mic_register=lambda cb: touched.__setitem__("mic", 1),
        )
        assert result is None
        assert touched == {"broadcaster": 0, "mic": 0}    # NOTHING touched

    def test_gate_check_precedes_imports_pin(self):
        src = (
            _REPO / "backend/core/ouroboros/governance/comms/duplex/"
            "sentry_bootstrap.py"
        ).read_text()
        body = src[src.index("def mount_passive_sentry"):]
        assert body.index("sentry_enabled()") < body.index(
            "from .passive_sentry",
        )

    async def test_gate_up_full_mount_with_blanking_wrap(self, monkeypatch):
        monkeypatch.setenv("JARVIS_PASSIVE_SENTRY_ENABLED", "true")

        class _Broadcaster:
            def __init__(self):
                self.events = []

            def publish_event(self, kind):
                self.events.append(kind)

        b = _Broadcaster()
        regs = []
        sentry = mount_passive_sentry(
            broadcaster=b, mic_register=lambda cb: regs.append(cb) or "h",
        )
        assert sentry is not None
        assert regs, "mic consumer never registered"
        assert sentry.capture.state == STATE_ACTIVE
        # The blanking wrap: AUDIO_PLAYING blanks, event still flows.
        b.publish_event("AUDIO_PLAYING")
        assert b.events == ["AUDIO_PLAYING"]
        assert sentry.blanked is True
        b.publish_event("AUDIO_IDLE")
        assert sentry.stats["mirage_suppressed"] == 0     # nothing fired yet

    def test_dry_lease_pathway_pin(self):
        src = (
            _REPO / "backend/core/ouroboros/governance/comms/duplex/"
            "sentry_bootstrap.py"
        ).read_text()
        assert "RemoteAudioLease" in src                  # the CLI wake path
        assert "acquire()" in src


# ---------------------------------------------------------------------------
# Rolling Biometric Evolution (E2E Sovereignty, 2026-07-19)
# ---------------------------------------------------------------------------


class TestBiometricEvolution:
    def _seed_db(self, tmp_path, dim=192):
        import sqlite3
        db = tmp_path / "learning.db"
        con = sqlite3.connect(db)
        con.execute(
            "CREATE TABLE speaker_profiles (speaker_id INTEGER, "
            "speaker_name TEXT, voiceprint_embedding BLOB, "
            "embedding_dimension INTEGER, total_samples INTEGER)",
        )
        base = np.linspace(0.1, 1.0, dim).astype(np.float32)
        con.execute(
            "INSERT INTO speaker_profiles VALUES (1, 'Derek J. Russell', "
            "?, ?, 272)", (base.tobytes(), dim),
        )
        con.commit(); con.close()
        return str(db), base

    def test_high_confidence_pass_evolves_baseline_shape_preserved(
        self, tmp_path, monkeypatch,
    ):
        """MANDATE 4 VERBATIM: high-confidence wake chunk → baseline
        voiceprint tensor updated in memory, shape UNCORRUPTED."""
        from backend.core.ouroboros.governance.comms.duplex import (
            biometric_evolution as be,
        )
        monkeypatch.setenv("JARVIS_BIOMETRIC_EVOLUTION_ALPHA", "0.05")
        db, base = self._seed_db(tmp_path)
        new = np.ones(192, dtype=np.float32)
        assert be.evolve_if_confident(0.95, new, db_path=db) is True
        sid, name, evolved = be.load_enrollment(db)
        assert name == "Derek J. Russell"
        assert evolved.shape == base.shape                 # UNCORRUPTED
        assert evolved.dtype == np.float32
        expected = 0.95 * base + 0.05 * new
        assert np.allclose(evolved, expected, atol=1e-6)   # exact EMA law
        import sqlite3
        n = sqlite3.connect(db).execute(
            "SELECT total_samples FROM speaker_profiles",
        ).fetchone()[0]
        assert n == 273                                    # sample counted

    def test_marginal_confidence_never_teaches(self, tmp_path):
        from backend.core.ouroboros.governance.comms.duplex import (
            biometric_evolution as be,
        )
        db, base = self._seed_db(tmp_path)
        assert be.evolve_if_confident(
            0.75, np.ones(192, dtype=np.float32), db_path=db,
        ) is False
        _s, _n, after = be.load_enrollment(db)
        assert np.array_equal(after, base)                 # untouched

    def test_shape_mismatch_aborts_no_corruption(self, tmp_path):
        from backend.core.ouroboros.governance.comms.duplex import (
            biometric_evolution as be,
        )
        db, base = self._seed_db(tmp_path)
        assert be.evolve_if_confident(
            0.95, np.ones(64, dtype=np.float32), db_path=db,
        ) is False
        _s, _n, after = be.load_enrollment(db)
        assert np.array_equal(after, base)

    def test_nonfinite_blend_rejected(self):
        from backend.core.ouroboros.governance.comms.duplex import (
            biometric_evolution as be,
        )
        base = np.ones(192, dtype=np.float32)
        bad = np.full(192, np.inf, dtype=np.float32)
        assert be.blend_profile(base, bad) is None

    def test_normalization_preserves_length_and_energy_shape(self):
        from backend.core.ouroboros.governance.comms.duplex import (
            biometric_evolution as be,
        )
        rng = np.random.default_rng(5)
        x = rng.standard_normal(16000).astype(np.float32) * 0.1
        y = be.normalize_acoustics(x)
        assert y.shape == x.shape and np.all(np.isfinite(y))

    def test_real_enrollment_resolves_dynamically(self):
        """Against the OPERATOR'S actual store (read-only) — the
        dynamic resolver finds the real 192-dim x-vector."""
        from backend.core.ouroboros.governance.comms.duplex import (
            biometric_evolution as be,
        )
        enrolled = be.load_enrollment()
        if enrolled is None:
            pytest.skip("no enrollment on this machine")
        _sid, name, emb = enrolled
        assert emb.dtype == np.float32 and emb.size in (192, 512)
        assert name                                        # a real human name
