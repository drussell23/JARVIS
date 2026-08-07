#!/usr/bin/env python3
"""
🚀 ADVANCED BIOMETRIC VOICE VERIFICATION SYSTEM
═══════════════════════════════════════════════════════════════════════════════

State-of-the-art probabilistic voice authentication with:
- Bayesian verification with uncertainty quantification
- Multi-modal biometric fusion (embedding + physics + acoustics)
- Mahalanobis distance with adaptive covariance
- Physics-based voice plausibility checking
- Anti-spoofing detection (replay, synthesis, voice conversion)
- Dynamic Time Warping for temporal alignment
- Adaptive threshold learning
- Zero hardcoded values - fully dynamic

All CPU-intensive work runs in thread pool to prevent blocking.

Author: Claude Code + Derek J. Russell
Version: 2.0.0 - Async Optimized (Beast Mode)
License: MIT
"""

import asyncio
import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Dict, List, Mapping, Optional, Tuple

import numpy as np
from scipy import stats
from scipy.spatial.distance import mahalanobis
from scipy.stats import multivariate_normal

# Import managed executor for clean shutdown
try:
    from core.thread_manager import ManagedThreadPoolExecutor
    _HAS_MANAGED_EXECUTOR = True
except ImportError:
    _HAS_MANAGED_EXECUTOR = False

# The measurability bands and the one ``is this a measurement`` predicate.
#
# This module is imported both as ``backend.voice.…`` and as ``voice.…``
# depending on which root is on sys.path — the same reason the executor import
# above is guarded. ``biological_bounds`` and ``embedding_ops`` pull in nothing
# heavier than numpy, so neither costs this path the SpeechBrain import it is
# careful to avoid.
try:  # pragma: no cover - exercised by whichever root the process happens to use
    from backend.voice.biological_bounds import BOUNDS, is_measured
    from backend.voice.embedding_ops import coerce_vector
except ImportError:  # pragma: no cover
    from voice.biological_bounds import BOUNDS, is_measured
    from voice.embedding_ops import coerce_vector

logger = logging.getLogger(__name__)

# Shared thread pool for CPU-intensive biometric computations
_biometric_executor: Optional[ThreadPoolExecutor] = None


def get_biometric_executor() -> ThreadPoolExecutor:
    """Get or create the shared thread pool for biometric computations."""
    global _biometric_executor
    if _biometric_executor is None:
        if _HAS_MANAGED_EXECUTOR:
            _biometric_executor = ManagedThreadPoolExecutor(
                max_workers=4,
                thread_name_prefix="biometric_verify",
                name="biometric_verify"
            )
        else:
            _biometric_executor = ThreadPoolExecutor(
                max_workers=4,
                thread_name_prefix="biometric_verify"
            )
        logger.info("🔧 Created biometric verification thread pool (4 workers)")
    return _biometric_executor


def shutdown_biometric_executor():
    """Shutdown the biometric thread pool gracefully."""
    global _biometric_executor
    if _biometric_executor is not None:
        logger.info("🔧 Shutting down biometric verification thread pool...")
        _biometric_executor.shutdown(wait=True)
        _biometric_executor = None


# ═══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class VoiceBiometricFeatures:
    """Comprehensive voice biometric features"""

    # Deep learning embedding
    embedding: np.ndarray
    embedding_confidence: float

    # Acoustic features
    pitch_mean: float
    pitch_std: float
    pitch_range: float
    formant_f1: float
    formant_f2: float
    formant_f3: float
    formant_f4: float

    # Spectral features
    spectral_centroid: float
    spectral_rolloff: float
    spectral_flux: float
    spectral_entropy: float

    # Temporal features
    speaking_rate: float
    pause_ratio: float
    energy_contour: np.ndarray

    # Voice quality
    jitter: float
    shimmer: float
    harmonic_to_noise_ratio: float

    # Metadata
    duration_seconds: float
    sample_rate: int
    timestamp: datetime = field(default_factory=datetime.now)


def _env_float(name: str, default: float) -> float:
    """
    A tunable read from the environment, defaulting on anything unreadable.

    A malformed override must not reach a security decision: silently keeping
    the default is correct, and the warning says which knob was ignored so a
    typo does not quietly persist as "the feature is not working".
    """
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("[AntiSpoof] %s=%r is not a number — keeping %s", name, raw, default)
        return default
    if not math.isfinite(value):
        logger.warning("[AntiSpoof] %s=%r is not finite — keeping %s", name, raw, default)
        return default
    return value


@dataclass
class PhysicsConstraints:
    """Physics-based constraints for human voice"""

    # Vocal tract physics
    min_vocal_tract_length: float = 0.13  # 13cm (child)
    max_vocal_tract_length: float = 0.20  # 20cm (adult male)

    # Pitch constraints (Hz)
    min_pitch_male: float = 85.0
    max_pitch_male: float = 180.0
    min_pitch_female: float = 165.0
    max_pitch_female: float = 255.0

    # Formant relationships (physics-based)
    f1_f2_min_ratio: float = 0.2
    f1_f2_max_ratio: float = 0.8

    # Harmonic structure
    min_harmonics: int = 3
    max_harmonic_deviation: float = 0.05  # 5%

    # Energy constraints
    min_hnr_db: float = 5.0  # Harmonic-to-Noise Ratio
    max_jitter: float = 0.02  # 2%
    max_shimmer: float = 0.10  # 10%

    # ── Measurability bands ──────────────────────────────────────────────
    # The range each quantity occupies in real human speech. A value outside
    # its band was not measured — a feature extractor that failed, a unit that
    # is not the one declared, or a default that was never filled in. It is NOT
    # a strange but genuine voice, and must not be scored as one.
    #
    # These exist because the enrolled profile on this machine carries
    # speaking_rate_wpm = 420.0 (no human sustains 420 wpm; conversational
    # speech is 110-180) and formants of 42.9 Hz / 80.0 Hz (F1 is ~500 Hz, F2
    # ~1500 Hz — these are off by an order of magnitude and are not formants).
    # Compared against, they made the operator's own voice look synthetic.
    #
    # The numbers live in ``biological_bounds.BOUNDS``, which is also what the
    # *extractors* gate on before writing. Sourcing the defaults from there
    # rather than restating them is what keeps the write side and the read side
    # from drifting into two different opinions of "possible" — the enrolled
    # 420 wpm passed the writer precisely because the writer had no opinion.
    # The env override namespace is unchanged and unified: each band has exactly
    # one knob, ``JARVIS_VOICE_PHYSICS_{MIN,MAX}_<NAME>``, honoured identically
    # by ``from_env`` below and by ``BiologicalBoundsValidator.from_env``.
    min_formant_f1_hz: float = BOUNDS["formant_f1_hz"].low
    max_formant_f1_hz: float = BOUNDS["formant_f1_hz"].high
    min_formant_f2_hz: float = BOUNDS["formant_f2_hz"].low
    max_formant_f2_hz: float = BOUNDS["formant_f2_hz"].high
    min_speaking_rate_wpm: float = BOUNDS["speaking_rate_wpm"].low
    max_speaking_rate_wpm: float = BOUNDS["speaking_rate_wpm"].high
    min_pitch_std_hz: float = BOUNDS["pitch_std_hz"].low
    max_pitch_std_hz: float = BOUNDS["pitch_std_hz"].high
    max_hnr_db: float = BOUNDS["harmonic_to_noise_ratio_db"].high

    # Measurability floor for HNR, distinct from ``min_hnr_db`` above.
    #
    # ``min_hnr_db`` (5.0) is a *plausibility* threshold — below it a voice
    # scores poorly on the harmonic check. Using it as the measurability gate
    # too, as the first version of this did, means a genuinely noisy but
    # perfectly real 3 dB capture reads as "never measured" and the check
    # abstains instead of scoring the evidence it has. The two bands answer
    # different questions and must be two fields.
    min_hnr_db_measurable: float = BOUNDS["harmonic_to_noise_ratio_db"].low
    min_pitch_hz_measurable: float = BOUNDS["pitch_mean_hz"].low
    max_pitch_hz_measurable: float = BOUNDS["pitch_mean_hz"].high

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "PhysicsConstraints":
        """
        Build constraints from the environment, field by field.

        Defaults are the phonetics-literature values above. Every field is
        overridable as ``JARVIS_VOICE_PHYSICS_<FIELD>`` so a different mic,
        codec or extractor version can be accommodated without a code change —
        and so the bands can be widened in the field if one proves too tight.

        A malformed override keeps the default rather than propagating a
        garbage bound into a security decision.
        """
        source = os.environ if env is None else env
        values = {}
        for f in fields(cls):
            key = f"JARVIS_VOICE_PHYSICS_{f.name.upper()}"
            raw = str(source.get(key, "")).strip()
            if not raw:
                continue
            try:
                values[f.name] = type(f.default)(raw)
            except (TypeError, ValueError):
                logger.warning(
                    "[Physics] %s=%r is not a %s — keeping default %r",
                    key, raw, type(f.default).__name__, f.default,
                )
        return cls(**values)

    def measured(self, value: Any, low: float, high: float) -> bool:
        """
        True when ``value`` is a real measurement inside its plausible band.

        ``None``, NaN, infinity, non-numeric and out-of-band all read as NOT
        measured. Zero is excluded by every band below, which is deliberate: an
        unset float field defaults to 0.0, and 0 Hz is not a formant.

        This is a thin delegate to ``biological_bounds.is_measured``, which is
        the one predicate the extractors, the anti-spoofing stage and the
        plausibility scorer all share. It stays here because callers hold a
        ``PhysicsConstraints`` and their bands live on it; it must not grow a
        second implementation, or the write side and the read side can once
        again disagree about what "measured" means.
        """
        return is_measured(value, low, high)


@dataclass
class SpoofingAssessment:
    """
    What the anti-spoofing stage concluded, and what it could not look at.

    ``score`` alone cannot distinguish "I checked and found nothing" from "I
    could not check", and collapsing those two was the defect: unmeasurable
    inputs produced indicators that fired, penalties that accumulated, and a
    verdict of synthetic speech against the enrolled operator.
    """

    score: float = 1.0
    indicators: List[Tuple[str, float]] = field(default_factory=list)
    abstained: List[str] = field(default_factory=list)

    @property
    def evidence_complete(self) -> bool:
        return not self.abstained

    def describe(self) -> str:
        fired = ", ".join(f"{n}:{p}" for n, p in self.indicators) or "none"
        skipped = ", ".join(self.abstained) or "none"
        return f"score={self.score:.2f} fired=[{fired}] abstained=[{skipped}]"


@dataclass
class PlausibilityAssessment:
    """
    What the physics stage scored, and what it could not look at.

    The twin of :class:`SpoofingAssessment`, deliberately the same shape: both
    stages read raw features, both can be handed quantities nothing measured,
    and both previously turned that into a confident number. Keeping the two
    result types symmetric is what stops one of them being fixed and the other
    quietly regressing — which is exactly what happened between #70426 and this
    change.

    ``score`` alone cannot distinguish "I checked and every component passed"
    from "I could not check anything", so the components that ran are kept by
    name alongside the ones that abstained.
    """

    score: float = 1.0
    components: Dict[str, float] = field(default_factory=dict)
    abstained: List[str] = field(default_factory=list)

    @property
    def evidence_complete(self) -> bool:
        return not self.abstained

    def finalize(self) -> "PlausibilityAssessment":
        """
        Set ``score`` to the mean of the components that actually ran.

        With no component measurable the score is 1.0 — NEUTRAL, not zero. This
        stage is a veto on impossible voices; with no evidence it must veto
        nothing and say so via ``abstained``. Scoring 0.0 here would convert a
        broken feature extractor into a rejection of the speaker, which is the
        precise defect this change exists to remove. The fused verdict is not
        thereby weakened: the embedding comparison is the primary gate and a
        failed one already scores 0.0 on its own.
        """
        if self.components:
            self.score = float(np.clip(np.mean(list(self.components.values())), 0.0, 1.0))
        else:
            self.score = 1.0
        return self

    def describe(self) -> str:
        ran = ", ".join(f"{n}:{v:.2f}" for n, v in self.components.items()) or "none"
        skipped = ", ".join(self.abstained) or "none"
        return f"score={self.score:.2f} scored=[{ran}] abstained=[{skipped}]"


@dataclass
class AntiSpoofingMetrics:
    """Anti-spoofing detection metrics"""

    is_live: bool
    is_human: bool
    is_original: bool

    replay_score: float
    synthesis_score: float
    voice_conversion_score: float

    microphone_consistency: float
    acoustic_environment_score: float

    confidence: float
    suspicious_indicators: List[str] = field(default_factory=list)


@dataclass
class VerificationResult:
    """Comprehensive verification result"""

    # Basic result
    verified: bool
    confidence: float
    threshold: float

    # Detailed scores
    embedding_similarity: float
    mahalanobis_distance: float
    acoustic_match_score: float
    physics_plausibility: float
    anti_spoofing_score: float

    # Bayesian analysis
    posterior_probability: float
    uncertainty: float
    confidence_interval: Tuple[float, float]

    # Multi-modal fusion
    fusion_weights: Dict[str, float]
    feature_contributions: Dict[str, float]

    # Decision factors
    decision_factors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    # Metadata
    processing_time_ms: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)


# ═══════════════════════════════════════════════════════════════════════════════
# ADVANCED BIOMETRIC VERIFICATION ENGINE
# ═══════════════════════════════════════════════════════════════════════════════


class AdvancedBiometricVerifier:
    """
    🧠 BEAST MODE Biometric Verification Engine

    Combines multiple advanced techniques:
    1. Probabilistic Bayesian verification
    2. Mahalanobis distance with adaptive covariance
    3. Multi-modal biometric fusion
    4. Physics-based plausibility checking
    5. Anti-spoofing detection
    6. Adaptive threshold learning

    All CPU-intensive work runs in thread pool to prevent blocking.
    """

    def __init__(
        self,
        learning_db=None,
        enable_adaptive_learning: bool = True,
        enable_anti_spoofing: bool = True
    ):
        self.learning_db = learning_db
        self.enable_adaptive_learning = enable_adaptive_learning
        self.enable_anti_spoofing = enable_anti_spoofing
        self._executor = get_biometric_executor()

        # Adaptive parameters (learned over time, no hardcoding!)
        self.speaker_models: Dict[str, "SpeakerModel"] = {}

        # Physics constraints — every band overridable per deployment.
        self.physics = PhysicsConstraints.from_env()

        # Anti-spoofing trip points. Read from the environment rather than
        # written into the checks, so a mic or codec that shifts the noise
        # floor can be accommodated without editing a security decision.
        self._replay_max_snr_db = _env_float("JARVIS_ANTISPOOF_MAX_SNR_DB", 50.0)
        self._replay_min_noise = _env_float("JARVIS_ANTISPOOF_MIN_BACKGROUND", 0.001)
        self._replay_max_quality = _env_float("JARVIS_ANTISPOOF_MAX_QUALITY", 0.95)
        self._synthesis_min_pitch_std = _env_float("JARVIS_ANTISPOOF_MIN_PITCH_STD", 5.0)
        self._synthesis_max_hnr_db = _env_float("JARVIS_ANTISPOOF_MAX_HNR_DB", 40.0)
        self._conversion_max_rate_diff = _env_float("JARVIS_ANTISPOOF_MAX_RATE_DIFF", 100.0)

        # Performance tracking
        self.verification_history: List[VerificationResult] = []
        self.false_rejection_rate = 0.0
        self.false_acceptance_rate = 0.0

        logger.info("🚀 Advanced Biometric Verifier initialized (Beast Mode - Async Optimized)")

    async def _run_in_executor(self, func, *args, **kwargs) -> Any:
        """Run a CPU-intensive function in the thread pool."""
        loop = asyncio.get_running_loop()
        if kwargs:
            func = partial(func, **kwargs)
        return await loop.run_in_executor(self._executor, func, *args)

    async def verify_speaker(
        self,
        test_features: VoiceBiometricFeatures,
        enrolled_features: VoiceBiometricFeatures,
        speaker_name: str,
        context: Optional[Dict] = None
    ) -> VerificationResult:
        """
        Advanced multi-modal biometric verification

        Args:
            test_features: Features from test audio
            enrolled_features: Enrolled speaker features
            speaker_name: Speaker identifier
            context: Optional context (time of day, environment, etc.)

        Returns:
            Comprehensive verification result
        """
        start_time = datetime.now()

        # Get or create speaker model
        speaker_model = await self._get_speaker_model(speaker_name, enrolled_features)

        # Run all verification stages in parallel using thread pool
        # This prevents blocking and allows true parallelism
        results = await asyncio.gather(
            self._compute_embedding_similarity(
                test_features.embedding,
                enrolled_features.embedding,
                speaker_model
            ),
            self._compute_mahalanobis_distance(
                test_features,
                enrolled_features,
                speaker_model
            ),
            self._compute_acoustic_match(
                test_features,
                enrolled_features,
                speaker_model
            ),
            self._check_physics_plausibility(
                test_features
            ),
            return_exceptions=True
        )

        # Unpack results with error handling
        # 0.0, NOT 0.5. Stage 0 is the primary biometric comparison; if it
        # could not be computed there is no evidence this is the speaker, and a
        # neutral 0.5 would carry a FAILED comparison into the fused score as
        # though it were a middling match. A fault must not become a verdict —
        # the same defect this chain has produced at every other layer.
        embedding_sim = float(results[0]) if not isinstance(results[0], BaseException) else 0.0
        mahal_distance = float(results[1]) if not isinstance(results[1], BaseException) else 0.5
        acoustic_score = float(results[2]) if not isinstance(results[2], BaseException) else 0.5

        # Physics returns an assessment, not a bare number: the score alone
        # cannot say which components were unmeasurable, and that distinction is
        # the whole point of the stage. A raised exception yields an assessment
        # that abstained on everything — neutral score, evidence incomplete —
        # rather than the old bare 0.8, which was a fabricated near-pass.
        if isinstance(results[3], BaseException):
            physics = PlausibilityAssessment(
                abstained=[f"stage_failed({type(results[3]).__name__})"]).finalize()
        else:
            physics = results[3]
        physics_score = physics.score

        # Log any exceptions
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.warning(f"Verification stage {i} failed: {r}")

        # Stage 5: Anti-spoofing detection
        spoof = SpoofingAssessment()
        if self.enable_anti_spoofing:
            spoof = await self._detect_spoofing(
                test_features,
                enrolled_features,
                context
            )
        spoofing_score = spoof.score

        # Stage 6: Multi-modal fusion with dynamic weights
        fusion_weights = await self._compute_fusion_weights(
            speaker_model,
            context
        )

        # Stage 7: Bayesian verification with uncertainty
        posterior_prob, uncertainty = await self._bayesian_verification(
            embedding_sim=embedding_sim,
            mahal_distance=mahal_distance,
            acoustic_score=acoustic_score,
            physics_score=physics_score,
            spoofing_score=spoofing_score,
            fusion_weights=fusion_weights,
            speaker_model=speaker_model
        )

        # Stage 8: Adaptive threshold decision
        threshold = await self._get_adaptive_threshold(
            speaker_model,
            context,
            uncertainty
        )

        verified = posterior_prob >= threshold

        # Stage 9: Confidence interval
        confidence_interval = await self._compute_confidence_interval(
            posterior_prob,
            uncertainty
        )

        # Collect decision factors
        decision_factors = []
        feature_contributions = {}

        if embedding_sim * fusion_weights.get('embedding', 0.4) > 0.2:
            decision_factors.append(f"Strong embedding match ({embedding_sim:.1%})")
            feature_contributions['embedding'] = embedding_sim * fusion_weights['embedding']

        if acoustic_score * fusion_weights.get('acoustic', 0.3) > 0.15:
            decision_factors.append(f"Acoustic features match ({acoustic_score:.1%})")
            feature_contributions['acoustic'] = acoustic_score * fusion_weights['acoustic']

        if physics_score < 0.8:
            decision_factors.append(f"⚠️  Physics plausibility low ({physics_score:.1%})")
        feature_contributions['physics'] = physics_score * fusion_weights.get('physics', 0.1)

        if spoofing_score < 0.9:
            decision_factors.append(f"⚠️  Possible spoofing detected ({spoofing_score:.1%})")
        feature_contributions['anti_spoofing'] = spoofing_score * fusion_weights.get('spoofing', 0.2)

        # Warnings
        warnings = []
        if uncertainty > 0.3:
            warnings.append(f"High uncertainty ({uncertainty:.1%})")
        if physics_score < 0.7:
            warnings.append("Voice physics constraints violated")
        if not physics.evidence_complete:
            # Symmetric with the anti-spoofing warning below, and for the same
            # reason: a plausibility score reached by skipping components is not
            # the same claim as one reached by passing them.
            warnings.append(
                f"Physics evidence incomplete: {len(physics.abstained)} "
                f"component(s) abstained ({'; '.join(physics.abstained)})"
            )
        if spoofing_score < 0.8:
            warnings.append("Spoofing indicators detected")
        if not spoof.evidence_complete:
            # A clean anti-spoofing score obtained by not looking is not a
            # clean score. Say so, at the same level as a positive finding.
            warnings.append(
                f"Anti-spoofing evidence incomplete: {len(spoof.abstained)} "
                f"check(s) abstained ({'; '.join(spoof.abstained)})"
            )

        # Update speaker model (adaptive learning)
        if self.enable_adaptive_learning and verified:
            await self._update_speaker_model(
                speaker_model,
                test_features,
                posterior_prob
            )

        processing_time = (datetime.now() - start_time).total_seconds() * 1000

        result = VerificationResult(
            verified=verified,
            confidence=posterior_prob,
            threshold=threshold,
            embedding_similarity=embedding_sim,
            mahalanobis_distance=mahal_distance,
            acoustic_match_score=acoustic_score,
            physics_plausibility=physics_score,
            anti_spoofing_score=spoofing_score,
            posterior_probability=posterior_prob,
            uncertainty=uncertainty,
            confidence_interval=confidence_interval,
            fusion_weights=fusion_weights,
            feature_contributions=feature_contributions,
            decision_factors=decision_factors,
            warnings=warnings,
            processing_time_ms=processing_time
        )

        # Track verification
        self.verification_history.append(result)
        await self._update_performance_metrics(result)

        logger.info(
            f"🎯 Verification: {speaker_name} | "
            f"Verified: {verified} | "
            f"Confidence: {posterior_prob:.1%} ± {uncertainty:.1%} | "
            f"Threshold: {threshold:.1%} | "
            f"Time: {processing_time:.1f}ms"
        )

        return result

    # =========================================================================
    # ASYNC COMPUTATION METHODS - Run CPU work in thread pool
    # =========================================================================

    async def _compute_embedding_similarity(
        self,
        test_emb: np.ndarray,
        enrolled_emb: np.ndarray,
        speaker_model: "SpeakerModel"
    ) -> float:
        """Compute embedding similarity with multiple metrics (thread pool)."""
        return await self._run_in_executor(
            self._compute_embedding_similarity_sync,
            test_emb, enrolled_emb, speaker_model
        )

    @staticmethod
    def _as_similarity_vector(embedding) -> "Optional[np.ndarray]":
        """Coerce an embedding to a flat float32 vector, or None.

        This used to re-implement `SpeechBrainEngine._as_host_vector` here, on
        the argument that importing the engine would pull torch, speechbrain and
        the model registry into a verification path that needs none of them. The
        argument was sound; the conclusion — copy the coercion — was not, and it
        is why a `list` (the one form `get_all_speaker_profiles` actually
        returns) fell through every branch of the third copy on 2026-08-06.

        The follow-up this docstring named is now done: `voice/embedding_ops`
        exists, costs nothing but numpy, and is the single answer all three
        layers call. dtype and device are representation differences and are
        cast; shape is not, and remains the caller's to reject — which is why
        this asks for no `expected_dim`.
        """
        return coerce_vector(embedding)

    def _compute_embedding_similarity_sync(
        self,
        test_emb: np.ndarray,
        enrolled_emb: np.ndarray,
        speaker_model: "SpeakerModel"
    ) -> float:
        """Sync embedding similarity computation."""
        # SHAPE FIRST. `np.dot` of a (1, 192) encoder output against a (192,)
        # stored profile yields a SIZE-1 1-D array, not a scalar — and
        # `float()` on that raises under numpy 2.x:
        #
        #     Verification stage 0 failed: only 0-dimensional arrays can be
        #     converted to Python scalars
        #
        # measured 2026-08-06 23:22:17. The function assumed 1-D vectors and
        # never enforced it; ECAPA emits (1, 192) and SQLite returns (192,).
        # Bound to new names rather than reassigned: the parameters are declared
        # non-optional, and rebinding them to an Optional makes a type checker
        # treat the None guard below as dead code — a warning that would sit in
        # the file forever right next to the guard it wrongly indicts.
        test_vec = self._as_similarity_vector(test_emb)
        enrolled_vec = self._as_similarity_vector(enrolled_emb)
        if test_vec is None or enrolled_vec is None:
            logger.error(
                "Embedding similarity: an embedding could not be coerced to a "
                "vector — refusing to score"
            )
            return 0.0

        # A 192D voiceprint against a differently-shaped vector is NOT a weak
        # match, it is not a comparison. Computing anyway would produce a
        # plausible number for arithmetic that never validly happened.
        if test_vec.shape[0] != enrolled_vec.shape[0]:
            logger.error(
                "Embedding similarity: ShapeMismatch %dD vs %dD — the live "
                "embedding came from a different encoder than the enrolled "
                "voiceprint. Refusing to score.",
                test_vec.shape[0], enrolled_vec.shape[0],
            )
            return 0.0

        # Cosine similarity (fast, baseline)
        test_norm = np.linalg.norm(test_vec)
        enrolled_norm = np.linalg.norm(enrolled_vec)

        if test_norm == 0 or enrolled_norm == 0:
            return 0.0

        cosine_sim = np.dot(test_vec, enrolled_vec) / (test_norm * enrolled_norm)

        # Euclidean distance (normalized)
        euclidean_dist = np.linalg.norm(test_vec - enrolled_vec)
        euclidean_sim = 1.0 / (1.0 + euclidean_dist)

        # Weighted combination (learned)
        weight_cosine = speaker_model.metric_weights.get('cosine', 0.7)
        weight_euclidean = speaker_model.metric_weights.get('euclidean', 0.3)

        similarity = (
            weight_cosine * cosine_sim +
            weight_euclidean * euclidean_sim
        )

        return float(np.clip(similarity, 0.0, 1.0))

    async def _compute_mahalanobis_distance(
        self,
        test_features: VoiceBiometricFeatures,
        enrolled_features: VoiceBiometricFeatures,
        speaker_model: "SpeakerModel"
    ) -> float:
        """Compute Mahalanobis distance with adaptive covariance (thread pool)."""
        return await self._run_in_executor(
            self._compute_mahalanobis_distance_sync,
            test_features, enrolled_features, speaker_model
        )

    def _compute_mahalanobis_distance_sync(
        self,
        test_features: VoiceBiometricFeatures,
        enrolled_features: VoiceBiometricFeatures,
        speaker_model: "SpeakerModel"
    ) -> float:
        """Sync Mahalanobis distance computation."""
        try:
            # Extract feature vector
            test_vector = self._features_to_vector(test_features)
            enrolled_vector = self._features_to_vector(enrolled_features)

            # Get adaptive covariance matrix
            cov_matrix = speaker_model.covariance_matrix

            # Compute Mahalanobis distance
            if cov_matrix is not None and np.linalg.det(cov_matrix) > 1e-10:
                distance = mahalanobis(test_vector, enrolled_vector, np.linalg.inv(cov_matrix))
            else:
                # Fallback to Euclidean if covariance unavailable
                distance = np.linalg.norm(test_vector - enrolled_vector)

            # Convert to similarity score (0-1)
            similarity = np.exp(-distance / speaker_model.mahalanobis_scale)

            return float(np.clip(similarity, 0.0, 1.0))

        except Exception as e:
            logger.warning(f"Mahalanobis distance failed: {e}, using fallback")
            test_vector = self._features_to_vector(test_features)
            enrolled_vector = self._features_to_vector(enrolled_features)
            distance = np.linalg.norm(test_vector - enrolled_vector)
            return float(np.exp(-distance))

    async def _compute_acoustic_match(
        self,
        test_features: VoiceBiometricFeatures,
        enrolled_features: VoiceBiometricFeatures,
        speaker_model: "SpeakerModel"
    ) -> float:
        """Compute acoustic feature matching score (thread pool)."""
        return await self._run_in_executor(
            self._compute_acoustic_match_sync,
            test_features, enrolled_features, speaker_model
        )

    def _compute_acoustic_match_sync(
        self,
        test_features: VoiceBiometricFeatures,
        enrolled_features: VoiceBiometricFeatures,
        speaker_model: "SpeakerModel"
    ) -> float:
        """Sync acoustic matching computation."""
        scores = []

        # Pitch matching (with tolerance for natural variation)
        pitch_diff = abs(test_features.pitch_mean - enrolled_features.pitch_mean)
        pitch_tolerance = speaker_model.pitch_std * 2.0
        pitch_score = np.exp(-pitch_diff / max(pitch_tolerance, 10.0))
        scores.append(pitch_score)

        # Formant matching (speaker-specific resonances)
        formant_diffs = [
            abs(test_features.formant_f1 - enrolled_features.formant_f1),
            abs(test_features.formant_f2 - enrolled_features.formant_f2),
            abs(test_features.formant_f3 - enrolled_features.formant_f3)
        ]
        formant_score = np.mean([np.exp(-diff / 200.0) for diff in formant_diffs])
        scores.append(formant_score)

        # Spectral matching
        spectral_diff = abs(test_features.spectral_centroid - enrolled_features.spectral_centroid)
        spectral_score = np.exp(-spectral_diff / 1000.0)
        scores.append(spectral_score)

        # Speaking rate (temporal characteristic)
        rate_diff = abs(test_features.speaking_rate - enrolled_features.speaking_rate)
        rate_score = np.exp(-rate_diff / 50.0)
        scores.append(rate_score)

        # Weighted average (learned weights)
        weights = speaker_model.acoustic_weights
        acoustic_score = np.average(scores, weights=weights)

        return float(np.clip(acoustic_score, 0.0, 1.0))

    async def _check_physics_plausibility(
        self,
        features: VoiceBiometricFeatures
    ) -> "PlausibilityAssessment":
        """Check if voice features are physically plausible (thread pool)."""
        return await self._run_in_executor(
            self._check_physics_plausibility_sync,
            features
        )

    def _check_physics_plausibility_sync(
        self,
        features: VoiceBiometricFeatures
    ) -> "PlausibilityAssessment":
        """
        Sync physics plausibility check — a component scores only on a measurement.

        This is the anti-spoofing stage's twin, and carried the identical defect
        after that one was fixed. Every component below reads a feature and
        compares it to a threshold; the comparison is arithmetically valid even
        when nothing measured the feature, which is exactly the trap:

          * an unset 0.0 formant gives ``0.0 / max(0.0, 1.0) = 0.0``, falls
            outside the F1/F2 ratio band, and scores that component **0.5**;
          * an unset 0.0 HNR gives ``0.0 / 5.0`` and scores it **0.0**.

        Two of five components pinned low by quantities nobody looked at, then
        averaged. On 2026-08-06 that produced ``physics_score < 0.7`` and the
        warning "Voice physics constraints violated" alongside the spoofing
        finding, against the enrolled operator's own voice.

        So each component is gated on its input being *measurable*, and a
        component whose input was not measured ABSTAINS: it is excluded from the
        mean entirely rather than contributing a low score, and it is recorded
        by name. Excluding rather than substituting a neutral 1.0 matters — a
        neutral value still dilutes a genuine finding from a component that did
        run, which would weaken the check exactly when evidence is thinnest.

        Measurability is not typicality, and the two bands are deliberately
        different. ``pitch_mean`` is *measurable* over 50-500 Hz while the
        typical band scored here is 85-255 Hz: a genuine 260 Hz speaker is a
        real measurement that scores below 1.0, not missing evidence. Narrowing
        the first band to the second would silently discard real speakers, and
        widening the second to the first would stop the check discriminating.

        Abstaining costs nothing in security terms. Physics plausibility is a
        veto on *impossible* voices, not a proxy for extractor health, and the
        primary gate is untouched: a failed embedding comparison already scores
        0.0 and cannot be rescued here.
        """
        physics = self.physics
        assessment = PlausibilityAssessment()

        def scores(name: str, value: float) -> None:
            assessment.components[name] = float(np.clip(value, 0.0, 1.0))

        def abstain(name: str, why: str) -> None:
            assessment.abstained.append(f"{name}({why})")

        # 1. Pitch — typical-range scoring, gated on a measurable f0.
        if physics.measured(features.pitch_mean,
                            physics.min_pitch_hz_measurable,
                            physics.max_pitch_hz_measurable):
            if physics.min_pitch_male <= features.pitch_mean <= physics.max_pitch_female:
                scores("pitch", 1.0)
            elif features.pitch_mean < physics.min_pitch_male:
                deviation = (physics.min_pitch_male - features.pitch_mean) / physics.min_pitch_male
                scores("pitch", float(np.exp(-deviation * 5.0)))
            else:
                deviation = (features.pitch_mean - physics.max_pitch_female) / physics.max_pitch_female
                scores("pitch", float(np.exp(-deviation * 5.0)))
        else:
            abstain("pitch", "pitch_mean unmeasured")

        # 2. Formant relationship — needs BOTH formants, and the ratio is only
        #    meaningful when each is a formant. The old ``max(f2, 1.0)`` guard
        #    protected the division and nothing else: it turned an unmeasured
        #    pair into the well-defined, entirely fictional ratio 0.0.
        f1_ok = physics.measured(features.formant_f1,
                                 physics.min_formant_f1_hz, physics.max_formant_f1_hz)
        f2_ok = physics.measured(features.formant_f2,
                                 physics.min_formant_f2_hz, physics.max_formant_f2_hz)
        if f1_ok and f2_ok:
            ratio = features.formant_f1 / features.formant_f2
            in_band = physics.f1_f2_min_ratio <= ratio <= physics.f1_f2_max_ratio
            scores("formants", 1.0 if in_band else 0.5)
        else:
            missing = "F1" if not f1_ok else ""
            missing += ("+" if missing and not f2_ok else "") + ("F2" if not f2_ok else "")
            abstain("formants", f"{missing} outside human range")

        # 3. Harmonic-to-Noise Ratio — measurability floor, not the plausibility
        #    floor. A real 3 dB capture is noisy evidence, not absent evidence.
        if physics.measured(features.harmonic_to_noise_ratio,
                            physics.min_hnr_db_measurable, physics.max_hnr_db):
            hnr = features.harmonic_to_noise_ratio
            if hnr >= physics.min_hnr_db:
                scores("hnr", 1.0)
            else:
                # Below the plausibility floor the score falls off toward zero.
                # ``max(min_hnr_db, ε)`` guards a zeroed override, not a missing
                # measurement — that case abstained above.
                scores("hnr", hnr / max(physics.min_hnr_db, 1e-6))
        else:
            abstain("hnr", "harmonic_to_noise_ratio unmeasured")

        # 4/5. Jitter and shimmer — perturbation fractions. Zero IS a legitimate
        #      reading here (a perfectly steady synthetic tone is 0.0 jitter and
        #      that is a finding), so these bands admit it and only a non-finite
        #      or out-of-range value abstains.
        jitter_bound = BOUNDS["jitter"]
        if physics.measured(features.jitter, jitter_bound.low, jitter_bound.high):
            if features.jitter <= physics.max_jitter:
                scores("jitter", 1.0)
            else:
                scores("jitter", physics.max_jitter / features.jitter)
        else:
            abstain("jitter", "jitter unmeasured")

        shimmer_bound = BOUNDS["shimmer"]
        if physics.measured(features.shimmer, shimmer_bound.low, shimmer_bound.high):
            if features.shimmer <= physics.max_shimmer:
                scores("shimmer", 1.0)
            else:
                scores("shimmer", physics.max_shimmer / features.shimmer)
        else:
            abstain("shimmer", "shimmer unmeasured")

        assessment.finalize()

        if assessment.abstained:
            # Never silent. A plausibility score obtained by not looking is not
            # a plausibility score, and the operator is entitled to know which
            # components did not run.
            logger.warning(
                "[Physics] %d component(s) ABSTAINED — inputs were not "
                "measurable, so they were excluded from the mean rather than "
                "scored: %s", len(assessment.abstained), assessment.describe(),
            )

        return assessment

    async def _detect_spoofing(
        self,
        test_features: VoiceBiometricFeatures,
        enrolled_features: VoiceBiometricFeatures,
        context: Optional[Dict]
    ) -> SpoofingAssessment:
        """Detect spoofing attacks (thread pool)."""
        return await self._run_in_executor(
            self._detect_spoofing_sync,
            test_features, enrolled_features, context
        )

    def _detect_spoofing_sync(
        self,
        test_features: VoiceBiometricFeatures,
        enrolled_features: VoiceBiometricFeatures,
        context: Optional[Dict]
    ) -> SpoofingAssessment:
        """
        Sync spoofing detection — an indicator fires only on a real measurement.

        Every check below reads a feature and compares it to a bound. If the
        feature was never measured, the comparison is still arithmetically
        valid and still produces a boolean, which is exactly the trap: an
        unset 0.0 formant gives ``0.0/1.0 = 0.0``, trips ``< 0.1``, and lands a
        0.5 penalty labelled "unnatural_formants" — a confident statement about
        a voice nobody looked at.

        Measured on 2026-08-06 against the enrolled operator: formants
        42.9/80.0 Hz and an enrolled speaking rate of 420 wpm produced
        ``[('unnatural_formants', 0.5), ('inconsistent_rate', 0.2)]``, a
        spoofing score of 0.3, and "That didn't sound like you" for the person
        the profile belongs to.

        So each check is gated on its inputs being inside the physically
        possible band for that quantity. Outside it, the check ABSTAINS: it
        contributes no penalty and is recorded by name, because a detector that
        silently skips work is as dishonest as one that invents findings.

        Abstaining costs nothing in security terms. Anti-spoofing is a veto on
        POSITIVE evidence of an attack, not a proxy for extractor health, and
        the primary biometric gate is untouched — a failed embedding comparison
        already scores 0.0 and cannot be rescued by this stage.
        """
        assessment = SpoofingAssessment()
        physics = self.physics

        def fires(name: str, penalty: float) -> None:
            assessment.indicators.append((name, penalty))

        def abstain(name: str, why: str) -> None:
            assessment.abstained.append(f"{name}({why})")

        # 1. Replay attack detection — only when quality is a shape we can read.
        if context and 'audio_quality' in context:
            quality = context['audio_quality']
            if isinstance(quality, dict):
                snr = quality.get('snr_db')
                if isinstance(snr, (int, float)) and math.isfinite(snr):
                    if snr > self._replay_max_snr_db:
                        fires("perfect_quality", 0.3)
                else:
                    abstain("perfect_quality", "snr_db unmeasured")
                noise = quality.get('background_noise')
                if isinstance(noise, (int, float)) and math.isfinite(noise):
                    if noise < self._replay_min_noise:
                        fires("no_background", 0.2)
                else:
                    abstain("no_background", "background_noise unmeasured")
            elif isinstance(quality, (int, float)) and math.isfinite(quality):
                if quality > self._replay_max_quality:
                    fires("perfect_quality", 0.2)
            else:
                # A string, as seen in the field ("Invalid quality score type:
                # <class 'str'>") — not a number, so not evidence either way.
                abstain("perfect_quality", f"quality is {type(quality).__name__}")

        # 2. Synthesis detection
        if physics.measured(test_features.pitch_std,
                            physics.min_pitch_std_hz, physics.max_pitch_std_hz):
            if test_features.pitch_std < self._synthesis_min_pitch_std:
                fires("low_pitch_variation", 0.4)
        else:
            abstain("low_pitch_variation", "pitch_std unmeasured")

        f1_ok = physics.measured(test_features.formant_f1,
                                 physics.min_formant_f1_hz, physics.max_formant_f1_hz)
        f2_ok = physics.measured(test_features.formant_f2,
                                 physics.min_formant_f2_hz, physics.max_formant_f2_hz)
        if f1_ok and f2_ok:
            ratio = test_features.formant_f1 / test_features.formant_f2
            if ratio < physics.f1_f2_min_ratio or ratio > physics.f1_f2_max_ratio:
                fires("unnatural_formants", 0.5)
        else:
            missing = "F1" if not f1_ok else ""
            missing += ("+" if missing and not f2_ok else "") + ("F2" if not f2_ok else "")
            abstain("unnatural_formants", f"{missing} outside human range")

        if physics.measured(test_features.harmonic_to_noise_ratio,
                            physics.min_hnr_db, physics.max_hnr_db):
            if test_features.harmonic_to_noise_ratio > self._synthesis_max_hnr_db:
                fires("perfect_harmonics", 0.3)
        else:
            abstain("perfect_harmonics", "HNR unmeasured")

        # 3. Voice conversion detection — needs BOTH sides to be real rates.
        # The enrolled side is the one that failed here: a stored 420 wpm is
        # not a speaking rate, and differencing against it guarantees a miss
        # for the very speaker the profile describes.
        test_rate_ok = physics.measured(
            test_features.speaking_rate,
            physics.min_speaking_rate_wpm, physics.max_speaking_rate_wpm)
        enrolled_rate_ok = physics.measured(
            enrolled_features.speaking_rate,
            physics.min_speaking_rate_wpm, physics.max_speaking_rate_wpm)
        if test_rate_ok and enrolled_rate_ok:
            if abs(test_features.speaking_rate
                   - enrolled_features.speaking_rate) > self._conversion_max_rate_diff:
                fires("inconsistent_rate", 0.2)
        else:
            side = "test" if not test_rate_ok else "enrolled"
            abstain("inconsistent_rate", f"{side} speaking_rate implausible")

        total_penalty = sum(penalty for _, penalty in assessment.indicators)
        assessment.score = float(max(0.0, 1.0 - total_penalty))

        if assessment.indicators:
            logger.warning("⚠️  Spoofing indicators detected: %s", assessment.indicators)
        if assessment.abstained:
            # Never silent. A check that did not run is a hole in the evidence
            # and the operator is entitled to know which one.
            logger.warning(
                "[AntiSpoof] %d check(s) ABSTAINED — inputs were not measurable, "
                "so they contributed no penalty: %s",
                len(assessment.abstained), assessment.describe(),
            )

        return assessment

    async def _owner_aware_antispoof_fusion(
        self,
        owner_match_score: float,
        spoof_prob: float,
        is_owner: bool,
        speaker_model: "SpeakerModel",
        embedding_sim: float,
        acoustic_score: float,
        physics_score: float
    ) -> Tuple[float, str, Dict[str, any]]:
        """Owner-aware anti-spoof fusion (thread pool)."""
        return await self._run_in_executor(
            self._owner_aware_antispoof_fusion_sync,
            owner_match_score, spoof_prob, is_owner, speaker_model,
            embedding_sim, acoustic_score, physics_score
        )

    def _owner_aware_antispoof_fusion_sync(
        self,
        owner_match_score: float,
        spoof_prob: float,
        is_owner: bool,
        speaker_model: "SpeakerModel",
        embedding_sim: float,
        acoustic_score: float,
        physics_score: float
    ) -> Tuple[float, str, Dict[str, any]]:
        """Sync owner-aware anti-spoof fusion."""
        OWNER_STRONG_MATCH_THRESHOLD = speaker_model.owner_strong_threshold
        OWNER_OVERRIDABLE_SPOOF_LIMIT = speaker_model.spoof_override_limit
        BASE_UNLOCK_THRESHOLD = speaker_model.decision_threshold

        if is_owner:
            OWNER_WEIGHT = 0.75
            LIVE_SPEECH_WEIGHT = 0.25
        else:
            OWNER_WEIGHT = 0.50
            LIVE_SPEECH_WEIGHT = 0.50

        live_speech_score = 1.0 - spoof_prob

        base_auth_score = (
            owner_match_score * OWNER_WEIGHT +
            live_speech_score * LIVE_SPEECH_WEIGHT
        )
        base_auth_score = np.clip(base_auth_score, 0.0, 1.0)

        final_auth_score = base_auth_score
        decision = "deny"
        rule_applied = "unknown"
        confidence_boost = 0.0

        if is_owner and owner_match_score >= OWNER_STRONG_MATCH_THRESHOLD:
            rule_applied = "strong_owner_match"

            if spoof_prob >= OWNER_OVERRIDABLE_SPOOF_LIMIT:
                decision = "deny"
                rule_applied = "extreme_spoof_attack"
            else:
                decision = "allow"
                identity_confidence = (owner_match_score - OWNER_STRONG_MATCH_THRESHOLD) / (1.0 - OWNER_STRONG_MATCH_THRESHOLD)
                confidence_boost = 0.10 + (identity_confidence * 0.15)
                minimum_score = BASE_UNLOCK_THRESHOLD + confidence_boost
                final_auth_score = max(base_auth_score, minimum_score)
                final_auth_score = np.clip(final_auth_score, 0.0, 1.0)

        elif is_owner and owner_match_score < OWNER_STRONG_MATCH_THRESHOLD:
            rule_applied = "weak_owner_match"
            adjusted_threshold = BASE_UNLOCK_THRESHOLD - 0.05

            if final_auth_score >= adjusted_threshold and spoof_prob < 0.75:
                decision = "allow"
            else:
                decision = "deny"

        else:
            rule_applied = "unknown_speaker"

            if spoof_prob >= 0.80:
                decision = "deny"
                rule_applied = "unknown_speaker_spoofed"
            elif final_auth_score >= BASE_UNLOCK_THRESHOLD and spoof_prob < 0.80:
                decision = "allow"
            else:
                decision = "deny"

        debug_info = {
            "owner_match_score": float(owner_match_score),
            "spoof_prob": float(spoof_prob),
            "live_speech_score": float(live_speech_score),
            "is_owner": is_owner,
            "base_auth_score": float(base_auth_score),
            "confidence_boost": float(confidence_boost),
            "final_auth_score": float(final_auth_score),
            "decision": decision,
            "rule_applied": rule_applied,
            "embedding_sim": float(embedding_sim),
            "acoustic_score": float(acoustic_score),
            "physics_score": float(physics_score),
            "threshold": float(BASE_UNLOCK_THRESHOLD),
            "owner_strong_threshold": float(OWNER_STRONG_MATCH_THRESHOLD),
            "spoof_override_limit": float(OWNER_OVERRIDABLE_SPOOF_LIMIT),
        }

        return final_auth_score, decision, debug_info

    async def _bayesian_verification(
        self,
        embedding_sim: float,
        mahal_distance: float,
        acoustic_score: float,
        physics_score: float,
        spoofing_score: float,
        fusion_weights: Dict[str, float],
        speaker_model: "SpeakerModel"
    ) -> Tuple[float, float]:
        """Bayesian verification with uncertainty (thread pool)."""
        return await self._run_in_executor(
            self._bayesian_verification_sync,
            embedding_sim, mahal_distance, acoustic_score, physics_score,
            spoofing_score, fusion_weights, speaker_model
        )

    def _bayesian_verification_sync(
        self,
        embedding_sim: float,
        mahal_distance: float,
        acoustic_score: float,
        physics_score: float,
        spoofing_score: float,
        fusion_weights: Dict[str, float],
        speaker_model: "SpeakerModel"
    ) -> Tuple[float, float]:
        """Sync Bayesian verification."""
        spoof_prob = 1.0 - spoofing_score
        is_owner = speaker_model.is_primary_owner
        owner_match_score = embedding_sim

        final_auth_score, fusion_decision, fusion_debug = self._owner_aware_antispoof_fusion_sync(
            owner_match_score=owner_match_score,
            spoof_prob=spoof_prob,
            is_owner=is_owner,
            speaker_model=speaker_model,
            embedding_sim=embedding_sim,
            acoustic_score=acoustic_score,
            physics_score=physics_score
        )

        speaker_model.last_fusion_debug = fusion_debug

        distance_from_threshold = abs(final_auth_score - speaker_model.decision_threshold)
        uncertainty = max(0.1, 1.0 - (distance_from_threshold * 2.0))
        uncertainty = np.clip(uncertainty, 0.0, 1.0)

        prior = speaker_model.prior_probability
        likelihoods = []

        if fusion_weights.get('embedding', 0) > 0:
            emb_likelihood = self._score_to_likelihood(embedding_sim, speaker_model.embedding_mean, speaker_model.embedding_std)
            likelihoods.append((emb_likelihood, fusion_weights['embedding']))

        if fusion_weights.get('mahalanobis', 0) > 0:
            mahal_likelihood = self._score_to_likelihood(mahal_distance, 0.8, 0.15)
            likelihoods.append((mahal_likelihood, fusion_weights['mahalanobis']))

        if fusion_weights.get('acoustic', 0) > 0:
            acoustic_likelihood = self._score_to_likelihood(acoustic_score, speaker_model.acoustic_mean, speaker_model.acoustic_std)
            likelihoods.append((acoustic_likelihood, fusion_weights['acoustic']))

        if fusion_weights.get('physics', 0) > 0:
            likelihoods.append((physics_score, fusion_weights['physics']))

        if fusion_weights.get('spoofing', 0) > 0:
            live_speech_score = 1.0 - spoof_prob
            likelihoods.append((live_speech_score, fusion_weights['spoofing']))

        weighted_likelihood = sum(l * w for l, w in likelihoods) / max(sum(w for _, w in likelihoods), 1.0)

        unnormalized_posterior = weighted_likelihood * prior
        impostor_prior = 1.0 - prior
        impostor_likelihood = 1.0 - weighted_likelihood
        normalizer = unnormalized_posterior + (impostor_likelihood * impostor_prior)

        posterior = unnormalized_posterior / max(normalizer, 1e-10)
        posterior = float(np.clip(final_auth_score, 0.0, 1.0))

        return posterior, float(uncertainty)

    def _score_to_likelihood(self, score: float, mean: float, std: float) -> float:
        """Convert similarity score to likelihood using Gaussian."""
        likelihood = stats.norm.pdf(score, loc=mean, scale=max(std, 0.01))
        max_likelihood = stats.norm.pdf(mean, loc=mean, scale=max(std, 0.01))
        return likelihood / max(max_likelihood, 1e-10)

    async def _compute_fusion_weights(
        self,
        speaker_model: "SpeakerModel",
        context: Optional[Dict]
    ) -> Dict[str, float]:
        """Compute dynamic fusion weights based on context."""
        weights = speaker_model.fusion_weights.copy()

        if context:
            if context.get('snr_db', 30) < 15:
                weights['embedding'] *= 1.3
                weights['acoustic'] *= 0.7

            if context.get('snr_db', 30) > 25:
                weights['acoustic'] *= 1.2
                weights['physics'] *= 1.1

        total = sum(weights.values())
        weights = {k: v / total for k, v in weights.items()}

        return weights

    async def _get_adaptive_threshold(
        self,
        speaker_model: "SpeakerModel",
        context: Optional[Dict],
        uncertainty: float
    ) -> float:
        """Compute adaptive threshold."""
        threshold = speaker_model.decision_threshold
        threshold += uncertainty * 0.1

        if context:
            hour = context.get('hour', 12)
            if hour < 6 or hour > 23:
                threshold += 0.05

            if context.get('unusual_location', False):
                threshold += 0.05

        threshold = np.clip(threshold, 0.3, 0.85)

        return float(threshold)

    async def _compute_confidence_interval(
        self,
        posterior: float,
        uncertainty: float,
        confidence_level: float = 0.95
    ) -> Tuple[float, float]:
        """Compute confidence interval for posterior probability."""
        std = uncertainty / 2.0
        z = stats.norm.ppf((1 + confidence_level) / 2)
        margin = z * std
        lower = max(0.0, posterior - margin)
        upper = min(1.0, posterior + margin)

        return (lower, upper)

    def _features_to_vector(self, features: VoiceBiometricFeatures) -> np.ndarray:
        """Convert features to flat vector for distance computation."""
        return np.array([
            features.pitch_mean,
            features.pitch_std,
            features.formant_f1,
            features.formant_f2,
            features.formant_f3,
            features.spectral_centroid,
            features.spectral_rolloff,
            features.speaking_rate,
            features.jitter,
            features.shimmer,
            features.harmonic_to_noise_ratio
        ])

    async def _get_speaker_model(
        self,
        speaker_name: str,
        enrolled_features: VoiceBiometricFeatures
    ) -> "SpeakerModel":
        """Get or create speaker model."""
        if speaker_name not in self.speaker_models:
            self.speaker_models[speaker_name] = SpeakerModel(
                speaker_name=speaker_name,
                enrolled_features=enrolled_features
            )
            logger.info(f"Created new speaker model for {speaker_name}")

        return self.speaker_models[speaker_name]

    async def _update_speaker_model(
        self,
        speaker_model: "SpeakerModel",
        new_features: VoiceBiometricFeatures,
        confidence: float
    ):
        """Update speaker model with new authentic sample (thread pool)."""
        if confidence < 0.7:
            return

        await self._run_in_executor(
            self._update_speaker_model_sync,
            speaker_model, new_features, confidence
        )

    def _update_speaker_model_sync(
        self,
        speaker_model: "SpeakerModel",
        new_features: VoiceBiometricFeatures,
        confidence: float
    ):
        """Sync speaker model update."""
        alpha = 0.1

        if len(new_features.embedding) == len(speaker_model.embedding_samples[0]) if speaker_model.embedding_samples else True:
            speaker_model.embedding_samples.append(new_features.embedding)

            if len(speaker_model.embedding_samples) > 50:
                speaker_model.embedding_samples = speaker_model.embedding_samples[-50:]

            all_embeddings = np.array(speaker_model.embedding_samples)
            if len(all_embeddings) > 1:
                sims = []
                for i, e1 in enumerate(all_embeddings):
                    for e2 in all_embeddings[i+1:]:
                        n1, n2 = np.linalg.norm(e1), np.linalg.norm(e2)
                        if n1 > 0 and n2 > 0:
                            sims.append(np.dot(e1, e2) / (n1 * n2))
                if sims:
                    speaker_model.embedding_mean = np.mean(sims)
                    speaker_model.embedding_std = np.std(sims)

        speaker_model.pitch_mean = (1 - alpha) * speaker_model.pitch_mean + alpha * new_features.pitch_mean
        speaker_model.pitch_std = np.sqrt(
            (1 - alpha) * speaker_model.pitch_std**2 +
            alpha * (new_features.pitch_mean - speaker_model.pitch_mean)**2
        )

        feature_vector = self._features_to_vector(new_features)
        speaker_model.feature_samples.append(feature_vector)

        if len(speaker_model.feature_samples) > 10:
            feature_samples = speaker_model.feature_samples[-50:]
            speaker_model.covariance_matrix = np.cov(np.array(feature_samples).T)

    async def _update_performance_metrics(self, result: VerificationResult):
        """Track performance metrics."""
        if len(self.verification_history) > 1000:
            self.verification_history = self.verification_history[-1000:]

        recent_results = self.verification_history[-100:]

        potential_frr = [r for r in recent_results if not r.verified and r.confidence > 0.5]
        self.false_rejection_rate = len(potential_frr) / max(len(recent_results), 1)

        potential_far = [r for r in recent_results if r.verified and r.confidence < 0.6]
        self.false_acceptance_rate = len(potential_far) / max(len(recent_results), 1)


# ═══════════════════════════════════════════════════════════════════════════════
# SPEAKER MODEL (Adaptive, No Hardcoding)
# ═══════════════════════════════════════════════════════════════════════════════


class SpeakerModel:
    """
    Adaptive speaker model that learns over time.
    No hardcoded thresholds - everything is learned!
    """

    def __init__(self, speaker_name: str, enrolled_features: VoiceBiometricFeatures, is_primary_owner: bool = False):
        self.speaker_name = speaker_name
        self.is_primary_owner = is_primary_owner

        # Embedding statistics
        self.embedding_samples: List[np.ndarray] = [enrolled_features.embedding]
        self.embedding_mean = 0.9
        self.embedding_std = 0.1

        # Acoustic statistics
        self.pitch_mean = enrolled_features.pitch_mean
        self.pitch_std = max(enrolled_features.pitch_std, 10.0)
        self.acoustic_mean = 0.8
        self.acoustic_std = 0.1

        # Covariance matrix
        self.feature_samples: List[np.ndarray] = []
        self.covariance_matrix: Optional[np.ndarray] = None
        self.mahalanobis_scale = 5.0

        # Decision parameters
        self.decision_threshold = 0.45
        self.prior_probability = 0.5

        # Owner-aware parameters
        self.owner_strong_threshold = 0.35
        self.spoof_override_limit = 0.90

        # Fusion weights
        self.fusion_weights = {
            'embedding': 0.40,
            'mahalanobis': 0.20,
            'acoustic': 0.20,
            'physics': 0.10,
            'spoofing': 0.10
        }

        # Metric weights
        self.metric_weights = {
            'cosine': 0.7,
            'euclidean': 0.3
        }

        # Acoustic feature weights
        self.acoustic_weights = [0.3, 0.3, 0.2, 0.2]

        # Performance tracking
        self.verification_count = 0
        self.successful_verifications = 0
        self.last_updated = datetime.now()

        self.last_fusion_debug: Optional[Dict[str, any]] = None

        if is_primary_owner:
            self.owner_strong_threshold = 0.30
            self.decision_threshold = 0.40
            logger.info(f"✅ Speaker model created for PRIMARY OWNER: {speaker_name}")
        else:
            logger.info(f"📝 Speaker model created for speaker: {speaker_name}")

    def get_success_rate(self) -> float:
        """Get historical success rate."""
        if self.verification_count == 0:
            return 0.5
        return self.successful_verifications / self.verification_count

    def adapt_owner_thresholds(self, false_rejection_rate: float, false_acceptance_rate: float):
        """Adaptive learning: adjust owner thresholds based on performance."""
        if not self.is_primary_owner:
            return

        if false_rejection_rate > 0.15:
            self.owner_strong_threshold = max(0.25, self.owner_strong_threshold - 0.02)
            self.decision_threshold = max(0.35, self.decision_threshold - 0.01)
            logger.info(
                f"📉 Adapting thresholds for {self.speaker_name}: "
                f"FRR={false_rejection_rate:.1%} → "
                f"owner_threshold={self.owner_strong_threshold:.2f}"
            )

        elif false_acceptance_rate > 0.05:
            self.owner_strong_threshold = min(0.50, self.owner_strong_threshold + 0.02)
            self.decision_threshold = min(0.50, self.decision_threshold + 0.01)
            self.spoof_override_limit = max(0.85, self.spoof_override_limit - 0.01)
            logger.info(
                f"📈 Tightening security for {self.speaker_name}: "
                f"FAR={false_acceptance_rate:.1%}"
            )
