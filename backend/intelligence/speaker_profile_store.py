#!/usr/bin/env python3
"""
Speaker Profile Store - Lightweight, Dependency-Free Voice Profile Access
=========================================================================

This module provides a lightweight interface to access voice profiles stored
in SQLite without requiring numpy, torch, or other heavy dependencies.

DESIGN PHILOSOPHY:
- Zero external dependencies beyond Python stdlib
- Works with system Python (no virtual environment required for diagnostics)
- Fast startup for diagnostic and test scripts
- Thread-safe for concurrent access
- Async and sync interfaces
- Fully dynamic - all paths from environment variables

USE CASES:
1. Diagnostic scripts checking profile validity
2. Test scripts verifying speaker verification
3. Lightweight profile queries in constrained environments
4. Backup/export utilities

For full voice recognition with ML models, use:
    from intelligence.learning_database import get_learning_database

Author: JARVIS System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import sqlite3
import struct
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


# =============================================================================
# CONFIGURATION
# =============================================================================

def _get_env(key: str, default: str) -> str:
    """Get environment variable with default."""
    return os.environ.get(key, default)


def _get_default_db_path() -> str:
    """Get the default database path from environment or standard location."""
    # Check environment variable first
    env_path = os.environ.get("JARVIS_LEARNING_DB_PATH")
    if env_path:
        return os.path.expanduser(env_path)
    
    # Standard location
    jarvis_dir = os.path.expanduser(
        _get_env("JARVIS_DATA_DIR", "~/.jarvis")
    )
    return os.path.join(jarvis_dir, "learning", "jarvis_learning.db")


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class SpeakerProfile:
    """
    Speaker profile data structure.
    
    This is a lightweight representation that stores embeddings as Python lists
    instead of numpy arrays, making it compatible with environments where numpy
    isn't installed.
    """
    speaker_name: str
    embedding: List[float]  # Python list, not numpy array
    embedding_dimension: int = 192
    total_samples: int = 0
    recognition_confidence: float = 0.0
    is_primary_user: bool = False
    
    # Optional acoustic features
    pitch_mean_hz: Optional[float] = None
    pitch_std_hz: Optional[float] = None
    formant_f1_hz: Optional[float] = None
    formant_f2_hz: Optional[float] = None
    spectral_centroid_hz: Optional[float] = None
    speaking_rate_wpm: Optional[float] = None
    energy_mean: Optional[float] = None
    
    # Timestamps
    created_at: Optional[str] = None
    last_updated: Optional[str] = None
    
    # Source tracking
    source: str = "sqlite"
    
    @property
    def is_valid(self) -> bool:
        """Check if profile has a valid embedding."""
        return (
            self.embedding is not None and
            len(self.embedding) >= 50 and
            self.embedding_dimension == len(self.embedding)
        )
    
    @property
    def embedding_norm(self) -> float:
        """Calculate L2 norm of embedding."""
        if not self.embedding:
            return 0.0
        return math.sqrt(sum(x * x for x in self.embedding))
    
    @property
    def is_normalized(self) -> bool:
        """Check if embedding is L2 normalized (norm ≈ 1.0)."""
        return abs(self.embedding_norm - 1.0) < 0.01
    
    def cosine_similarity(self, other_embedding: List[float]) -> float:
        """
        Calculate cosine similarity with another embedding.
        
        Args:
            other_embedding: List of floats representing another embedding
            
        Returns:
            Cosine similarity in range [-1, 1]
        """
        if not self.embedding or not other_embedding:
            return 0.0
        
        if len(self.embedding) != len(other_embedding):
            logger.warning(
                f"Embedding dimension mismatch: {len(self.embedding)} vs {len(other_embedding)}"
            )
            return 0.0
        
        # Calculate dot product
        dot_product = sum(a * b for a, b in zip(self.embedding, other_embedding))
        
        # Calculate norms
        norm_a = math.sqrt(sum(x * x for x in self.embedding))
        norm_b = math.sqrt(sum(x * x for x in other_embedding))
        
        if norm_a < 1e-10 or norm_b < 1e-10:
            return 0.0
        
        return dot_product / (norm_a * norm_b)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "speaker_name": self.speaker_name,
            "embedding": self.embedding,
            "embedding_dimension": self.embedding_dimension,
            "total_samples": self.total_samples,
            "recognition_confidence": self.recognition_confidence,
            "is_primary_user": self.is_primary_user,
            "pitch_mean_hz": self.pitch_mean_hz,
            "pitch_std_hz": self.pitch_std_hz,
            "formant_f1_hz": self.formant_f1_hz,
            "formant_f2_hz": self.formant_f2_hz,
            "spectral_centroid_hz": self.spectral_centroid_hz,
            "speaking_rate_wpm": self.speaking_rate_wpm,
            "energy_mean": self.energy_mean,
            "created_at": self.created_at,
            "last_updated": self.last_updated,
            "source": self.source,
            "is_valid": self.is_valid,
            "embedding_norm": self.embedding_norm,
            "is_normalized": self.is_normalized,
        }
    
    @classmethod
    def from_sqlite_row(cls, row: sqlite3.Row) -> 'SpeakerProfile':
        """
        Create SpeakerProfile from SQLite row.
        
        Args:
            row: SQLite row with profile data
            
        Returns:
            SpeakerProfile instance
        """
        # Helper to safely get column value (sqlite3.Row doesn't have .get())
        def safe_get(key: str, default=None):
            try:
                val = row[key]
                return val if val is not None else default
            except (KeyError, IndexError):
                return default
        
        # Extract embedding bytes and convert to list of floats
        embedding_bytes = row["voiceprint_embedding"]
        embedding = []
        
        if embedding_bytes:
            num_floats = len(embedding_bytes) // 4
            embedding = list(struct.unpack(f'<{num_floats}f', embedding_bytes))
        
        return cls(
            speaker_name=row["speaker_name"],
            embedding=embedding,
            embedding_dimension=safe_get("embedding_dimension", len(embedding)),
            total_samples=safe_get("total_samples", 0),
            recognition_confidence=safe_get("recognition_confidence", 0.0),
            is_primary_user=bool(safe_get("is_primary_user", False)),
            pitch_mean_hz=safe_get("pitch_mean_hz"),
            pitch_std_hz=safe_get("pitch_std_hz"),
            formant_f1_hz=safe_get("formant_f1_hz"),
            formant_f2_hz=safe_get("formant_f2_hz"),
            spectral_centroid_hz=safe_get("spectral_centroid_hz"),
            speaking_rate_wpm=safe_get("speaking_rate_wpm"),
            energy_mean=safe_get("energy_mean"),
            created_at=safe_get("created_at"),
            last_updated=safe_get("last_updated"),
            source="sqlite",
        )


# =============================================================================
# SPEAKER PROFILE STORE
# =============================================================================

class SpeakerProfileStore:
    """
    Lightweight speaker profile storage interface.
    
    Provides thread-safe access to speaker profiles stored in SQLite
    without requiring numpy or other heavy dependencies.
    
    Thread-safe and supports both sync and async operations.
    """
    
    _instance: Optional['SpeakerProfileStore'] = None
    _lock = threading.Lock()
    
    def __new__(cls, db_path: Optional[str] = None):
        """Singleton pattern for global access."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance
    
    def __init__(self, db_path: Optional[str] = None):
        """
        Initialize the speaker profile store.
        
        Args:
            db_path: Path to SQLite database. If None, uses default location.
        """
        if self._initialized:
            return
        
        self._db_path = db_path or _get_default_db_path()
        self._thread_local = threading.local()
        self._profiles_cache: Dict[str, SpeakerProfile] = {}
        self._cache_lock = threading.RLock()
        self._cache_timestamp: Optional[datetime] = None
        self._cache_ttl_seconds = int(os.environ.get("PROFILE_CACHE_TTL", "300"))
        
        self._initialized = True
        logger.debug(f"SpeakerProfileStore initialized with db_path={self._db_path}")
    
    @property
    def db_path(self) -> str:
        """Get the database path."""
        return self._db_path
    
    @property
    def db_exists(self) -> bool:
        """Check if the database file exists."""
        return os.path.exists(self._db_path)
    
    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._thread_local, 'connection') or self._thread_local.connection is None:
            if not self.db_exists:
                raise FileNotFoundError(f"Database not found: {self._db_path}")
            
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._thread_local.connection = conn
        
        return self._thread_local.connection
    
    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Context manager for database connection."""
        conn = self._get_connection()
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
    
    def _is_cache_valid(self) -> bool:
        """Check if the profile cache is still valid."""
        if not self._cache_timestamp:
            return False
        
        age = (datetime.now() - self._cache_timestamp).total_seconds()
        return age < self._cache_ttl_seconds
    
    def get_all_profiles(self, use_cache: bool = True) -> List[SpeakerProfile]:
        """
        Get all speaker profiles from the database.
        
        Args:
            use_cache: Whether to use cached profiles if available
            
        Returns:
            List of SpeakerProfile objects
        """
        # Check cache
        if use_cache and self._is_cache_valid():
            with self._cache_lock:
                return list(self._profiles_cache.values())
        
        profiles = []
        
        try:
            with self._connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT 
                        speaker_name,
                        voiceprint_embedding,
                        embedding_dimension,
                        total_samples,
                        recognition_confidence,
                        is_primary_user,
                        pitch_mean_hz,
                        pitch_std_hz,
                        formant_f1_hz,
                        formant_f2_hz,
                        spectral_centroid_hz,
                        speaking_rate_wpm,
                        energy_mean,
                        created_at,
                        last_updated
                    FROM speaker_profiles
                    WHERE voiceprint_embedding IS NOT NULL
                    ORDER BY is_primary_user DESC, last_updated DESC
                """)
                
                rows = cursor.fetchall()
                
                for row in rows:
                    try:
                        profile = SpeakerProfile.from_sqlite_row(row)
                        if profile.is_valid:
                            profiles.append(profile)
                        else:
                            logger.debug(
                                f"Skipping invalid profile: {profile.speaker_name} "
                                f"(dim={len(profile.embedding)})"
                            )
                    except Exception as e:
                        logger.warning(f"Failed to parse profile row: {e}")
                
                # Update cache
                with self._cache_lock:
                    self._profiles_cache = {p.speaker_name: p for p in profiles}
                    self._cache_timestamp = datetime.now()
                
        except FileNotFoundError:
            logger.error(f"Database not found: {self._db_path}")
        except sqlite3.Error as e:
            logger.error(f"SQLite error: {e}")
        except Exception as e:
            logger.error(f"Error loading profiles: {e}")
        
        return profiles
    
    def get_profile(self, speaker_name: str, use_cache: bool = True) -> Optional[SpeakerProfile]:
        """
        Get a specific speaker profile by name.
        
        Args:
            speaker_name: Name of the speaker
            use_cache: Whether to use cached profile if available
            
        Returns:
            SpeakerProfile or None if not found
        """
        # Check cache first
        if use_cache and self._is_cache_valid():
            with self._cache_lock:
                if speaker_name in self._profiles_cache:
                    return self._profiles_cache[speaker_name]
        
        try:
            with self._connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT 
                        speaker_name,
                        voiceprint_embedding,
                        embedding_dimension,
                        total_samples,
                        recognition_confidence,
                        is_primary_user,
                        pitch_mean_hz,
                        pitch_std_hz,
                        formant_f1_hz,
                        formant_f2_hz,
                        spectral_centroid_hz,
                        speaking_rate_wpm,
                        energy_mean,
                        created_at,
                        last_updated
                    FROM speaker_profiles
                    WHERE speaker_name = ? AND voiceprint_embedding IS NOT NULL
                """, (speaker_name,))
                
                row = cursor.fetchone()
                
                if row:
                    profile = SpeakerProfile.from_sqlite_row(row)
                    
                    # Update cache
                    with self._cache_lock:
                        self._profiles_cache[speaker_name] = profile
                    
                    return profile
                
        except Exception as e:
            logger.error(f"Error getting profile '{speaker_name}': {e}")
        
        return None
    
    def get_primary_user(self) -> Optional[SpeakerProfile]:
        """
        Get the primary user's profile.
        
        Returns:
            SpeakerProfile of primary user or None
        """
        profiles = self.get_all_profiles()
        
        for profile in profiles:
            if profile.is_primary_user:
                return profile
        
        # Return first profile if no primary set
        return profiles[0] if profiles else None
    
    def verify_speaker(
        self,
        test_embedding: List[float],
        threshold: float = 0.7
    ) -> Tuple[Optional[str], float]:
        """
        Verify a speaker against stored profiles.
        
        Args:
            test_embedding: The embedding to verify
            threshold: Minimum similarity threshold for match
            
        Returns:
            Tuple of (speaker_name, similarity) or (None, 0.0) if no match
        """
        best_match: Optional[str] = None
        best_similarity: float = 0.0
        
        profiles = self.get_all_profiles()
        
        for profile in profiles:
            similarity = profile.cosine_similarity(test_embedding)
            
            if similarity > best_similarity:
                best_similarity = similarity
                best_match = profile.speaker_name
        
        if best_similarity >= threshold:
            return best_match, best_similarity
        
        return None, best_similarity
    
    def invalidate_cache(self):
        """Invalidate the profile cache."""
        with self._cache_lock:
            self._profiles_cache.clear()
            self._cache_timestamp = None
    
    def get_database_info(self) -> Dict[str, Any]:
        """
        Get database information and statistics.
        
        Returns:
            Dictionary with database info
        """
        info = {
            "db_path": self._db_path,
            "db_exists": self.db_exists,
            "profile_count": 0,
            "primary_user": None,
            "cache_valid": self._is_cache_valid(),
            "cache_size": len(self._profiles_cache),
        }
        
        if not self.db_exists:
            return info
        
        try:
            profiles = self.get_all_profiles()
            info["profile_count"] = len(profiles)
            
            for profile in profiles:
                if profile.is_primary_user:
                    info["primary_user"] = {
                        "name": profile.speaker_name,
                        "embedding_dim": profile.embedding_dimension,
                        "confidence": profile.recognition_confidence,
                        "is_normalized": profile.is_normalized,
                    }
                    break
            
            # Database file size
            info["db_size_bytes"] = os.path.getsize(self._db_path)
            info["db_size_kb"] = info["db_size_bytes"] / 1024
            
        except Exception as e:
            info["error"] = str(e)
        
        return info
    
    # =========================================================================
    # ASYNC INTERFACE
    # =========================================================================
    
    async def get_all_profiles_async(self, use_cache: bool = True) -> List[SpeakerProfile]:
        """Async version of get_all_profiles."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.get_all_profiles, use_cache)
    
    async def get_profile_async(
        self, speaker_name: str, use_cache: bool = True
    ) -> Optional[SpeakerProfile]:
        """Async version of get_profile."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.get_profile, speaker_name, use_cache)
    
    async def verify_speaker_async(
        self,
        test_embedding: List[float],
        threshold: float = 0.7
    ) -> Tuple[Optional[str], float]:
        """Async version of verify_speaker."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.verify_speaker, test_embedding, threshold)


# =============================================================================
# GLOBAL ACCESS FUNCTIONS
# =============================================================================

_store_instance: Optional[SpeakerProfileStore] = None


def get_speaker_profile_store(db_path: Optional[str] = None) -> SpeakerProfileStore:
    """
    Get the global SpeakerProfileStore instance.
    
    Args:
        db_path: Optional custom database path
        
    Returns:
        SpeakerProfileStore singleton instance
    """
    global _store_instance
    if _store_instance is None:
        _store_instance = SpeakerProfileStore(db_path)
    return _store_instance


def get_all_speaker_profiles(
    db_path: Optional[str] = None,
    use_cache: bool = True
) -> List[SpeakerProfile]:
    """
    Convenience function to get all speaker profiles.
    
    Args:
        db_path: Optional custom database path
        use_cache: Whether to use cache
        
    Returns:
        List of SpeakerProfile objects
    """
    store = get_speaker_profile_store(db_path)
    return store.get_all_profiles(use_cache)


def get_speaker_profile(
    speaker_name: str,
    db_path: Optional[str] = None
) -> Optional[SpeakerProfile]:
    """
    Convenience function to get a specific speaker profile.
    
    Args:
        speaker_name: Name of the speaker
        db_path: Optional custom database path
        
    Returns:
        SpeakerProfile or None
    """
    store = get_speaker_profile_store(db_path)
    return store.get_profile(speaker_name)


def get_primary_user_profile(db_path: Optional[str] = None) -> Optional[SpeakerProfile]:
    """
    Convenience function to get the primary user's profile.
    
    Args:
        db_path: Optional custom database path
        
    Returns:
        SpeakerProfile or None
    """
    store = get_speaker_profile_store(db_path)
    return store.get_primary_user()


async def get_all_speaker_profiles_async(
    db_path: Optional[str] = None,
    use_cache: bool = True
) -> List[SpeakerProfile]:
    """
    Async convenience function to get all speaker profiles.
    """
    store = get_speaker_profile_store(db_path)
    return await store.get_all_profiles_async(use_cache)


# =============================================================================
# CLI INTERFACE
# =============================================================================

def main():
    """CLI interface for testing/diagnostics."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Speaker Profile Store - View and verify voice profiles"
    )
    parser.add_argument(
        "--db-path",
        help="Path to SQLite database"
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all profiles"
    )
    parser.add_argument(
        "--info",
        action="store_true",
        help="Show database info"
    )
    parser.add_argument(
        "--profile",
        help="Get specific profile by name"
    )
    parser.add_argument(
        "--primary",
        action="store_true",
        help="Get primary user profile"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON"
    )
    
    args = parser.parse_args()
    
    import json
    
    store = get_speaker_profile_store(args.db_path)
    
    def output(data):
        if args.json:
            print(json.dumps(data, indent=2, default=str))
        else:
            for key, value in data.items():
                print(f"  {key}: {value}")
    
    if args.info:
        print("\n📊 Database Info:")
        output(store.get_database_info())
    
    elif args.list:
        profiles = store.get_all_profiles()
        print(f"\n📋 Found {len(profiles)} profile(s):\n")
        
        for profile in profiles:
            owner_tag = " [OWNER]" if profile.is_primary_user else ""
            valid_tag = "✅" if profile.is_valid else "❌"
            norm_tag = "(normalized)" if profile.is_normalized else "(NOT normalized)"
            
            print(f"{valid_tag} {profile.speaker_name}{owner_tag}")
            print(f"   Dimension: {profile.embedding_dimension} {norm_tag}")
            print(f"   Confidence: {profile.recognition_confidence:.1%}")
            print(f"   Samples: {profile.total_samples}")
            print()
    
    elif args.profile:
        profile = store.get_profile(args.profile)
        if profile:
            print(f"\n📋 Profile: {args.profile}\n")
            output(profile.to_dict())
        else:
            print(f"❌ Profile not found: {args.profile}")
    
    elif args.primary:
        profile = store.get_primary_user()
        if profile:
            print(f"\n📋 Primary User: {profile.speaker_name}\n")
            output(profile.to_dict())
        else:
            print("❌ No primary user found")
    
    else:
        # Default: show summary
        info = store.get_database_info()
        print("\n🔐 Speaker Profile Store")
        print("=" * 50)
        print(f"Database: {info['db_path']}")
        print(f"Exists: {info['db_exists']}")
        print(f"Profiles: {info['profile_count']}")
        
        if info.get('primary_user'):
            pu = info['primary_user']
            print(f"Primary User: {pu['name']} (dim={pu['embedding_dim']})")
        
        print()


if __name__ == "__main__":
    main()
