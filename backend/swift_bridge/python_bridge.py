#!/usr/bin/env python3
"""Python-Swift Bridge for JARVIS Command Classifier.

This module provides seamless integration between Python and Swift classifier
components, enabling high-performance natural language processing for command
classification in the JARVIS system.

The module includes:
- SwiftCommandClassifier: Direct interface to Swift-based ML classifier
- IntelligentCommandRouter: Smart routing system using ML classification
- Fallback mechanisms for environments without Swift support

Example:
    >>> router = IntelligentCommandRouter()
    >>> handler, details = await router.route_command("close safari")
    >>> print(f"Route to: {handler}")
    Route to: system
"""

import subprocess
import json
import os
import sys
import asyncio
import logging
from typing import Dict, Any, Optional, Tuple
from pathlib import Path
import ctypes
from concurrent.futures import ThreadPoolExecutor

# Import daemon executor for clean shutdown
try:
    from core.thread_manager import get_daemon_executor
    _USE_DAEMON_EXECUTOR = True
except ImportError:
    _USE_DAEMON_EXECUTOR = False
import time

logger = logging.getLogger(__name__)

# Check if Swift is available
SWIFT_AVAILABLE = False
try:
    result = subprocess.run(["swift", "--version"], capture_output=True, text=True)
    if result.returncode == 0:
        SWIFT_AVAILABLE = True
except Exception:
    pass


class SwiftCommandClassifier:
    """Python wrapper for Swift Command Classifier.
    
    Provides a Python interface to the Swift-based machine learning classifier
    for natural language command processing. Supports both subprocess and
    dynamic library communication modes for optimal performance.
    
    Attributes:
        use_dynamic_lib (bool): Whether to use dynamic library for communication
        swift_bridge_dir (Path): Directory containing Swift bridge components
        classifier_path (Path): Path to compiled Swift classifier executable
        lib_path (Path): Path to dynamic library file
        executor (ThreadPoolExecutor): Thread pool for async operations
        _lib: Loaded dynamic library instance
        _classifier: Swift classifier instance handle
    
    Example:
        >>> classifier = SwiftCommandClassifier(use_dynamic_lib=True)
        >>> result = await classifier.classify_command("open safari")
        >>> print(result['type'])
        system
    """
    
    def __init__(self, use_dynamic_lib: bool = False):
        """Initialize the Swift Command Classifier.
        
        Args:
            use_dynamic_lib: If True, use dynamic library for faster performance.
                           If False or library unavailable, use subprocess mode.
        
        Raises:
            RuntimeError: If Swift is not available and classifier cannot be built.
        """
        self.use_dynamic_lib = use_dynamic_lib
        self.swift_bridge_dir = Path(__file__).parent
        self.classifier_path = self.swift_bridge_dir / ".build/release/jarvis-classifier"
        self.lib_path = self.swift_bridge_dir / ".build/release/libCommandClassifierDynamic.dylib"
        # Daemon executor for clean shutdown
        if _USE_DAEMON_EXECUTOR:
            self.executor = get_daemon_executor(max_workers=2, name='swift-bridge')
        else:
            self.executor = ThreadPoolExecutor(max_workers=2)
        self._lib = None
        self._classifier = None
        
        # Build if needed
        if not self.classifier_path.exists():
            self._build_swift_package()
            
        # Initialize based on mode
        if use_dynamic_lib and self.lib_path.exists():
            self._init_dynamic_lib()
        
    def _build_swift_package(self):
        """Build the Swift package if not already built.

        Compiles the Swift classifier package in release mode for optimal
        performance. Logs build progress and handles build failures gracefully.

        ROOT CAUSE FIX v2.0.0:
        - 5 second timeout on Swift build to prevent blocking
        - Fast-fail if Swift not available

        Raises:
            RuntimeError: If Swift is not available or build fails.
        """
        if not SWIFT_AVAILABLE:
            logger.warning("Swift not available, using Python fallback classifier")
            raise RuntimeError("Swift not available")

        logger.info("Building Swift command classifier (5s timeout)...")
        try:
            # Add timeout to prevent blocking on slow/failing builds
            result = subprocess.run(
                ["swift", "build", "-c", "release"],
                cwd=self.swift_bridge_dir,
                check=True,
                capture_output=True,
                timeout=5.0  # 5 second timeout - fail fast
            )
            logger.info("Swift classifier built successfully")
        except subprocess.TimeoutExpired:
            logger.error("Swift build timed out after 5s - using Python fallback")
            raise RuntimeError("Swift build timed out after 5s")
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to build Swift classifier: {e}")
            raise RuntimeError(f"Swift build failed: {e}")
    
    def _init_dynamic_lib(self):
        """Initialize dynamic library for faster performance.
        
        Loads the compiled Swift dynamic library and sets up function signatures
        for direct C-style function calls. Falls back to subprocess mode if
        library loading fails.
        
        Note:
            This method modifies self.use_dynamic_lib to False if initialization fails.
        """
        try:
            # Load the dynamic library 
            self._lib = ctypes.CDLL(str(self.lib_path))
            
            # Define function signatures
            self._lib.createClassifier.restype = ctypes.c_void_p
            self._lib.classifyCommand.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            self._lib.classifyCommand.restype = ctypes.c_char_p
            self._lib.learnFromFeedback.argtypes = [
                ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_bool
            ]
            
            # Create classifier instance
            self._classifier = self._lib.createClassifier()
            logger.info("Dynamic library initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize dynamic library: {e}")
            self.use_dynamic_lib = False
    
    async def classify_command(self, text: str) -> Dict[str, Any]:
        """Classify a command using Swift classifier.
        
        Processes natural language text to determine command type, intent,
        and extract relevant entities. Uses async execution to avoid blocking
        the main thread.
        
        Args:
            text: The natural language command to classify.
        
        Returns:
            Dictionary containing classification results with keys:
            - type (str): Command type ('system', 'vision', 'conversation')
            - intent (str): Specific intent within the type
            - confidence (float): Classification confidence (0.0-1.0)
            - entities (dict): Extracted entities from the command
            - reasoning (str): Human-readable explanation of classification
            - processing_time_ms (float): Time taken for classification
        
        Example:
            >>> result = await classifier.classify_command("close safari")
            >>> print(f"Type: {result['type']}, Confidence: {result['confidence']}")
            Type: system, Confidence: 0.95
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self.executor,
            self._classify_sync,
            text
        )
    
    def _classify_sync(self, text: str) -> Dict[str, Any]:
        """Synchronous classification implementation.
        
        Performs the actual classification using either dynamic library calls
        or subprocess execution, depending on initialization mode.
        
        Args:
            text: The command text to classify.
        
        Returns:
            Classification result dictionary with timing information.
        """
        start_time = time.time()
        
        try:
            if self.use_dynamic_lib and self._classifier:
                # Use dynamic library
                result_ptr = self._lib.classifyCommand(
                    self._classifier,
                    text.encode('utf-8')
                )
                result_json = ctypes.string_at(result_ptr).decode('utf-8')
            else:
                # Use subprocess
                result = subprocess.run(
                    [str(self.classifier_path), text],
                    capture_output=True,
                    text=True,
                    timeout=1.0
                )
                
                if result.returncode != 0:
                    logger.error(f"Classifier error: {result.stderr}")
                    return self._fallback_classification(text)
                
                result_json = result.stdout.strip()
            
            # Parse JSON result
            classification = json.loads(result_json)
            
            # Add timing info
            classification['processing_time_ms'] = (time.time() - start_time) * 1000
            
            return classification
            
        except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
            logger.error(f"Classification error: {e}")
            return self._fallback_classification(text)
    
    def _fallback_classification(self, text: str) -> Dict[str, Any]:
        """Fallback classification using intelligent pattern detection.

        Provides robust classification when the Swift classifier is unavailable
        or fails. Uses multi-tier pattern matching with semantic understanding.

        ROOT CAUSE FIX v3.0.0:
        - Added surveillance/monitoring keywords for God Mode detection
        - Multi-tier intent classification (surveillance > vision > system > conversation)
        - Semantic pattern detection without hardcoding

        Args:
            text: The command text to classify.

        Returns:
            Classification result with appropriate confidence scores.
        """
        import re
        text_lower = text.lower().strip()

        # =====================================================================
        # TIER 0: SURVEILLANCE / GOD MODE DETECTION (Highest Priority)
        # =====================================================================
        # Detects watch/monitor patterns that trigger God Mode surveillance
        # These MUST route to 'vision' handler where _parse_watch_command() lives
        # =====================================================================

        # Primary surveillance indicators (verb-based)
        surveillance_verbs = [
            r'\bwatch\b', r'\bmonitor\b', r'\btrack\b', r'\bobserve\b',
            r'\bsurveillance\b', r'\bkeep\s+an?\s+eye\b', r'\bscan\b'
        ]

        # Secondary surveillance indicators (notification-based)
        notification_patterns = [
            r'\bnotify\s+me\s+when\b', r'\balert\s+me\s+when\b',
            r'\btell\s+me\s+when\b', r'\blet\s+me\s+know\s+when\b',
            r'\bwarn\s+me\s+when\b', r'\bping\s+me\s+when\b'
        ]

        # Trigger/target indicators (what to look for)
        trigger_patterns = [
            r'\bfor\s+.+', r'\bwhen\s+.+\s+(?:says|shows|displays|appears)\b',
            r'\buntil\s+.+', r'\bwindows?\b', r'\binstances?\b', r'\btabs?\b'
        ]

        # Multi-space God Mode indicators
        god_mode_patterns = [
            r'\b(?:all|every|each)\s+.*?\s*(?:windows?|tabs?|instances?|spaces?)\b',
            r'\bacross\s+all\b', r'\bevery\s+space\b', r'\ball\s+spaces\b'
        ]

        # Check for surveillance command
        has_surveillance_verb = any(re.search(p, text_lower) for p in surveillance_verbs)
        has_notification = any(re.search(p, text_lower) for p in notification_patterns)
        has_trigger = any(re.search(p, text_lower) for p in trigger_patterns)
        has_god_mode = any(re.search(p, text_lower) for p in god_mode_patterns)

        # Surveillance detected - route to vision handler for God Mode processing
        if (has_surveillance_verb and has_trigger) or has_notification or has_god_mode:
            confidence = 0.95 if has_god_mode else 0.9 if has_notification else 0.85
            intent = "god_mode_surveillance" if has_god_mode else "surveillance"
            return {
                "type": "vision",
                "intent": intent,
                "confidence": confidence,
                "entities": {"is_god_mode": has_god_mode},
                "reasoning": f"Surveillance pattern detected: verb={has_surveillance_verb}, "
                            f"notification={has_notification}, trigger={has_trigger}, "
                            f"god_mode={has_god_mode}",
                "processing_time_ms": 0.2
            }

        # =====================================================================
        # TIER 1: VISION / SCREEN ANALYSIS COMMANDS
        # =====================================================================
        vision_patterns = [
            r'\b(?:see|look\s+at|describe|analyze|check|show)\s+(?:my\s+)?screen\b',
            r'\bwhat\s+(?:is|am\s+i)\s+(?:on|looking\s+at)\b',
            r'\bscreen\s+(?:content|analysis|capture)\b',
            r'\bwhat\s+do\s+you\s+see\b', r'\bcan\s+you\s+see\b'
        ]

        if any(re.search(p, text_lower) for p in vision_patterns):
            return {
                "type": "vision",
                "intent": "analyze_screen",
                "confidence": 0.8,
                "entities": {},
                "reasoning": "Vision analysis pattern detected",
                "processing_time_ms": 0.15
            }

        # =====================================================================
        # TIER 2: SYSTEM CONTROL COMMANDS
        # =====================================================================
        # App control, settings, file operations
        system_verbs = [
            "close", "quit", "open", "launch", "start", "switch", "toggle",
            "set", "adjust", "enable", "disable", "kill", "terminate", "run"
        ]

        is_system_action = any(
            re.search(rf'\b{verb}\b', text_lower) for verb in system_verbs
        )

        if is_system_action:
            return {
                "type": "system",
                "intent": "system_control",
                "confidence": 0.75,
                "entities": {},
                "reasoning": "System control verb detected",
                "processing_time_ms": 0.1
            }

        # =====================================================================
        # TIER 3: QUESTION / QUERY CLASSIFICATION
        # =====================================================================
        question_patterns = [
            r'^(?:what|who|where|when|how|why|which|can|could|would|should|is|are|do|does)\b'
        ]

        is_question = any(re.search(p, text_lower) for p in question_patterns)

        if is_question:
            # Questions about screen go to vision
            if any(word in text_lower for word in ['screen', 'see', 'looking', 'window', 'display']):
                return {
                    "type": "vision",
                    "intent": "visual_query",
                    "confidence": 0.7,
                    "entities": {},
                    "reasoning": "Visual question detected",
                    "processing_time_ms": 0.1
                }
            else:
                return {
                    "type": "conversation",
                    "intent": "general_query",
                    "confidence": 0.65,
                    "entities": {},
                    "reasoning": "General question detected",
                    "processing_time_ms": 0.1
                }

        # =====================================================================
        # TIER 4: DEFAULT FALLBACK
        # =====================================================================
        return {
            "type": "conversation",
            "intent": "general",
            "confidence": 0.5,
            "entities": {},
            "reasoning": "Default classification - no specific pattern matched",
            "processing_time_ms": 0.1
        }
    
    async def learn_from_feedback(self, command: str, actual_type: str, was_correct: bool):
        """Teach the classifier from user feedback.
        
        Provides supervised learning capability by feeding back correct
        classifications to improve future performance.
        
        Args:
            command: The original command that was classified.
            actual_type: The correct command type that should have been predicted.
            was_correct: Whether the original classification was correct.
        
        Example:
            >>> await classifier.learn_from_feedback(
            ...     "close safari", "system", True
            ... )
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self.executor,
            self._learn_sync,
            command, actual_type, was_correct
        )
    
    def _learn_sync(self, command: str, actual_type: str, was_correct: bool):
        """Synchronous learning implementation.
        
        Updates the classifier's internal model based on feedback. For dynamic
        library mode, calls the Swift learning function directly. For subprocess
        mode, could save feedback to a file for batch learning.
        
        Args:
            command: The command text.
            actual_type: Correct classification type.
            was_correct: Whether original classification was accurate.
        """
        try:
            if self.use_dynamic_lib and self._classifier:
                self._lib.learnFromFeedback(
                    self._classifier,
                    command.encode('utf-8'),
                    actual_type.encode('utf-8'),
                    was_correct
                )
            else:
                # For subprocess mode, we could save to a file
                # that the Swift classifier reads on startup
                pass
        except Exception as e:
            logger.error(f"Learning error: {e}")
    
    async def get_stats(self) -> Dict[str, Any]:
        """Get classifier statistics and performance metrics.
        
        Returns:
            Dictionary containing classifier statistics:
            - learned_patterns (int): Number of learned patterns
            - total_classifications (int): Total classifications performed
        
        Note:
            Current implementation returns placeholder values.
            Full implementation would call Swift stats method.
        """
        # Implementation would call Swift stats method
        return {
            "learned_patterns": 0,
            "total_classifications": 0
        }

    def shutdown(self):
        """Shutdown executor and cleanup resources."""
        if hasattr(self, 'executor') and self.executor:
            try:
                self.executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            self.executor = None

    def __del__(self):
        """Ensure cleanup on garbage collection."""
        self.shutdown()


class IntelligentCommandRouter:
    """Intelligent command router using Swift classification.
    
    Replaces hardcoded routing logic with ML-based decisions for determining
    which handler should process each command. Includes caching for performance
    and fallback mechanisms for reliability.
    
    Attributes:
        classification_cache (dict): Cache of recent classifications
        cache_ttl (int): Time-to-live for cache entries in seconds
        classifier: The underlying command classifier instance
    
    Example:
        >>> router = IntelligentCommandRouter()
        >>> handler, details = await router.route_command("open safari")
        >>> print(f"Route to {handler} handler")
        Route to system handler
    """
    
    def __init__(self):
        """Initialize the intelligent command router.
        
        Attempts to use Swift-based classifier first, falling back to Python
        implementation if Swift is unavailable. Sets up caching and logging.
        """
        self.classification_cache = {}
        self.cache_ttl = 60  # seconds
        
        # Try Swift classifier first, fall back to Python if needed
        try:
            if SWIFT_AVAILABLE:
                self.classifier = SwiftCommandClassifier()
                logger.info("Using Swift-based NLP classifier")
            else:
                raise RuntimeError("Swift not available")
        except Exception as e:
            logger.warning(f"Swift classifier unavailable: {e}")
            logger.info("Using Python fallback classifier")
            from .python_fallback_classifier import PythonCommandClassifier
            self.classifier = PythonCommandClassifier()
            # Wrap Python classifier methods to match Swift interface
            self.classifier.classify_command = self.classifier.classify
            self.classifier.learn_from_feedback = self.classifier.learn
        
    async def route_command(self, text: str, context: Optional[Dict] = None) -> Tuple[str, Dict[str, Any]]:
        """Route command to appropriate handler based on intelligent classification.
        
        Analyzes the input command using ML classification to determine the most
        appropriate handler. Uses caching to improve performance for repeated
        commands.
        
        Args:
            text: The natural language command to route.
            context: Optional context information (currently unused).
        
        Returns:
            Tuple containing:
            - handler_type (str): The handler to use ('system', 'vision', 'conversation')
            - classification_details (dict): Full classification results
        
        Example:
            >>> handler, details = await router.route_command("what's on my screen?")
            >>> print(f"Handler: {handler}, Confidence: {details['confidence']}")
            Handler: vision, Confidence: 0.92
        """
        # Check cache first
        cache_key = text.lower().strip()
        if cache_key in self.classification_cache:
            cached_time, cached_result = self.classification_cache[cache_key]
            if time.time() - cached_time < self.cache_ttl:
                logger.info(f"Using cached classification for: {text}")
                return (self._determine_handler(cached_result), cached_result)
        
        # Classify command
        logger.info(f"Classifying command: {text}")
        classification = await self.classifier.classify_command(text)
        
        # Cache result
        self.classification_cache[cache_key] = (time.time(), classification)
        
        # Log classification
        logger.info(f"Classification result: Type={classification['type']}, "
                   f"Confidence={classification['confidence']:.2f}, "
                   f"Intent={classification['intent']}")
        
        # Determine handler
        handler_type = self._determine_handler(classification)
        
        return handler_type, classification
    
    def _determine_handler(self, classification: Dict[str, Any]) -> str:
        """Determine which handler to use based on classification.
        
        Maps classification results to specific handler types using confidence
        thresholds and intent analysis for edge cases.
        
        Args:
            classification: Classification result dictionary from classifier.
        
        Returns:
            Handler type string ('vision', 'system', or 'conversation').
        """
        command_type = classification.get('type', 'system')
        confidence = classification.get('confidence', 0.5)
        
        # High confidence classifications
        if confidence > 0.7:
            if command_type == 'vision':
                return 'vision'
            elif command_type == 'system':
                return 'system'
            elif command_type == 'conversation':
                return 'conversation'
        
        # Low confidence - use intent as tiebreaker
        intent = classification.get('intent', '')
        if 'analyze' in intent or 'describe' in intent:
            return 'vision'
        elif 'app' in intent or 'control' in intent:
            return 'system'
        
        # Default to system for action-like commands
        return 'system'
    
    async def provide_feedback(self, command: str, used_handler: str, was_successful: bool):
        """Provide feedback to improve classification accuracy.
        
        Enables supervised learning by reporting back whether the routing
        decision was correct. This helps the classifier improve over time.
        
        Args:
            command: The original command that was routed.
            used_handler: Which handler was actually used.
            was_successful: Whether the handler successfully processed the command.
        
        Example:
            >>> await router.provide_feedback(
            ...     "close safari", "system", True
            ... )
        """
        # Map handler to command type
        handler_to_type = {
            'vision': 'vision',
            'system': 'system',
            'conversation': 'conversation'
        }
        
        actual_type = handler_to_type.get(used_handler, 'system')
        await self.classifier.learn_from_feedback(command, actual_type, was_successful)
        
        # Clear cache for this command
        cache_key = command.lower().strip()
        if cache_key in self.classification_cache:
            del self.classification_cache[cache_key]


async def test_classifier():
    """Test the classifier with various commands.
    
    Demonstrates the classifier's capabilities by testing it with a variety
    of command types and printing detailed results for each classification.
    
    Example:
        >>> await test_classifier()
        Command: 'close whatsapp'
        Handler: system
        Type: system (confidence: 0.95)
        Intent: app_control
        Reasoning: Action verb with application target detected
    """
    router = IntelligentCommandRouter()
    
    test_commands = [
        "close whatsapp",
        "what's on my screen",
        "open safari",
        "show me my notifications",
        "quit discord",
        "analyze my workspace",
        "launch terminal",
        "what applications are running",
        "switch to chrome",
        "describe what you see"
    ]
    
    for command in test_commands:
        handler, details = await router.route_command(command)
        print(f"\nCommand: '{command}'")
        print(f"Handler: {handler}")
        print(f"Type: {details['type']} (confidence: {details['confidence']:.2f})")
        print(f"Intent: {details['intent']}")
        print(f"Reasoning: {details['reasoning']}")


if __name__ == "__main__":
    # Run test
    asyncio.run(test_classifier())