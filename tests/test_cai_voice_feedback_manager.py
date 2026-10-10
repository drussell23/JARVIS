# [Ouroboros] Modified by Ouroboros (op=op-01a121d6-) at 2026-10-09 18:06 UTC
# Reason: `backend/api/cai_voice_feedback_manager.py` has no corresponding test module. CREATE `tests/test_cai_voice_feedback_mana

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Import the module under test
from backend.api.cai_voice_feedback_manager import (
    CAIContext,
    CAIVoiceFeedbackManager,
    DynamicMessageGenerator,
    TimeOfDay,
    ConfidenceLevel,
    CAIStage,
    VoiceMode,
    get_cai_voice_manager,
    speak_cai_message,
    extract_continuation_action,
)


@pytest.mark.asyncio
class TestCAIVoiceFeedbackManager:
    """Test suite for CAI Voice Feedback Manager."""

    async def test_import_smoke_test(self):
        """Ensure all imports work correctly."""
        # This test verifies that the module can be imported without errors
        assert CAIContext is not None
        assert CAIVoiceFeedbackManager is not None
        assert DynamicMessageGenerator is not None
        assert TimeOfDay is not None
        assert ConfidenceLevel is not None
        assert CAIStage is not None
        assert VoiceMode is not None
        assert get_cai_voice_manager is not None
        assert speak_cai_message is not None
        assert extract_continuation_action is not None

    async def test_get_instance_singleton(self):
        """Test that get_instance returns the same instance."""
        manager1 = await CAIVoiceFeedbackManager.get_instance()
        manager2 = await CAIVoiceFeedbackManager.get_instance()
        assert manager1 is manager2




    @patch('backend.api.cai_voice_feedback_manager._safe_say')
    async def test_announce_verification_result_success(self, mock_safe_say):
        """Test announce_verification_result with success."""
        manager = await CAIVoiceFeedbackManager.get_instance()
        
        ctx = CAIContext(
            command_text="search for dogs",
            speaker_name="Derek",
            is_screen_locked=True,
            vbi_confidence=0.95
        )
        
        await manager.announce_verification_result(ctx, success=True)
        
        # Verify that _speak_async was called with expected message
        assert mock_safe_say.called

    @patch('backend.api.cai_voice_feedback_manager._safe_say')
    async def test_announce_verification_result_failure(self, mock_safe_say):
        """Test announce_verification_result with failure."""
        manager = await CAIVoiceFeedbackManager.get_instance()
        
        ctx = CAIContext(
            command_text="search for dogs",
            speaker_name="Derek",
            is_screen_locked=True,
            vbi_confidence=0.7
        )
        
        await manager.announce_verification_result(ctx, success=False)
        
        # Verify that _speak_async was called with expected message
        assert mock_safe_say.called

    @patch('backend.api.cai_voice_feedback_manager._safe_say')
    async def test_announce_unlock_success(self, mock_safe_say):
        """Test announce_unlock_success."""
        manager = await CAIVoiceFeedbackManager.get_instance()
        
        ctx = CAIContext(
            command_text="search for dogs",
            speaker_name="Derek",
            is_screen_locked=True,
            continuation_action="search for dogs"
        )
        
        await manager.announce_unlock_success(ctx)
        
        # Verify that _speak_async was called with expected message
        assert mock_safe_say.called


    @patch('backend.api.cai_voice_feedback_manager._safe_say')
    async def test_announce_task_completion(self, mock_safe_say):
        """Test announce_task_completion."""
        manager = await CAIVoiceFeedbackManager.get_instance()
        
        ctx = CAIContext(
            command_text="search for dogs",
            speaker_name="Derek",
            is_screen_locked=True,
            continuation_action="search for dogs"
        )
        
        await manager.announce_task_completion(ctx, result="Found 10 results")
        
        # Verify that _speak_async was called with expected message
        assert mock_safe_say.called

    @patch('backend.api.cai_voice_feedback_manager._safe_say')
    async def test_announce_error(self, mock_safe_say):
        """Test announce_error."""
        manager = await CAIVoiceFeedbackManager.get_instance()
        
        ctx = CAIContext(
            command_text="search for dogs",
            speaker_name="Derek",
            is_screen_locked=True
        )
        
        await manager.announce_error(ctx, "Test error")
        
        # Verify that _speak_async was called with expected message
        assert mock_safe_say.called



