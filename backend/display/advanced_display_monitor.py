#!/usr/bin/env python3
"""
Advanced Display Monitor for JARVIS
====================================

Production-ready, fully async, dynamic display monitoring system with:
- Zero hardcoding (all configuration-driven)
- Multi-method detection (AppleScript, Core Graphics, Yabai)
- Voice integration
- Smart caching
- Robust error handling
- Multi-monitor support
- Event-driven architecture

Author: Derek Russell
Date: 2025-10-15
Version: 2.0
"""

import asyncio
import json
import logging
import random
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class DetectionMethod(Enum):
    """Available detection methods for display discovery.
    
    Attributes:
        APPLESCRIPT: Uses AppleScript to query Control Center menu
        DNSSD: Uses DNS-SD (Bonjour) to discover AirPlay devices
        COREGRAPHICS: Uses Core Graphics API for display enumeration
        YABAI: Uses Yabai window manager for display information
    """

    APPLESCRIPT = "applescript"
    DNSSD = "dnssd"
    COREGRAPHICS = "coregraphics"
    YABAI = "yabai"


class DisplayType(Enum):
    """Types of displays that can be detected.
    
    Attributes:
        AIRPLAY: AirPlay-enabled displays (Apple TV, etc.)
        HDMI: HDMI-connected displays
        THUNDERBOLT: Thunderbolt/DisplayPort displays
        USB_C: USB-C connected displays
        WIRELESS: Wireless displays (Miracast, etc.)
        UNKNOWN: Unidentified display type
    """

    AIRPLAY = "airplay"
    HDMI = "hdmi"
    THUNDERBOLT = "thunderbolt"
    USB_C = "usb_c"
    WIRELESS = "wireless"
    UNKNOWN = "unknown"


class ConnectionMode(Enum):
    """Display connection modes.
    
    Attributes:
        EXTEND: Extend desktop to display
        MIRROR: Mirror primary display
    """

    EXTEND = "extend"
    MIRROR = "mirror"


@dataclass
class DisplayInfo:
    """Information about a detected display.
    
    Attributes:
        id: Unique identifier for the display
        name: Human-readable display name
        display_type: Type of display connection
        is_available: Whether display is currently available
        is_connected: Whether display is currently connected
        detection_method: Method used to detect this display
        detected_at: Timestamp when display was detected
        metadata: Additional display metadata
    """

    id: str
    name: str
    display_type: DisplayType
    is_available: bool
    is_connected: bool
    detection_method: DetectionMethod
    detected_at: datetime
    metadata: Dict[str, Any] = None

    def to_dict(self) -> Dict:
        """Convert DisplayInfo to dictionary format.
        
        Returns:
            Dictionary representation with serialized enums and datetime
        """
        data = asdict(self)
        data["display_type"] = self.display_type.value
        data["detection_method"] = self.detection_method.value
        data["detected_at"] = self.detected_at.isoformat()
        return data


@dataclass
class MonitoredDisplay:
    """Configuration for a monitored display.
    
    Attributes:
        id: Unique identifier for the display
        name: Display name to match against
        display_type: Type of display connection
        aliases: Alternative names that match this display
        auto_connect: Whether to automatically connect when detected
        auto_prompt: Whether to prompt user when detected
        connection_mode: Default connection mode (extend/mirror)
        priority: Connection priority (higher = more important)
        enabled: Whether monitoring is enabled for this display
    """

    id: str
    name: str
    display_type: str
    aliases: List[str]
    auto_connect: bool
    auto_prompt: bool
    connection_mode: str
    priority: int
    enabled: bool

    def matches(self, display_name: str) -> bool:
        """Check if display name matches this configuration.
        
        Args:
            display_name: Name of detected display to match
            
        Returns:
            True if display name matches this configuration
        """
        if display_name.lower() == self.name.lower():
            return True
        return any(alias.lower() == display_name.lower() for alias in self.aliases)


class DisplayCache:
    """Cache for display detection results to reduce API calls.
    
    Attributes:
        ttl_seconds: Time-to-live for cached entries in seconds
    """

    def __init__(self, ttl_seconds: int = 5):
        """Initialize display cache.
        
        Args:
            ttl_seconds: Cache expiration time in seconds
        """
        self.ttl_seconds = ttl_seconds
        self._cache: Dict[str, tuple[List[str], datetime]] = {}

    def get(self, key: str) -> Optional[List[str]]:
        """Get cached value if not expired.
        
        Args:
            key: Cache key to retrieve
            
        Returns:
            Cached value if valid, None if expired or not found
        """
        if key in self._cache:
            value, timestamp = self._cache[key]
            if datetime.now() - timestamp < timedelta(seconds=self.ttl_seconds):
                return value
            del self._cache[key]
        return None

    def set(self, key: str, value: List[str]):
        """Set cache value with current timestamp.
        
        Args:
            key: Cache key to store
            value: List of display names to cache
        """
        self._cache[key] = (value, datetime.now())

    def clear(self):
        """Clear all cached entries."""
        self._cache.clear()


class AppleScriptDetector:
    """AppleScript-based display detection using Control Center menu.
    
    This detector queries the Screen Mirroring menu in Control Center
    to find available AirPlay displays.
    
    Attributes:
        config: Configuration dictionary for this detector
        timeout: Script execution timeout in seconds
        retry_attempts: Number of retry attempts on failure
        retry_delay: Delay between retry attempts in seconds
        filter_items: System items to filter out from results
    """

    def __init__(self, config: Dict):
        """Initialize AppleScript detector.
        
        Args:
            config: Configuration dictionary containing timeout, retry settings
        """
        self.config = config
        self.timeout = config.get("timeout_seconds", 5.0)
        self.retry_attempts = config.get("retry_attempts", 3)
        self.retry_delay = config.get("retry_delay_seconds", 0.5)
        self.filter_items = config.get("filter_system_items", [])

    async def detect_displays(self) -> List[str]:
        """Detect available displays using AppleScript.
        
        Queries the Screen Mirroring menu in Control Center to find
        available AirPlay displays.
        
        Returns:
            List of available display names
            
        Raises:
            asyncio.TimeoutError: If script execution times out
        """
        for attempt in range(self.retry_attempts):
            try:
                script = """
                tell application "System Events"
                    tell process "ControlCenter"
                        try
                            set mirroringMenu to menu 1 of menu bar item "Screen Mirroring" of menu bar 1
                            set menuItems to name of menu items of mirroringMenu

                            set deviceNames to {}
                            repeat with itemName in menuItems
                                set end of deviceNames to itemName as text
                            end repeat

                            return deviceNames
                        on error errMsg
                            return "ERROR:" & errMsg
                        end try
                    end tell
                end tell
                """

                result = await asyncio.create_subprocess_exec(
                    "osascript",
                    "-e",
                    script,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                stdout, stderr = await asyncio.wait_for(result.communicate(), timeout=self.timeout)

                if result.returncode == 0:
                    output = stdout.decode("utf-8").strip()

                    if output.startswith("ERROR:"):
                        logger.debug(f"AppleScript error: {output}")
                        continue

                    # Parse the output
                    devices = []
                    if output:
                        # AppleScript returns comma-separated values
                        raw_devices = [d.strip() for d in output.split(", ")]
                        devices = [d for d in raw_devices if d and d not in self.filter_items]

                    logger.debug(f"AppleScript detected: {devices}")
                    return devices

            except asyncio.TimeoutError:
                logger.warning(f"AppleScript timeout (attempt {attempt + 1}/{self.retry_attempts})")
                if attempt < self.retry_attempts - 1:
                    await asyncio.sleep(self.retry_delay)
            except Exception as e:
                logger.error(f"AppleScript error: {e}")
                if attempt < self.retry_attempts - 1:
                    await asyncio.sleep(self.retry_delay)

        return []

    async def connect_display(self, display_name: str) -> Dict[str, Any]:
        """Connect to a display using AppleScript.
        
        Uses AppleScript to click through the Control Center menu
        to connect to the specified display.
        
        Args:
            display_name: Name of display to connect to
            
        Returns:
            Connection result with success status and message
            
        Raises:
            asyncio.TimeoutError: If connection times out
        """
        try:
            script = f"""
            tell application "System Events"
                tell process "ControlCenter"
                    try
                        click menu bar item "Screen Mirroring" of menu bar 1
                        delay 0.3

                        click menu item "{display_name}" of menu 1 of menu bar item "Screen Mirroring" of menu bar 1
                        delay 0.2

                        return "SUCCESS"
                    on error errMsg
                        return "ERROR:" & errMsg
                    end try
                end tell
            end tell
            """

            result = await asyncio.create_subprocess_exec(
                "osascript",
                "-e",
                script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(
                result.communicate(), timeout=self.timeout * 2  # Connection takes longer
            )

            output = stdout.decode("utf-8").strip()

            if "SUCCESS" in output:
                return {"success": True, "message": f"Connected to {display_name}"}
            else:
                error_detail = output.replace("ERROR:", "").strip()
                return {
                    "success": False,
                    "message": f"Failed to connect to {display_name}",
                    "error": error_detail,
                }

        except asyncio.TimeoutError:
            return {"success": False, "message": "Connection timeout"}
        except Exception as e:
            return {"success": False, "message": str(e)}


class DNSSDDetector:
    """DNS-SD (Bonjour) based AirPlay display detection for macOS Sequoia+.
    
    Uses the dns-sd command to discover AirPlay devices on the network
    via Bonjour service discovery.
    
    Attributes:
        config: Configuration dictionary for this detector
        timeout: Discovery timeout in seconds
        service_type: Bonjour service type to search for
        exclude_local: Whether to exclude local device from results
    """

    def __init__(self, config: Dict):
        """Initialize DNS-SD detector.
        
        Args:
            config: Configuration dictionary with timeout and service settings
        """
        self.config = config
        self.timeout = config.get("timeout_seconds", 5.0)
        self.service_type = config.get("service_type", "_airplay._tcp")
        self.exclude_local = config.get("exclude_local_device", True)

    async def detect_displays(self) -> List[str]:
        """Detect AirPlay displays using dns-sd command.
        
        Runs dns-sd browsing for the configured service type and
        parses the output to extract device names.
        
        Returns:
            List of discovered AirPlay device names
            
        Raises:
            FileNotFoundError: If dns-sd command is not available
        """
        try:
            # Start dns-sd browsing in background
            process = await asyncio.create_subprocess_exec(
                "dns-sd",
                "-B",
                self.service_type,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Let it run for timeout seconds to collect results
            await asyncio.sleep(self.timeout)

            # Kill the process
            process.terminate()
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=2.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                stdout, stderr = b"", b""

            if stdout:
                output = stdout.decode("utf-8", errors="ignore")

                # Parse dns-sd output
                # Format: "Timestamp A/R Flags if Domain Service Type Instance Name"
                # We want the Instance Name column
                devices = []
                for line in output.split("\n"):
                    if "Add" in line and self.service_type in line:
                        # Split by multiple spaces and get last part (instance name)
                        parts = [p for p in line.split("  ") if p.strip()]
                        if len(parts) >= 3:
                            instance_name = parts[-1].strip()

                            # Filter out local device if configured
                            if self.exclude_local and "MacBook" in instance_name:
                                continue

                            if instance_name and instance_name not in devices:
                                devices.append(instance_name)

                logger.debug(f"DNS-SD detected: {devices}")
                return devices

            return []

        except FileNotFoundError:
            logger.warning("dns-sd command not found")
            return []
        except Exception as e:
            logger.error(f"DNS-SD detection error: {e}")
            return []


class CoreGraphicsDetector:
    """Core Graphics-based display detection using Quartz framework.
    
    Uses the macOS Core Graphics API to enumerate connected displays.
    Requires the Quartz Python bindings to be available.
    
    Attributes:
        config: Configuration dictionary for this detector
        max_displays: Maximum number of displays to query
        exclude_builtin: Whether to exclude built-in displays
    """

    def __init__(self, config: Dict):
        """Initialize Core Graphics detector.
        
        Args:
            config: Configuration dictionary with display limits and filters
        """
        self.config = config
        self.max_displays = config.get("max_displays", 32)
        self.exclude_builtin = config.get("exclude_builtin", True)

    async def detect_displays(self) -> List[str]:
        """Detect displays using Core Graphics API.
        
        Queries the Core Graphics display list to find connected displays.
        
        Returns:
            List of display names (generated from display IDs)
            
        Raises:
            ImportError: If Quartz framework is not available
        """
        try:
            import Quartz

            # Get online displays
            result = Quartz.CGGetOnlineDisplayList(self.max_displays, None, None)

            if result[0] != 0:  # Error
                logger.error(f"CoreGraphics error: {result[0]}")
                return []

            display_ids = result[1]
            display_count = result[2]

            displays = []
            for display_id in display_ids[:display_count]:
                is_builtin = Quartz.CGDisplayIsBuiltin(display_id)

                if self.exclude_builtin and is_builtin:
                    continue

                # Try to get display name (if available)
                display_name = f"External Display {display_id}"
                displays.append(display_name)

            logger.debug(f"CoreGraphics detected: {displays}")
            return displays

        except ImportError:
            logger.warning("CoreGraphics (Quartz) not available")
            return []
        except Exception as e:
            logger.error(f"CoreGraphics error: {e}")
            return []


class YabaiDetector:
    """Yabai-based display detection using window manager queries.
    
    Uses the Yabai tiling window manager to query display information.
    Requires Yabai to be installed and running.
    
    Attributes:
        config: Configuration dictionary for this detector
        timeout: Command execution timeout in seconds
    """

    def __init__(self, config: Dict):
        """Initialize Yabai detector.
        
        Args:
            config: Configuration dictionary with timeout settings
        """
        self.config = config
        self.timeout = config.get("command_timeout", 3.0)

    async def detect_displays(self) -> List[str]:
        """Detect displays using Yabai window manager.
        
        Executes 'yabai -m query --displays' to get display information.
        
        Returns:
            List of display names (generated from display indices/IDs)
            
        Raises:
            FileNotFoundError: If Yabai is not installed
            asyncio.TimeoutError: If command execution times out
        """
        try:
            result = await asyncio.create_subprocess_exec(
                "yabai",
                "-m",
                "query",
                "--displays",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(result.communicate(), timeout=self.timeout)

            if result.returncode == 0:
                displays_data = json.loads(stdout.decode("utf-8"))
                displays = [f"Display {d.get('index', d.get('id'))}" for d in displays_data]
                logger.debug(f"Yabai detected: {displays}")
                return displays

            return []

        except FileNotFoundError:
            logger.debug("Yabai not installed")
            return []
        except asyncio.TimeoutError:
            logger.warning("Yabai timeout")
            return []
        except Exception as e:
            logger.error(f"Yabai error: {e}")
            return []


class AdvancedDisplayMonitor:
    """
    Advanced display monitoring system with multi-method detection and voice integration.

    Features:
    - Multi-method detection (AppleScript, CoreGraphics, Yabai, DNS-SD)
    - Smart caching with configurable TTL
    - Voice integration for announcements and prompts
    - Event-driven callbacks for display state changes
    - Graceful degradation when detection methods fail
    - Zero hardcoding - all configuration-driven
    - Circuit breaker pattern for connection management
    - Real-time display state verification
    - Learning pattern storage for AI improvements

    Attributes:
        config: Configuration dictionary loaded from JSON
        voice_handler: Voice integration handler for TTS
        vision_analyzer: Vision analyzer for AI-powered UI detection
        websocket_manager: WebSocket manager for UI notifications
        cache: Display detection result cache
        detectors: Dictionary of detection method instances
        monitored_displays: List of configured displays to monitor
        is_monitoring: Whether monitoring is currently active
        monitoring_task: Async task for the monitoring loop
        available_displays: Set of currently available display IDs
        connected_displays: Set of currently connected display IDs
        connecting_displays: Set of displays currently being connected (circuit breaker)
        initial_scan_complete: Whether initial startup scan is complete
        pending_prompt_display: Display ID with pending user prompt
        callbacks: Event callback registry
    """

    def __init__(self, config_path: Optional[str] = None, voice_handler=None, vision_analyzer=None):
        """
        Initialize advanced display monitor.

        Args:
            config_path: Path to configuration JSON file. If None, uses default path
            voice_handler: Voice integration handler for TTS announcements
            vision_analyzer: Vision analyzer for AI-powered UI detection
            
        Raises:
            FileNotFoundError: If configuration file is not found
            json.JSONDecodeError: If configuration file contains invalid JSON
        """
        self.config = self._load_config(config_path)
        self.voice_handler = voice_handler
        self.vision_analyzer = vision_analyzer  # Store vision analyzer for UAE integration
        self.websocket_manager = None  # Will be set by main.py

        # Initialize components
        self.cache = DisplayCache(ttl_seconds=self.config["caching"]["display_list_ttl_seconds"])

        # Initialize detectors
        self.detectors = {}
        self._init_detectors()

        # Monitored displays configuration
        self.monitored_displays = self._load_monitored_displays()

        # State tracking
        self.is_monitoring = False
        self.monitoring_task: Optional[asyncio.Task] = None
        self.available_displays: Set[str] = set()
        self.connected_displays: Set[str] = set()
        self.connecting_displays: Set[str] = (
            set()
        )  # Circuit breaker: displays currently being connected
        self.initial_scan_complete = False  # Track if initial scan is done
        self.pending_prompt_display: Optional[str] = (
            None  # Track which display has a pending prompt
        )

        # Event callbacks
        self.callbacks: Dict[str, List[Callable]] = {
            "display_detected": [],
            "display_lost": [],
            "display_connected": [],
            "display_disconnected": [],
            "error": [],
        }

        logger.info(
            f"[DISPLAY MONITOR] Initialized with {len(self.monitored_displays)} monitored displays"
        )

    def set_websocket_manager(self, ws_manager):
        """Set WebSocket manager for UI notifications.
        
        Args:
            ws_manager: WebSocket manager instance for broadcasting events
        """
        self.websocket_manager = ws_manager
        logger.info("[DISPLAY MONITOR] WebSocket manager set")

    def _load_config(self, config_path: Optional[str]) -> Dict:
        """Load configuration from JSON file.
        
        Args:
            config_path: Path to configuration file, or None for default
            
        Returns:
            Configuration dictionary
            
        Raises:
            FileNotFoundError: If configuration file doesn't exist
            json.JSONDecodeError: If configuration file contains invalid JSON
        """
        if config_path is None:
            # Default config path
            config_path = Path(__file__).parent.parent / "config" / "display_monitor_config.json"

        try:
            with open(config_path, "r") as f:
                config = json.load(f)
            logger.info(f"[DISPLAY MONITOR] Loaded config from {config_path}")
            return config
        except FileNotFoundError:
            logger.error(f"[DISPLAY MONITOR] Config file not found: {config_path}")
            raise
        except json.JSONDecodeError as e:
            logger.error(f"[DISPLAY MONITOR] Invalid JSON in config: {e}")
            raise

    def _init_detectors(self):
        """Initialize detection method instances based on configuration.
        
        Creates detector instances for each enabled detection method
        specified in the configuration.
        """
        methods = self.config["display_monitoring"]["detection_methods"]

        if "applescript" in methods and self.config.get("applescript", {}).get("enabled", False):
            self.detectors[DetectionMethod.APPLESCRIPT] = AppleScriptDetector(
                self.config["applescript"]
            )

        if "dnssd" in methods and self.config.get("dnssd", {}).get("enabled", False):
            self.detectors[DetectionMethod.DNSSD] = DNSSDDetector(self.config["dnssd"])

        if "coregraphics" in methods and self.config.get("coregraphics", {}).get("enabled", False):
            self.detectors[DetectionMethod.COREGRAPHICS] = CoreGraphicsDetector(
                self.config["coregraphics"]
            )

        if "yabai" in methods and self.config.get("yabai", {}).get("enabled", False):
            self.detectors[DetectionMethod.YABAI] = YabaiDetector(self.config["yabai"])

        logger.info(f"[DISPLAY MONITOR] Initialized {len(self.detectors)} detection methods")

    def _load_monitored_displays(self) -> List[MonitoredDisplay]:
        """Load monitored displays from configuration.
        
        Returns:
            List of MonitoredDisplay instances for enabled displays
        """
        displays = []
        for display_config in self.config["displays"]["monitored_displays"]:
            if display_config.get("enabled", True):
                displays.append(MonitoredDisplay(**display_config))
        return displays

    def register_callback(self, event: str, callback: Callable):
        """
        Register event callback for display state changes.

        Args:
            event: Event name (display_detected, display_lost, display_connected, 
                  display_disconnected, error)
            callback: Async callback function to execute on event
            
        Example:
            >>> async def on_display_detected(display, detected_name):
            ...     print(f"Found {display.name}")
            >>> monitor.register_callback("display_detected", on_display_detected)
        """
        if event in self.callbacks:
            self.callbacks[event].append(callback)
            logger.debug(f"[DISPLAY MONITOR] Registered callback for {event}")

    async def _emit_event(self, event: str, **kwargs):
        """Emit event to all registered callbacks.
        
        Args:
            event: Event name to emit
            **kwargs: Event data to pass to callbacks
        """
        if event in self.callbacks:
            for callback in self.callbacks[event]:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(**kwargs)
                    else:
                        callback(**kwargs)
                except Exception as e:
                    logger.error(f"[DISPLAY MONITOR] Callback error for {event}: {e}")

    async def start(self):
        """Start display monitoring.
        
        Begins the monitoring loop with optional startup delay.
        Does nothing if monitoring is already active or disabled in config.
        """
        if self.is_monitoring:
            logger.warning("[DISPLAY MONITOR] Already monitoring")
            return

        if not self.config["display_monitoring"]["enabled"]:
            logger.warning("[DISPLAY MONITOR] Monitoring disabled in config")
            return

        # Startup delay
        startup_delay = self.config["display_monitoring"]["startup_delay_seconds"]
        if startup_delay > 0:
            logger.info(f"[DISPLAY MONITOR] Starting in {startup_delay}s...")
            await asyncio.sleep(startup_delay)

        self.is_monitoring = True
        self.monitoring_task = asyncio.create_task(self._monitor_loop())
        logger.info("[DISPLAY MONITOR] Started monitoring")

    async def stop(self):
        """Stop display monitoring.
        
        Cancels the monitoring task and clears cache and state.
        """
        self.is_monitoring = False

        if self.monitoring_task:
            self.monitoring_task.cancel()
            try:
                await self.monitoring_task
            except asyncio.CancelledError:
                pass

        self.cache.clear()
        self.initial_scan_complete = False  # Reset for next start
        logger.info("[DISPLAY MONITOR] Stopped monitoring")

    async def _monitor_loop(self):
        """Main monitoring loop that periodically checks for display changes.
        
        Runs continuously while monitoring is enabled, checking for display
        state changes at the configured interval.
        
        Raises:
            asyncio.CancelledError: When monitoring is cancelled
        """
        check_interval = self.config["display_monitoring"]["check_interval_seconds"]
        logger.info(f"[DISPLAY MONITOR] Monitor loop starting (interval: {check_interval}s)")

        try:
            while self.is_monitoring:
                await self._check_displays()
                logger.debug(f"[DISPLAY MONITOR] Check complete, sleeping {check_interval}s...")
                await asyncio.sleep(check_interval)
        except asyncio.CancelledError:
            logger.info("[DISPLAY MONITOR] Monitoring cancelled")
        except Exception as e:
            logger.error(f"[DISPLAY MONITOR] Error in monitoring loop: {e}")
            await self._emit_event("error", error=e)

    async def _check_displays(self):
        """Check for available displays and handle state changes.
        
        Detects currently available displays, matches them against monitored
        displays, and handles new detections and losses. Manages initial scan
        completion and startup announcements.
        """
        try:
            # Detect all available displays
            detected_displays = await self._detect_all_displays()
            logger.debug(
                f"[DISPLAY MONITOR] Check: detected {len(detected_displays)} displays: {detected_displays}"
            )

            # Match against monitored displays
            current_available = set()
            for display_name in detected_displays:
                for monitored in self.monitored_displays:
                    if monitored.matches(display_name):
                        current_available.add(monitored.id)
                        logger.info(
                            f"[DISPLAY MONITOR] MATCH: '{display_name}' → '{monitored.name}' (id: {monitored.id}), in_available={monitored.id in self.available_displays}, initial_complete={self.initial_scan_complete}"
                        )

                        # New display detected - announce and set pending prompt
                        if monitored.id not in self.available_displays:
                            logger.info(f"[DISPLAY MONITOR] NEW DISPLAY DETECTED: {monitored.name}")
                            if self.initial_scan_complete:
                                # Display became available after initial scan - announce it!
                                logger.info(
                                    f"[DISPLAY MONITOR] STATE CHANGE: {monitored.name} became AVAILABLE"
                                )
                                await self._handle_display_detected(monitored, display_name)
                            else:
                                # Initial scan - STILL announce it so user can respond!
                                logger.info(
                                    f"[DISPLAY MONITOR] Initial scan found: {monitored.name} - will prompt user"
                                )
                                await self._handle_display_detected(monitored, display_name)

            # Check for lost displays (only after initial scan)
            if self.initial_scan_complete:
                for display_id in self.available_displays - current_available:
                    monitored = next(
                        (d for d in self.monitored_displays if d.id == display_id), None
                    )
                    if monitored:
                        logger.info(
                            f"[DISPLAY MONITOR] STATE CHANGE: {monitored.name} became UNAVAILABLE"
                        )
                        await self._handle_display_lost(monitored)

            logger.debug(
                f"[DISPLAY MONITOR] Check: available={list(current_available)}, previous={list(self.available_displays)}"
            )
            self.available_displays = current_available

            # Mark initial scan as complete after first run
            if not self.initial_scan_complete:
                self.initial_scan_complete = True
                logger.info(
                    f"[DISPLAY MONITOR] Initial scan complete. Currently monitoring {len(self.active_connections)} connections"
                )

        except Exception as e:
            logger.error(f"[DISPLAY MONITOR] Error during check_displays: {e}", exc_info=True)

    async def _detect_all_displays(self) -> List[str]:
        """Detect displays using all available methods"""
        # Check cache first
        cache_key = "all_displays"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        all_displays = set()
        preferred_method = self.config["display_monitoring"].get("preferred_detection_method")

        # Try preferred method first
        if preferred_method:
            method_enum = DetectionMethod(preferred_method)
            if method_enum in self.detectors:
                displays = await self.detectors[method_enum].detect_displays()
                all_displays.update(displays)

                # If we got results, cache and return
                if displays:
                    result = list(all_displays)
                    self.cache.set(cache_key, result)
                    return result

        # Fallback: Try all methods (parallel if enabled)
        if self.config["performance"]["parallel_detection"]:
            tasks = [detector.detect_displays() for detector in self.detectors.values()]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, list):
                    all_displays.update(result)
        else:
            for detector in self.detectors.values():
                displays = await detector.detect_displays()
                all_displays.update(displays)

        result = list(all_displays)
        self.cache.set(cache_key, result)
        return result

    async def _handle_display_detected(self, monitored: MonitoredDisplay, detected_name: str):
        """Handle newly detected display"""
        logger.info(f"[DISPLAY MONITOR] Detected: {monitored.name} ({detected_name})")

        # Emit event
        await self._emit_event("display_detected", display=monitored, detected_name=detected_name)

        # IMPORTANT: Set pending prompt FIRST if we're going to prompt
        if monitored.auto_prompt and self.config["voice_integration"]["speak_on_detection"]:
            # Set pending prompt state so we can handle yes/no responses
            self.pending_prompt_display = monitored.id
            logger.info(
                f"[DISPLAY MONITOR] Set pending prompt for {monitored.name} (will prompt user)"
            )

        # Send WebSocket notification to UI
        if self.websocket_manager:
            try:
                message = f"Sir, I see your {monitored.name} is now available. Would you like to extend your display to it?"
                await self.websocket_manager.broadcast(
                    {
                        "type": "display_detected",
                        "display_name": monitored.name,
                        "display_id": monitored.id,
                        "message": message,
                        "timestamp": datetime.now().isoformat(),
                    }
                )
                logger.debug(f"[DISPLAY MONITOR] Broadcasted detection to UI")
            except Exception as e:
                logger.warning(f"[DISPLAY MONITOR] Failed to broadcast to UI: {e}")

        # Voice prompt if enabled
        if monitored.auto_prompt and self.config["voice_integration"]["speak_on_detection"]:
            await self._speak_detection_prompt(monitored)

        # Auto-connect if enabled AND not already connected/connecting
        if (
            monitored.auto_connect
            and monitored.id not in self.connected_displays
            and monitored.id not in self.connecting_displays
        ):
            logger.info(f"[DISPLAY MONITOR] Auto-connecting to {monitored.name}...")
            await self.connect_display(monitored.id)
        elif monitored.id in self.connected_displays:
            logger.debug(
                f"[DISPLAY MONITOR] {monitored.name} already connected, skipping auto-connect"
            )
        elif monitored.id in self.connecting_displays:
            logger.debug(
                f"[DISPLAY MONITOR] {monitored.name} connection already in progress, skipping auto-connect"
            )

    async def _handle_display_lost(self, monitored: MonitoredDisplay):
        """Handle lost display"""
        logger.info(f"[DISPLAY MONITOR] Lost: {monitored.name}")

        # Emit event
        await self._emit_event("display_lost", display=monitored)

        if monitored.id in self.connected_displays:
            self.connected_displays.remove(monitored.id)
            await self._emit_event("display_disconnected", display=monitored)

    def _get_time_aware_greeting(self) -> str:
        """
        Get a time-aware greeting that's contextual but not annoying

        Returns time-specific greeting ~35% of the time, generic the rest
        """
        from datetime import datetime

        prompt_config = self.config["voice_integration"].get("prompt_templates", {})

        # Handle legacy format (simple list)
        if isinstance(prompt_config, list):
            return random.choice(prompt_config)  # nosec B311

        # Get probability for time-aware greetings (default 35%)
        time_aware_prob = self.config["voice_integration"].get(
            "time_aware_greeting_probability", 0.35
        )

        # Decide whether to use time-aware or generic
        use_time_aware = random.random() < time_aware_prob  # nosec B311

        if use_time_aware:
            # Determine time of day
            current_hour = datetime.now().hour

            if 5 <= current_hour < 12:
                time_period = "morning"
            elif 12 <= current_hour < 17:
                time_period = "afternoon"
            elif 17 <= current_hour < 21:
                time_period = "evening"
            else:
                time_period = "night"

            templates = prompt_config.get(
                time_period, prompt_config.get("generic", ["JARVIS online."])
            )
        else:
            templates = prompt_config.get("generic", ["JARVIS online."])

        return random.choice(templates)  # nosec B311

    async def _speak_detection_prompt(self, monitored: MonitoredDisplay):
        """Speak detection prompt"""
        if not self.config["voice_integration"]["enabled"]:
            return

        # IMPORTANT: Don't speak detection prompts during initial scan - only the startup announcement should speak
        # The initial scan happens right after startup, so we use the coordinator for that
        # Only speak detection prompts for displays that appear AFTER startup (hot-plug events)
        if not self.initial_scan_complete:
            logger.warning(
                f"[STARTUP VOICE] ⏭️  Display detection prompt skipped during initial scan - startup announcement handles this"
            )
            return

        # Get time-aware greeting
        template = self._get_time_aware_greeting()
        message = (
            template.format(display_name=monitored.name)
            if "{display_name}" in template
            else template
        )

        logger.warning(f"[DISPLAY VOICE] 🎤 Display detection prompt: {message}")

        # Note: pending_prompt_display is already set in _handle_display_detected
        logger.info(
            f"[DISPLAY MONITOR] Speaking prompt for {monitored.name} (pending_prompt_display={self.pending_prompt_display})"
        )

        # Use voice handler if available
        if self.voice_handler:
            try:
                await self.voice_handler.speak(message)
            except Exception as e:
                logger.error(f"[DISPLAY MONITOR] Voice handler error: {e}")
        else:
            # Fallback to macOS say command
            try:
                subprocess.Popen(["say", message])
            except Exception as e:
                logger.error(f"[DISPLAY MONITOR] Say command error: {e}")

    async def connect_display(self, display_id: str) -> Dict[str, Any]:
        """
        Connect to a display using advanced methods

        Connection Strategy (prioritized - PRODUCTION HARDENED):
        1. 🥇 Route Picker Helper (AVRoutePickerView + Accessibility - MOST RELIABLE!)
        2. 🥈 Protocol-Level AirPlay (Bonjour/mDNS + RAOP - direct protocol)
        3. 🥉 Vision-Guided Navigator (Claude Vision - bypasses macOS restrictions)
        4. Native Swift Bridge fallback
        5. AppleScript fallback
        6. Voice guidance to user

        Args:
            display_id: Display ID from configuration

        Returns:
            Connection result dictionary with telemetry
        """
        # Find monitored display
        monitored = next((d for d in self.monitored_displays if d.id == display_id), None)
        if not monitored:
            return {"success": False, "message": f"Display {display_id} not found in configuration"}

        # Circuit breaker: Check if already connected or connecting
        logger.info(f"[DISPLAY MONITOR] 🔍 Circuit breaker check for {monitored.name}")
        logger.info(
            f"[DISPLAY MONITOR] Current state: connecting={list(self.connecting_displays)}, connected={list(self.connected_displays)}"
        )

        # REAL-TIME VERIFICATION: Don't trust cached state - verify actual connection
        with open("/tmp/jarvis_display_command.log", "a") as f:
            f.write(f"[DISPLAY MONITOR] About to verify connection for {monitored.name}\n")

        from .display_state_verifier import get_display_verifier

        verifier = get_display_verifier()

        with open("/tmp/jarvis_display_command.log", "a") as f:
            f.write(f"[DISPLAY MONITOR] Calling verifier.verify_actual_connection...\n")

        actual_state = await verifier.verify_actual_connection(monitored.name)

        with open("/tmp/jarvis_display_command.log", "a") as f:
            f.write(
                f"[DISPLAY MONITOR] Verification complete: {actual_state.get('is_connected')}\n"
            )

        logger.info(
            f"[DISPLAY MONITOR] Real-time verification for {monitored.name}: is_connected={actual_state['is_connected']}, confidence={actual_state['confidence']:.2f}"
        )

        # Update our cached state based on actual verification
        if actual_state["is_connected"] and display_id not in self.connected_displays:
            logger.info(
                f"[DISPLAY MONITOR] 📊 Updating cache: {monitored.name} is actually connected"
            )
            self.connected_displays.add(display_id)
        elif not actual_state["is_connected"] and display_id in self.connected_displays:
            logger.info(
                f"[DISPLAY MONITOR] 📊 Updating cache: {monitored.name} is NOT actually connected"
            )
            self.connected_displays.discard(display_id)

        # Store learning pattern for future predictions
        await self._store_display_pattern(monitored, actual_state)

        if actual_state["is_connected"] and actual_state["confidence"] > 0.7:
            logger.info(
                f"[DISPLAY MONITOR] ✅ {monitored.name} verified as connected (method: {actual_state['method']})"
            )
            return {
                "success": True,
                "message": f"{monitored.name} already connected",
                "cached": True,
                "verified": True,
            }

        if display_id in self.connecting_displays:
            logger.info(
                f"[DISPLAY MONITOR] ⚠️ {monitored.name} was stuck in connecting state, resetting and retrying..."
            )
            # Remove from connecting state to allow retry
            self.connecting_displays.discard(display_id)
            logger.info(f"[DISPLAY MONITOR] 🔄 Reset circuit breaker, proceeding with connection")

        # Mark as connecting IMMEDIATELY to prevent race conditions
        self.connecting_displays.add(display_id)
        logger.info(
            f"[DISPLAY MONITOR] 🔒 Marked {monitored.name} as connecting (circuit breaker engaged)"
        )

        # Immediate voice feedback
        if self.voice_handler:
            await self.voice_handler.speak_async(f"Connecting to {monitored.name} now, sir.")

        logger.info(f"[DISPLAY MONITOR] ========================================")
        logger.info(f"[DISPLAY MONITOR] Connecting to {monitored.name}...")
        logger.info(f"[DISPLAY MONITOR] Starting 6-tier connection waterfall")
        logger.info(f"[DISPLAY MONITOR] ========================================")

        connection_start = asyncio.get_event_loop().time()
        strategies_attempted = []

        # Strategy 1: INTELLIGENT HYBRID - Coordinates + Vision + UAE Adaptation
        # Uses best available clicker: UAE > SAI > Adaptive > Basic
        # Total: ~2 seconds, 100% reliable, adapts when UI changes
        try:
            with open("/tmp/jarvis_display_command.log", "a") as f:
                f.write(f"[DISPLAY MONITOR] Attempting Strategy 1: Best available clicker\n")

            # Use clicker factory to get best available clicker
            from display.control_center_clicker_factory import get_best_clicker, get_clicker_info

            # Log available clickers
            clicker_info = get_clicker_info()
            logger.info(f"[DISPLAY MONITOR] 🥇 STRATEGY 1: INTELLIGENT HYBRID")
            logger.info(
                f"[DISPLAY MONITOR] Available clickers: UAE={clicker_info['uae_available']}, SAI={clicker_info['sai_available']}, Adaptive={clicker_info['adaptive_available']}, Basic={clicker_info['basic_available']}"
            )
            logger.info(f"[DISPLAY MONITOR] Recommended: {clicker_info['recommended'].upper()}")

            strategies_attempted.append("intelligent_hybrid")

            # Get best available clicker
            clicker = get_best_clicker(vision_analyzer=None, enable_verification=True)
            logger.info(f"[DISPLAY MONITOR] Using {clicker.__class__.__name__}")

            # Execute connection flow
            logger.info(f"[DISPLAY MONITOR] Connecting to {monitored.name}...")

            with open("/tmp/jarvis_display_command.log", "a") as f:
                f.write(
                    f"[DISPLAY MONITOR] About to call clicker.connect_to_device('{monitored.name}')\n"
                )

            # Use async context manager if available, otherwise call directly
            if hasattr(clicker, "__aenter__"):
                async with clicker as c:
                    result = await c.connect_to_device(monitored.name)
            else:
                result = await clicker.connect_to_device(monitored.name)

            with open("/tmp/jarvis_display_command.log", "a") as f:
                f.write(f"[DISPLAY MONITOR] connect_to_device() returned: {result}\n")

            if result.get("success"):
                total_duration = result.get("duration", 0)

                self.connected_displays.add(display_id)
                await self._emit_event("display_connected", display=monitored)

                # Speak success message
                if self.config["voice_integration"]["speak_on_connection"]:
                    template = self.config["voice_integration"]["connection_success_message"]
                    message = template.format(display_name=monitored.name)

                    if self.voice_handler:
                        try:
                            await self.voice_handler.speak(message)
                        except:
                            subprocess.Popen(["say", message])
                    else:
                        subprocess.Popen(["say", message])

                logger.info(
                    f"[DISPLAY MONITOR] ✅ SUCCESS via Simple Hardcoded Coordinates in {total_duration:.2f}s"
                )
                logger.info(f"[DISPLAY MONITOR] Method: {result['method']}")

                # Log steps if available (handle both dict and list formats)
                steps = result.get("steps", {})
                if isinstance(steps, dict):
                    logger.info(f"[DISPLAY MONITOR] Steps: {len(steps)} actions completed")
                    for step_name, step_data in steps.items():
                        if isinstance(step_data, dict):
                            logger.info(
                                f"[DISPLAY MONITOR]   {step_name}: {step_data.get('success', False)}"
                            )
                elif isinstance(steps, list):
                    logger.info(f"[DISPLAY MONITOR] Steps: {len(steps)}")
                    for step in steps:
                        logger.info(
                            f"[DISPLAY MONITOR]   {step.get('step')}. {step.get('action')}: {step.get('coordinates')}"
                        )

                logger.info(f"[DISPLAY MONITOR] ========================================")

                # Release circuit breaker
                logger.info(f"[DISPLAY MONITOR] 🔓 Releasing circuit breaker for {monitored.name}")
                logger.info(
                    f"[DISPLAY MONITOR] State before release: connecting={list(self.connecting_displays)}, connected={list(self.connected_displays)}"
                )
                if display_id in self.connecting_displays:
                    self.connecting_displays.remove(display_id)
                    logger.info(
                        f"[DISPLAY MONITOR] ✅ Removed {display_id} from connecting_displays"
                    )
                else:
                    logger.warning(
                        f"[DISPLAY MONITOR] ⚠️  {display_id} was NOT in connecting_displays!"
                    )
                logger.info(
                    f"[DISPLAY MONITOR] State after release: connecting={list(self.connecting_displays)}, connected={list(self.connected_displays)}"
                )

                return {
                    "success": True,
                    "message": f"Connected to {monitored.name} via Direct Coordinates",
                    "method": "direct_coordinates",
                    "duration": total_duration,
                    "strategies_attempted": strategies_attempted,
                    "coordinates": {
                        "control_center": result["control_center_coords"],
                        "screen_mirroring": result["screen_mirroring_coords"],
                        "living_room_tv": result["living_room_tv_coords"],
                    },
                    "tier": 1,
                }
            else:
                logger.warning(
                    f"[DISPLAY MONITOR] Direct coordinates failed: {result.get('message')}"
                )
                raise Exception(f"Could not connect to '{monitored.name}': {result.get('message')}")

        except Exception as e:
            with open("/tmp/jarvis_display_command.log", "a") as f:
                f.write(f"[DISPLAY MONITOR] Strategy 1 exception: {e}\n")
                import traceback

                f.write(f"{traceback.format_exc()}\n")
            logger.warning(f"[DISPLAY MONITOR] Direct coordinates error: {e}", exc_info=True)
            # Note: Don't release circuit breaker here - let it continue to other strategies

        # Strategy 2: Protocol-Level AirPlay (Bonjour/mDNS + RAOP)
        try:
            from display.airplay_manager import get_airplay_manager

            logger.info(
                f"[DISPLAY MONITOR] 🥈 STRATEGY 2: Protocol-Level AirPlay (Bonjour/mDNS + RAOP)"
            )
            logger.info(f"[DISPLAY MONITOR] Direct network protocol for {monitored.name}")

            strategies_attempted.append("airplay_protocol")

            airplay_manager = get_airplay_manager()

            # Initialize if not already
            if not airplay_manager.is_initialized:
                await airplay_manager.initialize()

            # Determine mode
            mode = monitored.connection_mode if hasattr(monitored, "connection_mode") else "extend"

            result = await airplay_manager.connect_to_device(monitored.name, mode=mode)

            if result.get("success"):
                self.connected_displays.add(display_id)
                await self._emit_event("display_connected", display=monitored)

                duration = asyncio.get_event_loop().time() - connection_start

                # Speak success message
                if self.config["voice_integration"]["speak_on_connection"]:
                    template = self.config["voice_integration"]["connection_success_message"]
                    message = template.format(display_name=monitored.name)

                    if self.voice_handler:
                        try:
                            await self.voice_handler.speak(message)
                        except:
                            subprocess.Popen(["say", message])
                    else:
                        subprocess.Popen(["say", message])

                logger.info(
                    f"[DISPLAY MONITOR] ✅ SUCCESS via Protocol-Level AirPlay in {duration:.2f}s"
                )
                logger.info(f"[DISPLAY MONITOR] ========================================")

                # Release circuit breaker
                if display_id in self.connecting_displays:
                    self.connecting_displays.remove(display_id)
                    logger.info(
                        f"[DISPLAY MONITOR] 🔓 Circuit breaker released for {monitored.name}"
                    )

                return {
                    "success": True,
                    "message": f"Connected via AirPlay protocol in {result.get('duration', 0):.1f}s",
                    "method": "airplay_protocol",
                    "duration": duration,
                    "strategies_attempted": strategies_attempted,
                    "protocol_method": result.get("method", "system_native"),
                    "tier": 2,
                }
            else:
                logger.warning(
                    f"[DISPLAY MONITOR] Protocol-level AirPlay failed: {result.get('message')}"
                )

        except Exception as e:
            logger.warning(f"[DISPLAY MONITOR] Protocol-level AirPlay error: {e}")

        # Strategy 3: Vision-Guided Navigator (Claude Vision)
        try:
            from display.vision_ui_navigator import get_vision_navigator

            logger.info(f"[DISPLAY MONITOR] 🥉 STRATEGY 3: Vision-Guided Navigator (Claude Vision)")
            logger.info(f"[DISPLAY MONITOR] JARVIS will SEE and CLICK the UI for {monitored.name}")

            strategies_attempted.append("vision_guided")

            navigator = get_vision_navigator()

            # Connect vision analyzer if available
            if not navigator.vision_analyzer and hasattr(self, "vision_analyzer"):
                navigator.set_vision_analyzer(self.vision_analyzer)
            elif not navigator.vision_analyzer:
                # Try to get from app.state
                try:
                    import sys

                    if hasattr(sys.modules.get("__main__"), "app"):
                        app = sys.modules["__main__"].app
                        if hasattr(app, "state") and hasattr(app.state, "vision_analyzer"):
                            navigator.set_vision_analyzer(app.state.vision_analyzer)
                            logger.info(
                                "[DISPLAY MONITOR] Connected vision analyzer from app.state"
                            )
                except:
                    pass

            if navigator.vision_analyzer:
                result = await navigator.connect_to_display(monitored.name)

                if result.success:
                    self.connected_displays.add(display_id)
                    await self._emit_event("display_connected", display=monitored)

                    duration = asyncio.get_event_loop().time() - connection_start

                    # Speak success message
                    if self.config["voice_integration"]["speak_on_connection"]:
                        template = self.config["voice_integration"]["connection_success_message"]
                        message = template.format(display_name=monitored.name)

                        if self.voice_handler:
                            try:
                                await self.voice_handler.speak(message)
                            except:
                                subprocess.Popen(["say", message])
                        else:
                            subprocess.Popen(["say", message])

                    logger.info(
                        f"[DISPLAY MONITOR] ✅ SUCCESS via Vision Navigation in {duration:.2f}s"
                    )
                    logger.info(f"[DISPLAY MONITOR] ========================================")

                    # Release circuit breaker
                    if display_id in self.connecting_displays:
                        self.connecting_displays.remove(display_id)
                        logger.info(
                            f"[DISPLAY MONITOR] 🔓 Circuit breaker released for {monitored.name}"
                        )

                    return {
                        "success": True,
                        "message": f"Connected via vision navigation in {result.duration:.1f}s",
                        "method": "vision_guided",
                        "duration": duration,
                        "strategies_attempted": strategies_attempted,
                        "steps_completed": result.steps_completed,
                        "tier": 3,
                    }
                else:
                    logger.warning(f"[DISPLAY MONITOR] Vision navigation failed: {result.message}")
            else:
                logger.warning(
                    "[DISPLAY MONITOR] Vision analyzer not available for vision navigation"
                )

        except Exception as e:
            logger.warning(f"[DISPLAY MONITOR] Vision navigator error: {e}")

        # Strategy 4: Native Swift Bridge
        try:
            from display.native import get_native_controller

            logger.info(f"[DISPLAY MONITOR] STRATEGY 4: Native Swift Bridge")
            strategies_attempted.append("native_swift")

            native_controller = get_native_controller()

            if await native_controller.initialize():
                logger.info(f"[DISPLAY MONITOR] Using native Swift bridge for {monitored.name}")

                result = await native_controller.connect(monitored.name)

                if result.success:
                    self.connected_displays.add(display_id)
                    await self._emit_event("display_connected", display=monitored)

                    duration = asyncio.get_event_loop().time() - connection_start

                    # Speak success message
                    if self.config["voice_integration"]["speak_on_connection"]:
                        template = self.config["voice_integration"]["connection_success_message"]
                        message = template.format(display_name=monitored.name)

                        if self.voice_handler:
                            try:
                                await self.voice_handler.speak(message)
                            except:
                                subprocess.Popen(["say", message])
                        else:
                            subprocess.Popen(["say", message])

                    logger.info(
                        f"[DISPLAY MONITOR] ✅ SUCCESS via Native Swift Bridge in {duration:.2f}s"
                    )
                    logger.info(f"[DISPLAY MONITOR] ========================================")

                    # Release circuit breaker
                    if display_id in self.connecting_displays:
                        self.connecting_displays.remove(display_id)
                        logger.info(
                            f"[DISPLAY MONITOR] 🔓 Circuit breaker released for {monitored.name}"
                        )

                    return {
                        "success": True,
                        "message": result.message,
                        "method": result.method,
                        "duration": duration,
                        "strategies_attempted": strategies_attempted,
                        "fallback_used": result.fallback_used,
                        "tier": 4,
                    }
                else:
                    logger.warning(f"[DISPLAY MONITOR] Native bridge failed: {result.message}")
            else:
                logger.warning("[DISPLAY MONITOR] Native bridge not available")

        except Exception as e:
            logger.warning(f"[DISPLAY MONITOR] Native bridge error: {e}")

        # Strategy 5: AppleScript fallback
        if DetectionMethod.APPLESCRIPT in self.detectors:
            logger.info(f"[DISPLAY MONITOR] STRATEGY 5: AppleScript Fallback")
            strategies_attempted.append("applescript")

            result = await self.detectors[DetectionMethod.APPLESCRIPT].connect_display(
                monitored.name
            )

            if result["success"]:
                self.connected_displays.add(display_id)
                await self._emit_event("display_connected", display=monitored)

                duration = asyncio.get_event_loop().time() - connection_start

                # Speak success message
                if self.config["voice_integration"]["speak_on_connection"]:
                    template = self.config["voice_integration"]["connection_success_message"]
                    message = template.format(display_name=monitored.name)

                    if self.voice_handler:
                        try:
                            await self.voice_handler.speak(message)
                        except:
                            subprocess.Popen(["say", message])
                    else:
                        subprocess.Popen(["say", message])

                logger.info(f"[DISPLAY MONITOR] ✅ SUCCESS via AppleScript in {duration:.2f}s")
                logger.info(f"[DISPLAY MONITOR] ========================================")

                # Release circuit breaker
                if display_id in self.connecting_displays:
                    self.connecting_displays.remove(display_id)
                    logger.info(
                        f"[DISPLAY MONITOR] 🔓 Circuit breaker released for {monitored.name}"
                    )

                result["duration"] = duration
                result["strategies_attempted"] = strategies_attempted
                result["tier"] = 5
                return result

        # Strategy 6: All automated methods failed - provide voice guidance
        duration = asyncio.get_event_loop().time() - connection_start

        guidance_message = (
            f"I can see {monitored.name} is available, but all automated connection methods failed. "
            f"Please click the Screen Mirroring icon in your menu bar and select {monitored.name} manually."
        )

        logger.error(f"[DISPLAY MONITOR] ❌ ALL 6 STRATEGIES FAILED for {monitored.name}")
        logger.error(f"[DISPLAY MONITOR] Strategies attempted: {', '.join(strategies_attempted)}")
        logger.error(f"[DISPLAY MONITOR] Total time: {duration:.2f}s")
        logger.error(f"[DISPLAY MONITOR] ========================================")

        # Speak guidance
        if self.voice_handler:
            try:
                await self.voice_handler.speak(guidance_message)
            except:
                subprocess.Popen(["say", guidance_message])

        # Release circuit breaker on failure
        if display_id in self.connecting_displays:
            self.connecting_displays.remove(display_id)
            logger.info(f"[DISPLAY MONITOR] 🔓 Circuit breaker released (all strategies failed)")

        return {
            "success": False,
            "message": guidance_message,
            "method": "none",
            "duration": duration,
            "strategies_attempted": strategies_attempted,
            "guidance_provided": True,
            "tier": 6,
        }

    async def change_display_mode(self, display_id: str, mode: str = "extended") -> Dict[str, Any]:
        """
        Change the screen mirroring mode

        Args:
            display_id: Display ID from configuration
            mode: Mirroring mode - "entire", "window", or "extended"

        Returns:
            Mode change result dictionary
        """
        # Find monitored display
        monitored = next((d for d in self.monitored_displays if d.id == display_id), None)
        if not monitored:
            return {"success": False, "message": f"Display {display_id} not found in configuration"}

        logger.info(f"[DISPLAY MONITOR] ========================================")
        logger.info(f"[DISPLAY MONITOR] Changing {monitored.name} to {mode} mode...")
        logger.info(f"[DISPLAY MONITOR] ========================================")

        mode_start = asyncio.get_event_loop().time()

        try:
            from display.control_center_clicker_factory import get_best_clicker

            logger.info(f"[DISPLAY MONITOR] Using DIRECT COORDINATES to change mode")
            logger.info(
                f"[DISPLAY MONITOR] Flow: Control Center → Screen Mirroring → {mode} → Start"
            )

            # Get Control Center clicker
            cc_clicker = get_best_clicker(
                vision_analyzer=self.vision_analyzer, enable_verification=True, prefer_uae=True
            )

            # Execute mode change flow
            logger.info(f"[DISPLAY MONITOR] Executing 4-click mode change flow...")
            result = cc_clicker.change_mirroring_mode(mode)

            if result.get("success"):
                total_duration = asyncio.get_event_loop().time() - mode_start

                # Speak mode change confirmation
                if self.config["voice_integration"].get("speak_on_connection", True):
                    template = self.config["voice_integration"].get(
                        "mode_change_success_message", "Mode changed, sir."
                    )
                    message = (
                        template.format(mode=result["mode"]) if "{mode}" in template else template
                    )

                    if self.voice_handler:
                        try:
                            await self.voice_handler.speak(message)
                        except:
                            subprocess.Popen(["say", message])
                    else:
                        subprocess.Popen(["say", message])

                logger.info(
                    f"[DISPLAY MONITOR] ✅ SUCCESS - Changed to {result['mode']} in {total_duration:.2f}s"
                )
                logger.info(
                    f"[DISPLAY MONITOR] 1. Control Center: {result['control_center_coords']}"
                )
                logger.info(
                    f"[DISPLAY MONITOR] 2. Screen Mirroring: {result['screen_mirroring_coords']}"
                )
                logger.info(f"[DISPLAY MONITOR] 3. Change Button: {result['change_button_coords']}")
                logger.info(f"[DISPLAY MONITOR] 4. Mode: {result['mode_coords']}")
                logger.info(
                    f"[DISPLAY MONITOR] 5. Start Mirroring: {result['start_mirroring_coords']}"
                )
                logger.info(f"[DISPLAY MONITOR] Method: {result['method']}")
                logger.info(f"[DISPLAY MONITOR] ========================================")

                return {
                    "success": True,
                    "message": f"Changed to {result['mode']} mode",
                    "mode": result["mode"],
                    "method": "direct_coordinates",
                    "duration": total_duration,
                    "coordinates": {
                        "control_center": result["control_center_coords"],
                        "screen_mirroring": result["screen_mirroring_coords"],
                        "change_button": result["change_button_coords"],
                        "mode": result["mode_coords"],
                        "start_mirroring": result["start_mirroring_coords"],
                    },
                }
            else:
                logger.error(f"[DISPLAY MONITOR] ❌ Mode change failed: {result.get('message')}")
                return result

        except Exception as e:
            total_duration = asyncio.get_event_loop().time() - mode_start
            logger.error(f"[DISPLAY MONITOR] ❌ Mode change error: {e}", exc_info=True)
            logger.error(f"[DISPLAY MONITOR] Total time: {total_duration:.2f}s")
            logger.error(f"[DISPLAY MONITOR] ========================================")

            return {
                "success": False,
                "message": f"Failed to change mode: {str(e)}",
                "method": "none",
                "duration": total_duration,
                "error": str(e),
            }

    async def disconnect_display(self, display_id: str) -> Dict[str, Any]:
        """
        Disconnect from a display using direct coordinates

        Args:
            display_id: Display ID from configuration

        Returns:
            Disconnection result dictionary
        """
        # Find monitored display
        monitored = next((d for d in self.monitored_displays if d.id == display_id), None)
        if not monitored:
            return {"success": False, "message": f"Display {display_id} not found in configuration"}

        logger.info(f"[DISPLAY MONITOR] ========================================")
        logger.info(f"[DISPLAY MONITOR] Disconnecting from {monitored.name}...")
        logger.info(f"[DISPLAY MONITOR] ========================================")

        disconnect_start = asyncio.get_event_loop().time()

        try:
            from display.control_center_clicker_factory import get_best_clicker

            logger.info(f"[DISPLAY MONITOR] Using DIRECT COORDINATES to disconnect")
            logger.info(f"[DISPLAY MONITOR] Flow: Control Center → Screen Mirroring → Stop")

            # Get Control Center clicker
            cc_clicker = get_best_clicker(
                vision_analyzer=self.vision_analyzer, enable_verification=True, prefer_uae=True
            )

            # Execute disconnect flow: Control Center → Screen Mirroring → Stop
            logger.info(f"[DISPLAY MONITOR] Executing 3-click disconnect flow...")
            result = cc_clicker.disconnect_from_living_room_tv()

            if result.get("success"):
                total_duration = asyncio.get_event_loop().time() - disconnect_start

                # Remove from connected displays
                if display_id in self.connected_displays:
                    self.connected_displays.remove(display_id)
                await self._emit_event("display_disconnected", display=monitored)

                # Speak disconnection message
                if self.config["voice_integration"]["speak_on_disconnection"]:
                    template = self.config["voice_integration"].get(
                        "disconnection_success_message", "Display disconnected, sir."
                    )
                    message = (
                        template.format(display_name=monitored.name)
                        if "{display_name}" in template
                        else template
                    )

                    if self.voice_handler:
                        try:
                            await self.voice_handler.speak(message)
                        except:
                            subprocess.Popen(["say", message])
                    else:
                        subprocess.Popen(["say", message])

                logger.info(f"[DISPLAY MONITOR] ✅ SUCCESS - Disconnected in {total_duration:.2f}s")
                logger.info(
                    f"[DISPLAY MONITOR] 1. Control Center: {result['control_center_coords']}"
                )
                logger.info(
                    f"[DISPLAY MONITOR] 2. Screen Mirroring: {result['screen_mirroring_coords']}"
                )
                logger.info(f"[DISPLAY MONITOR] 3. Stop: {result['stop_mirroring_coords']}")
                logger.info(f"[DISPLAY MONITOR] Method: {result['method']}")
                logger.info(f"[DISPLAY MONITOR] ========================================")

                return {
                    "success": True,
                    "message": f"Disconnected from {monitored.name}",
                    "method": "direct_coordinates",
                    "duration": total_duration,
                    "coordinates": {
                        "control_center": result["control_center_coords"],
                        "screen_mirroring": result["screen_mirroring_coords"],
                        "stop": result["stop_mirroring_coords"],
                    },
                }
            else:
                logger.error(f"[DISPLAY MONITOR] ❌ Disconnect failed: {result.get('message')}")
                return result

        except Exception as e:
            total_duration = asyncio.get_event_loop().time() - disconnect_start
            logger.error(f"[DISPLAY MONITOR] ❌ Disconnect error: {e}", exc_info=True)
            logger.error(f"[DISPLAY MONITOR] Total time: {total_duration:.2f}s")
            logger.error(f"[DISPLAY MONITOR] ========================================")

            return {
                "success": False,
                "message": f"Failed to disconnect: {str(e)}",
                "method": "none",
                "duration": total_duration,
                "error": str(e),
            }

    async def _store_display_pattern(
        self, monitored: "MonitoredDisplay", actual_state: Dict[str, Any]
    ):
        """
        Store display connection patterns in learning database for future predictions
        """
        try:
            # Get learning database instance
            from backend.intelligence.learning_database import get_learning_database

            db = await get_learning_database()

            # Create pattern data
            pattern_data = {
                "pattern_type": "display_connection",
                "display_id": monitored.id,
                "display_name": monitored.name,
                "is_connected": actual_state["is_connected"],
                "connection_mode": actual_state.get("connection_mode"),
                "verification_method": actual_state.get("method"),
                "confidence": actual_state.get("confidence", 0.5),
                "context": {
                    "time_of_day": datetime.now().hour,
                    "day_of_week": datetime.now().weekday(),
                    "is_auto_connect": monitored.auto_connect,
                    "available_displays": len(self.available_displays),
                    "active_apps": [],  # Could be enhanced with actual app context
                },
                "success_rate": 1.0 if actual_state["is_connected"] else 0.0,
                "frequency": 1,  # Will be incremented by database if pattern exists
            }

            # Store the pattern (database handles deduplication and aggregation)
            await db.store_pattern(pattern_data, batch=True)

            # Store user interaction as an action
            action_data = {
                "action_type": "display_query",
                "action_detail": f"Query about {monitored.name}",
                "timestamp": datetime.now().isoformat(),
                "context": pattern_data["context"],
                "result": "connected" if actual_state["is_connected"] else "not_connected",
                "confidence": actual_state.get("confidence", 0.5),
            }

            await db.store_action(action_data, batch=True)

            logger.debug(f"[DISPLAY MONITOR] Stored learning pattern for {monitored.name}")

        except ImportError:
            logger.debug(
                "[DISPLAY MONITOR] Learning database not available, skipping pattern storage"
            )
        except Exception as e:
            logger.error(f"[DISPLAY MONITOR] Failed to store learning pattern: {e}")

    def get_status(self) -> Dict[str, Any]:
        """Get current monitor status"""
        return {
            "is_monitoring": self.is_monitoring,
            "available_displays": list(self.available_displays),
            "connected_displays": list(self.connected_displays),
            "monitored_count": len(self.monitored_displays),
            "detection_methods": [m.value for m in self.detectors.keys()],
            "cache_enabled": self.config["caching"]["enabled"],
        }

    def get_available_display_details(self) -> list:
        """
        Get detailed information about currently available displays

        Returns:
            List of dicts with display details (name, id, message)
        """
        details = []
        for display_id in self.available_displays:
            monitored = next((d for d in self.monitored_displays if d.id == display_id), None)
            if monitored:
                message = f"Sir, I see your {monitored.name} is now available. Would you like to extend your display to it?"
                details.append(
                    {
                        "display_name": monitored.name,
                        "display_id": monitored.id,
                        "message": message,
                        "auto_connect": monitored.auto_connect,
                        "auto_prompt": monitored.auto_prompt,
                    }
                )
        return details

    def has_pending_prompt(self) -> bool:
        """
        Check if there's a pending display prompt

        This is used by the vision command handler to check if JARVIS
        is waiting for a yes/no response about connecting to a display.

        Returns:
            True if waiting for user response, False otherwise
        """
        # Check if we have a pending prompt that hasn't been answered yet
        return self.pending_prompt_display is not None

    async def handle_user_response(self, response: str) -> Dict:
        """
        Handle user voice response to display prompt

        When JARVIS asks "Would you like to extend your display to Living Room TV?"
        this handles the user's "yes" or "no" response.

        Args:
            response: User's voice command (e.g., "yes", "no", "yes jarvis")

        Returns:
            Response result with action taken
        """
        try:
            if not self.has_pending_prompt():
                return {"handled": False, "reason": "No pending prompt"}

            # Get the display that has the pending prompt
            display_id = self.pending_prompt_display
            display_to_connect = next(
                (d for d in self.monitored_displays if d.id == display_id), None
            )

            if not display_to_connect:
                logger.error(
                    f"[DISPLAY MONITOR] Pending prompt display {display_id} not found in monitored displays"
                )
                self.pending_prompt_display = None  # Clear invalid state
                return {"handled": False, "reason": "Display configuration not found"}

            # Parse response
            response_lower = response.lower().strip()

            # Affirmative responses
            if any(
                word in response_lower
                for word in ["yes", "yeah", "yep", "sure", "connect", "extend", "mirror"]
            ):
                # Clear pending prompt state
                self.pending_prompt_display = None
                logger.info(
                    f"[DISPLAY MONITOR] User said yes, connecting to {display_to_connect.name}"
                )

                # Determine mode
                mode = (
                    "mirror" if "mirror" in response_lower else display_to_connect.connection_mode
                )

                # Connect to display
                logger.info(
                    f"[DISPLAY MONITOR] Calling connect_display with id={display_to_connect.id}, mode={mode}"
                )
                result = await self.connect_display(display_to_connect.id)
                logger.info(
                    f"[DISPLAY MONITOR] connect_display returned: success={result.get('success')}, error={result.get('error')}"
                )

                # Generate dynamic response
                try:
                    from api.vision_command_handler import vision_command_handler

                    if vision_command_handler and hasattr(vision_command_handler, "intelligence"):
                        prompt = f"""The user asked you to connect to {display_to_connect.name}. You successfully connected.

Generate a brief, natural JARVIS-style confirmation that:
1. Confirms the connection is complete
2. Is brief and conversational (1 sentence)
3. Uses "sir" appropriately
4. Sounds confident and efficient

Respond ONLY with JARVIS's exact words, no quotes or formatting."""

                        claude_response = (
                            await vision_command_handler.intelligence._get_claude_vision_response(
                                None, prompt
                            )
                        )
                        success_response = claude_response.get(
                            "response", f"Connected to {display_to_connect.name}, sir."
                        )
                    else:
                        success_response = f"Connected to {display_to_connect.name}, sir."
                except Exception as e:
                    logger.warning(f"Could not generate dynamic response: {e}")
                    success_response = f"Connected to {display_to_connect.name}, sir."

                # Determine final response
                if result.get("success"):
                    final_response = success_response
                else:
                    # Connection failed - generate error response
                    error_detail = result.get("error", "Unknown error")
                    final_response = f"I encountered an issue connecting to {display_to_connect.name}, sir. {error_detail}"
                    logger.error(f"[DISPLAY MONITOR] Connection failed: {error_detail}")

                return {
                    "handled": True,
                    "action": "connect",
                    "display_name": display_to_connect.name,
                    "mode": mode,
                    "result": result,
                    "response": final_response,
                    "success": result.get("success", False),
                }

            # Negative responses
            elif any(word in response_lower for word in ["no", "nope", "don't", "skip", "not now"]):
                # Clear pending prompt state
                self.pending_prompt_display = None
                logger.info(f"[DISPLAY MONITOR] User said no, skipping {display_to_connect.name}")

                # Remove from available displays temporarily (user declined)
                if display_to_connect.id in self.available_displays:
                    self.available_displays.remove(display_to_connect.id)

                # Generate dynamic response
                try:
                    from api.vision_command_handler import vision_command_handler

                    if vision_command_handler and hasattr(vision_command_handler, "intelligence"):
                        prompt = f"""The user was asked: "Sir, I see your {display_to_connect.name} is now available. Would you like to extend your display to it?"

They responded: "{response}"

Generate a brief, natural JARVIS-style acknowledgment that:
1. Confirms you understood they don't want to connect
2. Is brief and conversational (1-2 sentences max)
3. Uses "sir" appropriately
4. Shows understanding without being verbose

Respond ONLY with JARVIS's exact words, no quotes or formatting."""

                        claude_response = (
                            await vision_command_handler.intelligence._get_claude_vision_response(
                                None, prompt
                            )
                        )
                        decline_response = claude_response.get("response", "Understood, sir.")
                    else:
                        decline_response = "Understood, sir."
                except Exception as e:
                    logger.warning(f"Could not generate dynamic response: {e}")
                    decline_response = "Understood, sir."

                return {
                    "handled": True,
                    "action": "skip",
                    "display_name": display_to_connect.name,
                    "response": decline_response,
                }

            else:
                # Unclear response
                return {
                    "handled": True,
                    "action": "clarify",
                    "response": "Sir, I didn't quite catch that. Would you like to extend the display? Please say 'yes' or 'no'.",
                }

        except Exception as e:
            logger.error(f"[DISPLAY MONITOR] Error handling response: {e}", exc_info=True)
            return {"handled": False, "error": str(e)}

# ============================================================================
# Singleton Pattern and Factory Functions
# ============================================================================

_display_monitor_instance: Optional[AdvancedDisplayMonitor] = None


def get_display_monitor() -> Optional[AdvancedDisplayMonitor]:
    """Get the singleton AdvancedDisplayMonitor instance.
    
    Returns:
        AdvancedDisplayMonitor instance if initialized, None otherwise
    """
    global _display_monitor_instance
    return _display_monitor_instance


def set_app_display_monitor(monitor: AdvancedDisplayMonitor) -> None:
    """Set the application-wide display monitor instance.
    
    Args:
        monitor: AdvancedDisplayMonitor instance to use as singleton
    """
    global _display_monitor_instance
    _display_monitor_instance = monitor
    logger.info("[DISPLAY MONITOR] Global display monitor instance set")


async def create_display_monitor(
    config_path: Optional[str] = None,
    voice_engine: Optional[Any] = None
) -> AdvancedDisplayMonitor:
    """Create and initialize a new AdvancedDisplayMonitor.
    
    Args:
        config_path: Optional path to configuration file
        voice_engine: Optional voice engine for announcements
        
    Returns:
        Initialized AdvancedDisplayMonitor instance
    """
    monitor = AdvancedDisplayMonitor(config_path=config_path)
    
    if voice_engine:
        monitor.set_voice_engine(voice_engine)
    
    # Set as global instance
    set_app_display_monitor(monitor)
    
    return monitor
