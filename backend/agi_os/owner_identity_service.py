"""
JARVIS AGI OS - Dynamic Owner Identity Service

Advanced dynamic user identification via voice biometrics that enables JARVIS
to recognize and personally address the macOS laptop owner without any hardcoding.

Key Features:
- Dynamic owner identification via voice biometrics
- Integration with SpeakerVerificationService
- Caching with smart invalidation
- macOS system user detection as fallback
- Multi-factor identity fusion (voice + behavioral + context)
- Async-first architecture
- Event-driven identity updates

Usage:
    from agi_os.owner_identity_service import (
        OwnerIdentityService,
        get_owner_identity,
        IdentityContext,
    )

    # Get singleton instance
    identity_service = await get_owner_identity()

    # Get current owner name (with context)
    owner = await identity_service.get_current_owner()
    print(f"Hello, {owner.name}!")

    # Verify if audio is from owner
    is_owner, confidence = await identity_service.verify_owner_voice(audio_data)
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)


class IdentitySource(Enum):
    """Source of identity information."""
    VOICE_BIOMETRIC = "voice_biometric"
    CACHED_PROFILE = "cached_profile"
    DATABASE_PROFILE = "database_profile"
    MACOS_SYSTEM = "macos_system"
    BEHAVIORAL_PATTERN = "behavioral_pattern"
    FALLBACK = "fallback"


class IdentityConfidence(Enum):
    """Confidence level in identity assertion."""
    VERIFIED = "verified"           # Voice-verified this session
    HIGH = "high"                   # Recent voice verification (<1 hour)
    MODERATE = "moderate"           # Profile-based, not voice-verified today
    LOW = "low"                     # System user or behavioral only
    UNKNOWN = "unknown"             # No identity established


@dataclass
class OwnerProfile:
    """Comprehensive owner profile."""
    name: str
    speaker_id: Optional[str] = None

    # Confidence metrics
    identity_confidence: IdentityConfidence = IdentityConfidence.UNKNOWN
    voice_confidence: float = 0.0
    behavioral_confidence: float = 0.0

    # Source tracking
    identity_source: IdentitySource = IdentitySource.FALLBACK

    # Temporal data
    last_voice_verification: Optional[datetime] = None
    last_interaction: Optional[datetime] = None
    session_start: Optional[datetime] = None

    # Profile metadata
    is_primary_user: bool = False
    security_level: str = "standard"
    total_voice_samples: int = 0

    # Personalization data
    preferences: Dict[str, Any] = field(default_factory=dict)
    nicknames: List[str] = field(default_factory=list)
    greeting_style: str = "formal"  # formal, casual, playful

    # macOS integration
    macos_username: Optional[str] = None
    macos_full_name: Optional[str] = None
    home_directory: Optional[str] = None


@dataclass
class IdentityContext:
    """Context for an identity lookup."""
    audio_data: Optional[bytes] = None
    behavioral_signals: Dict[str, Any] = field(default_factory=dict)
    require_voice_verification: bool = False
    accept_cached: bool = True
    max_cache_age_seconds: int = 3600  # 1 hour default


class OwnerIdentityService:
    """
    Dynamic owner identity service using voice biometrics.

    This service provides real-time owner identification by integrating with
    the SpeakerVerificationService. It maintains a cached identity that is
    refreshed based on voice interactions.
    """

    def __init__(self):
        """Initialize the owner identity service."""
        self._speaker_verification = None
        self._learning_db = None
        self._initialized = False

        # Current owner state
        self._current_owner: Optional[OwnerProfile] = None
        self._owner_lock = asyncio.Lock()

        # Cache management
        self._profile_cache: Dict[str, OwnerProfile] = {}
        self._cache_ttl = timedelta(hours=1)
        self._last_cache_refresh: Optional[datetime] = None

        # Event callbacks
        self._identity_change_callbacks: List[Callable] = []
        self._verification_callbacks: List[Callable] = []

        # macOS system info cache
        self._macos_user_info: Optional[Dict[str, str]] = None

        # Statistics
        self._stats = {
            'voice_verifications': 0,
            'cache_hits': 0,
            'cache_misses': 0,
            'fallback_used': 0,
            'identity_changes': 0,
        }

        logger.info("OwnerIdentityService initialized")

    async def initialize(
        self,
        speaker_verification=None,
        learning_db=None,
    ) -> None:
        """
        Initialize the service with dependencies.

        Args:
            speaker_verification: SpeakerVerificationService instance
            learning_db: JARVISLearningDatabase instance
        """
        if self._initialized:
            logger.debug("OwnerIdentityService already initialized")
            return

        self._speaker_verification = speaker_verification
        self._learning_db = learning_db

        # Load macOS system user info
        await self._load_macos_user_info()

        # Load primary owner from database if available
        await self._load_primary_owner_from_profiles()

        self._initialized = True
        logger.info(
            f"✅ OwnerIdentityService initialized - "
            f"Owner: {self._current_owner.name if self._current_owner else 'Unknown'}"
        )

    async def _load_macos_user_info(self) -> None:
        """Load macOS system user information as fallback."""
        try:
            # Get current macOS username
            username = os.environ.get('USER') or os.getlogin()

            # Get full name from macOS directory services
            try:
                result = subprocess.run(
                    ['dscl', '.', '-read', f'/Users/{username}', 'RealName'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0:
                    # Parse output: "RealName:\n Full Name"
                    lines = result.stdout.strip().split('\n')
                    if len(lines) > 1:
                        full_name = lines[1].strip()
                    else:
                        full_name = username.replace('.', ' ').title()
                else:
                    full_name = username.replace('.', ' ').title()
            except (subprocess.TimeoutExpired, FileNotFoundError):
                full_name = username.replace('.', ' ').title()

            # Get home directory
            home_dir = os.path.expanduser('~')

            self._macos_user_info = {
                'username': username,
                'full_name': full_name,
                'home_directory': home_dir,
            }

            # Extract first name for casual greeting
            first_name = full_name.split()[0] if full_name else username.title()

            logger.info(
                f"📱 macOS user info loaded: {full_name} ({username})"
            )

        except Exception as e:
            logger.warning(f"Failed to load macOS user info: {e}")
            self._macos_user_info = None

    async def _load_primary_owner_from_profiles(self) -> None:
        """Load the primary owner from speaker profiles."""
        if not self._speaker_verification:
            logger.debug("No speaker verification service - using macOS fallback")
            await self._set_fallback_owner()
            return

        try:
            # Check if speaker verification has profiles loaded
            profiles = getattr(self._speaker_verification, 'speaker_profiles', {})

            # Find primary user profile
            primary_owner = None
            for name, profile in profiles.items():
                if profile.get('is_primary_user', False):
                    primary_owner = OwnerProfile(
                        name=name,
                        speaker_id=profile.get('speaker_id'),
                        identity_confidence=IdentityConfidence.MODERATE,
                        voice_confidence=profile.get('confidence', 0.0),
                        identity_source=IdentitySource.DATABASE_PROFILE,
                        is_primary_user=True,
                        security_level=profile.get('security_level', 'standard'),
                        total_voice_samples=profile.get('total_samples', 0),
                        session_start=datetime.now(),
                    )
                    break

            if primary_owner:
                # Merge with macOS info if available
                if self._macos_user_info:
                    primary_owner.macos_username = self._macos_user_info.get('username')
                    primary_owner.macos_full_name = self._macos_user_info.get('full_name')
                    primary_owner.home_directory = self._macos_user_info.get('home_directory')

                self._current_owner = primary_owner
                self._profile_cache[primary_owner.name] = primary_owner

                logger.info(
                    f"✅ Primary owner loaded from profiles: {primary_owner.name} "
                    f"(ID: {primary_owner.speaker_id}, Samples: {primary_owner.total_voice_samples})"
                )
            else:
                logger.info("No primary user in speaker profiles - using macOS fallback")
                await self._set_fallback_owner()

        except Exception as e:
            logger.warning(f"Failed to load primary owner from profiles: {e}")
            await self._set_fallback_owner()

    async def _set_fallback_owner(self) -> None:
        """Set fallback owner from macOS system user."""
        if self._macos_user_info:
            full_name = self._macos_user_info.get('full_name', 'User')
            first_name = full_name.split()[0] if full_name else 'User'

            self._current_owner = OwnerProfile(
                name=first_name,
                identity_confidence=IdentityConfidence.LOW,
                identity_source=IdentitySource.MACOS_SYSTEM,
                macos_username=self._macos_user_info.get('username'),
                macos_full_name=full_name,
                home_directory=self._macos_user_info.get('home_directory'),
                session_start=datetime.now(),
            )

            self._stats['fallback_used'] += 1
            logger.info(f"📱 Fallback owner set: {first_name} (from macOS)")
        else:
            # Ultimate fallback
            self._current_owner = OwnerProfile(
                name="there",  # "Hello there" sounds natural
                identity_confidence=IdentityConfidence.UNKNOWN,
                identity_source=IdentitySource.FALLBACK,
                session_start=datetime.now(),
            )
            self._stats['fallback_used'] += 1
            logger.warning("⚠️ No user info available - using generic greeting")

    async def get_current_owner(
        self,
        context: Optional[IdentityContext] = None
    ) -> OwnerProfile:
        """
        Get the current owner profile.

        Args:
            context: Optional context for identity lookup

        Returns:
            OwnerProfile with current owner information
        """
        if not self._initialized:
            await self.initialize()

        context = context or IdentityContext()

        async with self._owner_lock:
            # If we have audio and voice verification is requested
            if context.audio_data and (
                context.require_voice_verification or
                not context.accept_cached
            ):
                await self._verify_and_update_owner(context.audio_data)
                self._stats['voice_verifications'] += 1

            # Check cache validity
            elif context.accept_cached and self._current_owner:
                cache_valid = self._is_cache_valid(context.max_cache_age_seconds)
                if cache_valid:
                    self._stats['cache_hits'] += 1
                else:
                    self._stats['cache_misses'] += 1
                    # Refresh from profiles
                    await self._load_primary_owner_from_profiles()

            # Update last interaction time
            if self._current_owner:
                self._current_owner.last_interaction = datetime.now()

            return self._current_owner or await self._get_fallback_owner()

    async def _get_fallback_owner(self) -> OwnerProfile:
        """Get a fallback owner profile."""
        await self._set_fallback_owner()
        return self._current_owner

    def _is_cache_valid(self, max_age_seconds: int) -> bool:
        """Check if cached identity is still valid."""
        if not self._current_owner:
            return False

        if not self._current_owner.last_voice_verification:
            # Never voice-verified, but might still be valid from profile
            if self._current_owner.identity_source == IdentitySource.DATABASE_PROFILE:
                return True  # Profile-based identity is acceptable
            return False

        age = datetime.now() - self._current_owner.last_voice_verification
        return age.total_seconds() < max_age_seconds

    async def _verify_and_update_owner(self, audio_data: bytes) -> None:
        """
        Verify speaker from audio and update owner profile.

        Args:
            audio_data: Audio bytes for verification
        """
        if not self._speaker_verification:
            logger.warning("No speaker verification service available")
            return

        try:
            # Call speaker verification service
            result = await self._speaker_verification.verify_speaker(audio_data)

            if result.get('verified') and result.get('is_owner'):
                speaker_name = result.get('speaker_name', 'Unknown')
                confidence = result.get('confidence', 0.0)

                # Update or create owner profile
                new_owner = OwnerProfile(
                    name=speaker_name,
                    speaker_id=result.get('speaker_id'),
                    identity_confidence=IdentityConfidence.VERIFIED,
                    voice_confidence=confidence,
                    identity_source=IdentitySource.VOICE_BIOMETRIC,
                    last_voice_verification=datetime.now(),
                    last_interaction=datetime.now(),
                    is_primary_user=True,
                    security_level=result.get('security_level', 'standard'),
                    session_start=self._current_owner.session_start if self._current_owner else datetime.now(),
                )

                # Merge macOS info
                if self._macos_user_info:
                    new_owner.macos_username = self._macos_user_info.get('username')
                    new_owner.macos_full_name = self._macos_user_info.get('full_name')
                    new_owner.home_directory = self._macos_user_info.get('home_directory')

                # Check for identity change
                if self._current_owner and self._current_owner.name != new_owner.name:
                    self._stats['identity_changes'] += 1
                    await self._notify_identity_change(self._current_owner, new_owner)

                self._current_owner = new_owner
                self._profile_cache[speaker_name] = new_owner

                logger.info(
                    f"🎤 Voice-verified owner: {speaker_name} "
                    f"(confidence: {confidence:.1%})"
                )

            else:
                logger.debug(
                    f"Voice verification result: verified={result.get('verified')}, "
                    f"is_owner={result.get('is_owner')}, "
                    f"confidence={result.get('confidence', 0):.1%}"
                )

        except Exception as e:
            logger.error(f"Voice verification failed: {e}", exc_info=True)

    async def verify_owner_voice(
        self,
        audio_data: bytes,
        update_profile: bool = True
    ) -> Tuple[bool, float]:
        """
        Verify if audio is from the owner.

        Args:
            audio_data: Audio bytes to verify
            update_profile: Whether to update owner profile on success

        Returns:
            Tuple of (is_owner, confidence)
        """
        if not self._speaker_verification:
            logger.warning("No speaker verification service - cannot verify voice")
            return False, 0.0

        try:
            is_owner, confidence = await self._speaker_verification.is_owner(audio_data)

            if is_owner and update_profile:
                await self._verify_and_update_owner(audio_data)

            return is_owner, confidence

        except Exception as e:
            logger.error(f"Owner voice verification failed: {e}")
            return False, 0.0

    async def get_owner_name(
        self,
        use_first_name: bool = True,
        audio_data: Optional[bytes] = None
    ) -> str:
        """
        Get the owner's name for addressing them.

        Args:
            use_first_name: If True, return first name only
            audio_data: Optional audio for verification

        Returns:
            Owner's name for greeting
        """
        context = IdentityContext(
            audio_data=audio_data,
            require_voice_verification=audio_data is not None
        )

        owner = await self.get_current_owner(context)

        if use_first_name:
            # Extract first name
            name_parts = owner.name.split()
            return name_parts[0] if name_parts else owner.name

        return owner.name

    async def get_greeting_name(self) -> str:
        """
        Get the best name to use in greetings.

        This method returns the most appropriate name for casual greetings,
        preferring verified names over fallbacks.

        Returns:
            Name suitable for greeting
        """
        owner = await self.get_current_owner()

        # If we have high confidence, use the name
        if owner.identity_confidence in [
            IdentityConfidence.VERIFIED,
            IdentityConfidence.HIGH,
            IdentityConfidence.MODERATE
        ]:
            return owner.name.split()[0]  # First name

        # For low confidence, use more generic but still personal
        if owner.macos_full_name:
            return owner.macos_full_name.split()[0]

        # Ultimate fallback
        return "there"

    async def is_owner_verified(self) -> bool:
        """Check if current owner has been voice-verified recently."""
        if not self._current_owner:
            return False

        return self._current_owner.identity_confidence == IdentityConfidence.VERIFIED

    async def get_identity_confidence(self) -> IdentityConfidence:
        """Get the current identity confidence level."""
        if not self._current_owner:
            return IdentityConfidence.UNKNOWN
        return self._current_owner.identity_confidence

    async def get_owner_stats(self) -> Dict[str, Any]:
        """Get owner identity service statistics."""
        owner = await self.get_current_owner()

        return {
            'current_owner': owner.name if owner else None,
            'identity_confidence': owner.identity_confidence.value if owner else 'unknown',
            'identity_source': owner.identity_source.value if owner else 'none',
            'last_voice_verification': (
                owner.last_voice_verification.isoformat()
                if owner and owner.last_voice_verification else None
            ),
            'service_stats': self._stats.copy(),
            'macos_user': self._macos_user_info.get('full_name') if self._macos_user_info else None,
        }

    # Event callbacks

    def on_identity_change(self, callback: Callable) -> None:
        """Register callback for identity changes."""
        self._identity_change_callbacks.append(callback)

    def on_verification(self, callback: Callable) -> None:
        """Register callback for voice verifications."""
        self._verification_callbacks.append(callback)

    async def _notify_identity_change(
        self,
        old_owner: OwnerProfile,
        new_owner: OwnerProfile
    ) -> None:
        """Notify callbacks of identity change."""
        for callback in self._identity_change_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(old_owner, new_owner)
                else:
                    callback(old_owner, new_owner)
            except Exception as e:
                logger.error(f"Identity change callback failed: {e}")

    # Connection to speaker verification service

    async def connect_speaker_verification(self, service) -> None:
        """
        Connect to speaker verification service dynamically.

        Args:
            service: SpeakerVerificationService instance
        """
        self._speaker_verification = service
        await self._load_primary_owner_from_profiles()
        logger.info("✅ Connected to SpeakerVerificationService")

    async def refresh_from_profiles(self) -> None:
        """Force refresh owner from speaker profiles."""
        await self._load_primary_owner_from_profiles()

    async def shutdown(self) -> None:
        """Clean shutdown of the identity service."""
        logger.info("🛑 Shutting down OwnerIdentityService")
        self._identity_change_callbacks.clear()
        self._verification_callbacks.clear()
        self._profile_cache.clear()


# Singleton management
_identity_service: Optional[OwnerIdentityService] = None
_identity_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_owner_identity(
    speaker_verification=None,
    learning_db=None,
) -> OwnerIdentityService:
    """
    Get the singleton OwnerIdentityService instance.

    Args:
        speaker_verification: Optional SpeakerVerificationService
        learning_db: Optional JARVISLearningDatabase

    Returns:
        OwnerIdentityService singleton
    """
    global _identity_service

    async with _identity_lock:
        if _identity_service is None:
            _identity_service = OwnerIdentityService()
            await _identity_service.initialize(
                speaker_verification=speaker_verification,
                learning_db=learning_db,
            )
        elif speaker_verification and not _identity_service._speaker_verification:
            await _identity_service.connect_speaker_verification(speaker_verification)

        return _identity_service


async def get_owner_name(audio_data: Optional[bytes] = None) -> str:
    """
    Convenience function to get the owner's name.

    Args:
        audio_data: Optional audio for verification

    Returns:
        Owner's name
    """
    service = await get_owner_identity()
    return await service.get_owner_name(audio_data=audio_data)


async def verify_is_owner(audio_data: bytes) -> Tuple[bool, float]:
    """
    Convenience function to verify if audio is from owner.

    Args:
        audio_data: Audio bytes to verify

    Returns:
        Tuple of (is_owner, confidence)
    """
    service = await get_owner_identity()
    return await service.verify_owner_voice(audio_data)


# Factory function for creating identity context
def create_identity_context(
    audio_data: Optional[bytes] = None,
    require_verification: bool = False,
    max_cache_age: int = 3600,
) -> IdentityContext:
    """
    Create an identity context for lookups.

    Args:
        audio_data: Optional audio for verification
        require_verification: Whether to require voice verification
        max_cache_age: Maximum cache age in seconds

    Returns:
        IdentityContext instance
    """
    return IdentityContext(
        audio_data=audio_data,
        require_voice_verification=require_verification,
        max_cache_age_seconds=max_cache_age,
    )
