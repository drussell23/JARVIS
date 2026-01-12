"""
Claude Vision-enabled Chatbot for JARVIS
Extends the basic Claude chatbot with vision capabilities for screen analysis
"""

import os

# Fix import path for vision modules
import sys

backend_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)

import asyncio
import base64
import hashlib
import io
import logging

# Platform and system detection
import platform
import re
import subprocess
import tempfile
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Union

from PIL import Image

logger = logging.getLogger(__name__)

# Check if required packages are available
try:
    import anthropic

    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False
    logger.warning("Anthropic package not installed. Install with: pip install anthropic")

try:
    import pyautogui

    SCREENSHOT_AVAILABLE = True
except (ImportError, AttributeError) as e:
    SCREENSHOT_AVAILABLE = False
    logger.warning(f"PyAutoGUI not available: {e}. Screenshot fallback disabled.")

try:
    from vision.claude_vision_analyzer_main import ClaudeVisionAnalyzer

    VISION_ANALYZER_AVAILABLE = True
except ImportError:
    VISION_ANALYZER_AVAILABLE = False
    logger.warning("Claude vision analyzer not available")


class ClaudeVisionChatbot:
    """
    Vision-enabled Claude chatbot that can analyze screen content
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "claude-3-5-sonnet-20241022",  # Vision-capable model
        max_tokens: int = 1024,
        temperature: float = 0.7,
        system_prompt: Optional[str] = None,
        vision_analyzer: Optional[Any] = None,  # Allow passing existing analyzer
        use_intelligent_selection: bool = True,  # Enable intelligent model selection
    ):
        """Initialize Claude vision chatbot with intelligent model selection"""
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError(
                "Anthropic API key required. Set ANTHROPIC_API_KEY environment variable "
                "or pass api_key parameter"
            )

        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.use_intelligent_selection = use_intelligent_selection

        # Dynamic JARVIS system prompt with real-time context
        self._initialize_dynamic_system_prompt(system_prompt)

        # Initialize client
        if ANTHROPIC_AVAILABLE:
            self.client = anthropic.Anthropic(api_key=self.api_key)
        else:
            self.client = None

        # Use provided vision analyzer or initialize new one
        if vision_analyzer:
            # Use the provided analyzer (e.g., from app.state)
            self.vision_analyzer = vision_analyzer
            logger.info("Using provided vision analyzer instance")
        elif VISION_ANALYZER_AVAILABLE and self.api_key:
            # Create new analyzer with real-time monitoring by default
            self.vision_analyzer = ClaudeVisionAnalyzer(self.api_key, enable_realtime=True)
            logger.info(
                "Initialized new Claude Vision Analyzer with real-time monitoring capabilities"
            )
        else:
            self.vision_analyzer = None
            logger.warning("Vision analyzer not available - real-time monitoring disabled")

        # Conversation history
        self.conversation_history: List[Dict[str, str]] = []
        self.max_history_length = 10

        # Monitoring state
        self._monitoring_active = False
        self._capture_method = "unknown"

        # Screenshot cache (cache for 5 seconds to handle repeated requests)
        self._screenshot_cache = None
        self._screenshot_cache_time = None
        self._screenshot_cache_duration = timedelta(seconds=5)

        # Dynamic vision detection configuration
        self._initialize_vision_detection_system()

        # Platform-specific configuration
        self._platform = platform.system().lower()
        self._capture_methods = self._initialize_capture_methods()

        # Dynamic cache configuration
        self._cache_config = self._get_cache_configuration()

    def is_vision_command(self, user_input: str) -> bool:
        """Enhanced vision command detection with intent analysis"""
        input_lower = user_input.lower().strip()

        # IMPORTANT: Exclude lock/unlock screen commands - these are system commands
        if "lock" in input_lower and "screen" in input_lower:
            return False
        if "unlock" in input_lower and "screen" in input_lower:
            return False

        # Quick keyword pre-check for performance
        if not any(keyword in input_lower for keyword in self._vision_keywords):
            return False

        # Check regex patterns for more accurate detection
        for pattern in self._compiled_vision_patterns:
            if pattern.search(input_lower):
                # Analyze intent for later use
                self._last_vision_intent = self._analyze_vision_intent(input_lower)
                return True

        # Fuzzy matching for typos and variations
        words = input_lower.split()
        for word in words:
            for keyword in self._vision_keywords:
                # Allow for typos (edit distance)
                if self._similar_words(word, keyword, threshold=0.8):
                    self._last_vision_intent = self._analyze_vision_intent(input_lower)
                    return True

        return False

    async def capture_screenshot(self) -> Optional[Image.Image]:
        """Fast async screenshot capture with intelligent method selection"""
        import asyncio
        from concurrent.futures import ThreadPoolExecutor

        # Skip cache for immediate response
        # Generate cache key based on intent
        cache_key = self._generate_cache_key()

        # Quick cache check (non-blocking)
        cached_screenshot = None
        try:
            cached_screenshot = await asyncio.wait_for(
                self._get_cached_screenshot(cache_key), timeout=0.1  # 100ms timeout for cache check
            )
            if cached_screenshot:
                logger.info(f"Using cached screenshot (key: {cache_key})")
                return cached_screenshot
        except asyncio.TimeoutError:
            pass  # Skip cache if it takes too long

        # Use the fastest method based on platform
        screenshot = None

        # For macOS, use the fastest native method
        if self._platform == "darwin":
            try:
                # Use screencapture command directly (fastest on macOS)
                screenshot = await self._capture_screencapture_cmd_fast()
                if screenshot:
                    logger.info(f"Fast screenshot captured: {screenshot.size}")
                    # Cache asynchronously (don't wait)
                    asyncio.create_task(self._cache_screenshot(cache_key, screenshot))
                    return screenshot
            except Exception as e:
                logger.debug(f"Fast capture failed, falling back: {e}")

        # Fallback to PyAutoGUI if available (cross-platform)
        if SCREENSHOT_AVAILABLE:
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    screenshot = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(executor, pyautogui.screenshot),
                        timeout=2.0,  # 2 second timeout
                    )
                    if screenshot:
                        logger.info(f"PyAutoGUI screenshot captured: {screenshot.size}")
                        # Cache asynchronously
                        asyncio.create_task(self._cache_screenshot(cache_key, screenshot))
                        return screenshot
            except Exception as e:
                logger.warning(f"PyAutoGUI capture failed: {e}")

        # Last resort: try all methods with timeout
        for method_info in self._capture_methods[:2]:  # Try only first 2 methods
            try:
                screenshot = await asyncio.wait_for(
                    method_info["function"](), timeout=1.0  # 1 second per method
                )
                if screenshot and isinstance(screenshot, Image.Image):
                    logger.info(f"Screenshot captured using {method_info['name']}")
                    asyncio.create_task(self._cache_screenshot(cache_key, screenshot))
                    return screenshot
            except Exception:
                continue

        logger.error("All screenshot capture methods failed")
        return None

        # Post-process screenshot based on intent
        if hasattr(self, "_last_vision_intent"):
            screenshot = await self._optimize_screenshot(screenshot, self._last_vision_intent)

        # Cache the screenshot
        await self._cache_screenshot(cache_key, screenshot, used_method)

        return screenshot

    async def analyze_screen_with_vision(self, user_input: str) -> str:
        """Enhanced screen analysis with dynamic optimization"""
        try:
            total_start = datetime.now()

            # Use vision analyzer if available for multi-space support
            if self.vision_analyzer and hasattr(self.vision_analyzer, "smart_analyze"):
                try:
                    # Capture screenshot
                    capture_start = datetime.now()
                    screenshot = await self.capture_screenshot()
                    capture_time = (datetime.now() - capture_start).total_seconds()
                    logger.info(f"Screenshot capture took {capture_time:.2f}s")

                    if not screenshot:
                        return "I apologize, sir, but I'm unable to capture your screen at the moment. Please ensure screen recording permissions are enabled in System Preferences > Security & Privacy > Privacy > Screen Recording."

                    # Convert PIL Image to numpy array for vision analyzer
                    import numpy as np

                    screenshot_np = np.array(screenshot)

                    # Use smart_analyze for multi-space aware analysis
                    analysis_start = datetime.now()
                    result = await self.vision_analyzer.smart_analyze(screenshot_np, user_input)
                    analysis_time = (datetime.now() - analysis_start).total_seconds()
                    logger.info(f"Vision analyzer smart_analyze took {analysis_time:.2f}s")

                    # Extract the response
                    if isinstance(result, dict) and "content" in result:
                        ai_response = result["content"]
                    elif isinstance(result, str):
                        ai_response = result
                    else:
                        ai_response = str(result)

                    # Update conversation history
                    self._update_history(user_input, ai_response)

                    total_time = (datetime.now() - total_start).total_seconds()
                    logger.info(f"Total vision processing took {total_time:.2f}s")

                    return ai_response

                except Exception as analyzer_error:
                    logger.error(
                        f"Vision analyzer error: {analyzer_error}, falling back to direct API"
                    )
                    # Fall through to original implementation

            # Intelligent model selection path
            if self.use_intelligent_selection:
                try:
                    logger.info("Using intelligent model selection for vision request")

                    # Capture screenshot
                    capture_start = datetime.now()
                    screenshot = await self.capture_screenshot()
                    capture_time = (datetime.now() - capture_start).total_seconds()
                    logger.info(f"Screenshot capture took {capture_time:.2f}s")

                    if not screenshot:
                        return (
                            "I apologize, sir, but I'm unable to capture your screen at the moment."
                        )

                    # Prepare screenshot
                    screenshot = await self._prepare_screenshot_for_api(screenshot, user_input)

                    # Convert to base64
                    buffer = io.BytesIO()
                    if screenshot.mode == "RGBA":
                        rgb_image = Image.new("RGB", screenshot.size, (255, 255, 255))
                        rgb_image.paste(screenshot, mask=screenshot.split()[3])
                        screenshot = rgb_image
                    format_config = self._get_image_format_config(user_input)
                    screenshot.save(
                        buffer,
                        format=format_config["format"],
                        quality=format_config["quality"],
                        optimize=format_config["optimize"],
                    )
                    image_base64 = base64.b64encode(buffer.getvalue()).decode()

                    # Build messages with vision
                    messages = self._build_messages_with_vision(user_input, image_base64)

                    # Use intelligent selection
                    response = await self._generate_with_intelligent_selection(
                        messages=messages, user_input=user_input, is_vision=True
                    )

                    if response:
                        return response
                    else:
                        logger.warning("Intelligent selection returned None, falling back")

                except Exception as e:
                    logger.error(f"Intelligent selection failed: {e}, falling back to direct API")

            # Original implementation (fallback)
            # Capture screenshot
            capture_start = datetime.now()
            screenshot = await self.capture_screenshot()
            capture_time = (datetime.now() - capture_start).total_seconds()
            logger.info(f"Screenshot capture took {capture_time:.2f}s")

            if not screenshot:
                return "I apologize, sir, but I'm unable to capture your screen at the moment. Please ensure screen recording permissions are enabled in System Preferences > Security & Privacy > Privacy > Screen Recording."

            # Dynamic image optimization based on intent
            encode_start = datetime.now()
            screenshot = await self._prepare_screenshot_for_api(screenshot, user_input)

            # Convert to base64 for Claude API with optimization
            buffer = io.BytesIO()
            # Convert RGBA to RGB if needed for JPEG
            if screenshot.mode == "RGBA":
                # Create a white background
                rgb_image = Image.new("RGB", screenshot.size, (255, 255, 255))
                rgb_image.paste(screenshot, mask=screenshot.split()[3])  # Use alpha channel as mask
                screenshot = rgb_image
            # Dynamic format and quality based on intent
            format_config = self._get_image_format_config(user_input)
            screenshot.save(
                buffer,
                format=format_config["format"],
                quality=format_config["quality"],
                optimize=format_config["optimize"],
            )
            image_base64 = base64.b64encode(buffer.getvalue()).decode()
            encode_time = (datetime.now() - encode_start).total_seconds()
            logger.info(
                f"Image encoding took {encode_time:.2f}s (format: {format_config['format']}, quality: {format_config['quality']})"
            )

            # Build the vision message
            messages = self._build_messages_with_vision(user_input, image_base64)

            # Make API call with vision
            api_start = datetime.now()

            # Use a faster model for quick vision checks if available
            self.model
            if "can you see" in user_input.lower() and "claude-3-haiku" in self.model:
                # For simple "can you see" queries, we can use the same model
                pass

            # Dynamic prompt generation based on query analysis
            system_prompt = self._generate_vision_system_prompt(user_input)

            # Dynamic API configuration based on intent
            api_config = self._get_vision_api_config(user_input)

            response = await asyncio.to_thread(
                self.client.messages.create,
                model=api_config["model"],
                max_tokens=api_config["max_tokens"],
                temperature=api_config["temperature"],
                system=system_prompt,
                messages=messages,
            )

            # Extract response
            ai_response = response.content[0].text

            # Log performance
            api_time = (datetime.now() - api_start).total_seconds()
            total_time = (datetime.now() - total_start).total_seconds()
            logger.info(f"Claude API call took {api_time:.2f}s")
            logger.info(f"Total vision processing took {total_time:.2f}s")

            # Update conversation history
            self._update_history(user_input, ai_response)

            return ai_response

        except anthropic.APIError as e:
            logger.error(f"Claude API error: {e}")
            return f"I encountered an API error while analyzing your screen: {str(e)}"
        except Exception as e:
            logger.error(f"Error in screen analysis: {e}")
            return f"I apologize, sir, but I encountered an error analyzing your screen: {str(e)}"

    async def _generate_with_intelligent_selection(
        self, user_input: str, screenshot_base64: Optional[str] = None, is_vision: bool = False
    ) -> str:
        """
        Generate response using intelligent model selection with vision support

        This method:
        1. Imports the hybrid orchestrator
        2. Builds context from conversation history
        3. Calls execute_with_intelligent_model_selection() with vision capabilities
        4. Returns the selected model's response

        Args:
            user_input: User's query
            screenshot_base64: Optional base64-encoded screenshot for vision tasks
            is_vision: Whether this is a vision analysis task

        Returns:
            AI response from intelligently selected model
        """
        try:
            from backend.core.hybrid_orchestrator import HybridOrchestrator

            # Get or create orchestrator
            orchestrator = HybridOrchestrator()
            if not orchestrator.is_running:
                await orchestrator.start()

            # Build context from conversation history
            context = {
                "conversation_history": self.conversation_history[-3:],  # Last 3 exchanges
                "user_focus": "vision_analysis" if is_vision else "casual",
                "system_prompt": self.system_prompt,
                "has_image": screenshot_base64 is not None,
            }

            # Add screenshot to context if available
            if screenshot_base64:
                context["image_data"] = screenshot_base64
                context["image_format"] = "base64"

            # Build full prompt with system instructions
            full_prompt = f"{self.system_prompt}\n\nUser: {user_input}\n\nAssistant:"

            # Determine intent and required capabilities based on task type
            if is_vision:
                intent = "vision_analysis"
                required_capabilities = {"vision", "vision_analyze_heavy", "multimodal"}
            else:
                intent = "conversational_ai"
                required_capabilities = {"conversational_ai", "chatbot_inference"}

            # Execute with intelligent model selection
            start_time = datetime.now()
            result = await orchestrator.execute_with_intelligent_model_selection(
                query=full_prompt,
                intent=intent,
                required_capabilities=required_capabilities,
                context=context,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
            )

            if not result.get("success"):
                raise Exception(result.get("error", "Unknown error"))

            # Extract response
            ai_response = result.get("text", "").strip()

            if not ai_response:
                raise Exception("Empty response from intelligent model selection")

            # Log which model was used
            model_used = result.get("model_used", "unknown")
            response_time = (datetime.now() - start_time).total_seconds()
            logger.info(
                f"Vision chatbot response generated in {response_time:.2f}s using {model_used}"
            )

            return ai_response

        except ImportError:
            logger.warning("Hybrid orchestrator not available, using direct Claude API")
            raise
        except Exception as e:
            logger.error(f"Error in intelligent model selection: {e}")
            raise

    def _initialize_dynamic_system_prompt(self, custom_prompt: Optional[str] = None):
        """Initialize system prompt with dynamic context"""
        # Get dynamic time context
        current_datetime = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
        timezone = self._get_timezone_name()

        # Build dynamic prompt components
        base_components = [
            "You are JARVIS, an intelligent AI assistant inspired by Tony Stark's AI from Iron Man.",
            "You have advanced vision capabilities and can see and analyze the user's screen when asked.",
            "You are helpful, witty, and highly capable.",
            "You speak with a refined, professional tone while being personable and occasionally adding subtle humor.",
            "When analyzing screens, you provide detailed, accurate descriptions and helpful insights about what you observe.",
            "You excel at understanding context and providing insightful, well-structured responses.",
        ]

        # Add dynamic context
        context_components = [
            f"Current date and time: {current_datetime}",
            f"Timezone: {timezone}" if timezone else None,
            f"Platform: {platform.system()} {platform.release()}",
        ]

        # Filter out None values and join
        all_components = base_components + [c for c in context_components if c]
        self.system_prompt = custom_prompt or " ".join(all_components)

    def _initialize_vision_detection_system(self):
        """Initialize dynamic vision pattern detection"""
        # Core vision keywords for quick checks
        self._vision_keywords = {
            "screen",
            "see",
            "look",
            "view",
            "show",
            "display",
            "vision",
            "visual",
            "analyze",
            "examine",
            "check",
            "monitor",
            "desktop",
            "window",
            "describe",
            "watch",
            "observe",
            "inspect",
        }

        # Build regex patterns dynamically
        self._compiled_vision_patterns = self._compile_vision_patterns()

    def _compile_vision_patterns(self) -> List[re.Pattern]:
        """Compile vision detection patterns dynamically"""
        patterns = []

        # Action + target patterns
        action_words = ["see", "look", "view", "show", "analyze", "check", "examine", "describe"]
        target_words = ["screen", "display", "monitor", "desktop", "window"]

        for action in action_words:
            for target in target_words:
                patterns.extend(
                    [
                        rf"\b{action}\s+(?:my\s+|the\s+)?{target}\b",
                        rf"\bcan\s+you\s+{action}\s+(?:my\s+|the\s+)?{target}\b",
                        rf"\b{action}\s+what(?:\'s|\s+is)\s+on\s+(?:my\s+|the\s+)?{target}\b",
                    ]
                )

        # Question patterns
        patterns.extend(
            [
                r"\bwhat\s+do\s+you\s+see\b",
                r"\bwhat(?:\'s|\s+is)\s+on\s+(?:my\s+)?screen\b",
                r"\btell\s+me\s+what\s+you\s+see\b",
                r"\banalyze\s+this\b",
                r"\blook\s+at\s+this\b",
                r"\bscreen\s*shot\b",
                r"\bvisual\s+analysis\b",
            ]
        )

        # Compile all patterns with case-insensitive flag
        return [re.compile(p, re.IGNORECASE) for p in patterns]

    def _initialize_capture_methods(self) -> List[Dict[str, Any]]:
        """Initialize platform-specific capture methods"""
        methods = []

        # PyAutoGUI (cross-platform)
        if SCREENSHOT_AVAILABLE:
            methods.append(
                {"name": "pyautogui", "function": self._capture_pyautogui, "priority": 1}
            )

        # Platform-specific methods
        if self._platform == "darwin":  # macOS
            methods.extend(
                [
                    {"name": "native_macos", "function": self._capture_native_macos, "priority": 2},
                    {
                        "name": "screencapture_cmd",
                        "function": self._capture_screencapture_cmd,
                        "priority": 3,
                    },
                ]
            )
        elif self._platform == "win32":  # Windows
            methods.append(
                {"name": "windows_capture", "function": self._capture_windows, "priority": 2}
            )
        elif "linux" in self._platform:  # Linux
            methods.append(
                {"name": "linux_capture", "function": self._capture_linux, "priority": 2}
            )

        # Vision analyzer as fallback
        if self.vision_analyzer:
            methods.append(
                {
                    "name": "vision_analyzer",
                    "function": self._capture_vision_analyzer,
                    "priority": 99,
                }
            )

        # Sort by priority
        return sorted(methods, key=lambda x: x["priority"])

    def _get_cache_configuration(self) -> Dict[str, Any]:
        """Get dynamic cache configuration"""
        return {
            "duration": timedelta(seconds=5),
            "max_size": 10,
            "strategy": "lru",  # Least Recently Used
            "hash_method": "md5",
        }

    def _analyze_vision_intent(self, text: str) -> Dict[str, Any]:
        """Analyze the intent behind a vision query"""
        intent = {
            "urgency": "normal",
            "detail_level": "standard",
            "focus_areas": [],
            "query_type": "general",
            "requires_interaction": False,
        }

        # Detect urgency
        if any(word in text for word in ["urgent", "quick", "fast", "immediately", "asap"]):
            intent["urgency"] = "high"

        # Detect detail level
        if any(
            word in text for word in ["detail", "comprehensive", "thorough", "everything", "full"]
        ):
            intent["detail_level"] = "high"
        elif any(word in text for word in ["brief", "summary", "quick", "glance"]):
            intent["detail_level"] = "low"

        # Detect focus areas
        if any(word in text for word in ["error", "bug", "issue", "problem"]):
            intent["focus_areas"].append("errors")
        if any(word in text for word in ["code", "program", "script"]):
            intent["focus_areas"].append("code")
        if any(word in text for word in ["text", "document", "content"]):
            intent["focus_areas"].append("text")

        # Detect query type
        if "can you see" in text.lower():
            intent["query_type"] = "confirmation"
        elif any(word in text for word in ["analyze", "examine"]):
            intent["query_type"] = "analysis"

        return intent

    def _similar_words(self, word1: str, word2: str, threshold: float = 0.8) -> bool:
        """Check if two words are similar (for typo detection)"""
        # Simple similarity check - can be enhanced with edit distance
        if word1 == word2:
            return True

        # Check if one is substring of other
        if len(word1) > 3 and len(word2) > 3:
            if word1 in word2 or word2 in word1:
                return True

        # Basic edit distance approximation
        if abs(len(word1) - len(word2)) > 2:
            return False

        matches = sum(1 for a, b in zip(word1, word2) if a == b)
        similarity = matches / max(len(word1), len(word2))

        return similarity >= threshold

    async def _capture_pyautogui(self) -> Optional[Image.Image]:
        """Capture using PyAutoGUI"""
        if not SCREENSHOT_AVAILABLE:
            return None
        try:
            return await asyncio.to_thread(pyautogui.screenshot)
        except Exception as e:
            raise Exception(f"PyAutoGUI capture failed: {e}")

    async def _capture_native_macos(self) -> Optional[Image.Image]:
        """Capture using native macOS methods"""
        try:
            from vision.screen_capture_module import capture_screen_native

            screenshot_path = capture_screen_native()
            if screenshot_path:
                return Image.open(screenshot_path)
        except Exception:
            pass
        return None

    async def _capture_screencapture_cmd_fast(self) -> Optional[Image.Image]:
        """Ultra-fast screenshot capture for macOS using screencapture"""
        import asyncio
        import os

        try:
            # Use in-memory approach for speed
            tmp_path = f"/tmp/jarvis_screen_{os.getpid()}.png"

            # Run screencapture with minimal options for speed
            cmd = ["screencapture", "-x", "-t", "png", tmp_path]

            # Use asyncio subprocess for true async
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )

            # Wait with timeout
            try:
                await asyncio.wait_for(process.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                process.kill()
                return None

            if process.returncode == 0 and os.path.exists(tmp_path):
                # Load image and immediately delete temp file
                img = Image.open(tmp_path)
                # Convert to RGB to ensure compatibility
                if img.mode == "RGBA":
                    img = img.convert("RGB")
                # Make a copy to ensure file can be deleted
                img_copy = img.copy()
                img.close()
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
                return img_copy
        except Exception as e:
            logger.debug(f"Fast screencapture failed: {e}")
        return None

    async def _capture_screencapture_cmd(self) -> Optional[Image.Image]:
        """Capture using macOS screencapture command (fallback method)"""
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                cmd = ["screencapture", "-x"]
                # Add cursor if needed based on intent
                if hasattr(self, "_last_vision_intent") and self._last_vision_intent.get(
                    "show_cursor"
                ):
                    cmd.append("-C")
                cmd.append(tmp.name)

                result = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                await result.communicate()

                if result.returncode == 0:
                    return Image.open(tmp.name)
        except Exception:
            pass
        return None

    async def _capture_windows(self) -> Optional[Image.Image]:
        """Capture on Windows"""
        try:
            from PIL import ImageGrab

            return await asyncio.to_thread(ImageGrab.grab)
        except Exception:
            return None

    async def _capture_linux(self) -> Optional[Image.Image]:
        """Capture on Linux"""
        commands = [["gnome-screenshot", "-f"], ["scrot"], ["import", "-window", "root"]]

        for cmd_template in commands:
            try:
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    cmd = cmd_template + [tmp.name]
                    result = await asyncio.create_subprocess_exec(
                        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                    )
                    await result.communicate()

                    if result.returncode == 0:
                        return Image.open(tmp.name)
            except Exception:
                continue
        return None

    async def _capture_vision_analyzer(self) -> Optional[Image.Image]:
        """Capture using vision analyzer"""
        if self.vision_analyzer and hasattr(self.vision_analyzer, "capture_screen"):
            return await self.vision_analyzer.capture_screen()
        return None

    def _generate_cache_key(self) -> str:
        """Generate intelligent cache key"""
        components = [
            str(datetime.now().timestamp()),
            getattr(self, "_last_vision_intent", {}).get("query_type", "general"),
        ]

        key_string = "_".join(components)
        return hashlib.md5(key_string.encode()).hexdigest()[:16]

    async def _get_cached_screenshot(self, cache_key: str) -> Optional[Image.Image]:
        """Get screenshot from intelligent cache"""
        if not hasattr(self, "_screenshot_cache_store"):
            self._screenshot_cache_store = {}

        cached = self._screenshot_cache_store.get(cache_key)
        if cached:
            timestamp, screenshot, metadata = cached
            cache_duration = self._cache_config["duration"]

            # Check if cache is still valid
            if datetime.now() - timestamp < cache_duration:
                # Extend cache for frequently accessed items
                if metadata.get("access_count", 0) > 3:
                    cache_duration *= 2

                metadata["access_count"] = metadata.get("access_count", 0) + 1
                return screenshot

        return None

    async def _cache_screenshot(
        self, cache_key: str, screenshot: Image.Image, method: str = "unknown"
    ):
        """Cache screenshot with metadata"""
        if not hasattr(self, "_screenshot_cache_store"):
            self._screenshot_cache_store = {}

        # Implement LRU cache
        max_size = self._cache_config.get("max_size", 10)
        if len(self._screenshot_cache_store) >= max_size:
            # Remove least recently used
            oldest_key = min(
                self._screenshot_cache_store.keys(),
                key=lambda k: self._screenshot_cache_store[k][2].get("last_access", datetime.min),
            )
            del self._screenshot_cache_store[oldest_key]

        self._screenshot_cache_store[cache_key] = (
            datetime.now(),
            screenshot,
            {"method": method, "access_count": 0, "last_access": datetime.now()},
        )

    async def _optimize_screenshot(
        self, screenshot: Image.Image, intent: Dict[str, Any]
    ) -> Image.Image:
        """Optimize screenshot based on intent analysis"""
        # Dynamic resizing based on detail level
        if intent.get("detail_level") == "low":
            max_dimension = 1280
        elif intent.get("detail_level") == "high":
            max_dimension = 2560
        else:
            max_dimension = 1920

        # Resize if needed
        if screenshot.width > max_dimension or screenshot.height > max_dimension:
            ratio = min(max_dimension / screenshot.width, max_dimension / screenshot.height)
            new_size = (int(screenshot.width * ratio), int(screenshot.height * ratio))
            screenshot = screenshot.resize(new_size, Image.Resampling.LANCZOS)
            logger.info(f"Optimized screenshot to {new_size} based on intent")

        return screenshot

    async def _prepare_screenshot_for_api(
        self, screenshot: Image.Image, user_input: str
    ) -> Image.Image:
        """Prepare screenshot for API submission"""
        intent = getattr(self, "_last_vision_intent", {})

        # Apply optimizations
        screenshot = await self._optimize_screenshot(screenshot, intent)

        # Convert RGBA to RGB if needed
        if screenshot.mode == "RGBA":
            rgb_image = Image.new("RGB", screenshot.size, (255, 255, 255))
            rgb_image.paste(screenshot, mask=screenshot.split()[3])
            screenshot = rgb_image

        return screenshot

    def _get_image_format_config(self, user_input: str) -> Dict[str, Any]:
        """Get dynamic image format configuration"""
        intent = getattr(self, "_last_vision_intent", {})

        # High quality for detailed analysis
        if intent.get("detail_level") == "high":
            return {"format": "PNG", "quality": 95, "optimize": True}
        # Fast processing for quick checks
        elif intent.get("urgency") == "high" or intent.get("query_type") == "confirmation":
            return {"format": "JPEG", "quality": 70, "optimize": True}
        # Standard balanced approach
        else:
            return {"format": "JPEG", "quality": 85, "optimize": True}

    def _generate_vision_system_prompt(self, user_input: str) -> str:
        """Generate dynamic system prompt for vision queries"""
        current_datetime = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
        user_lower = user_input.lower()

        # Base JARVIS prompt
        base = (
            "You are JARVIS, Tony Stark's AI assistant with advanced vision capabilities. "
            f"Current date and time: {current_datetime}. "
            "When analyzing screens, be specific and precise - read actual values, not generic descriptions. "
        )

        # Add specific instructions based on query type
        if any(word in user_lower for word in ["battery", "power", "charge"]):
            specific = (
                "The user is asking about battery status. "
                "Always report the EXACT percentage number visible on screen. "
                "Never give vague responses like 'the battery appears to be...' - give specific values."
            )
        elif any(word in user_lower for word in ["time", "clock"]):
            specific = (
                "The user is asking about the time. "
                "Report the EXACT time shown on the screen's clock. "
                "Be precise with the format (e.g., '2:34 PM' or '14:34')."
            )
        elif "status bar" in user_lower or "menu bar" in user_lower:
            specific = (
                "The user wants to know about the status/menu bar. "
                "List every visible element with its specific value or state. "
                "Include time, battery percentage, network status, and all visible icons."
            )
        else:
            specific = (
                "Provide specific, actionable information about what you see. "
                "Include exact values for UI elements like time, battery, notifications. "
                "Name specific applications and describe actual content, not generic observations."
            )

        # Combine components
        return base + specific

    def _get_vision_api_config(self, user_input: str) -> Dict[str, Any]:
        """Get dynamic API configuration for vision requests"""
        intent = getattr(self, "_last_vision_intent", {})

        # Model selection based on requirements
        if intent.get("urgency") == "high" and "haiku" in self.model.lower():
            model = self.model  # Use faster model if available
        else:
            model = self.model

        # Token limits based on detail level
        if intent.get("detail_level") == "high":
            max_tokens = 2048
        elif intent.get("query_type") == "confirmation":
            max_tokens = 256
        else:
            max_tokens = self.max_tokens

        # Temperature based on task type
        if intent.get("query_type") == "analysis":
            temperature = 0.3  # More focused
        else:
            temperature = self.temperature

        return {"model": model, "max_tokens": max_tokens, "temperature": temperature}

    def _get_timezone_name(self) -> Optional[str]:
        """Get system timezone name"""
        try:
            if self._platform == "darwin":
                result = subprocess.run(
                    ["systemsetup", "-gettimezone"], capture_output=True, text=True
                )
                if result.returncode == 0:
                    output = result.stdout.strip()
                    if "Time Zone:" in output:
                        return output.split("Time Zone:")[1].strip()
            elif os.path.exists("/etc/timezone"):
                with open("/etc/timezone", "r") as f:
                    return f.read().strip()
        except Exception:
            pass
        return None

    def _build_messages_with_vision(
        self, user_input: str, image_base64: str
    ) -> List[Dict[str, Any]]:
        """Build message list for Claude API including vision content"""
        messages = []

        # Add conversation history (last 3 exchanges for context)
        for entry in self.conversation_history[-3:]:
            messages.append({"role": "user", "content": entry["user"]})
            messages.append({"role": "assistant", "content": entry["assistant"]})

        # Enhance the prompt based on what the user is asking
        enhanced_prompt = self._enhance_vision_prompt(user_input)

        # Add current user input with image
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": image_base64,
                        },
                    },
                    {"type": "text", "text": enhanced_prompt},
                ],
            }
        )

        return messages

    def _enhance_vision_prompt(self, user_input: str) -> str:
        """Enhance prompt for specific UI element detection using Claude's pure vision intelligence"""
        user_lower = user_input.lower()

        # Advanced vision intelligence framework with metacognitive awareness
        vision_intelligence = (
            "🧠 METACOGNITIVE VISION INTELLIGENCE:\n\n"
            "AWARENESS LEVELS:\n"
            "- What I KNOW (directly observable): 'I can clearly see...'\n"
            "- What I INFER (context-based): 'Based on context, this appears to be...'\n"
            "- What I CANNOT DETERMINE: 'I cannot determine... because...'\n\n"
            "CONFIDENCE EXPRESSION:\n"
            "- CERTAIN (95-100%): 'I can clearly see...'\n"
            "- PROBABLE (70-95%): 'This appears to be...'\n"
            "- POSSIBLE (40-70%): 'This might be...'\n"
            "- UNCERTAIN (<40%): 'I'm uncertain, but...'\n\n"
            "INTERFACE UNIVERSALITY:\n"
            "- Standard GUIs, games, terminals, CAD, music software, scientific tools\n"
            "- ASCII art, console UIs, embedded systems, custom applications\n"
            "- Analog displays: gauges, knobs, sliders, waveforms\n"
            "- Abstract visualizations: graphs, diagrams, 3D renders\n\n"
            "CONTEXTUAL INTERPRETATION:\n"
            "- Same visual element = different meanings in different contexts\n"
            "- Use application type, surrounding elements, and user intent\n"
            "- Consider cultural and domain-specific conventions\n\n"
            "PRIVACY & ETHICS:\n"
            "- Never attempt to read password fields or intentionally hidden content\n"
            "- Acknowledge sensitive content without revealing it\n"
            "- Respect redaction, blurring, and privacy measures\n\n"
        )

        # Check for battery queries
        if any(word in user_lower for word in ["battery", "power", "charge", "charging"]):
            return (
                f"{user_input}\n\n{vision_intelligence}"
                "SPECIFIC TASK: Report the SYSTEM battery level.\n"
                "- Look in the system status area (top-right macOS, bottom-right Windows)\n"
                "- If you see multiple battery indicators, identify which is the actual system battery\n"
                "- Report format: 'Your battery is at X%' or 'I see multiple battery indicators...'\n"
                "- If battery is hidden or obscured, say so explicitly"
            )

        # Check for time queries
        elif any(word in user_lower for word in ["time", "clock", "hour"]):
            return (
                f"{user_input}\n\n{vision_intelligence}"
                "SPECIFIC TASK: Report the current SYSTEM time.\n"
                "- Find the system clock (not times in videos, code, or screenshots)\n"
                "- If multiple times are visible, use visual cues to identify the system time\n"
                "- Report format: 'The time is X:XX PM'\n"
                "- Express confidence if there's ambiguity"
            )

        # Check for status bar queries
        elif "status bar" in user_lower or "menu bar" in user_lower:
            return (
                f"{user_input}\n\n{vision_intelligence}"
                "SPECIFIC TASK: Analyze the system status/menu bar.\n"
                "- Focus on the actual system bar, not app toolbars\n"
                "- Report all visible elements with exact values\n"
                "- Note which parts might be hidden by other windows\n"
                "- List from left to right or by importance"
            )

        # Check for app-specific queries
        app_keywords = [
            "slack",
            "discord",
            "chrome",
            "safari",
            "vscode",
            "terminal",
            "finder",
            "mail",
        ]
        mentioned_app = None
        for app in app_keywords:
            if app in user_lower:
                mentioned_app = app
                break

        if mentioned_app:
            return (
                f"{user_input}\n\n{vision_intelligence}"
                f"SPECIFIC TASK: User is asking about {mentioned_app.title()}.\n"
                f"- Determine if {mentioned_app.title()} is visible, partially visible, or not visible\n"
                f"- If not visible, explain why (minimized, different desktop, not running)\n"
                f"- Don't assume or hallucinate its presence"
            )

        # Check for general screen queries
        elif any(
            phrase in user_lower
            for phrase in ["what do you see", "what's on", "can you see", "describe"]
        ):
            return (
                f"{user_input}\n\n{vision_intelligence}"
                "COMPREHENSIVE SCREEN ANALYSIS:\n"
                "1) Identify all visible windows and their layering order\n"
                "2) Note which window appears to be active/in focus\n"
                "3) Report system UI elements with exact values\n"
                "4) Describe spatial relationships and partial occlusions\n"
                "5) Mention any dynamic content or loading states\n"
                "6) If asked about something specific that's not visible, say so"
            )

        # Multi-monitor awareness
        if any(
            phrase in user_lower
            for phrase in ["other monitor", "second screen", "external monitor"]
        ):
            return (
                f"{user_input}\n\n{vision_intelligence}"
                "NOTE: User may be asking about a different monitor.\n"
                "- You can only see what's in this screenshot\n"
                "- If they need another monitor analyzed, suggest they specify which screen"
            )

        # Check for ambiguous/vague queries
        elif any(word in user_lower for word in ["that", "this", "thing", "stuff", "it"]):
            return (
                f"{user_input}\n\n{vision_intelligence}"
                "AMBIGUOUS REFERENCE HANDLING:\n"
                "- The user's query is vague. List all possible interpretations\n"
                "- For each possibility, explain what it is and where it's located\n"
                "- Ask clarifying questions: 'Are you referring to...?'\n"
                "- Provide helpful context for disambiguation"
            )

        # Data visualization queries
        elif any(
            word in user_lower
            for word in ["graph", "chart", "trend", "data", "visualization", "plot"]
        ):
            return (
                f"{user_input}\n\n{vision_intelligence}"
                "DATA VISUALIZATION INTERPRETATION:\n"
                "- Don't just describe the visual - interpret the data\n"
                "- Identify: type of viz, axes, trends, patterns, outliers\n"
                "- Explain what story the data is telling\n"
                "- Note any unclear or ambiguous aspects"
            )

        # Non-standard interfaces
        elif any(
            word in user_lower
            for word in ["game", "terminal", "console", "ascii", "music", "daw", "synth"]
        ):
            return (
                f"{user_input}\n\n{vision_intelligence}"
                "NON-STANDARD INTERFACE ANALYSIS:\n"
                "- Recognize this may not be a traditional GUI\n"
                "- For games: understand HUD elements, game state, resources\n"
                "- For terminals: parse ASCII art, command outputs, TUIs\n"
                "- For music software: read knobs, faders, waveforms\n"
                "- Apply domain-specific knowledge appropriately"
            )

        # Privacy-sensitive queries
        elif any(
            word in user_lower for word in ["password", "login", "credential", "secret", "private"]
        ):
            return (
                f"{user_input}\n\n{vision_intelligence}"
                "PRIVACY-SENSITIVE CONTENT:\n"
                "- Acknowledge password fields without attempting to read them\n"
                "- Note if content is intentionally obscured or redacted\n"
                "- Provide helpful information while respecting privacy\n"
                "- Suggest secure alternatives if user needs help"
            )

        # Functional/intent queries
        elif any(
            phrase in user_lower
            for phrase in ["is it working", "did it work", "is this right", "correct"]
        ):
            return (
                f"{user_input}\n\n{vision_intelligence}"
                "FUNCTIONAL ASSESSMENT:\n"
                "- Distinguish visual state from functional state\n"
                "- Look for success/error indicators\n"
                "- Consider what 'working correctly' means in context\n"
                "- Provide both observation and interpretation"
            )

        # Cultural/language queries
        elif any(
            word in user_lower for word in ["language", "translate", "foreign", "character", "text"]
        ):
            return (
                f"{user_input}\n\n{vision_intelligence}"
                "MULTI-LANGUAGE HANDLING:\n"
                "- Identify languages and writing systems\n"
                "- Note RTL layouts or special formatting\n"
                "- Provide translations/transliterations when possible\n"
                "- Acknowledge limitations in language recognition"
            )

        # Window counting and management queries
        elif any(
            phrase in user_lower
            for phrase in [
                "how many window",
                "count window",
                "window open",
                "desktop space",
                "mission control",
            ]
        ):
            return (
                f"{user_input}\n\n{vision_intelligence}"
                "WINDOW COUNTING & MANAGEMENT:\n"
                "- Count ALL visible windows (including Mission Control/Exposé)\n"
                "- Distinguish windows from other UI elements\n"
                "- Group by application and desktop space\n"
                "- Note window states (active, minimized, background)\n"
                "- Identify spatial relationships between windows\n"
                "PROVIDE:\n"
                "1. Total window count\n"
                "2. Breakdown by app\n"
                "3. Desktop space organization\n"
                "4. Current focus/active window"
            )

        # Window arrangement and workflow queries
        elif any(
            word in user_lower for word in ["arrange", "layout", "organize", "workflow", "setup"]
        ):
            return (
                f"{user_input}\n\n{vision_intelligence}"
                "WINDOW ARRANGEMENT & WORKFLOW ANALYSIS:\n"
                "- Identify window arrangement patterns\n"
                "- Recognize common workflows (dev setup, research mode, etc.)\n"
                "- Note spatial relationships (side-by-side, overlapping)\n"
                "- Understand functional groupings\n"
                "- Suggest organization improvements if relevant"
            )

        # Application state queries
        elif any(
            phrase in user_lower
            for phrase in ["what apps", "applications running", "programs open"]
        ):
            return (
                f"{user_input}\n\n{vision_intelligence}"
                "APPLICATION STATE INVENTORY:\n"
                "- List all running applications\n"
                "- Count windows per application\n"
                "- Note which has focus\n"
                "- Identify background vs foreground apps\n"
                "- Check dock/taskbar for minimized apps"
            )

        # Default - return with general intelligence enhancement
        return (
            f"{user_input}\n\n{vision_intelligence}"
            "COMPREHENSIVE ANALYSIS:\n"
            "- Provide intelligent, context-aware interpretation\n"
            "- Express confidence levels appropriately\n"
            "- Handle any interface type or visual content\n"
            "- If windows/apps mentioned, provide detailed window analysis\n"
            "- Ask for clarification when query is ambiguous"
        )

    async def _is_monitoring_command(self, user_input: str) -> bool:
        """Check if this is a continuous monitoring command"""
        # IMPORTANT: Exclude lock/unlock screen commands
        text_lower = user_input.lower()
        if "lock" in text_lower or "unlock" in text_lower:
            return False  # Never treat lock/unlock as monitoring

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

        return has_monitoring and has_screen

    async def _handle_monitoring_command(self, user_input: str) -> str:
        """Handle continuous monitoring commands"""
        text_lower = user_input.lower()
        logger.info(f"[MONITORING] Processing monitoring command: {user_input}")

        # Check if we have the enhanced vision analyzer with video streaming
        if self.vision_analyzer is not None:
            logger.info(
                "[MONITORING] Vision analyzer is available, attempting to handle monitoring"
            )
            try:
                # Use the already initialized vision analyzer
                # Handle different monitoring commands
                if any(
                    word in text_lower
                    for word in ["start", "enable", "activate", "begin", "turn on"]
                ):
                    logger.info("[MONITORING] Starting video streaming...")
                    logger.info(
                        f"[MONITORING] Vision analyzer available: {self.vision_analyzer is not None}"
                    )
                    logger.info(
                        f"[MONITORING] Vision analyzer ID: {id(self.vision_analyzer) if self.vision_analyzer else None}"
                    )

                    # Check video streaming module before starting
                    if hasattr(self.vision_analyzer, "get_video_streaming"):
                        vs = await self.vision_analyzer.get_video_streaming()
                        logger.info(f"[MONITORING] Video streaming module exists: {vs is not None}")
                        if vs:
                            logger.info(
                                f"[MONITORING] Video streaming already capturing: {vs.is_capturing}"
                            )

                    # Start video streaming
                    try:
                        logger.info(
                            f"[MONITORING] Vision analyzer type: {type(self.vision_analyzer).__name__}"
                        )
                        logger.info(
                            f"[MONITORING] Vision analyzer has start_video_streaming: {hasattr(self.vision_analyzer, 'start_video_streaming')}"
                        )
                        logger.info(
                            f"[MONITORING] Vision analyzer config: enable_video_streaming={self.vision_analyzer.config.enable_video_streaming}"
                        )
                        result = await self.vision_analyzer.start_video_streaming()
                        logger.info(f"[MONITORING] Video streaming result: {result}")
                        logger.info(f"[MONITORING] Result success: {result.get('success')}")

                        if result.get("success"):
                            # Update monitoring state
                            self._monitoring_active = True
                            capture_method = result.get("metrics", {}).get(
                                "capture_method", "unknown"
                            )

                            # IMPORTANT: Return the proper response about video capture activation
                            if capture_method == "macos_native":
                                return "I have successfully activated native macOS video capturing for monitoring your screen. The purple recording indicator should now be visible in your menu bar, confirming that screen recording is active. I'm capturing at 30 FPS and will continuously monitor for any changes or important events on your screen."
                            elif capture_method == "swift_native":
                                return "I have successfully activated Swift-based macOS video capturing for monitoring your screen. The purple recording indicator should now be visible in your menu bar, confirming that screen recording is active. I'm capturing at 30 FPS with enhanced permission handling and will continuously monitor for any changes or important events on your screen."
                            elif capture_method == "direct_swift":
                                return "I have successfully activated direct Swift video capturing for monitoring your screen. The purple recording indicator should now be visible in your menu bar, confirming that screen recording is active. I'm monitoring continuously at 30 FPS and will watch for any changes or important events on your screen until you tell me to stop."
                            elif capture_method == "purple_indicator":
                                return "I have successfully started monitoring your screen. The purple recording indicator should now be visible in your menu bar, confirming that screen recording is active. I'm capturing your screen at 30 FPS and will continuously monitor for any changes or important events until you tell me to stop."
                            else:
                                return f"I've started monitoring your screen using {capture_method} capture mode at 30 FPS. I'll continuously watch for any changes or important events on your screen."
                        else:
                            error_msg = result.get("error", "Unknown error")
                            logger.error(
                                f"[MONITORING] Failed to start video streaming: {error_msg}"
                            )
                            return f"I encountered an issue starting video streaming: {error_msg}. Please check that screen recording permissions are enabled in System Preferences."

                    except Exception as e:
                        logger.error(f"[MONITORING] Exception starting video streaming: {e}")
                        return f"I encountered an error starting video monitoring: {str(e)}. Please ensure screen recording permissions are enabled."

                elif any(
                    word in text_lower
                    for word in ["stop", "disable", "deactivate", "end", "turn off"]
                ):
                    # Stop video streaming
                    result = await self.vision_analyzer.stop_video_streaming()
                    if result.get("success"):
                        self._monitoring_active = False
                        self._capture_method = "unknown"
                        return "I've stopped monitoring your screen. The video streaming has been disabled and the recording indicator should have disappeared."
                    else:
                        self._monitoring_active = False
                        self._capture_method = "unknown"
                        return "The screen monitoring appears to be already stopped."

                else:
                    # Generic monitoring request - start monitoring and describe
                    result = await self.vision_analyzer.start_video_streaming()
                    if result.get("success"):
                        # Update monitoring state
                        self._monitoring_active = True
                        self._capture_method = result.get("metrics", {}).get(
                            "capture_method", "unknown"
                        )

                        # Analyze for 5 seconds
                        analysis_result = await self.vision_analyzer.analyze_video_stream(
                            "Monitor the screen and describe any changes or important elements you see.",
                            duration_seconds=5.0,
                        )

                        if analysis_result.get("success"):
                            frames_analyzed = analysis_result.get("frames_analyzed", 0)
                            descriptions = []

                            if "results" in analysis_result:
                                for result in analysis_result["results"][:3]:  # First 3 analyses
                                    if "analysis" in result:
                                        descriptions.append(str(result["analysis"]))

                            response = f"I'm now continuously monitoring your screen at 30 FPS. I've analyzed {frames_analyzed} frames in the last 5 seconds.\n\n"

                            if descriptions:
                                response += "Here's what I observed:\n" + "\n".join(
                                    f"• {desc[:100]}..." for desc in descriptions
                                )
                            else:
                                response += (
                                    "I'm watching your screen for any changes or important events."
                                )

                            return response
                        else:
                            return "I've started monitoring your screen. I'll watch for changes and alert you to anything important."

            except Exception as e:
                logger.error(f"Error in monitoring command: {e}")
                return f"I encountered an error setting up continuous monitoring: {str(e)}. Let me fall back to standard screenshot analysis."

        else:
            logger.warning("[MONITORING] Vision analyzer is None - cannot start monitoring")

        # Fallback response if enhanced analyzer not available
        return "I'll need the enhanced vision system to enable continuous monitoring. Currently, I can only take screenshots on demand. Please ensure the vision system is properly initialized."

    def _is_screen_query(self, user_input: str) -> bool:
        """Check if user is asking about what's currently on screen"""
        screen_query_patterns = [
            r"can you see",
            r"do you see",
            r"what do you see",
            r"what.*on.*screen",
            r"what.*looking at",
            r"describe.*screen",
            r"tell me what",
            r"what is on",
            r"what\'s on",
            r"what are you seeing",
            r"what can you see",
        ]

        input_lower = user_input.lower()
        for pattern in screen_query_patterns:
            if re.search(pattern, input_lower):
                return True
        return False

    async def _analyze_current_screen(self, query: str) -> str:
        """Analyze current screen content when monitoring is active"""
        try:
            logger.info(f"[MONITOR] Real-time screen analysis requested: {query}")

            # Capture current screen
            try:
                screenshot = await self.capture_screenshot()
            except Exception as e:
                logger.error(f"[MONITOR] Screenshot capture failed: {e}")
                return f"I encountered an error capturing your screen: {str(e)}. Please ensure screen recording permissions are enabled."

            if not screenshot:
                return "I'm having trouble capturing the screen right now. Please ensure screen recording permissions are enabled in System Preferences."

            # Create an enhanced prompt with pure vision intelligence
            query_lower = query.lower()

            # Base intelligence framework for real-time monitoring
            monitoring_intelligence = (
                "You are JARVIS, Tony Stark's AI assistant, actively monitoring the screen in real-time.\n"
                "Use your advanced vision intelligence to:\n"
                "- UNDERSTAND the complete visual context including window relationships\n"
                "- DISTINGUISH between system UI and application content\n"
                "- RECOGNIZE when elements are obscured, minimized, or on different screens\n"
                "- EXPRESS CONFIDENCE levels when there's ambiguity\n"
                "- PROVIDE INTELLIGENT INSIGHTS about what's happening\n\n"
            )

            if "battery" in query_lower or "power" in query_lower or "charge" in query_lower:
                analysis_prompt = (
                    f"{monitoring_intelligence}"
                    f"The user asked: '{query}'\n\n"
                    "REAL-TIME BATTERY ANALYSIS:\n"
                    "- Locate the SYSTEM battery indicator (not battery icons in apps/screenshots)\n"
                    "- Report the exact percentage visible\n"
                    "- Note charging state\n"
                    "- If multiple batteries visible, identify the system one\n"
                    "- If obscured or not visible, explain why"
                )

            elif "time" in query_lower or "clock" in query_lower:
                analysis_prompt = (
                    f"{monitoring_intelligence}"
                    f"The user asked: '{query}'\n\n"
                    "REAL-TIME TIME CHECK:\n"
                    "- Find the SYSTEM clock (not times in apps/videos)\n"
                    "- Report the exact time displayed\n"
                    "- If multiple times visible, explain which is the system time\n"
                    "- Express confidence if ambiguous"
                )

            elif "status bar" in query_lower or "menu bar" in query_lower:
                analysis_prompt = (
                    f"{monitoring_intelligence}"
                    f"The user asked: '{query}'\n\n"
                    "REAL-TIME STATUS BAR ANALYSIS:\n"
                    "- Examine the entire system status/menu bar\n"
                    "- Report all elements with exact values\n"
                    "- Note any parts hidden by windows\n"
                    "- Distinguish from app toolbars"
                )

            elif any(
                app in query_lower
                for app in ["slack", "discord", "chrome", "safari", "vscode", "terminal"]
            ):
                # Extract the app name
                app_name = next(
                    (
                        app
                        for app in ["slack", "discord", "chrome", "safari", "vscode", "terminal"]
                        if app in query_lower
                    ),
                    None,
                )
                analysis_prompt = (
                    f"{monitoring_intelligence}"
                    f"The user asked: '{query}'\n\n"
                    f"REAL-TIME {app_name.upper()} CHECK:\n"
                    f"- Is {app_name.title()} visible on screen?\n"
                    f"- If yes: Is it in foreground or background? What's visible?\n"
                    f"- If no: Explain (minimized, different desktop, not running)\n"
                    f"- Don't assume - only report what you actually see"
                )

            else:
                # Comprehensive screen analysis
                analysis_prompt = (
                    f"{monitoring_intelligence}"
                    f"The user asked: '{query}'\n\n"
                    "REAL-TIME COMPREHENSIVE ANALYSIS:\n"
                    "- Identify ALL visible windows and their z-order\n"
                    "- Note which window is active/focused\n"
                    "- Report exact system UI values\n"
                    "- Describe window relationships and occlusions\n"
                    "- Mention any dynamic content or transitions\n"
                    "- If they ask about something not visible, explain why\n"
                    "- Provide intelligent insights about the user's workflow"
                )

            # Add temporal awareness for monitoring
            analysis_prompt += (
                "\n\nREAL-TIME CONTEXT: Since you're monitoring in real-time, "
                "note if anything appears to be changing or if the user might need "
                "to know about something that just happened or is about to happen."
            )

            # Window tracking for monitoring mode
            if any(
                word in query_lower for word in ["window", "app", "program", "desktop", "space"]
            ):
                analysis_prompt += (
                    "\n\nWINDOW TRACKING: Monitor and report:"
                    "- New windows opening"
                    "- Windows closing or minimizing"
                    "- Focus changes between windows"
                    "- Desktop space switches"
                    "- Application launches or quits"
                )

            # Analyze the screenshot
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": analysis_prompt},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": self._encode_image_optimized(screenshot),
                            },
                        },
                    ],
                }
            ]

            # Make API call with vision
            try:
                # Try intelligent model selection first if enabled
                if self.use_intelligent_selection:
                    try:
                        logger.info(
                            "[MONITOR] Using intelligent model selection for screen analysis"
                        )

                        # Extract screenshot from messages for intelligent selection
                        screenshot_base64 = None
                        for content in messages[0]["content"]:
                            if content.get("type") == "image":
                                screenshot_base64 = content["source"]["data"]
                                break

                        if screenshot_base64:
                            response_text = await self._generate_with_intelligent_selection(
                                messages=messages, user_input=query, is_vision=True
                            )

                            if response_text:
                                logger.info(
                                    f"[MONITOR] Screen analysis completed via intelligent selection"
                                )
                                return response_text
                            else:
                                logger.warning(
                                    "[MONITOR] Intelligent selection returned None, falling back"
                                )
                    except Exception as e:
                        logger.error(
                            f"[MONITOR] Intelligent selection failed: {e}, falling back to direct API"
                        )

                # Fallback to direct API call
                response = await asyncio.to_thread(
                    self.client.messages.create,
                    model=self.model,
                    max_tokens=300,
                    temperature=0.7,
                    messages=messages,
                    system="You are JARVIS, an AI assistant with real-time screen monitoring capabilities. Provide natural, conversational responses about what you observe on the user's screen.",
                )

                if response and hasattr(response, "content") and len(response.content) > 0:
                    return response.content[0].text
                else:
                    logger.error("[MONITOR] Empty response from Claude API")
                    return "I'm having trouble analyzing the screen right now. The vision API returned an empty response."

            except Exception as api_error:
                logger.error(f"[MONITOR] Claude API error: {api_error}")
                if "api_key" in str(api_error).lower():
                    return "I need my vision API key to analyze your screen. Please ensure ANTHROPIC_API_KEY is set."
                elif "rate_limit" in str(api_error).lower():
                    return "I'm being rate limited by the vision API. Please try again in a moment."
                else:
                    return f"I encountered an error with the vision API: {str(api_error)}. Please try again."

        except Exception as e:
            logger.error(f"[MONITOR] Error analyzing current screen: {e}", exc_info=True)
            return "I'm having trouble analyzing the screen right now. Let me try taking a fresh screenshot."

    async def analyze_multiple_images_with_prompt(
        self, images: list, prompt: str, max_tokens: int = 1000
    ) -> dict:
        """
        Analyze multiple images (e.g., from different desktop spaces) with a single prompt
        Used by multi-space vision system
        """
        if not self.is_available():
            return {
                "text": "Multi-space vision analysis unavailable without API key",
                "detailed_description": "API key required",
            }

        try:
            # Build messages with multiple images
            messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]

            # Add each image to the content
            for img_data in images:
                if isinstance(img_data, dict) and "image" in img_data:
                    # Convert numpy array or PIL image to base64
                    if hasattr(img_data["image"], "shape"):  # numpy array
                        import numpy as np
                        from PIL import Image

                        pil_image = Image.fromarray(np.uint8(img_data["image"]))
                    else:
                        pil_image = img_data["image"]

                    # Convert to base64
                    import base64
                    import io

                    buffered = io.BytesIO()
                    pil_image.save(buffered, format="PNG")
                    image_base64 = base64.b64encode(buffered.getvalue()).decode()

                    # Add image with label
                    label = img_data.get("label", "Image")
                    messages[0]["content"].append({"type": "text", "text": f"\n{label}:"})
                    messages[0]["content"].append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": image_base64,
                            },
                        }
                    )

            # TODO: Add intelligent model selection for multi-image analysis in future enhancement
            # Would require extending _generate_with_intelligent_selection to handle multiple images

            # Create the API request
            response = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=self.temperature,
                system=self.system_prompt,
                messages=messages,
            )

            # Extract response text
            response_text = ""
            if hasattr(response, "content") and len(response.content) > 0:
                response_text = response.content[0].text

            return {"text": response_text, "detailed_description": response_text}

        except Exception as e:
            logger.error(f"Error analyzing multiple images: {e}")
            return {
                "text": f"Error analyzing multiple desktop spaces: {str(e)}",
                "detailed_description": str(e),
            }

    async def generate_response(self, user_input: str) -> str:
        """
        Process user input and generate response, using vision when appropriate
        """
        if not self.is_available():
            return "Claude API is not available. Please install anthropic package and set API key."

        # Check for continuous monitoring commands
        is_monitoring = await self._is_monitoring_command(user_input)
        logger.info(
            f"[VISION DEBUG] Is monitoring command: {is_monitoring} for input: {user_input}"
        )

        if is_monitoring:
            logger.info(f"[VISION DEBUG] Routing to _handle_monitoring_command")
            return await self._handle_monitoring_command(user_input)

        # Check if monitoring is active and user is asking about what's on screen
        if self._monitoring_active and self._is_screen_query(user_input):
            logger.info(f"[VISION DEBUG] Monitoring active and screen query detected: {user_input}")
            # Use real-time screen analysis when monitoring is active
            return await self._analyze_current_screen(user_input)

        # Check if this is a vision command
        if self.is_vision_command(user_input):
            logger.info(f"Vision command detected: {user_input}")
            return await self.analyze_screen_with_vision(user_input)

        # Otherwise, use regular text processing
        try:
            # Build messages for the API
            messages = self._build_messages(user_input)

            # Update system prompt with current date
            # Use correct year - datetime.now() returns actual current date
            current_datetime = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
            dynamic_system_prompt = f"""{self.system_prompt}

Note: The current date and time is {current_datetime}. Always use this as the reference for any time-related queries."""

            # Make API call
            start_time = datetime.now()

            # Try intelligent model selection first if enabled
            if self.use_intelligent_selection:
                try:
                    logger.info("Using intelligent model selection for text request")
                    ai_response = await self._generate_with_intelligent_selection(
                        messages=messages,
                        user_input=user_input,
                        is_vision=False,
                        system_prompt=dynamic_system_prompt,
                    )

                    if ai_response:
                        # Log performance
                        response_time = (datetime.now() - start_time).total_seconds()
                        logger.info(f"Intelligent selection completed in {response_time:.2f}s")

                        # Update conversation history
                        self._update_history(user_input, ai_response)

                        return ai_response
                    else:
                        logger.warning(
                            "Intelligent selection returned None, falling back to direct API"
                        )
                except Exception as e:
                    logger.error(f"Intelligent selection failed: {e}, falling back to direct API")

            # Fallback to direct API call
            response = await asyncio.to_thread(
                self.client.messages.create,
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                system=dynamic_system_prompt,
                messages=messages,
            )

            # Extract response
            ai_response = response.content[0].text

            # Log performance
            response_time = (datetime.now() - start_time).total_seconds()
            logger.info(f"Claude API call completed in {response_time:.2f}s")

            # Update conversation history
            self._update_history(user_input, ai_response)

            return ai_response

        except Exception as e:
            logger.error(f"Error generating response: {e}")
            return f"I encountered an error: {str(e)}"

    def _build_messages(
        self,
        user_input: str,
        context_window: Optional[int] = None,
        include_system_messages: bool = False,
        filter_strategy: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Build message list with intelligent context management"""
        messages = []

        # Determine context window size dynamically
        if context_window is None:
            context_window = self._calculate_optimal_context_window(user_input)

        # Get relevant history based on strategy
        relevant_history = self._get_relevant_history(context_window, filter_strategy)

        # Add system messages if needed
        if include_system_messages and hasattr(self, "_last_system_message"):
            messages.append({"role": "system", "content": self._last_system_message})

        # Build conversation context intelligently
        for entry in relevant_history:
            # Add user message
            user_msg = {"role": "user", "content": entry["user"]}

            # Add metadata if available
            if "user_metadata" in entry:
                user_msg["metadata"] = entry["user_metadata"]

            messages.append(user_msg)

            # Add assistant message
            assistant_msg = {"role": "assistant", "content": entry["assistant"]}

            if "assistant_metadata" in entry:
                assistant_msg["metadata"] = entry["assistant_metadata"]

            messages.append(assistant_msg)

        # Add current user input with enhanced context
        current_msg = {"role": "user", "content": user_input}

        # Add query metadata
        if hasattr(self, "_last_vision_intent"):
            current_msg["metadata"] = {
                "intent": self._last_vision_intent,
                "timestamp": datetime.now().isoformat(),
                "is_vision": self.is_vision_command(user_input),
            }

        messages.append(current_msg)

        # Validate message size
        messages = self._validate_message_size(messages)

        return messages

    def _calculate_optimal_context_window(self, user_input: str) -> int:
        """Calculate optimal context window based on query type"""
        # Base window size
        base_window = 5

        # Adjust based on query complexity
        if self.is_vision_command(user_input):
            # Vision commands may need less context
            base_window = 3
        elif any(
            keyword in user_input.lower() for keyword in ["continue", "more", "explain", "why"]
        ):
            # Follow-up questions need more context
            base_window = 7
        elif len(user_input) > 200:
            # Complex queries benefit from more context
            base_window = 6

        # Adjust based on available history
        available_history = len(self.conversation_history)

        # Dynamic adjustment based on conversation flow
        if available_history > 0:
            recent_lengths = [
                len(h["user"]) + len(h["assistant"]) for h in self.conversation_history[-3:]
            ]
            avg_length = sum(recent_lengths) / len(recent_lengths) if recent_lengths else 0

            # Reduce context for very long conversations
            if avg_length > 1000:
                base_window = max(2, base_window - 2)

        return min(base_window, available_history)

    def _get_relevant_history(
        self, context_window: int, filter_strategy: Optional[str]
    ) -> List[Dict]:
        """Get relevant history based on filtering strategy"""
        if not self.conversation_history:
            return []

        if filter_strategy == "importance":
            # Score conversations by importance
            scored_history = []
            for i, entry in enumerate(self.conversation_history):
                score = self._calculate_conversation_importance(entry, i)
                scored_history.append((score, entry))

            # Sort by importance and take top entries
            scored_history.sort(key=lambda x: x[0], reverse=True)
            return [entry for _, entry in scored_history[:context_window]]

        elif filter_strategy == "semantic":
            # Get semantically similar conversations
            return self._get_semantically_similar_history(context_window)

        elif filter_strategy == "recent_topics":
            # Filter by recent topics
            recent_topics = self._extract_recent_topics()
            filtered = []
            for entry in reversed(self.conversation_history):
                if any(topic in entry["user"].lower() for topic in recent_topics):
                    filtered.append(entry)
                    if len(filtered) >= context_window:
                        break
            return list(reversed(filtered))

        else:
            # Default: most recent conversations
            return self.conversation_history[-context_window:]

    def _calculate_conversation_importance(self, entry: Dict, index: int) -> float:
        """Calculate importance score for a conversation"""
        score = 0.0

        # Recency factor (more recent = higher score)
        recency_weight = (index + 1) / len(self.conversation_history)
        score += recency_weight * 0.3

        # Length factor (longer = more substantial)
        total_length = len(entry["user"]) + len(entry["assistant"])
        length_score = min(total_length / 500, 1.0)
        score += length_score * 0.2

        # Keyword importance
        important_keywords = ["error", "important", "remember", "note", "key", "critical"]
        keyword_count = sum(
            1
            for kw in important_keywords
            if kw in entry["user"].lower() or kw in entry["assistant"].lower()
        )
        score += keyword_count * 0.1

        # Vision relevance
        if self.is_vision_command(entry["user"]):
            score += 0.2

        # Question/Answer quality
        if "?" in entry["user"] and len(entry["assistant"]) > 100:
            score += 0.2

        return score

    def _validate_message_size(self, messages: List[Dict]) -> List[Dict]:
        """Validate and trim messages to fit API limits"""
        # Estimate token count (rough approximation)
        total_chars = sum(len(msg.get("content", "")) for msg in messages)
        estimated_tokens = total_chars / 4  # Rough estimate

        # Claude's context limit (adjust based on model)
        max_tokens = 100000 if "claude-3" in self.model else 9000

        # If within limits, return as-is
        if estimated_tokens < max_tokens * 0.8:  # Leave 20% buffer
            return messages

        # Intelligent trimming
        logger.warning(f"Message size ({estimated_tokens} tokens) exceeds limit, trimming...")

        # Keep system messages and current query
        essential_messages = [
            msg for msg in messages if msg.get("role") == "system" or msg == messages[-1]
        ]

        # Progressively remove older messages
        other_messages = [msg for msg in messages if msg not in essential_messages]

        while len(other_messages) > 2 and estimated_tokens > max_tokens * 0.8:
            # Remove oldest exchange (user + assistant)
            if len(other_messages) >= 2:
                other_messages = other_messages[2:]

            # Recalculate
            total_chars = sum(
                len(msg.get("content", "")) for msg in essential_messages + other_messages
            )
            estimated_tokens = total_chars / 4

        return essential_messages + other_messages

    def _extract_recent_topics(self) -> List[str]:
        """Extract recent topics from conversation"""
        topics = []

        # Simple topic extraction from recent conversations
        for entry in self.conversation_history[-3:]:
            text = entry["user"].lower() + " " + entry["assistant"].lower()

            # Extract nouns and key phrases
            topic_indicators = ["about", "regarding", "concerning", "related to"]
            for indicator in topic_indicators:
                if indicator in text:
                    # Extract words after indicator
                    parts = text.split(indicator)
                    if len(parts) > 1:
                        potential_topic = parts[1].split()[0:3]
                        topics.extend(potential_topic)

        return list(set(topics))  # Unique topics

    def _update_history(
        self,
        user_input: str,
        ai_response: str,
        metadata: Optional[Dict[str, Any]] = None,
        preserve_important: bool = True,
    ):
        """Update conversation history with intelligent management"""
        # Create enhanced history entry
        history_entry = {
            "user": user_input,
            "assistant": ai_response,
            "timestamp": datetime.now().isoformat(),
            "id": self._generate_conversation_id(),
            "metadata": {
                "user_length": len(user_input),
                "assistant_length": len(ai_response),
                "is_vision": self.is_vision_command(user_input),
                "model_used": self.model,
                "temperature": self.temperature,
            },
        }

        # Add additional metadata if provided
        if metadata:
            history_entry["metadata"].update(metadata)

        # Add performance metrics if available
        if hasattr(self, "_last_response_time"):
            history_entry["metadata"]["response_time_ms"] = self._last_response_time

        # Add to history
        self.conversation_history.append(history_entry)

        # Update analytics
        if hasattr(self, "_usage_analytics"):
            if history_entry["metadata"].get("is_vision"):
                self._usage_analytics["vision_requests"] += 1

        # Intelligent history management
        if len(self.conversation_history) > self.max_history_length:
            if preserve_important:
                self._intelligent_history_trim()
            else:
                # Simple FIFO trimming
                self.conversation_history = self.conversation_history[-self.max_history_length :]

        # Trigger cleanup if needed
        if len(self.conversation_history) > self.max_history_length * 1.5:
            asyncio.create_task(self.optimize_for_performance())

    def _generate_conversation_id(self) -> str:
        """Generate unique conversation ID"""
        timestamp = datetime.now().timestamp()
        random_component = hashlib.md5(str(timestamp).encode()).hexdigest()[:8]
        return f"conv_{int(timestamp)}_{random_component}"

    def _intelligent_history_trim(self):
        """Trim history while preserving important conversations"""
        if len(self.conversation_history) <= self.max_history_length:
            return

        # Score all conversations
        scored_conversations = []
        for i, entry in enumerate(self.conversation_history):
            score = self._calculate_preservation_score(entry, i)
            scored_conversations.append((score, i, entry))

        # Sort by score (higher = more important)
        scored_conversations.sort(key=lambda x: x[0], reverse=True)

        # Keep top conversations up to max_history_length
        conversations_to_keep = []
        for score, original_index, entry in scored_conversations[: self.max_history_length]:
            conversations_to_keep.append((original_index, entry))

        # Sort by original index to maintain order
        conversations_to_keep.sort(key=lambda x: x[0])

        # Update history
        self.conversation_history = [entry for _, entry in conversations_to_keep]

        logger.info(
            f"Intelligently trimmed history from {len(scored_conversations)} to {len(self.conversation_history)} entries"
        )

    def _calculate_preservation_score(self, entry: Dict, index: int) -> float:
        """Calculate score for preserving a conversation"""
        score = 0.0
        metadata = entry.get("metadata", {})

        # Recency (exponential decay)
        age_factor = index / len(self.conversation_history)
        score += (1 - age_factor) * 0.3

        # Vision queries are often important
        if metadata.get("is_vision"):
            score += 0.25

        # Long responses indicate substantial content
        if metadata.get("assistant_length", 0) > 500:
            score += 0.2

        # Error or important keywords
        important_patterns = ["error", "important", "remember", "save", "critical", "bug", "issue"]
        text = (entry.get("user", "") + entry.get("assistant", "")).lower()
        if any(pattern in text for pattern in important_patterns):
            score += 0.3

        # Questions with detailed answers
        if "?" in entry.get("user", "") and metadata.get("assistant_length", 0) > 200:
            score += 0.15

        # Performance anomalies (very slow or fast responses)
        response_time = metadata.get("response_time_ms", 0)
        if response_time > 5000 or (response_time > 0 and response_time < 100):
            score += 0.1  # Might be interesting edge cases

        return score

    async def generate_response_with_context(
        self,
        user_input: str,
        include_analytics: bool = True,
        include_suggestions: bool = True,
        custom_context: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Generate response with comprehensive context and metadata"""
        # Track timing
        start_time = datetime.now()

        # Analyze input before processing
        input_analysis = self._analyze_user_input(user_input)

        # Generate response with error handling
        try:
            response = await self.generate_response(user_input)
            success = True
            error_info = None
        except Exception as e:
            logger.error(f"Error generating response: {e}")
            response = self._get_error_response(e)
            success = False
            error_info = str(e)

        # Calculate metrics
        response_time = (datetime.now() - start_time).total_seconds() * 1000

        # Build comprehensive context
        context_data = {
            "response": response,
            "success": success,
            "conversation_id": self._get_or_create_conversation_id(),
            "message_id": self._generate_message_id(),
            "timestamp": datetime.now().isoformat(),
            "response_time_ms": response_time,
            # Conversation state
            "conversation": {
                "message_count": len(self.conversation_history),
                "session_duration": self._calculate_session_duration(),
                "topics": self._extract_conversation_topics(),
                "context_window_used": self._get_last_context_window_size(),
            },
            # Model information
            "model_info": {
                "model": self.model,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "vision_capable": True,
                "model_type": self._get_model_type(),
            },
            # Input analysis
            "input_analysis": input_analysis,
            # Platform capabilities
            "capabilities": {
                "vision_available": self.is_available(),
                "screenshot_methods": len(getattr(self, "_capture_methods", [])),
                "platform": getattr(self, "_platform", "unknown"),
            },
        }

        # Add error info if applicable
        if error_info:
            context_data["error"] = {
                "message": error_info,
                "type": type(e).__name__ if "e" in locals() else "Unknown",
                "timestamp": datetime.now().isoformat(),
            }

        # Add analytics if requested
        if include_analytics and hasattr(self, "_usage_analytics"):
            context_data["analytics"] = self._get_session_analytics()

        # Add suggestions if requested
        if include_suggestions:
            context_data["suggestions"] = self._generate_follow_up_suggestions(user_input, response)

        # Add custom context if provided
        if custom_context:
            context_data["custom"] = custom_context

        # Update internal tracking
        self._last_response_time = response_time
        self._last_context_data = context_data

        return context_data

    def _analyze_user_input(self, user_input: str) -> Dict[str, Any]:
        """Analyze user input for insights"""
        analysis = {
            "length": len(user_input),
            "word_count": len(user_input.split()),
            "is_question": "?" in user_input,
            "is_command": any(
                cmd in user_input.lower() for cmd in ["show", "tell", "explain", "describe"]
            ),
            "is_vision": self.is_vision_command(user_input),
            "sentiment": self._detect_simple_sentiment(user_input),
            "complexity": self._estimate_query_complexity(user_input),
            "language_hints": self._detect_language_hints(user_input),
        }

        # Add intent if vision command
        if analysis["is_vision"] and hasattr(self, "_last_vision_intent"):
            analysis["vision_intent"] = self._last_vision_intent

        return analysis

    def _detect_simple_sentiment(self, text: str) -> str:
        """Detect basic sentiment from text"""
        positive_words = ["thanks", "great", "awesome", "perfect", "excellent", "good"]
        negative_words = ["error", "wrong", "bad", "issue", "problem", "fail"]

        text_lower = text.lower()
        positive_count = sum(1 for word in positive_words if word in text_lower)
        negative_count = sum(1 for word in negative_words if word in text_lower)

        if positive_count > negative_count:
            return "positive"
        elif negative_count > positive_count:
            return "negative"
        else:
            return "neutral"

    def _estimate_query_complexity(self, text: str) -> str:
        """Estimate query complexity"""
        word_count = len(text.split())

        # Check for complex indicators
        complex_indicators = ["how", "why", "explain", "analyze", "compare", "multiple"]
        has_complex = any(indicator in text.lower() for indicator in complex_indicators)

        if word_count > 50 or has_complex:
            return "complex"
        elif word_count > 20:
            return "moderate"
        else:
            return "simple"

    def _detect_language_hints(self, text: str) -> List[str]:
        """Detect language or communication hints"""
        hints = []

        text_lower = text.lower()

        # Formality
        if any(word in text_lower for word in ["please", "could you", "would you"]):
            hints.append("polite")

        # Urgency
        if any(word in text_lower for word in ["urgent", "asap", "quickly", "now"]):
            hints.append("urgent")

        # Technical
        if any(word in text_lower for word in ["api", "code", "debug", "error", "function"]):
            hints.append("technical")

        return hints

    def _get_or_create_conversation_id(self) -> str:
        """Get or create conversation ID for session"""
        if not hasattr(self, "_conversation_id"):
            self._conversation_id = f"conv_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{hashlib.md5(str(id(self)).encode()).hexdigest()[:8]}"
        return self._conversation_id

    def _generate_message_id(self) -> str:
        """Generate unique message ID"""
        return f"msg_{int(datetime.now().timestamp() * 1000)}_{hashlib.md5(str(datetime.now()).encode()).hexdigest()[:6]}"

    def _calculate_session_duration(self) -> float:
        """Calculate current session duration in seconds"""
        if not self.conversation_history:
            return 0.0

        first_timestamp = datetime.fromisoformat(self.conversation_history[0]["timestamp"])
        return (datetime.now() - first_timestamp).total_seconds()

    def _extract_conversation_topics(self) -> List[str]:
        """Extract main topics from conversation"""
        topics = set()

        # Analyze recent conversations
        for entry in self.conversation_history[-5:]:
            text = entry["user"] + " " + entry["assistant"]

            # Simple topic extraction
            if "vision" in text.lower() or "screen" in text.lower():
                topics.add("vision_analysis")
            if "error" in text.lower() or "debug" in text.lower():
                topics.add("troubleshooting")
            if "code" in text.lower() or "program" in text.lower():
                topics.add("programming")
            if "help" in text.lower() or "how" in text.lower():
                topics.add("assistance")

        return list(topics)

    def _get_last_context_window_size(self) -> int:
        """Get the size of the last context window used"""
        return getattr(self, "_last_context_window_size", 0)

    def _get_session_analytics(self) -> Dict[str, Any]:
        """Get current session analytics"""
        analytics = {
            "total_messages": len(self.conversation_history),
            "vision_queries": sum(
                1 for h in self.conversation_history if h.get("metadata", {}).get("is_vision")
            ),
            "avg_response_time_ms": self._usage_analytics.get("avg_response_time", 0),
            "error_count": self._usage_analytics.get("errors", 0),
            "cache_hit_rate": (
                self._calculate_cache_hit_rate()
                if hasattr(self, "_calculate_cache_hit_rate")
                else 0
            ),
        }

        return analytics

    def _generate_follow_up_suggestions(self, user_input: str, response: str) -> List[str]:
        """Generate intelligent follow-up suggestions"""
        suggestions = []

        # Based on query type
        if self.is_vision_command(user_input):
            suggestions.extend(
                [
                    "Can you analyze a specific part of the screen?",
                    "What else do you notice in the image?",
                    "Can you describe the layout in more detail?",
                ]
            )
        elif "?" in user_input:
            suggestions.extend(
                [
                    "Would you like more details?",
                    "Can I clarify anything else?",
                    "Do you have a follow-up question?",
                ]
            )

        # Based on response content
        if "error" in response.lower():
            suggestions.append("How can I help troubleshoot this issue?")
        elif len(response) > 500:
            suggestions.append("Would you like a summary of the key points?")

        return suggestions[:3]  # Limit to 3 suggestions

    def _get_error_response(self, error: Exception) -> str:
        """Generate appropriate error response"""
        error_type = type(error).__name__

        if "API" in error_type:
            return "I'm experiencing API connectivity issues. Please try again in a moment."
        elif "Permission" in error_type:
            return "I need additional permissions to complete this request. Please check system settings."
        elif "Timeout" in error_type:
            return "The request took too long to process. Please try again with a simpler query."
        else:
            return f"I encountered an unexpected error: {error_type}. Please try again."

    async def clear_history(self, preserve_context: bool = False, preserve_last_n: int = 0):
        """Enhanced history clearing with intelligent options"""
        if not hasattr(self, "_history_metadata"):
            self._history_metadata = {}

        # Store metadata about the cleared history
        cleared_count = len(self.conversation_history)
        cleared_timestamp = datetime.now()

        if preserve_context:
            # Preserve important context from history
            context_summary = await self._extract_context_summary()
            self._history_metadata["last_context"] = context_summary

        if preserve_last_n > 0:
            # Keep the most recent N exchanges
            preserved = self.conversation_history[-preserve_last_n:]
            self.conversation_history.clear()
            self.conversation_history.extend(preserved)
            logger.info(f"Conversation history cleared, preserved last {preserve_last_n} exchanges")
        else:
            # Complete clear
            self.conversation_history.clear()
            logger.info(f"Conversation history cleared ({cleared_count} exchanges removed)")

        # Track clearing events for analytics
        if not hasattr(self, "_clear_history_log"):
            self._clear_history_log = []

        self._clear_history_log.append(
            {
                "timestamp": cleared_timestamp,
                "cleared_count": cleared_count,
                "preserved_count": preserve_last_n,
                "preserve_context": preserve_context,
            }
        )

        # Cleanup old logs (keep last 100)
        if len(self._clear_history_log) > 100:
            self._clear_history_log = self._clear_history_log[-100:]

    def is_available(self) -> bool:
        """Comprehensive availability check with diagnostics"""
        # Basic availability
        basic_available = ANTHROPIC_AVAILABLE and self.client is not None

        if not basic_available:
            return False

        # Extended health checks
        if not hasattr(self, "_last_health_check"):
            self._last_health_check = None
            self._health_check_cache = None

        # Cache health check for 60 seconds
        if self._last_health_check is None or datetime.now() - self._last_health_check > timedelta(
            seconds=60
        ):

            self._health_check_cache = self._perform_health_check()
            self._last_health_check = datetime.now()

        return self._health_check_cache

    def _perform_health_check(self) -> bool:
        """Perform comprehensive health check"""
        checks = {
            "api_key": bool(self.api_key),
            "client": self.client is not None,
            "model_valid": self._is_valid_model(self.model),
            "screenshot_available": self._check_screenshot_capability(),
            "vision_analyzer": self.vision_analyzer is not None,
        }

        # Log any issues
        failed_checks = [k for k, v in checks.items() if not v]
        if failed_checks:
            logger.warning(f"Health check failures: {failed_checks}")

        # Require at least API key and client
        return checks["api_key"] and checks["client"]

    def _is_valid_model(self, model: str) -> bool:
        """Check if model is valid and supported"""
        valid_models = [
            "claude-3-opus",
            "claude-3-sonnet",
            "claude-3-haiku",
            "claude-3-5-sonnet",
            "claude-2.1",
            "claude-2.0",
            "claude-instant-1.2",
        ]
        return any(valid in model.lower() for valid in valid_models)

    def _check_screenshot_capability(self) -> bool:
        """Check screenshot capability for current platform"""
        if SCREENSHOT_AVAILABLE:
            return True

        # Check platform-specific alternatives
        if hasattr(self, "_capture_methods"):
            return len(self._capture_methods) > 0

        return False

    def get_usage_stats(self) -> Dict[str, Any]:
        """Get comprehensive usage statistics and analytics"""
        # Initialize analytics tracking if needed
        if not hasattr(self, "_usage_analytics"):
            self._usage_analytics = {
                "total_requests": 0,
                "vision_requests": 0,
                "errors": 0,
                "total_tokens": 0,
                "cache_hits": 0,
                "cache_misses": 0,
                "avg_response_time": 0,
                "response_times": [],
            }

        # Calculate dynamic statistics
        stats = {
            # Basic info
            "model": self.model,
            "model_type": self._get_model_type(),
            "api_available": self.is_available(),
            # History stats
            "history": {
                "current_length": len(self.conversation_history),
                "max_length": self.max_history_length,
                "total_characters": sum(
                    len(h.get("user", "")) + len(h.get("assistant", ""))
                    for h in self.conversation_history
                ),
                "avg_exchange_length": self._calculate_avg_exchange_length(),
            },
            # Vision capabilities
            "vision": {
                "capable": True,
                "screenshot_available": SCREENSHOT_AVAILABLE,
                "capture_methods": len(getattr(self, "_capture_methods", [])),
                "analyzer_available": self.vision_analyzer is not None,
                "supported_formats": self._get_supported_image_formats(),
            },
            # Performance metrics
            "performance": {
                "total_requests": self._usage_analytics["total_requests"],
                "vision_requests": self._usage_analytics["vision_requests"],
                "error_rate": self._calculate_error_rate(),
                "avg_response_time_ms": self._usage_analytics["avg_response_time"],
                "cache_hit_rate": self._calculate_cache_hit_rate(),
            },
            # Platform info
            "platform": {
                "os": platform.system(),
                "python_version": platform.python_version(),
                "timezone": self._get_timezone_name() or "Unknown",
            },
            # Configuration
            "config": {
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "cache_duration": getattr(self, "_cache_config", {})
                .get("duration", timedelta(seconds=5))
                .total_seconds(),
            },
        }

        return stats

    def _get_model_type(self) -> str:
        """Determine model type from model name"""
        model_lower = self.model.lower()
        if "opus" in model_lower:
            return "opus"
        elif "sonnet" in model_lower:
            return "sonnet"
        elif "haiku" in model_lower:
            return "haiku"
        elif "instant" in model_lower:
            return "instant"
        else:
            return "unknown"

    def _calculate_avg_exchange_length(self) -> float:
        """Calculate average conversation exchange length"""
        if not self.conversation_history:
            return 0.0

        total_length = sum(
            len(h.get("user", "")) + len(h.get("assistant", "")) for h in self.conversation_history
        )
        return total_length / len(self.conversation_history)

    def _get_supported_image_formats(self) -> List[str]:
        """Get list of supported image formats"""
        formats = ["JPEG", "PNG"]

        try:
            from PIL import Image

            # Add more formats if PIL supports them
            additional_formats = ["GIF", "BMP", "WEBP"]
            for fmt in additional_formats:
                try:
                    Image.new("RGB", (1, 1)).save(io.BytesIO(), format=fmt)
                    formats.append(fmt)
                except Exception:
                    pass
        except Exception:
            pass

        return formats

    def _calculate_error_rate(self) -> float:
        """Calculate error rate percentage"""
        total = self._usage_analytics["total_requests"]
        if total == 0:
            return 0.0
        return (self._usage_analytics["errors"] / total) * 100

    def _calculate_cache_hit_rate(self) -> float:
        """Calculate cache hit rate percentage"""
        total_cache_requests = (
            self._usage_analytics["cache_hits"] + self._usage_analytics["cache_misses"]
        )
        if total_cache_requests == 0:
            return 0.0
        return (self._usage_analytics["cache_hits"] / total_cache_requests) * 100

    @property
    def model_name(self) -> str:
        """Get model name with dynamic information"""
        # Add context about model capabilities
        base_model = self.model

        # Add capability indicators
        capabilities = []
        if "vision" in base_model.lower() or "3" in base_model:
            capabilities.append("vision")
        if "instant" in base_model.lower() or "haiku" in base_model.lower():
            capabilities.append("fast")
        if "opus" in base_model.lower():
            capabilities.append("advanced")

        if capabilities:
            return f"{base_model} [{', '.join(capabilities)}]"
        return base_model

    async def generate_response_stream(self, user_input: str, adaptive: bool = True):
        """Enhanced streaming with adaptive chunk sizes and timing"""
        # Check if this is a vision command
        is_vision = self.is_vision_command(user_input)

        # Get the response
        start_time = datetime.now()
        response = await self.generate_response(user_input)
        response_time = (datetime.now() - start_time).total_seconds()

        # Determine optimal streaming parameters
        if adaptive:
            stream_params = self._calculate_stream_parameters(response, response_time, is_vision)
        else:
            stream_params = {"chunk_size": 50, "delay": 0.01}

        # Track streaming metrics
        chunks_sent = 0
        total_delay = 0

        # Stream with dynamic parameters
        chunk_size = stream_params["chunk_size"]
        base_delay = stream_params["delay"]

        for i in range(0, len(response), chunk_size):
            chunk = response[i : i + chunk_size]

            # Dynamic delay based on content
            delay = self._calculate_chunk_delay(chunk, base_delay, chunks_sent)

            yield chunk

            await asyncio.sleep(delay)
            chunks_sent += 1
            total_delay += delay

            # Adaptive adjustment mid-stream
            if adaptive and chunks_sent % 10 == 0:
                # Adjust parameters based on performance
                remaining = len(response) - i
                if remaining > chunk_size * 5:  # Still have significant content
                    chunk_size = min(chunk_size * 2, 200)  # Speed up

        # Log streaming metrics
        if hasattr(self, "_streaming_metrics"):
            self._streaming_metrics.append(
                {
                    "response_length": len(response),
                    "chunks": chunks_sent,
                    "total_delay": total_delay,
                    "avg_chunk_size": len(response) / chunks_sent if chunks_sent > 0 else 0,
                    "is_vision": is_vision,
                }
            )

    def _calculate_stream_parameters(
        self, response: str, response_time: float, is_vision: bool
    ) -> Dict[str, Any]:
        """Calculate optimal streaming parameters"""
        response_length = len(response)

        # Base parameters
        params = {"chunk_size": 50, "delay": 0.01}

        # Adjust for response length
        if response_length < 200:
            # Short response - larger chunks
            params["chunk_size"] = 100
            params["delay"] = 0.02
        elif response_length > 2000:
            # Long response - smaller initial chunks, will adapt
            params["chunk_size"] = 30
            params["delay"] = 0.005

        # Adjust for response time (simulate realistic typing)
        if response_time < 1.0:
            # Fast response - slow down streaming to seem more natural
            params["delay"] *= 2
        elif response_time > 5.0:
            # Slow response - speed up streaming
            params["delay"] *= 0.5

        # Adjust for content type
        if is_vision:
            # Vision responses often have structured content
            params["chunk_size"] = 75  # Larger chunks for descriptions

        return params

    def _calculate_chunk_delay(self, chunk: str, base_delay: float, chunk_index: int) -> float:
        """Calculate delay for specific chunk based on content"""
        # Natural pauses at punctuation
        if chunk.rstrip().endswith((".", "!", "?")):
            return base_delay * 3  # Longer pause at sentence end
        elif chunk.rstrip().endswith((",", ";", ":")):
            return base_delay * 2  # Medium pause at clause breaks
        elif "\n" in chunk:
            return base_delay * 2.5  # Pause at line breaks

        # Speed up after initial chunks
        if chunk_index > 5:
            return base_delay * 0.8

        return base_delay

    async def get_conversation_history(
        self,
        include_metadata: bool = False,
        last_n: Optional[int] = None,
        filter_by: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get conversation history with filtering and metadata options"""
        history = self.conversation_history.copy()

        # Apply filtering
        if filter_by:
            if filter_by == "vision":
                # Filter for vision-related exchanges
                history = [h for h in history if self.is_vision_command(h.get("user", ""))]
            elif filter_by == "long":
                # Filter for substantial exchanges
                history = [
                    h for h in history if len(h.get("user", "")) + len(h.get("assistant", "")) > 200
                ]
            elif filter_by == "recent":
                # Filter for recent exchanges (last hour)
                cutoff = datetime.now() - timedelta(hours=1)
                history = [
                    h
                    for h in history
                    if datetime.fromisoformat(h.get("timestamp", datetime.now().isoformat()))
                    > cutoff
                ]

        # Limit to last N if specified
        if last_n and last_n > 0:
            history = history[-last_n:]

        # Add metadata if requested
        if include_metadata:
            enriched_history = []
            for i, exchange in enumerate(history):
                enriched = exchange.copy()
                enriched["metadata"] = {
                    "index": i,
                    "user_length": len(exchange.get("user", "")),
                    "assistant_length": len(exchange.get("assistant", "")),
                    "is_vision": self.is_vision_command(exchange.get("user", "")),
                    "timestamp_parsed": datetime.fromisoformat(
                        exchange.get("timestamp", datetime.now().isoformat())
                    ),
                }
                enriched_history.append(enriched)
            return enriched_history

        return history

    def set_system_prompt(self, prompt: str, merge_with_default: bool = False):
        """Update system prompt with validation and options"""
        # Validate prompt
        if not prompt or not isinstance(prompt, str):
            logger.warning("Invalid system prompt provided")
            return

        # Store original if first time
        if not hasattr(self, "_original_system_prompt"):
            self._original_system_prompt = self.system_prompt

        if merge_with_default:
            # Merge with current dynamic prompt
            self._initialize_dynamic_system_prompt(prompt)
        else:
            # Direct replacement but add current context
            current_datetime = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
            timezone = self._get_timezone_name()

            # Add context to custom prompt
            context_suffix = f"\n\nCurrent date/time: {current_datetime}"
            if timezone:
                context_suffix += f" ({timezone})"

            self.system_prompt = prompt + context_suffix

        # Track prompt changes
        if not hasattr(self, "_prompt_history"):
            self._prompt_history = []

        self._prompt_history.append(
            {
                "timestamp": datetime.now(),
                "prompt": self.system_prompt,
                "merged": merge_with_default,
            }
        )

        # Limit history
        if len(self._prompt_history) > 20:
            self._prompt_history = self._prompt_history[-20:]

        logger.info(f"System prompt updated (merged: {merge_with_default})")

    async def get_response(self, prompt: str, **kwargs) -> str:
        """Enhanced alias with additional options and tracking"""
        # Track API usage
        if hasattr(self, "_usage_analytics"):
            self._usage_analytics["total_requests"] += 1

        start_time = datetime.now()

        try:
            # Check for any kwargs that modify behavior
            if kwargs.get("stream", False):
                # Return streaming generator
                return self.generate_response_stream(prompt, adaptive=kwargs.get("adaptive", True))

            # Standard response
            response = await self.generate_response(prompt)

            # Track success
            response_time = (datetime.now() - start_time).total_seconds() * 1000  # ms
            if hasattr(self, "_usage_analytics"):
                self._usage_analytics["response_times"].append(response_time)
                # Keep only last 100 response times
                if len(self._usage_analytics["response_times"]) > 100:
                    self._usage_analytics["response_times"] = self._usage_analytics[
                        "response_times"
                    ][-100:]
                # Update average
                self._usage_analytics["avg_response_time"] = sum(
                    self._usage_analytics["response_times"]
                ) / len(self._usage_analytics["response_times"])

            return response

        except Exception as e:
            # Track errors
            if hasattr(self, "_usage_analytics"):
                self._usage_analytics["errors"] += 1
            logger.error(f"Error in get_response: {e}")
            raise

    async def _extract_context_summary(self) -> str:
        """Extract important context from conversation history"""
        if not self.conversation_history:
            return ""

        # Extract key topics and entities
        topics = set()
        for exchange in self.conversation_history[-5:]:  # Last 5 exchanges
            user_text = exchange.get("user", "").lower()
            # Simple topic extraction
            if "screen" in user_text or "vision" in user_text:
                topics.add("vision_analysis")
            if "error" in user_text or "bug" in user_text:
                topics.add("troubleshooting")
            if "code" in user_text:
                topics.add("programming")

        return f"Previous topics: {', '.join(topics)}" if topics else ""

    def get_health_status(self) -> Dict[str, Any]:
        """Get detailed health and status information"""
        health = {"status": "healthy" if self.is_available() else "unhealthy", "checks": {}}

        # Detailed checks
        checks = [
            ("api_key_present", bool(self.api_key)),
            ("client_initialized", self.client is not None),
            (
                "model_valid",
                self._is_valid_model(self.model) if hasattr(self, "_is_valid_model") else True,
            ),
            ("anthropic_library", ANTHROPIC_AVAILABLE),
            (
                "screenshot_capability",
                (
                    self._check_screenshot_capability()
                    if hasattr(self, "_check_screenshot_capability")
                    else SCREENSHOT_AVAILABLE
                ),
            ),
            ("vision_analyzer", self.vision_analyzer is not None),
            (
                "conversation_history",
                hasattr(self, "conversation_history")
                and isinstance(self.conversation_history, list),
            ),
        ]

        for check_name, check_result in checks:
            health["checks"][check_name] = check_result

        # Overall status
        critical_checks = ["api_key_present", "client_initialized", "anthropic_library"]
        health["critical_ok"] = all(health["checks"].get(check, False) for check in critical_checks)

        return health

    async def optimize_for_performance(self):
        """Optimize chatbot for better performance"""
        optimizations_applied = []

        # Clear old history if too long
        if len(self.conversation_history) > self.max_history_length * 2:
            await self.clear_history(preserve_last_n=self.max_history_length)
            optimizations_applied.append("trimmed_history")

        # Clean up cache
        if hasattr(self, "_screenshot_cache_store"):
            current_time = datetime.now()
            cache_config = getattr(self, "_cache_config", {"duration": timedelta(seconds=5)})

            # Remove expired entries
            expired_keys = []
            for key, (timestamp, _, _) in self._screenshot_cache_store.items():
                if current_time - timestamp > cache_config["duration"] * 2:
                    expired_keys.append(key)

            for key in expired_keys:
                del self._screenshot_cache_store[key]

            if expired_keys:
                optimizations_applied.append(f"cleared_{len(expired_keys)}_cache_entries")

        # Reset analytics if too large
        if (
            hasattr(self, "_usage_analytics")
            and len(self._usage_analytics.get("response_times", [])) > 1000
        ):
            self._usage_analytics["response_times"] = self._usage_analytics["response_times"][-100:]
            optimizations_applied.append("trimmed_analytics")

        # Clear old prompt history
        if hasattr(self, "_prompt_history") and len(self._prompt_history) > 50:
            self._prompt_history = self._prompt_history[-20:]
            optimizations_applied.append("trimmed_prompt_history")

        logger.info(f"Performance optimizations applied: {optimizations_applied}")
        return optimizations_applied

    async def export_conversation(
        self, format: str = "json", include_system: bool = False
    ) -> Union[str, Dict]:
        """Export conversation in various formats"""
        history = await self.get_conversation_history(include_metadata=True)

        if format == "json":
            export_data = {
                "exported_at": datetime.now().isoformat(),
                "model": self.model,
                "conversation_count": len(history),
                "conversations": history,
            }

            if include_system:
                export_data["system_prompt"] = self.system_prompt
                export_data["configuration"] = {
                    "temperature": self.temperature,
                    "max_tokens": self.max_tokens,
                }

            return export_data

        elif format == "markdown":
            md_lines = [f"# Conversation Export\n"]
            md_lines.append(f"**Date**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            md_lines.append(f"**Model**: {self.model}\n")

            if include_system:
                md_lines.append(f"\n## System Prompt\n```\n{self.system_prompt}\n```\n")

            md_lines.append(f"\n## Conversation\n")

            for i, exchange in enumerate(history):
                timestamp = exchange.get("timestamp", "")
                md_lines.append(f"\n### Exchange {i+1} - {timestamp}\n")
                md_lines.append(f"**User**: {exchange.get('user', '')}\n")
                md_lines.append(f"**Assistant**: {exchange.get('assistant', '')}\n")

            return "\n".join(md_lines)

        elif format == "txt":
            txt_lines = [f"Conversation Export - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"]
            txt_lines.append(f"Model: {self.model}")
            txt_lines.append("=" * 50)

            for exchange in history:
                txt_lines.append(f"\nUser: {exchange.get('user', '')}")
                txt_lines.append(f"Assistant: {exchange.get('assistant', '')}")
                txt_lines.append("-" * 30)

            return "\n".join(txt_lines)

        else:
            raise ValueError(f"Unsupported export format: {format}")

    def get_capabilities(self) -> Dict[str, Any]:
        """Get detailed capabilities of the chatbot"""
        return {
            "text_generation": True,
            "vision_analysis": True,
            "streaming": True,
            "conversation_memory": True,
            "max_conversation_length": self.max_history_length,
            "supported_image_formats": (
                self._get_supported_image_formats()
                if hasattr(self, "_get_supported_image_formats")
                else ["JPEG", "PNG"]
            ),
            "platform_support": {
                "macos": self._platform == "darwin" if hasattr(self, "_platform") else True,
                "windows": self._platform == "win32" if hasattr(self, "_platform") else True,
                "linux": "linux" in self._platform if hasattr(self, "_platform") else True,
            },
            "api_features": {
                "models": [
                    "claude-3-opus",
                    "claude-3-sonnet",
                    "claude-3-haiku",
                    "claude-3-5-sonnet",
                ],
                "max_tokens": 4096,
                "vision_enabled": True,
                "temperature_range": (0.0, 1.0),
            },
            "performance_features": {
                "caching": hasattr(self, "_screenshot_cache_store"),
                "adaptive_streaming": True,
                "multi_method_capture": hasattr(self, "_capture_methods"),
                "intent_analysis": hasattr(self, "_analyze_vision_intent"),
            },
        }

    async def reset(self, keep_config: bool = True):
        """Reset the chatbot to initial state"""
        logger.info(f"Resetting chatbot (keep_config: {keep_config})")

        # Clear conversation
        await self.clear_history()

        # Reset analytics
        if hasattr(self, "_usage_analytics"):
            self._usage_analytics = {
                "total_requests": 0,
                "vision_requests": 0,
                "errors": 0,
                "total_tokens": 0,
                "cache_hits": 0,
                "cache_misses": 0,
                "avg_response_time": 0,
                "response_times": [],
            }

        # Clear caches
        if hasattr(self, "_screenshot_cache_store"):
            self._screenshot_cache_store.clear()

        if hasattr(self, "_pattern_cache"):
            self._pattern_cache.clear()

        # Reset to original prompt
        if hasattr(self, "_original_system_prompt") and not keep_config:
            self.system_prompt = self._original_system_prompt

        # Re-initialize if needed
        if not keep_config:
            self.temperature = 0.7
            self.max_tokens = 1024

        logger.info("Chatbot reset complete")
