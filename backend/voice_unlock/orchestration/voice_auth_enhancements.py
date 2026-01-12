#!/usr/bin/env python3
"""
Voice Authentication Enhancements v2.0
======================================

Comprehensive enhancements to the existing voice authentication system with:
- ChromaDB voice pattern recognition and anti-spoofing
- Langfuse authentication audit trail and observability
- Helicone cost optimization with intelligent caching
- Environmental adaptation with learning
- Progressive confidence communication

This module integrates seamlessly with voice_auth_orchestrator.py and voice_auth_graph.py
without duplicating functionality.

Author: JARVIS AI System
Version: 2.0.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)

# Optional integrations
CHROMADB_AVAILABLE = False
LANGFUSE_AVAILABLE = False
HELICONE_AVAILABLE = False

try:
    import chromadb
    from chromadb.config import Settings as ChromaSettings
    CHROMADB_AVAILABLE = True
except ImportError:
    logger.debug("ChromaDB not available - pattern recognition will be disabled")

try:
    from langfuse import Langfuse
    from langfuse.decorators import observe, langfuse_context
    LANGFUSE_AVAILABLE = True
except ImportError:
    # Create no-op decorators if Langfuse not available
    def observe(*args, **kwargs):
        def decorator(func):
            return func
        return decorator if not args else args[0]
    langfuse_context = None
    logger.debug("Langfuse not available - audit trail will be disabled")

try:
    from helicone import Helicone
    HELICONE_AVAILABLE = True
except ImportError:
    logger.debug("Helicone not available - cost optimization will use basic caching")


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class VoiceAuthEnhancementConfig:
    """Configuration for voice authentication enhancements."""

    # ChromaDB Pattern Recognition
    enable_pattern_recognition: bool = field(
        default_factory=lambda: os.getenv("VOICE_AUTH_PATTERN_RECOGNITION", "true").lower() == "true"
    )
    pattern_db_path: str = field(
        default_factory=lambda: os.getenv(
            "VOICE_AUTH_PATTERN_DB",
            str(Path.home() / ".cache" / "jarvis" / "voice_patterns")
        )
    )
    max_patterns_per_user: int = field(
        default_factory=lambda: int(os.getenv("VOICE_AUTH_MAX_PATTERNS", "100"))
    )
    pattern_similarity_threshold: float = field(
        default_factory=lambda: float(os.getenv("VOICE_AUTH_PATTERN_SIMILARITY", "0.85"))
    )

    # Langfuse Audit Trail
    enable_audit_trail: bool = field(
        default_factory=lambda: os.getenv("VOICE_AUTH_AUDIT_TRAIL", "true").lower() == "true"
    )
    langfuse_public_key: Optional[str] = field(
        default_factory=lambda: os.getenv("LANGFUSE_PUBLIC_KEY")
    )
    langfuse_secret_key: Optional[str] = field(
        default_factory=lambda: os.getenv("LANGFUSE_SECRET_KEY")
    )
    langfuse_host: str = field(
        default_factory=lambda: os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
    )

    # Helicone Cost Optimization
    enable_cost_optimization: bool = field(
        default_factory=lambda: os.getenv("VOICE_AUTH_COST_OPT", "true").lower() == "true"
    )
    cache_ttl_seconds: float = field(
        default_factory=lambda: float(os.getenv("VOICE_AUTH_CACHE_TTL", "30.0"))
    )
    max_cache_entries: int = field(
        default_factory=lambda: int(os.getenv("VOICE_AUTH_CACHE_MAX", "100"))
    )
    helicone_api_key: Optional[str] = field(
        default_factory=lambda: os.getenv("HELICONE_API_KEY")
    )

    # Environmental Adaptation
    enable_environmental_learning: bool = field(
        default_factory=lambda: os.getenv("VOICE_AUTH_ENV_LEARNING", "true").lower() == "true"
    )
    noise_threshold_db: float = field(
        default_factory=lambda: float(os.getenv("VOICE_AUTH_NOISE_THRESHOLD", "-42.0"))
    )
    snr_threshold_db: float = field(
        default_factory=lambda: float(os.getenv("VOICE_AUTH_SNR_THRESHOLD", "12.0"))
    )

    # Progressive Confidence Communication
    enable_progressive_feedback: bool = field(
        default_factory=lambda: os.getenv("VOICE_AUTH_PROGRESSIVE_FEEDBACK", "true").lower() == "true"
    )
    verbose_feedback: bool = field(
        default_factory=lambda: os.getenv("VOICE_AUTH_VERBOSE_FEEDBACK", "false").lower() == "true"
    )


# =============================================================================
# ChromaDB Voice Pattern Recognition
# =============================================================================

class VoicePatternStore:
    """
    ChromaDB-based voice pattern storage and retrieval.

    Stores:
    - Historical voice embeddings with context
    - Environmental variations (microphone types, locations)
    - Temporal patterns (voice changes over time)
    - Anti-spoofing reference patterns
    """

    def __init__(self, config: VoiceAuthEnhancementConfig):
        self.config = config
        self.client: Optional[chromadb.Client] = None
        self.voice_collection: Optional[chromadb.Collection] = None
        self.environment_collection: Optional[chromadb.Collection] = None
        self._initialized = False
        self._pattern_cache: Dict[str, Any] = {}

    async def initialize(self) -> bool:
        """Initialize ChromaDB client and collections."""
        if not CHROMADB_AVAILABLE or not self.config.enable_pattern_recognition:
            logger.info("[PatternStore] ChromaDB disabled - pattern recognition unavailable")
            return False

        try:
            # Create persistent client
            os.makedirs(self.config.pattern_db_path, exist_ok=True)

            self.client = chromadb.Client(ChromaSettings(
                is_persistent=True,
                persist_directory=self.config.pattern_db_path,
                anonymized_telemetry=False,
            ))

            # Voice patterns collection
            self.voice_collection = self.client.get_or_create_collection(
                name="voice_patterns_v2",
                metadata={"description": "Voice authentication patterns with context"}
            )

            # Environmental patterns collection
            self.environment_collection = self.client.get_or_create_collection(
                name="environmental_patterns",
                metadata={"description": "Environmental voice variations"}
            )

            self._initialized = True
            voice_count = self.voice_collection.count()
            env_count = self.environment_collection.count()

            logger.info(
                f"[PatternStore] Initialized | "
                f"Voice patterns: {voice_count} | "
                f"Environmental patterns: {env_count}"
            )
            return True

        except Exception as e:
            logger.error(f"[PatternStore] Initialization failed: {e}")
            return False

    async def store_authentication_pattern(
        self,
        embedding: np.ndarray,
        speaker_id: str,
        confidence: float,
        success: bool,
        environmental_context: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Store an authentication pattern for learning."""
        if not self._initialized or not self.voice_collection:
            return False

        try:
            pattern_id = f"{speaker_id}_{int(time.time()*1000000)}"

            meta = {
                "speaker_id": speaker_id,
                "confidence": confidence,
                "success": success,
                "timestamp": datetime.now().isoformat(),
                "microphone": environmental_context.get("microphone", "unknown"),
                "location_hash": environmental_context.get("location_hash", "unknown"),
                "snr_db": environmental_context.get("snr_db", 0.0),
                "noise_level_db": environmental_context.get("noise_level_db", 0.0),
                **(metadata or {}),
            }

            self.voice_collection.add(
                embeddings=[embedding.tolist()],
                metadatas=[meta],
                ids=[pattern_id]
            )

            # Cleanup old patterns periodically
            if pattern_id.endswith("000"):  # Every ~1000th pattern
                await self._cleanup_old_patterns(speaker_id)

            return True

        except Exception as e:
            logger.error(f"[PatternStore] Failed to store pattern: {e}")
            return False

    async def find_similar_patterns(
        self,
        embedding: np.ndarray,
        speaker_id: str,
        n_results: int = 5,
        filter_metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Find similar voice patterns for a speaker."""
        if not self._initialized or not self.voice_collection:
            return []

        try:
            # Build where clause
            where_clause = {"speaker_id": speaker_id}
            if filter_metadata:
                where_clause.update(filter_metadata)

            results = self.voice_collection.query(
                query_embeddings=[embedding.tolist()],
                n_results=n_results,
                where=where_clause
            )

            patterns = []
            if results and results['metadatas']:
                for i, metadata in enumerate(results['metadatas'][0]):
                    distance = results['distances'][0][i] if results['distances'] else 1.0
                    similarity = 1.0 - min(distance, 1.0)

                    patterns.append({
                        'similarity': similarity,
                        'metadata': metadata,
                        'distance': distance,
                    })

            return patterns

        except Exception as e:
            logger.debug(f"[PatternStore] Pattern search failed: {e}")
            return []

    async def detect_replay_attack(
        self,
        embedding: np.ndarray,
        environmental_signature: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, float, str]:
        """
        Detect replay attack by analyzing pattern anomalies.

        Returns:
            (is_replay, confidence, reason)
        """
        if not self._initialized:
            return False, 0.0, "pattern_store_unavailable"

        try:
            # Find very similar patterns (>98% match is suspicious)
            results = self.voice_collection.query(
                query_embeddings=[embedding.tolist()],
                n_results=10,
            )

            if results and results['distances']:
                for i, distance in enumerate(results['distances'][0]):
                    similarity = 1.0 - distance

                    # Perfect or near-perfect match
                    if similarity > 0.98:
                        metadata = results['metadatas'][0][i]

                        # Check if environmental signature differs significantly
                        if environmental_signature:
                            stored_snr = metadata.get('snr_db', 0)
                            current_snr = environmental_signature.get('snr_db', 0)
                            snr_diff = abs(stored_snr - current_snr)

                            # Identical voice but different environment = replay
                            if snr_diff > 5.0:  # Significant SNR difference
                                logger.warning(
                                    f"[PatternStore] Replay attack detected: "
                                    f"similarity={similarity:.4f}, SNR diff={snr_diff:.1f}dB"
                                )
                                return True, similarity, "environment_mismatch"

                        # Multiple near-perfect matches in short time window
                        stored_time = metadata.get('timestamp', '')
                        if stored_time:
                            try:
                                stored_dt = datetime.fromisoformat(stored_time)
                                time_diff = (datetime.now() - stored_dt).total_seconds()

                                # Perfect match within 60 seconds = suspicious
                                if time_diff < 60 and similarity > 0.99:
                                    logger.warning(
                                        f"[PatternStore] Replay attack detected: "
                                        f"Perfect match within {time_diff:.0f}s"
                                    )
                                    return True, similarity, "temporal_anomaly"
                            except Exception:
                                pass

            return False, 0.0, "legitimate"

        except Exception as e:
            logger.debug(f"[PatternStore] Replay detection failed: {e}")
            return False, 0.0, "detection_error"

    async def learn_environmental_variation(
        self,
        speaker_id: str,
        microphone_type: str,
        location_hash: str,
        voice_adjustment: float,
    ) -> bool:
        """Learn how voice changes in different environments."""
        if not self._initialized or not self.environment_collection:
            return False

        try:
            env_id = hashlib.md5(
                f"{speaker_id}_{microphone_type}_{location_hash}".encode()
            ).hexdigest()[:16]

            # Check if this environment exists
            existing = self.environment_collection.get(ids=[env_id])

            if existing and existing['ids']:
                # Update existing
                meta = existing['metadatas'][0]
                meta['usage_count'] = meta.get('usage_count', 0) + 1
                meta['avg_adjustment'] = (
                    (meta.get('avg_adjustment', 0) * (meta['usage_count'] - 1) + voice_adjustment)
                    / meta['usage_count']
                )
                meta['last_seen'] = datetime.now().isoformat()

                self.environment_collection.update(
                    ids=[env_id],
                    metadatas=[meta]
                )
            else:
                # Create new
                self.environment_collection.add(
                    ids=[env_id],
                    embeddings=[[0.0] * 192],  # Placeholder
                    metadatas=[{
                        "speaker_id": speaker_id,
                        "microphone_type": microphone_type,
                        "location_hash": location_hash,
                        "usage_count": 1,
                        "avg_adjustment": voice_adjustment,
                        "first_seen": datetime.now().isoformat(),
                        "last_seen": datetime.now().isoformat(),
                    }]
                )

            logger.debug(
                f"[PatternStore] Learned environment: "
                f"{microphone_type} at {location_hash[:8]} "
                f"(adjustment: {voice_adjustment:+.3f})"
            )
            return True

        except Exception as e:
            logger.debug(f"[PatternStore] Environment learning failed: {e}")
            return False

    async def get_environmental_adjustment(
        self,
        speaker_id: str,
        microphone_type: str,
        location_hash: str,
    ) -> Optional[float]:
        """Get learned voice adjustment for an environment."""
        if not self._initialized or not self.environment_collection:
            return None

        try:
            env_id = hashlib.md5(
                f"{speaker_id}_{microphone_type}_{location_hash}".encode()
            ).hexdigest()[:16]

            result = self.environment_collection.get(ids=[env_id])

            if result and result['metadatas']:
                return result['metadatas'][0].get('avg_adjustment')

            return None

        except Exception as e:
            logger.debug(f"[PatternStore] Environment lookup failed: {e}")
            return None

    async def _cleanup_old_patterns(self, speaker_id: str) -> None:
        """Remove old patterns to stay within limits."""
        if not self.voice_collection:
            return

        try:
            # Get all patterns for this speaker
            results = self.voice_collection.get(
                where={"speaker_id": speaker_id}
            )

            if results and results['ids']:
                count = len(results['ids'])
                if count > self.config.max_patterns_per_user:
                    # Sort by timestamp and remove oldest
                    patterns_with_time = []
                    for i, pattern_id in enumerate(results['ids']):
                        metadata = results['metadatas'][i]
                        timestamp_str = metadata.get('timestamp', '1970-01-01T00:00:00')
                        try:
                            timestamp = datetime.fromisoformat(timestamp_str)
                        except Exception:
                            timestamp = datetime(1970, 1, 1)
                        patterns_with_time.append((pattern_id, timestamp))

                    # Sort and remove oldest
                    patterns_with_time.sort(key=lambda x: x[1])
                    to_remove = count - self.config.max_patterns_per_user
                    ids_to_remove = [p[0] for p in patterns_with_time[:to_remove]]

                    self.voice_collection.delete(ids=ids_to_remove)
                    logger.debug(f"[PatternStore] Cleaned up {len(ids_to_remove)} old patterns for {speaker_id}")

        except Exception as e:
            logger.debug(f"[PatternStore] Cleanup failed: {e}")

    async def get_stats(self) -> Dict[str, Any]:
        """Get pattern store statistics."""
        if not self._initialized:
            return {"initialized": False}

        try:
            return {
                "initialized": True,
                "voice_patterns": self.voice_collection.count() if self.voice_collection else 0,
                "environmental_patterns": self.environment_collection.count() if self.environment_collection else 0,
                "config": {
                    "max_patterns_per_user": self.config.max_patterns_per_user,
                    "similarity_threshold": self.config.pattern_similarity_threshold,
                },
            }
        except Exception:
            return {"initialized": True, "error": "stats_unavailable"}


# =============================================================================
# Langfuse Authentication Audit Trail
# =============================================================================

class AuthenticationAuditTrail:
    """
    Langfuse-based authentication audit trail and observability.

    Provides:
    - Complete authentication decision traces
    - Performance monitoring and optimization insights
    - Security incident detection and forensics
    - Cost tracking and optimization
    """

    def __init__(self, config: VoiceAuthEnhancementConfig):
        self.config = config
        self.langfuse: Optional[Langfuse] = None
        self._initialized = False
        self._audit_cache: deque = deque(maxlen=1000)

    async def initialize(self) -> bool:
        """Initialize Langfuse client."""
        if not LANGFUSE_AVAILABLE or not self.config.enable_audit_trail:
            logger.info("[AuditTrail] Langfuse disabled - audit trail unavailable")
            return False

        if not self.config.langfuse_public_key or not self.config.langfuse_secret_key:
            logger.warning("[AuditTrail] Langfuse credentials not configured")
            return False

        try:
            self.langfuse = Langfuse(
                public_key=self.config.langfuse_public_key,
                secret_key=self.config.langfuse_secret_key,
                host=self.config.langfuse_host,
            )

            self._initialized = True
            logger.info(f"[AuditTrail] Initialized with host: {self.config.langfuse_host}")
            return True

        except Exception as e:
            logger.error(f"[AuditTrail] Initialization failed: {e}")
            return False

    @observe(name="voice_authentication")
    async def trace_authentication(
        self,
        session_id: str,
        user_id: str,
        decision: str,
        confidence: float,
        duration_ms: float,
        details: Dict[str, Any],
    ) -> bool:
        """Trace an authentication attempt."""
        if not self._initialized:
            # Still cache locally even if Langfuse unavailable
            self._audit_cache.append({
                "session_id": session_id,
                "user_id": user_id,
                "decision": decision,
                "confidence": confidence,
                "duration_ms": duration_ms,
                "timestamp": datetime.now().isoformat(),
                **details,
            })
            return False

        try:
            # Langfuse will automatically trace this via @observe decorator
            if langfuse_context:
                langfuse_context.update_current_trace(
                    session_id=session_id,
                    user_id=user_id,
                    metadata={
                        "decision": decision,
                        "confidence": confidence,
                        "duration_ms": duration_ms,
                        **details,
                    },
                    tags=["voice_authentication", decision.lower()],
                )

            return True

        except Exception as e:
            logger.debug(f"[AuditTrail] Trace failed: {e}")
            return False

    async def get_authentication_history(
        self,
        user_id: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Get recent authentication history for a user."""
        # Return from local cache for now
        return [
            entry for entry in reversed(self._audit_cache)
            if entry.get('user_id') == user_id
        ][:limit]

    async def get_security_incidents(
        self,
        since: datetime,
        incident_types: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Get security incidents (failed auth, replay attacks, etc.)."""
        incidents = []

        for entry in reversed(self._audit_cache):
            entry_time = datetime.fromisoformat(entry.get('timestamp', ''))
            if entry_time < since:
                continue

            # Check for incidents
            if entry.get('decision') == 'DENY':
                incidents.append({
                    "type": "failed_authentication",
                    "timestamp": entry['timestamp'],
                    "user_id": entry.get('user_id'),
                    "confidence": entry.get('confidence'),
                    "details": entry,
                })
            elif entry.get('replay_detected'):
                incidents.append({
                    "type": "replay_attack",
                    "timestamp": entry['timestamp'],
                    "user_id": entry.get('user_id'),
                    "details": entry,
                })

        return incidents

    async def shutdown(self) -> None:
        """Shutdown audit trail and flush remaining traces."""
        if self.langfuse:
            try:
                self.langfuse.flush()
            except Exception:
                pass


# Continued in next part...

# =============================================================================
# Cost Optimization with Intelligent Caching
# =============================================================================

@dataclass
class CachedAuthResult:
    """Cached authentication result."""
    embedding_hash: str
    user_id: str
    confidence: float
    decision: str
    timestamp: float
    environmental_hash: str
    cost_saved_usd: float = 0.0

class VoiceAuthCostOptimizer:
    """
    Intelligent caching and cost optimization for voice authentication.

    Strategies:
    - Embedding similarity-based caching
    - Environmental context matching
    - Temporal validity windows
    - Cost tracking and reporting
    """

    def __init__(self, config: VoiceAuthEnhancementConfig):
        self.config = config
        self._cache: Dict[str, CachedAuthResult] = {}
        self._cache_hits = 0
        self._cache_misses = 0
        self._total_cost_saved_usd = 0.0

    def _hash_embedding(self, embedding: np.ndarray) -> str:
        """Generate hash for embedding."""
        # Round to 3 decimals for fuzzy matching
        rounded = np.round(embedding, 3)
        return hashlib.md5(rounded.tobytes()).hexdigest()[:16]

    def _hash_environment(self, env: Dict[str, Any]) -> str:
        """Generate hash for environmental context."""
        env_str = json.dumps({
            "mic": env.get("microphone", ""),
            "loc": env.get("location_hash", "")[:8],
            "snr_range": int(env.get("snr_db", 0) / 5) * 5,  # 5dB buckets
        }, sort_keys=True)
        return hashlib.md5(env_str.encode()).hexdigest()[:8]

    async def check_cache(
        self,
        embedding: np.ndarray,
        user_id: str,
        environmental_context: Dict[str, Any],
    ) -> Optional[CachedAuthResult]:
        """Check if we have a cached result for this authentication."""
        if not self.config.enable_cost_optimization:
            return None

        emb_hash = self._hash_embedding(embedding)
        env_hash = self._hash_environment(environmental_context)
        cache_key = f"{user_id}_{emb_hash}_{env_hash}"

        cached = self._cache.get(cache_key)

        if cached:
            # Check if still valid
            age = time.time() - cached.timestamp
            if age < self.config.cache_ttl_seconds:
                self._cache_hits += 1
                logger.debug(
                    f"[CostOptimizer] Cache HIT (age: {age:.1f}s, "
                    f"saved: ${cached.cost_saved_usd:.4f})"
                )
                return cached

            # Expired - remove
            del self._cache[cache_key]

        self._cache_misses += 1
        return None

    async def store_result(
        self,
        embedding: np.ndarray,
        user_id: str,
        environmental_context: Dict[str, Any],
        confidence: float,
        decision: str,
        cost_usd: float = 0.0031,  # Approximate cost per auth
    ) -> bool:
        """Store authentication result in cache."""
        if not self.config.enable_cost_optimization:
            return False

        emb_hash = self._hash_embedding(embedding)
        env_hash = self._hash_environment(environmental_context)
        cache_key = f"{user_id}_{emb_hash}_{env_hash}"

        # Cleanup old entries if cache is full
        if len(self._cache) >= self.config.max_cache_entries:
            # Remove oldest 10%
            to_remove = sorted(
                self._cache.items(),
                key=lambda x: x[1].timestamp
            )[:self.config.max_cache_entries // 10]

            for key, _ in to_remove:
                del self._cache[key]

        self._cache[cache_key] = CachedAuthResult(
            embedding_hash=emb_hash,
            user_id=user_id,
            confidence=confidence,
            decision=decision,
            timestamp=time.time(),
            environmental_hash=env_hash,
            cost_saved_usd=cost_usd,
        )

        self._total_cost_saved_usd += cost_usd
        return True

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        total_requests = self._cache_hits + self._cache_misses
        hit_rate = self._cache_hits / max(1, total_requests)

        return {
            "cache_enabled": self.config.enable_cost_optimization,
            "cache_size": len(self._cache),
            "max_cache_size": self.config.max_cache_entries,
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "hit_rate": hit_rate,
            "total_cost_saved_usd": round(self._total_cost_saved_usd, 4),
            "estimated_monthly_savings_usd": round(self._total_cost_saved_usd * 30, 2),
        }


# =============================================================================
# Progressive Confidence Communicator
# =============================================================================

class ProgressiveConfidenceCommunicator:
    """
    Generates progressive, context-aware feedback messages.

    Based on:
    - Confidence level (high, medium, low, insufficient)
    - Environmental conditions (noise, device changes)
    - Temporal context (time of day, unlock patterns)
    - Failure reason (specific actionable feedback)
    """

    def __init__(self, config: VoiceAuthEnhancementConfig):
        self.config = config

    def generate_success_message(
        self,
        user_id: str,
        confidence: float,
        level_name: str,
        environmental_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Generate success message based on confidence and context."""
        env = environmental_context or {}

        # High confidence (>95%)
        if confidence >= 0.95:
            greetings = [
                f"Of course, {user_id}.",
                f"Welcome back, {user_id}.",
                f"Good to see you, {user_id}.",
            ]
            hour = datetime.now().hour
            if 5 <= hour < 12:
                return f"Good morning, {user_id}. Unlocking for you."
            elif 12 <= hour < 18:
                return f"Good afternoon, {user_id}. Unlocking now."
            elif 18 <= hour < 22:
                return f"Good evening, {user_id}. Unlocking for you."
            else:
                return f"Up late, {user_id}? Unlocking now."

        # Good confidence (90-95%)
        elif confidence >= 0.90:
            return f"Voice verified, {user_id}. Unlocking now."

        # Acceptable confidence (85-90%)
        elif confidence >= 0.85:
            if env.get("is_noisy", False):
                return (
                    f"Got it despite the background noise, {user_id}. "
                    f"Unlocking for you."
                )
            elif env.get("microphone_changed", False):
                return (
                    f"Recognized you on the new microphone, {user_id}. "
                    f"Unlocking now."
                )
            else:
                return f"Verified. Unlocking for you, {user_id}."

        # Borderline with multi-factor help
        elif confidence >= 0.80:
            if level_name == "BEHAVIORAL_FUSION":
                return (
                    f"Your voice confidence was a bit lower ({confidence:.0%}), "
                    f"but your behavioral patterns match perfectly. "
                    f"Unlocking, {user_id}."
                )
            else:
                return f"Identity confirmed. Unlocking for you, {user_id}."

        # Lower confidence but still passed
        else:
            return f"Authentication successful. Welcome, {user_id}."

    def generate_failure_message(
        self,
        confidence: float,
        failure_reason: Optional[str] = None,
        retry_count: int = 0,
        environmental_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Generate failure message with actionable guidance."""
        env = environmental_context or {}

        # Specific failure reasons with guidance
        if failure_reason == "replay_attack":
            return (
                "Security alert: I detected characteristics consistent with "
                "a voice recording rather than live speech. "
                "Please speak live to the microphone."
            )

        elif failure_reason == "deepfake":
            return (
                "Security alert: Synthetic voice characteristics detected. "
                "Access denied."
            )

        elif failure_reason == "background_noise":
            if retry_count == 0:
                return (
                    "I'm having trouble hearing you clearly - there's background noise. "
                    "Could you try again, maybe speak louder?"
                )
            else:
                return (
                    "Still struggling with the background noise. "
                    "Can you move to a quieter location or use a different microphone?"
                )

        elif failure_reason == "microphone_change":
            return (
                "I notice you're using a different microphone. "
                "Let me recalibrate... Try saying 'unlock my screen' one more time."
            )

        elif failure_reason == "low_snr":
            return (
                "Audio quality is too low to verify securely. "
                "Please check your microphone and try again."
            )

        # Generic failure based on confidence
        if confidence < 0.30:
            return (
                "I don't recognize this voice. This device is locked to Derek. "
                "If you need access, please use the password."
            )
        elif confidence < 0.50:
            if retry_count >= 2:
                return (
                    "I'm unable to verify your voice after multiple attempts. "
                    "Please use password authentication."
                )
            else:
                return (
                    "Voice not recognized. Please try again, speaking clearly "
                    "and directly into the microphone."
                )
        elif confidence < 0.70:
            return (
                "Voice confidence too low for this action. "
                "Please try again or use password authentication."
            )
        else:
            # Borderline - encourage retry
            if retry_count == 0:
                return "Almost got it - please say that one more time."
            else:
                return (
                    "Having trouble verifying your voice. "
                    "Try password authentication if this continues."
                )

    def generate_retry_suggestion(
        self,
        failure_reason: str,
        environmental_context: Dict[str, Any],
    ) -> str:
        """Generate specific retry suggestion based on failure analysis."""
        env = environmental_context

        suggestions = {
            "background_noise": "Move to a quieter location or speak closer to the microphone.",
            "low_snr": "Increase microphone volume or reduce background noise.",
            "microphone_change": "Continue using this microphone - I'll learn it over time.",
            "voice_mismatch": "Speak clearly and at a normal pace.",
            "liveness_failed": "Speak naturally - don't use a recording.",
        }

        return suggestions.get(failure_reason, "Please try again.")


# =============================================================================
# Main Enhancement Integration
# =============================================================================

class VoiceAuthEnhancementManager:
    """
    Master integration point for all voice auth enhancements.

    Coordinates:
    - Pattern recognition (ChromaDB)
    - Audit trail (Langfuse)
    - Cost optimization (caching)
    - Progressive feedback
    """

    def __init__(self, config: Optional[VoiceAuthEnhancementConfig] = None):
        self.config = config or VoiceAuthEnhancementConfig()

        # Components
        self.pattern_store = VoicePatternStore(self.config)
        self.audit_trail = AuthenticationAuditTrail(self.config)
        self.cost_optimizer = VoiceAuthCostOptimizer(self.config)
        self.feedback_generator = ProgressiveConfidenceCommunicator(self.config)

        self._initialized = False

    async def initialize(self) -> bool:
        """Initialize all enhancement components."""
        logger.info("[EnhancementManager] Initializing components...")

        pattern_ok = await self.pattern_store.initialize()
        audit_ok = await self.audit_trail.initialize()

        self._initialized = True

        status = []
        if pattern_ok:
            status.append("✓ Pattern Recognition")
        if audit_ok:
            status.append("✓ Audit Trail")
        if self.config.enable_cost_optimization:
            status.append("✓ Cost Optimization")
        if self.config.enable_progressive_feedback:
            status.append("✓ Progressive Feedback")

        logger.info(f"[EnhancementManager] Initialized: {', '.join(status)}")
        return self._initialized

    async def pre_authentication_hook(
        self,
        audio_data: bytes,
        user_id: str,
        embedding: Optional[np.ndarray] = None,
        environmental_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Hook to run before authentication.

        Returns enrichment data to improve authentication.
        """
        enrichment = {
            "cached_result": None,
            "environmental_adjustment": None,
            "replay_risk": 0.0,
            "known_environment": False,
        }

        env = environmental_context or {}

        # Check cache if embedding provided
        if embedding is not None:
            cached = await self.cost_optimizer.check_cache(
                embedding, user_id, env
            )
            if cached:
                enrichment["cached_result"] = cached

        # Check for environmental adjustments
        if embedding is not None:
            adj = await self.pattern_store.get_environmental_adjustment(
                user_id,
                env.get("microphone", "unknown"),
                env.get("location_hash", "unknown"),
            )
            if adj is not None:
                enrichment["environmental_adjustment"] = adj
                enrichment["known_environment"] = True

        # Check for replay attack
        if embedding is not None:
            is_replay, confidence, reason = await self.pattern_store.detect_replay_attack(
                embedding, env
            )
            if is_replay:
                enrichment["replay_risk"] = confidence
                enrichment["replay_reason"] = reason

        return enrichment

    async def post_authentication_hook(
        self,
        session_id: str,
        user_id: str,
        embedding: Optional[np.ndarray],
        confidence: float,
        decision: str,
        success: bool,
        duration_ms: float,
        environmental_context: Dict[str, Any],
        details: Dict[str, Any],
    ) -> None:
        """
        Hook to run after authentication.

        Handles learning, auditing, and caching.
        """
        # Store pattern for learning
        if embedding is not None and success:
            await self.pattern_store.store_authentication_pattern(
                embedding,
                user_id,
                confidence,
                success,
                environmental_context,
                metadata={"session_id": session_id},
            )

        # Learn environmental variation
        if embedding is not None:
            await self.pattern_store.learn_environmental_variation(
                user_id,
                environmental_context.get("microphone", "unknown"),
                environmental_context.get("location_hash", "unknown"),
                voice_adjustment=confidence - 0.85,  # Adjustment from baseline
            )

        # Store in cache if successful
        if embedding is not None and success:
            await self.cost_optimizer.store_result(
                embedding,
                user_id,
                environmental_context,
                confidence,
                decision,
            )

        # Audit trail
        await self.audit_trail.trace_authentication(
            session_id,
            user_id,
            decision,
            confidence,
            duration_ms,
            {
                "success": success,
                "environmental_context": environmental_context,
                **details,
            },
        )

    def generate_feedback(
        self,
        user_id: str,
        confidence: float,
        decision: str,
        level_name: str = "PRIMARY",
        failure_reason: Optional[str] = None,
        retry_count: int = 0,
        environmental_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Generate progressive confidence feedback message."""
        if decision in ("AUTHENTICATED", "SUCCESS"):
            return self.feedback_generator.generate_success_message(
                user_id, confidence, level_name, environmental_context
            )
        else:
            return self.feedback_generator.generate_failure_message(
                confidence, failure_reason, retry_count, environmental_context
            )

    async def get_comprehensive_stats(self) -> Dict[str, Any]:
        """Get statistics from all components."""
        return {
            "pattern_store": await self.pattern_store.get_stats(),
            "cost_optimizer": self.cost_optimizer.get_stats(),
            "config": {
                "pattern_recognition": self.config.enable_pattern_recognition,
                "audit_trail": self.config.enable_audit_trail,
                "cost_optimization": self.config.enable_cost_optimization,
                "environmental_learning": self.config.enable_environmental_learning,
                "progressive_feedback": self.config.enable_progressive_feedback,
            },
        }

    async def shutdown(self) -> None:
        """Shutdown all components gracefully."""
        logger.info("[EnhancementManager] Shutting down...")
        await self.audit_trail.shutdown()


# =============================================================================
# Global singleton
# =============================================================================

_enhancement_manager: Optional[VoiceAuthEnhancementManager] = None
_enhancement_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_voice_auth_enhancements(
    force_new: bool = False,
    config: Optional[VoiceAuthEnhancementConfig] = None,
) -> VoiceAuthEnhancementManager:
    """Get or create the voice auth enhancement manager."""
    global _enhancement_manager

    async with _enhancement_lock:
        if _enhancement_manager is None or force_new:
            _enhancement_manager = VoiceAuthEnhancementManager(config)
            await _enhancement_manager.initialize()

        return _enhancement_manager


__all__ = [
    "VoiceAuthEnhancementConfig",
    "VoicePatternStore",
    "AuthenticationAuditTrail",
    "VoiceAuthCostOptimizer",
    "ProgressiveConfidenceCommunicator",
    "VoiceAuthEnhancementManager",
    "get_voice_auth_enhancements",
]
