#!/usr/bin/env python3
"""
Advanced Profile Consolidation Service for JARVIS Voice System
Handles profile merging, embedding management, and dynamic feature extraction
"""

import asyncio
import asyncpg
import numpy as np
import logging
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from datetime import datetime
import json
import hashlib
from pathlib import Path

logger = logging.getLogger(__name__)

# v132.0: TLS-Safe Connection Factory Import
# All asyncpg connections must use TLS-safe factory to prevent race conditions
_TLS_SAFE_FACTORY_AVAILABLE = False
tls_safe_connect = None

try:
    from intelligence.cloud_sql_connection_manager import tls_safe_connect as _tls_safe_connect
    tls_safe_connect = _tls_safe_connect
    _TLS_SAFE_FACTORY_AVAILABLE = True
except ImportError:
    try:
        from backend.intelligence.cloud_sql_connection_manager import tls_safe_connect as _tls_safe_connect
        tls_safe_connect = _tls_safe_connect
        _TLS_SAFE_FACTORY_AVAILABLE = True
    except ImportError:
        logger.debug("[ProfileConsolidation] TLS-safe factory not available")


@dataclass
class SpeakerProfile:
    """Enhanced speaker profile with all biometric data"""
    speaker_id: int
    speaker_name: str
    embedding: np.ndarray
    embedding_dimension: int
    acoustic_features: Dict[str, float] = field(default_factory=dict)
    total_samples: int = 0
    quality_score: float = 0.0
    is_primary: bool = False
    security_level: str = "standard"
    created_at: datetime = field(default_factory=datetime.now)
    last_verified: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class ProfileConsolidationService:
    """Service for consolidating and managing speaker profiles"""

    def __init__(self):
        self.conn: Optional[asyncpg.Connection] = None
        self.profiles_cache: Dict[int, SpeakerProfile] = {}
        self.embedding_normalizer = EmbeddingNormalizer()
        self.feature_extractor = AcousticFeatureExtractor()

    async def initialize(self, config: Optional[Dict] = None):
        """Initialize service with dynamic configuration"""
        if config:
            self.config = config
        else:
            self.config = await self._load_config_from_secret_manager()

        # Connect to database
        self.conn = await self._get_database_connection()

        # Load existing profiles
        await self._load_profiles()

    async def _load_config_from_secret_manager(self) -> Dict:
        """Load configuration from secret manager"""
        try:
            import sys
            import os
            sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
            from core.secret_manager import get_db_password

            db_password = get_db_password()
            return {
                "host": "127.0.0.1",
                "port": 5432,
                "database": "jarvis_learning",
                "user": "jarvis",
                "password": db_password,
            }
        except Exception as e:
            logger.warning(f"Failed to load from secret manager: {e}, using config file")
            try:
                from intelligence.cloud_sql_connection_manager import database_config_path
            except ImportError:
                from backend.intelligence.cloud_sql_connection_manager import database_config_path
            config_path = database_config_path()
            if config_path is not None:
                with open(config_path) as f:
                    config = json.load(f)
                    cloud_sql = config.get("cloud_sql", {})
                    return {
                        "host": cloud_sql.get("host", "127.0.0.1"),
                        "port": cloud_sql.get("port", 5432),
                        "database": cloud_sql.get("database", "jarvis_learning"),
                        "user": cloud_sql.get("user", "jarvis"),
                        "password": cloud_sql.get("password"),
                    }
            raise

    async def _get_database_connection(self) -> asyncpg.Connection:
        """
        Get database connection with retry logic.

        v132.0: Uses TLS-safe factory to prevent asyncpg TLS race conditions.
        """
        max_retries = 3

        # v132.0: Prefer TLS-safe factory to prevent race conditions
        if _TLS_SAFE_FACTORY_AVAILABLE and tls_safe_connect is not None:
            try:
                conn = await tls_safe_connect(
                    host=self.config.get("host", "127.0.0.1"),
                    port=self.config.get("port", 5432),
                    database=self.config.get("database", "jarvis_learning"),
                    user=self.config.get("user", "jarvis"),
                    password=self.config.get("password", ""),
                    timeout=30.0,
                    max_retries=max_retries,
                )
                if conn:
                    return conn
                raise RuntimeError("TLS-safe connection returned None")
            except Exception as e:
                logger.warning(f"TLS-safe connection failed: {e}, falling back to direct asyncpg")

        # Fallback to direct asyncpg (not recommended - may cause TLS race)
        logger.warning(
            "[ProfileConsolidation] TLS-safe factory not available, "
            "using direct asyncpg (may cause TLS race conditions)"
        )
        for attempt in range(max_retries):
            try:
                return await asyncpg.connect(**self.config)
            except Exception as e:
                if attempt == max_retries - 1:
                    raise
                logger.warning(f"Connection attempt {attempt + 1} failed: {e}, retrying...")
                await asyncio.sleep(2 ** attempt)

    async def _load_profiles(self):
        """Load all speaker profiles from database"""
        profiles = await self.conn.fetch("""
            SELECT * FROM speaker_profiles
            ORDER BY speaker_id
        """)

        for row in profiles:
            profile = await self._row_to_profile(row)
            self.profiles_cache[profile.speaker_id] = profile

    async def _row_to_profile(self, row: asyncpg.Record) -> SpeakerProfile:
        """Convert database row to SpeakerProfile object"""
        # Extract embedding
        embedding_bytes = row.get('voiceprint_embedding')
        if embedding_bytes:
            embedding = np.frombuffer(embedding_bytes, dtype=np.float32)
        else:
            embedding = np.array([])

        # Extract acoustic features
        acoustic_features = {}
        feature_fields = [
            'pitch_mean_hz', 'pitch_std_hz', 'pitch_range_hz',
            'formant_f1_hz', 'formant_f2_hz', 'formant_f3_hz',
            'spectral_centroid_hz', 'spectral_rolloff_hz',
            'energy_mean', 'energy_std', 'speaking_rate_wpm',
            'jitter_percent', 'shimmer_percent'
        ]

        for field in feature_fields:
            value = row.get(field)
            if value is not None:
                acoustic_features[field] = float(value)

        return SpeakerProfile(
            speaker_id=row['speaker_id'],
            speaker_name=row['speaker_name'],
            embedding=embedding,
            embedding_dimension=len(embedding),
            acoustic_features=acoustic_features,
            total_samples=row.get('total_samples', 0),
            quality_score=row.get('enrollment_quality_score', 0.0),
            is_primary=row.get('is_primary_user', False),
            security_level=row.get('security_level', 'standard'),
            created_at=row.get('created_at'),
            last_verified=row.get('last_verified'),
            metadata={
                'verification_count': row.get('verification_count', 0),
                'successful_verifications': row.get('successful_verifications', 0),
                'failed_verifications': row.get('failed_verifications', 0),
            }
        )

    async def consolidate_profiles(
        self,
        primary_name: str = "Derek J. Russell",
        auto_merge: bool = False
    ) -> Dict[str, Any]:
        """
        Consolidate duplicate speaker profiles intelligently

        Args:
            primary_name: The primary name to use for consolidated profile
            auto_merge: If True, automatically merge without confirmation

        Returns:
            Dict with consolidation results
        """
        logger.info("🔄 Starting intelligent profile consolidation...")

        # Step 1: Detect duplicate profiles
        duplicates = await self._detect_duplicate_profiles(primary_name)

        if not duplicates:
            logger.info("✅ No duplicate profiles found")
            return {"status": "no_duplicates", "profiles_checked": len(self.profiles_cache)}

        # Step 2: Analyze profiles for best features
        analysis = await self._analyze_profiles(duplicates)

        logger.info(f"📊 Analysis complete:")
        logger.info(f"   - Found {len(duplicates)} profiles to merge")
        logger.info(f"   - Best embedding: {analysis['best_embedding_profile']} "
                   f"({analysis['best_embedding_size']} dimensions)")
        logger.info(f"   - Best features: {analysis['best_features_profile']}")
        logger.info(f"   - Total samples: {analysis['total_samples']}")

        if not auto_merge:
            # In production, you might want to confirm with user
            logger.info("⚠️  Auto-merge disabled, returning analysis")
            return {"status": "analysis_only", "analysis": analysis}

        # Step 3: Merge profiles
        merged_profile = await self._merge_profiles(duplicates, analysis, primary_name)

        # Step 4: Update database
        await self._update_database_with_merged_profile(merged_profile, duplicates)

        # Step 5: Cleanup old profiles
        await self._cleanup_duplicate_profiles(duplicates, merged_profile)

        # Step 6: Verify consolidation
        verification = await self._verify_consolidation(merged_profile)

        return {
            "status": "success",
            "merged_profile_id": merged_profile.speaker_id,
            "profiles_merged": len(duplicates),
            "total_samples": merged_profile.total_samples,
            "embedding_dimension": merged_profile.embedding_dimension,
            "quality_score": merged_profile.quality_score,
            "verification": verification
        }

    async def _detect_duplicate_profiles(self, primary_name: str) -> List[SpeakerProfile]:
        """Detect duplicate profiles based on name similarity and patterns"""
        duplicates = []
        primary_name_lower = primary_name.lower()

        # Common variations to check
        name_variations = [
            primary_name_lower,
            primary_name_lower.split()[0] if ' ' in primary_name_lower else primary_name_lower,  # First name only
            primary_name_lower.replace('.', '').replace(' ', ''),  # No dots/spaces
        ]

        for profile in self.profiles_cache.values():
            profile_name_lower = profile.speaker_name.lower()

            # Check if profile name matches any variation
            for variation in name_variations:
                if variation in profile_name_lower or profile_name_lower in variation:
                    duplicates.append(profile)
                    break

        return duplicates

    async def _analyze_profiles(self, profiles: List[SpeakerProfile]) -> Dict[str, Any]:
        """Analyze profiles to determine best features to keep"""
        analysis = {
            "best_embedding_profile": None,
            "best_embedding_size": 0,
            "best_features_profile": None,
            "best_features_count": 0,
            "total_samples": 0,
            "best_quality_score": 0.0,
            "profiles": []
        }

        for profile in profiles:
            profile_info = {
                "id": profile.speaker_id,
                "name": profile.speaker_name,
                "embedding_size": profile.embedding_dimension,
                "features_count": len(profile.acoustic_features),
                "samples": profile.total_samples,
                "quality": profile.quality_score
            }
            analysis["profiles"].append(profile_info)

            # Track best embedding
            if profile.embedding_dimension > analysis["best_embedding_size"]:
                analysis["best_embedding_size"] = profile.embedding_dimension
                analysis["best_embedding_profile"] = profile.speaker_name

            # Track best features
            if len(profile.acoustic_features) > analysis["best_features_count"]:
                analysis["best_features_count"] = len(profile.acoustic_features)
                analysis["best_features_profile"] = profile.speaker_name

            # Accumulate totals
            analysis["total_samples"] += profile.total_samples
            if profile.quality_score:
                analysis["best_quality_score"] = max(analysis["best_quality_score"], profile.quality_score)

        return analysis

    async def _merge_profiles(
        self,
        profiles: List[SpeakerProfile],
        analysis: Dict[str, Any],
        primary_name: str
    ) -> SpeakerProfile:
        """Merge multiple profiles into one optimal profile"""

        # Start with the profile that has the best features
        base_profile = None
        best_embedding_profile = None

        for profile in profiles:
            if profile.speaker_name == analysis["best_features_profile"]:
                base_profile = profile
            if profile.speaker_name == analysis["best_embedding_profile"]:
                best_embedding_profile = profile

        if not base_profile:
            base_profile = profiles[0]

        # Create merged profile
        merged = SpeakerProfile(
            speaker_id=base_profile.speaker_id,  # Keep the primary ID
            speaker_name=primary_name,
            embedding=best_embedding_profile.embedding if best_embedding_profile else base_profile.embedding,
            embedding_dimension=analysis["best_embedding_size"],
            acoustic_features=base_profile.acoustic_features.copy(),
            total_samples=analysis["total_samples"],
            quality_score=analysis["best_quality_score"],
            is_primary=True,
            security_level="high",
            created_at=min(p.created_at for p in profiles if p.created_at),
            last_verified=max((p.last_verified for p in profiles if p.last_verified), default=None),
            metadata={
                "merged_from": [p.speaker_id for p in profiles],
                "merge_date": datetime.now().isoformat(),
                "merge_count": len(profiles)
            }
        )

        # Merge acoustic features (take average where both exist, keep unique ones)
        all_features = {}
        feature_counts = {}

        for profile in profiles:
            for feature_name, value in profile.acoustic_features.items():
                if feature_name not in all_features:
                    all_features[feature_name] = 0.0
                    feature_counts[feature_name] = 0
                all_features[feature_name] += value
                feature_counts[feature_name] += 1

        # Average the features
        for feature_name in all_features:
            merged.acoustic_features[feature_name] = all_features[feature_name] / feature_counts[feature_name]

        return merged

    async def _update_database_with_merged_profile(
        self,
        profile: SpeakerProfile,
        original_profiles: List[SpeakerProfile]
    ):
        """Update database with merged profile"""

        # Update the main profile
        await self.conn.execute("""
            UPDATE speaker_profiles
            SET
                speaker_name = $1,
                voiceprint_embedding = $2,
                embedding_dimension = $3,
                total_samples = $4,
                enrollment_quality_score = $5,
                is_primary_user = $6,
                security_level = $7,
                pitch_mean_hz = $8,
                pitch_std_hz = $9,
                formant_f1_hz = $10,
                formant_f2_hz = $11,
                formant_f3_hz = $12,
                spectral_centroid_hz = $13,
                energy_mean = $14,
                speaking_rate_wpm = $15,
                last_updated = NOW()
            WHERE speaker_id = $16
        """,
            profile.speaker_name,
            profile.embedding.tobytes() if profile.embedding.size > 0 else None,
            profile.embedding_dimension,
            profile.total_samples,
            profile.quality_score,
            profile.is_primary,
            profile.security_level,
            profile.acoustic_features.get('pitch_mean_hz'),
            profile.acoustic_features.get('pitch_std_hz'),
            profile.acoustic_features.get('formant_f1_hz'),
            profile.acoustic_features.get('formant_f2_hz'),
            profile.acoustic_features.get('formant_f3_hz'),
            profile.acoustic_features.get('spectral_centroid_hz'),
            profile.acoustic_features.get('energy_mean'),
            profile.acoustic_features.get('speaking_rate_wpm'),
            profile.speaker_id
        )

        # Consolidate voice samples from all profiles to the primary one
        other_ids = [p.speaker_id for p in original_profiles if p.speaker_id != profile.speaker_id]
        if other_ids:
            for other_id in other_ids:
                await self.conn.execute("""
                    UPDATE voice_samples
                    SET speaker_id = $1
                    WHERE speaker_id = $2
                """, profile.speaker_id, other_id)

        logger.info(f"✅ Updated profile {profile.speaker_id} with merged data")

    async def _cleanup_duplicate_profiles(
        self,
        original_profiles: List[SpeakerProfile],
        merged_profile: SpeakerProfile
    ):
        """Remove duplicate profiles after merging"""

        # Delete all profiles except the merged one
        profiles_to_delete = [
            p.speaker_id for p in original_profiles
            if p.speaker_id != merged_profile.speaker_id
        ]

        if profiles_to_delete:
            await self.conn.execute("""
                DELETE FROM speaker_profiles
                WHERE speaker_id = ANY($1::int[])
            """, profiles_to_delete)

            logger.info(f"🗑️ Removed {len(profiles_to_delete)} duplicate profiles")

    async def _verify_consolidation(self, profile: SpeakerProfile) -> Dict[str, Any]:
        """Verify the consolidation was successful"""

        # Check the profile in database
        db_profile = await self.conn.fetchrow("""
            SELECT
                speaker_id,
                speaker_name,
                total_samples,
                LENGTH(voiceprint_embedding) as embedding_size,
                embedding_dimension,
                is_primary_user,
                enrollment_quality_score
            FROM speaker_profiles
            WHERE speaker_id = $1
        """, profile.speaker_id)

        # Count actual samples
        sample_count = await self.conn.fetchval("""
            SELECT COUNT(*) FROM voice_samples WHERE speaker_id = $1
        """, profile.speaker_id)

        # Get sample statistics
        sample_stats = await self.conn.fetchrow("""
            SELECT
                COUNT(DISTINCT audio_hash) as unique_samples,
                COUNT(audio_data) as samples_with_audio,
                COUNT(mfcc_features) as samples_with_mfcc,
                AVG(quality_score) as avg_quality
            FROM voice_samples
            WHERE speaker_id = $1
        """, profile.speaker_id)

        return {
            "profile_exists": db_profile is not None,
            "name_correct": db_profile['speaker_name'] == profile.speaker_name if db_profile else False,
            "embedding_size": db_profile['embedding_size'] if db_profile else 0,
            "embedding_dimension": db_profile['embedding_dimension'] if db_profile else 0,
            "is_primary": db_profile['is_primary_user'] if db_profile else False,
            "total_samples": sample_count,
            "unique_samples": sample_stats['unique_samples'] if sample_stats else 0,
            "samples_with_audio": sample_stats['samples_with_audio'] if sample_stats else 0,
            "samples_with_features": sample_stats['samples_with_mfcc'] if sample_stats else 0,
            "average_quality": float(sample_stats['avg_quality']) if sample_stats and sample_stats['avg_quality'] else 0.0
        }

    async def cleanup(self):
        """Clean up resources"""
        if self.conn:
            await self.conn.close()


class EmbeddingNormalizer:
    """Normalize embeddings across different dimensions"""

    def normalize(self, embedding: np.ndarray, target_dim: Optional[int] = None) -> np.ndarray:
        """Normalize embedding to unit vector and optionally resize"""
        # L2 normalization
        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding = embedding / norm

        # Resize if needed
        if target_dim and len(embedding) != target_dim:
            if len(embedding) > target_dim:
                # Truncate
                embedding = embedding[:target_dim]
            else:
                # Pad with zeros
                padding = np.zeros(target_dim - len(embedding))
                embedding = np.concatenate([embedding, padding])

        return embedding


class AcousticFeatureExtractor:
    """Extract acoustic features from audio samples"""

    async def extract_from_samples(self, conn: asyncpg.Connection, speaker_id: int) -> Dict[str, float]:
        """Extract aggregate acoustic features from voice samples"""

        features = await conn.fetchrow("""
            SELECT
                AVG(pitch_mean) as pitch_mean_hz,
                AVG(pitch_std) as pitch_std_hz,
                AVG(energy_mean) as energy_mean,
                AVG(duration_ms) as avg_duration_ms,
                COUNT(*) as sample_count
            FROM voice_samples
            WHERE speaker_id = $1
                AND pitch_mean IS NOT NULL
        """, speaker_id)

        if features and features['pitch_mean_hz']:
            return {
                'pitch_mean_hz': float(features['pitch_mean_hz']),
                'pitch_std_hz': float(features['pitch_std_hz']) if features['pitch_std_hz'] else 0.0,
                'energy_mean': float(features['energy_mean']) if features['energy_mean'] else 0.0,
                'avg_duration_ms': float(features['avg_duration_ms']) if features['avg_duration_ms'] else 0.0,
                'sample_count': int(features['sample_count'])
            }

        return {}


async def main():
    """Run profile consolidation"""
    service = ProfileConsolidationService()

    try:
        await service.initialize()

        logger.info("=" * 80)
        logger.info("INTELLIGENT PROFILE CONSOLIDATION SERVICE")
        logger.info("=" * 80)

        # Run consolidation
        result = await service.consolidate_profiles(
            primary_name="Derek J. Russell",
            auto_merge=True  # Set to True to actually merge
        )

        logger.info("\n" + "=" * 80)
        logger.info("CONSOLIDATION COMPLETE")
        logger.info("=" * 80)
        logger.info(f"Status: {result['status']}")

        if result['status'] == 'success':
            logger.info(f"✅ Merged {result['profiles_merged']} profiles")
            logger.info(f"✅ Profile ID: {result['merged_profile_id']}")
            logger.info(f"✅ Total Samples: {result['total_samples']}")
            logger.info(f"✅ Embedding Dimension: {result['embedding_dimension']}")
            logger.info(f"✅ Quality Score: {result['quality_score']:.2f}")

            verification = result['verification']
            logger.info("\n📊 Verification:")
            logger.info(f"   Profile exists: {verification['profile_exists']}")
            logger.info(f"   Embedding size: {verification['embedding_size']} bytes")
            logger.info(f"   Samples with audio: {verification['samples_with_audio']}")
            logger.info(f"   Average quality: {verification['average_quality']:.2f}")

    finally:
        await service.cleanup()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    asyncio.run(main())