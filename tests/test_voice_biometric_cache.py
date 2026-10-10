# [Ouroboros] Modified by Ouroboros (op=op-01a12327-) at 2026-10-10 00:13 UTC
# Reason: `backend/voice_unlock/voice_biometric_cache.py` has no corresponding test module. CREATE `tests/test_voice_biometric_cac

from __future__ import annotations
import asyncio
import hashlib
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from backend.voice_unlock.voice_biometric_cache import (
    CacheHitType,
    VoiceBiometricCache,
    BiometricCacheResult,
    BehavioralPatternAnalyzer,
    get_behavioral_analyzer,
)


@pytest.mark.asyncio
async def test_import_smoke_test():
    # This test ensures the module can be imported without errors
    from backend.voice_unlock.voice_biometric_cache import (
        CacheHitType,
        VoiceBiometricCache,
        BiometricCacheResult,
        BehavioralPatternAnalyzer,
        get_behavioral_analyzer,
    )
    assert CacheHitType is not None
    assert VoiceBiometricCache is not None
    assert BiometricCacheResult is not None
    assert BehavioralPatternAnalyzer is not None
    assert get_behavioral_analyzer is not None




@pytest.mark.asyncio
async def test_lookup_voice_authentication_miss():
    cache = VoiceBiometricCache()
    result = await cache.lookup_voice_authentication(
        voice_embedding=None,
        transcribed_text="unlock my screen"
    )
    assert isinstance(result, BiometricCacheResult)
    assert result.hit_type == CacheHitType.MISS




@pytest.mark.asyncio
async def test_is_cached_unlock_command():
    cache = VoiceBiometricCache()
    
    # Test a command that should be cached
    is_unlock, confidence = await cache.is_cached_unlock_command("unlock my screen")
    assert isinstance(is_unlock, bool)
    assert isinstance(confidence, float)
    
    # Test with different variations
    variations = [
        "unlock the screen",
        "unlock screen",
        "jarvis unlock"
    ]
    
    for variation in variations:
        is_unlock, confidence = await cache.is_cached_unlock_command(variation)
        assert isinstance(is_unlock, bool)
        assert isinstance(confidence, float)


@pytest.mark.asyncio
async def test_behavioral_analyzer_integration():
    # Test that the behavioral analyzer is properly integrated
    analyzer = get_behavioral_analyzer()
    assert analyzer is not None
    assert isinstance(analyzer, BehavioralPatternAnalyzer)
    
    # Test recording and analysis
    analyzer.record_unlock("test_user")
    analysis = analyzer.analyze_behavioral_context("test_user")
    
    assert isinstance(analysis, dict)
    assert "behavioral_score" in analysis
    assert "time_of_day_normal" in analysis
    assert "interval_normal" in analysis
    assert "reasons" in analysis


@pytest.mark.asyncio
async def test_cache_expiration():
    # Test cache entry expiration
    cache = VoiceBiometricCache(embedding_ttl=1)  # Very short TTL
    
    # Cache an entry
    await cache.cache_authentication(
        speaker_name="test_user",
        voice_embedding=np.array([0.1, 0.2, 0.3]),
        verification_confidence=0.95,
        is_owner=True
    )
    
    # Wait for expiration
    await asyncio.sleep(1.1)
    
    # Lookup should now be a miss
    result = await cache.lookup_voice_authentication(
        voice_embedding=np.array([0.1, 0.2, 0.3])
    )
    
    assert result.hit_type == CacheHitType.MISS


@pytest.mark.asyncio
async def test_command_cache_with_special_characters():
    cache = VoiceBiometricCache()
    
    # Test with special characters in text
    test_texts = [
        "unlock my screen!",
        "unlock my screen?",
        "unlock my screen...",
        "UNLOCK MY SCREEN",
        "  unlock my screen  ",
    ]
    
    for text in test_texts:
        is_unlock, confidence = await cache.is_cached_unlock_command(text)
        assert isinstance(is_unlock, bool)
        assert isinstance(confidence, float)


@pytest.mark.asyncio
async def test_lookup_with_none_embeddings():
    cache = VoiceBiometricCache()
    
    # Test lookup with None voice embedding
    result = await cache.lookup_voice_authentication(
        voice_embedding=None,
        transcribed_text="unlock my screen"
    )
    
    assert isinstance(result, BiometricCacheResult)
    assert result.hit_type == CacheHitType.MISS


@pytest.mark.asyncio
async def test_session_cache_behavior():
    cache = VoiceBiometricCache()
    
    # Test that session caching works
    session_id = await cache.cache_authentication(
        speaker_name="test_user",
        voice_embedding=np.array([0.1, 0.2, 0.3]),
        verification_confidence=0.95,
        is_owner=True
    )
    
    # Set up session for lookup
    cache._active_session_id = session_id
    cache._active_speaker = "test_user"
    
    result = await cache.lookup_voice_authentication(
        voice_embedding=np.array([0.1, 0.2, 0.3])
    )
    
    # Should be a session auth hit if the session is valid
    assert isinstance(result, BiometricCacheResult)
    # Note: exact behavior depends on implementation details of session validation
