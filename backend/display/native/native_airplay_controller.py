#!/usr/bin/env python3
"""
Native AirPlay Controller - Python Interface
=============================================

Async Python interface to Swift native AirPlay bridge.

Features:
- Async/await subprocess management
- Automatic Swift compilation with caching
- JSON-based communication
- Self-healing error recovery
- Multiple fallback strategies
- Zero hardcoding

Author: Derek Russell
Date: 2025-10-15
Version: 2.0
"""

import asyncio
import json
import logging
import subprocess
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from enum import Enum
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class ConnectionMethod(Enum):
    """Available connection methods for AirPlay display connections.
    
    Attributes:
        MENU_BAR_CLICK: Connect via macOS menu bar interaction
        KEYBOARD_AUTOMATION: Connect using keyboard shortcuts
        APPLESCRIPT: Connect via AppleScript automation
        PRIVATE_API: Connect using private macOS APIs
    """
    MENU_BAR_CLICK = "menu_bar_click"
    KEYBOARD_AUTOMATION = "keyboard_automation"
    APPLESCRIPT = "applescript"
    PRIVATE_API = "private_api"


@dataclass
class ConnectionResult:
    """Result of a connection attempt to an AirPlay display.
    
    Attributes:
        success: Whether the connection was successful
        message: Human-readable status message
        method: Connection method that was used
        display_name: Name of the target display
        duration: Time taken for connection attempt in seconds
        fallback_used: Whether fallback methods were employed
        error_details: Additional error information if connection failed
    """
    success: bool
    message: str
    method: str
    display_name: str
    duration: float
    fallback_used: bool
    error_details: Optional[Dict[str, Any]] = None


@dataclass
class DisplayDevice:
    """Information about a discovered AirPlay display device.
    
    Attributes:
        id: Unique identifier for the display
        name: Human-readable display name
        type: Type of display device (e.g., "AppleTV", "AirPlayReceiver")
        is_available: Whether the display is currently available for connection
        metadata: Additional device-specific information
    """
    id: str
    name: str
    type: str
    is_available: bool
    metadata: Dict[str, str]


class SwiftBridgeManager:
    """Manages Swift bridge compilation and execution for native AirPlay operations.
    
    This class handles the compilation of Swift code into a native binary and
    provides an interface for executing commands through the compiled bridge.
    
    Attributes:
        swift_file: Path to the Swift source file
        config_file: Path to the configuration file
        binary_path: Path where the compiled binary will be stored
        build_cache: Directory for build cache files
    """
    
    def __init__(self, swift_file: Path, config_file: Path):
        """Initialize the Swift bridge manager.
        
        Args:
            swift_file: Path to the Swift source file to compile
            config_file: Path to the JSON configuration file
        """
        self.swift_file = swift_file
        self.config_file = config_file
        self.binary_path = swift_file.parent / "AirPlayBridge"
        self.build_cache = swift_file.parent / ".build_cache"
        self.build_cache.mkdir(exist_ok=True)
        
    async def ensure_compiled(self) -> bool:
        """Ensure Swift bridge is compiled, compile if needed.
        
        Checks if the binary exists and is up-to-date with the source file.
        If not, triggers a compilation process.
        
        Returns:
            True if binary is available and current, False otherwise
            
        Raises:
            Exception: If compilation check fails due to system errors
        """
        try:
            # Check if binary exists and is up-to-date
            if await self._is_binary_current():
                logger.debug("[SWIFT BRIDGE] Binary is current")
                return True
            
            logger.info("[SWIFT BRIDGE] Compiling native AirPlay bridge...")
            return await self._compile()
            
        except Exception as e:
            logger.error(f"[SWIFT BRIDGE] Compilation check failed: {e}")
            return False
    
    async def _is_binary_current(self) -> bool:
        """Check if compiled binary is up-to-date with source file.
        
        Compares the hash of the current source file with the cached hash
        from the last successful compilation.
        
        Returns:
            True if binary is current, False if recompilation is needed
        """
        if not self.binary_path.exists():
            return False
        
        # Check source file hash
        current_hash = self._get_source_hash()
        cached_hash_file = self.build_cache / "source_hash.txt"
        
        if cached_hash_file.exists():
            cached_hash = cached_hash_file.read_text().strip()
            if cached_hash == current_hash:
                return True
        
        return False
    
    def _get_source_hash(self) -> str:
        """Get SHA256 hash of Swift source file content.
        
        Returns:
            Hexadecimal string representation of the file hash
        """
        content = self.swift_file.read_bytes()
        return hashlib.sha256(content).hexdigest()
    
    async def _compile(self) -> bool:
        """Compile Swift bridge with required frameworks.
        
        Compiles the Swift source file into an optimized binary with all
        necessary macOS frameworks linked.
        
        Returns:
            True if compilation successful, False otherwise
            
        Raises:
            asyncio.TimeoutError: If compilation takes longer than 30 seconds
            Exception: If compilation process fails to start
        """
        try:
            cmd = [
                "swiftc",
                str(self.swift_file),
                "-o", str(self.binary_path),
                "-framework", "Foundation",
                "-framework", "CoreGraphics",
                "-framework", "ApplicationServices",
                "-framework", "IOKit",
                "-framework", "Cocoa",
                "-O"  # Optimize
            ]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=30.0
            )
            
            if process.returncode == 0:
                # Save source hash
                source_hash = self._get_source_hash()
                hash_file = self.build_cache / "source_hash.txt"
                hash_file.write_text(source_hash)
                
                logger.info("[SWIFT BRIDGE] ✅ Compilation successful")
                return True
            else:
                error_msg = stderr.decode('utf-8', errors='ignore')
                logger.error(f"[SWIFT BRIDGE] ❌ Compilation failed:\n{error_msg}")
                return False
                
        except asyncio.TimeoutError:
            logger.error("[SWIFT BRIDGE] Compilation timeout")
            return False
        except Exception as e:
            logger.error(f"[SWIFT BRIDGE] Compilation error: {e}")
            return False
    
    async def execute(self, command: str, args: List[str]) -> Dict[str, Any]:
        """Execute a command through the Swift bridge.
        
        Runs the compiled Swift binary with the specified command and arguments,
        returning the JSON response.
        
        Args:
            command: The command to execute (e.g., "discover", "connect")
            args: List of command arguments
            
        Returns:
            Dictionary containing the command result, typically with keys:
            - success: Boolean indicating if command succeeded
            - message: Status message
            - method: Method used (for connection commands)
            - Additional command-specific data
            
        Raises:
            asyncio.TimeoutError: If command execution exceeds 30 seconds
            json.JSONDecodeError: If bridge returns invalid JSON
        """
        try:
            cmd = [str(self.binary_path), command] + args + [str(self.config_file)]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=30.0
            )
            
            if process.returncode == 0:
                output = stdout.decode('utf-8', errors='ignore').strip()
                if output:
                    return json.loads(output)
                else:
                    return {"success": False, "message": "No output from bridge"}
            else:
                error_msg = stderr.decode('utf-8', errors='ignore')
                logger.warning(f"[SWIFT BRIDGE] Command failed: {error_msg}")
                
                # Try to parse error as JSON
                try:
                    return json.loads(stdout.decode('utf-8', errors='ignore'))
                except Exception:
                    return {
                        "success": False,
                        "message": error_msg or "Unknown error",
                        "method": "none"
                    }
                    
        except asyncio.TimeoutError:
            return {
                "success": False,
                "message": "Command timeout",
                "method": "none"
            }
        except Exception as e:
            return {
                "success": False,
                "message": str(e),
                "method": "none"
            }


class NativeAirPlayController:
    """Production-grade native AirPlay controller with comprehensive error handling.
    
    This controller provides a high-level interface for discovering and connecting
    to AirPlay displays using native macOS APIs through a Swift bridge. It features
    automatic compilation, multiple connection strategies, and detailed statistics.
    
    Features:
    - Async/await support for non-blocking operations
    - Multiple connection strategies with automatic fallback
    - Self-healing error recovery
    - Zero hardcoding - fully configuration-driven
    - Comprehensive logging and metrics collection
    
    Attributes:
        module_dir: Directory containing this module
        swift_file: Path to the Swift bridge source file
        config_file: Path to the configuration file
        config: Loaded configuration dictionary
        bridge: Swift bridge manager instance
        is_compiled: Whether the Swift bridge is compiled and ready
        last_connection_time: Timestamp of last successful connection
        connection_stats: Dictionary tracking connection statistics
    """
    
    def __init__(self, config_path: Optional[str] = None):
        """Initialize native AirPlay controller.
        
        Args:
            config_path: Optional path to configuration file. If None, uses
                        default path relative to module location.
                        
        Raises:
            FileNotFoundError: If configuration file doesn't exist
            json.JSONDecodeError: If configuration file contains invalid JSON
        """
        # Paths
        self.module_dir = Path(__file__).parent
        self.swift_file = self.module_dir / "AirPlayBridge.swift"
        
        if config_path is None:
            config_path = self.module_dir.parent.parent / "config" / "airplay_config.json"
        self.config_file = Path(config_path)
        
        # Load configuration
        self.config = self._load_config()
        
        # Initialize Swift bridge manager
        self.bridge = SwiftBridgeManager(self.swift_file, self.config_file)
        
        # State
        self.is_compiled = False
        self.last_connection_time: Optional[datetime] = None
        self.connection_stats = {
            "total_attempts": 0,
            "successful": 0,
            "failed": 0,
            "by_method": {}
        }
        
        logger.info("[NATIVE AIRPLAY] Controller initialized")
    
    def _load_config(self) -> Dict[str, Any]:
        """Load and parse the configuration file.
        
        Returns:
            Dictionary containing the parsed configuration
            
        Raises:
            FileNotFoundError: If configuration file doesn't exist
            json.JSONDecodeError: If configuration file contains invalid JSON
        """
        try:
            with open(self.config_file) as f:
                config = json.load(f)
            logger.info(f"[NATIVE AIRPLAY] Loaded config from {self.config_file}")
            return config
        except FileNotFoundError:
            logger.error(f"[NATIVE AIRPLAY] Config not found: {self.config_file}")
            raise
        except json.JSONDecodeError as e:
            logger.error(f"[NATIVE AIRPLAY] Invalid JSON in config: {e}")
            raise
    
    async def initialize(self) -> bool:
        """Initialize the native bridge by ensuring Swift code is compiled.
        
        This method checks if the Swift bridge is already compiled and ready.
        If not, it triggers the compilation process. This is safe to call
        multiple times.
        
        Returns:
            True if bridge is ready for use, False if compilation failed
        """
        if self.is_compiled:
            return True
        
        logger.info("[NATIVE AIRPLAY] Initializing native bridge...")
        self.is_compiled = await self.bridge.ensure_compiled()
        
        if self.is_compiled:
            logger.info("[NATIVE AIRPLAY] ✅ Native bridge ready")
        else:
            logger.warning("[NATIVE AIRPLAY] ⚠️  Native bridge unavailable (compilation failed)")
        
        return self.is_compiled
    
    async def discover_displays(self) -> List[DisplayDevice]:
        """Discover available AirPlay displays on the network.
        
        Scans for AirPlay-compatible displays and returns information about
        each discovered device including availability status.
        
        Returns:
            List of DisplayDevice objects representing discovered displays.
            Returns empty list if no displays found or if bridge unavailable.
            
        Example:
            >>> controller = NativeAirPlayController()
            >>> displays = await controller.discover_displays()
            >>> for display in displays:
            ...     print(f"Found: {display.name} ({display.type})")
        """
        logger.info("[NATIVE AIRPLAY] Discovering displays...")
        
        # Ensure bridge is compiled
        if not await self.initialize():
            logger.warning("[NATIVE AIRPLAY] Bridge not available, returning empty list")
            return []
        
        try:
            result = await self.bridge.execute("discover", [])
            
            if isinstance(result, list):
                devices = [DisplayDevice(**device) for device in result]
                logger.info(f"[NATIVE AIRPLAY] Found {len(devices)} displays: {[d.name for d in devices]}")
                return devices
            else:
                logger.warning(f"[NATIVE AIRPLAY] Unexpected discovery result: {result}")
                return []
                
        except Exception as e:
            logger.error(f"[NATIVE AIRPLAY] Discovery error: {e}", exc_info=True)
            return []
    
    async def connect(self, display_name: str) -> ConnectionResult:
        """Connect to an AirPlay display using native methods with fallback strategies.
        
        Attempts to connect to the specified display using multiple connection
        methods as configured. Automatically falls back to alternative methods
        if the primary method fails.
        
        Args:
            display_name: Name of the display to connect to (case-sensitive)
            
        Returns:
            ConnectionResult object containing detailed information about the
            connection attempt including success status, method used, duration,
            and any error details.
            
        Example:
            >>> controller = NativeAirPlayController()
            >>> result = await controller.connect("Living Room TV")
            >>> if result.success:
            ...     print(f"Connected via {result.method} in {result.duration:.2f}s")
            ... else:
            ...     print(f"Failed: {result.message}")
        """
        start_time = datetime.now()
        self.connection_stats["total_attempts"] += 1
        
        logger.info(f"[NATIVE AIRPLAY] Connecting to '{display_name}'...")
        
        # Ensure bridge is compiled
        if not await self.initialize():
            return ConnectionResult(
                success=False,
                message="Native bridge unavailable (compilation failed)",
                method="none",
                display_name=display_name,
                duration=0.0,
                fallback_used=False,
                error_details={"reason": "bridge_unavailable"}
            )
        
        try:
            # Execute connection
            result = await self.bridge.execute("connect", [display_name])
            
            duration = (datetime.now() - start_time).total_seconds()
            
            # Parse result
            if result.get("success"):
                self.connection_stats["successful"] += 1
                self.last_connection_time = datetime.now()
                
                method = result.get("method", "unknown")
                self.connection_stats["by_method"][method] = \
                    self.connection_stats["by_method"].get(method, 0) + 1
                
                logger.info(f"[NATIVE AIRPLAY] ✅ Connected via {method} in {duration:.2f}s")
                
                return ConnectionResult(
                    success=True,
                    message=result.get("message", f"Connected to {display_name}"),
                    method=method,
                    display_name=display_name,
                    duration=result.get("duration", duration),
                    fallback_used=result.get("fallbackUsed", False)
                )
            else:
                self.connection_stats["failed"] += 1
                error_msg = result.get("message", "Connection failed")
                
                logger.warning(f"[NATIVE AIRPLAY] ❌ Connection failed: {error_msg}")
                
                return ConnectionResult(
                    success=False,
                    message=error_msg,
                    method=result.get("method", "none"),
                    display_name=display_name,
                    duration=duration,
                    fallback_used=result.get("fallbackUsed", False),
                    error_details=result
                )
                
        except Exception as e:
            self.connection_stats["failed"] += 1
            duration = (datetime.now() - start_time).total_seconds()
            
            logger.error(f"[NATIVE AIRPLAY] Exception during connection: {e}", exc_info=True)
            
            return ConnectionResult(
                success=False,
                message=f"Exception: {str(e)}",
                method="none",
                display_name=display_name,
                duration=duration,
                fallback_used=False,
                error_details={"exception": str(e), "type": type(e).__name__}
            )
    
    def get_stats(self) -> Dict[str, Any]:
        """Get comprehensive connection statistics.
        
        Returns:
            Dictionary containing connection statistics including:
            - total_attempts: Total number of connection attempts
            - successful: Number of successful connections
            - failed: Number of failed connections
            - success_rate: Success rate as a percentage
            - by_method: Breakdown of successful connections by method
            - last_connection: ISO timestamp of last successful connection
            - bridge_compiled: Whether the Swift bridge is compiled
            
        Example:
            >>> stats = controller.get_stats()
            >>> print(f"Success rate: {stats['success_rate']}%")
        """
        success_rate = 0.0
        if self.connection_stats["total_attempts"] > 0:
            success_rate = (self.connection_stats["successful"] / 
                          self.connection_stats["total_attempts"]) * 100
        
        return {
            "total_attempts": self.connection_stats["total_attempts"],
            "successful": self.connection_stats["successful"],
            "failed": self.connection_stats["failed"],
            "success_rate": round(success_rate, 2),
            "by_method": self.connection_stats["by_method"],
            "last_connection": self.last_connection_time.isoformat() if self.last_connection_time else None,
            "bridge_compiled": self.is_compiled
        }
    
    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive controller status information.
        
        Returns:
            Dictionary containing detailed status information including:
            - initialized: Whether the controller is fully initialized
            - config_loaded: Whether configuration was loaded successfully
            - swift_file_exists: Whether the Swift source file exists
            - binary_exists: Whether the compiled binary exists
            - config_file: Path to the configuration file
            - connection_methods: List of available connection methods
            - stats: Current connection statistics
            
        Example:
            >>> status = controller.get_status()
            >>> if status['initialized']:
            ...     print("Controller is ready")
        """
        return {
            "initialized": self.is_compiled,
            "config_loaded": self.config is not None,
            "swift_file_exists": self.swift_file.exists(),
            "binary_exists": self.bridge.binary_path.exists(),
            "config_file": str(self.config_file),
            "connection_methods": list(self.config.get("connection_methods", {}).keys()),
            "stats": self.get_stats()
        }


# Singleton instance
_native_controller: Optional[NativeAirPlayController] = None


def get_native_controller(config_path: Optional[str] = None) -> NativeAirPlayController:
    """Get singleton native controller instance.
    
    Returns the same controller instance across multiple calls to ensure
    consistent state and avoid recompilation overhead.
    
    Args:
        config_path: Optional path to configuration file. Only used on first call.
        
    Returns:
        NativeAirPlayController instance
        
    Example:
        >>> controller = get_native_controller()
        >>> # Later in the code...
        >>> same_controller = get_native_controller()  # Returns same instance
    """
    global _native_controller
    if _native_controller is None:
        _native_controller = NativeAirPlayController(config_path)
    return _native_controller


if __name__ == "__main__":
    # Test the controller
    async def test():
        """Test function demonstrating controller usage and capabilities.
        
        This function provides a comprehensive test of the controller's
        functionality including initialization, discovery, connection,
        and statistics reporting.
        """
        logging.basicConfig(level=logging.INFO)
        
        controller = get_native_controller()
        
        print("\n" + "="*60)
        print("Native AirPlay Controller Test")
        print("="*60)
        
        # Initialize
        print("\n1. Initializing...")
        success = await controller.initialize()
        print(f"   Initialized: {success}")
        
        # Get status
        print("\n2. Controller Status:")
        status = controller.get_status()
        for key, value in status.items():
            print(f"   {key}: {value}")
        
        # Discover displays
        print("\n3. Discovering displays...")
        displays = await controller.discover_displays()
        print(f"   Found {len(displays)} displays:")
        for display in displays:
            print(f"   - {display.name} ({display.type}) - Available: {display.is_available}")
        
        # Test connection (if displays found)
        if displays:
            print(f"\n4. Testing connection to '{displays[0].name}'...")
            result = await controller.connect(displays[0].name)
            print(f"   Success: {result.success}")
            print(f"   Message: {result.message}")
            print(f"   Method: {result.method}")
            print(f"   Duration: {result.duration:.2f}s")
            print(f"   Fallback used: {result.fallback_used}")
        
        # Get stats
        print("\n5. Connection Statistics:")
        stats = controller.get_stats()
        for key, value in stats.items():
            print(f"   {key}: {value}")
        
        print("\n" + "="*60)
    
    asyncio.run(test())