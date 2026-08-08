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
    from backend.voice.biological_bounds import (
        BOUNDS, UNMEASURED, PerturbationCaps, PerturbationTolerance,
        default_validator, fold_measurement, is_measured,
        renormalized_weighted_mean,
    )
    from backend.voice.embedding_ops import coerce_vector
except ImportError:  # pragma: no cover
    from voice.biological_bounds import (
        BOUNDS, UNMEASURED, PerturbationCaps, PerturbationTolerance,
        default_validator, fold_measurement, is_measured,
        renormalized_weighted_mean,
    )
    from voice.embedding_ops import coerce_vector

#: Shared with the extractors and the sanitiser — the adaptive model must judge
#: an observation by the same bands the writer used, or it will learn from a
#: value the writer would have refused.
_BOUNDS_VALIDATOR = default_validator()

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


#: Smallest representable step. Used where a divisor is derived from configured
#: values that a determined operator could set equal, which would otherwise be a
#: ZeroDivisionError inside a thread pool.
_EPS = float(np.finfo(np.float64).eps)


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
class AcousticAssessment:
    """
    What the acoustic stage compared, and what it could not.

    The third sibling of :class:`SpoofingAssessment` and
    :class:`PlausibilityAssessment`. All three read raw features, all three can
    be handed quantities nothing measured, and all three turned that into a
    confident number until they were fixed one at a time — which is the
    argument for their result types being the same shape. A future reader
    adding a fourth stage should find three identical patterns, not three
    different ones.

    ``weights`` carries the model's learned weight for each component that ran,
    so the mean can renormalise over survivors instead of assuming all four
    contributed.
    """

    score: float = UNMEASURED
    components: Dict[str, float] = field(default_factory=dict)
    weights: Dict[str, float] = field(default_factory=dict)
    abstained: List[str] = field(default_factory=list)
    partial: List[str] = field(default_factory=list)

    @property
    def evidence_complete(self) -> bool:
        return not self.abstained and not self.partial

    @property
    def has_evidence(self) -> bool:
        """True when at least one component actually compared something."""
        return bool(self.components) and is_measured(self.score, 0.0, 1.0)

    def finalize(self) -> "AcousticAssessment":
        """
        Weighted mean over the components that ran, weights renormalised.

        With nothing measurable the score stays ``UNMEASURED`` — deliberately
        NOT a neutral 0.5. This stage must then be DROPPED from the fusion
        rather than contribute a fabricated middle value, and ``has_evidence``
        is how the fusion knows. Naming the absence is what lets the caller do
        the neutral thing; naming a number would force it to do the wrong one.
        """
        contributions = [(score, self.weights.get(name, 1.0))
                         for name, score in self.components.items()]
        self.score = renormalized_weighted_mean(contributions)
        if is_measured(self.score, None, None):
            self.score = float(np.clip(self.score, 0.0, 1.0))
        return self

    def describe(self) -> str:
        ran = ", ".join(f"{n}:{v:.2f}" for n, v in self.components.items()) or "none"
        skipped = ", ".join(self.abstained + self.partial) or "none"
        shown = f"{self.score:.2f}" if is_measured(self.score, None, None) else "unmeasured"
        return f"score={shown} scored=[{ran}] abstained=[{skipped}]"


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
    #: The jitter/shimmer limits this recording was actually held to, so a log
    #: line can say WHICH caps produced a low score rather than leaving the
    #: reader to assume the clinical ones.
    perturbation_caps: Optional[PerturbationCaps] = None

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

        # Perturbation limits scaled to the recording, not to a clinic. The
        # 0.02/0.10 constants this replaces are head-mounted-microphone figures
        # and they penalised the operator's own verified sample by 0.27/0.33.
        self._perturbation = PerturbationTolerance.from_env()

        # Anti-spoofing trip points. Read from the environment rather than
        # written into the checks, so a mic or codec that shifts the noise
        # floor can be accommodated without editing a security decision.
        # How far a degraded capture re-weights the fusion. Applied against the
        # degradation ratio PerturbationTolerance already computes (0 at the
        # clean anchor, 1 at the floor), so these say how MUCH to lean, never at
        # what SNR to start leaning — that anchor has exactly one definition in
        # this file and it is not here.
        self._noise_embedding_gain = _env_float("JARVIS_FUSION_NOISE_EMBEDDING_GAIN", 0.30)
        self._noise_perturbation_loss = _env_float("JARVIS_FUSION_NOISE_PERTURBATION_LOSS", 0.30)

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

        # ── Dead capture ────────────────────────────────────────────────────
        # Refused BEFORE the stages run, because a dead tensor does not fail
        # loudly — it produces plausible-looking numbers. A zero-magnitude
        # embedding gives a 0/0 cosine, and every stage downstream then reports
        # a score computed from silence.
        #
        # macOS TCC makes this a live path rather than a hypothetical. The
        # microphone is gated per-client, and the client at a lock screen is not
        # this process: SecurityAgent and the broker daemon each need their own
        # grant, and a daemon has no UI in which to be asked for one. A denial
        # while locked therefore does not raise — it hands back a buffer of
        # zeros, which is exactly the input this refuses.
        #
        # "Fail secure" and "fail open" are the same action here and it is worth
        # being explicit about why: this returns UNVERIFIED, so no grant is
        # deposited, so the mechanism yields, so loginwindow's own password
        # prompt handles the unlock. Denying the biometric IS opening the door
        # to the normal authenticator. What would fail closed in the dangerous
        # sense is raising — an exception inside a worker thread surfaces as a
        # stage fault, and this chain has substituted fabricated scores for
        # stage faults at every previous layer.
        dead_capture = self._dead_capture_reason(test_features)
        if dead_capture is not None:
            logger.error(
                "🔇 [Verify] no usable capture for %s (%s) — UNVERIFIED. If the "
                "screen is locked, check the microphone grant for SecurityAgent "
                "and %s; a TCC denial returns silence, not an error.",
                speaker_name, dead_capture, "the unlock broker",
            )
            return VerificationResult(
                verified=False,
                confidence=0.0,
                threshold=float(speaker_model.decision_threshold),
                # UNMEASURED, not 0.0. A zero here would read downstream as a
                # comparison that ran and scored nothing, which is the precise
                # confusion between a fault and a verdict this file exists to
                # keep out of the authorization path.
                embedding_similarity=UNMEASURED,
                mahalanobis_distance=UNMEASURED,
                acoustic_match_score=UNMEASURED,
                physics_plausibility=UNMEASURED,
                anti_spoofing_score=UNMEASURED,
                # The decision, by contrast, IS known: nothing was verified.
                posterior_probability=0.0,
                uncertainty=1.0,
                confidence_interval=(0.0, 0.0),
                fusion_weights={},
                feature_contributions={},
                decision_factors=[f"No usable capture: {dead_capture}"],
                warnings=[
                    "Microphone produced no signal; falling through to the "
                    "system password prompt",
                ],
                processing_time_ms=(datetime.now() - start_time).total_seconds() * 1000.0,
            )

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
                test_features,
                context
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

        # Acoustic returns an assessment for the same reason physics does: the
        # score alone cannot say whether any component actually compared
        # anything. A raised stage yields an assessment that abstained on
        # everything rather than the old bare 0.5, which was a fabricated
        # middling match manufactured by a crash.
        if isinstance(results[2], BaseException):
            acoustic = AcousticAssessment(
                abstained=[f"stage_failed({type(results[2]).__name__})"]).finalize()
        else:
            acoustic = results[2]
        acoustic_score = acoustic.score

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

        # Reporting only — none of this reaches the verdict, which
        # _owner_aware_antispoof_fusion_sync decides. It still must not invent
        # numbers or raise.
        #
        # Two defects here. The `.get(name, <literal>)` guards paired with a bare
        # `fusion_weights[name]` on the very next line: the guard admitted the
        # key might be absent and the assignment then indexed it anyway, so an
        # absent weight was a KeyError inside a verification, not a fallback. And
        # the literals themselves were a second, unrelated set of weights that
        # nothing had measured — a stage whose weight went missing was reported
        # as though someone had chosen 0.4 for it.
        #
        # An absent weight now means the contribution is not reported. A
        # contribution computed from a made-up weight is not a smaller
        # measurement; it is a different one.
        def _contribution(name: str, score: float) -> Optional[float]:
            weight = fusion_weights.get(name)
            if not is_measured(weight, 0.0, None) or not is_measured(score, None, None):
                return None
            return float(score) * float(weight)

        _embedding_contribution = _contribution('embedding', embedding_sim)
        if _embedding_contribution is not None and _embedding_contribution > 0.2:
            decision_factors.append(f"Strong embedding match ({embedding_sim:.1%})")
            feature_contributions['embedding'] = _embedding_contribution

        _acoustic_contribution = _contribution('acoustic', acoustic_score)
        if _acoustic_contribution is not None and _acoustic_contribution > 0.15:
            decision_factors.append(f"Acoustic features match ({acoustic_score:.1%})")
            feature_contributions['acoustic'] = _acoustic_contribution

        if is_measured(physics_score, None, None) and physics_score < 0.8:
            decision_factors.append(f"⚠️  Physics plausibility low ({physics_score:.1%})")
        _physics_contribution = _contribution('physics', physics_score)
        if _physics_contribution is not None:
            feature_contributions['physics'] = _physics_contribution

        if is_measured(spoofing_score, None, None) and spoofing_score < 0.9:
            decision_factors.append(f"⚠️  Possible spoofing detected ({spoofing_score:.1%})")
        _spoofing_contribution = _contribution('spoofing', spoofing_score)
        if _spoofing_contribution is not None:
            feature_contributions['anti_spoofing'] = _spoofing_contribution

        # Warnings
        warnings = []
        if uncertainty > 0.3:
            warnings.append(f"High uncertainty ({uncertainty:.1%})")
        if physics_score < 0.7:
            warnings.append("Voice physics constraints violated")
        if not acoustic.has_evidence:
            warnings.append(
                "Acoustic comparison had no evidence: every component abstained "
                f"({'; '.join(acoustic.abstained) or 'no components'}) — the "
                "term was dropped from the fusion rather than scored"
            )
        elif not acoustic.evidence_complete:
            warnings.append(
                f"Acoustic evidence incomplete: {len(acoustic.abstained + acoustic.partial)} "
                f"component(s) reduced ({'; '.join(acoustic.abstained + acoustic.partial)})"
            )
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

        # Escalate anything the fusion guard intercepted. This is the async
        # side of that guard: the fusion runs in a worker thread and cannot
        # await, so it records the anomaly and this drains it.
        await self._escalate_authorization_anomaly(speaker_model, speaker_name)

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

    async def _escalate_authorization_anomaly(
        self,
        speaker_model: "SpeakerModel",
        speaker_name: str,
    ) -> None:
        """
        Hand a non-finite authorization value to the runtime health sensor.

        A NaN reaching the authorization tensor is not a voice problem, it is a
        RUNTIME problem: something upstream produced a value that is not a
        number and every guard between there and here failed to notice. It must
        be visible to the organism that fixes such things, not buried in a log
        line inside a thread pool.

        Uses the sensor's existing push entry point — the same one
        ``TaskHarvester`` and ``LoopSentinel`` use — so this adds a reporter,
        not a second sensor. Registration rather than import, so a verifier
        running before the intake layer is up simply finds nothing and moves on.

        NEVER raises. A failure to report a fault must not become a second
        fault on the authorization path, and the verdict has already been
        decided (deny) by the time this runs.
        """
        debug = getattr(speaker_model, "last_fusion_debug", None)
        if not isinstance(debug, dict):
            return
        anomaly = debug.pop("authorization_anomaly", None)
        if not anomaly:
            return

        try:
            try:
                from backend.core.ouroboros.governance.intake.sensors.runtime_health_sensor import (
                    HealthFinding, get_runtime_health_sensor,
                )
            except ImportError:
                from core.ouroboros.governance.intake.sensors.runtime_health_sensor import (  # type: ignore
                    HealthFinding, get_runtime_health_sensor,
                )

            sensor = get_runtime_health_sensor()
            if sensor is None:
                logger.critical(
                    "🚨 [Verify] authorization anomaly with no runtime health "
                    "sensor registered to receive it: %s", anomaly,
                )
                return

            await sensor.report(HealthFinding(
                category="authorization_integrity",
                severity="critical",
                summary=(
                    f"Non-finite value reached the voice authorization tensor "
                    f"({anomaly.get('kind', 'unknown')}) — verification failed "
                    f"closed for '{speaker_name}'"
                ),
                details=dict(anomaly, speaker=speaker_name),
                target_files=("backend/voice/advanced_biometric_verification.py",),
            ))
        except Exception:  # noqa: BLE001 — reporting a fault may not create one
            logger.critical(
                "🚨 [Verify] authorization anomaly could not be reported: %s",
                anomaly, exc_info=True,
            )

    async def _compute_acoustic_match(
        self,
        test_features: VoiceBiometricFeatures,
        enrolled_features: VoiceBiometricFeatures,
        speaker_model: "SpeakerModel"
    ) -> "AcousticAssessment":
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
    ) -> "AcousticAssessment":
        """
        Sync acoustic matching — a component contributes only when BOTH sides
        were measured.

        The third of the three fused stages, and the last to get this. Every
        comparison below is a difference between a test feature and an enrolled
        one; ``abs(NaN - x)`` is NaN, ``exp(-NaN)`` is NaN, and ``np.average``
        of anything containing NaN is NaN. Once the enrolled formants were
        NULLed as biologically impossible, this stage emitted NaN into the
        fusion on every single verification.

        It did not corrupt the verdict, and the reason is worse than if it had:
        the Bayesian block that consumes it discards its own result (see
        ``_bayesian_verification_sync``). The channel is SEVERED, not safe. A
        NaN guard placed only there would guard nothing, and the moment anyone
        reconnects the fusion the bomb arms itself.

        BOTH sides must be measured, not just one. Comparing a live formant
        against an absent enrolled one is not a weak match — it is not a
        comparison, and scoring it produces a number for arithmetic that never
        validly happened. That is the same rule ``_compute_embedding_similarity``
        applies to a dimension mismatch.

        Surviving components are combined with the model's learned weights
        RENORMALISED over them, which is the only mathematically neutral way to
        drop a term: substituting 0.5 would penalise a good match and inflate a
        bad one, and substituting the model's own mean would maximise the
        Gaussian likelihood downstream — a stage that measured nothing vouching
        for the speaker.
        """
        assessment = AcousticAssessment()
        physics = self.physics
        # Learned weights, positionally aligned with the four components below.
        weights = list(getattr(speaker_model, "acoustic_weights", []) or [])
        while len(weights) < 4:
            weights.append(1.0)

        def both_measured(name: str, low: float, high: float) -> bool:
            t = getattr(test_features, name)
            e = getattr(enrolled_features, name)
            return physics.measured(t, low, high) and physics.measured(e, low, high)

        # 1. Pitch — tolerance from the speaker's own learned spread.
        if both_measured("pitch_mean", physics.min_pitch_hz_measurable,
                         physics.max_pitch_hz_measurable):
            pitch_diff = abs(test_features.pitch_mean - enrolled_features.pitch_mean)
            tolerance = speaker_model.pitch_std * 2.0
            if not is_measured(tolerance, 1e-6, None):
                # A model whose learned spread is unusable falls back to the
                # floor the original code used, rather than dividing by NaN.
                tolerance = 10.0
            assessment.components["pitch"] = float(np.exp(-pitch_diff / max(tolerance, 10.0)))
            assessment.weights["pitch"] = weights[0]
        else:
            assessment.abstained.append("pitch(unmeasured on one or both sides)")

        # 2. Formants — per-formant, so one absent formant costs that formant
        #    rather than the whole component. F1+F2 carry most of the speaker
        #    information; requiring all three would abstain far too often.
        formant_scores = []
        missing = []
        for index, (name, low, high) in enumerate((
            ("formant_f1", physics.min_formant_f1_hz, physics.max_formant_f1_hz),
            ("formant_f2", physics.min_formant_f2_hz, physics.max_formant_f2_hz),
            ("formant_f3", BOUNDS["formant_f3_hz"].low, BOUNDS["formant_f3_hz"].high),
        )):
            if both_measured(name, low, high):
                diff = abs(getattr(test_features, name) - getattr(enrolled_features, name))
                formant_scores.append(float(np.exp(-diff / 200.0)))
            else:
                missing.append(f"F{index + 1}")
        if formant_scores:
            assessment.components["formants"] = float(np.mean(formant_scores))
            assessment.weights["formants"] = weights[1]
            if missing:
                assessment.partial.append(f"formants({'+'.join(missing)} unmeasured)")
        else:
            assessment.abstained.append("formants(none measurable on both sides)")

        # 3. Spectral centroid
        if both_measured("spectral_centroid", BOUNDS["spectral_centroid_hz"].low,
                         BOUNDS["spectral_centroid_hz"].high):
            diff = abs(test_features.spectral_centroid - enrolled_features.spectral_centroid)
            assessment.components["spectral"] = float(np.exp(-diff / 1000.0))
            assessment.weights["spectral"] = weights[2]
        else:
            assessment.abstained.append("spectral(unmeasured on one or both sides)")

        # 4. Speaking rate — the enrolled side of this was 420 wpm.
        if both_measured("speaking_rate", physics.min_speaking_rate_wpm,
                         physics.max_speaking_rate_wpm):
            diff = abs(test_features.speaking_rate - enrolled_features.speaking_rate)
            assessment.components["rate"] = float(np.exp(-diff / 50.0))
            assessment.weights["rate"] = weights[3]
        else:
            assessment.abstained.append("rate(unmeasured on one or both sides)")

        assessment.finalize()

        if assessment.abstained:
            logger.warning(
                "[Acoustic] %d component(s) ABSTAINED — one or both sides were "
                "not measured, so they were dropped and the remaining weights "
                "renormalised: %s", len(assessment.abstained), assessment.describe(),
            )

        return assessment

    async def _check_physics_plausibility(
        self,
        features: VoiceBiometricFeatures,
        context: Optional[Dict] = None,
    ) -> "PlausibilityAssessment":
        """Check if voice features are physically plausible (thread pool)."""
        return await self._run_in_executor(
            self._check_physics_plausibility_sync,
            features, context
        )

    @property
    def _tolerance(self) -> PerturbationTolerance:
        """
        The perturbation tolerance, built on first use.

        Resolved lazily rather than read straight off ``__init__`` so that a
        verifier reached through any construction path — a partially built
        instance in a test, a subclass that overrides ``__init__``, an object
        restored from a pickle — still scores instead of raising
        ``AttributeError`` from inside a thread pool, where the traceback would
        surface as "verification stage 3 failed" and be substituted with a
        fabricated score. The scorer must not depend on construction order.
        """
        tolerance = getattr(self, "_perturbation", None)
        if tolerance is None:
            tolerance = PerturbationTolerance.from_env()
            self._perturbation = tolerance
        return tolerance

    def _perturbation_caps(
        self,
        features: VoiceBiometricFeatures,
        context: Optional[Dict] = None,
    ) -> "PerturbationCaps":
        """
        Jitter/shimmer limits for this recording, from its measured SNR.

        SNR is taken from the caller's ``context['audio_quality']`` when the
        capture path measured it — the same block the anti-spoofing stage
        already reads, so there is one source of truth for recording quality
        rather than two that can disagree. Absent that, the tolerance falls back
        to its unknown-SNR default rather than assuming studio conditions.

        Deliberately NOT derived from ``energy_contour`` as a substitute: signal
        energy is not signal-to-noise, and a loud recording in a loud room would
        read as clean. Guessing an SNR to satisfy a parameter is the same error
        as guessing a formant.
        """
        snr = None
        if isinstance(context, dict):
            quality = context.get("audio_quality")
            if isinstance(quality, dict):
                snr = quality.get("snr_db")
            elif is_measured(quality, None, None):
                # Some callers pass a bare quality scalar; it is not an SNR and
                # must not be read as one.
                snr = None
        return self._tolerance.caps_for(snr)

    def _check_physics_plausibility_sync(
        self,
        features: VoiceBiometricFeatures,
        context: Optional[Dict] = None,
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

        # 4/5. Jitter and shimmer — perturbation fractions, held to what THIS
        #      recording can support.
        #
        #      The fixed 0.02 / 0.10 caps are clinical figures from voice
        #      pathology work: head-mounted microphone, treated room, where
        #      cycle-to-cycle perturbation is dominated by the larynx. Applied
        #      to a laptop array in a room with a fan they measure the room. The
        #      operator's own VERIFIED sample scored jitter:0.27 shimmer:0.33
        #      against them and dragged an otherwise clean stage to 0.65.
        #
        #      So the caps scale with the measured noise floor. Below the
        #      informative floor the estimates carry no speaker information at
        #      all and the components abstain — a limit wide enough to pass
        #      anything is not a check, it is a check-shaped hole.
        caps = self._perturbation_caps(features, context)
        assessment.perturbation_caps = caps

        for name, value, cap in (
            ("jitter", features.jitter, caps.jitter),
            ("shimmer", features.shimmer, caps.shimmer),
        ):
            bound = BOUNDS[name]
            if not physics.measured(value, bound.low, bound.high):
                abstain(name, f"{name} unmeasured")
            elif not self._tolerance.informative(caps.snr_db):
                # Only reachable with a MEASURED sub-floor SNR, so snr_db is a
                # number here; unknown SNR scores against the widened caps.
                abstain(name, f"SNR {caps.snr_db:.1f} dB below informative floor")
            elif value <= cap:
                scores(name, 1.0)
            else:
                scores(name, cap / value)

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
        """
        Sync owner-aware anti-spoof fusion — THE authorization tensor.

        This, not ``_bayesian_verification_sync``, is what decides an unlock:
        that function computes a full Bayesian posterior and then overwrites it
        with this function's ``final_auth_score`` on the next line. Every guard
        that matters therefore belongs here.

        Only two inputs reach the score: ``owner_match_score`` (the embedding
        similarity) and ``spoof_prob``. ``acoustic_score`` and ``physics_score``
        are recorded in the debug block and do not influence the decision.

        NaN is intercepted on the way in and on the way out, and it fails
        CLOSED. A NaN comparison is False, so an unguarded NaN would fall
        through every ``>=`` branch to whichever else-clause it happened to
        reach — a verdict decided by control flow rather than by evidence. The
        anomaly is recorded in the debug block for the async caller to escalate
        to the runtime health sensor; this runs in a worker thread and cannot
        await, and a watchdog that shares a lock with what it guards is not a
        watchdog.
        """
        OWNER_STRONG_MATCH_THRESHOLD = speaker_model.owner_strong_threshold
        OWNER_OVERRIDABLE_SPOOF_LIMIT = speaker_model.spoof_override_limit
        BASE_UNLOCK_THRESHOLD = speaker_model.decision_threshold

        # ── NaN interceptor: inputs ──────────────────────────────────────
        corrupt = [name for name, value in (
            ("owner_match_score", owner_match_score),
            ("spoof_prob", spoof_prob),
            ("decision_threshold", BASE_UNLOCK_THRESHOLD),
            ("owner_strong_threshold", OWNER_STRONG_MATCH_THRESHOLD),
            ("spoof_override_limit", OWNER_OVERRIDABLE_SPOOF_LIMIT),
        ) if not is_measured(value, None, None)]
        if corrupt:
            logger.critical(
                "🚨 [Fusion] NON-FINITE INPUT to the authorization tensor: %s — "
                "DENYING. A NaN compares False against every threshold, so an "
                "unguarded one decides the verdict by control flow.",
                ", ".join(corrupt),
            )
            return 0.0, "deny", {
                "decision": "deny",
                "rule_applied": "nan_input_fail_closed",
                "final_auth_score": 0.0,
                "authorization_anomaly": {
                    "kind": "non_finite_fusion_input",
                    "fields": corrupt,
                    "owner_match_score": repr(owner_match_score),
                    "spoof_prob": repr(spoof_prob),
                },
            }

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

        # ── NaN interceptor: output ──────────────────────────────────────
        # The inputs were finite, so an infinite output means the arithmetic
        # between them produced one — a corrupted threshold, a poisoned
        # confidence boost. Fail closed and say so rather than clip a NaN into
        # a plausible-looking number.
        anomaly = None
        if not is_measured(final_auth_score, None, None):
            logger.critical(
                "🚨 [Fusion] authorization tensor evaluated to %r from finite "
                "inputs (owner=%r spoof=%r rule=%s) — DENYING",
                final_auth_score, owner_match_score, spoof_prob, rule_applied,
            )
            anomaly = {
                "kind": "non_finite_fusion_output",
                "rule_applied": rule_applied,
                "owner_match_score": float(owner_match_score),
                "spoof_prob": float(spoof_prob),
            }
            final_auth_score, decision, rule_applied = 0.0, "deny", "nan_output_fail_closed"

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
            # Recorded, NOT decisive — neither of these reaches final_auth_score.
            # They are reported as-is, including NaN, because an absent stage is
            # a fact worth seeing in the debug block rather than a 0.0 that
            # reads like a measured failure.
            "acoustic_score": float(acoustic_score),
            "physics_score": float(physics_score),
            "acoustic_is_evidence": bool(is_measured(acoustic_score, 0.0, 1.0)),
            "physics_is_evidence": bool(is_measured(physics_score, 0.0, 1.0)),
            "threshold": float(BASE_UNLOCK_THRESHOLD),
            "owner_strong_threshold": float(OWNER_STRONG_MATCH_THRESHOLD),
            "spoof_override_limit": float(OWNER_OVERRIDABLE_SPOOF_LIMIT),
        }
        if anomaly is not None:
            debug_info["authorization_anomaly"] = anomaly

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
        """
        Sync verification — returns the owner-aware fusion score and uncertainty.

        NAMED "BAYESIAN", AND IT WAS NOT. This function used to compute a full
        Bayesian posterior — prior, per-stage Gaussian likelihoods, weighted
        likelihood, normaliser — and then discard every bit of it::

            posterior = unnormalized_posterior / max(normalizer, 1e-10)
            posterior = float(np.clip(final_auth_score, 0.0, 1.0))   # <- overwrite

        The second line overwrote the first. The verdict has always come from
        ``_owner_aware_antispoof_fusion_sync``; the Bayesian block burned CPU on
        every verification and, far worse, made the file read as though physics
        and acoustic scores influenced authorization when they never did.

        That misreading had a cost. It is why a NaN from the acoustic stage
        looked harmless: it flowed only into arithmetic whose result was thrown
        away. The channel was severed, not safe — and a NaN guard installed on
        that block would have guarded nothing while looking like diligence.

        The dead computation is removed rather than repaired, because repairing
        it means WIRING IT IN, and wiring it in changes what authorises an
        unlock (physics and acoustic would begin to move the verdict). That is
        a security-semantics decision for the operator, not a tidy-up. What
        remains is honest about being a thin wrapper.

        ``fusion_weights`` is retained in the signature: it still shapes
        ``_compute_fusion_weights``' contract and the caller's reporting, and
        removing a parameter to reflect dead code would hide the fact that the
        weights are currently advisory.
        """
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

        posterior = float(np.clip(final_auth_score, 0.0, 1.0))

        # Last line of defence. The fusion already fails closed on a non-finite
        # score, so reaching this means a NaN was introduced between there and
        # here — a case no test covers precisely because nothing should be able
        # to do it. Deny, and surface it: the anomaly rides back in the debug
        # block, which the async caller drains to the runtime health sensor.
        if not is_measured(posterior, 0.0, 1.0):
            logger.critical(
                "🚨 [Verify] posterior is %r after the fusion guard — DENYING",
                posterior,
            )
            debug = getattr(speaker_model, "last_fusion_debug", None)
            if isinstance(debug, dict):
                debug["authorization_anomaly"] = {
                    "kind": "non_finite_posterior",
                    "posterior": repr(posterior),
                    "final_auth_score": repr(final_auth_score),
                }
            posterior = 0.0

        return posterior, float(uncertainty)

    def _dead_capture_reason(self, features: "VoiceBiometricFeatures") -> Optional[str]:
        """
        Why this capture carries no biometric evidence, or ``None`` if it does.

        Scoped to the embedding on purpose. It is the ONLY channel that reaches
        the verdict — ``_owner_aware_antispoof_fusion_sync`` scores
        ``owner_match_score`` (the embedding similarity) and ``spoof_prob``, and
        records everything else without letting it influence the result. A gate
        that also demanded live scalar features would be refusing captures over
        evidence that cannot change the answer.

        Magnitude, not just finiteness. Cosine similarity divides by the norms,
        so an all-zero vector is 0/0: numpy returns NaN with a RuntimeWarning
        nobody reads, and NaN then compares False against every threshold — a
        verdict decided by which else-clause it reaches. The zero vector is also
        the specific shape a TCC-denied microphone produces, which is what makes
        this the likely case rather than the paranoid one.
        """
        vector = coerce_vector(getattr(features, "embedding", None))
        if vector is None or vector.size == 0:
            return "embedding absent"
        if not bool(np.all(np.isfinite(vector))):
            return "embedding contains non-finite values"

        norm = float(np.linalg.norm(vector))
        if not math.isfinite(norm) or norm <= _EPS:
            return f"embedding has zero magnitude (norm={norm:.3g})"
        return None

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
        """
        Fusion weights adjusted for capture quality, renormalised over the ones
        that survive.

        THE UNMEASURED-SNR DEFAULT WAS BACKWARDS. This read
        ``context.get('snr_db', 30)`` twice. 30 dB is above the clean anchor, so
        a capture whose SNR was never measured took the ``> 25`` branch and had
        its acoustic and physics weights INFLATED — the two channels noise
        degrades first. An absent measurement was promoting exactly the evidence
        least able to survive its absence, and it did so silently, because a
        default in a ``.get`` looks like a measurement everywhere downstream.

        ``PerturbationTolerance`` in this same file already refuses that error
        for jitter and shimmer: "assuming studio conditions would penalise every
        real capture". Two functions in one file cannot hold opposite opinions
        about what an unknown SNR means, so this now asks the same object. An
        unmeasured SNR produces NO adjustment at all — not a guessed one.

        The adjustment is continuous rather than two step functions, taken from
        the degradation ratio the tolerance already computes: 0.0 at the clean
        anchor, 1.0 at the floor. One SNR model for the whole file.

        Weights that are not measurements are DROPPED and the remainder
        renormalised — the discipline ``renormalized_weighted_mean`` documents.
        Substituting a value for an absent weight would let a stage that
        measured nothing vote. An empty dict is a legitimate return: it means no
        weight survived, and the caller must drop its own term rather than
        invent one.
        """
        weights = {
            name: float(value)
            for name, value in (speaker_model.fusion_weights or {}).items()
            if is_measured(value, 0.0, None)
        }
        if not weights:
            logger.warning(
                "⚠️  [Fusion] no measured fusion weights on the speaker model; "
                "contributions will be reported as unweighted"
            )
            return {}

        caps = self._tolerance.caps_for((context or {}).get('snr_db'))

        # caps.snr_db is None exactly when the SNR was not measured. No opinion
        # is the honest adjustment; the caps object has already widened its own
        # limits to compensate, and doing it twice would be double-counting.
        if caps.snr_db is not None:
            span = max(self._tolerance.max_scale - 1.0, _EPS)
            degradation = float(np.clip((caps.scale - 1.0) / span, 0.0, 1.0))

            for name, factor in (
                ('embedding', 1.0 + degradation * self._noise_embedding_gain),
                ('acoustic',  1.0 - degradation * self._noise_perturbation_loss),
                ('physics',   1.0 - degradation * self._noise_perturbation_loss),
            ):
                if name in weights:
                    weights[name] = max(weights[name] * factor, 0.0)

        total = sum(weights.values())
        if not is_measured(total, None, None) or total <= 0.0:
            logger.warning(
                "⚠️  [Fusion] weights summed to %r after adjustment; abstaining "
                "rather than renormalising by a non-positive total", total,
            )
            return {}

        return {name: value / total for name, value in weights.items()}

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

        # THE ADAPTIVE MODEL MAY ONLY LEARN FROM MEASUREMENTS.
        #
        # These are exponential moving averages over PERSISTENT state, and an
        # EMA cannot recover from NaN: `(1-a)*NaN + a*x` is NaN, and so is every
        # update after it. One unmeasurable pitch permanently corrupts the model
        # that every later verification is judged against.
        #
        # And this runs only when `verified` is True — so the poisoning would be
        # seeded by the SUCCESS path, the one nobody inspects for corruption. It
        # became reachable the moment the extractors started honestly reporting
        # NaN instead of substituting 150.0 Hz, which is exactly the kind of
        # second-order consequence that makes a partial fix worse than none.
        #
        # `fold_measurement` refuses a non-measurement and leaves the running
        # value alone. Skipping an update costs one sample of adaptation;
        # accepting one costs the model.
        previous_pitch_mean = speaker_model.pitch_mean
        speaker_model.pitch_mean = fold_measurement(
            speaker_model.pitch_mean, new_features.pitch_mean, alpha,
            quantity="pitch_mean_hz", validator=_BOUNDS_VALIDATOR,
            label="speaker_model.pitch_mean",
        )

        # Only update the spread if the mean actually moved — otherwise the
        # deviation term is measured against a mean that ignored this sample.
        if speaker_model.pitch_mean != previous_pitch_mean and is_measured(
            new_features.pitch_mean, None, None
        ):
            variance = ((1 - alpha) * speaker_model.pitch_std ** 2 +
                        alpha * (new_features.pitch_mean - speaker_model.pitch_mean) ** 2)
            if is_measured(variance, 0.0, None):
                speaker_model.pitch_std = float(np.sqrt(variance))

        # The covariance matrix feeds Mahalanobis distance. One NaN row makes
        # every entry NaN, so a sample carrying any unmeasured feature is not
        # admitted to the sample set at all rather than being admitted and
        # poisoning the matrix on the next recompute.
        feature_vector = self._features_to_vector(new_features)
        if np.all(np.isfinite(feature_vector)):
            speaker_model.feature_samples.append(feature_vector)

            if len(speaker_model.feature_samples) > 10:
                feature_samples = speaker_model.feature_samples[-50:]
                covariance = np.cov(np.array(feature_samples).T)
                if np.all(np.isfinite(covariance)):
                    speaker_model.covariance_matrix = covariance
                else:
                    logger.warning(
                        "[SpeakerModel] recomputed covariance is not finite — "
                        "keeping the previous matrix rather than poisoning "
                        "Mahalanobis distance for every future verification"
                    )
        else:
            unmeasured = int(np.sum(~np.isfinite(feature_vector)))
            logger.info(
                "[SpeakerModel] sample has %d unmeasured feature(s) — not "
                "admitted to the covariance sample set", unmeasured,
            )

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
