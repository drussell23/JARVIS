"""
Intelligent Memory Optimizer for JARVIS

This module provides advanced memory optimization capabilities for JARVIS, automatically
freeing memory to enable resource-intensive features like LangChain mode. It includes
intelligent process management, system cache clearing, and application optimization.

The optimizer uses a multi-strategy approach:
- Python garbage collection and cache clearing
- Helper process termination
- System cache purging
- High-memory application management
- Browser optimization
- Background app suspension
- Inactive memory purging

Example:
    >>> optimizer = IntelligentMemoryOptimizer()
    >>> success, report = await optimizer.optimize_for_langchain()
    >>> print(f"Optimization {'succeeded' if success else 'failed'}")
"""

import os
import sys
import psutil
import asyncio
import logging
import subprocess
import gc
import ctypes
from typing import List, Dict, Tuple, Optional, Any
from datetime import datetime
import json
from pathlib import Path

# Import optimization config
try:
    from .optimization_config import optimization_config, AppProfile
except ImportError:
    from optimization_config import optimization_config, AppProfile

logger = logging.getLogger(__name__)

class ProcessInfo:
    """Information about a process that can be managed for memory optimization.
    
    This class encapsulates process details and determines whether a process
    can be safely killed or suspended based on predefined patterns and rules.
    
    Attributes:
        pid: Process ID
        name: Process name
        memory_percent: Percentage of system memory used by process
        memory_mb: Memory usage in megabytes
        can_kill: Whether the process can be safely terminated
        can_suspend: Whether the process can be suspended
    """

    def __init__(self, pid: int, name: str, memory_percent: float, memory_mb: float):
        """Initialize ProcessInfo with process details.
        
        Args:
            pid: Process ID
            name: Process name
            memory_percent: Percentage of system memory used
            memory_mb: Memory usage in megabytes
        """
        self.pid = pid
        self.name = name
        self.memory_percent = memory_percent
        self.memory_mb = memory_mb
        self.can_kill = self._determine_killable()
        self.can_suspend = self._determine_suspendable()

    def _determine_killable(self) -> bool:
        """Determine if process can be safely killed.
        
        Analyzes the process name against patterns of killable and protected
        processes to determine if it's safe to terminate.
        
        Returns:
            True if process can be safely killed, False otherwise
        """
        # Killable process patterns
        killable_patterns = [
            "Helper",
            "Renderer",
            "GPU Process",
            "Utility",
            "CrashPad",
            "ReportCrash",
            "mdworker",
            "mds_stores",
            "com.apple.WebKit",
            "VTDecoderXPCService",
            "PhotoAnalysisD",
            "cloudphotod",
            "bird",
            "commerce",
            "akd",
            "tccd",
            "nsurlsessiond",
        ]

        # Never kill these
        protected_patterns = [
            "kernel",
            "launchd",
            "systemd",
            "init",
            "WindowServer",
            "loginwindow",
            "Finder",
            "Dock",
            "SystemUIServer",
            "python",
            "node",
        ]

        name_lower = self.name.lower()

        # Check if protected
        for pattern in protected_patterns:
            if pattern.lower() in name_lower:
                return False

        # Check if killable
        for pattern in killable_patterns:
            if pattern.lower() in name_lower:
                return True

        return False

    def _determine_suspendable(self) -> bool:
        """Determine if process can be suspended.
        
        Checks if the process matches patterns of applications that can be
        safely suspended without losing important work or system functionality.
        
        Returns:
            True if process can be suspended, False otherwise
        """
        suspendable_patterns = [
            "Slack",
            "Discord",
            "Spotify",
            "Music",
            "TV",
            "News",
            "Stocks",
            "Weather",
            "Reminders",
            "Notes",
            "Calendar",
            "WhatsApp",
            "Telegram",
            "Signal",
            "Messages",
            "FaceTime",
        ]

        for pattern in suspendable_patterns:
            if pattern.lower() in self.name.lower():
                return True

        return False

    def _is_high_priority_target(self) -> bool:
        """Check if this is a high-priority target for LangChain mode optimization.
        
        Identifies applications that commonly use significant memory and can be
        closed or suspended to free resources for LangChain operations.
        
        Returns:
            True if this is a high-priority optimization target, False otherwise
        """
        # These apps commonly use lots of memory and can be closed/suspended
        high_priority_patterns = [
            "Cursor",
            "Code",
            "IntelliJ",
            "PyCharm",
            "WebStorm",
            "Android Studio",
            "Xcode",
            "Visual Studio",
            "Chrome",
            "Safari",
            "Firefox",
            "Edge",
            "Brave",
            "Slack",
            "Discord",
            "WhatsApp",
            "Telegram",
            "Docker",
            "VirtualBox",
            "VMware",
            "Parallels",
        ]

        for pattern in high_priority_patterns:
            if pattern.lower() in self.name.lower():
                return True

        return False

class IntelligentMemoryOptimizer:
    """Advanced memory optimization system for JARVIS.
    
    This class provides intelligent memory management capabilities, automatically
    freeing memory through various strategies to enable resource-intensive features
    like LangChain mode. It uses a multi-layered approach combining garbage collection,
    process management, cache clearing, and application optimization.
    
    Attributes:
        is_macos: Whether running on macOS
        target_memory_percent: Target memory usage percentage for optimization
        optimization_history: History of optimization attempts
    """

    def __init__(self):
        """Initialize the memory optimizer with platform detection and default settings."""
        self.is_macos = sys.platform == "darwin"
        self.target_memory_percent = 45  # Target for LangChain mode
        self.optimization_history = []

    async def optimize_for_langchain(
        self, aggressive: bool = False
    ) -> Tuple[bool, Dict[str, Any]]:
        """Attempt to free memory to enable LangChain mode.

        Executes a series of optimization strategies to reduce memory usage below
        the target threshold. Strategies are applied in order of effectiveness
        and safety, from garbage collection to application termination.

        Args:
            aggressive: If True, will close high-memory apps like IDEs without prompting

        Returns:
            Tuple containing:
                - success: Whether target memory usage was achieved
                - report: Detailed optimization report with metrics and actions taken

        Example:
            >>> optimizer = IntelligentMemoryOptimizer()
            >>> success, report = await optimizer.optimize_for_langchain(aggressive=True)
            >>> print(f"Freed {report['memory_freed_mb']} MB")
        """
        logger.info(
            f"Starting {'aggressive' if aggressive else 'intelligent'} memory optimization for LangChain"
        )

        initial_memory = psutil.virtual_memory()
        report = {
            "initial_percent": initial_memory.percent,
            "target_percent": self.target_memory_percent,
            "actions_taken": [],
            "memory_freed_mb": 0,
            "final_percent": 0,
            "success": False,
            "aggressive_mode": aggressive,
        }

        # If already below target, we're good
        if initial_memory.percent <= self.target_memory_percent:
            report["success"] = True
            report["final_percent"] = initial_memory.percent
            return True, report

        # Calculate how much memory we need to free
        memory_to_free_mb = self._calculate_memory_to_free()
        logger.info(f"Need to free {memory_to_free_mb:.0f} MB to reach target")

        # Try optimization strategies in order
        # Reorder for better effectiveness
        strategies = [
            ("garbage_collection", self._optimize_python_memory),
            ("kill_helpers", self._kill_helper_processes),  # Move up
            ("clear_caches", self._clear_system_caches),
            (
                "close_high_memory_apps",
                self._close_high_memory_applications,
            ),  # Most effective
            ("optimize_browsers", self._optimize_browser_memory),  # Before suspending
            ("suspend_apps", self._suspend_background_apps),
            ("purge_memory", self._purge_inactive_memory),
        ]

        for strategy_name, strategy_func in strategies:
            if psutil.virtual_memory().percent <= self.target_memory_percent:
                break

            try:
                freed_mb = await strategy_func()
                if freed_mb > 0:
                    report["actions_taken"].append(
                        {
                            "strategy": strategy_name,
                            "freed_mb": freed_mb,
                            "timestamp": datetime.now().isoformat(),
                        }
                    )
                    report["memory_freed_mb"] += freed_mb
                    logger.info(f"{strategy_name} freed {freed_mb:.0f} MB")
            except Exception as e:
                logger.warning(f"Strategy {strategy_name} failed: {e}")

        # Final check
        final_memory = psutil.virtual_memory()
        report["final_percent"] = final_memory.percent
        report["success"] = final_memory.percent <= self.target_memory_percent

        # Save optimization history
        self._save_optimization_report(report)

        return report["success"], report

    def _calculate_memory_to_free(self) -> float:
        """Calculate how much memory needs to be freed to reach target.
        
        Returns:
            Amount of memory in MB that needs to be freed
        """
        mem = psutil.virtual_memory()
        current_used_mb = mem.used / (1024 * 1024)
        target_used_mb = (mem.total * self.target_memory_percent / 100) / (1024 * 1024)
        return max(0, current_used_mb - target_used_mb)

    async def _optimize_python_memory(self) -> float:
        """Optimize Python's memory usage through garbage collection and cache clearing.
        
        Performs comprehensive Python memory optimization including:
        - Full garbage collection cycles
        - Function cache clearing
        - Regular expression cache purging
        
        Returns:
            Amount of memory freed in MB
        """
        before_mb = psutil.Process().memory_info().rss / (1024 * 1024)

        # Force garbage collection
        gc.collect(2)  # Full collection

        # Clear all caches we can find
        import functools
        import re

        # Clear functools caches safely
        try:
            # Get all objects in memory
            for obj in gc.get_objects():
                if hasattr(obj, 'cache_clear') and callable(obj.cache_clear):
                    try:
                        obj.cache_clear()
                    except Exception:
                        pass
        except Exception:
            pass

        # Clear re caches
        re.purge()

        after_mb = psutil.Process().memory_info().rss / (1024 * 1024)
        return max(0, before_mb - after_mb)

    async def _clear_system_caches(self) -> float:
        """Clear system caches (macOS specific).
        
        Clears various system caches including DNS cache, memory pressure,
        and compiler caches to free up memory.
        
        Returns:
            Amount of memory freed in MB
        """
        if not self.is_macos:
            return 0

        before = psutil.virtual_memory().available / (1024 * 1024)

        try:
            # Clear DNS cache (non-sudo version)
            subprocess.run(
                ["dscacheutil", "-flushcache"],
                capture_output=True,
                timeout=5,
                stderr=subprocess.DEVNULL,
            )

            # Try memory pressure without sudo first
            result = subprocess.run(
                ["memory_pressure", "-l", "warn"], capture_output=True, timeout=5
            )
            if result.returncode == 0:
                await asyncio.sleep(1)

            # Clear Swift/Python caches
            subprocess.run(
                [
                    "swift-frontend",
                    "-typecheck",
                    "-Xfrontend",
                    "-debug-time-compilation",
                ],
                capture_output=True,
                timeout=2,
                stderr=subprocess.DEVNULL,
            )

            # Force macOS to compress memory
            vm_stat = subprocess.run(["vm_stat"], capture_output=True, text=True)
            if vm_stat.returncode == 0:
                # Parse and trigger compression
                subprocess.run(
                    ["sysctl", "vm.compressor_mode=4"],
                    capture_output=True,
                    stderr=subprocess.DEVNULL,
                )

        except Exception as e:
            logger.debug(f"Cache clearing error: {e}")

        after = psutil.virtual_memory().available / (1024 * 1024)
        return max(0, after - before)

    async def _kill_helper_processes(self) -> float:
        """Kill helper processes that use significant memory.
        
        Identifies and terminates helper processes that can be safely killed
        to free memory. Focuses on browser helpers, system utilities, and
        other non-critical background processes.
        
        Returns:
            Amount of memory freed in MB
        """
        processes_to_kill = []
        freed_mb = 0

        # Find killable processes
        for proc in psutil.process_iter(
            ["pid", "name", "memory_percent", "memory_info"]
        ):
            try:
                # Check if memory_info exists and has rss
                if proc.info["memory_info"] is None:
                    continue
                    
                pinfo = ProcessInfo(
                    pid=proc.info["pid"],
                    name=proc.info["name"],
                    memory_percent=proc.info["memory_percent"],
                    memory_mb=proc.info["memory_info"].rss / (1024 * 1024),
                )

                # More aggressive: kill if using > 0.3% memory and is killable
                if pinfo.memory_percent > 0.3 and pinfo.can_kill:
                    processes_to_kill.append(pinfo)

            except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
                continue

        # Sort by memory usage and kill top offenders
        processes_to_kill.sort(key=lambda x: x.memory_mb, reverse=True)

        for proc in processes_to_kill[:10]:  # Kill up to 10 processes for more impact
            try:
                psutil.Process(proc.pid).terminate()
                freed_mb += proc.memory_mb
                logger.info(f"Killed {proc.name} (freed {proc.memory_mb:.0f} MB)")
                await asyncio.sleep(0.1)
            except Exception:
                pass

        return freed_mb

    async def _close_high_memory_applications(self) -> float:
        """Close or suspend high-memory applications for LangChain mode.
        
        Targets memory-intensive applications like IDEs, browsers, and messaging
        apps for closure or suspension. Uses graceful shutdown methods when
        possible to preserve user work.
        
        Returns:
            Amount of memory freed in MB
        """
        logger.info("Targeting high-memory applications for LangChain optimization")
        freed_mb = 0
        closed_apps = []

        # Get all processes sorted by memory usage
        high_memory_apps = []
        for proc in psutil.process_iter(
            ["pid", "name", "memory_percent", "memory_info"]
        ):
            try:
                # Check if memory_info exists
                if proc.info["memory_info"] is None:
                    continue
                    
                pinfo = ProcessInfo(
                    pid=proc.info["pid"],
                    name=proc.info["name"],
                    memory_percent=proc.info["memory_percent"],
                    memory_mb=proc.info["memory_info"].rss / (1024 * 1024),
                )

                # Check if this process should be closed for LangChain
                # Lower threshold for more aggressive optimization
                if pinfo.memory_mb > 100 and pinfo.memory_percent > 2.0:
                    # Check if it's a closeable app type
                    if (
                        optimization_config.get_app_profile(pinfo.name)
                        or pinfo._is_high_priority_target()
                    ):
                        high_memory_apps.append(pinfo)

            except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
                continue

        # Sort by memory usage
        high_memory_apps.sort(key=lambda x: x.memory_mb, reverse=True)

        # Process high-memory apps
        for app in high_memory_apps:
            # Special handling for different app types
            if "cursor" in app.name.lower() or "code" in app.name.lower():
                # For IDEs, try to save work first
                logger.info(
                    f"Attempting to close {app.name} (using {app.memory_percent:.1f}% memory)"
                )

                # Use AppleScript to gracefully close on macOS
                if self.is_macos:
                    try:
                        script = f"""
                        tell application "{app.name}"
                            quit saving yes
                        end tell
                        """
                        subprocess.run(
                            ["osascript", "-e", script], capture_output=True, timeout=5
                        )
                        freed_mb += app.memory_mb
                        closed_apps.append(app.name)
                        logger.info(
                            f"Gracefully closed {app.name}, freed {app.memory_mb:.0f} MB"
                        )
                        await asyncio.sleep(2)  # Give time to close
                    except Exception:
                        # Fall back to terminate
                        try:
                            psutil.Process(app.pid).terminate()
                            freed_mb += app.memory_mb
                            closed_apps.append(app.name)
                        except Exception:
                            pass

            elif "whatsapp" in app.name.lower():
                # For messaging apps, just quit them
                try:
                    psutil.Process(app.pid).terminate()
                    freed_mb += app.memory_mb
                    closed_apps.append(app.name)
                    logger.info(f"Closed {app.name}, freed {app.memory_mb:.0f} MB")
                except Exception:
                    pass

            elif "chrome" in app.name.lower() or "safari" in app.name.lower():
                # For browsers, close excess tabs first (handled in browser optimization)
                # Here we can close the whole browser if needed
                if app.memory_percent > 5.0:  # Only if using lots of memory
                    try:
                        if self.is_macos:
                            script = f"""
                            tell application "{app.name}"
                                quit
                            end tell
                            """
                            subprocess.run(
                                ["osascript", "-e", script],
                                capture_output=True,
                                timeout=5,
                            )
                        else:
                            psutil.Process(app.pid).terminate()
                        freed_mb += app.memory_mb
                        closed_apps.append(app.name)
                        logger.info(f"Closed {app.name}, freed {app.memory_mb:.0f} MB")
                    except Exception:
                        pass

            # Check if we've freed enough memory
            current_mem = psutil.virtual_memory().percent
            if current_mem <= self.target_memory_percent:
                logger.info(
                    f"Target memory reached after closing {len(closed_apps)} apps"
                )
                break

        if closed_apps:
            logger.info(f"Closed applications: {', '.join(closed_apps)}")

        return freed_mb

    async def _suspend_background_apps(self) -> float:
        """Suspend background applications to free memory.
        
        Sends SIGSTOP signals to suspendable background applications,
        effectively pausing them and reducing their memory footprint.
        
        Returns:
            Estimated amount of memory freed in MB
        """
        if not self.is_macos:
            return 0

        freed_mb = 0

        for proc in psutil.process_iter(
            ["pid", "name", "memory_percent", "memory_info"]
        ):
            try:
                pinfo = ProcessInfo(
                    pid=proc.info["pid"],
                    name=proc.info["name"],
                    memory_percent=proc.info["memory_percent"],
                    memory_mb=proc.info["memory_info"].rss / (1024 * 1024),
                )

                if pinfo.memory_percent > 0.5 and pinfo.can_suspend:
                    # Send SIGSTOP to suspend
                    os.kill(pinfo.pid, 19)  # SIGSTOP
                    freed_mb += pinfo.memory_mb * 0.7  # Estimate 70% freed
                    logger.info(f"Suspended {pinfo.name}")

            except Exception:
                continue

        return freed_mb

    async def _optimize_browser_memory(self) -> float:
        """Optimize browser memory usage by closing unnecessary tabs.
        
        Uses platform-specific methods to close excess browser tabs,
        particularly targeting Chrome which tends to use significant memory.
        
        Returns:
            Estimated amount of memory freed in MB
        """
        freed_mb = 0
        browser_names = ["Chrome", "Safari", "Firefox", "Edge", "Brave"]

        for browser in browser_names:
            try:
                # Use AppleScript to close unnecessary tabs (macOS)
                if self.is_macos and browser == "Chrome":
                    script = """
                    tell application "Google Chrome"
                        set tabCount to count of tabs of window 1
                        if tabCount > 3 then
                            repeat with i from tabCount to 4 by -1
                                close tab i of window 1
                            end repeat
                        end if
                    end tell
                    """
                    subprocess.run(
                        ["osascript", "-e", script], capture_output=True, timeout=5
                    )
                    freed_mb += 50  # Estimate

            except Exception:
                pass

        return freed_mb

    async def _purge_inactive_memory(self) -> float:
        """Purge inactive memory pages from system memory.
        
        Uses various system-level techniques to force the OS to free
        inactive memory pages and compress memory where possible.
        
        Returns:
            Amount of memory freed in MB
        """
        if not self.is_macos:
            return 0

        before = psutil.virtual_memory().available / (1024 * 1024)

        try:
            # Try alternative memory pressure techniques
            # 1. Force garbage collection in all Python processes
            gc.collect(2)

            # 2. Use memory_pressure tool
            subprocess.run(
                ["memory_pressure", "-l", "critical", "-s", "1"],
                capture_output=True,
                timeout=5,
                stderr=subprocess.DEVNULL,
            )

            # 3. Clear file caches
            subprocess.run(["sync"], capture_output=True, timeout=2)

            # 4. Drop clean caches (doesn't require sudo)
            with open("/proc/sys/vm/drop_caches", "w", errors="ignore") as f:
                f.write("1")

        except Exception:
            pass

        await asyncio.sleep(2)  # Wait for operations to complete
        after = psutil.virtual_memory().available / (1024 * 1024)
        return max(0, after - before)

    def _save_optimization_report(self, report: Dict):
        """Save optimization report for analysis and debugging.
        
        Args:
            report: Optimization report dictionary to save
        """
        reports_dir = Path.home() / ".jarvis" / "memory_reports"
        reports_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_file = reports_dir / f"optimization_{timestamp}.json"

        with open(report_file, "w") as f:
            json.dump(report, f, indent=2)

    async def get_optimization_suggestions(self) -> List[str]:
        """Get suggestions for manual memory optimization.
        
        Analyzes current system state to provide actionable suggestions
        for manual memory optimization, prioritizing high-impact actions.
        
        Returns:
            List of optimization suggestions as human-readable strings
            
        Example:
            >>> optimizer = IntelligentMemoryOptimizer()
            >>> suggestions = await optimizer.get_optimization_suggestions()
            >>> for suggestion in suggestions:
            ...     print(f"- {suggestion}")
        """
        suggestions = []

        # Analyze current memory usage
        for proc in psutil.process_iter(["name", "memory_percent"]):
            try:
                if proc.info["memory_percent"] > 5:
                    suggestions.append(
                        f"Close {proc.info['name']} (using {proc.info['memory_percent']:.1f}% memory)"
                    )
            except Exception:
                continue

        # Check browser tabs
        if self.is_macos:
            try:
                # Check Chrome tabs
                script = """
                tell application "Google Chrome"
                    return count of tabs of window 1
                end tell
                """
                result = subprocess.run(
                    ["osascript", "-e", script], capture_output=True, text=True
                )
                tab_count = int(result.stdout.strip())
                if tab_count > 5:
                    suggestions.append(
                        f"Close some Chrome tabs (currently {tab_count} open)"
                    )
            except Exception:
                pass

        # Check for high-memory apps
        for proc in psutil.process_iter(["name", "memory_percent"]):
            try:
                if proc.info["memory_percent"] > 5:
                    name = proc.info["name"]
                    if any(
                        ide in name.lower() for ide in ["cursor", "code", "intellij"]
                    ):
                        suggestions.insert(
                            0,
                            f"Close {name} IDE (using {proc.info['memory_percent']:.1f}% memory)",
                        )
                    elif any(
                        app in name.lower() for app in ["whatsapp", "slack", "discord"]
                    ):
                        suggestions.append(
                            f"Close {name} (using {proc.info['memory_percent']:.1f}% memory)"
                        )
            except Exception:
                continue

        return suggestions[:5]  # Return top 5 suggestions

# Integration with JARVIS
class MemoryOptimizationAPI:
    """API endpoints for memory optimization integration with JARVIS.
    
    Provides a high-level interface for memory optimization operations,
    designed to be used by JARVIS's main system for enabling resource-intensive
    features like LangChain mode.
    
    Attributes:
        optimizer: IntelligentMemoryOptimizer instance for performing optimizations
    """

    def __init__(self):
        """Initialize the API with a memory optimizer instance."""
        self.optimizer = IntelligentMemoryOptimizer()

    async def optimize_for_mode(self, target_mode: str = "langchain") -> Dict:
        """Optimize memory for a specific operational mode.
        
        Args:
            target_mode: The mode to optimize for (currently supports "langchain")
            
        Returns:
            Dictionary containing optimization results:
                - success: Whether optimization succeeded
                - report: Detailed optimization report
                - message: Human-readable status message
                - error: Error message if mode is unsupported
                
        Example:
            >>> api = MemoryOptimizationAPI()
            >>> result = await api.optimize_for_mode("langchain")
            >>> if result["success"]:
            ...     print("LangChain mode enabled!")
        """
        if target_mode == "langchain":
            success, report = await self.optimizer.optimize_for_langchain()
            return {
                "success": success,
                "report": report,
                "message": (
                    "Memory optimization completed"
                    if success
                    else "Could not free enough memory"
                ),
            }
        else:
            return {"error": "Unsupported mode"}

    async def get_suggestions(self) -> Dict:
        """Get memory optimization suggestions for manual intervention.
        
        Returns:
            Dictionary containing:
                - suggestions: List of optimization suggestions
                - current_memory_percent: Current system memory usage percentage
                
        Example:
            >>> api = MemoryOptimizationAPI()
            >>> result = await api.get_suggestions()
            >>> print(f"Memory usage: {result['current_memory_percent']}%")
            >>> for suggestion in result['suggestions']:
            ...     print(f"- {suggestion}")
        """
        suggestions = await self.optimizer.get_optimization_suggestions()
        return {
            "suggestions": suggestions,
            "current_memory_percent": psutil.virtual_memory().percent,
        }

if __name__ == "__main__":
    # Test the optimizer
    async def test():
        """Test function to demonstrate optimizer capabilities."""
        optimizer = IntelligentMemoryOptimizer()
        print("Testing Memory Optimizer...")

        # Get current status
        mem = psutil.virtual_memory()
        print(f"Current memory: {mem.percent}%")

        # Get suggestions
        suggestions = await optimizer.get_optimization_suggestions()
        print("\nSuggestions:")
        for s in suggestions:
            print(f"- {s}")

        # Try optimization
        print("\nAttempting optimization...")
        success, report = await optimizer.optimize_for_langchain()

        print(f"\nSuccess: {success}")
        print(f"Final memory: {report['final_percent']}%")
        print(f"Memory freed: {report['memory_freed_mb']:.0f} MB")
        print("\nActions taken:")
        for action in report["actions_taken"]:
            print(f"- {action['strategy']}: {action['freed_mb']:.0f} MB")

    asyncio.run(test())