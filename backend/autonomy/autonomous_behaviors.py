#!/usr/bin/env python3
"""
Autonomous Behaviors for JARVIS

This module implements specific behavior patterns for different scenarios in the JARVIS
autonomous system. It provides handlers for messages, meetings, workspace organization,
and security events, enabling the system to automatically respond to various situations
without user intervention.

The module includes:
- MessageHandler: Processes and categorizes incoming messages
- MeetingHandler: Prepares workspace for meetings
- WorkspaceOrganizer: Maintains optimal workspace layout
- SecurityHandler: Responds to security-related events
- AutonomousBehaviorManager: Coordinates all autonomous behaviors

Example:
    >>> manager = AutonomousBehaviorManager()
    >>> actions = await manager.process_workspace_state(workspace_state, windows)
    >>> print(f"Generated {len(actions)} autonomous actions")
"""

import asyncio
import re
import os
import logging
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime, timedelta
from collections import defaultdict

from vision.window_detector import WindowInfo
try:
    from vision.claude_vision_analyzer_main import ClaudeVisionAnalyzer
except ImportError:
    ClaudeVisionAnalyzer = None
    
from .autonomous_decision_engine import AutonomousAction, ActionPriority, ActionCategory

logger = logging.getLogger(__name__)

class MessageHandler:
    """Autonomous message handling behaviors.
    
    This class analyzes incoming messages and notifications to determine appropriate
    autonomous actions. It can classify messages as automated notifications, meeting
    reminders, urgent communications, or security alerts, and generate corresponding
    actions.
    
    Attributes:
        vision_analyzer: Optional ClaudeVisionAnalyzer for extracting message content
        automated_patterns: Regex patterns for identifying automated messages
        meeting_patterns: Regex patterns for identifying meeting reminders
        urgent_patterns: Regex patterns for identifying urgent messages
        security_patterns: Regex patterns for identifying security alerts
    """
    
    def __init__(self):
        """Initialize the MessageHandler with vision analyzer and message patterns."""
        # Initialize vision analyzer with API key from environment
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if api_key and ClaudeVisionAnalyzer is not None:
            try:
                self.vision_analyzer = ClaudeVisionAnalyzer(api_key)
            except Exception as e:
                logger.warning(f"Failed to initialize ClaudeVisionAnalyzer: {e}")
                self.vision_analyzer = None
        else:
            logger.warning("ANTHROPIC_API_KEY not set or ClaudeVisionAnalyzer not available - vision analysis will be limited")
            self.vision_analyzer = None
        
        # Patterns for different message types
        self.automated_patterns = [
            r"automated|automatic|bot|system notification|digest|summary",
            r"noreply|no-reply|do not reply|automated message",
            r"scheduled|recurring|daily|weekly|monthly",
            r"newsletter|subscription|marketing"
        ]
        
        self.meeting_patterns = [
            r"meeting in \d+ minutes?|starts? (at|in)|scheduled for",
            r"standup|stand-up|sync|call|conference",
            r"zoom|teams|google meet|hangout|webex|meeting link",
            r"calendar reminder|event reminder"
        ]
        
        self.urgent_patterns = [
            r"urgent|asap|emergency|critical|immediate",
            r"important|priority|deadline|due (today|soon)",
            r"action required|response needed|waiting for",
            r"blocker|blocked|issue|problem"
        ]
        
        self.security_patterns = [
            r"security alert|suspicious|unauthorized|breach",
            r"password|credential|authentication|2fa|two.?factor",
            r"login attempt|access denied|verification",
            r"fraud|scam|phishing|malware"
        ]
    
    async def handle_routine_message(self, message_window: WindowInfo) -> Optional[AutonomousAction]:
        """Determine appropriate action for routine messages.
        
        Analyzes a message window to classify the message type and generate an
        appropriate autonomous action. Handles automated notifications, meeting
        reminders, urgent messages, and security alerts.
        
        Args:
            message_window: WindowInfo object containing message window details
            
        Returns:
            AutonomousAction object with recommended action, or None if no action needed
            
        Raises:
            Exception: If message content extraction or analysis fails
            
        Example:
            >>> window = WindowInfo(app_name="Slack", window_title="Urgent: Server down")
            >>> action = await handler.handle_routine_message(window)
            >>> print(action.action_type)  # "highlight_urgent_message"
        """
        try:
            # Extract message content using vision
            content = await self._extract_message_content(message_window)
            
            # Classify message
            if self._is_automated_notification(content):
                return AutonomousAction(
                    action_type="dismiss_notification",
                    target=message_window.app_name,
                    params={
                        "window_id": message_window.window_id,
                        "reason": "Automated notification"
                    },
                    priority=ActionPriority.LOW,
                    confidence=0.95,
                    category=ActionCategory.NOTIFICATION,
                    reasoning="Routine automated message that doesn't require attention"
                )
            
            if self._is_meeting_reminder(content):
                meeting_info = self._extract_meeting_info(content)
                return AutonomousAction(
                    action_type="prepare_for_meeting",
                    target="workspace",
                    params={
                        "meeting_info": meeting_info,
                        "window_id": message_window.window_id
                    },
                    priority=ActionPriority.HIGH,
                    confidence=0.9,
                    category=ActionCategory.CALENDAR,
                    reasoning=f"Meeting starting soon: {meeting_info.get('title', 'Unknown')}"
                )
            
            if self._is_urgent_message(content):
                return AutonomousAction(
                    action_type="highlight_urgent_message",
                    target=message_window.app_name,
                    params={
                        "window_id": message_window.window_id,
                        "urgency_level": self._get_urgency_level(content)
                    },
                    priority=ActionPriority.HIGH,
                    confidence=0.85,
                    category=ActionCategory.COMMUNICATION,
                    reasoning="Urgent message requiring attention"
                )
            
            if self._is_security_alert(content):
                return AutonomousAction(
                    action_type="security_alert",
                    target="security",
                    params={
                        "window_id": message_window.window_id,
                        "alert_type": self._get_security_type(content),
                        "app": message_window.app_name
                    },
                    priority=ActionPriority.CRITICAL,
                    confidence=0.95,
                    category=ActionCategory.SECURITY,
                    reasoning="Security-related message requiring immediate attention"
                )
            
            # Default action for unclassified messages
            return AutonomousAction(
                action_type="queue_for_review",
                target=message_window.app_name,
                params={
                    "window_id": message_window.window_id,
                    "message_preview": content[:100]
                },
                priority=ActionPriority.MEDIUM,
                confidence=0.7,
                category=ActionCategory.COMMUNICATION,
                reasoning="Message queued for later review"
            )
            
        except Exception as e:
            logger.error(f"Error handling message: {e}")
            return None
    
    async def _extract_message_content(self, window: WindowInfo) -> str:
        """Extract message content from window using vision analysis.
        
        Uses the ClaudeVisionAnalyzer to extract text content from the message window.
        Falls back to window title if vision analysis is unavailable or fails.
        
        Args:
            window: WindowInfo object containing window details
            
        Returns:
            Extracted message content as string
            
        Raises:
            Exception: If both vision analysis and fallback methods fail
        """
        try:
            # Use vision analyzer to extract text if available
            if self.vision_analyzer:
                # Get current screen context to understand the message
                try:
                    # Analyze current screen for message content
                    context = await self.vision_analyzer.get_screen_context()
                    
                    # If the window is visible, try to extract specific content
                    if window.app_name in context.get('description', ''):
                        # More specific analysis for the active window
                        import subprocess
                        import tempfile
                        from PIL import Image
                        import numpy as np
                        
                        # Capture current screen
                        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                            tmp_path = tmp.name
                        
                        _cap = await asyncio.create_subprocess_exec(
                            'screencapture', '-x', tmp_path,
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        await asyncio.wait_for(_cap.wait(), timeout=5.0)
                        image = Image.open(tmp_path)
                        screenshot = np.array(image)
                        os.unlink(tmp_path)
                        
                        # Analyze for message content
                        result = await self.vision_analyzer.analyze_screenshot(
                            screenshot,
                            f"Extract the message content from the {window.app_name} window. Focus on the main text or notification content."
                        )
                        
                        # Extract meaningful content
                        description = result.get('description', '')
                        if description and len(description) > len(window.window_title):
                            return description
                    
                    # If no specific content found, use window title
                    return window.window_title
                    
                except Exception as e:
                    logger.debug(f"Vision analysis error: {e}")
                    return window.window_title
            else:
                # Fallback to window title when vision analyzer not available
                return window.window_title
        except Exception as e:
            logger.debug(f"Vision analysis failed: {e}")
            # Fallback to window title
            return window.window_title
    
    def _is_automated_notification(self, content: str) -> bool:
        """Check if message is an automated notification.
        
        Args:
            content: Message content to analyze
            
        Returns:
            True if message appears to be automated, False otherwise
        """
        content_lower = content.lower()
        return any(re.search(pattern, content_lower) for pattern in self.automated_patterns)
    
    def _is_meeting_reminder(self, content: str) -> bool:
        """Check if message is a meeting reminder.
        
        Args:
            content: Message content to analyze
            
        Returns:
            True if message appears to be a meeting reminder, False otherwise
        """
        content_lower = content.lower()
        return any(re.search(pattern, content_lower) for pattern in self.meeting_patterns)
    
    def _is_urgent_message(self, content: str) -> bool:
        """Check if message is urgent.
        
        Args:
            content: Message content to analyze
            
        Returns:
            True if message appears to be urgent, False otherwise
        """
        content_lower = content.lower()
        return any(re.search(pattern, content_lower) for pattern in self.urgent_patterns)
    
    def _is_security_alert(self, content: str) -> bool:
        """Check if message is security-related.
        
        Args:
            content: Message content to analyze
            
        Returns:
            True if message appears to be security-related, False otherwise
        """
        content_lower = content.lower()
        return any(re.search(pattern, content_lower) for pattern in self.security_patterns)
    
    def _extract_meeting_info(self, content: str) -> Dict[str, Any]:
        """Extract meeting information from content.
        
        Parses meeting-related content to extract key information like time,
        platform, and title.
        
        Args:
            content: Message content containing meeting information
            
        Returns:
            Dictionary containing extracted meeting information with keys:
            - title: Meeting title or subject
            - time: Meeting time information
            - platform: Meeting platform (Zoom, Teams, etc.)
        """
        info = {"title": "Meeting", "time": None, "platform": None}
        
        # Extract time
        time_match = re.search(r"in (\d+) minutes?|at (\d{1,2}:\d{2})", content, re.IGNORECASE)
        if time_match:
            if time_match.group(1):
                minutes = int(time_match.group(1))
                info["time"] = f"in {minutes} minutes"  # Keep as string for compatibility
            else:
                info["time"] = time_match.group(2)
        
        # Extract platform
        for platform in ["zoom", "teams", "meet", "hangout", "webex"]:
            if platform in content.lower():
                info["platform"] = platform.capitalize()
                break
        
        # Extract title (first line or subject)
        lines = content.split('\n')
        if lines:
            info["title"] = lines[0].strip()[:50]
        
        return info
    
    def _get_urgency_level(self, content: str) -> str:
        """Determine urgency level from content.
        
        Args:
            content: Message content to analyze
            
        Returns:
            Urgency level as string: "critical", "urgent", or "medium"
        """
        content_lower = content.lower()
        
        if any(word in content_lower for word in ["emergency", "critical", "asap", "immediate"]):
            return "critical"
        elif any(word in content_lower for word in ["urgent", "important", "deadline"]):
            return "critical"  # "urgent" should also be critical priority
        else:
            return "medium"
    
    def _get_security_type(self, content: str) -> str:
        """Determine security alert type.
        
        Args:
            content: Message content to analyze
            
        Returns:
            Security alert type as string: "authentication", "access_attempt", 
            "fraud_alert", or "general_security"
        """
        content_lower = content.lower()
        
        if "password" in content_lower or "credential" in content_lower:
            return "authentication"
        elif "login" in content_lower or "access" in content_lower:
            return "access_attempt"
        elif "fraud" in content_lower or "scam" in content_lower:
            return "fraud_alert"
        else:
            return "general_security"

class MeetingHandler:
    """Autonomous meeting preparation behaviors.
    
    This class handles workspace preparation for upcoming meetings, including
    hiding sensitive applications, opening meeting platforms, muting distracting
    apps, and organizing the desktop for a professional appearance.
    
    Attributes:
        preparation_actions: List of preparation actions taken
        sensitive_apps: List of application names considered sensitive
    """
    
    def __init__(self):
        """Initialize the MeetingHandler with default settings."""
        self.preparation_actions = []
        self.sensitive_apps = [
            "1Password", "Bitwarden", "LastPass", "KeePass",
            "Banking", "PayPal", "Venmo", "CashApp",
            "Personal", "Private", "Confidential"
        ]
    
    async def prepare_for_meeting(self, meeting_info: Dict[str, Any], 
                                  current_windows: List[WindowInfo]) -> List[AutonomousAction]:
        """Generate actions to prepare workspace for meeting.
        
        Analyzes the current workspace and generates a list of actions to prepare
        for an upcoming meeting, including security measures and workspace organization.
        
        Args:
            meeting_info: Dictionary containing meeting details (title, time, platform)
            current_windows: List of currently open windows
            
        Returns:
            List of AutonomousAction objects for meeting preparation
            
        Example:
            >>> meeting_info = {"title": "Team Standup", "platform": "Zoom"}
            >>> actions = await handler.prepare_for_meeting(meeting_info, windows)
            >>> print(f"Generated {len(actions)} preparation actions")
        """
        actions = []
        
        # Hide sensitive windows
        for window in current_windows:
            if self._is_sensitive_window(window):
                actions.append(AutonomousAction(
                    action_type="minimize_window",
                    target=window.app_name,
                    params={"window_id": window.window_id},
                    priority=ActionPriority.HIGH,
                    confidence=0.95,
                    category=ActionCategory.SECURITY,
                    reasoning=f"Hiding sensitive app before meeting: {window.app_name}"
                ))
        
        # Open meeting platform
        platform = meeting_info.get("platform")
        if platform:
            actions.append(AutonomousAction(
                action_type="open_application",
                target=platform,
                params={"meeting_link": meeting_info.get("link")},
                priority=ActionPriority.HIGH,
                confidence=0.9,
                category=ActionCategory.CALENDAR,
                reasoning=f"Opening {platform} for upcoming meeting"
            ))
        
        # Mute distracting apps
        distracting_apps = self._find_distracting_apps(current_windows)
        for app in distracting_apps:
            actions.append(AutonomousAction(
                action_type="mute_notifications",
                target=app,
                params={"duration_minutes": 60},
                priority=ActionPriority.MEDIUM,
                confidence=0.85,
                category=ActionCategory.NOTIFICATION,
                reasoning=f"Muting {app} notifications during meeting"
            ))
        
        # Clean up desktop if needed
        if self._desktop_needs_cleanup(current_windows):
            actions.append(AutonomousAction(
                action_type="organize_desktop",
                target="desktop",
                params={"style": "meeting_ready"},
                priority=ActionPriority.MEDIUM,
                confidence=0.8,
                category=ActionCategory.MAINTENANCE,
                reasoning="Organizing desktop for professional appearance"
            ))
        
        return actions
    
    def _is_sensitive_window(self, window: WindowInfo) -> bool:
        """Check if window contains sensitive content.
        
        Args:
            window: WindowInfo object to check
            
        Returns:
            True if window is considered sensitive, False otherwise
        """
        window_text = f"{window.app_name} {window.window_title}".lower()
        
        # Check against sensitive apps
        for sensitive in self.sensitive_apps:
            if sensitive.lower() in window_text:
                return True
        
        # Check for sensitive patterns in title
        sensitive_patterns = [
            r"password|credential|secret",
            r"bank|finance|money|payment",
            r"personal|private|confidential",
            r"medical|health|prescription"
        ]
        
        return any(re.search(pattern, window_text) for pattern in sensitive_patterns)
    
    def _find_distracting_apps(self, windows: List[WindowInfo]) -> List[str]:
        """Find apps that might be distracting during meetings.
        
        Args:
            windows: List of current windows to analyze
            
        Returns:
            List of application names that could be distracting
        """
        distracting = []
        distracting_patterns = [
            "Discord", "Slack", "Messages", "WhatsApp", "Telegram",
            "Twitter", "Facebook", "Instagram", "TikTok",
            "YouTube", "Netflix", "Spotify", "Music"
        ]
        
        seen_apps = set()
        for window in windows:
            for pattern in distracting_patterns:
                if pattern.lower() in window.app_name.lower() and window.app_name not in seen_apps:
                    distracting.append(window.app_name)
                    seen_apps.add(window.app_name)
                    break
        
        return distracting
    
    def _desktop_needs_cleanup(self, windows: List[WindowInfo]) -> bool:
        """Check if desktop needs organization.
        
        Args:
            windows: List of current windows to analyze
            
        Returns:
            True if desktop appears cluttered and needs cleanup, False otherwise
        """
        # Simple heuristic: too many visible windows
        visible_windows = [w for w in windows if w.is_visible]
        return len(visible_windows) > 10

class WorkspaceOrganizer:
    """Autonomous workspace organization behaviors.
    
    This class analyzes the current workspace layout and suggests organization
    improvements such as window arrangement, duplicate removal, and focus mode
    activation to maintain an efficient working environment.
    
    Attributes:
        project_patterns: Dictionary of project-specific patterns
        window_groups: Default dictionary for grouping windows by context
    """
    
    def __init__(self):
        """Initialize the WorkspaceOrganizer with default settings."""
        self.project_patterns = {}
        self.window_groups = defaultdict(list)
    
    async def analyze_and_organize(self, windows: List[WindowInfo], 
                                   user_state: str) -> List[AutonomousAction]:
        """Analyze workspace and suggest organization actions.
        
        Examines the current workspace state and generates actions to improve
        organization, including window arrangement, duplicate removal, and
        focus mode suggestions.
        
        Args:
            windows: List of current windows in the workspace
            user_state: Current user state ("focused", "available", etc.)
            
        Returns:
            List of AutonomousAction objects for workspace organization
            
        Example:
            >>> organizer = WorkspaceOrganizer()
            >>> actions = await organizer.analyze_and_organize(windows, "focused")
            >>> print(f"Suggested {len(actions)} organization actions")
        """
        actions = []
        
        # Group windows by project/context
        window_groups = self._group_windows_by_context(windows)
        
        # Check for inefficient layouts
        if self._has_overlapping_windows(windows):
            actions.append(AutonomousAction(
                action_type="arrange_windows",
                target="workspace",
                params={
                    "layout": "tiled",
                    "groups": window_groups
                },
                priority=ActionPriority.LOW,
                confidence=0.8,
                category=ActionCategory.MAINTENANCE,
                reasoning="Detected overlapping windows - suggesting tiled layout"
            ))
        
        # Check for too many windows
        if len([w for w in windows if w.is_visible]) > 15:
            actions.extend(self._suggest_window_reduction(windows))
        
        # Check for duplicate windows
        duplicates = self._find_duplicate_windows(windows)
        for dup in duplicates:
            actions.append(AutonomousAction(
                action_type="close_duplicate",
                target=dup.app_name,
                params={"window_id": dup.window_id},
                priority=ActionPriority.LOW,
                confidence=0.9,
                category=ActionCategory.MAINTENANCE,
                reasoning=f"Found duplicate {dup.app_name} window"
            ))
        
        # Suggest focus mode if too cluttered
        if self._workspace_is_cluttered(windows) and user_state == "focused":
            actions.append(AutonomousAction(
                action_type="enable_focus_mode",
                target="workspace",
                params={
                    "keep_apps": self._identify_primary_work_apps(windows),
                    "minimize_others": True
                },
                priority=ActionPriority.MEDIUM,
                confidence=0.85,
                category=ActionCategory.MAINTENANCE,
                reasoning="Suggesting focus mode to reduce distractions"
            ))
        
        return actions
    
    def _group_windows_by_context(self, windows: List[WindowInfo]) -> Dict[str, List[WindowInfo]]:
        """Group windows by project or context.
        
        Args:
            windows: List of windows to group
            
        Returns:
            Dictionary mapping context names to lists of windows
        """
        groups = defaultdict(list)
        
        # Simple grouping by app type and content
        for window in windows:
            if "code" in window.app_name.lower() or "ide" in window.window_title.lower():
                groups["development"].append(window)
            elif any(term in window.app_name.lower() for term in ["chrome", "safari", "firefox"]):
                groups["browser"].append(window)
            elif any(term in window.app_name.lower() for term in ["terminal", "iterm", "console"]):
                groups["terminal"].append(window)
            elif any(term in window.app_name.lower() for term in ["slack", "discord", "messages"]):
                groups["communication"].append(window)
            else:
                groups["other"].append(window)
        
        return dict(groups)
    
    def _has_overlapping_windows(self, windows: List[WindowInfo]) -> bool:
        """Check if windows are overlapping significantly.
        
        Args:
            windows: List of windows to check for overlaps
            
        Returns:
            True if significant overlapping is detected, False otherwise
        """
        visible_windows = [w for w in windows if w.is_visible]
        
        # Simple overlap detection
        for i, w1 in enumerate(visible_windows):
            for w2 in visible_windows[i+1:]:
                if self._windows_overlap(w1, w2):
                    return True
        
        return False
    
    def _windows_overlap(self, w1: WindowInfo, w2: WindowInfo) -> bool:
        """Check if two windows overlap.
        
        Args:
            w1: First window to check
            w2: Second window to check
            
        Returns:
            True if windows overlap, False otherwise
        """
        # Get bounds
        b1 = w1.bounds
        b2 = w2.bounds
        
        # Check for overlap
        return not (
            b1["x"] + b1["width"] <= b2["x"] or
            b2["x"] + b2["width"] <= b1["x"] or
            b1["y"] + b1["height"] <= b2["y"] or
            b2["y"] + b2["height"] <= b1["y"]
        )
    
    def _suggest_window_reduction(self, windows: List[WindowInfo]) -> List[AutonomousAction]:
        """Suggest which windows to close or minimize.
        
        Args:
            windows: List of windows to analyze for reduction
            
        Returns:
            List of AutonomousAction objects for window reduction
        """
        actions = []
        
        # Find inactive windows (simplified logic)
        for window in windows:
            if window.is_visible and "untitled" in window.window_title.lower():
                actions.append(AutonomousAction(
                    action_type="close_window",
                    target=window.app_name,
                    params={"window_id": window.window_id},
                    priority=ActionPriority.LOW,
                    confidence=0.7,
                    category=ActionCategory.MAINTENANCE,
                    reasoning="Closing untitled/empty window"
                ))
        
        return actions
    
    def _find_duplicate_windows(self, windows: List[WindowInfo]) -> List[WindowInfo]:
        """Find duplicate windows of the same app.
        
        Args:
            windows: List of windows to check for duplicates
            
        Returns:
            List of WindowInfo objects that are duplicates
        """
        duplicates = []
        app_windows = defaultdict(list)
        
        for window in windows:
            app_windows[window.app_name].append(window)
        
        for app, wins in app_windows.items():
            if len(wins) > 1:
                # Keep the focused one or the first one
                focused = [w for w in wins if w.is_focused]
                if focused:
                    keep = focused[0]
                else:
                    keep = wins[0]
                
                for w in wins:
                    if w.window_id != keep.window_id and w.window_title == keep.window_title:
                        duplicates.append(w)
        
        return duplicates
    
    def _workspace_is_cluttered(self, windows: List[WindowInfo]) -> bool:
        """Check if workspace is too cluttered.
        
        Args:
            windows: List of windows to analyze
            
        Returns:
            True if workspace appears cluttered, False otherwise
        """
        visible_count = len([w for w in windows if w.is_visible])
        
        # Consider cluttered if:
        # - More than 10 visible windows
        # - Multiple overlapping windows
        # - Multiple apps with multiple windows
        
        if visible_count > 10:
            return True
        
        if self._has_overlapping_windows(windows):
            return True
        
        return False
    
    def _identify_primary_work_apps(self, windows: List[WindowInfo]) -> List[str]:
        """Identify primary work applications.
        
        Args:
            windows: List of windows to analyze
            
        Returns:
            List of application names considered primary for work
        """
        work_apps = []
        work_patterns = [
            "code", "visual studio", "xcode", "android studio",
            "terminal", "iterm", "console",
            "chrome", "safari", "firefox",
            "notes", "notion", "obsidian"
        ]
        
        seen = set()
        for window in windows:
            if window.is_focused:
                work_apps.append(window.app_name)
                seen.add(window.app_name)
                continue
            
            for pattern in work_patterns:
                if pattern in window.app_name.lower() and window.app_name not in seen:
                    work_apps.append(window.app_name)
                    seen.add(window.app_name)
                    break
        
        return work_apps[:5]  # Limit to 5 primary apps

class SecurityHandler:
    """Autonomous security-related behaviors.
    
    This class handles security events and threats by automatically responding
    to suspicious activities, protecting sensitive data, and alerting users to
    potential security issues.
    
    Attributes:
        security_apps: List of security-related application names
        suspicious_patterns: Regex patterns for identifying suspicious activities
    """
    
    def __init__(self):
        """Initialize the SecurityHandler with security app patterns."""
        self.security_apps = ["1Password", "Bitwarden", "LastPass", "Keychain"]
        self.suspicious_patterns = [
            r"unauthorized access|failed login|suspicious activity",
            r"verify your account|confirm your identity",
            r"unusual activity|security alert",
            r"password expired|change password"
        ]
    
    async def handle_security_event(self, event_type: str,
                                   context: Dict[str, Any]) -> List[AutonomousAction]:
        """Handle security-related events.

        Processes different types of security events and generates appropriate
        autonomous actions to protect the user and system.

        Args:
            event_type: Type of security event ("suspicious_login", "password_manager_open", etc.)
            context: Dictionary containing event context and details

        Returns:
            List of AutonomousAction objects for security response

        Raises:
            ValueError: If event_type is not recognized

        Example:
            >>> context = {"app_name": "1Password", "in_meeting": True}
            >>> actions = await handler.handle_security_event("password_manager_open", context)
            >>> print(f"Generated {len(actions)} security actions")
        """
        actions = []

        if event_type == "suspicious_login":
            actions.append(AutonomousAction(
                action_type="security_alert",
                description="Alert user about suspicious login attempt",
                confidence=0.9,
                parameters={"event_type": event_type, "context": context},
                requires_approval=False,
                risk_level="high"
            ))
        elif event_type == "password_manager_open":
            # Check if in meeting - hide sensitive info
            if context.get("in_meeting", False):
                actions.append(AutonomousAction(
                    action_type="privacy_protection",
                    description="Blur or hide password manager during meeting",
                    confidence=0.85,
                    parameters={"app_name": context.get("app_name", "unknown")},
                    requires_approval=True,
                    risk_level="medium"
                ))

        return actions


class AutonomousBehaviorManager:
    """Unified manager for all autonomous behavior handlers.

    Coordinates between different specialized handlers (MessageHandler, MeetingHandler,
    WorkspaceOrganizer, SecurityHandler) to provide comprehensive autonomous behaviors.

    Attributes:
        message_handler: Handler for message/notification behaviors
        meeting_handler: Handler for meeting-related behaviors
        workspace_organizer: Handler for workspace organization
        security_handler: Handler for security events
    """

    def __init__(self):
        """Initialize the behavior manager with all handlers."""
        self.message_handler = MessageHandler()
        self.meeting_handler = MeetingHandler()
        self.workspace_organizer = WorkspaceOrganizer()
        self.security_handler = SecurityHandler()

        # Configuration
        self._enabled = True
        self._config = {
            'auto_respond_enabled': True,
            'meeting_management_enabled': True,
            'workspace_optimization_enabled': True,
            'security_protection_enabled': True
        }

    @property
    def is_enabled(self) -> bool:
        """Check if behavior manager is enabled."""
        return self._enabled

    def enable(self) -> None:
        """Enable autonomous behaviors."""
        self._enabled = True

    def disable(self) -> None:
        """Disable autonomous behaviors."""
        self._enabled = False

    async def process_event(self, event_type: str, context: Dict[str, Any]) -> List[AutonomousAction]:
        """Process an event and generate appropriate autonomous actions.

        Routes events to the appropriate handler based on event type and
        aggregates resulting actions.

        Args:
            event_type: Type of event to process
            context: Event context and details

        Returns:
            List of autonomous actions to execute
        """
        if not self._enabled:
            return []

        actions = []

        # Route to appropriate handler
        if event_type in ["new_message", "urgent_message", "notification"]:
            if self._config['auto_respond_enabled']:
                handler_actions = await self.message_handler.handle_notification(
                    context.get("app_name", ""),
                    context.get("content", ""),
                    context.get("in_meeting", False),
                    context.get("focus_mode", False)
                )
                actions.extend(handler_actions)

        elif event_type in ["meeting_starting", "meeting_ending", "calendar_event"]:
            if self._config['meeting_management_enabled']:
                handler_actions = await self.meeting_handler.prepare_for_meeting(context)
                actions.extend(handler_actions)

        elif event_type in ["workspace_cluttered", "focus_session_start", "focus_session_end"]:
            if self._config['workspace_optimization_enabled']:
                # Workspace organization handled through workspace_organizer
                pass

        elif event_type in ["suspicious_login", "password_manager_open", "security_alert"]:
            if self._config['security_protection_enabled']:
                handler_actions = await self.security_handler.handle_security_event(
                    event_type,
                    context
                )
                actions.extend(handler_actions)

        return actions

    def get_config(self) -> Dict[str, Any]:
        """Get current configuration."""
        return {
            'enabled': self._enabled,
            **self._config
        }

    def update_config(self, config: Dict[str, Any]) -> None:
        """Update configuration.

        Args:
            config: Dictionary of configuration updates
        """
        for key, value in config.items():
            if key in self._config:
                self._config[key] = value
            elif key == 'enabled':
                self._enabled = value


# Global singleton instance
_behavior_manager: Optional[AutonomousBehaviorManager] = None


def get_behavior_manager() -> AutonomousBehaviorManager:
    """Get the global behavior manager instance.

    Returns:
        AutonomousBehaviorManager: Singleton behavior manager instance
    """
    global _behavior_manager
    if _behavior_manager is None:
        _behavior_manager = AutonomousBehaviorManager()
    return _behavior_manager