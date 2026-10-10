# [Ouroboros] Modified by Ouroboros (op=op-01a1221b-) at 2026-10-09 19:21 UTC
# Reason: `backend/api/activity_reporting_commands.py` has no corresponding test module. CREATE `tests/test_activity_reporting_com

import pytest
from backend.api.activity_reporting_commands import is_activity_reporting_command

class TestActivityReportingCommands:
    def test_import_smoke_test(self):
        """Test that the module can be imported without errors"""
        assert is_activity_reporting_command is not None

    def test_is_activity_reporting_command_positive_cases(self):
        """Test that function correctly identifies activity reporting commands"""
        # Direct reporting requests
        assert is_activity_reporting_command("report any changes")
        assert is_activity_reporting_command("report changes")
        assert is_activity_reporting_command("report activities")
        assert is_activity_reporting_command("report any specific changes")
        assert is_activity_reporting_command("tell me what changes")
        assert is_activity_reporting_command("tell me about changes")
        assert is_activity_reporting_command("notify me of changes")
        assert is_activity_reporting_command("let me know what changes")
        assert is_activity_reporting_command("inform me of changes")
        
        # Monitoring requests
        assert is_activity_reporting_command("watch for changes")
        assert is_activity_reporting_command("monitor changes")
        assert is_activity_reporting_command("monitor activities")
        assert is_activity_reporting_command("track changes")
        assert is_activity_reporting_command("observe changes")
        assert is_activity_reporting_command("keep an eye on")
        assert is_activity_reporting_command("watch my workspace")
        assert is_activity_reporting_command("monitor my workspace")
        
        # Activity queries
        assert is_activity_reporting_command("what's happening")
        assert is_activity_reporting_command("what changed")
        assert is_activity_reporting_command("any changes")
        assert is_activity_reporting_command("show me changes")
        assert is_activity_reporting_command("workspace activity")
        assert is_activity_reporting_command("desktop activity")
        
        # Confirmation responses
        assert is_activity_reporting_command("yes report")
        assert is_activity_reporting_command("yes notify")
        assert is_activity_reporting_command("yes tell me")
        assert is_activity_reporting_command("yes monitor")
        assert is_activity_reporting_command("yes watch")
        
        # Workspace insights
        assert is_activity_reporting_command("workspace insights")
        assert is_activity_reporting_command("give me insights")
        assert is_activity_reporting_command("analyze my workspace")
        assert is_activity_reporting_command("workspace summary")

    def test_is_activity_reporting_command_negative_cases(self):
        """Test that function correctly rejects non-activity reporting commands"""
        # Non-matching commands
        assert not is_activity_reporting_command("hello world")
        assert not is_activity_reporting_command("what is your name")
        assert not is_activity_reporting_command("please help me")
        assert not is_activity_reporting_command("open the door")
        assert not is_activity_reporting_command("")
        
        # Commands with partial matches
        assert not is_activity_reporting_command("reporting changes")
        assert not is_activity_reporting_command("monitoring activities")
        assert not is_activity_reporting_command("watching for changes")

    def test_is_activity_reporting_command_case_insensitive(self):
        """Test that function correctly handles case insensitive matching"""
        # Uppercase versions
        assert is_activity_reporting_command("REPORT ANY CHANGES")
        assert is_activity_reporting_command("REPORT CHANGES")
        assert is_activity_reporting_command("WATCH FOR CHANGES")
        assert is_activity_reporting_command("MONITOR CHANGES")
        
        # Mixed case versions
        assert is_activity_reporting_command("Report Any Changes")
        assert is_activity_reporting_command("Watch For Changes")
        assert is_activity_reporting_command("Monitor Changes")

    def test_is_activity_reporting_command_partial_matches(self):
        """Test that function correctly handles partial matches (should not match)"""
        # These should NOT match because they are partial substrings of patterns
        assert not is_activity_reporting_command("reporting")
        assert not is_activity_reporting_command("monitoring")
        assert not is_activity_reporting_command("watching")
        assert not is_activity_reporting_command("workspace")
        
        # These should match because they contain full patterns
        assert is_activity_reporting_command("I want to report changes")
        assert is_activity_reporting_command("Please monitor activities")
        assert is_activity_reporting_command("Watch for changes in my workspace")