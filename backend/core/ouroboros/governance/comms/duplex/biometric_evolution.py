"""Rolling Biometric Evolution — the organism adapts to its operator.

E2E Sovereignty (operator authorization 2026-07-19). The REAL
enrollment lives in the learning DB (`speaker_profiles`: 192-dim
speechbrain x-vector, 272 samples for the operator) — resolved
DYNAMICALLY through the store's own path resolver (mandate 1: zero
hardcoded paths; env `JARVIS_LEARNING_DB_PATH` / `JARVIS_DATA_DIR`
honored exactly as `speaker_profile_store` does).

Mandate correction, stated openly: the authorization named MFCC
blending, but the enrolled tensor is an X-VECTOR — blending MFCCs into
an x-vector store would corrupt it. Evolution therefore blends in the
profile's NATIVE embedding space with the same slow-EMA law.

Pipeline: normalize_acoustics (numpy log-spectral mean subtraction —
the classic dereverb pre-filter; room response is multiplicative in
the spectrum, so subtracting the per-utterance log-spectral mean
strips the static channel) → scorer → high-confidence pass
(> JARVIS_SENTRY_EVOLUTION_DELTA) → EMA blend
(JARVIS_BIOMETRIC_EVOLUTION_ALPHA, slow) → persisted to the SAME row.
Strict tensor guards: shape mismatch or non-finite blend ABORTS —
the enrollment can never be corrupted by a bad sample. NEVER raises.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import struct
from typing import Any, Optional, Tuple

import numpy as np

logger = logging.getLogger("Ouroboros.BiometricEvolution")


def _evolution_alpha() -> float:
    try:
        return max(0.001, min(0.2, float(os.environ.get(
            "JARVIS_BIOMETRIC_EVOLUTION_ALPHA", "0.02",
        ))))
    except (TypeError, ValueError):
        return 0.02


def evolution_delta() -> float:
    """High-confidence gate for evolution (NOT verification — a
    sample must clear this well above the auth threshold before it
    may teach the profile)."""
    try:
        return max(0.5, min(0.99, float(os.environ.get(
            "JARVIS_SENTRY_EVOLUTION_DELTA", "0.85",
        ))))
    except (TypeError, ValueError):
        return 0.85


def resolve_profile_db() -> Optional[str]:
    """Dynamic namespace resolution via the EXISTING store's resolver
    (DRY — its env contract IS the contract). NEVER raises."""
    try:
        from backend.intelligence.speaker_profile_store import (  # noqa: E501,PLC0415
            _get_default_db_path,
        )
        path = _get_default_db_path()
        return path if os.path.exists(path) else None
    except Exception:  # noqa: BLE001
        return None


def load_enrollment(
    db_path: Optional[str] = None,
) -> Optional[Tuple[int, str, "np.ndarray"]]:
    """(speaker_id, name, embedding[float32]) for the HIGHEST-
    confidence profile (most enrollment samples). NEVER raises."""
    try:
        path = db_path or resolve_profile_db()
        if path is None:
            return None
        con = sqlite3.connect(path)
        try:
            row = con.execute(
                "SELECT speaker_id, speaker_name, voiceprint_embedding, "
                "embedding_dimension FROM speaker_profiles "
                "ORDER BY total_samples DESC LIMIT 1",
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        sid, name, blob, dim = row
        emb = np.frombuffer(blob, dtype=np.float32)
        if dim and emb.size != int(dim):
            # 64-bit stored profiles: honor the declared dimension.
            emb64 = np.frombuffer(blob, dtype=np.float64)
            if emb64.size == int(dim):
                emb = emb64.astype(np.float32)
        if emb.size == 0:
            return None
        return int(sid), str(name), emb.copy()
    except Exception:  # noqa: BLE001
        logger.debug("[BioEvo] enrollment load degraded", exc_info=True)
        return None


def normalize_acoustics(window: "np.ndarray", rate: int = 16000) -> "np.ndarray":
    """Dynamic Acoustic Normalization (numpy-only, DRY): pre-emphasis
    + log-spectral mean subtraction — strips the static room/channel
    response (reverb tail energy) before scoring. Pure; NEVER raises;
    degrades to the input."""
    try:
        x = np.asarray(window, dtype=np.float32).reshape(-1)
        if x.size < 256:
            return x
        # Pre-emphasis (standard 0.97) balances the spectral tilt.
        y = np.empty_like(x)
        y[0] = x[0]
        y[1:] = x[1:] - 0.97 * x[:-1]
        # Log-spectral mean subtraction (channel = multiplicative →
        # additive in log-spectrum → subtract the utterance mean).
        n = 512
        hop = 256
        frames = [
            y[i:i + n] * np.hanning(n)
            for i in range(0, y.size - n, hop)
        ]
        if not frames:
            return y
        spec = np.array([np.fft.rfft(f) for f in frames])
        mag = np.abs(spec) + 1e-8
        phase = spec / mag
        log_mag = np.log(mag)
        log_mag -= log_mag.mean(axis=0, keepdims=True)
        clean = np.fft.irfft(np.exp(log_mag) * phase, n=n)
        out = np.zeros(y.size, dtype=np.float32)
        win_acc = np.zeros(y.size, dtype=np.float32)
        h = np.hanning(n).astype(np.float32)
        for k, i in enumerate(range(0, y.size - n, hop)):
            out[i:i + n] += clean[k].astype(np.float32) * h
            win_acc[i:i + n] += h * h
        win_acc[win_acc < 1e-6] = 1.0
        return out / win_acc
    except Exception:  # noqa: BLE001
        return np.asarray(window, dtype=np.float32).reshape(-1)


def blend_profile(
    baseline: "np.ndarray",
    new_embedding: "np.ndarray",
    *,
    alpha: Optional[float] = None,
) -> Optional["np.ndarray"]:
    """The EMA law: ``(1−α)·baseline + α·new``, α slow. STRICT tensor
    guards — shape mismatch, dtype garbage, or non-finite result
    returns None (the enrollment can NEVER be corrupted). Pure."""
    try:
        a = alpha if alpha is not None else _evolution_alpha()
        b = np.asarray(baseline, dtype=np.float32).reshape(-1)
        n = np.asarray(new_embedding, dtype=np.float32).reshape(-1)
        if b.size == 0 or b.shape != n.shape:
            return None
        blended = (1.0 - a) * b + a * n
        if not np.all(np.isfinite(blended)) or blended.shape != b.shape:
            return None
        return blended
    except Exception:  # noqa: BLE001
        return None


def persist_evolution(
    speaker_id: int,
    blended: "np.ndarray",
    *,
    db_path: Optional[str] = None,
) -> bool:
    """Write the evolved tensor back to the SAME enrollment row +
    bump total_samples. NEVER raises."""
    try:
        path = db_path or resolve_profile_db()
        if path is None:
            return False
        blob = np.asarray(blended, dtype=np.float32).tobytes()
        con = sqlite3.connect(path)
        try:
            con.execute(
                "UPDATE speaker_profiles SET voiceprint_embedding=?, "
                "total_samples=total_samples+1 WHERE speaker_id=?",
                (blob, speaker_id),
            )
            con.commit()
        finally:
            con.close()
        logger.info(
            "[BioEvo] profile %d evolved (EMA α=%.3f, dim=%d)",
            speaker_id, _evolution_alpha(), blended.size,
        )
        return True
    except Exception:  # noqa: BLE001
        logger.debug("[BioEvo] persist degraded", exc_info=True)
        return False


def evolve_if_confident(
    confidence: float,
    new_embedding: Any,
    *,
    db_path: Optional[str] = None,
) -> bool:
    """The sentry hook: high-confidence pass → blend → persist.
    Anything below the evolution delta is verification-only (the
    profile never learns from marginal samples). NEVER raises."""
    try:
        if confidence < evolution_delta():
            return False
        enrolled = load_enrollment(db_path)
        if enrolled is None:
            return False
        sid, _name, baseline = enrolled
        blended = blend_profile(baseline, new_embedding)
        if blended is None:
            return False
        return persist_evolution(sid, blended, db_path=db_path)
    except Exception:  # noqa: BLE001
        return False


__all__ = [
    "blend_profile",
    "evolution_delta",
    "evolve_if_confident",
    "load_enrollment",
    "normalize_acoustics",
    "persist_evolution",
    "resolve_profile_db",
]
