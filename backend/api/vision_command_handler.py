"""
Vision Command Handler for JARVIS - Pure Intelligence Version
NO TEMPLATES. NO HARDCODING. PURE CLAUDE VISION INTELLIGENCE.

Every response is generated fresh by Claude based on what it sees.
"""

import asyncio
import logging
import os
from typing import Dict, Any, Optional, Callable, List
from datetime import datetime

from .pure_vision_intelligence import (
    PureVisionIntelligence, 
    ProactiveIntelligence,
    WorkflowIntelligence,
    ConversationContext,
)
from .proactive_monitoring_handler import get_monitoring_handler
from .activity_reporting_commands import is_activity_reporting_command

logger = logging.getLogger(__name__)

# Import new monitoring system components
try:
    from vision.monitoring_command_classifier import (
        classify_monitoring_command,
        CommandType,
        MonitoringAction,
    )
    from vision.monitoring_state_manager import (
        get_state_manager,
        MonitoringState,
        MonitoringCapability,
    )
    from vision.macos_indicator_controller import get_indicator_controller

    monitoring_system_available = True
except ImportError as e:
    logger.warning(f"Monitoring system components not available: {e}")
    monitoring_system_available = False

# Import enhanced multi-space system
try:
    from vision.enhanced_multi_space_integration import EnhancedMultiSpaceSystem

    enhanced_system_available = True
except ImportError as e:
    logger.warning(f"Enhanced multi-space system not available: {e}")
    enhanced_system_available = False

# Import workspace name processor
try:
    from vision.workspace_name_processor import (
        process_jarvis_response,
        update_workspace_names,
    )

    workspace_processor_available = True
except ImportError as e:
    logger.warning(f"Workspace name processor not available: {e}")
    workspace_processor_available = False
    process_jarvis_response = lambda x, y=None: x  # Fallback to identity function
    update_workspace_names = lambda x: None

# Import workspace name detector for better name detection
try:
    from vision.workspace_name_detector import (
        process_response_with_workspace_names,
        get_current_workspace_names,
    )

    workspace_detector_available = True
except ImportError as e:
    logger.warning(f"Workspace name detector not available: {e}")
    workspace_detector_available = False
    process_response_with_workspace_names = lambda x, y=None: x
    get_current_workspace_names = lambda: {}

# Import Yabai-based multi-space intelligence system
try:
    from vision.yabai_space_detector import YabaiSpaceDetector, YabaiStatus
    from vision.workspace_analyzer import WorkspaceAnalyzer
    from vision.space_response_generator import SpaceResponseGenerator

    yabai_system_available = True
    logger.info("[VISION] ✅ Yabai multi-space intelligence system loaded")
except ImportError as e:
    logger.warning(f"Yabai multi-space system not available: {e}")
    yabai_system_available = False

# Import Intelligent Query Classification System
try:
    from vision.intelligent_query_classifier import (
        QueryIntent,
        ClassificationResult,
        get_query_classifier,
    )
    from vision.smart_query_router import get_smart_router
    from vision.query_context_manager import get_context_manager
    from vision.adaptive_learning_system import get_learning_system
    from vision.performance_monitor import get_performance_monitor

    intelligent_system_available = True
    logger.info("[VISION] ✅ Intelligent query classification system loaded")
except ImportError as e:
    logger.warning(f"Intelligent classification system not available: {e}")
    intelligent_system_available = False

# Import Proactive Suggestions System
try:
    from vision.proactive_suggestions import get_proactive_system, ProactiveSuggestion

    proactive_suggestions_available = True
    logger.info("[VISION] ✅ Proactive suggestions system loaded")
except ImportError as e:
    logger.warning(f"Proactive suggestions system not available: {e}")
    proactive_suggestions_available = False

# Import IntelligentCommandHandler for God Mode surveillance
try:
    from voice.intelligent_command_handler import IntelligentCommandHandler
    INTELLIGENT_HANDLER_AVAILABLE = True
    logger.info("[VISION] ✅ IntelligentCommandHandler loaded (God Mode surveillance enabled)")
except ImportError as e:
    logger.warning(f"IntelligentCommandHandler not available - God Mode surveillance disabled: {e}")
    INTELLIGENT_HANDLER_AVAILABLE = False


class WebSocketLogger:
    """Logger that sends logs to WebSocket for browser console"""

    def __init__(self):
        self.websocket_callback: Optional[Callable] = None
        
    def set_websocket_callback(self, callback: Callable):
        """Set callback to send logs through WebSocket"""
        self.websocket_callback = callback
        
    async def log(self, message: str, level: str = "info"):
        """Send log message through WebSocket"""
        if self.websocket_callback:
            try:
                await self.websocket_callback(
                    {
                    "type": "debug_log",
                    "message": f"[VISION] {message}",
                    "level": level,
                        "timestamp": datetime.now().isoformat(),
                    }
                )
            except Exception as e:
                logger.error(f"Failed to send WebSocket log: {e}")
        
        # Also log to server console
        if level == "error":
            logger.error(message)
        else:
            logger.info(message)


# Global WebSocket logger instance
ws_logger = WebSocketLogger()


class VisionCommandHandler:
    """
    Handles vision commands using pure Claude intelligence.
    Zero templates, zero hardcoded responses.
    """
    
    def __init__(self):
        self.vision_manager = None
        self.intelligence = None
        self.proactive = None
        self.workflow = None
        self.monitoring_active = False
        self.jarvis_api = None  # For voice integration
        
        # Initialize enhanced multi-space system if available
        self.enhanced_system = None
        if enhanced_system_available:
            try:
                self.enhanced_system = EnhancedMultiSpaceSystem()
                logger.info("[VISION] Enhanced multi-space system initialized")
            except Exception as e:
                logger.warning(f"[VISION] Could not initialize enhanced system: {e}")

        # Initialize Yabai-based multi-space intelligence system
        self.yabai_detector = None
        self.workspace_analyzer = None
        self.space_response_generator = None
        if yabai_system_available:
            try:
                self.yabai_detector = YabaiSpaceDetector()
                self.workspace_analyzer = WorkspaceAnalyzer()
                self.space_response_generator = SpaceResponseGenerator(
                    use_sir_prefix=True
                )
                logger.info("[VISION] ✅ Yabai multi-space intelligence initialized")
            except Exception as e:
                logger.warning(f"[VISION] Could not initialize Yabai system: {e}")

        # Initialize Intelligent Query Classification System
        self.classifier = None
        self.smart_router = None
        self.context_manager = None
        self.learning_system = None
        self.performance_monitor = None

        if intelligent_system_available:
            try:
                # Get singleton instances
                self.context_manager = get_context_manager()
                self.learning_system = get_learning_system()
                self.performance_monitor = get_performance_monitor(
                    report_interval_minutes=60
                )

                # Initialize classifier and router without Claude client (can be added later)
                self.classifier = get_query_classifier(claude_client=None)
                self.smart_router = get_smart_router(
                    yabai_handler=self._handle_yabai_query,
                    vision_handler=self._handle_vision_query,
                    multi_space_handler=self._handle_multi_space_query,
                    claude_client=None,
                )

                logger.info(
                    "[VISION] ✅ Intelligent query classification system initialized"
                )
            except Exception as e:
                logger.warning(f"[VISION] Could not initialize intelligent system: {e}")

        # Initialize Proactive Suggestions System
        self.proactive_system = None
        if proactive_suggestions_available:
            try:
                self.proactive_system = get_proactive_system()
                logger.info("[VISION] ✅ Proactive suggestions system initialized")
            except Exception as e:
                logger.warning(f"[VISION] Could not initialize proactive system: {e}")

        # Initialize IntelligentCommandHandler for God Mode surveillance (lazy loading)
        self._intelligent_handler = None
        self._intelligent_handler_initialized = False

    async def _get_intelligent_handler(self) -> Optional[Any]:
        """
        Lazy initialization of IntelligentCommandHandler for God Mode surveillance.

        ROOT CAUSE FIX v5.0.0:
        - Async wrapper with timeout protection (5s max)
        - Non-blocking initialization via executor
        - Prevents voice thread hang during handler init

        Returns None if not available or initialization times out.
        """
        if not INTELLIGENT_HANDLER_AVAILABLE:
            return None

        if self._intelligent_handler_initialized:
            return self._intelligent_handler

        # ===========================================================
        # TIMEOUT-PROTECTED INITIALIZATION - 5 SECOND MAX
        # ===========================================================
        # IntelligentCommandHandler init is synchronous and can hang:
        # - Swift router initialization
        # - ClaudeCommandInterpreter API calls
        # - ClaudeVisionChatbot creation
        # ===========================================================
        handler_init_timeout = float(os.getenv("JARVIS_HANDLER_INIT_TIMEOUT", "5"))

        try:
            logger.info(f"[VISION] Initializing IntelligentCommandHandler (timeout: {handler_init_timeout}s)...")

            # Get user name from environment or use default
            user_name = os.getenv("USER_NAME", "Derek")

            # Wrap synchronous initialization in executor with timeout
            loop = asyncio.get_event_loop()

            def _create_handler():
                return IntelligentCommandHandler(user_name=user_name)

            self._intelligent_handler = await asyncio.wait_for(
                loop.run_in_executor(None, _create_handler),
                timeout=handler_init_timeout
            )

            self._intelligent_handler_initialized = True
            logger.info("[VISION] ✅ IntelligentCommandHandler ready - God Mode activated")
            return self._intelligent_handler

        except asyncio.TimeoutError:
            logger.error(
                f"[VISION] ⚠️ IntelligentCommandHandler init timed out after {handler_init_timeout}s. "
                f"God Mode surveillance unavailable."
            )
            self._intelligent_handler_initialized = True  # Don't retry
            return None
        except Exception as e:
            logger.error(f"[VISION] Failed to initialize IntelligentCommandHandler: {e}")
            self._intelligent_handler_initialized = True  # Don't retry
            return None

    async def initialize_intelligence(self, api_key: str = None):
        """Initialize pure vision intelligence system"""
        if not self.intelligence:
            # If no API key provided, try to get from SecretManager or environment
            if not api_key:
                try:
                    from core.secret_manager import get_anthropic_key
                    api_key = get_anthropic_key()
                    if api_key:
                        logger.info("[PURE VISION] ✅ Using API key from SecretManager")
                except Exception as e:
                    logger.warning(f"[PURE VISION] Could not get key from SecretManager: {e}")

            if not api_key:
                api_key = os.getenv("ANTHROPIC_API_KEY")
                if api_key:
                    logger.info("[PURE VISION] Using API key from environment")

            # If still no API key, we cannot initialize vision
            if not api_key:
                error_msg = (
                    "❌ No Anthropic API key found. Vision features require an API key.\n"
                    "Please configure it using:\n"
                    "  1. GCP Secret Manager: gcloud secrets create anthropic-api-key --data-file=-\n"
                    "  2. Environment: export ANTHROPIC_API_KEY='your-key-here'\n"
                    "  3. Keychain: security add-generic-password -s jarvis_anthropic-api-key -w 'your-key'"
                )
                logger.error(f"[PURE VISION] {error_msg}")
                raise ValueError(error_msg)

            # Try to get existing vision analyzer from app state
            vision_analyzer = None
            try:
                from api.jarvis_factory import get_app_state

                app_state = get_app_state()
                if app_state and hasattr(app_state, "vision_analyzer"):
                    vision_analyzer = app_state.vision_analyzer
                    logger.info(
                        "[PURE VISION] Using existing vision analyzer from app state"
                    )
            except Exception as e:
                logger.debug(f"Could not get vision analyzer from app state: {e}")
                
            # If no app state analyzer and we have an API key, create one
            if not vision_analyzer and api_key:
                try:
                    from vision.claude_vision_analyzer_main import ClaudeVisionAnalyzer

                    vision_analyzer = ClaudeVisionAnalyzer(api_key)
                    logger.info(
                        "[PURE VISION] Created new vision analyzer with API key"
                    )
                except Exception as e:
                    logger.error(f"Failed to create vision analyzer: {e}")
                
            # Store the vision analyzer reference for later use
            self.vision_analyzer = vision_analyzer
            
            # If no existing analyzer, create a wrapper for the API
            if vision_analyzer:
                # Create a Claude client wrapper that uses the existing vision analyzer
                class ClaudeVisionWrapper:
                    def __init__(self, analyzer):
                        self.analyzer = analyzer
                        
                    async def analyze_image_with_prompt(
                        self, image: Any, prompt: str, max_tokens: int = 500
                    ) -> Dict[str, Any]:
                        """Wrapper to use existing vision analyzer"""
                        try:
                            # Use the existing analyzer's analyze method
                            result = await self.analyzer.analyze_image_with_prompt(
                                image=image, prompt=prompt
                            )
                            
                            # Extract the response text
                            if isinstance(result, dict):
                                # First check for 'content' key (from analyze_image_with_prompt)
                                if "content" in result:
                                    return {"content": result["content"]}
                                # Then check for description or response
                                return {
                                    "content": result.get(
                                        "description",
                                        result.get("response", str(result)),
                                    )
                                }
                            else:
                                return {"content": str(result)}
                        except Exception as e:
                            logger.error(f"Vision analyzer error: {e}")
                            raise
                    
                    async def analyze_multiple_images_with_prompt(
                        self,
                        images: List[Dict[str, Any]],
                        prompt: str,
                        max_tokens: int = 1000,
                    ) -> Dict[str, Any]:
                        """Wrapper for multi-space analysis"""
                        try:
                            # Use the analyzer's multi-image method
                            if hasattr(
                                self.analyzer, "analyze_multiple_images_with_prompt"
                            ):
                                result = await self.analyzer.analyze_multiple_images_with_prompt(
                                    images=images, prompt=prompt, max_tokens=max_tokens
                                )
                                return result
                            else:
                                # Fallback: analyze first image only
                                logger.warning(
                                    "Analyzer doesn't support multi-image analysis, using first image only"
                                )
                                if images:
                                    first_image = images[0]["image"]
                                    return await self.analyze_image_with_prompt(
                                        first_image, prompt, max_tokens
                                    )
                                else:
                                    return {
                                        "content": "No images provided for analysis",
                                        "success": False,
                                    }
                        except Exception as e:
                            logger.error(f"Multi-image vision analyzer error: {e}")
                            raise
                            
                claude_client = ClaudeVisionWrapper(vision_analyzer)
            else:
                # No vision analyzer available - use mock
                logger.warning(
                    "[PURE VISION] No vision analyzer available, using mock responses"
                )
                claude_client = None
            
            # Initialize intelligence systems with multi-space enabled
            self.intelligence = PureVisionIntelligence(
                claude_client, enable_multi_space=True
            )
            
            # Give intelligence access to this handler for context storage
            self.intelligence.jarvis_api = self
            self.proactive = ProactiveIntelligence(self.intelligence)
            self.workflow = WorkflowIntelligence(self.intelligence)
            
            # Update enhanced system with intelligence if available
            if self.enhanced_system:
                self.enhanced_system.vision_intelligence = self.intelligence
                logger.info(
                    "[ENHANCED] Updated enhanced system with vision intelligence"
                )

            # Update intelligent classification system with Claude client
            if intelligent_system_available and claude_client:
                try:
                    # Update existing classifier with Claude client
                    if self.classifier:
                        self.classifier.claude = claude_client
                        logger.info(
                            "[INTELLIGENT] Updated classifier with Claude client"
                        )

                    # Update existing smart router with Claude client
                    if self.smart_router:
                        self.smart_router.classifier.claude = (
                            claude_client
                            if hasattr(self.smart_router, "classifier")
                            and self.smart_router.classifier
                            else None
                        )
                        logger.info(
                            "[INTELLIGENT] Updated smart router with Claude client"
                        )
                except Exception as e:
                    logger.warning(
                        f"[INTELLIGENT] Could not update classifier/router with Claude client: {e}"
                    )
            
            logger.info("[PURE VISION] Intelligence systems initialized")
            
    async def handle_command(self, command_text: str, timeout: float = 45.0) -> Dict[str, Any]:
        """
        Handle any vision command with pure intelligence.
        No pattern matching, no templates - Claude understands intent.

        Args:
            command_text: The user's command
            timeout: Overall timeout for the command (default 45s)

        Returns:
            Response dict with handled status and response
        """
        logger.info(f"[VISION] ========== STARTING handle_command for: {command_text} ==========")
        await ws_logger.log(f"Processing vision command: {command_text}")

        # Wrap the entire command handling with a timeout
        try:
            return await asyncio.wait_for(
                self._handle_command_internal(command_text),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.error(f"[VISION] ❌ Command timed out after {timeout}s: {command_text}")
            return {
                "handled": True,
                "response": (
                    f"I apologize, Sir, but the request took longer than expected ({timeout}s). "
                    "This could be due to slow API responses or system resources. "
                    "Would you like me to try again?"
                ),
                "error": True,
                "timeout": True,
                "pure_intelligence": True,
                "monitoring_active": self.monitoring_active,
            }
        except Exception as e:
            logger.error(f"[VISION] ❌ Command failed with exception: {e}", exc_info=True)
            return {
                "handled": True,
                "response": f"I encountered an unexpected error, Sir: {str(e)}. Please try again.",
                "error": True,
                "exception": str(e),
                "pure_intelligence": True,
                "monitoring_active": self.monitoring_active,
            }

    async def _handle_command_internal(self, command_text: str) -> Dict[str, Any]:
        """
        Internal command handling logic (called with timeout wrapper).
        """

        # ===========================================================
        # FAST-FAIL DETECTION - Return immediately for common issues
        # ===========================================================
        # 1. Check if intelligence is initialized
        if not self.intelligence:
            logger.warning("[VISION] ⚠️ Fast-fail: Intelligence not initialized")
            return {
                "handled": True,
                "response": "I'm still initializing my vision systems. Please try again in a moment.",
                "error": True,
                "fast_fail": "intelligence_not_ready",
            }

        # 2. Check for empty/invalid command
        if not command_text or not command_text.strip():
            logger.warning("[VISION] ⚠️ Fast-fail: Empty command")
            return {
                "handled": True,
                "response": "I didn't catch that, Sir. Could you please repeat your request?",
                "error": True,
                "fast_fail": "empty_command",
            }

        # 3. Check command length (prevent processing extremely long inputs)
        if len(command_text) > 5000:
            logger.warning(f"[VISION] ⚠️ Fast-fail: Command too long ({len(command_text)} chars)")
            return {
                "handled": True,
                "response": "That request is quite lengthy. Could you please summarize what you'd like me to do?",
                "error": True,
                "fast_fail": "command_too_long",
            }

        # 4. Fast pre-check for non-vision commands (handled elsewhere)
        command_lower = command_text.lower()
        non_vision_keywords = [
            "weather", "temperature", "forecast",
            "time", "date", "alarm", "timer", "reminder",
            "play", "pause", "stop", "music", "volume",
            "email", "message", "call", "text",
        ]
        if any(keyword in command_lower for keyword in non_vision_keywords):
            # These might be handled by other systems - let them pass through
            logger.info(f"[VISION] Non-vision keyword detected, will check if handled")

        # IMPORTANT: Check if this is a lock/unlock screen command - should NOT be handled by vision
        command_lower = command_text.lower()
        if ("lock" in command_lower and "screen" in command_lower) or (
            "unlock" in command_lower and "screen" in command_lower
        ):
            logger.info(
                f"[VISION] Lock/unlock screen command detected, not handling as vision"
            )
            return {
                "handled": False,
                "reason": "Lock/unlock screen commands are system commands, not vision",
            }

        # =========================================================================
        # 🪃 v63.0: BOOMERANG PROTOCOL - Voice-Activated Window Return
        # =========================================================================
        # Handle "bring back windows" commands before other processing.
        # This triggers the Boomerang Protocol to return exiled windows.
        #
        # Supported commands:
        # - "bring back my windows"
        # - "bring back Chrome windows"
        # - "return all windows"
        # - "summon the windows"
        # =========================================================================
        boomerang_keywords = [
            "bring back", "bring my windows", "bring the windows",
            "return windows", "return my windows", "return all windows",
            "summon windows", "summon my windows", "get back windows",
            "restore windows", "restore my windows"
        ]

        if any(keyword in command_lower for keyword in boomerang_keywords):
            logger.info(f"[VISION] 🪃 BOOMERANG VOICE COMMAND detected: {command_text}")

            try:
                from backend.vision.yabai_space_detector import get_yabai_detector
                yabai = get_yabai_detector()

                # Extract app filter from command if present
                app_filter = None
                common_apps = ["chrome", "safari", "firefox", "terminal", "code", "slack", "discord", "figma"]
                for app in common_apps:
                    if app in command_lower:
                        # Capitalize properly
                        app_filter = app.title() if app != "code" else "Visual Studio Code"
                        break

                # Execute Boomerang voice command
                result = await yabai.boomerang_voice_command_async(
                    command=command_text,
                    app_filter=app_filter
                )

                response_message = result.get("response_message", "No windows to return.")
                returned_count = len(result.get("returned_windows", []))

                logger.info(f"[VISION] 🪃 BOOMERANG COMPLETE: {returned_count} windows returned")

                return {
                    "handled": True,
                    "response": response_message,
                    "boomerang": True,
                    "returned_count": returned_count,
                    "command_type": "boomerang_return",
                }

            except Exception as e:
                logger.error(f"[VISION] 🪃 Boomerang command failed: {e}", exc_info=True)
                return {
                    "handled": True,
                    "response": f"I tried to bring back your windows, but encountered an issue: {str(e)}",
                    "boomerang": True,
                    "error": True,
                }

        # =========================================================================
        # 🏎️ GOD MODE SURVEILLANCE - Voice-Activated Window Monitoring
        # =========================================================================
        # Check for "watch" commands FIRST - route to IntelligentCommandHandler
        # This enables: "watch all Chrome for Error" → spawns Ferrari Engines
        #
        # ROOT CAUSE FIX v5.0.0:
        # - Timeout protection on both handler init and execution
        # - Non-blocking returns with informative error messages
        # - Prevents indefinite hangs on surveillance commands
        # =========================================================================
        try:
            handler = await self._get_intelligent_handler()
            if handler:
                # Check if this is a watch/monitor command
                watch_params = handler._parse_watch_command(command_text)
                if watch_params:
                    logger.info(f"[VISION] 🏎️  GOD MODE: Watch command detected - routing to surveillance system")
                    logger.info(f"[VISION] Params: app={watch_params['app_name']}, trigger='{watch_params['trigger_text']}', all_spaces={watch_params['all_spaces']}")

                    # ===========================================================
                    # TIMEOUT-PROTECTED SURVEILLANCE EXECUTION - 20 SECOND MAX
                    # ===========================================================
                    # _execute_surveillance_command includes:
                    # - VisualMonitorAgent initialization (has its own 10s timeout)
                    # - Window discovery (has its own 5s timeout)
                    # - Watcher spawning (has its own 15s timeout)
                    # Total cascade: ~30s max, but we add outer protection at 20s
                    # for immediate acknowledgment (watchers continue in background)
                    # ===========================================================
                    surveillance_timeout = float(os.getenv("JARVIS_SURVEILLANCE_TIMEOUT", "20"))

                    try:
                        response_text = await asyncio.wait_for(
                            handler._execute_surveillance_command(watch_params),
                            timeout=surveillance_timeout
                        )
                    except asyncio.TimeoutError:
                        logger.error(
                            f"[VISION] ⚠️ Surveillance command timed out after {surveillance_timeout}s "
                            f"for app='{watch_params['app_name']}'"
                        )
                        return {
                            "handled": True,
                            "response": (
                                f"I'm setting up monitoring for {watch_params['app_name']}, but it's taking "
                                f"longer than expected. The surveillance may still start in the background. "
                                f"Please try again if you don't see monitoring activate."
                            ),
                            "god_mode": True,
                            "surveillance_params": watch_params,
                            "command_type": "god_mode_surveillance",
                            "timeout": True,
                        }

                    logger.info(f"[VISION] ✅ God Mode surveillance complete")
                    return {
                        "handled": True,
                        "response": response_text,
                        "god_mode": True,
                        "surveillance_params": watch_params,
                        "command_type": "god_mode_surveillance",
                    }
        except Exception as e:
            logger.error(f"[VISION] God Mode surveillance error: {e}", exc_info=True)
            # Don't fail - continue to other handlers

        # =========================================================================
        # SYSTEM COMMAND BYPASS - Route to UnifiedCommandProcessor, NOT vision
        # =========================================================================
        # These commands should be handled by system control, not vision analysis.
        # Capturing screenshots for these commands causes hangs (especially when locked).
        # =========================================================================
        import re

        system_command_patterns = [
            # Search commands - open browser and search
            r"\b(search|google|look\s*up|browse)\s+(for\s+)?",
            # App launch commands
            r"\b(open|launch|start|run|quit|close|exit)\s+\w+",
            # Navigation commands
            r"\bgo\s+to\s+",
            r"\bnavigate\s+to\s+",
            # File/folder operations
            r"\b(create|delete|move|copy|rename)\s+(file|folder|directory)",
            # System control
            r"\b(restart|shutdown|sleep|hibernate)\b",
            r"\b(volume|brightness)\s+(up|down|mute|unmute)",
            # Clipboard operations
            r"\b(copy|paste|cut)\b",
            # Typing/clicking
            r"\b(type|click|scroll|drag|press)\b",
        ]

        for pattern in system_command_patterns:
            if re.search(pattern, command_lower, re.IGNORECASE):
                logger.info(
                    f"[VISION] 🚀 System command detected (pattern: {pattern[:30]}...) - routing to UnifiedCommandProcessor"
                )
                return {
                    "handled": False,
                    "reason": f"System command - should be handled by UnifiedCommandProcessor",
                    "detected_pattern": pattern[:30],
                }
        # =========================================================================
        # END SYSTEM COMMAND BYPASS
        # =========================================================================

        # SIMPLE TV MONITOR: Check for display prompt responses (YES/NO) - HIGHEST PRIORITY
        logger.info(f"[VISION] Checking if TV monitor response handler should process: '{command_text}'")
        tv_response_result = await self._handle_tv_monitor_response(command_text)
        logger.info(f"[VISION] TV monitor handler result: handled={tv_response_result.get('handled')}, action={tv_response_result.get('action')}")
        if tv_response_result.get("handled"):
            logger.info(f"[VISION] TV monitor handled the command, returning response")
            return tv_response_result
        
        # PHASE 1.2C: Check for voice prompt responses (YES/NO)
        voice_response_result = await self._handle_voice_prompt_response(command_text)
        if voice_response_result.get("handled"):
            return voice_response_result
        
        # PHASE 1.2C: Check for proximity-aware routing
        proximity_routing_result = await self._handle_proximity_aware_routing(command_text)
        if proximity_routing_result.get("handled"):
            return proximity_routing_result
        
        # Check for multi-monitor queries
        if self._is_multi_monitor_query(command_text):
            monitor_result = await self._handle_multi_monitor_query(command_text)
            if monitor_result.get("handled"):
                return monitor_result
        
        # Check if this is a follow-up to a multi-space query
        follow_up_result = await self._handle_multi_space_follow_up(command_text)
        if follow_up_result.get("handled"):
            return follow_up_result

        # Check if this is a multi-space query that should use intelligent orchestration
        if self._is_multi_space_query(command_text):
            orchestration_result = await self._handle_intelligent_orchestration(command_text)
            if orchestration_result.get("handled"):
                return orchestration_result

        # Check if this is a proactive monitoring command
        monitoring_handler = get_monitoring_handler(self)
        monitoring_result = await monitoring_handler.handle_monitoring_request(
            command_text
        )
        if monitoring_result.get("handled"):
            return monitoring_result
        
        # INTELLIGENT CLASSIFICATION SYSTEM (RUNS FIRST - HIGHEST PRIORITY)
        # Use smart router to classify and route query to optimal pipeline
        # ==============================================================================

        if intelligent_system_available and self.smart_router and self.context_manager:
            try:
                logger.info(f"[INTELLIGENT] Using smart router for query classification: '{command_text}'")

                # Get context for classification
                context = self.context_manager.get_context_for_query(command_text)

                # Add current Yabai state to context if available
                if self.yabai_detector:
                    try:
                        active_space = self.yabai_detector.get_current_space()
                        all_spaces = self.yabai_detector.enumerate_all_spaces()
                        context["active_space"] = (
                            active_space.get("index") if active_space else None
                        )
                        context["total_spaces"] = len(all_spaces) if all_spaces else 0
                    except Exception:
                        pass  # Continue without Yabai context

                # Route the query through intelligent system
                routing_result = await self.smart_router.route_query(
                    query=command_text, context=context
                )

                logger.info(f"[INTELLIGENT] Query classified as: {routing_result.intent.value} (confidence: {getattr(routing_result, 'confidence', 'N/A')})")

                # Record query in context manager
                self.context_manager.record_query(
                    query=command_text,
                    intent=routing_result.intent.value,
                    active_space=context.get("active_space"),
                    total_spaces=context.get("total_spaces", 0),
                    response_latency_ms=routing_result.latency_ms,
                )

                # Collect performance metrics periodically
                if (
                    self.performance_monitor
                    and self.performance_monitor.should_generate_report()
                ):
                    await self.performance_monitor.collect_metrics()
                    self.performance_monitor.mark_report_generated()
                    logger.info("[INTELLIGENT] Performance metrics collected")

                # Return routed response
                return {
                    "handled": True,
                    "response": routing_result.response,
                    "intelligent_routing": True,
                    "intent": routing_result.intent.value,
                    "latency_ms": routing_result.latency_ms,
                    "metadata": routing_result.metadata,
                    "monitoring_active": self.monitoring_active,
                }

            except Exception as e:
                logger.error(
                    f"[INTELLIGENT] Smart routing failed, falling back to enhanced/legacy: {e}",
                    exc_info=True,
                )
                # Fall through to enhanced system
        # ==============================================================================
        # END INTELLIGENT CLASSIFICATION SYSTEM
        # ==============================================================================
        
        # Ensure intelligence is initialized
        if not self.intelligence:
            await self.initialize_intelligence()
            
        # Check if this is a multi-space query first
        needs_multi_space = False
        if self.intelligence and hasattr(self.intelligence, "_should_use_multi_space"):
            needs_multi_space = self.intelligence._should_use_multi_space(command_text)
            logger.info(f"[VISION] Multi-space query detected: {needs_multi_space}")
        
        # Capture screen(s) based on query type
        if needs_multi_space:
            # Capture multiple spaces for comprehensive analysis
            screenshot = await self._capture_screen(multi_space=True)
            logger.info(
                f"[VISION] Captured {len(screenshot) if isinstance(screenshot, dict) else 1} space(s)"
            )
        else:
            # Single space capture
            screenshot = await self._capture_screen()
            
        if not screenshot:
            # Even error messages come from Claude - but be more specific about timeout
            logger.error(f"[VISION-CAPTURE] ❌ Screenshot capture returned None for command: '{command_text}'")
            return await self._get_error_response(
                "screenshot_failed",
                command_text,
                details="Screen capture timed out or failed. This may be due to screen recording permissions or system resources."
            )
            
        # FAST PATH: Check for monitoring commands using keywords ONLY (no Claude call)
        # This prevents unnecessary API calls for 99% of vision queries
        monitoring_keywords = [
            "start monitoring", "enable monitoring", "monitor my screen",
            "enable screen monitoring", "monitoring capabilities", "turn on monitoring",
            "activate monitoring", "begin monitoring", "stop monitoring", "disable monitoring",
            "turn off monitoring", "deactivate monitoring", "stop watching"
        ]

        command_lower = command_text.lower()
        is_monitoring_command = (
            is_activity_reporting_command(command_text) or
            any(keyword in command_lower for keyword in monitoring_keywords)
        )

        if is_monitoring_command:
            logger.info(f"[VISION] ✅ Fast match: Monitoring command detected")
        else:
            logger.info(f"[VISION] ✅ Fast match: Regular vision query (skipping monitoring check)")

        if is_monitoring_command:
            return await self._handle_monitoring_command(command_text, screenshot)
        else:
            # Pure vision query - let Claude see and respond
            response = await self.intelligence.understand_and_respond(
                screenshot, command_text
            )

            return {
                "handled": True,
                "response": response,
                "pure_intelligence": True,
                "monitoring_active": self.monitoring_active,
                "context": self.intelligence.context.get_temporal_context(),
            }

        # Fallback: If we reach here, something went wrong
        logger.warning(f"[VISION] No handler processed the command: {command_text}")
        return {
            "handled": True,
            "response": "Let me analyze your desktop spaces for you, Sir.",
            "fallback": True,
        }

    async def analyze_screen(self, command_text: str) -> Dict[str, Any]:
        """Analyze screen with enhanced multi-space intelligence"""
        logger.info(f"[VISION] analyze_screen called with: {command_text}")

        # Ensure intelligence is initialized
        if not self.intelligence:
            await self.initialize_intelligence()

        # Use the same logic as handle_command but for screen analysis
        try:
            # Check if this is a multi-space query
            needs_multi_space = False
            if self.intelligence and hasattr(
                self.intelligence, "_should_use_multi_space"
            ):
                needs_multi_space = self.intelligence._should_use_multi_space(
                    command_text
                )
                logger.info(f"[VISION] Multi-space query detected: {needs_multi_space}")

            # Capture screen(s) based on query type
            if needs_multi_space:
                # Capture multiple spaces for comprehensive analysis
                screenshot = await self._capture_screen(multi_space=True)
                logger.info(
                    f"[VISION] Captured {len(screenshot) if isinstance(screenshot, dict) else 1} space(s)"
                )
            else:
                # Single space capture
                screenshot = await self._capture_screen()

            if not screenshot:
                # Even error messages come from Claude
                return await self._get_error_response("screenshot_failed", command_text)

            # Try Yabai multi-space intelligence first
            if (
                needs_multi_space
                and self.yabai_detector
                and self.workspace_analyzer
                and self.space_response_generator
            ):
                try:
                    logger.info("[YABAI] Using Yabai-based multi-space intelligence")

                    # Check Yabai availability
                    status = self.yabai_detector.get_status()

                    if status == YabaiStatus.AVAILABLE:
                        # Get workspace data from Yabai
                        workspace_data = self.yabai_detector.get_workspace_summary()
                        spaces_dict = workspace_data["spaces"]

                        # Convert dict spaces to objects with required attributes
                        from types import SimpleNamespace
                        spaces = []
                        for space_dict in spaces_dict:
                            # Use space_id field that actually exists in the dict
                            space_id = space_dict.get("space_id", space_dict.get("id", 0))
                            space = SimpleNamespace(
                                index=space_id,
                                display_name=space_dict.get("space_name", f"Space {space_id}"),
                                is_focused=space_dict.get("is_current", False),
                                display=space_dict.get("display", 1)
                            )
                            spaces.append(space)

                        # Collect all windows from all spaces
                        windows = []
                        for space_dict in spaces_dict:
                            # Use space_id field, not index
                            space_id = space_dict.get("space_id")
                            if space_id:
                                # Get windows directly from the space dict
                                space_windows = space_dict.get("windows", [])
                                
                                # Convert window dicts to objects with required attributes
                                for window_dict in space_windows:
                                    window = SimpleNamespace(
                                        app_name=window_dict.get("app", "Unknown"),
                                        title=window_dict.get("title", ""),
                                        window_id=window_dict.get("id", 0),
                                        space_index=space_id,
                                        is_minimized=window_dict.get("minimized", False),
                                        is_hidden=window_dict.get("hidden", False),
                                        is_substantial=True  # Assume windows from Yabai are substantial
                                    )
                                    windows.append(window)

                        # Analyze workspace activity
                        analysis = self.workspace_analyzer.analyze(spaces, windows)

                        # Generate natural language response
                        response = (
                            self.space_response_generator.generate_overview_response(
                                analysis, include_details=True
                            )
                        )

                        # Log performance info
                        logger.info(
                            f"[YABAI] Successfully generated workspace overview with {len(spaces)} spaces"
                        )

                        return {
                            "handled": True,
                            "response": response,
                            "pure_intelligence": True,
                            "yabai_powered": True,
                            "monitoring_active": self.monitoring_active,
                            "context": (
                                self.intelligence.context.get_temporal_context()
                                if self.intelligence
                                else {}
                            ),
                            "analysis_metadata": {
                                "total_spaces": analysis.total_spaces,
                                "active_spaces": analysis.active_spaces,
                                "unique_applications": analysis.unique_applications,
                                "detected_project": analysis.detected_project,
                                "yabai_status": status.value,
                                "total_windows": len(windows),
                            },
                        }
                    else:
                        # Yabai not available - provide installation guidance
                        logger.warning(
                            f"[YABAI] Not available (status: {status.value})"
                        )
                        response = self.space_response_generator.generate_yabai_installation_response(
                            status
                        )

                        return {
                            "handled": True,
                            "response": response,
                            "yabai_status": status.value,
                            "monitoring_active": self.monitoring_active,
                        }

                except Exception as e:
                    logger.error(
                        f"[YABAI] Error using Yabai system: {e}", exc_info=True
                    )
                    # Fall through to Claude-based analysis

            # Use enhanced multi-space intelligence if available (fallback)
            if (
                self.intelligence
                and hasattr(self.intelligence, "multi_space_extension")
                and hasattr(
                    self.intelligence.multi_space_extension,
                    "analyze_comprehensive_workspace",
                )
            ):

                # Get workspace data
                window_data = await self.intelligence._gather_multi_space_data()

                # Use enhanced analysis
                enhanced_response = self.intelligence.multi_space_extension.analyze_comprehensive_workspace(
                    command_text, window_data
                )

                logger.info(
                    f"[ENHANCED VISION] Generated enhanced response: {len(enhanced_response)} chars"
                )
                return {
                    "handled": True,
                    "response": enhanced_response,
                    "pure_intelligence": True,
                    "monitoring_active": self.monitoring_active,
                    "context": self.intelligence.context.get_temporal_context(),
                    "enhanced_analysis": True,
                }
            else:
                # Fallback to basic Claude analysis
                logger.info(f"[VISION] Using basic Claude analysis, screenshot type: {type(screenshot)}")
                if isinstance(screenshot, dict):
                    logger.info(f"[VISION] Screenshot dict keys: {list(screenshot.keys())}, values: {[type(v) for v in screenshot.values()]}")
                
                response = await self.intelligence.understand_and_respond(
                    screenshot, command_text
                )
                
                return {
                    "handled": True,
                    "response": response,
                    "pure_intelligence": True,
                    "monitoring_active": self.monitoring_active,
                    "context": self.intelligence.context.get_temporal_context(),
                }

        except Exception as e:
            logger.error(f"[VISION] Error in analyze_screen: {e}")
            import traceback

            traceback.print_exc()

            # Provide more helpful error messages
            error_type = type(e).__name__
            error_msg = str(e)
            
            if error_type == "ValueError":
                if "screenshot" in error_msg.lower() or "capture" in error_msg.lower() or "invalid" in error_msg.lower():
                    user_message = (
                        "I'm unable to capture screenshots of your desktop spaces at the moment. "
                        "Please ensure screen recording permissions are enabled for JARVIS in "
                        "System Preferences > Security & Privacy > Privacy > Screen Recording."
                    )
                else:
                    user_message = f"I encountered a data error: {error_msg}. Please try again."
            else:
                user_message = f"I encountered an error while analyzing your screen: {error_msg}. Please try again."

            # Return error response
            return {
                "handled": False,
                "response": user_message,
                "error": error_msg,
                "error_type": error_type,
                "monitoring_active": self.monitoring_active,
            }
            
    async def _is_monitoring_command(self, command: str, screenshot: Any) -> bool:
        """Let Claude determine if this is a monitoring command"""
        prompt = f"""Look at the screen and the user's command: "{command}"

Is this command asking to start or stop screen monitoring/watching?
Respond with just "YES" or "NO".

Examples of monitoring commands:
- "start monitoring my screen"
- "stop watching"
- "activate vision monitoring"
- "enable screen monitoring"
- "enable screen monitoring capabilities"
- "turn on monitoring"

Examples of non-monitoring commands:
- "what do you see?"
- "what's my battery?"
- "analyze this screen"
"""
        
        response = await self.intelligence._get_claude_vision_response(
            screenshot, prompt
        )
        return response.get("response", "").strip().upper() == "YES"

    async def _handle_monitoring_command(
        self, command: str, screenshot: Any
    ) -> Dict[str, Any]:
        """Handle monitoring commands with natural responses"""
        
        # Check if this is an activity reporting command
        if is_activity_reporting_command(command):
            monitoring_handler = get_monitoring_handler(self)
            return await monitoring_handler.enable_change_reporting()
        
        # Let Claude understand if this is start or stop
        intent_prompt = f"""The user said: "{command}"

Are they asking to START or STOP monitoring?
Respond with just "START" or "STOP".
"""
        
        response = await self.intelligence._get_claude_vision_response(
            screenshot, intent_prompt
        )
        intent = response.get("response", "").strip().upper()
        
        if intent == "START":
            self.monitoring_active = True
            self.proactive.monitoring_active = True
            
            # Start multi-space monitoring with purple indicator
            monitoring_success = False
            if hasattr(self.intelligence, "start_multi_space_monitoring"):
                monitoring_started = (
                    await self.intelligence.start_multi_space_monitoring()
                )
                if monitoring_started:
                    logger.info("Multi-space monitoring started with purple indicator")
                    monitoring_success = True
                else:
                    logger.warning("Failed to start multi-space monitoring")
                    monitoring_success = False
            
            # Update vision status if monitoring started successfully
            if monitoring_success:
                try:
                    from vision.vision_status_manager import get_vision_status_manager

                    vision_status_manager = get_vision_status_manager()
                    await vision_status_manager.update_vision_status(True)
                    logger.info("✅ Vision status updated to connected (old flow)")
                except Exception as e:
                    logger.error(f"Failed to update vision status: {e}")
            
            # Get natural response for starting monitoring
            if monitoring_success:
                start_prompt = f"""The user asked: "{command}"

You're JARVIS. The screen monitoring is now ACTIVE with the macOS purple indicator visible.

Give a BRIEF confirmation (1-2 sentences max) that includes:
1. Monitoring is now active
2. The purple indicator is visible in the menu bar
3. You can see their screen

Example: "Screen monitoring is now active, Sir. The purple indicator is visible in your menu bar, and I can see your desktop."

BE CONCISE. Do not explain technical details or list options.
"""
            else:
                start_prompt = f"""The user asked: "{command}"

You're JARVIS. Screen monitoring FAILED to start due to permissions.

Give a BRIEF response (1-2 sentences) explaining:
1. Monitoring couldn't start
2. They need to grant screen recording permission

Example: "I couldn't start screen monitoring, Sir. Please grant screen recording permission in System Preferences."

BE CONCISE.
"""
            response = await self.intelligence._get_claude_vision_response(
                screenshot, start_prompt
            )
            
            # Start proactive monitoring
            asyncio.create_task(self._proactive_monitoring_loop())
            
        else:  # STOP
            self.monitoring_active = False
            self.proactive.monitoring_active = False
            
            # Stop multi-space monitoring and remove purple indicator
            if hasattr(self.intelligence, "stop_multi_space_monitoring"):
                await self.intelligence.stop_multi_space_monitoring()
                logger.info("Multi-space monitoring stopped, purple indicator removed")
            
            # Update vision status to disconnected
            try:
                from vision.vision_status_manager import get_vision_status_manager

                vision_status_manager = get_vision_status_manager()
                await vision_status_manager.update_vision_status(False)
                logger.info("✅ Vision status updated to disconnected (old flow)")
            except Exception as e:
                logger.error(f"Failed to update vision status: {e}")
            
            # Get natural response for stopping monitoring
            stop_prompt = f"""The user asked: "{command}"

You're JARVIS. Screen monitoring has been STOPPED and the purple indicator is gone.

Give a BRIEF confirmation (1 sentence) that monitoring has stopped.

Example: "Screen monitoring has been disabled, Sir."

BE CONCISE. No technical details.
"""
            response = await self.intelligence._get_claude_vision_response(
                screenshot, stop_prompt
            )
            
        return {
            "handled": True,
            "response": response.get("response"),
            "monitoring_active": self.monitoring_active,
            "pure_intelligence": True,
        }
    
    async def _handle_monitoring_control(
        self, command: str, context: Dict[str, Any], screenshot: Any
    ) -> Dict[str, Any]:
        """Handle monitoring control commands with new system"""
        state_manager = get_state_manager()
        indicator_controller = get_indicator_controller()
        
        action = context["action"]
        
        if action == MonitoringAction.START:
            # Check if we can start monitoring
            can_start, reason = state_manager.can_start_monitoring()
            if not can_start:
                return {
                    "handled": True,
                    "response": f"I cannot start monitoring right now, Sir. {reason}",
                    "monitoring_active": state_manager.is_monitoring_active(),
                    "pure_intelligence": True,
                }
            
            # Transition to activating state
            await state_manager.transition_to(MonitoringState.ACTIVATING)
            
            # Activate macOS indicator
            indicator_result = await indicator_controller.activate_indicator()
            
            if indicator_result["success"]:
                # Update state manager
                state_manager.update_component_status("macos_indicator", True)
                state_manager.add_capability(MonitoringCapability.MACOS_INDICATOR)
                
                # Start multi-space monitoring
                monitoring_started = False
                if hasattr(self.intelligence, "start_multi_space_monitoring"):
                    monitoring_started = (
                        await self.intelligence.start_multi_space_monitoring()
                    )
                    if monitoring_started:
                        state_manager.update_component_status("multi_space", True)
                        state_manager.add_capability(MonitoringCapability.MULTI_SPACE)
                
                # Update monitoring active flag
                self.monitoring_active = True
                self.proactive.monitoring_active = True
                state_manager.update_component_status("vision_intelligence", True)
                
                # Transition to active state
                await state_manager.transition_to(MonitoringState.ACTIVE)
                
                # Update vision status to connected
                try:
                    from vision.vision_status_manager import get_vision_status_manager

                    vision_status_manager = get_vision_status_manager()
                    await vision_status_manager.update_vision_status(True)
                    logger.info("✅ Vision status updated to connected")
                except Exception as e:
                    logger.error(f"Failed to update vision status: {e}")
                
                # Start proactive monitoring
                asyncio.create_task(self._proactive_monitoring_loop())
                
                return {
                    "handled": True,
                    "response": "Screen monitoring is now active, Sir. The purple indicator is visible in your menu bar, and I can see your entire workspace.",
                    "monitoring_active": True,
                    "pure_intelligence": True,
                    "indicator_active": True,
                }
            else:
                # Indicator activation failed
                await state_manager.transition_to(
                    MonitoringState.ERROR,
                    {
                        "error": "Failed to activate macOS indicator",
                        "details": indicator_result,
                    },
                )
                
                return {
                    "handled": True,
                    "response": "I couldn't activate screen monitoring, Sir. Please ensure screen recording permission is granted in System Preferences.",
                    "monitoring_active": False,
                    "pure_intelligence": True,
                    "error": indicator_result.get("error"),
                }
                
        elif action == MonitoringAction.STOP:
            # Check if we can stop monitoring
            can_stop, reason = state_manager.can_stop_monitoring()
            if not can_stop:
                return {
                    "handled": True,
                    "response": f"I cannot stop monitoring right now, Sir. {reason}",
                    "monitoring_active": state_manager.is_monitoring_active(),
                    "pure_intelligence": True,
                }
            
            # Transition to deactivating state
            await state_manager.transition_to(MonitoringState.DEACTIVATING)
            
            # Stop monitoring components
            self.monitoring_active = False
            self.proactive.monitoring_active = False
            
            # Stop multi-space monitoring
            if hasattr(self.intelligence, "stop_multi_space_monitoring"):
                await self.intelligence.stop_multi_space_monitoring()
            
            # Deactivate macOS indicator
            indicator_result = await indicator_controller.deactivate_indicator()
            
            # Clear capabilities
            state_manager.remove_capability(MonitoringCapability.MACOS_INDICATOR)
            state_manager.remove_capability(MonitoringCapability.MULTI_SPACE)
            
            # Update component status
            state_manager.update_component_status("macos_indicator", False)
            state_manager.update_component_status("multi_space", False)
            state_manager.update_component_status("vision_intelligence", False)
            
            # Transition to inactive state
            await state_manager.transition_to(MonitoringState.INACTIVE)
            
            # Update vision status to disconnected
            try:
                from vision.vision_status_manager import get_vision_status_manager

                vision_status_manager = get_vision_status_manager()
                await vision_status_manager.update_vision_status(False)
                logger.info("✅ Vision status updated to disconnected")
            except Exception as e:
                logger.error(f"Failed to update vision status: {e}")
            
            return {
                "handled": True,
                "response": "Screen monitoring has been disabled, Sir.",
                "monitoring_active": False,
                "pure_intelligence": True,
                "indicator_active": False,
            }
        
        return {
            "handled": True,
            "response": "I'm not sure how to handle that monitoring command, Sir.",
            "monitoring_active": self.monitoring_active,
            "pure_intelligence": True,
        }
    
    async def _handle_monitoring_status(
        self, command: str, context: Dict[str, Any], screenshot: Any
    ) -> Dict[str, Any]:
        """Handle monitoring status queries"""
        state_manager = get_state_manager()
        state_info = state_manager.get_state_info()
        
        # Build status response
        if state_info["is_active"]:
            capabilities = state_info["active_capabilities"]
            cap_text = f"with {', '.join(capabilities)}" if capabilities else ""
            response = f"Yes Sir, monitoring is currently active {cap_text}. The purple indicator should be visible in your menu bar."
        elif state_info["is_transitioning"]:
            response = f"Monitoring is currently {state_info['current_state'].replace('_', ' ')}, Sir."
        else:
            response = "No Sir, monitoring is not active. Would you like me to start monitoring your screen?"
        
        return {
            "handled": True,
            "response": response,
            "monitoring_active": state_info["is_active"],
            "pure_intelligence": True,
            "state_info": state_info,
        }

    async def _handle_yabai_query(
        self, query: str, context: Optional[Dict[str, Any]]
    ) -> str:
        """
        Handle metadata-only query using Yabai (no screenshots)
        Fast path for workspace overview queries
        """
        try:
            if not self.yabai_detector:
                raise ValueError("Yabai system not available")

            logger.info("[INTELLIGENT] Handling metadata-only query with Yabai")

            # Use Yabai's built-in describe_workspace method for natural language response
            if hasattr(self.yabai_detector, "describe_workspace"):
                response = self.yabai_detector.describe_workspace()
                return response

            # Fallback: Get workspace data and build response manually
            workspace_data = self.yabai_detector.enumerate_all_spaces()
            if not workspace_data:
                return "No desktop spaces detected, Sir."

            # Build detailed response
            space_descriptions = []
            for space in workspace_data:
                space_num = space.get("index", "?")
                apps = space.get("applications", [])
                window_count = space.get("window_count", 0)

                if window_count == 0:
                    space_descriptions.append(f"Space {space_num}: Empty")
                else:
                    primary_app = apps[0] if apps else "Unknown"
                    if len(apps) > 1:
                        space_descriptions.append(
                            f"Space {space_num}: {primary_app} (+{len(apps)-1} more apps, {window_count} windows)"
                        )
                    else:
                        space_descriptions.append(
                            f"Space {space_num}: {primary_app} ({window_count} window{'s' if window_count != 1 else ''})"
                        )

            response = (
                f"Sir, you have {len(workspace_data)} desktop spaces:\n\n"
                + "\n".join(space_descriptions)
            )
            return response

        except Exception as e:
            logger.error(f"[INTELLIGENT] Yabai query handler error: {e}")
            raise

    async def _handle_vision_query(
        self, query: str, context: Optional[Dict[str, Any]], multi_space: bool = False
    ) -> str:
        """
        Handle visual analysis query with current screen capture
        """
        try:
            logger.info(
                f"[INTELLIGENT] Handling vision query (multi_space: {multi_space})"
            )

            # Capture screen
            screenshot = await self._capture_screen(multi_space=multi_space)

            if not screenshot:
                return "I couldn't capture your screen, Sir. Please check screen recording permissions."

            # Use Claude vision intelligence to analyze
            if self.intelligence:
                response = await self.intelligence.understand_and_respond(
                    screenshot, query
                )
                return response
            else:
                return "Vision intelligence not initialized yet, Sir."

        except Exception as e:
            logger.error(f"[INTELLIGENT] Vision query handler error: {e}")
            raise

    async def _handle_multi_space_query(
        self, query: str, context: Optional[Dict[str, Any]]
    ) -> str:
        """
        Handle deep analysis query with Yabai metadata + current space screenshot

        Uses NON-DISRUPTIVE approach:
        - Yabai metadata for ALL spaces (apps, windows, titles)
        - Screenshot of CURRENT space only (no switching)
        - Claude combines both for intelligent analysis
        """
        try:
            logger.info("[INTELLIGENT] Handling multi-space query with Yabai + current space")

            # Get Yabai metadata for ALL spaces (non-disruptive)
            window_data = None
            if self.intelligence and hasattr(
                self.intelligence, "_gather_multi_space_data"
            ):
                window_data = await self.intelligence._gather_multi_space_data()

                # Debug: Log the structure of window_data
                logger.info(f"[INTELLIGENT-DEBUG] Window data keys: {window_data.keys() if window_data else 'None'}")

                spaces = window_data.get('spaces', [])
                logger.info(f"[INTELLIGENT] Retrieved Yabai data for {len(spaces)} spaces")

                # Debug: Log first space details to verify we have window titles
                if spaces:
                    first_space = spaces[0] if isinstance(spaces, list) else next(iter(spaces.values())) if isinstance(spaces, dict) else None
                    if first_space:
                        logger.info(f"[INTELLIGENT-DEBUG] First space sample: {first_space}")
                        if 'windows' in first_space:
                            first_window = first_space['windows'][0] if first_space['windows'] else None
                            if first_window:
                                logger.info(f"[INTELLIGENT-DEBUG] First window sample: app={first_window.get('app', 'N/A')}, title={first_window.get('title', 'N/A')[:50]}")

            # Capture ONLY current space screenshot (non-disruptive)
            current_screenshot = await self._capture_screen(multi_space=False)

            if not current_screenshot:
                logger.warning("[INTELLIGENT] Could not capture current space, using Yabai only")
                # Fall back to Yabai-only analysis
                if self.yabai_detector and self.workspace_analyzer:
                    return await self._handle_yabai_query(query, context)
                return "I couldn't analyze your desktop spaces, Sir."

            # Build enhanced prompt with Yabai context
            if self.intelligence and window_data:
                # Handle different formats of spaces data (could be list or dict)
                spaces_raw = window_data.get('spaces', [])

                # Convert to list if it's a dict
                if isinstance(spaces_raw, dict):
                    spaces = list(spaces_raw.values())
                elif isinstance(spaces_raw, list):
                    spaces = spaces_raw
                else:
                    # Try spaces_list as alternative key
                    spaces = window_data.get('spaces_list', [])
                total_spaces = len(spaces)

                context_summary = [f"You have {total_spaces} desktop spaces total."]

                # Summarize each space with detailed window information
                for space in spaces:
                    space_idx = space.get('index', 'unknown')
                    windows = space.get('windows', [])
                    is_active = space.get('has-focus', False)

                    if windows:
                        # Build detailed window list with titles
                        window_details = []
                        for w in windows[:5]:  # Show first 5 windows with details
                            app = w.get('app', 'Unknown')
                            title = w.get('title', '')
                            if title and len(title) > 50:
                                title = title[:50] + '...'
                            if title:
                                window_details.append(f"{app} ({title})")
                            else:
                                window_details.append(app)

                        detail_list = ', '.join(window_details)
                        suffix = f' + {len(windows)-5} more' if len(windows) > 5 else ''
                        context_summary.append(
                            f"Space {space_idx}{' (current)' if is_active else ''}: {detail_list}{suffix}"
                        )
                    else:
                        context_summary.append(f"Space {space_idx}: Empty")

                # Build enhanced prompt
                enhanced_prompt = f"""{query}

**Complete Desktop Space Overview:**
{chr(10).join(context_summary)}

**Current Space Screenshot:**
The attached screenshot shows your currently active space (Space {window_data.get('current_space', {}).get('id', '?')}).

**Instructions for Analysis:**
Based on the window titles and applications listed above for ALL spaces, plus the visual screenshot of your current space:

1. **Current Space Details**: Describe what's visible in the screenshot - what you're actively working on right now.

2. **Other Space Activities**: For each of the other {total_spaces-1} spaces, analyze the window titles to identify:
   - What task or project you're working on in that space
   - The primary activity (coding, communication, browsing, etc.)
   - Any specific files, documents, or content being worked with

3. **Overall Workflow**: Identify patterns, themes, or how these spaces relate to each other in your overall workflow.

Be SPECIFIC and DETAILED. Use the actual window titles to infer what work is being done. Don't just list apps - explain what you're doing in each space based on the window titles."""

                # Debug: Log the enhanced prompt to verify it's being built correctly
                logger.info(f"[INTELLIGENT-DEBUG] Enhanced prompt length: {len(enhanced_prompt)} chars")
                logger.info(f"[INTELLIGENT-DEBUG] Context summary preview: {context_summary[:3] if context_summary else 'No context'}")
                logger.info("[INTELLIGENT] Sending enhanced prompt to Claude with current space + Yabai context")

                # Call Claude with current screenshot + enhanced prompt
                response = await self.intelligence.understand_and_respond(
                    current_screenshot, enhanced_prompt
                )

                # Process with workspace names if available
                if workspace_processor_available and window_data:
                    response = process_response_with_workspace_names(
                        response, window_data
                    )

                # Store context for follow-up queries
                self._last_multi_space_context = {
                    'spaces': spaces,
                    'window_data': window_data,
                    'timestamp': datetime.now()
                }

                return response or "I analyzed your desktop spaces, Sir."

            # Fallback: Claude with screenshot only (no Yabai context)
            elif self.intelligence:
                logger.info("[INTELLIGENT] Using Claude with current screenshot only")
                response = await self.intelligence.understand_and_respond(
                    current_screenshot, query
                )
                return response

            # Fallback: Yabai metadata-only analysis
            logger.info(
                "[INTELLIGENT] Intelligence not available, falling back to Yabai metadata analysis"
            )
            if self.yabai_detector and self.workspace_analyzer:
                response = await self._handle_yabai_query(query, context)
                
                # Store context for follow-up queries even in fallback mode
                try:
                    spaces = self.yabai_detector.enumerate_all_spaces()
                    self._last_multi_space_context = {
                        'spaces': spaces,
                        'window_data': None,
                        'timestamp': datetime.now()
                    }
                except Exception as e:
                    logger.debug(f"Could not store multi-space context: {e}")
                
                return response

            return "Multi-space analysis not available, Sir."

        except Exception as e:
            logger.error(f"[INTELLIGENT] Multi-space query handler error: {e}", exc_info=True)
            raise

    async def _handle_multi_space_follow_up(self, command_text: str) -> Dict[str, Any]:
        """
        Handle follow-up questions to multi-space queries with Claude Vision analysis
        """
        try:
            command_lower = command_text.lower()
            
            # Check if this is a follow-up response
            follow_up_indicators = [
                "yes", "sure", "okay", "do it", "tell me more", "explain",
                "what about", "how about", "show me", "describe", "analyze"
            ]
            
            is_follow_up = any(indicator in command_lower for indicator in follow_up_indicators)
            
            if not is_follow_up:
                return {"handled": False}
            
            # Check if we have recent multi-space context or orchestration context
            has_context = (
                hasattr(self, '_last_multi_space_context') or 
                hasattr(self, '_last_orchestration_context')
            )
            
            if not has_context:
                return {"handled": False}
            
            # Try to use orchestration context first, then fall back to multi-space context
            if hasattr(self, '_last_orchestration_context'):
                orchestration_context = self._last_orchestration_context
                context_id = orchestration_context.get('context_id')
                
                if context_id:
                    # Use intelligent orchestrator for follow-up
                    api_key = None
                    try:
                        from core.secret_manager import get_anthropic_key
                        api_key = get_anthropic_key()
                    except Exception:
                        api_key = os.getenv("ANTHROPIC_API_KEY")

                    if api_key:
                        try:
                            from vision.intelligent_orchestrator import get_intelligent_orchestrator
                            orchestrator = get_intelligent_orchestrator()
                            
                            result = await orchestrator.handle_follow_up_query(
                                query=command_text,
                                context_id=context_id,
                                claude_api_key=api_key
                            )
                            
                            if result.get("success"):
                                return {
                                    "handled": True,
                                    "response": result.get("analysis", {}).get("analysis", "Follow-up analysis completed"),
                                    "follow_up_analysis": True,
                                    "uses_claude_vision": True,
                                    "orchestration": True
                                }
                        except Exception as e:
                            logger.warning(f"[FOLLOW-UP] Orchestration follow-up failed: {e}")
            
            # Fall back to original multi-space context
            if hasattr(self, '_last_multi_space_context'):
                context = self._last_multi_space_context
                spaces = context.get('spaces', [])
                
                if not spaces:
                    return {"handled": False}
            else:
                return {"handled": False}
            
            logger.info("[FOLLOW-UP] Detected multi-space follow-up query")
            
            # Determine which space to analyze
            target_space = None
            
            # Check for specific space references
            import re
            space_match = re.search(r'space\s+(\d+)', command_lower)
            if space_match:
                target_space_id = int(space_match.group(1))
                target_space = next((s for s in spaces if s.get('space_id') == target_space_id), None)
            
            # Check for application references
            if not target_space:
                for space in spaces:
                    apps = space.get('applications', [])
                    for app in apps:
                        if app.lower() in command_lower:
                            target_space = space
                            break
                    if target_space:
                        break
            
            # Default to current space if no specific target
            if not target_space:
                target_space = next((s for s in spaces if s.get('is_current', False)), spaces[0] if spaces else None)
            
            if not target_space:
                return {"handled": False}
            
            # Capture screenshot of the target space
            logger.info(f"[FOLLOW-UP] Analyzing Space {target_space.get('space_id', '?')} with Claude Vision")
            
            # Switch to the target space and capture screenshot
            screenshot = await self._capture_space_screenshot(target_space.get('space_id', 1))
            
            if screenshot is None:
                return {
                    "handled": True,
                    "response": f"I couldn't capture a screenshot of Space {target_space.get('space_id', '?')}, Sir.",
                    "follow_up_analysis": True
                }
            
            # Build enhanced prompt for Claude Vision analysis
            space_id = target_space.get('space_id', '?')
            apps = target_space.get('applications', [])
            primary_app = target_space.get('primary_activity', 'Unknown')
            
            enhanced_prompt = f"""You are JARVIS analyzing Space {space_id} in detail.

SPACE CONTEXT:
- Space ID: {space_id}
- Primary Application: {primary_app}
- Applications: {', '.join(apps) if apps else 'None'}
- Is Current Space: {target_space.get('is_current', False)}

USER'S FOLLOW-UP REQUEST: "{command_text}"

INSTRUCTIONS:
1. Analyze the screenshot of Space {space_id} in detail
2. Focus on what the user is asking about specifically
3. Provide detailed information about:
   - What applications are visible and what they're showing
   - Any specific content, errors, or important information
   - The current state of work in this space
4. Be specific and detailed - quote exact text when visible
5. Address the user as "Sir" naturally
6. If there are any errors or issues, highlight them clearly

Provide a comprehensive analysis of what you see in Space {space_id}."""

            # Use Claude Vision for detailed analysis
            if self.intelligence and hasattr(self.intelligence, 'claude') and self.intelligence.claude:
                # Call Claude Vision directly for detailed analysis
                try:
                    claude_response = await self.intelligence.claude.analyze_image_with_prompt(
                        image=screenshot,
                        prompt=enhanced_prompt,
                        max_tokens=1000
                    )
                    
                    # Extract response text
                    if isinstance(claude_response, dict):
                        response = claude_response.get('content', claude_response.get('response', str(claude_response)))
                    else:
                        response = str(claude_response)
                        
                except Exception as e:
                    logger.error(f"[FOLLOW-UP] Claude Vision analysis failed: {e}")
                    response = f"I can see Space {space_id} has {primary_app} running, but I encountered an error during detailed analysis, Sir."
            else:
                response = f"I can see Space {space_id} has {primary_app} running, but I need Claude Vision to provide detailed analysis, Sir."
            
            return {
                "handled": True,
                "response": response,
                "follow_up_analysis": True,
                "analyzed_space": space_id,
                "uses_claude_vision": True
            }
                
        except Exception as e:
            logger.error(f"[FOLLOW-UP] Multi-space follow-up handler error: {e}", exc_info=True)
            return {"handled": False}

    async def _capture_space_screenshot(self, space_id: int) -> Any:
        """Capture screenshot of a specific space without switching"""
        try:
            logger.info(f"[FOLLOW-UP] Attempting to capture Space {space_id} without switching")
            
            # Try to use multi-space capture engine for non-disruptive capture
            try:
                from vision.multi_space_capture_engine import MultiSpaceCaptureEngine, SpaceCaptureRequest, CaptureQuality
                
                # Create capture engine
                capture_engine = MultiSpaceCaptureEngine()
                
                # Create capture request
                request = SpaceCaptureRequest(
                    space_ids=[space_id],
                    reason="follow_up_analysis",
                    quality=CaptureQuality.FULL,
                    use_cache=True
                )
                
                # Attempt capture using CG Windows API (non-disruptive)
                screenshot = await capture_engine._capture_with_cg_windows(space_id, request)
                
                if screenshot is not None:
                    logger.info(f"[FOLLOW-UP] Successfully captured Space {space_id} using CG Windows API")
                    return screenshot
                else:
                    logger.warning(f"[FOLLOW-UP] CG Windows capture failed for Space {space_id}")
                    
            except Exception as e:
                logger.warning(f"[FOLLOW-UP] Multi-space capture engine failed: {e}")
            
            # Fallback: Use AppleScript to switch spaces (disruptive but functional)
            try:
                logger.info(f"[FOLLOW-UP] Attempting AppleScript space switch to Space {space_id}")
                import subprocess
                
                # Get current space from context to switch back later
                original_space_id = None
                if hasattr(self, '_last_multi_space_context'):
                    spaces = self._last_multi_space_context.get('spaces', [])
                    current_space = next((s for s in spaces if s.get('is_current', False)), None)
                    if current_space:
                        original_space_id = current_space.get('space_id')
                
                # Calculate how many spaces to move (right = +1, left = -1)
                if original_space_id and space_id != original_space_id:
                    spaces_to_move = space_id - original_space_id
                    
                    # Use AppleScript to switch spaces
                    for _ in range(abs(spaces_to_move)):
                        if spaces_to_move > 0:
                            # Move right
                            result = subprocess.run(
                                ['osascript', '-e', 'tell application "System Events" to key code 19 using {control down}'],
                                capture_output=True,
                                text=True,
                                timeout=2
                            )
                        else:
                            # Move left
                            result = subprocess.run(
                                ['osascript', '-e', 'tell application "System Events" to key code 18 using {control down}'],
                                capture_output=True,
                                text=True,
                                timeout=2
                            )
                        
                        if result.returncode != 0:
                            logger.warning(f"[FOLLOW-UP] AppleScript space switch failed: {result.stderr}")
                            break
                        
                        # Small delay between switches
                        await asyncio.sleep(0.3)
                    
                    logger.info(f"[FOLLOW-UP] Successfully switched to Space {space_id}")
                    # Additional delay to let the space switch complete
                    await asyncio.sleep(0.5)
                    
                    # Capture screenshot of the switched space
                    screenshot = await self._capture_screen(multi_space=False)
                    
                    # Switch back to original space
                    if original_space_id:
                        try:
                            spaces_to_move_back = original_space_id - space_id
                            for _ in range(abs(spaces_to_move_back)):
                                if spaces_to_move_back > 0:
                                    # Move right
                                    subprocess.run(
                                        ['osascript', '-e', 'tell application "System Events" to key code 19 using {control down}'],
                                        capture_output=True,
                                        text=True,
                                        timeout=2
                                    )
                                else:
                                    # Move left
                                    subprocess.run(
                                        ['osascript', '-e', 'tell application "System Events" to key code 18 using {control down}'],
                                        capture_output=True,
                                        text=True,
                                        timeout=2
                                    )
                                await asyncio.sleep(0.3)
                            logger.info(f"[FOLLOW-UP] Switched back to Space {original_space_id}")
                        except Exception as e:
                            logger.warning(f"[FOLLOW-UP] Failed to switch back: {e}")
                    
                    return screenshot
                else:
                    logger.info(f"[FOLLOW-UP] Already on Space {space_id}, capturing current space")
                    screenshot = await self._capture_screen(multi_space=False)
                    return screenshot
                        
            except Exception as e:
                logger.warning(f"[FOLLOW-UP] AppleScript space switching failed: {e}")
            
            # Final fallback: capture current space and inform user
            logger.info(f"[FOLLOW-UP] Falling back to current space capture")
            screenshot = await self._capture_screen(multi_space=False)
            
            return screenshot
            
        except Exception as e:
            logger.error(f"[FOLLOW-UP] Failed to capture space {space_id}: {e}")
            return None

    def _is_multi_monitor_query(self, command_text: str) -> bool:
        """Check if query is about multiple monitors/displays"""
        query_lower = command_text.lower()
        monitor_keywords = [
            "monitor", "display", "screen",
            "second monitor", "primary monitor", "main monitor",
            "monitor 1", "monitor 2", "monitor 3", "monitor 4",
            "all monitors", "all displays", "both monitors",
            "left monitor", "right monitor", "show me all displays"
        ]
        return any(keyword in query_lower for keyword in monitor_keywords)
    
    def _is_multi_space_query(self, command_text: str) -> bool:
        """Check if query is about multiple desktop spaces OR specific space analysis"""
        command_lower = command_text.lower()
        
        # Multi-space overview indicators
        multi_space_indicators = [
            "across", "all my", "every", "each", "multiple", "spaces", "desktops",
            "workspace", "what's happening", "what is happening", "show me all",
            "tell me about", "overview", "summary", "list all"
        ]
        
        # Visual analysis indicators (even for single space)
        visual_analysis_indicators = [
            "error", "see", "look at", "show me", "what's in", "what is in",
            "analyze", "read", "check", "debug", "terminal", "code", "browser",
            "space 1", "space 2", "space 3", "space 4", "space 5", "space 6",
            "space 7", "space 8", "space 9", "space 10"
        ]
        
        # Return true if it's a multi-space query OR a visual analysis query
        return (any(indicator in command_lower for indicator in multi_space_indicators) or
                any(indicator in command_lower for indicator in visual_analysis_indicators))

    async def _handle_multi_monitor_query(self, command_text: str) -> Dict[str, Any]:
        """Handle multi-monitor specific queries with intelligent routing"""
        try:
            logger.info("[MULTI-MONITOR] Handling multi-monitor query")
            
            from vision.multi_monitor_detector import MultiMonitorDetector
            from vision.query_disambiguation import get_query_disambiguator
            
            detector = MultiMonitorDetector()
            
            # Detect displays
            displays = await detector.detect_displays()
            
            if len(displays) == 0:
                return {
                    "handled": True,
                    "response": "Sir, I cannot detect any displays. Please ensure screen recording permissions are enabled.",
                    "monitoring_active": self.monitoring_active
                }
            
            if len(displays) == 1:
                # Single display - redirect to normal space analysis
                logger.info("[MULTI-MONITOR] Only one display, routing to multi-space handler")
                return {"handled": False}  # Let multi-space handler take over
            
            # Handle "show me all displays" type queries
            query_lower = command_text.lower()
            if any(keyword in query_lower for keyword in ["all displays", "all monitors", "show me all"]):
                summary = await detector.get_display_summary()
                
                # Generate natural language summary
                response_parts = [f"Sir, you have {len(displays)} displays connected:"]
                for i, display in enumerate(displays):
                    position = "Primary" if display.is_primary else f"Monitor {i+1}"
                    resolution = f"{display.resolution[0]}x{display.resolution[1]}"
                    response_parts.append(f"\n• {position}: {resolution}")
                    
                    # Add space info if available
                    if summary.get("space_mappings"):
                        spaces_on_display = [
                            space_id for space_id, mapping in summary["space_mappings"].items()
                            if mapping.display_id == display.display_id
                        ]
                        if spaces_on_display:
                            response_parts.append(f"  (Spaces: {', '.join(map(str, spaces_on_display))})")
                
                return {
                    "handled": True,
                    "response": "".join(response_parts),
                    "display_summary": summary,
                    "monitoring_active": self.monitoring_active
                }
            
            # Parse which monitor user is asking about
            disambiguator = get_query_disambiguator()
            monitor_ref = await disambiguator.resolve_monitor_reference(command_text, displays)
            
            if monitor_ref is None or monitor_ref.ambiguous:
                # Ask for clarification
                clarification = await disambiguator.ask_clarification(command_text, displays)
                return {
                    "handled": True,
                    "response": clarification,
                    "needs_clarification": True,
                    "available_displays": len(displays),
                    "monitoring_active": self.monitoring_active
                }
            
            # Capture specific monitor
            result = await detector.capture_all_displays()
            
            if monitor_ref.display_id not in result.displays_captured:
                return {
                    "handled": True,
                    "response": f"Sir, I was unable to capture display {monitor_ref.display_id}.",
                    "monitoring_active": self.monitoring_active
                }
            
            # Get display info
            target_display = next((d for d in displays if d.display_id == monitor_ref.display_id), None)
            
            # Analyze with Claude
            screenshot = result.displays_captured[monitor_ref.display_id]
            
            if target_display:
                display_name = "the primary display" if target_display.is_primary else f"Monitor {displays.index(target_display) + 1}"
                analysis_query = f"Analyze this screenshot from {display_name}: {command_text}"
            else:
                analysis_query = command_text
            
            # Use Claude Vision for analysis
            response = await self.intelligence.understand_and_respond(screenshot, analysis_query)
            
            return {
                "handled": True,
                "response": response,
                "display_id": monitor_ref.display_id,
                "display_info": {
                    "resolution": target_display.resolution,
                    "is_primary": target_display.is_primary,
                    "position": target_display.position
                } if target_display else {},
                "monitoring_active": self.monitoring_active
            }
            
        except Exception as e:
            logger.error(f"[MULTI-MONITOR] Error handling query: {e}", exc_info=True)
            return {"handled": False}
    
    async def _handle_tv_monitor_response(self, command_text: str) -> Dict[str, Any]:
        """
        SIMPLE TV MONITOR: Handle voice responses for TV connection prompts

        Intercepts "yes", "no" commands when JARVIS asks about connecting to Living Room TV
        """
        try:
            from display import get_display_monitor

            monitor = get_display_monitor()

            # Only handle if we're waiting for a response
            has_pending = monitor.has_pending_prompt()
            logger.info(f"[TV MONITOR] Checking pending prompt: {has_pending}, pending_display={getattr(monitor, 'pending_prompt_display', None)}")

            if not has_pending:
                logger.info(f"[TV MONITOR] No pending prompt, skipping handler")
                return {"handled": False}

            logger.info(f"[TV MONITOR] Has pending prompt! Handling response: '{command_text}'")
            
            # Handle the voice response
            result = await monitor.handle_user_response(command_text)
            
            if result.get("handled"):
                # Use the dynamic response from handle_user_response
                response_text = result.get("response", "")

                return {
                    "handled": True,
                    "response": response_text,
                    "action": result.get("action"),
                    "monitoring_active": self.monitoring_active,
                    "connection_result": result.get("result", {})
                }
            else:
                return {"handled": False}
                
        except Exception as e:
            logger.error(f"[TV MONITOR] Error handling response: {e}")
            return {"handled": False}
    
    async def _handle_voice_prompt_response(self, command_text: str) -> Dict[str, Any]:
        """
        PHASE 1.2C: Handle voice prompt responses (Yes/No for display connection)
        
        Intercepts "yes", "no", "connect", etc. commands when waiting for
        display connection prompt response.
        """
        try:
            from proximity.voice_prompt_manager import get_voice_prompt_manager
            
            manager = get_voice_prompt_manager()
            
            # Only handle if we're waiting for a response
            if manager.prompt_state.value != "waiting_for_response":
                return {"handled": False}
            
            logger.info(f"[VOICE PROMPT] Handling response: {command_text}")
            
            # Handle the voice response
            result = await manager.handle_voice_response(command_text)
            
            if result.get("handled"):
                return {
                    "handled": True,
                    "response": result.get("response", ""),
                    "action": result.get("action"),
                    "connection_result": result.get("connection_result"),
                    "monitoring_active": self.monitoring_active
                }
            else:
                return {"handled": False}
                
        except Exception as e:
            logger.error(f"[VOICE PROMPT] Error handling response: {e}")
            return {"handled": False}
    
    async def _handle_proximity_aware_routing(self, command_text: str) -> Dict[str, Any]:
        """
        PHASE 1.2C: Handle proximity-aware command routing
        
        Routes vision commands to displays based on user proximity context.
        Adds natural language acknowledgments and generates voice prompts.
        """
        try:
            # Check if this is a vision/display command
            vision_keywords = ["show", "display", "what's", "analyze", "look at", "see", "check"]
            if not any(keyword in command_text.lower() for keyword in vision_keywords):
                return {"handled": False}
            
            logger.info("[PROXIMITY ROUTING] Attempting proximity-aware routing")
            
            from proximity.proximity_command_router import get_proximity_command_router
            from proximity.proximity_display_bridge import get_proximity_display_bridge
            from proximity.voice_prompt_manager import get_voice_prompt_manager
            from proximity.display_availability_detector import get_availability_detector
            
            # Get routing result
            router = get_proximity_command_router()
            routing_result = await router.route_command(command_text)
            
            if routing_result.get("success") and routing_result.get("proximity_based"):
                # Proximity-based routing successful
                voice_response = routing_result.get("voice_response")
                target_display = routing_result.get("target_display")
                display_id = routing_result.get("display_id")
                
                # Check if target display is available (TV on/off detection)
                detector = get_availability_detector()
                is_available = await detector.is_display_available(display_id)
                
                if not is_available:
                    return {
                        "handled": True,
                        "response": f"Sir, the {routing_result.get('display_name')} appears to be offline or disconnected. Please ensure it's powered on.",
                        "monitoring_active": self.monitoring_active
                    }
                
                # Check if we should generate a connection prompt
                bridge = get_proximity_display_bridge()
                decision = await bridge.make_connection_decision()
                
                if decision and decision.action == ConnectionAction.PROMPT_USER:
                    # Generate voice prompt
                    prompt_manager = get_voice_prompt_manager()
                    prompt = await prompt_manager.generate_prompt_for_decision(decision)
                    
                    if prompt:
                        # Return prompt to user
                        return {
                            "handled": True,
                            "response": prompt,
                            "awaiting_response": True,
                            "display_id": display_id,
                            "monitoring_active": self.monitoring_active
                        }
                
                # Regular routing response
                logger.info(f"[PROXIMITY ROUTING] Routed to display: {routing_result.get('display_name')}")
                
                return {
                    "handled": True,
                    "response": f"{voice_response}\n\nProcessing your command: {command_text}",
                    "routing_info": routing_result,
                    "proximity_based": True,
                    "monitoring_active": self.monitoring_active
                }
            else:
                # No proximity data or routing failed
                return {"handled": False}
                
        except Exception as e:
            logger.error(f"[PROXIMITY ROUTING] Error: {e}")
            return {"handled": False}
    
    async def _handle_intelligent_orchestration(self, command_text: str) -> Dict[str, Any]:
        """Handle multi-space queries using intelligent orchestration"""
        try:
            logger.info("[ORCHESTRATOR] Handling multi-space query with intelligent orchestration")

            # Get API key (can be None for metadata-only queries)
            api_key = None
            try:
                from core.secret_manager import get_anthropic_key
                api_key = get_anthropic_key()
            except Exception:
                api_key = os.getenv("ANTHROPIC_API_KEY")

            if not api_key:
                logger.warning("[ORCHESTRATOR] No Claude API key - will use metadata-based analysis only")
            
            # Import and use intelligent orchestrator
            from vision.intelligent_orchestrator import get_intelligent_orchestrator
            
            orchestrator = get_intelligent_orchestrator()
            
            # Perform intelligent analysis (works with or without API key)
            result = await orchestrator.analyze_workspace_intelligently(
                query=command_text,
                claude_api_key=api_key  # Can be None
            )
            
            if result.get("success"):
                # Store context for follow-up questions
                context_id = result.get("context_id")
                if context_id:
                    self._last_orchestration_context = {
                        "context_id": context_id,
                        "timestamp": datetime.now(),
                        "query": command_text
                    }
                
                return {
                    "handled": True,
                    "response": result.get("analysis", {}).get("analysis", "Analysis completed"),
                    "orchestration": True,
                    "context_id": context_id,
                    "patterns": result.get("patterns_detected", []),
                    "performance": result.get("performance", {}),
                    "uses_claude_vision": True
                }
            else:
                logger.warning(f"[ORCHESTRATOR] Analysis failed: {result.get('error')}")
                return {"handled": False}
                
        except Exception as e:
            logger.error(f"[ORCHESTRATOR] Intelligent orchestration failed: {e}", exc_info=True)
            return {"handled": False}

    async def _proactive_monitoring_loop(self):
        """Proactive monitoring with pure intelligence"""
        logger.info("[VISION] Starting proactive monitoring loop")
        
        while self.monitoring_active:
            try:
                # Wait before next check
                await asyncio.sleep(5)
                
                if not self.monitoring_active:
                    break
                    
                # Capture screen and check for important changes
                screenshot = await self._capture_screen()
                if screenshot:
                    proactive_message = await self.proactive.observe_and_communicate(
                        screenshot
                    )
                    
                    if proactive_message and self.jarvis_api:
                        # Send proactive message through JARVIS voice
                        try:
                            await self.jarvis_api.speak_proactive(proactive_message)
                        except Exception as e:
                            logger.error(f"Failed to speak proactive message: {e}")
                            
            except Exception as e:
                logger.error(f"Proactive monitoring error: {e}")
                await asyncio.sleep(5)
                
    async def _capture_screen(
        self, multi_space=False, space_number=None
    ) -> Optional[Any]:
        """
        Capture screen(s) with multi-space support

        Args:
            multi_space: If True, capture all desktop spaces
            space_number: If provided, capture specific space

        Returns:
            Single screenshot or Dict[int, screenshot] for multi-space
        """
        try:
            # Initialize vision manager if needed
            await self._ensure_vision_manager()

            if (
                self.vision_manager
                and hasattr(self.vision_manager, "vision_analyzer")
                and self.vision_manager.vision_analyzer
            ):
                # Use enhanced capture with multi-space support WITH TIMEOUT
                logger.info(f"[VISION-CAPTURE] Starting screen capture (multi_space={multi_space}, space_number={space_number})")
                try:
                    screenshot = await asyncio.wait_for(
                        self.vision_manager.vision_analyzer.capture_screen(
                            multi_space=multi_space, space_number=space_number
                        ),
                        timeout=15.0  # 15 second timeout for screen capture
                    )
                    logger.info(f"[VISION-CAPTURE] ✅ Screen capture completed successfully")
                    return screenshot
                except asyncio.TimeoutError:
                    logger.error(f"[VISION-CAPTURE] ❌ Screen capture timed out after 15 seconds")
                    return None
            else:
                # Try to capture screen directly as fallback
                logger.info("[VISION-CAPTURE] Attempting direct screen capture fallback...")
                try:
                    # Try macOS screencapture WITH TIMEOUT
                    import subprocess
                    import tempfile
                    from PIL import Image

                    with tempfile.NamedTemporaryFile(
                        suffix=".png", delete=False
                    ) as tmp:
                        tmp_path = tmp.name

                    # Capture screen with timeout
                    result = subprocess.run(
                        ["screencapture", "-x", tmp_path],
                        capture_output=True,
                        text=True,
                        timeout=10.0  # 10 second timeout for direct capture
                    )

                    if result.returncode == 0:
                        # Load and return image
                        screenshot = Image.open(tmp_path)
                        os.unlink(tmp_path)  # Clean up
                        logger.info("[VISION-CAPTURE] ✅ Direct screen capture successful")
                        return screenshot
                    else:
                        logger.error(f"[VISION-CAPTURE] ❌ screencapture command failed: {result.stderr}")

                except subprocess.TimeoutExpired:
                    logger.error(f"[VISION-CAPTURE] ❌ Direct capture timed out after 10 seconds")
                except Exception as e:
                    logger.error(f"[VISION-CAPTURE] ❌ Direct capture failed: {e}")
                    
        except Exception as e:
            logger.error(f"Screen capture error: {e}")
            
        return None
        
    async def _ensure_vision_manager(self):
        """Initialize vision manager if not already done"""
        if not self.vision_manager:
            try:
                logger.info("[VISION INIT] Attempting to import vision_manager...")
                try:
                    from api.vision_websocket import vision_manager
                except ImportError:
                    from .vision_websocket import vision_manager
                    
                self.vision_manager = vision_manager
                logger.info(f"[VISION INIT] Vision manager imported: {vision_manager}")
                
                # Check if vision_analyzer needs initialization
                if hasattr(vision_manager, "vision_analyzer"):
                    if vision_manager.vision_analyzer is None:
                        logger.info(
                            "[VISION INIT] Vision analyzer is None, checking app state..."
                        )
                        # Try to get from app state
                        try:
                            import sys
                            import os

                            sys.path.append(
                                os.path.dirname(
                                    os.path.dirname(os.path.abspath(__file__))
                                )
                            )
                            from main import app

                            if hasattr(app.state, "vision_analyzer"):
                                vision_manager.vision_analyzer = (
                                    app.state.vision_analyzer
                                )
                                logger.info(
                                    "[VISION INIT] Set vision analyzer from app state"
                                )
                        except Exception as e:
                            logger.error(
                                f"[VISION INIT] Failed to get vision analyzer from app state: {e}"
                            )
                        
                        # If still None and we have our own vision analyzer, use that
                        if (
                            vision_manager.vision_analyzer is None
                            and hasattr(self, "vision_analyzer")
                            and self.vision_analyzer
                        ):
                            vision_manager.vision_analyzer = self.vision_analyzer
                            logger.info(
                                "[VISION INIT] Set vision analyzer from handler"
                            )
                    else:
                        logger.info("[VISION INIT] Vision analyzer already set")
                        
            except Exception as e:
                logger.error(f"Failed to initialize vision manager: {e}")
                
    async def _get_error_response(
        self, error_type: str, command: str, details: str = ""
    ) -> Dict[str, Any]:
        """Even errors are communicated naturally by Claude"""
        error_prompt = f"""The user asked: "{command}"

An error occurred: {error_type}
{f"Details: {details}" if details else ""}

You're JARVIS. Respond naturally to explain the issue and suggest a solution.
Be helpful and specific, but keep it conversational.
Never use generic error messages or technical jargon.
"""
        
        # Use mock response if no Claude client
        if self.intelligence and self.intelligence.claude:
            response = await self.intelligence._get_claude_vision_response(
                None, error_prompt
            )
            error_message = response.get("response")
        else:
            # Natural fallback
            if error_type == "screenshot_failed":
                if "timed out" in details.lower():
                    error_message = (
                        "The screen capture is taking longer than expected, Sir. "
                        "This might be due to system resource constraints or screen recording permissions. "
                        "Please ensure JARVIS has Screen Recording permissions in System Settings > Privacy & Security."
                    )
                else:
                    error_message = "I'm having trouble accessing your screen right now, Sir. Let me check the vision system configuration."
            elif error_type == "intelligence_error":
                error_message = "I encountered an issue processing that request. Let me recalibrate the vision systems."
            else:
                error_message = f"Something went wrong with that request, Sir. {details if details else 'Let me investigate.'}"
                
        return {
            "handled": True,
            "response": error_message,
            "error": True,
            "pure_intelligence": True,
            "monitoring_active": self.monitoring_active,
        }
        
    def get_session_stats(self) -> Dict[str, Any]:
        """Get session statistics"""
        stats = {
            "conversation_length": 0,
            "monitoring_active": self.monitoring_active,
            "workflow_state": "unknown",
            "emotional_state": "neutral",
        }

        # Legacy intelligence stats
        if self.intelligence and self.intelligence.context:
            stats.update(
                {
                "conversation_length": len(self.intelligence.context.history),
                "workflow_state": self.intelligence.context.workflow_state,
                    "emotional_state": (
                        self.intelligence.context.emotional_context.value
                        if self.intelligence.context.emotional_context
                        else "neutral"
                    ),
                }
            )

        # Add intelligent system stats if available
        if intelligent_system_available and self.context_manager:
            try:
                intelligent_stats = self.context_manager.get_session_stats()
                stats["intelligent_system"] = intelligent_stats
            except Exception as e:
                logger.warning(f"Could not get intelligent system stats: {e}")

        return stats

    async def get_performance_report(self) -> Dict[str, Any]:
        """
        Get comprehensive performance report from intelligent system

        Returns:
            Performance report with metrics and insights
        """
        if not intelligent_system_available or not self.performance_monitor:
            return {"available": False, "message": "Intelligent system not available"}

        try:
            # Collect latest metrics
            await self.performance_monitor.collect_metrics()

            # Generate report
            report = self.performance_monitor.generate_report()

            # Add insights
            report["insights"] = self.performance_monitor.get_performance_insights()

            # Add real-time stats
            report["real_time"] = self.performance_monitor.get_real_time_stats()

            return {"available": True, "report": report}

        except Exception as e:
            logger.error(f"Failed to generate performance report: {e}")
            return {"available": False, "error": str(e)}

    async def get_classification_stats(self) -> Dict[str, Any]:
        """
        Get classification statistics

        Returns:
            Classification accuracy and routing stats
        """
        if not intelligent_system_available:
            return {"available": False}

        try:
            stats = {}

            # Classifier stats
            if self.classifier:
                stats["classifier"] = self.classifier.get_performance_stats()

            # Router stats
            if self.smart_router:
                stats["router"] = self.smart_router.get_routing_stats()

            # Learning stats
            if self.learning_system:
                stats["learning"] = self.learning_system.get_accuracy_report()

            # Context stats
            if self.context_manager:
                stats["context"] = self.context_manager.get_session_stats()
                stats["user_preferences"] = self.context_manager.get_user_preferences()

            # Proactive suggestions stats
            if self.proactive_system:
                stats["proactive_suggestions"] = self.proactive_system.get_statistics()

            # A/B testing stats
            if (
                self.smart_router
                and hasattr(self.smart_router, "ab_test")
                and self.smart_router.ab_test
            ):
                stats["ab_testing"] = self.smart_router.get_ab_test_report()

            return {"available": True, "stats": stats}

        except Exception as e:
            logger.error(f"Failed to get classification stats: {e}")
            return {"available": False, "error": str(e)}

    async def get_proactive_suggestions(self) -> Dict[str, Any]:
        """
        Get proactive suggestions based on current state

        Returns:
            Dict with suggestions if available
        """
        if not proactive_suggestions_available or not self.proactive_system:
            return {"available": False}

        try:
            # Get current context
            context = {}
            if self.context_manager:
                context = self.context_manager.get_context_for_query("")

            # Get Yabai data if available
            yabai_data = None
            if self.yabai_detector:
                try:
                    yabai_data = {
                        "spaces": self.yabai_detector.enumerate_all_spaces(),
                        "active_space": context.get("active_space"),
                    }
                except Exception:
                    pass  # Continue without Yabai data

            # Analyze and get suggestion
            suggestion = await self.proactive_system.analyze_and_suggest(
                context, yabai_data
            )

            if suggestion:
                return {
                    "available": True,
                    "has_suggestion": True,
                    "suggestion": {
                        "id": suggestion.suggestion_id,
                        "type": suggestion.type.value,
                        "priority": suggestion.priority.value,
                        "message": suggestion.message,
                        "action": suggestion.action,
                    },
                }
            else:
                return {"available": True, "has_suggestion": False}

        except Exception as e:
            logger.error(f"Failed to get proactive suggestions: {e}")
            return {"available": False, "error": str(e)}

    async def respond_to_suggestion(
        self, suggestion_id: str, accepted: bool
    ) -> Dict[str, Any]:
        """
        Handle user's response to a proactive suggestion

        Args:
            suggestion_id: ID of the suggestion
            accepted: Whether user accepted or dismissed

        Returns:
            Dict with response or action result
        """
        if not proactive_suggestions_available or not self.proactive_system:
            return {"available": False}

        try:
            # Record user response
            await self.proactive_system.record_user_response(suggestion_id, accepted)

            if accepted:
                # Find the suggestion and execute its action
                suggestions = self.proactive_system.get_active_suggestions()
                suggestion = next(
                    (s for s in suggestions if s.suggestion_id == suggestion_id), None
                )

                if suggestion:
                    # Execute the suggested action
                    action = suggestion.action

                    if action.startswith("analyze_space_"):
                        space_id = action.split("_")[-1]
                        # Analyze the specific space
                        return {
                            "accepted": True,
                            "action": "analyze_space",
                            "space_id": space_id,
                            "message": f"Analyzing Space {space_id}...",
                        }

                    elif action == "workspace_summary":
                        # Generate workspace summary
                        return {
                            "accepted": True,
                            "action": "workspace_summary",
                            "message": "Generating workspace summary...",
                        }

                    elif action == "workspace_overview":
                        # Generate workspace overview
                        return {
                            "accepted": True,
                            "action": "workspace_overview",
                            "message": "Here's your workspace overview...",
                        }

                    elif action == "analyze_workflow":
                        # Analyze workflow
                        return {
                            "accepted": True,
                            "action": "analyze_workflow",
                            "message": "Analyzing your workflow patterns...",
                        }

                    else:
                        return {
                            "accepted": True,
                            "action": action,
                            "message": "Processing your request...",
                        }

            return {
                "accepted": accepted,
                "message": "Dismissed" if not accepted else "Accepted",
            }

        except Exception as e:
            logger.error(f"Failed to respond to suggestion: {e}")
            return {"error": str(e)}


# Singleton instance
vision_command_handler = VisionCommandHandler()
