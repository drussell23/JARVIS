# [Ouroboros] Modified by Ouroboros (op=op-01a1233b-) at 2026-10-10 00:36 UTC
# Reason: `backend/voice_unlock/setup_voice_unlock.py` has no corresponding test module. CREATE `tests/test_setup_voice_unlock.py`

from unittest.mock import patch, MagicMock
import json
import os
from pathlib import Path

# Import the function we're testing
from backend.voice_unlock.setup_voice_unlock import setup_voice_unlock


class TestSetupVoiceUnlock:
    def test_import_smoke_test(self):
        """Test that the module can be imported without errors."""
        # This test ensures no syntax errors or import issues
        assert callable(setup_voice_unlock)


    @patch('backend.voice_unlock.setup_voice_unlock.input')
    @patch('backend.voice_unlock.setup_voice_unlock.getpass.getpass')
    @patch('backend.voice_unlock.setup_voice_unlock.subprocess.run')
    async def test_setup_cancelled_by_user(self, mock_subprocess_run, mock_getpass, mock_input):
        """Test that setup is cancelled when user doesn't confirm."""
        mock_input.return_value = 'no'
        
        # Call the function
        result = setup_voice_unlock()
        
        # Verify it returns early
        assert result is None

    @patch('backend.voice_unlock.setup_voice_unlock.input')
    @patch('backend.voice_unlock.setup_voice_unlock.getpass.getpass')
    @patch('backend.voice_unlock.setup_voice_unlock.subprocess.run')
    async def test_password_mismatch_cancelled(self, mock_subprocess_run, mock_getpass, mock_input):
        """Test that setup is cancelled when passwords don't match."""
        mock_input.return_value = 'yes'
        mock_getpass.side_effect = ['password123', 'different_password']
        
        # Call the function
        result = setup_voice_unlock()
        
        # Verify it returns early
        assert result is None

    @patch('backend.voice_unlock.setup_voice_unlock.input')
    @patch('backend.voice_unlock.setup_voice_unlock.getpass.getpass')
    @patch('backend.voice_unlock.setup_voice_unlock.subprocess.run')
    async def test_keychain_store_failure(self, mock_subprocess_run, mock_getpass, mock_input):
        """Test that setup is cancelled when keychain storage fails."""
        mock_input.return_value = 'yes'
        mock_getpass.side_effect = ['password123', 'password123']
        
        # Mock subprocess to simulate failure
        mock_subprocess_run.return_value = MagicMock(returncode=1, stderr='Keychain error')
        
        # Call the function
        result = setup_voice_unlock()
        
        # Verify it returns early
        assert result is None

