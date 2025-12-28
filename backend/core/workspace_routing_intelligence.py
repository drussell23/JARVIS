"""
JARVIS Workspace Routing Intelligence v1.0
==========================================

Intelligent routing system for Google Workspace commands with context-aware
visual execution (the "Iron Man" experience).

This module solves the routing problem where commands like "Draft an email" were
falling back to generic Vision handlers instead of being routed to the specialized
GoogleWorkspaceAgent with visual execution.

Architecture:
    ┌──────────────────────────────────────────────────────────────────┐
    │              Workspace Routing Intelligence                      │
    │  ┌──────────────┐    ┌──────────────┐    ┌───────────────────┐   │
    │  │   Intent     │ -> │   Spatial    │ -> │   Execution       │   │
    │  │   Detector   │    │   Awareness  │    │   Mode Selector   │   │
    │  └──────────────┘    └──────────────┘    └───────────────────┘   │
    └──────────────────────────────────────────────────────────────────┘

Flow:
1. Detect workspace intent (Draft email, Check calendar, etc.)
2. Query spatial awareness to find Gmail/Calendar windows
3. Decide execution strategy:
   - Tier 1 (API): Google API (fast, cloud)
   - Tier 2 (Local): macOS Calendar/Contacts (native)
   - Tier 3 (Visual): Computer Use (works with any app, visual feedback)
4. Route to GoogleWorkspaceAgent with execution mode preference

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Enums
# =============================================================================

class WorkspaceIntent(str, Enum):
    """Workspace-specific intents."""
    DRAFT_EMAIL = "draft_email"
    SEND_EMAIL = "send_email"
    CHECK_EMAIL = "check_email"
    SEARCH_EMAIL = "search_email"
    REPLY_EMAIL = "reply_email"

    CHECK_CALENDAR = "check_calendar"
    CREATE_EVENT = "create_event"
    SCHEDULE_MEETING = "schedule_meeting"
    CANCEL_EVENT = "cancel_event"

    CREATE_DOCUMENT = "create_document"
    EDIT_DOCUMENT = "edit_document"
    SHARE_DOCUMENT = "share_document"

    GET_CONTACTS = "get_contacts"
    ADD_CONTACT = "add_contact"

    WORKSPACE_SUMMARY = "workspace_summary"
    UNKNOWN = "unknown"


class ExecutionMode(str, Enum):
    """Execution strategy for workspace commands."""
    API_ONLY = "api"              # Google API (cloud, fast)
    LOCAL_ONLY = "local"          # macOS native apps (CalendarBridge)
    VISUAL_ONLY = "visual"        # Computer Use (screen automation)
    VISUAL_PREFERRED = "visual_preferred"  # Prefer visual if possible
    AUTO = "auto"                 # Intelligent selection


# =============================================================================
# Result Data Classes
# =============================================================================

@dataclass
class WorkspaceIntentResult:
    """Result of workspace intent detection."""
    is_workspace_command: bool
    intent: WorkspaceIntent
    confidence: float
    entities: Dict[str, Any] = field(default_factory=dict)
    execution_mode: ExecutionMode = ExecutionMode.AUTO
    requires_visual: bool = False
    spatial_target: Optional[str] = None  # e.g., "Gmail tab in Space 3"
    reasoning: str = ""


# =============================================================================
# Intent Patterns (Dynamic, No Hardcoding)
# =============================================================================

@dataclass
class IntentPattern:
    """Pattern for detecting workspace intents."""
    intent: WorkspaceIntent
    triggers: List[str]  # Trigger phrases
    entity_patterns: Dict[str, str] = field(default_factory=dict)  # Regex patterns for entities
    visual_keywords: List[str] = field(default_factory=list)  # Keywords that suggest visual execution
    weight: float = 1.0  # Pattern confidence weight


# =============================================================================
# Workspace Intent Detector
# =============================================================================

class WorkspaceIntentDetector:
    """
    Detects workspace-specific intents from commands with zero hardcoding.

    Uses dynamic pattern learning and contextual understanding to route
    commands to the appropriate agent and execution mode.
    """

    def __init__(self):
        """Initialize the workspace intent detector."""
        self._patterns: List[IntentPattern] = []
        self._initialize_patterns()

        # Lazy-loaded components
        self._spatial_awareness = None
        self._neural_mesh_bridge = None

        logger.info("[WorkspaceIntent] Detector initialized")

    def _initialize_patterns(self) -> None:
        """Initialize workspace intent patterns dynamically."""

        # Email intents
        self._patterns.append(IntentPattern(
            intent=WorkspaceIntent.DRAFT_EMAIL,
            triggers=[
                "draft email", "draft an email", "draft a message",
                "compose email", "compose message", "write email",
                "create email", "start email", "new email",
                "email draft", "prepare email",
            ],
            entity_patterns={
                "recipient": r"to\s+([a-z]+(?:\s+[a-z]+)?)",  # "to John Smith"
                "subject": r"(?:about|regarding|re:|subject:?)\s+(.+?)(?:\s+to|\s+for|$)",
            },
            visual_keywords=["draft", "compose", "write", "type"],
            weight=1.0,
        ))

        self._patterns.append(IntentPattern(
            intent=WorkspaceIntent.SEND_EMAIL,
            triggers=[
                "send email", "send message", "send an email",
                "email", "message to", "send to",
            ],
            entity_patterns={
                "recipient": r"(?:to|email)\s+([a-z]+(?:\s+[a-z]+)?)",
                "content": r"(?:saying|that says|message:?)\s+(.+)$",
            },
            visual_keywords=[],
            weight=0.9,
        ))

        self._patterns.append(IntentPattern(
            intent=WorkspaceIntent.CHECK_EMAIL,
            triggers=[
                "check email", "check my email", "check inbox",
                "unread emails", "new emails", "any emails",
                "email notifications", "inbox status",
            ],
            entity_patterns={
                "sender": r"from\s+([a-z]+(?:\s+[a-z]+)?)",
            },
            visual_keywords=[],
            weight=0.8,
        ))

        self._patterns.append(IntentPattern(
            intent=WorkspaceIntent.SEARCH_EMAIL,
            triggers=[
                "search email", "find email", "look for email",
                "search for", "find message from",
            ],
            entity_patterns={
                "query": r"(?:for|about|regarding)\s+(.+)$",
                "sender": r"from\s+([a-z]+(?:\s+[a-z]+)?)",
            },
            visual_keywords=["search", "find", "look"],
            weight=0.85,
        ))

        # Calendar intents
        self._patterns.append(IntentPattern(
            intent=WorkspaceIntent.CHECK_CALENDAR,
            triggers=[
                "check calendar", "check my calendar", "what's my schedule",
                "my schedule", "calendar", "schedule for",
                "meetings", "appointments", "what's next",
                "next meeting", "today's schedule",
            ],
            entity_patterns={
                "date": r"(?:for|on)\s+(today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d+)",
            },
            visual_keywords=[],
            weight=0.9,
        ))

        self._patterns.append(IntentPattern(
            intent=WorkspaceIntent.CREATE_EVENT,
            triggers=[
                "create event", "schedule event", "add event",
                "new event", "create meeting", "schedule meeting",
                "book meeting", "set up meeting",
            ],
            entity_patterns={
                "title": r"(?:called|titled|named)\s+(.+?)(?:\s+for|\s+on|$)",
                "date": r"(?:for|on)\s+(today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d+)",
                "time": r"at\s+(\d+(?::\d+)?\s*(?:am|pm)?)",
            },
            visual_keywords=["create", "schedule", "add"],
            weight=0.95,
        ))

        # Document intents
        self._patterns.append(IntentPattern(
            intent=WorkspaceIntent.CREATE_DOCUMENT,
            triggers=[
                "create document", "create doc", "new document",
                "write document", "draft document", "make document",
                "create essay", "write essay", "write paper",
            ],
            entity_patterns={
                "title": r"(?:called|titled|named|on)\s+(.+?)(?:\s+about|$)",
                "topic": r"(?:about|on|regarding)\s+(.+)$",
            },
            visual_keywords=["write", "draft", "create"],
            weight=0.9,
        ))

        # Contacts intents
        self._patterns.append(IntentPattern(
            intent=WorkspaceIntent.GET_CONTACTS,
            triggers=[
                "get contact", "find contact", "contact info",
                "phone number for", "email for", "contact details",
            ],
            entity_patterns={
                "name": r"(?:for|of)\s+([a-z]+(?:\s+[a-z]+)?)",
            },
            visual_keywords=[],
            weight=0.7,
        ))

        # Summary intent
        self._patterns.append(IntentPattern(
            intent=WorkspaceIntent.WORKSPACE_SUMMARY,
            triggers=[
                "workspace summary", "daily summary", "daily briefing",
                "what's happening", "catch me up", "status update",
            ],
            entity_patterns={},
            visual_keywords=[],
            weight=0.8,
        ))

        logger.info(f"[WorkspaceIntent] Loaded {len(self._patterns)} intent patterns")

    async def detect(self, command: str) -> WorkspaceIntentResult:
        """
        Detect workspace intent from command.

        Args:
            command: User command string

        Returns:
            WorkspaceIntentResult with detection details
        """
        command_lower = command.lower().strip()

        best_match: Optional[IntentPattern] = None
        best_score = 0.0
        matched_entities = {}

        # Pattern matching
        for pattern in self._patterns:
            score = 0.0
            temp_entities = {}

            # Check trigger phrases
            for trigger in pattern.triggers:
                if trigger in command_lower:
                    # Exact match gets full weight
                    score = pattern.weight

                    # Extract entities using regex patterns
                    for entity_name, entity_pattern in pattern.entity_patterns.items():
                        match = re.search(entity_pattern, command_lower, re.IGNORECASE)
                        if match:
                            temp_entities[entity_name] = match.group(1).strip()

                    break

            # Update best match
            if score > best_score:
                best_score = score
                best_match = pattern
                matched_entities = temp_entities

        # No match found
        if not best_match or best_score < 0.5:
            return WorkspaceIntentResult(
                is_workspace_command=False,
                intent=WorkspaceIntent.UNKNOWN,
                confidence=0.0,
                reasoning="No workspace intent patterns matched",
            )

        # Determine if visual execution is preferred
        requires_visual = any(kw in command_lower for kw in best_match.visual_keywords)

        # Determine execution mode
        execution_mode = await self._determine_execution_mode(
            best_match.intent,
            command_lower,
            requires_visual,
        )

        # Query spatial awareness for visual execution
        spatial_target = None
        if execution_mode in (ExecutionMode.VISUAL_ONLY, ExecutionMode.VISUAL_PREFERRED):
            spatial_target = await self._find_workspace_target(best_match.intent)

        return WorkspaceIntentResult(
            is_workspace_command=True,
            intent=best_match.intent,
            confidence=best_score,
            entities=matched_entities,
            execution_mode=execution_mode,
            requires_visual=requires_visual,
            spatial_target=spatial_target,
            reasoning=f"Matched '{best_match.intent.value}' with confidence {best_score:.1%}",
        )

    async def _determine_execution_mode(
        self,
        intent: WorkspaceIntent,
        command: str,
        requires_visual: bool,
    ) -> ExecutionMode:
        """
        Determine the best execution mode for the intent.

        Args:
            intent: Detected workspace intent
            command: Original command string
            requires_visual: Whether visual execution is strongly suggested

        Returns:
            ExecutionMode enum
        """
        # Interactive commands (draft, compose, write) should prefer visual
        interactive_intents = {
            WorkspaceIntent.DRAFT_EMAIL,
            WorkspaceIntent.CREATE_DOCUMENT,
            WorkspaceIntent.EDIT_DOCUMENT,
        }

        if intent in interactive_intents or requires_visual:
            return ExecutionMode.VISUAL_PREFERRED

        # Read-only commands can use API for speed
        readonly_intents = {
            WorkspaceIntent.CHECK_EMAIL,
            WorkspaceIntent.CHECK_CALENDAR,
            WorkspaceIntent.GET_CONTACTS,
            WorkspaceIntent.WORKSPACE_SUMMARY,
        }

        if intent in readonly_intents:
            return ExecutionMode.AUTO  # Let agent decide

        # Default to auto mode (agent will use 3-tier waterfall)
        return ExecutionMode.AUTO

    async def _find_workspace_target(self, intent: WorkspaceIntent) -> Optional[str]:
        """
        Use spatial awareness to find the workspace target window/tab.

        Args:
            intent: Workspace intent

        Returns:
            Description of spatial target (e.g., "Gmail tab in Space 3") or None
        """
        try:
            # Lazy load spatial awareness agent
            if not self._spatial_awareness:
                from backend.neural_mesh.agents.spatial_awareness_agent import (
                    get_spatial_awareness_agent,
                )
                self._spatial_awareness = await get_spatial_awareness_agent()

            if not self._spatial_awareness:
                return None

            # Map intent to app/window patterns
            search_patterns = {
                WorkspaceIntent.DRAFT_EMAIL: ["gmail", "mail", "inbox"],
                WorkspaceIntent.SEND_EMAIL: ["gmail", "mail", "inbox"],
                WorkspaceIntent.CHECK_EMAIL: ["gmail", "mail", "inbox"],
                WorkspaceIntent.CHECK_CALENDAR: ["calendar", "google calendar"],
                WorkspaceIntent.CREATE_EVENT: ["calendar", "google calendar"],
                WorkspaceIntent.CREATE_DOCUMENT: ["google docs", "docs", "document"],
            }

            patterns = search_patterns.get(intent, [])
            if not patterns:
                return None

            # Query all spaces for workspace apps
            result = await self._spatial_awareness.find_window_across_spaces(
                patterns=patterns,
                fuzzy=True,
            )

            if result and result.get("found"):
                space_id = result.get("space_id", "unknown")
                window_title = result.get("window_title", "unknown")
                return f"{window_title} in Space {space_id}"

            return None

        except Exception as e:
            logger.debug(f"[WorkspaceIntent] Spatial awareness query failed: {e}")
            return None


# =============================================================================
# Singleton Access
# =============================================================================

_workspace_detector: Optional[WorkspaceIntentDetector] = None


def get_workspace_detector() -> WorkspaceIntentDetector:
    """Get the global workspace intent detector instance."""
    global _workspace_detector

    if _workspace_detector is None:
        _workspace_detector = WorkspaceIntentDetector()

    return _workspace_detector
