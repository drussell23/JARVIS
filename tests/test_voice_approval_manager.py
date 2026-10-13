# [Ouroboros] Modified by Ouroboros (op=op-01a136df-) at 2026-10-13 20:29 UTC
# Reason: `backend/agi_os/voice_approval_manager.py` has no corresponding test module. CREATE `tests/test_voice_approval_manager.p

import asyncio
import json
import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

# Import the module under test
from backend.agi_os.voice_approval_manager import (
    ApprovalStatus,
    ApprovalUrgency,
    ApprovalRequest,
    ApprovalResponse,
    ApprovalPattern,
    VoiceApprovalManager
)


def test_import_smoke():
    """Test that the module can be imported without errors."""
    assert VoiceApprovalManager is not None
    assert ApprovalRequest is not None
    assert ApprovalResponse is not None
    assert ApprovalStatus is not None
    assert ApprovalUrgency is not None
    assert ApprovalPattern is not None


class TestVoiceApprovalManager:
    def setup_method(self):
        """Set up test fixtures before each test method."""
        self.manager = VoiceApprovalManager()
        # Clear any existing patterns for clean tests
        self.manager._patterns.clear()
        self.manager._pending_requests.clear()
        self.manager._resolved_responses.clear()
        self.manager._history.clear()
        self.manager._approval_callbacks.clear()
        
    def test_init(self):
        """Test initialization of VoiceApprovalManager."""
        assert self.manager._auto_approve_threshold == 0.90
        assert self.manager._auto_deny_threshold == 0.30
        assert self.manager._learning_threshold == 5
        assert self.manager._quiet_hours['enabled'] is True
        assert len(self.manager._category_rules) > 0
        assert len(self.manager._approval_keywords) > 0
        assert len(self.manager._denial_keywords) > 0
        
    def test_approval_request_creation(self):
        """Test creating an ApprovalRequest with default values."""
        request = ApprovalRequest(
            action_type="fix_error",
            target="line 42",
            confidence=0.85,
            reasoning="Detected syntax error"
        )
        
        assert request.action_type == "fix_error"
        assert request.target == "line 42"
        assert request.confidence == 0.85
        assert request.reasoning == "Detected syntax error"
        assert request.urgency == ApprovalUrgency.NORMAL
        assert request.timeout == 30.0
        assert request.allow_auto_approval is True
        assert len(request.request_id) == 12  # MD5 hash truncated to 12 chars
        
    def test_approval_request_with_custom_values(self):
        """Test creating an ApprovalRequest with custom values."""
        request = ApprovalRequest(
            action_type="send_message",
            target="slack_channel",
            confidence=0.95,
            reasoning="Sending update to team",
            urgency=ApprovalUrgency.HIGH,
            timeout=15.0,
            allow_auto_approval=False
        )
        
        assert request.action_type == "send_message"
        assert request.target == "slack_channel"
        assert request.confidence == 0.95
        assert request.reasoning == "Sending update to team"
        assert request.urgency == ApprovalUrgency.HIGH
        assert request.timeout == 15.0
        assert request.allow_auto_approval is False
        
    def test_approval_response_to_dict(self):
        """Test converting ApprovalResponse to dictionary."""
        response = ApprovalResponse(
            request_id="test123",
            status=ApprovalStatus.APPROVED,
            approved=True,
            response_method="voice",
            user_feedback="Looks good",
            confidence_used=0.95
        )
        
        result = response.to_dict()
        assert result['request_id'] == "test123"
        assert result['status'] == "approved"
        assert result['approved'] is True
        assert result['response_method'] == "voice"
        assert result['user_feedback'] == "Looks good"
        assert result['confidence_used'] == 0.95
        
    def test_approval_pattern_properties(self):
        """Test ApprovalPattern property calculations."""
        pattern = ApprovalPattern(
            action_type="fix_error",
            target_pattern="*.py"
        )
        
        # Test initial state
        assert pattern.approval_rate == 0.5  # Default rate when no decisions
        assert pattern.total_decisions == 0
        
        # Add some decisions
        pattern.approved_count = 3
        pattern.denied_count = 2
        
        assert pattern.approval_rate == 0.6  # 3 / (3 + 2)
        assert pattern.total_decisions == 5
        
        
        
        
    def test_check_auto_decision_no_decision(self):
        """Test when no auto-decision should be made."""
        request = ApprovalRequest(
            action_type="fix_error",
            target="line 42",
            confidence=0.6,
            reasoning="Detected syntax error"
        )
        
        # Should not make an auto-decision
        result = self.manager._check_auto_decision(request)
        assert result is None
        
        
        
    def test_build_pattern_key(self):
        """Test pattern key building."""
        request = ApprovalRequest(
            action_type="fix_error",
            target="line 42",
            confidence=0.85,
            reasoning="Detected syntax error"
        )
        
        key = self.manager._build_pattern_key(request)
        assert key == "fix_error:line_number"
        
    def test_generalize_target(self):
        """Test target generalization."""
        # Test line number
        result = self.manager._generalize_target("line 42", "fix_error")
        assert result == "line_number"
        
        # Test file extensions
        result = self.manager._generalize_target("script.py", "run_script")
        assert result == "*.py"
        
        result = self.manager._generalize_target("app.js", "run_script")
        assert result == "*.js"
        
        # Test regular target
        result = self.manager._generalize_target("some_target", "fix_error")
        assert result == "some_target"
        
    def test_configure_thresholds(self):
        """Test configuring approval thresholds."""
        self.manager.configure_threshold(
            auto_approve=0.85,
            auto_deny=0.25,
            learning=10
        )
        
        assert self.manager._auto_approve_threshold == 0.85
        assert self.manager._auto_deny_threshold == 0.25
        assert self.manager._learning_threshold == 10
        
    def test_configure_quiet_hours(self):
        """Test configuring quiet hours."""
        self.manager.configure_quiet_hours(
            enabled=False,
            start=23,
            end=7
        )
        
        assert self.manager._quiet_hours['enabled'] is False
        assert self.manager._quiet_hours['start'] == 23
        assert self.manager._quiet_hours['end'] == 7
        
    def test_get_stats(self):
        """Test getting approval statistics."""
        stats = self.manager.get_stats()
        assert 'total_requests' in stats
        assert 'auto_approved' in stats
        assert 'user_approved' in stats
        assert 'user_denied' in stats
        
        
    def test_record_decision(self):
        """Test recording a user decision."""
        request = ApprovalRequest(
            action_type="fix_error",
            target="line 42",
            confidence=0.85,
            reasoning="Detected syntax error"
        )
        
        response = self.manager.record_decision(request, True)
        assert response.approved is True
        assert response.status == ApprovalStatus.APPROVED
        
    def test_respond_to_pending(self):
        """Test responding to a pending approval request."""
        request = ApprovalRequest(
            action_type="fix_error",
            target="line 42",
            confidence=0.85,
            reasoning="Detected syntax error"
        )
        
        # Add to pending requests
        self.manager._pending_requests[request.request_id] = request
        
        # Respond to it
        result = self.manager.respond_to_pending(request.request_id, True)
        assert result is True
        
    def test_respond_to_pending_not_found(self):
        """Test responding to a non-existent pending request."""
        result = self.manager.respond_to_pending("nonexistent", True)
        assert result is False
        
    def test_on_approval_callback(self):
        """Test registering and removing approval callbacks."""
        def dummy_callback(request, response):
            pass
        
        # Register callback
        self.manager.on_approval(dummy_callback)
        assert len(self.manager._approval_callbacks) == 1
        
        # Remove callback
        result = self.manager.remove_approval_callback(dummy_callback)
        assert result is True
        assert len(self.manager._approval_callbacks) == 0
        
    def test_remove_nonexistent_callback(self):
        """Test removing a callback that doesn't exist."""
        def dummy_callback(request, response):
            pass
        
        # Try to remove non-existent callback
        result = self.manager.remove_approval_callback(dummy_callback)
        assert result is False
        
    def test_get_pending_request_summary(self):
        """Test getting pending request summary."""
        request = ApprovalRequest(
            action_type="fix_error",
            target="line 42",
            confidence=0.85,
            reasoning="Detected syntax error"
        )
        
        # Add to pending requests
        self.manager._pending_requests[request.request_id] = request
        
        summary = self.manager.get_pending_request_summary()
        assert summary is not None
        assert summary['request_id'] == request.request_id
        assert summary['action_type'] == 'fix_error'
        assert summary['target'] == 'line 42'
        
    def test_get_pending_request_summary_no_pending(self):
        """Test getting pending request summary when no requests are pending."""
        summary = self.manager.get_pending_request_summary()
        assert summary is None
        
    async def test_set_owner_identity(self):
        """Test setting owner identity service."""
        mock_service = AsyncMock()
        await self.manager.set_owner_identity(mock_service)
        assert self.manager._owner_identity == mock_service
        
    async def test_set_speaker_verification(self):
        """Test setting speaker verification service."""
        mock_service = AsyncMock()
        await self.manager.set_speaker_verification(mock_service)
        assert self.manager._speaker_verification == mock_service