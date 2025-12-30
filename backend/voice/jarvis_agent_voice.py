#!/usr/bin/env python3
"""
JARVIS Agent Voice System - AI Agent with System Control
Enhanced version with macOS control capabilities
"""

import os
import asyncio
import logging
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime
import json
import re

from voice.ml_enhanced_voice_system import MLEnhancedVoiceSystem
from voice.jarvis_personality_adapter import PersonalityAdapter
from system_control import ClaudeCommandInterpreter, CommandCategory, SafetyLevel
from chatbots.claude_vision_chatbot import ClaudeVisionChatbot
from system_control.weather_bridge import WeatherBridge
from core.async_pipeline import get_async_pipeline, AdvancedAsyncPipeline

logger = logging.getLogger(__name__)

# Document Writer Integration
try:
    from context_intelligence.executors.document_writer import get_document_writer, parse_document_request
    DOCUMENT_WRITER_AVAILABLE = True
    logger.info("✅ Document Writer module available")
except ImportError as e:
    DOCUMENT_WRITER_AVAILABLE = False
    logger.warning(f"Document Writer not available: {e}")

# Vision System v2.0 Integration
try:
    from voice.vision_v2_integration import VisionV2Integration

    VISION_V2_AVAILABLE = True
except ImportError:
    VISION_V2_AVAILABLE = False
    logger.warning("Vision System v2.0 not available")


class JARVISAgentVoice(MLEnhancedVoiceSystem):
    """JARVIS AI Agent with system control capabilities"""

    def __init__(self, user_name: str = "Sir", vision_analyzer=None):
        super().__init__(user_name)
        self.user_name = user_name
        self.wake_words = ["jarvis", "hey jarvis", "okay jarvis", "yo jarvis"]
        self.wake_word_variations = ["jar vis", "hey jar vis", "jarv", "j.a.r.v.i.s"]
        self.urgent_wake_words = ["jarvis emergency", "jarvis urgent"]
        
        # Store vision analyzer/handler
        self.vision_analyzer = vision_analyzer
        self.vision_handler = vision_analyzer  # Support both names

        # Initialize system control
        self.api_key = os.getenv("ANTHROPIC_API_KEY")
        if self.api_key:
            self.command_interpreter = ClaudeCommandInterpreter(self.api_key)
            # Pass vision analyzer to chatbot if provided
            self.claude_chatbot = ClaudeVisionChatbot(self.api_key, vision_analyzer=vision_analyzer)
            self.system_control_enabled = True
            
            # Set vision handler in command interpreter if available
            if vision_analyzer and hasattr(self.command_interpreter, 'set_vision_handler'):
                self.command_interpreter.set_vision_handler(vision_analyzer)
        else:
            self.system_control_enabled = False
            logger.warning("System control disabled - no API key")

        # Add personality adapter for compatibility
        self.personality = PersonalityAdapter(self)
        
        # Initialize weather bridge and system
        self.weather_bridge = WeatherBridge()
        
        # Initialize weather system with vision handler if available
        if vision_analyzer:
            from system_control.weather_system_config import initialize_weather_system
            from system_control.macos_controller import MacOSController
            self.controller = MacOSController()
            initialize_weather_system(vision_analyzer, self.controller)
            logger.info("Initialized unified weather system with vision")
        else:
            self.controller = None

        # Initialize command mode and confirmations
        self.command_mode = "conversation"  # conversation, system_control, workflow
        self.pending_confirmations = {}

        # System control keywords
        self.system_keywords = {
            "open",
            "close",
            "launch",
            "quit",
            "switch",
            "time",  # Add time as a system keyword
            "date",  # Add date as a system keyword
            "day",   # Add day as a system keyword
            "today", # Add today as a system keyword
            "late",  # Add late as system keyword for time queries
            "early", # Add early as system keyword for time queries
            "morning", # Time periods
            "afternoon",
            "evening",
            "night",
            "weather",  # Add weather as a system keyword
            "temperature",  # Add temperature
            "forecast",  # Add forecast
            "rain",  # Weather conditions
            "snow",
            "sunny",
            "cloudy",
            "show",
            "volume",
            "mute",
            "screenshot",
            "sleep",
            "wifi",
            "search",
            "google",
            "browse",
            "website",
            "create",
            "delete",
            "file",
            "folder",
            "routine",
            "workflow",
            "setup",
            "screen",
            "update",
            "monitor",
            "vision",
            "see",
            "check",
            "messages",
            "errors",
            "windows",
            "workspace",
            "optimize",
            "meeting",
            "privacy",
            "sensitive",
            "productivity",
            "notifications",
            "notification",
            "whatsapp",
            "discord",
            "slack",
            "telegram",
            "chrome",
            "safari",
            "spotify",
        }

        # Add special commands compatibility
        self.special_commands = {
            "system control": "Switch to system control mode",
            "conversation mode": "Switch to conversation mode",
            "morning routine": "Start morning routine",
            "development setup": "Start development setup",
            "check my screen": "Analyze what's on screen",
            "check for updates": "Check for software updates",
            "monitor updates": "Start monitoring for updates",
            "vision mode": "Enable screen comprehension",
            "meeting prep": "Prepare for meeting",
        }

        # Add voice_engine compatibility (references parent's if exists)
        if hasattr(self, "voice_engine"):
            self.voice_engine = self.voice_engine
        else:
            # Create a mock voice engine for compatibility
            class MockVoiceEngine:
                def speak(self, text):
                    logger.info(f"[Voice]: {text}")

            self.voice_engine = MockVoiceEngine()

        # Lazy load vision v2 to prevent startup hang
        self.vision_v2 = None
        self.vision_v2_enabled = False
        self._vision_v2_initialized = False
        self.workspace_intelligence_enabled = False

        # Try to initialize workspace intelligence first (Phase 1)
        try:
            from vision.jarvis_workspace_integration import JARVISWorkspaceIntelligence

            self.workspace_intelligence = JARVISWorkspaceIntelligence()
            self.workspace_intelligence_enabled = True
            logger.info(
                "Workspace intelligence (multi-window) initialized successfully"
            )
        except ImportError:
            logger.info("Workspace intelligence not available")

        # Lazy load vision to prevent startup hang
        self.vision_integration = None
        self.vision_system = None
        self.vision_enabled = False
        self.intelligent_vision_enabled = False
        self._vision_initialized = False

        # Add agent-specific responses
        self.agent_responses = {
            "app_opened": "I've opened {app} for you, {user}.",
            "app_closed": "{app} has been closed, {user}.",
            "volume_set": "Volume adjusted to {level}%, {user}.",
            "screenshot_taken": "Screenshot captured and saved, {user}.",
            "workflow_started": "Initiating {workflow} routine, {user}.",
            "confirmation_needed": "This action requires your confirmation, {user}. Say 'confirm' to proceed or 'cancel' to abort.",
            "action_completed": "[GENERIC] Task completed successfully, {user}.",
            "action_failed": "I apologize, {user}, but I couldn't complete that action.",
            "system_control_mode": "Switching to system control mode. I can now help you control your Mac.",
            "conversation_mode": "Returning to conversation mode, {user}.",
            # Time-related responses
            "current_time": "It's {time}, {user}.",
            "current_time_with_context": "It's {time}, {user}. {context}",
            "current_date_time": "It's {time} on {date}, {user}.",
            "time_with_appointment": "It's {time}, {user}. {appointment_info}",
        }

        # Initialize async command pipeline for non-blocking operations
        self.async_pipeline = get_async_pipeline(jarvis_instance=self)
        logger.info("✅ Async command pipeline initialized for non-blocking processing")

    def _ensure_vision_v2_initialized(self):
        """Lazy initialize Vision System v2.0 when needed"""
        if self._vision_v2_initialized:
            return

        self._vision_v2_initialized = True

        try:
            if VISION_V2_AVAILABLE:
                self.vision_v2 = VisionV2Integration()
                if self.vision_v2.enabled:
                    self.vision_enabled = True
                    self.vision_v2_enabled = True
                    logger.info(
                        "Vision System v2.0 initialized - ML-powered vision active"
                    )
                else:
                    self.vision_v2_enabled = False
            else:
                self.vision_v2_enabled = False
        except Exception as e:
            logger.error(f"Failed to initialize Vision System v2.0: {e}")
            self.vision_v2_enabled = False

    def _ensure_vision_initialized(self):
        """Lazy initialize vision system when needed"""
        if self._vision_initialized:
            return

        self._vision_initialized = True

        try:
            # Try to use intelligent vision first
            from vision.intelligent_vision_integration import IntelligentJARVISVision

            self.vision_integration = IntelligentJARVISVision()
            self.vision_enabled = True
            self.intelligent_vision_enabled = True
            logger.info("Intelligent vision system initialized successfully")
        except ImportError:
            # Fallback to basic vision
            try:
                from vision.screen_vision import (
                    ScreenVisionSystem,
                    JARVISVisionIntegration,
                )

                self.vision_system = ScreenVisionSystem()
                self.vision_integration = JARVISVisionIntegration(self.vision_system)
                self.vision_enabled = True
                logger.info("Basic vision system initialized")
            except ImportError:
                logger.info("Vision system not available - install vision dependencies")

        # These are now initialized in __init__
        # Command modes, pending_confirmations, system_keywords, and agent_responses
        # are already set up at initialization

    async def process_voice_input(self, text: str) -> str:
        """Process voice input with system control capabilities using async pipeline"""
        logger.info(f"[JARVIS DEBUG] process_voice_input received: '{text}'")

        # Use async pipeline for completely non-blocking processing
        try:
            result = await self.async_pipeline.process_async(text, self.user_name)
            # Pipeline now returns dict, extract response string
            return result.get("response", "I apologize, but I couldn't process that request.")
        except Exception as e:
            logger.error(f"Async pipeline error: {e}", exc_info=True)
            # Fallback to legacy processing if async pipeline fails
            return await self._legacy_process_voice_input(text)

    async def _legacy_process_voice_input(self, text: str) -> str:
        """Legacy processing method (fallback)"""
        logger.info(f"[JARVIS DEBUG] Using legacy processing for: '{text}'")

        # Check if we need to detect wake word in text
        if not self.running:
            if not self.detect_wake_word_in_text(text):
                logger.info("No wake word detected, ignoring")
                return ""
            else:
                # Wake word detected, activate
                logger.info("Wake word detected, activating JARVIS")
                self.running = True

        # Check for mode switches
        if "system control" in text.lower() or "control my mac" in text.lower():
            self.command_mode = "system_control"
            return self._format_response("system_control_mode")

        if "conversation mode" in text.lower() or "normal mode" in text.lower():
            self.command_mode = "conversation"
            return self._format_response("conversation_mode")

        # Check for pending confirmations
        if self.pending_confirmations:
            return await self._handle_confirmation(text)

        # =========================================================================
        # ROOT CAUSE FIX v9.0.0: Route surveillance through UnifiedCommandProcessor
        # =========================================================================
        # PROBLEM: Monitoring commands were being routed to claude_chatbot.generate_response()
        # which doesn't have proper surveillance detection. This caused "Application window active"
        # responses instead of proper God Mode activation.
        #
        # SOLUTION: Route ALL surveillance/monitoring commands through UnifiedCommandProcessor
        # which has robust grammar-based detection and IntelligentCommandHandler routing.
        # =========================================================================
        text_lower = text.lower()

        # Surveillance detection keywords (matches UnifiedCommandProcessor)
        monitoring_keywords = [
            "monitor", "monitoring", "watch", "watching", "track", "tracking",
            "continuous", "continuously", "real-time", "realtime", "actively",
            "surveillance", "observe", "observing", "stream", "streaming",
            "alert when", "notify when", "detect when", "look for", "scan for",
        ]

        # Multi-target patterns (God Mode triggers)
        import re
        god_mode_pattern = r"\b(all|every|each)\s+(?:\w+\s*)?(windows?|tabs?|instances?|spaces?)\b"

        # Surveillance structure patterns
        surveillance_patterns = ["for", "when", "until", "if", "whenever", "while"]

        has_monitoring = any(keyword in text_lower for keyword in monitoring_keywords)
        has_multi_target = bool(re.search(god_mode_pattern, text_lower, re.IGNORECASE))
        has_surveillance_structure = any(p in text_lower for p in surveillance_patterns)

        # Detect surveillance intent
        is_surveillance_command = (
            (has_monitoring and has_surveillance_structure) or
            (has_monitoring and has_multi_target)
        )

        if is_surveillance_command:
            logger.info(
                f"[JARVIS] 👁️ SURVEILLANCE COMMAND DETECTED: '{text}' | "
                f"monitoring={has_monitoring}, multi_target={has_multi_target}, "
                f"structure={has_surveillance_structure}"
            )
            logger.info("[JARVIS] Routing to UnifiedCommandProcessor (God Mode surveillance)")

            # Route through UnifiedCommandProcessor which has proper surveillance handling
            try:
                from api.unified_command_processor import UnifiedCommandProcessor
                processor = UnifiedCommandProcessor()
                result = await processor.process_command(text)

                if result and result.get('response'):
                    response = result['response']
                    logger.info(f"[JARVIS] ✅ Surveillance response: {response[:100]}...")
                    return response
                else:
                    logger.warning(f"[JARVIS] Surveillance result missing response: {result}")
                    return f"I've initiated monitoring, {self.user_name}. I'll alert you when I detect what you're looking for."

            except Exception as e:
                logger.error(f"[JARVIS] ❌ Surveillance routing failed: {e}", exc_info=True)
                return f"I encountered an error setting up monitoring, {self.user_name}: {str(e)}"

        # Check for document creation commands (more flexible)
        document_keywords = ["write", "create", "draft", "compose", "generate", "make", "prepare", "type"]
        document_types = ["essay", "report", "paper", "article", "document", "blog", "story", "letter"]
        
        # Also check for common typos and variations
        text_normalized = text_lower.replace("why me", "write me").replace("right me", "write me")

        has_document_keyword = any(keyword in text_normalized for keyword in document_keywords)
        has_document_type = any(dtype in text_normalized for dtype in document_types)

        if has_document_keyword and has_document_type:
            logger.info(f"[JARVIS DEBUG] DOCUMENT CREATION COMMAND DETECTED! Text: '{text}'")
            if DOCUMENT_WRITER_AVAILABLE:
                try:
                    return await self._handle_document_creation(text)
                except Exception as e:
                    logger.error(f"Error handling document creation: {e}", exc_info=True)
                    return f"I encountered an error creating the document, {self.user_name}: {str(e)}"
            else:
                return f"I apologize, {self.user_name}, but the document creation system is not available at the moment."

        # Detect if this is a system command
        if self._is_system_command(text):
            logger.info(f"Detected system command: {text}")
            return await self._handle_system_command(text)

        # Otherwise, use normal conversation processing
        logger.info(f"Processing as conversation: {text}")

        # Since parent doesn't have process_voice_input, handle conversation here
        if self.claude_chatbot:
            try:
                response = await self.claude_chatbot.generate_response(text)
                logger.info(f"Claude response: {response}")
                return response
            except Exception as e:
                logger.error(f"Error getting Claude response: {e}")
                return f"I apologize, {self.user_name}, but I encountered an error processing your request."
        else:
            return f"I'm sorry, {self.user_name}, but I need my API key to answer that question."

    def detect_wake_word_in_text(self, text: str) -> bool:
        """Detect wake word in text input"""
        text_lower = text.lower()

        # Check primary wake words
        for wake_word in self.wake_words:
            if wake_word in text_lower:
                return True

        # Check variations
        for variation in self.wake_word_variations:
            if variation in text_lower:
                return True

        # Check urgent wake words
        for urgent in self.urgent_wake_words:
            if urgent in text_lower:
                return True

        return False

    def _is_system_command(self, text: str) -> bool:
        """Detect if input is a system command - now more intelligent"""
        text_lower = text.lower()

        # Check if in system control mode
        if self.command_mode == "system_control":
            return True

        # FIRST: Check for monitoring commands - these should NOT be treated as app control
        monitoring_keywords = [
            "monitor",
            "monitoring",
            "watch",
            "watching",
            "track",
            "tracking",
            "continuous",
            "continuously",
            "real-time",
            "realtime",
            "actively",
            "surveillance",
            "observe",
            "observing",
            "stream",
            "streaming",
        ]
        screen_keywords = ["screen", "display", "desktop", "workspace", "monitor"]

        has_monitoring = any(keyword in text_lower for keyword in monitoring_keywords)
        has_screen = any(keyword in text_lower for keyword in screen_keywords)

        if has_monitoring and has_screen:
            # This is a monitoring command - NOT a system command
            return False

        # SECOND: Check for direct app control commands (open/close/launch/quit)
        # These should ALWAYS go to system control, not vision
        app_control_keywords = [
            "open",
            "close",
            "launch",
            "quit",
            "exit",
            "start",
            "kill",
            "terminate",
        ]
        for keyword in app_control_keywords:
            if keyword in text_lower:
                # Make sure it's not a question about these actions
                question_words = [
                    "what",
                    "which",
                    "how",
                    "can you",
                    "are you able",
                    "is it possible",
                ]
                if not any(q_word in text_lower for q_word in question_words):
                    # This is a direct command like "close whatsapp" or "open chrome"
                    return True

        # INTELLIGENT DETECTION: Check if query is asking about screen content
        # Instead of hardcoded phrases, look for patterns that indicate screen queries
        screen_query_indicators = [
            # Questions about visibility
            ("do i have", "any"),  # do i have any...
            ("can you see", ""),  # can you see...
            ("what", "see"),  # what do you see
            ("show me", ""),  # show me...
            ("check", ""),  # check [anything]
            ("is there", ""),  # is there...
            ("are there", ""),  # are there...
            # Questions about specific things on screen
            ("notifications", ""),
            ("messages", ""),
            ("errors", ""),
            ("windows", ""),
            ("screen", ""),
            # App-related queries (any app, not hardcoded)
            ("from", ""),  # notifications from X, messages from Y
            ("in", ""),  # what's in X
            ("on", ""),  # what's on X
        ]

        # Check if query contains screen-related question patterns
        for indicator1, indicator2 in screen_query_indicators:
            if indicator1 in text_lower:
                if not indicator2 or indicator2 in text_lower:
                    # This looks like a screen query - use vision!
                    return True

        # Also check for app names mentioned with query words
        query_words = [
            "notification",
            "message",
            "check",
            "running",
            "any",
            "have",
        ]
        if any(word in text_lower for word in query_words):
            # Could be asking about any app - let vision figure it out
            # BUT NOT if it's a direct command
            if not any(keyword in text_lower for keyword in app_control_keywords):
                return True

        # Check for system keywords (for non-vision commands)
        return any(keyword in text_lower for keyword in self.system_keywords)

    async def _handle_system_command(self, text: str) -> str:
        """Handle system control commands"""
        # Check for vision commands with expanded patterns
        text_lower = text.lower()
        
        # CHECK FOR DIRECT VISION QUERIES FIRST - these should work regardless of system_control
        vision_query_keywords = [
            "can you see", "what's on my screen", "what do you see",
            "show me my screen", "what is on my screen", "are you able to see"
        ]
        if any(keyword in text_lower for keyword in vision_query_keywords):
            logger.info(f"Direct vision query detected: {text}")
            return await self._handle_vision_command(text)
        
        # For non-vision commands, check if system control is enabled
        if not self.system_control_enabled:
            return "System control is not available. Please configure your API key."
        
        # CHECK FOR WEATHER COMMANDS - weather takes priority over time
        # because "weather for today" should be weather, not time
        if self._is_weather_command(text_lower):
            return await self._handle_weather_command(text_lower)
        
        # CHECK FOR TIME COMMANDS - handle immediately without vision
        if self._is_time_command(text_lower):
            return await self._handle_time_command(text_lower)

        # CHECK FOR LOCK/UNLOCK COMMANDS FIRST - these must never go to vision
        if any(phrase in text_lower for phrase in ["lock my screen", "unlock my screen", "lock screen", "unlock screen", "lock the screen", "unlock the screen"]):
            logger.info(f"Lock/unlock command detected: {text}")
            # Route to system control for proper lock/unlock handling
            try:
                from api.unified_command_processor import UnifiedCommandProcessor
                processor = UnifiedCommandProcessor()
                result = await processor.process_command(text)
                if result and result.get('response'):
                    return result['response']
            except Exception as e:
                logger.error(f"Failed to process lock/unlock command: {e}")
                return "I couldn't process the lock/unlock command. Please try again."

        # CHECK FOR ACTION COMMANDS - these should execute, not analyze
        action_commands = {
            "close": ["close", "quit", "exit", "terminate"],
            "open": ["open", "launch", "start", "run"],
            "switch": ["switch to", "activate", "focus on"],
            "system": [
                "set volume",
                "mute",
                "unmute",
                "screenshot",
                "restart",
                "shutdown",
            ],
            "file": ["create", "delete", "move", "copy", "rename"],
        }

        # Check if this is a direct action command
        is_action_command = False
        logger.debug(f"Checking if '{text_lower}' is an action command")
        for action_type, keywords in action_commands.items():
            logger.debug(
                f"Checking action type '{action_type}' with keywords: {keywords}"
            )
            if any(keyword in text_lower for keyword in keywords):
                logger.debug(f"Found keyword match for action type: {action_type}")
                # This looks like an action command
                # But make sure it's not a question about the action
                question_words = [
                    "what",
                    "which",
                    "do i have",
                    "are there",
                    "show me",
                    "tell me",
                    "list",
                ]
                if not any(q_word in text_lower for q_word in question_words):
                    is_action_command = True
                    logger.info(f"Identified as action command of type: {action_type}")
                    break

        # NEW: Check if this is a vision query about screen content
        # These should NEVER go to command interpreter
        from api.vision_query_bypass import VisionQueryBypass
        if VisionQueryBypass.should_bypass_interpretation(text):
            logger.info(f"Vision query detected, bypassing command interpretation: {text}")
            # Route directly to vision handler
            return await self._handle_vision_command(text)
        
        # If it's an action command, skip vision and execute directly
        if is_action_command:
            logger.info(f"Processing action command directly: {text}")
            # Skip all vision processing and go directly to command execution
        # INTELLIGENT ROUTING: Let vision handle ANY query about screen content
        # Don't hardcode specific apps or patterns - let vision figure it out
        elif self.workspace_intelligence_enabled and not is_action_command:
            # If the query seems to be asking about screen content, use workspace intelligence
            # This includes ANY app, ANY notification type, ANY message type
            screen_content_keywords = [
                "notification",
                "message",
                "error",
                "window",
                "screen",
                "running",
                "check",
                "see",
                "show",
                "have",
                "any",
                "what",
                "where",
                "from",
                "in",
                "on",
            ]

            # If query contains screen-related words, let vision analyze it
            # But be smart about it - don't match partial words like "what" in "whatsapp"
            import re

            word_boundary_keywords = [
                "what",
                "where",
                "have",
                "any",
                "in",
                "on",
                "from",
            ]
            other_keywords = [
                k for k in screen_content_keywords if k not in word_boundary_keywords
            ]

            # Check word boundaries for short words
            has_keyword = False
            for keyword in word_boundary_keywords:
                if re.search(r"\b" + keyword + r"\b", text_lower):
                    has_keyword = True
                    break

            # Check regular contains for longer words
            if not has_keyword:
                has_keyword = any(keyword in text_lower for keyword in other_keywords)

            if has_keyword:
                return await self._handle_workspace_command(text)

        # Standard vision triggers (only if not an action command and not lock/unlock)
        if not is_action_command and not any(word in text_lower for word in ["lock", "unlock"]):
            vision_triggers = [
                "screen",
                "update",
                "monitor",
                "vision",
                "see",
                "what am i",
                "what i'm",
                "working on",
                "cursor",
                "analyze",
                "look at",
                "show me",
                "tell me about",
                "describe",
                "can you see",
                "do you see",
            ]
            if any(trigger in text_lower for trigger in vision_triggers):
                return await self._handle_vision_command(text)

        try:
            # =====================================================================
            # ROOT CAUSE FIX v9.0.0: Route surveillance through UnifiedCommandProcessor
            # =====================================================================
            text_lower = text.lower()

            # Use same detection logic as UnifiedCommandProcessor
            monitoring_keywords = [
                "monitor", "monitoring", "watch", "watching", "track", "tracking",
                "alert when", "notify when", "detect when", "look for", "scan for",
            ]
            surveillance_patterns = ["for", "when", "until", "if", "whenever", "while"]
            god_mode_pattern = r"\b(all|every|each)\s+(?:\w+\s*)?(windows?|tabs?|instances?|spaces?)\b"

            has_monitoring = any(keyword in text_lower for keyword in monitoring_keywords)
            has_multi_target = bool(re.search(god_mode_pattern, text_lower, re.IGNORECASE))
            has_surveillance_structure = any(p in text_lower for p in surveillance_patterns)

            is_surveillance = (has_monitoring and has_surveillance_structure) or (has_monitoring and has_multi_target)

            if is_surveillance:
                # Route through UnifiedCommandProcessor for proper God Mode handling
                logger.info(f"[JARVIS] 👁️ Surveillance detected in system path: '{text}'")
                from api.unified_command_processor import UnifiedCommandProcessor
                processor = UnifiedCommandProcessor()
                result = await processor.process_command(text)
                if result and result.get('response'):
                    return result['response']
                return f"I've initiated monitoring, {self.user_name}."

            # Get system context
            context = {
                "mode": self.command_mode,
                "open_apps": self.command_interpreter.controller.list_open_applications(),
                "time": datetime.now().strftime("%H:%M"),
                "user": self.user_name,
            }

            # Interpret command using Claude
            intent = await self.command_interpreter.interpret_command(text, context)

            # Log the interpreted command
            logger.info(
                f"Command interpreted: {intent.action} on {intent.target} "
                f"(confidence: {intent.confidence:.2f})"
            )

            # Check confidence threshold
            if intent.confidence < 0.6:
                return f"I'm not sure what you want me to do, {self.user_name}. Could you please rephrase?"

            # Execute the command
            result = await self.command_interpreter.execute_intent(intent)

            # Handle results
            if result.follow_up_needed:
                # Store pending confirmation
                confirmation_id = f"confirm_{datetime.now().timestamp()}"
                self.pending_confirmations[confirmation_id] = {
                    "intent": intent,
                    "data": result.data,
                    "timestamp": datetime.now(),
                }
                return (
                    self._format_response("confirmation_needed") + " " + result.message
                )

            elif result.success:
                # Format success response
                if intent.category == CommandCategory.APPLICATION:
                    # Use the actual result message which contains intelligent status info
                    if "already running" in result.message:
                        return f"{result.message}, {self.user_name}."
                    elif "not running" in result.message:
                        return f"{result.message}, {self.user_name}."
                    elif intent.action == "open_app":
                        return self._format_response("app_opened", app=intent.target)
                    elif intent.action == "close_app":
                        return self._format_response("app_closed", app=intent.target)
                elif intent.category == CommandCategory.SYSTEM:
                    if intent.action == "set_volume":
                        return self._format_response(
                            "volume_set", level=intent.parameters.get("level", 50)
                        )
                    elif intent.action == "screenshot":
                        return self._format_response("screenshot_taken")
                elif intent.category == CommandCategory.WORKFLOW:
                    return self._format_response(
                        "workflow_started", workflow=intent.target
                    )

                # Generic success
                return self._format_response("action_completed") + " " + result.message

            else:
                # Handle failure
                return self._format_response("action_failed") + " " + result.message

        except Exception as e:
            logger.error(f"System command error: {e}")
            return f"I encountered an error, {self.user_name}. Please try again."

    async def _handle_document_creation(self, text: str) -> str:
        """Handle document creation commands"""
        logger.info(f"[DOCUMENT WRITER] Processing document creation: '{text}'")

        try:
            # Parse the document request from the command
            intent = {"text": text}  # Minimal intent structure
            document_request = parse_document_request(text, intent)

            logger.info(f"[DOCUMENT WRITER] Parsed request: {document_request.document_type.value} on '{document_request.topic}'")

            # Get the document writer
            writer = get_document_writer()

            # Create the document (this will handle Google Docs API + Claude streaming)
            result = await writer.create_document(
                request=document_request,
                progress_callback=None,  # Could add voice narration here
                websocket=None
            )

            if result.get("success"):
                doc_url = result.get("document_url", "")
                word_count = result.get("word_count", 0)
                # More natural responses with less "Sir"
                responses = [
                    f"Your {document_request.document_type.value} on '{document_request.topic}' is ready. I've written {word_count} words and it's open in Google Docs.",
                    f"I've completed your {document_request.document_type.value} about {document_request.topic}. {word_count} words, now open in your browser.",
                    f"Done! Your {word_count}-word {document_request.document_type.value} on '{document_request.topic}' is ready in Google Docs, {self.user_name}.",
                ]
                import random
                return random.choice(responses)
            else:
                error = result.get("error", "Unknown error")
                return f"I ran into an issue creating your document: {error}"

        except Exception as e:
            logger.error(f"[DOCUMENT WRITER] Error: {e}", exc_info=True)
            return f"I apologize, {self.user_name}, but I encountered an error while creating your document: {str(e)}"

    async def _handle_confirmation(self, text: str) -> str:
        """Handle confirmation responses"""
        text_lower = text.lower()

        if "confirm" in text_lower or "yes" in text_lower or "proceed" in text_lower:
            # Execute the pending action
            if self.pending_confirmations:
                # Get the most recent confirmation
                conf_id = list(self.pending_confirmations.keys())[-1]
                confirmation = self.pending_confirmations[conf_id]

                # Check if it's a file deletion
                if confirmation["data"].get("action") == "delete":
                    path = confirmation["data"]["path"]
                    success, message = self.command_interpreter.controller.delete_file(
                        path, confirm=False
                    )

                    # Clear confirmation
                    del self.pending_confirmations[conf_id]

                    if success:
                        return self._format_response("action_completed") + " " + message
                    else:
                        return self._format_response("action_failed") + " " + message

        elif "cancel" in text_lower or "no" in text_lower or "abort" in text_lower:
            # Cancel pending actions
            self.pending_confirmations.clear()
            return f"Action cancelled, {self.user_name}."

        return "Please say 'confirm' to proceed or 'cancel' to abort."

    async def _handle_workspace_command(self, text: str) -> str:
        """Handle multi-window workspace commands"""
        try:
            response = await self.workspace_intelligence.handle_workspace_command(text)
            return response
        except Exception as e:
            logger.error(f"Workspace command error: {e}")
            # Fallback to regular vision if workspace fails
            return await self._handle_vision_command(text)

    async def _handle_vision_command(self, text: str) -> str:
        """Handle vision-related commands with immediate response and async processing"""
        import asyncio
        text_lower = text.lower()
        
        # Quick response patterns for common vision queries
        quick_responses = {
            "can you see my screen": "Yes, I can see your screen. ",
            "what's on my screen": "Looking at your screen now. ",
            "what do you see": "I'm analyzing what's visible. ",
            "are you able to see": "Yes, I have screen access. ",
            "do you see": "Yes, I can see ",
        }
        
        # Find matching quick response
        quick_prefix = None
        for pattern, response in quick_responses.items():
            if pattern in text_lower:
                quick_prefix = response
                break
                
        # =========================================================================
        # ROOT CAUSE FIX v9.0.0: Route surveillance through UnifiedCommandProcessor
        # =========================================================================
        monitoring_keywords = [
            "monitor", "monitoring", "watch", "watching", "track", "tracking",
            "alert when", "notify when", "detect when", "look for", "scan for",
        ]
        surveillance_patterns = ["for", "when", "until", "if", "whenever", "while"]
        god_mode_pattern = r"\b(all|every|each)\s+(?:\w+\s*)?(windows?|tabs?|instances?|spaces?)\b"

        has_monitoring = any(keyword in text_lower for keyword in monitoring_keywords)
        has_multi_target = bool(re.search(god_mode_pattern, text_lower, re.IGNORECASE))
        has_surveillance_structure = any(p in text_lower for p in surveillance_patterns)

        is_surveillance = (has_monitoring and has_surveillance_structure) or (has_monitoring and has_multi_target)

        if is_surveillance:
            logger.info(f"[JARVIS] 👁️ Surveillance detected in vision path: '{text}'")
            try:
                from api.unified_command_processor import UnifiedCommandProcessor
                processor = UnifiedCommandProcessor()
                result = await asyncio.wait_for(
                    processor.process_command(text),
                    timeout=30.0  # Allow time for God Mode setup
                )
                if result and result.get('response'):
                    return result['response']
                return f"I've initiated monitoring, {self.user_name}."
            except asyncio.TimeoutError:
                return f"The monitoring setup is taking longer than expected, {self.user_name}."
            except Exception as e:
                logger.error(f"Error handling surveillance command: {e}")
                return f"I encountered an issue with monitoring setup: {str(e)}"
                
        # For regular vision queries, provide immediate response then analyze
        try:
            # If we have Claude chatbot, use it for vision
            if hasattr(self, 'claude_chatbot') and self.claude_chatbot:
                try:
                    # For simple "can you see" queries, return immediately
                    if "can you see" in text_lower and "screen" in text_lower:
                        # Quick check if screen access is available
                        try:
                            # Try a quick screenshot test
                            import subprocess
                            result = subprocess.run(
                                ['screencapture', '-x', '-t', 'png', '/dev/null'],
                                capture_output=True,
                                timeout=0.5
                            )
                            if result.returncode == 0:
                                # Get list of visible apps for immediate response
                                script = '''
                                tell application "System Events"
                                    get name of (every process whose visible is true)
                                end tell
                                '''
                                app_result = subprocess.run(
                                    ['osascript', '-e', script],
                                    capture_output=True,
                                    text=True,
                                    timeout=0.5
                                )
                                
                                if app_result.returncode == 0:
                                    apps = app_result.stdout.strip().split(", ")
                                    visible_apps = [app for app in apps if app and app != "Finder"][:3]
                                    if visible_apps:
                                        app_list = ', '.join(visible_apps)
                                        if len(visible_apps) == 1:
                                            return f"Yes, I can see your screen. You have {app_list} open."
                                        else:
                                            return f"Yes, I can see your screen. You have {app_list} among other applications open."
                                    else:
                                        return "Yes, I can see your screen. Your desktop is visible with multiple applications."
                                else:
                                    return "Yes, I can see your screen and monitor your desktop activities."
                            else:
                                return "I need screen recording permission to see your display. Please enable it in System Preferences > Security & Privacy > Privacy > Screen Recording."
                        except Exception as e:
                            logger.debug(f"Quick screen check failed: {e}")
                            return "Yes, I have access to screen monitoring capabilities."
                    
                    # For more complex vision queries, use full analysis with timeout
                    logger.info(f"Processing vision query: {text}")
                    response = await asyncio.wait_for(
                        self.claude_chatbot.analyze_screen_with_vision(text),
                        timeout=8.0  # 8 second timeout for vision analysis
                    )
                    
                    if quick_prefix and not response.startswith(quick_prefix):
                        response = quick_prefix + response
                        
                    return response
                    
                except asyncio.TimeoutError:
                    logger.warning("Vision analysis timed out")
                    if quick_prefix:
                        # Provide basic response based on what we can quickly determine
                        return quick_prefix + "The detailed analysis is taking longer than expected. I can see your desktop with multiple applications open."
                    return "I can see your screen, but the detailed analysis is taking a moment. You have several applications open on your desktop."
                    
                except Exception as e:
                    logger.error(f"Vision error: {e}")
                    if "screen recording" in str(e).lower():
                        return "I need screen recording permission to see your display. Please enable it in System Preferences > Security & Privacy > Privacy > Screen Recording."
                    return "I can access screen monitoring, but encountered an issue with the detailed analysis."
            
            # Fallback if no Claude chatbot
            if quick_prefix:
                return quick_prefix + "You have multiple applications open on your desktop."
            return "I have screen access capabilities. You appear to have several applications running."
            
        except Exception as e:
            logger.error(f"Vision command error: {e}")
            return "I can monitor your screen, but encountered a temporary issue. Please try again."

    def _format_response(self, response_type: str, **kwargs) -> str:
        """Format agent responses"""
        # Try agent_responses first, then fall back to default
        template = self.agent_responses.get(response_type, "")

        if not template:
            # Provide a default response if not found
            template = f"Command {response_type} completed, {{user}}."

        # Add default user name
        kwargs["user"] = kwargs.get("user", self.user_name)

        return template.format(**kwargs)

    async def execute_workflow(self, workflow_name: str) -> str:
        """Execute predefined workflows with voice feedback"""

        voice_feedback = {
            "morning_routine": [
                "Starting your morning routine, {user}.",
                "Opening your email...",
                "Checking your calendar...",
                "Getting today's weather...",
                "Morning routine complete, {user}. Have a productive day!",
            ],
            "development_setup": [
                "Setting up your development environment, {user}.",
                "Launching Visual Studio Code...",
                "Opening terminal...",
                "Starting Docker...",
                "Development environment ready, {user}!",
            ],
            "meeting_prep": [
                "Preparing for your meeting, {user}.",
                "Adjusting volume...",
                "Closing distractions...",
                "Opening Zoom...",
                "You're all set for your meeting, {user}.",
            ],
        }

        if workflow_name not in voice_feedback:
            yield f"Unknown workflow: {workflow_name}"
            return

        # Execute workflow with voice feedback
        feedback_messages = voice_feedback[workflow_name]

        for i, message in enumerate(feedback_messages):
            # Speak the message
            formatted_message = message.format(user=self.user_name)
            if i == 0:
                yield formatted_message

            # Execute corresponding action
            if i < len(feedback_messages) - 1:
                await asyncio.sleep(1)  # Brief pause between actions

        # Execute actual workflow
        success, result = await self.command_interpreter.controller.execute_workflow(
            workflow_name
        )

        if success:
            yield feedback_messages[-1].format(user=self.user_name)
        else:
            yield f"There was an issue with the workflow, {self.user_name}: {result}"

    def get_capabilities(self) -> Dict[str, List[str]]:
        """Get current agent capabilities"""
        capabilities = super().get_capabilities()

        if self.system_control_enabled:
            capabilities["system_control"] = {
                "applications": [
                    "Open any macOS application",
                    "Close applications",
                    "Switch between apps",
                    "List open applications",
                ],
                "files": [
                    "Create files and folders",
                    "Open documents",
                    "Search for files",
                    "Delete files (with confirmation)",
                ],
                "system": [
                    "Control volume",
                    "Take screenshots",
                    "Toggle WiFi",
                    "Sleep display",
                ],
                "web": [
                    "Open websites",
                    "Perform web searches",
                    "Research information",
                ],
                "workflows": [
                    "Morning routine",
                    "Development setup",
                    "Meeting preparation",
                ],
            }

        return capabilities

    def get_help_commands(self) -> str:
        """Get help on available commands"""
        help_text = super().get_help_commands()

        if self.system_control_enabled:
            help_text += """
            
System Control Commands:
- "Open [application]" - Launch any application
- "Close [application]" - Quit an application  
- "Set volume to [X]%" - Adjust system volume
- "Take a screenshot" - Capture screen
- "Search for [query]" - Web search
- "Start my morning routine" - Execute workflow
- "Switch to system control mode" - Focus on system commands
            """

        return help_text
    
    def _is_time_command(self, text: str) -> bool:
        """Check if this is a time-related query using dynamic pattern matching"""
        import re
        
        # Convert to lowercase for case-insensitive matching
        text_lower = text.lower().strip()
        
        # EXCLUDE weather queries - they should not be treated as time queries
        weather_indicators = ['weather', 'temperature', 'forecast', 'rain', 'snow', 'sunny', 'cloudy']
        if any(indicator in text_lower for indicator in weather_indicators):
            return False
        
        # Dynamic regex patterns for flexible matching
        time_regex_patterns = [
            # Time queries with various formats
            r'\b(what|whats|what\'s|tell|give|show|check|get|display)\s*(me)?\s*(the)?\s*time\b',
            r'\btime\s*(is\s*it|now|please)\b',
            r'\b(current|present)\s*time\b',
            
            # Date queries with flexibility
            r'\b(what|whats|what\'s|tell|give|show|check)\s*(me)?\s*(the)?\s*(today\'s|todays|current)?\s*date\b',
            r'\b(what|which)\s*(day|date)\s*(is\s*)?(it|today)\b',
            r'\btoday\'s\s*(date|day)\b',
            
            # Hour/clock references
            r'\b(what|which)\s*hour\b',
            r'\b(check|look\s*at)\s*(the)?\s*clock\b',
            
            # Combined time/date queries
            r'\b(time|date)\s*and\s*(time|date)\b',
            r'\b(date|day)\s*(and|&)\s*time\b',
            
            # Casual time queries
            r'\b(got|have|know)\s*(the)?\s*time\b',
            r'\bdo\s*you\s*(have|know)\s*(the)?\s*time\b',
            
            # Time period queries
            r'\bis\s*it\s*(morning|afternoon|evening|night|late|early|noon|midnight)\b',
            r'\bwhat\s*time\s*of\s*(day|night)\b',
            r'\b(morning|evening|night)\s*yet\b',
            
            # Relative time queries
            r'\bhow\s*(late|early)\s*(is\s*it)?\b',
            r'\bam\s*i\s*(late|early)\b',
            
            # International variations
            r'\b(hora|heure|zeit|tempo)\b',  # Spanish/French/German/Italian for "time"
            r'\b(fecha|date|datum|data)\b',  # Date in multiple languages
            
            # Natural language variations
            r'\bwhat\s*o\'?clock\b',
            r'\b(when|what)\s*is\s*now\b'
        ]
        
        # Check regex patterns
        for pattern in time_regex_patterns:
            if re.search(pattern, text_lower):
                return True
        
        # Fuzzy keyword matching for typos and variations
        time_keywords = {
            'time', 'date', 'day', 'hour', 'clock', 'today', 'now',
            'morning', 'afternoon', 'evening', 'night', 'late', 'early',
            'oclock', "o'clock", 'midnight', 'noon', 'midday'
        }
        
        # Check for keywords with edit distance tolerance
        words = text_lower.split()
        for word in words:
            # Direct match
            if word in time_keywords:
                return True
            
            # Check for partial matches (e.g., "timing" contains "time")
            for keyword in time_keywords:
                if len(keyword) > 3 and keyword in word:
                    return True
        
        return False
    
    async def _handle_time_command(self, text: str) -> str:
        """Handle time-related commands with robust, context-aware responses"""
        from datetime import datetime
        import pytz
        import re
        import locale
        import platform
        
        try:
            # ALWAYS get real system time - NO manipulation
            import subprocess
            
            # Get actual system time directly
            try:
                # Use date command for REAL system time
                result = subprocess.run(
                    ["date", "+%Y-%m-%d %H:%M:%S %Z %A %B"],
                    capture_output=True,
                    text=True,
                    timeout=1
                )
                
                if result.returncode == 0:
                    # Parse real system time
                    system_time = result.stdout.strip()
                    parts = system_time.split()
                    
                    # Extract date and time
                    date_parts = parts[0].split('-')
                    time_parts = parts[1].split(':')
                    timezone_name = parts[2] if len(parts) > 2 else "local"
                    
                    # Create datetime from REAL system values
                    now = datetime(
                        year=int(date_parts[0]),
                        month=int(date_parts[1]),
                        day=int(date_parts[2]),
                        hour=int(time_parts[0]),
                        minute=int(time_parts[1]),
                        second=int(time_parts[2])
                    )
                    
                    logger.info(f"Using REAL system time: {system_time}")
                else:
                    # Fallback but with warning
                    now = datetime.now()
                    timezone_name = "local"
                    logger.warning("Failed to get real system time, using Python datetime")
                    
            except Exception as e:
                logger.error(f"Error getting system time: {e}")
                now = datetime.now()
                timezone_name = "local"
            
            # Intelligent query analysis
            query_analysis = self._analyze_time_query(text)
            
            # Dynamic format selection based on user preferences and locale
            time_format = self._get_time_format(query_analysis)
            date_format = self._get_date_format(query_analysis)
            
            # Format time and date dynamically
            time_str = now.strftime(time_format)
            date_str = now.strftime(date_format) if query_analysis['wants_date'] else None
            
            # Get dynamic context
            context_info = await self._get_dynamic_time_context(now, query_analysis)
            
            # Build response dynamically
            response_parts = []
            
            # Add time if requested or by default
            if query_analysis['wants_time'] or not query_analysis['wants_date']:
                response_parts.append(f"It's {time_str}")
            
            # Add date if requested
            if query_analysis['wants_date']:
                if response_parts:
                    response_parts.append(f"on {date_str}")
                else:
                    response_parts.append(f"Today is {date_str}")
            
            # Add timezone if requested or if not local
            if query_analysis['wants_timezone'] or timezone_name != "local":
                response_parts.append(f"({timezone_name})")
            
            # Construct base response
            base_response = " ".join(response_parts)
            
            # Add contextual information
            if context_info:
                if query_analysis['wants_context'] or query_analysis['context_type']:
                    base_response += f". {context_info}"
            
            # Add personalization
            if self.user_name and self.user_name != "User":
                base_response += f", {self.user_name}"
            
            return base_response
                    
        except Exception as e:
            logger.error(f"Error handling time command: {e}", exc_info=True)
            # Robust fallback with multiple attempts
            try:
                fallback_time = datetime.now().strftime("%-I:%M %p")
                return f"It's {fallback_time}, {self.user_name}."
            except:
                # Ultimate fallback
                return f"I'm having trouble accessing the time right now, {self.user_name}."
    
    async def _get_dynamic_time_context(self, dt: datetime, query_analysis: dict) -> str:
        """Get dynamic contextual information about the time"""
        hour = dt.hour
        minute = dt.minute
        weekday = dt.strftime("%A")
        month = dt.month
        
        # Dynamic time period calculation
        time_periods = [
            {"start": 0, "end": 4, "name": "late night", "greeting": "Still up"},
            {"start": 4, "end": 6, "name": "very early morning", "greeting": "You're up early"},
            {"start": 6, "end": 9, "name": "early morning", "greeting": "Good morning"},
            {"start": 9, "end": 12, "name": "morning", "greeting": "Good morning"},
            {"start": 12, "end": 13, "name": "midday", "greeting": "Good afternoon"},
            {"start": 13, "end": 17, "name": "afternoon", "greeting": "Good afternoon"},
            {"start": 17, "end": 19, "name": "early evening", "greeting": "Good evening"},
            {"start": 19, "end": 22, "name": "evening", "greeting": "Good evening"},
            {"start": 22, "end": 24, "name": "night", "greeting": "Good night"}
        ]
        
        current_period = next((p for p in time_periods if p["start"] <= hour < p["end"]), time_periods[-1])
        
        # Dynamic activity suggestions based on multiple factors
        context_parts = []
        
        # Add greeting if appropriate
        if query_analysis.get('wants_greeting', True):
            context_parts.append(current_period["greeting"])
        
        # Dynamic contextual observations
        contextual_observations = []
        
        # Time-based observations
        if hour < 5:
            contextual_observations.append("It's quite late")
        elif hour < 6:
            contextual_observations.append("It's very early")
        elif 22 <= hour:
            contextual_observations.append("Getting late")
        
        # Meal time suggestions
        meal_times = [
            {"start": 6, "end": 10, "meal": "breakfast"},
            {"start": 11.5, "end": 14, "meal": "lunch"},
            {"start": 17.5, "end": 20, "meal": "dinner"}
        ]
        
        current_time_decimal = hour + minute / 60.0
        for meal in meal_times:
            if meal["start"] <= current_time_decimal <= meal["end"]:
                contextual_observations.append(f"Good time for {meal['meal']}")
                break
        
        # Work/rest suggestions based on day and time
        is_weekend = weekday in ["Saturday", "Sunday"]
        is_work_hours = 9 <= hour < 17 and not is_weekend
        
        if is_work_hours:
            if hour < 10:
                contextual_observations.append("Hope you have a productive day")
            elif 15 <= hour < 17:
                contextual_observations.append("The workday is winding down")
        elif is_weekend and 10 <= hour < 12:
            contextual_observations.append("Enjoy your weekend")
        
        # Seasonal context
        seasonal_contexts = {
            12: "Winter solstice season",
            1: "New Year season",
            3: "Spring is approaching",
            6: "Summer solstice season",
            9: "Autumn is here",
            10: "Fall season"
        }
        
        if month in seasonal_contexts and query_analysis.get('wants_extended_context'):
            contextual_observations.append(seasonal_contexts[month])
        
        # Activity-based suggestions from calendar
        if query_analysis.get('wants_activities'):
            calendar_context = await self._check_calendar_context(dt)
            if calendar_context:
                contextual_observations.append(calendar_context)
        
        # Combine observations intelligently
        if contextual_observations:
            context_parts.extend(contextual_observations[:2])  # Limit to avoid verbosity
        
        # Build final context
        context = ". ".join(filter(None, context_parts))
        
        # Add punctuation if needed
        if context and not context.endswith(('.', '!', '?')):
            context += "."
        
        return context
    
    def _analyze_time_query(self, text: str) -> dict:
        """Analyze the time query to understand user intent"""
        import re
        
        text_lower = text.lower()
        
        analysis = {
            'wants_time': any(word in text_lower for word in ['time', 'clock', 'hour']),
            'wants_date': any(word in text_lower for word in ['date', 'day', 'today']),
            'wants_timezone': any(word in text_lower for word in ['timezone', 'tz', 'zone']),
            'wants_context': any(word in text_lower for word in ['context', 'details', 'more']),
            'wants_greeting': not any(word in text_lower for word in ['just', 'only', 'simple']),
            'wants_activities': any(word in text_lower for word in ['calendar', 'events', 'schedule']),
            'wants_extended_context': any(word in text_lower for word in ['full', 'complete', 'everything']),
            'context_type': None,
            'format_preference': None
        }
        
        # Detect specific context requests
        if re.search(r'\b(morning|afternoon|evening|night)\b', text_lower):
            analysis['context_type'] = 'time_period'
        elif re.search(r'\b(late|early)\b', text_lower):
            analysis['context_type'] = 'relative_time'
        elif re.search(r'\b(meal|lunch|dinner|breakfast)\b', text_lower):
            analysis['context_type'] = 'meal_time'
        
        # Detect format preferences
        if re.search(r'\b(24|twenty.?four|military)\b', text_lower):
            analysis['format_preference'] = '24h'
        elif re.search(r'\b(12|twelve|am|pm)\b', text_lower):
            analysis['format_preference'] = '12h'
        
        return analysis
    
    def _get_time_format(self, query_analysis: dict) -> str:
        """Get appropriate time format based on analysis and locale"""
        import locale
        
        # Check user preference from query
        if query_analysis.get('format_preference') == '24h':
            return "%H:%M"  # 24-hour format
        elif query_analysis.get('format_preference') == '12h':
            return "%-I:%M %p"  # 12-hour format with AM/PM
        
        # Try to detect system locale preference
        try:
            system_locale = locale.getlocale()[0]
            # Countries that typically use 24-hour format
            if system_locale and any(country in system_locale for country in ['de', 'fr', 'es', 'it', 'ru', 'zh', 'jp', 'ko']):
                return "%H:%M"
        except:
            pass
        
        # Default to 12-hour format with AM/PM
        return "%-I:%M %p"
    
    def _get_date_format(self, query_analysis: dict) -> str:
        """Get appropriate date format based on analysis and locale"""
        import locale
        
        # Check if user wants full or abbreviated format
        if query_analysis.get('wants_extended_context'):
            return "%A, %B %-d, %Y"  # Full format: Monday, September 9, 2024
        
        # Try to detect system locale preference
        try:
            system_locale = locale.getlocale()[0]
            if system_locale:
                if 'US' in system_locale:
                    return "%A, %B %-d"  # Monday, September 9
                elif 'GB' in system_locale:
                    return "%A, %-d %B"  # Monday, 9 September
                elif any(eu in system_locale for eu in ['de', 'fr', 'es', 'it']):
                    return "%A %-d %B"  # Monday 9 September
        except:
            pass
        
        # Default format
        return "%A, %B %-d"  # Monday, September 9
    
    def _get_macos_timezone(self) -> Optional[str]:
        """Get timezone on macOS"""
        import subprocess
        try:
            result = subprocess.run(['systemsetup', '-gettimezone'], 
                                  capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                output = result.stdout.strip()
                if "Time Zone:" in output:
                    return output.split("Time Zone:")[1].strip()
        except:
            pass
        return None
    
    def _get_unix_timezone(self) -> Optional[str]:
        """Get timezone on Unix/Linux systems"""
        try:
            # Try reading /etc/timezone
            if os.path.exists('/etc/timezone'):
                with open('/etc/timezone', 'r') as f:
                    return f.read().strip()
            
            # Try reading the symlink
            if os.path.exists('/etc/localtime'):
                import os
                tz_path = os.path.realpath('/etc/localtime')
                if '/zoneinfo/' in tz_path:
                    return tz_path.split('/zoneinfo/')[-1]
        except:
            pass
        return None
    
    def _get_python_timezone(self) -> Optional[str]:
        """Get timezone using Python's time module"""
        try:
            import time
            if hasattr(time, 'tzname'):
                return time.tzname[time.daylight]
        except:
            pass
        return None
    
    async def _check_calendar_context(self, current_time: datetime) -> Optional[str]:
        """Check for relevant calendar events near the current time"""
        try:
            # Import calendar bridge
            from system_control.calendar_bridge import get_calendar_bridge
            
            # Get calendar bridge instance
            calendar_bridge = get_calendar_bridge()
            
            # Get smart calendar context
            context = await calendar_bridge.get_smart_time_context(current_time)
            
            if context:
                logger.debug(f"Calendar context: {context}")
                return context
            
            # If no smart context, check for simple next event
            current_event, next_event, upcoming_events = await calendar_bridge.get_contextual_info(current_time)
            
            # Priority 1: Current event
            if current_event:
                return calendar_bridge.format_event_context(current_event, current_time, "current")
            
            # Priority 2: Urgent next event (within 30 minutes)
            if next_event:
                minutes_until = next_event.time_until_minutes(current_time)
                if minutes_until <= 30:
                    return calendar_bridge.format_event_context(next_event, current_time, "next")
                
                # Priority 3: Important event types within 2 hours
                from system_control.calendar_bridge import EventType
                if (minutes_until <= 120 and 
                    next_event.event_type in [EventType.MEETING, EventType.APPOINTMENT]):
                    return calendar_bridge.format_event_context(next_event, current_time, "next")
            
            # Priority 4: Multiple events warning
            if len(upcoming_events) >= 3:
                events_soon = sum(1 for e in upcoming_events if e.time_until_minutes() <= 60)
                if events_soon >= 2:
                    return f"You have {events_soon} events in the next hour"
            
            # No relevant calendar context
            return None
            
        except ImportError as e:
            logger.debug(f"Calendar bridge not available: {e}")
            return None
        except Exception as e:
            logger.debug(f"Calendar context check failed: {e}")
            return None
    
    def _is_weather_command(self, text: str) -> bool:
        """Check if this is a weather-related query using pattern matching"""
        return self.weather_bridge.is_weather_query(text)
    
    async def _force_vision_weather_read(self) -> str:
        """Force vision to read weather from Weather app as last resort"""
        try:
            # Open Weather app first
            import subprocess
            subprocess.run(['open', '-a', 'Weather'], check=False)
            await asyncio.sleep(2)  # Wait for app to open
            
            # Use vision to read whatever is on screen
            if hasattr(self, 'vision_handler') and self.vision_handler:
                # Use fast weather analysis instead of describe_screen
                if hasattr(self.vision_handler, 'analyze_weather_fast'):
                    result_dict = await self.vision_handler.analyze_weather_fast()
                    if result_dict.get('success') and result_dict.get('analysis'):
                        return result_dict['analysis']
                    
                # Fallback to describe_screen if fast analysis not available
                vision_params = {
                    'query': 'Look at the Weather app on screen. Read the current temperature number, weather condition (sunny/cloudy/etc), and today\'s high/low temperatures. Be specific with the exact numbers you see.'
                }
                result = await self.vision_handler.describe_screen(vision_params)
                
                if result.success and result.description:
                    # Extract basic weather info from description
                    description = result.description
                    
                    # Look for temperature patterns
                    import re
                    temp_match = re.search(r'(\d+)\s*(?:°|degrees)', description)
                    current_temp = temp_match.group(1) if temp_match else "unavailable"
                    
                    # Look for conditions
                    conditions = ['sunny', 'cloudy', 'rainy', 'clear', 'overcast', 'partly cloudy', 'snow']
                    found_condition = "current conditions"
                    for condition in conditions:
                        if condition in description.lower():
                            found_condition = condition
                            break
                    
                    response = f"Based on the Weather app, it's currently {current_temp}° and {found_condition}"
                    
                    # Add any additional details found
                    high_match = re.search(r'high[:\s]*(\d+)', description.lower())
                    low_match = re.search(r'low[:\s]*(\d+)', description.lower())
                    
                    if high_match and low_match:
                        response += f" with a high of {high_match.group(1)}° and low of {low_match.group(1)}° today"
                    
                    if self.user_name and self.user_name != "User":
                        response += f", {self.user_name}"
                    
                    return response
            
            # If all else fails, return a minimal response
            return f"The Weather app is now open with your local forecast, {self.user_name if self.user_name else 'Sir'}."
            
        except Exception as e:
            logger.error(f"Force vision weather read failed: {e}")
            return f"I'm having difficulty reading the weather information, {self.user_name if self.user_name else 'Sir'}."
    
    async def _handle_weather_command(self, text: str) -> str:
        """Handle weather-related commands using weather bridge (API primary, vision fallback)"""
        logger.info(f"[WEATHER HANDLER] Starting weather command processing: {text}")
        
        try:
            # PRIORITY 1: Use weather bridge which handles API -> Vision fallback automatically
            if hasattr(self, 'weather_bridge') and self.weather_bridge:
                logger.info("[WEATHER HANDLER] Using weather bridge for API-first approach")
                
                # Process the weather query through the bridge
                # The bridge will automatically:
                # 1. Try OpenWeatherMap API first (if configured)
                # 2. Fall back to vision extraction if API fails
                # 3. Try other sources (Core Location, Swift tool, etc.)
                response = await asyncio.wait_for(
                    self.weather_bridge.process_weather_query(text),
                    timeout=10.0  # 10 second timeout (API should be fast)
                )
                
                logger.info(f"[WEATHER HANDLER] Weather bridge returned: {response[:100] if response else 'None'}...")
                
                # Add user name if we have a good response
                if response and self.user_name and self.user_name != "User":
                    if not response.endswith(self.user_name) and not self.user_name in response:
                        response = f"{response}, {self.user_name}"
                
                return response
            
            # PRIORITY 2: Direct fallback - just open the Weather app
            logger.warning("[WEATHER HANDLER] Weather bridge not available, opening Weather app")
            try:
                import subprocess
                subprocess.run(['open', '-a', 'Weather'], check=False)
                return f"I've opened the Weather app for you to check the forecast, {self.user_name if self.user_name else 'Sir'}."
            except:
                return f"I'm unable to check the weather at the moment, {self.user_name if self.user_name else 'Sir'}."
                
        except asyncio.TimeoutError:
            logger.error("[WEATHER HANDLER] Weather request timed out")
            # Try to at least open the Weather app
            try:
                import subprocess
                subprocess.run(['open', '-a', 'Weather'], check=False)
                return f"The weather service is taking longer than expected. I've opened the Weather app for you, {self.user_name if self.user_name else 'Sir'}."
            except:
                return f"I'm having trouble accessing weather information right now, {self.user_name if self.user_name else 'Sir'}."
        
        except Exception as e:
            logger.error(f"[WEATHER HANDLER] Error processing weather command: {e}", exc_info=True)
            # Emergency fallback - try to open Weather app
            try:
                import subprocess
                subprocess.run(['open', '-a', 'Weather'], check=False)
                return f"I encountered an error checking the weather. I've opened the Weather app for you, {self.user_name if self.user_name else 'Sir'}."
            except:
                return f"I'm unable to access weather information at the moment, {self.user_name if self.user_name else 'Sir'}."
