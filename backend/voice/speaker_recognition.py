"""
Dynamic Speaker Recognition System
Learns and identifies speakers (especially Derek J. Russell) by voice
Uses voice embeddings for biometric authentication
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class VoiceProfile:
    """Voice profile for a speaker"""

    speaker_name: str
    speaker_id: int
    embedding: np.ndarray  # Voice embedding (128-512 dimensions)
    sample_count: int  # Number of voice samples used
    confidence: float  # Confidence in this profile (0.0-1.0)
    created_at: datetime
    updated_at: datetime
    is_owner: bool = False  # Is this the device owner (Derek J. Russell)
    security_clearance: str = "standard"  # standard, elevated, admin


class SpeakerRecognitionEngine:
    """
    Advanced speaker recognition engine using voice embeddings.

    Features:
    - Automatic speaker identification from voice
    - Voice enrollment (learn new speakers)
    - Continuous learning (improve profiles over time)
    - Owner detection (Derek J. Russell gets special privileges)
    - Security verification for sensitive commands
    - Zero-shot learning (recognize from few samples)
    """

    def __init__(self):
        self.profiles: Dict[str, VoiceProfile] = {}
        self.model = None
        self.device = None
        self.initialized = False
        self.learning_db = None

        # Intelligent voice router (cost-aware local/cloud routing)
        self.voice_router = None

        # 🚀 UNIFIED VOICE CACHE: Fast-path for instant recognition
        self.unified_cache = None

        # Similarity thresholds
        self.recognition_threshold = 0.75  # Minimum similarity to recognize speaker
        self.verification_threshold = 0.85  # Higher threshold for security commands
        self.enrollment_threshold = 0.65  # Lower threshold for initial enrollment

        # Owner profile (loaded from database)
        self.owner_profile: Optional[VoiceProfile] = None

        # Background task tracking to prevent GC collection
        self._background_tasks: set = set()

    async def initialize(self):
        """
        Initialize speaker recognition engine with FULLY ASYNC model loading.

        CRITICAL FIX: All ML model loading is now wrapped in asyncio.to_thread()
        with proper timeouts to prevent blocking the event loop.

        Previous bug: EncoderClassifier.from_hparams() was called synchronously,
        blocking the event loop and causing unkillable "startup timeout" hangs.
        """
        if self.initialized:
            return

        logger.info("🎭 Initializing Speaker Recognition Engine (async-safe)...")

        # 🚀 UNIFIED CACHE: Try to connect for instant recognition fast-path
        try:
            from voice_unlock.unified_voice_cache_manager import get_unified_cache_manager

            self.unified_cache = get_unified_cache_manager()
            if self.unified_cache and self.unified_cache.is_ready:
                logger.info(f"✅ Unified voice cache connected ({self.unified_cache.profiles_loaded} profiles)")
            else:
                logger.debug("Unified voice cache not ready yet")
        except ImportError:
            logger.debug("Unified voice cache module not available")
        except Exception as e:
            logger.debug(f"Unified voice cache connection failed: {e}")

        # Initialize intelligent voice router (handles model selection)
        try:
            from voice.intelligent_voice_router import get_voice_router

            self.voice_router = get_voice_router()
            await self.voice_router.initialize()
            logger.info("✅ Intelligent voice router initialized (cost-aware local/cloud)")
        except Exception as e:
            logger.warning(f"Voice router unavailable: {e}")

        # Load speaker recognition model ASYNCHRONOUSLY with timeout
        # This prevents the event loop from blocking during model loading
        await self._load_speaker_model_async()

        # Load existing voice profiles from database
        await self._load_profiles_from_database()

        self.initialized = True
        logger.info(f"✅ Speaker Recognition initialized ({len(self.profiles)} profiles loaded)")

    async def _load_speaker_model_async(self, timeout: float = 45.0):
        """
        Load speaker recognition model asynchronously with timeout protection.

        Uses asyncio.to_thread() to run synchronous PyTorch/SpeechBrain code
        without blocking the event loop. This allows timeouts to actually work.

        Args:
            timeout: Maximum time to wait for model loading (default 45s)
        """
        import time
        start_time = time.perf_counter()

        def _load_speechbrain_model():
            """Synchronous SpeechBrain model loader (runs in thread)."""
            # v93.0: Updated to use speechbrain.inference (pretrained deprecated in SpeechBrain 1.0)
            try:
                from speechbrain.inference import EncoderClassifier
            except ImportError:
                # Fallback for older SpeechBrain versions
                from speechbrain.pretrained import EncoderClassifier
            import torch

            # Limit torch threads to prevent CPU overload
            torch.set_num_threads(2)

            model_name = "speechbrain/spkrec-xvect-voxceleb"
            save_dir = str(Path.home() / ".jarvis" / "models" / "speaker_recognition")
            device = self._get_optimal_device()

            logger.info(f"Loading SpeechBrain model in background thread: {model_name}")

            model = EncoderClassifier.from_hparams(
                source=model_name,
                savedir=save_dir,
                run_opts={"device": device},
            )

            logger.info(f"SpeechBrain model loaded on device: {device}")
            return model

        def _load_resemblyzer_model():
            """Synchronous Resemblyzer loader (runs in thread)."""
            from resemblyzer import VoiceEncoder
            logger.info("Loading Resemblyzer voice encoder in background thread...")
            return VoiceEncoder()

        # Try SpeechBrain first (best quality)
        try:
            logger.info(f"Attempting to load SpeechBrain model (timeout: {timeout}s)...")

            # Run in thread with timeout - THIS IS THE KEY FIX
            # asyncio.wait_for() + asyncio.to_thread() allows proper timeout handling
            self.model = await asyncio.wait_for(
                asyncio.to_thread(_load_speechbrain_model),
                timeout=timeout
            )

            elapsed = (time.perf_counter() - start_time) * 1000
            logger.info(f"✅ SpeechBrain x-vector model loaded successfully ({elapsed:.0f}ms)")
            return

        except asyncio.TimeoutError:
            elapsed = (time.perf_counter() - start_time) * 1000
            logger.warning(f"⏱️ SpeechBrain model load TIMEOUT after {elapsed:.0f}ms - falling back to Resemblyzer")

        except ImportError:
            logger.info("SpeechBrain not available, falling back to Resemblyzer")

        except Exception as e:
            logger.warning(f"SpeechBrain loading failed: {e} - falling back to Resemblyzer")

        # Fallback: Try Resemblyzer (lighter weight, faster to load)
        try:
            start_time = time.perf_counter()

            self.model = await asyncio.wait_for(
                asyncio.to_thread(_load_resemblyzer_model),
                timeout=15.0  # Resemblyzer is much faster
            )

            elapsed = (time.perf_counter() - start_time) * 1000
            logger.info(f"✅ Resemblyzer voice encoder loaded ({elapsed:.0f}ms)")
            return

        except asyncio.TimeoutError:
            logger.warning("⏱️ Resemblyzer model load TIMEOUT")

        except ImportError:
            logger.warning("Resemblyzer not available")

        except Exception as e:
            logger.warning(f"Resemblyzer loading failed: {e}")

        # Final fallback: No local model, use voice router only
        logger.warning("⚠️ No speaker recognition model loaded - using voice router only")
        self.model = None

    def _get_optimal_device(self) -> str:
        """Determine optimal device for speaker recognition"""
        try:
            import torch

            if torch.cuda.is_available():
                return "cuda"
            elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                return "mps"
        except ImportError:
            pass
        return "cpu"

    async def _load_profiles_from_database(self):
        """Load speaker profiles from learning database"""
        try:
            from intelligence.learning_database import get_learning_database

            self.learning_db = await get_learning_database()

            # Get all speaker profiles
            profiles_data = await self.learning_db.get_all_speaker_profiles()

            for profile_data in profiles_data:
                speaker_name = profile_data["speaker_name"]
                speaker_id = profile_data["speaker_id"]

                # Deserialize voice embedding
                embedding = (
                    np.frombuffer(profile_data["voiceprint_embedding"], dtype=np.float32)
                    if profile_data["voiceprint_embedding"]
                    else None
                )

                if embedding is not None:
                    profile = VoiceProfile(
                        speaker_name=speaker_name,
                        speaker_id=speaker_id,
                        embedding=embedding,
                        sample_count=profile_data.get("total_samples", 0),
                        confidence=profile_data.get("recognition_confidence", 0.5),
                        created_at=datetime.fromisoformat(
                            profile_data.get("created_at", datetime.now().isoformat())
                        ),
                        updated_at=datetime.fromisoformat(
                            profile_data.get("last_updated", datetime.now().isoformat())
                        ),
                        is_owner=profile_data.get("is_primary_user", False),
                        security_clearance=profile_data.get("security_level", "standard"),
                    )

                    self.profiles[speaker_name] = profile

                    # Set as owner if marked as primary user
                    if profile.is_owner:
                        self.owner_profile = profile
                        logger.info(f"👑 Owner profile loaded: {speaker_name}")

            logger.info(f"📚 Loaded {len(self.profiles)} speaker profiles from database")

        except Exception as e:
            logger.error(f"Failed to load speaker profiles from database: {e}")

    async def identify_speaker(
        self,
        audio_data: bytes,
        return_confidence: bool = True,
        verification_level: str = "standard",
    ) -> Tuple[Optional[str], float]:
        """
        Identify speaker from audio using intelligent routing.

        Args:
            audio_data: Raw audio bytes
            return_confidence: Return confidence score
            verification_level: "quick", "standard", "high", "critical"

        Returns:
            (speaker_name, confidence) or (None, 0.0) if unknown
        """
        if not self.initialized:
            await self.initialize()

        # 🚀 UNIFIED CACHE FAST-PATH: Try instant recognition before expensive models
        # This provides ~1ms matching vs 200-500ms for full model inference
        if self.unified_cache and self.unified_cache.is_ready:
            try:
                cache_result = await asyncio.wait_for(
                    self.unified_cache.verify_voice_from_audio(
                        audio_data=audio_data,
                        sample_rate=16000,
                    ),
                    timeout=2.0  # Fast-path timeout
                )

                if cache_result.matched and cache_result.similarity >= 0.85:
                    # HIGH CONFIDENCE INSTANT MATCH - skip expensive models!
                    logger.info(
                        f"⚡ UNIFIED CACHE INSTANT MATCH: {cache_result.speaker_name} "
                        f"(similarity={cache_result.similarity:.2%}, type={cache_result.match_type})"
                    )
                    return cache_result.speaker_name, cache_result.similarity

            except asyncio.TimeoutError:
                logger.debug("Unified cache fast-path timed out, using standard path")
            except Exception as e:
                logger.debug(f"Unified cache fast-path failed: {e}")

        # Try intelligent voice router first (cost-aware local/cloud routing)
        if self.voice_router:
            try:
                from voice.intelligent_voice_router import VerificationLevel

                # Map string to enum
                level_map = {
                    "quick": VerificationLevel.QUICK,
                    "standard": VerificationLevel.STANDARD,
                    "high": VerificationLevel.HIGH,
                    "critical": VerificationLevel.CRITICAL,
                }
                level = level_map.get(verification_level, VerificationLevel.STANDARD)

                # Use intelligent router
                result = await self.voice_router.recognize_speaker(
                    audio_data, verification_level=level
                )

                # Save embedding to learning database for continuous improvement
                if result.speaker_name != "Unknown":
                    await self._save_voice_sample(
                        speaker_name=result.speaker_name,
                        audio_data=audio_data,
                        embedding=result.embedding,
                        confidence=result.confidence,
                        model_used=result.model_used.value,
                    )

                logger.info(
                    f"🎭 Speaker identified via {result.model_used.value}: "
                    f"{result.speaker_name} (confidence: {result.confidence:.2f}, "
                    f"latency: {result.latency_ms:.0f}ms, cost: ${result.cost_cents/100:.4f})"
                )

                return result.speaker_name, result.confidence

            except Exception as e:
                logger.warning(f"Voice router failed, falling back to legacy: {e}")

        # Fallback to legacy local model
        if self.model is None:
            return await self._identify_speaker_heuristic(audio_data)

        try:
            # Extract voice embedding from audio
            embedding = await self._extract_embedding(audio_data)

            if embedding is None:
                # GRACEFUL DEGRADATION: Return low confidence instead of hard fail
                # This allows fallback to physics-only or alternative authentication
                logger.info("🔄 Embedding extraction failed - enabling graceful degradation")
                return None, 0.08  # Very low confidence signals degradation mode

            # Compare with all known profiles
            best_match = None
            best_similarity = 0.0

            for speaker_name, profile in self.profiles.items():
                similarity = self._cosine_similarity(embedding, profile.embedding)

                if similarity > best_similarity:
                    best_similarity = similarity
                    best_match = speaker_name

            # Check if similarity meets threshold
            if best_similarity >= self.recognition_threshold:
                logger.info(
                    f"🎭 Speaker identified (legacy): {best_match} (confidence: {best_similarity:.2f})"
                )

                # Update profile with new sample (continuous learning)
                _task = asyncio.create_task(
                    self._update_profile_with_sample(best_match, embedding, audio_data),
                    name="speaker-recog-update-profile",
                )
                self._background_tasks.add(_task)
                _task.add_done_callback(self._background_tasks.discard)

                return best_match, best_similarity
            else:
                logger.info(
                    f"🎭 Unknown speaker (best match: {best_match} @ {best_similarity:.2f}, threshold: {self.recognition_threshold})"
                )
                return None, best_similarity

        except Exception as e:
            logger.error(f"Speaker identification failed: {e}")
            # GRACEFUL DEGRADATION: Return minimal confidence instead of hard fail
            # This allows the authentication system to try alternative methods
            logger.info("🔄 Speaker identification exception - enabling graceful degradation")
            return None, 0.05

    async def verify_speaker(self, audio_data: bytes, claimed_speaker: str) -> Tuple[bool, float]:
        """
        Verify if audio matches claimed speaker (for security commands).

        Args:
            audio_data: Raw audio bytes
            claimed_speaker: Name of speaker to verify against

        Returns:
            (is_match, confidence)
        """
        if not self.initialized:
            await self.initialize()

        if claimed_speaker not in self.profiles:
            logger.warning(f"No profile found for claimed speaker: {claimed_speaker}")
            return False, 0.0

        try:
            # Extract embedding
            embedding = await self._extract_embedding(audio_data)
            if embedding is None:
                return False, 0.0

            # Compare with claimed speaker's profile
            profile = self.profiles[claimed_speaker]
            similarity = self._cosine_similarity(embedding, profile.embedding)

            # Use higher threshold for verification
            is_match = similarity >= self.verification_threshold

            logger.info(
                f"🔐 Speaker verification: {claimed_speaker} - {'✅ PASS' if is_match else '❌ FAIL'} (confidence: {similarity:.2f})"
            )

            return is_match, similarity

        except Exception as e:
            logger.error(f"Speaker verification failed: {e}")
            return False, 0.0

    async def enroll_speaker(
        self, speaker_name: str, audio_samples: List[bytes], is_owner: bool = False
    ) -> bool:
        """
        Enroll a new speaker with voice samples.

        Args:
            speaker_name: Name of speaker
            audio_samples: List of audio samples (at least 3-5 recommended)
            is_owner: Mark this speaker as device owner (Derek J. Russell)

        Returns:
            True if enrollment successful
        """
        if not self.initialized:
            await self.initialize()

        logger.info(
            f"🎓 Enrolling speaker: {speaker_name} ({len(audio_samples)} samples, owner={is_owner})"
        )

        try:
            # Extract embeddings from all samples
            embeddings = []
            for audio in audio_samples:
                embedding = await self._extract_embedding(audio)
                if embedding is not None:
                    embeddings.append(embedding)

            if len(embeddings) == 0:
                logger.error("No valid embeddings extracted from audio samples")
                return False

            # Average embeddings to create speaker profile
            avg_embedding = np.mean(embeddings, axis=0)

            # Calculate confidence based on consistency
            confidences = [self._cosine_similarity(emb, avg_embedding) for emb in embeddings]
            avg_confidence = np.mean(confidences)

            # Create profile
            profile = VoiceProfile(
                speaker_name=speaker_name,
                speaker_id=hash(speaker_name) % 1000000,  # Temporary ID (will be replaced by DB)
                embedding=avg_embedding,
                sample_count=len(embeddings),
                confidence=avg_confidence,
                created_at=datetime.now(),
                updated_at=datetime.now(),
                is_owner=is_owner,
                security_clearance="admin" if is_owner else "standard",
            )

            # Save to database
            if self.learning_db:
                speaker_id = await self.learning_db.get_or_create_speaker_profile(
                    speaker_name=speaker_name
                )
                profile.speaker_id = speaker_id

                # Update with embedding
                await self.learning_db.update_speaker_embedding(
                    speaker_id=speaker_id,
                    embedding=avg_embedding.tobytes(),
                    confidence=avg_confidence,
                    is_primary_user=is_owner,
                )

            # Store in memory
            self.profiles[speaker_name] = profile

            if is_owner:
                self.owner_profile = profile
                logger.info(f"👑 Owner profile created: {speaker_name}")

            logger.info(f"✅ Speaker enrolled: {speaker_name} (confidence: {avg_confidence:.2f})")
            return True

        except Exception as e:
            logger.error(f"Speaker enrollment failed: {e}")
            return False

    async def _extract_embedding(self, audio_data: bytes) -> Optional[np.ndarray]:
        """
        Extract voice embedding from audio.

        PERFORMANCE FIX: All CPU-intensive operations (librosa, torch, MFCC)
        are now run in asyncio.to_thread() to prevent blocking the event loop.
        This is critical for keeping the backend responsive during voice unlock.
        """
        try:
            # Run the entire CPU-intensive extraction in a thread pool
            # to avoid blocking the async event loop
            return await asyncio.to_thread(
                self._extract_embedding_sync, audio_data
            )
        except Exception as e:
            logger.error(f"Failed to extract embedding: {e}")
            return None

    def _extract_embedding_sync(self, audio_data: bytes) -> Optional[np.ndarray]:
        """
        Synchronous embedding extraction - runs in thread pool via asyncio.to_thread().

        This method performs CPU-intensive operations:
        - librosa.load() for audio decoding
        - torch.FloatTensor() and model.encode_batch() for ECAPA embedding
        - librosa.feature.mfcc() for fallback embedding

        THREAD SAFETY: Captures model reference at start to prevent segfaults
        if model is unloaded during extraction.
        """
        try:
            # SAFETY: Capture model reference at start of method
            # This prevents segfaults if model is set to None during extraction
            model_ref = self.model
            if model_ref is None:
                logger.warning("Model reference is None - cannot extract embedding")
                return None

            # Convert audio bytes to numpy array
            import io

            # Try with librosa
            try:
                import librosa

                audio_array, sr = librosa.load(io.BytesIO(audio_data), sr=16000, mono=True)
            except ImportError:
                # Fallback to scipy
                import scipy.io.wavfile as wavfile

                sr, audio_array = wavfile.read(io.BytesIO(audio_data))
                if len(audio_array.shape) > 1:
                    audio_array = audio_array.mean(axis=1)
                audio_array = audio_array.astype(np.float32) / 32768.0

            # Extract embedding with model using captured reference
            if hasattr(model_ref, "encode_batch"):
                # SpeechBrain ECAPA-TDNN
                import torch

                # Double-check reference is still valid
                if model_ref is None:
                    logger.warning("Model reference became None during extraction")
                    return None

                # Limit torch threads to prevent CPU overload
                torch.set_num_threads(1)

                audio_tensor = torch.FloatTensor(audio_array).unsqueeze(0)
                with torch.no_grad():  # Disable gradient computation for inference
                    # CRITICAL: Use .copy() to avoid memory corruption!
                    # .numpy() shares memory with tensor - must copy before returning
                    result = model_ref.encode_batch(audio_tensor).squeeze().cpu()
                    embedding = np.array(result.numpy(), dtype=np.float32, copy=True)
            elif hasattr(model_ref, "embed_utterance"):
                # Resemblyzer
                embedding = model_ref.embed_utterance(audio_array)
            else:
                # Fallback: use MFCC features as simple embedding
                import librosa

                mfcc = librosa.feature.mfcc(y=audio_array, sr=sr, n_mfcc=20)
                embedding = np.mean(mfcc, axis=1)

            return embedding

        except Exception as e:
            logger.error(f"Failed to extract embedding (sync): {e}")
            return None

    def _cosine_similarity(self, emb1: np.ndarray, emb2: np.ndarray) -> float:
        """Calculate cosine similarity between two embeddings"""
        dot_product = np.dot(emb1, emb2)
        norm1 = np.linalg.norm(emb1)
        norm2 = np.linalg.norm(emb2)

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return dot_product / (norm1 * norm2)

    async def _save_voice_sample(
        self,
        speaker_name: str,
        audio_data: bytes,
        embedding: np.ndarray,
        confidence: float,
        model_used: str,
    ):
        """
        Save voice sample to learning database for continuous improvement.

        This enables the system to learn and improve voice recognition over time.
        """
        try:
            if not self.learning_db:
                from intelligence.learning_database import get_learning_database

                self.learning_db = await get_learning_database()

            # Store embedding in database
            embedding_bytes = embedding.astype(np.float32).tobytes()

            # Update speaker profile with new embedding
            speaker_id = await self.learning_db.get_or_create_speaker_profile(speaker_name)

            await self.learning_db.update_speaker_embedding(
                speaker_id=speaker_id,
                embedding=embedding_bytes,
                confidence=confidence,
                is_primary_user=(speaker_name == "Derek J. Russell"),
            )

            # Also record the voice sample
            audio_duration_ms = len(audio_data) / 16  # Rough estimate (16kHz)
            await self.learning_db.record_voice_sample(
                speaker_name=speaker_name,
                audio_data=audio_data,
                transcription="",  # Transcription handled elsewhere
                audio_duration_ms=audio_duration_ms,
                quality_score=confidence,
            )

            logger.debug(
                f"💾 Saved voice sample for {speaker_name} ({model_used}, confidence: {confidence:.2f})"
            )

        except Exception as e:
            logger.error(f"Failed to save voice sample to learning database: {e}")

    async def _identify_speaker_heuristic(self, audio_data: bytes) -> Tuple[Optional[str], float]:
        """
        Fallback speaker identification using heuristics.

        When speaker recognition model is not available, use simpler methods:
        - Check if this is the primary user based on system context
        - Use audio characteristics (pitch, energy, duration)
        """
        # Check if there's only one profile (assume it's them)
        if len(self.profiles) == 1:
            speaker_name = list(self.profiles.keys())[0]
            logger.info(f"🎭 Single profile heuristic: assuming speaker is {speaker_name}")
            return speaker_name, 0.8  # Medium confidence

        # Check if owner profile exists (assume it's the owner)
        if self.owner_profile:
            logger.info(
                f"🎭 Owner heuristic: assuming speaker is {self.owner_profile.speaker_name}"
            )
            return self.owner_profile.speaker_name, 0.75

        # Unknown
        return None, 0.0

    async def _update_profile_with_sample(
        self, speaker_name: str, embedding: np.ndarray, audio_data: bytes
    ):
        """Update speaker profile with new sample (continuous learning)"""
        try:
            if speaker_name not in self.profiles:
                return

            profile = self.profiles[speaker_name]

            # Moving average of embeddings
            alpha = 0.1  # Learning rate (10% new, 90% old)
            profile.embedding = (1 - alpha) * profile.embedding + alpha * embedding
            profile.sample_count += 1
            profile.updated_at = datetime.now()

            # Update in database
            if self.learning_db:
                await self.learning_db.record_voice_sample(
                    speaker_name=speaker_name,
                    audio_data=audio_data,
                    transcription="",  # Unknown at this point
                    audio_duration_ms=len(audio_data) / 32,
                    quality_score=0.9,  # Assume good quality
                )

                await self.learning_db.update_speaker_embedding(
                    speaker_id=profile.speaker_id,
                    embedding=profile.embedding.tobytes(),
                    confidence=profile.confidence,
                )

            logger.debug(f"📈 Updated profile for {speaker_name} (sample #{profile.sample_count})")

        except Exception as e:
            logger.error(f"Failed to update profile: {e}")

    def is_owner(self, speaker_name: Optional[str]) -> bool:
        """Check if speaker is the device owner"""
        if not speaker_name or speaker_name not in self.profiles:
            return False
        return self.profiles[speaker_name].is_owner

    def get_security_clearance(self, speaker_name: Optional[str]) -> str:
        """Get security clearance level for speaker"""
        if not speaker_name or speaker_name not in self.profiles:
            return "none"
        return self.profiles[speaker_name].security_clearance


# Global singleton
_speaker_recognition_engine: Optional[SpeakerRecognitionEngine] = None


def get_speaker_recognition_engine() -> SpeakerRecognitionEngine:
    """Get global speaker recognition engine instance"""
    global _speaker_recognition_engine
    if _speaker_recognition_engine is None:
        _speaker_recognition_engine = SpeakerRecognitionEngine()
    return _speaker_recognition_engine
