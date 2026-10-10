# [Ouroboros] Modified by Ouroboros (op=op-01a12230-) at 2026-10-09 19:46 UTC
# Reason: `backend/api/simple_unlock_handler.py` has no corresponding test module. CREATE `tests/test_simple_unlock_handler.py` co

import asyncio
from unittest.mock import MagicMock, patch, AsyncMock
import pytest
from backend.api.simple_unlock_handler import (
    handle_unlock_command,
    preload_unlock_dependencies,
    _get_owner_name,
    _get_cached_speaker_service,
    _analyze_unlock_command,
    _execute_screen_action,
    _generate_contextual_response,
)


class TestSimpleUnlockHandler:
    def test_import_smoke(self):
        """Test that the module can be imported without errors"""
        assert handle_unlock_command is not None
        assert preload_unlock_dependencies is not None

    def test_analyze_unlock_command_valid_unlock(self):
        """Test _analyze_unlock_command with valid unlock command"""
        result = asyncio.run(_analyze_unlock_command("unlock my screen"))
        assert result["is_valid"] is True
        assert result["action"] == "unlock_screen"

    def test_analyze_unlock_command_valid_lock(self):
        """Test _analyze_unlock_command with valid lock command"""
        result = asyncio.run(_analyze_unlock_command("lock my screen"))
        assert result["is_valid"] is True
        assert result["action"] == "lock_screen"

    def test_analyze_unlock_command_invalid(self):
        """Test _analyze_unlock_command with invalid command"""
        result = asyncio.run(_analyze_unlock_command("please do something"))
        assert result["is_valid"] is False
        assert "no_valid_intent_detected" in result["context"]["reason"]

    def test_generate_contextual_response_unlocked(self):
        """Test _generate_contextual_response when screen is already unlocked"""
        context = {"screen_state": "unlocked", "verified_speaker_name": "John"}
        response = asyncio.run(_generate_contextual_response("unlock_screen", context, None))
        assert "already unlocked" in response
        assert "John" in response

    def test_generate_contextual_response_urgent(self):
        """Test _generate_contextual_response with urgent command"""
        context = {"urgency": "high", "verified_speaker_name": "Alice"}
        response = asyncio.run(_generate_contextual_response("unlock_screen", context, None))
        assert "Right away" in response
        assert "Alice" in response

    def test_generate_contextual_response_normal(self):
        """Test _generate_contextual_response with normal command"""
        context = {"urgency": "normal", "verified_speaker_name": "Bob"}
        response = asyncio.run(_generate_contextual_response("unlock_screen", context, None))
        assert "Of course" in response
        assert "Bob" in response



    @pytest.mark.asyncio
    async def test_get_owner_name_cache(self):
        """Test _get_owner_name with caching"""
        # First call - should populate cache
        with patch("intelligence.learning_database.get_learning_database", AsyncMock()) as mock_db:
            mock_db.return_value.get_all_speaker_profiles = AsyncMock(return_value=[{"is_primary_user": True, "speaker_name": "User"}])
            result1 = await _get_owner_name()
            
            # Second call - should use cache
            result2 = await _get_owner_name()
            assert result1 == result2

    @pytest.mark.asyncio
    async def test_get_cached_speaker_service_cache(self):
        """Test _get_cached_speaker_service with caching"""
        # First call - should populate cache
        with patch("voice.speaker_verification_service.get_speaker_verification_service", AsyncMock()) as mock_service:
            mock_service.return_value = "mock_service"
            result1 = await _get_cached_speaker_service()
            
            # Second call - should use cache
            result2 = await _get_cached_speaker_service()
            assert result1 == result2

    @pytest.mark.asyncio
    async def test_preload_unlock_dependencies(self):
        """Test preload_unlock_dependencies function"""
        await preload_unlock_dependencies()
        # Should not raise any exceptions
        assert True
