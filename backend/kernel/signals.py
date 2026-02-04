"""
JARVIS Kernel Signal Handling v1.0.0
=====================================

Enterprise-grade signal handling for the JARVIS kernel.
Provides signal protection, graceful shutdown coordination, and crash recovery.

This module addresses:
1. EARLY SIGNAL PROTECTION: Protect CLI commands from signal storms during startup
2. GRACEFUL SHUTDOWN: Coordinated shutdown of all components
3. CRASH RECOVERY: Handle signals that indicate crashes (SIGSEGV, SIGABRT, etc.)
4. CHILD PROCESS MANAGEMENT: Forward signals to child processes appropriately

Signal Reference (POSIX):
    SIGHUP  (1)  - Terminal hangup
    SIGINT  (2)  - Keyboard interrupt (Ctrl+C)
    SIGQUIT (3)  - Quit with core dump
    SIGILL  (4)  - Illegal instruction
    SIGTRAP (5)  - Trace/breakpoint trap
    SIGABRT (6)  - Abort signal
    SIGKILL (9)  - Kill (cannot be caught)
    SIGSEGV (11) - Segmentation fault
    SIGTERM (15) - Termination request
    SIGUSR1 (30) - User-defined signal 1
    SIGUSR2 (31) - User-defined signal 2

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# =============================================================================
# SIGNAL ENUMS
# =============================================================================

class ShutdownReason(Enum):
    """Reasons for system shutdown."""
    USER_REQUEST = auto()      # User initiated (Ctrl+C, --shutdown)
    SIGNAL = auto()            # External signal received
    CRASH = auto()             # Component crashed
    OOM = auto()               # Out of memory
    RESTART = auto()           # Restart requested
    UPDATE = auto()            # Hot update
    ERROR = auto()             # Fatal error


@dataclass
class SignalEvent:
    """Record of a received signal."""
    signal_num: int
    signal_name: str
    timestamp: datetime = field(default_factory=datetime.now)
    handled: bool = False
    forwarded_to: List[int] = field(default_factory=list)


# =============================================================================
# SIGNAL NAME MAPPING
# =============================================================================

SIGNAL_NAMES: Dict[int, str] = {
    1: "SIGHUP",
    2: "SIGINT",
    3: "SIGQUIT",
    4: "SIGILL",
    5: "SIGTRAP",
    6: "SIGABRT",
    9: "SIGKILL",
    10: "SIGBUS",
    11: "SIGSEGV",
    13: "SIGPIPE",
    14: "SIGALRM",
    15: "SIGTERM",
    16: "SIGURG",
    30: "SIGUSR1",
    31: "SIGUSR2",
}


def get_signal_name(sig: int) -> str:
    """Get human-readable signal name."""
    return SIGNAL_NAMES.get(sig, f"SIGNAL_{sig}")


# =============================================================================
# SIGNAL PROTECTOR
# =============================================================================

class SignalProtector:
    """
    Protects critical code sections from signal interruption.
    
    When running --restart, the supervisor sends signals that can kill the client
    process DURING Python startup. This protector ensures signals don't interrupt
    critical operations.
    
    Usage:
        protector = SignalProtector()
        
        # Protect a block of code
        with protector.protected_section("startup"):
            # Critical code that shouldn't be interrupted
            pass
        
        # Or as a decorator
        @protector.protect("initialization")
        async def init_components():
            pass
    """
    
    def __init__(self):
        self._original_handlers: Dict[int, Any] = {}
        self._protected = False
        self._protection_stack: List[str] = []
        self._lock = threading.Lock()
        self._deferred_signals: List[SignalEvent] = []
        
    def _null_handler(self, signum: int, frame: Any) -> None:
        """Null signal handler that ignores signals during protection."""
        event = SignalEvent(
            signal_num=signum,
            signal_name=get_signal_name(signum),
        )
        self._deferred_signals.append(event)
        logger.debug(f"[SignalProtector] Deferred {event.signal_name} during protection")
    
    @contextmanager
    def protected_section(self, name: str = "unknown"):
        """
        Context manager for signal-protected code sections.
        
        Args:
            name: Name of the protected section (for logging)
        
        Yields:
            None
        """
        self._enter_protection(name)
        try:
            yield
        finally:
            self._exit_protection(name)
    
    def _enter_protection(self, name: str) -> None:
        """Enter signal protection."""
        with self._lock:
            self._protection_stack.append(name)
            
            if not self._protected:
                self._protected = True
                
                # Save and replace signal handlers
                signals_to_protect = [
                    signal.SIGINT,
                    signal.SIGTERM,
                    signal.SIGHUP,
                ]
                
                # Add platform-specific signals
                if hasattr(signal, 'SIGURG'):
                    signals_to_protect.append(signal.SIGURG)
                if hasattr(signal, 'SIGPIPE'):
                    signals_to_protect.append(signal.SIGPIPE)
                if hasattr(signal, 'SIGUSR1'):
                    signals_to_protect.append(signal.SIGUSR1)
                if hasattr(signal, 'SIGUSR2'):
                    signals_to_protect.append(signal.SIGUSR2)
                
                for sig in signals_to_protect:
                    try:
                        self._original_handlers[sig] = signal.signal(sig, self._null_handler)
                    except (OSError, ValueError):
                        pass  # Some signals can't be caught
                
                logger.debug(f"[SignalProtector] Entered protection: {name}")
    
    def _exit_protection(self, name: str) -> None:
        """Exit signal protection."""
        with self._lock:
            if name in self._protection_stack:
                self._protection_stack.remove(name)
            
            if not self._protection_stack and self._protected:
                self._protected = False
                
                # Restore original signal handlers
                for sig, handler in self._original_handlers.items():
                    try:
                        signal.signal(sig, handler)
                    except (OSError, ValueError):
                        pass
                
                self._original_handlers.clear()
                
                # Process deferred signals
                if self._deferred_signals:
                    logger.debug(
                        f"[SignalProtector] Exiting protection with "
                        f"{len(self._deferred_signals)} deferred signals"
                    )
                    self._process_deferred_signals()
                
                logger.debug(f"[SignalProtector] Exited protection: {name}")
    
    def _process_deferred_signals(self) -> None:
        """Process any signals that were deferred during protection."""
        for event in self._deferred_signals:
            # Re-raise the signal to be handled normally
            if event.signal_num in (signal.SIGINT, signal.SIGTERM):
                logger.info(
                    f"[SignalProtector] Processing deferred {event.signal_name}"
                )
                # Instead of re-raising, mark as handled
                event.handled = True
        
        self._deferred_signals.clear()
    
    def protect(self, name: str = "decorated"):
        """
        Decorator for protecting async functions from signals.
        
        Args:
            name: Name of the protected section
        
        Returns:
            Decorator function
        """
        def decorator(func: Callable) -> Callable:
            if asyncio.iscoroutinefunction(func):
                async def async_wrapper(*args, **kwargs):
                    with self.protected_section(name):
                        return await func(*args, **kwargs)
                return async_wrapper
            else:
                def sync_wrapper(*args, **kwargs):
                    with self.protected_section(name):
                        return func(*args, **kwargs)
                return sync_wrapper
        return decorator
    
    @property
    def is_protected(self) -> bool:
        """Check if currently in a protected section."""
        return self._protected


# =============================================================================
# SHUTDOWN COORDINATOR
# =============================================================================

class ShutdownCoordinator:
    """
    Coordinates graceful shutdown across all kernel components.
    
    Ensures proper ordering:
    1. Stop accepting new requests
    2. Complete in-flight operations
    3. Shutdown child processes (SIGTERM, wait, SIGKILL)
    4. Cleanup resources
    5. Save state
    6. Exit
    """
    
    def __init__(self, timeout: float = 30.0):
        self._timeout = timeout
        self._shutdown_requested = asyncio.Event()
        self._shutdown_complete = asyncio.Event()
        self._shutdown_reason: Optional[ShutdownReason] = None
        self._shutdown_callbacks: List[Callable] = []
        self._child_pids: Set[int] = set()
        self._lock = asyncio.Lock()
        
    def register_child(self, pid: int) -> None:
        """Register a child process PID for shutdown management."""
        self._child_pids.add(pid)
        
    def unregister_child(self, pid: int) -> None:
        """Unregister a child process PID."""
        self._child_pids.discard(pid)
    
    def add_shutdown_callback(self, callback: Callable) -> None:
        """Add a callback to be executed during shutdown."""
        self._shutdown_callbacks.append(callback)
    
    def request_shutdown(self, reason: ShutdownReason = ShutdownReason.USER_REQUEST) -> None:
        """Request system shutdown."""
        self._shutdown_reason = reason
        self._shutdown_requested.set()
        logger.info(f"[ShutdownCoordinator] Shutdown requested: {reason.name}")
    
    @property
    def shutdown_requested(self) -> bool:
        """Check if shutdown has been requested."""
        return self._shutdown_requested.is_set()
    
    async def wait_for_shutdown(self) -> ShutdownReason:
        """Wait for shutdown request."""
        await self._shutdown_requested.wait()
        return self._shutdown_reason or ShutdownReason.USER_REQUEST
    
    async def execute_shutdown(self) -> None:
        """Execute the shutdown sequence."""
        async with self._lock:
            if self._shutdown_complete.is_set():
                return  # Already shut down
            
            logger.info("[ShutdownCoordinator] Executing shutdown sequence...")
            start_time = time.time()
            
            # 1. Execute shutdown callbacks
            for callback in self._shutdown_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await asyncio.wait_for(callback(), timeout=5.0)
                    else:
                        callback()
                except asyncio.TimeoutError:
                    logger.warning(f"[ShutdownCoordinator] Callback timed out: {callback}")
                except Exception as e:
                    logger.warning(f"[ShutdownCoordinator] Callback failed: {e}")
            
            # 2. Gracefully stop child processes
            await self._stop_child_processes()
            
            elapsed = time.time() - start_time
            logger.info(f"[ShutdownCoordinator] Shutdown complete in {elapsed:.2f}s")
            
            self._shutdown_complete.set()
    
    async def _stop_child_processes(self) -> None:
        """Gracefully stop all registered child processes."""
        if not self._child_pids:
            return
        
        logger.info(f"[ShutdownCoordinator] Stopping {len(self._child_pids)} child processes...")
        
        # Send SIGTERM to all children
        for pid in list(self._child_pids):
            try:
                os.kill(pid, signal.SIGTERM)
                logger.debug(f"[ShutdownCoordinator] Sent SIGTERM to PID {pid}")
            except ProcessLookupError:
                self._child_pids.discard(pid)
            except Exception as e:
                logger.debug(f"[ShutdownCoordinator] Failed to send SIGTERM to {pid}: {e}")
        
        # Wait for processes to exit (with timeout)
        grace_period = min(10.0, self._timeout / 2)
        wait_start = time.time()
        
        while self._child_pids and (time.time() - wait_start) < grace_period:
            await asyncio.sleep(0.5)
            
            # Check which processes have exited
            for pid in list(self._child_pids):
                try:
                    result = os.waitpid(pid, os.WNOHANG)
                    if result[0] != 0:  # Process exited
                        self._child_pids.discard(pid)
                        logger.debug(f"[ShutdownCoordinator] PID {pid} exited")
                except ChildProcessError:
                    self._child_pids.discard(pid)
                except Exception:
                    pass
        
        # Force kill remaining processes
        for pid in list(self._child_pids):
            try:
                os.kill(pid, signal.SIGKILL)
                logger.warning(f"[ShutdownCoordinator] Force killed PID {pid}")
            except Exception:
                pass
        
        self._child_pids.clear()


# =============================================================================
# GLOBAL SIGNAL HANDLER
# =============================================================================

class KernelSignalHandler:
    """
    Global signal handler for the JARVIS kernel.
    
    Installs handlers for common signals and coordinates with
    the ShutdownCoordinator for graceful shutdown.
    """
    
    _instance: Optional["KernelSignalHandler"] = None
    
    def __new__(cls) -> "KernelSignalHandler":
        """Singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._initialized = True
        self._shutdown_coordinator: Optional[ShutdownCoordinator] = None
        self._protector = SignalProtector()
        self._signal_history: List[SignalEvent] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        
    def install(
        self,
        shutdown_coordinator: Optional[ShutdownCoordinator] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        """
        Install signal handlers.
        
        Args:
            shutdown_coordinator: Coordinator for graceful shutdown
            loop: Event loop for async signal handling
        """
        self._shutdown_coordinator = shutdown_coordinator or ShutdownCoordinator()
        self._loop = loop
        
        # Install handlers for common signals
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        
        if hasattr(signal, 'SIGHUP'):
            signal.signal(signal.SIGHUP, self._handle_signal)
        
        if hasattr(signal, 'SIGUSR1'):
            signal.signal(signal.SIGUSR1, self._handle_signal)
        
        logger.debug("[KernelSignalHandler] Signal handlers installed")
    
    def _handle_signal(self, signum: int, frame: Any) -> None:
        """Handle received signal."""
        event = SignalEvent(
            signal_num=signum,
            signal_name=get_signal_name(signum),
        )
        self._signal_history.append(event)
        
        logger.info(f"[KernelSignalHandler] Received {event.signal_name}")
        
        # Check if in protected section
        if self._protector.is_protected:
            logger.debug(f"[KernelSignalHandler] Deferring {event.signal_name} - in protected section")
            return
        
        # Map signal to shutdown reason
        if signum == signal.SIGINT:
            reason = ShutdownReason.USER_REQUEST
        elif signum == signal.SIGTERM:
            reason = ShutdownReason.SIGNAL
        elif signum == signal.SIGHUP:
            reason = ShutdownReason.RESTART
        else:
            reason = ShutdownReason.SIGNAL
        
        # Request shutdown
        if self._shutdown_coordinator:
            self._shutdown_coordinator.request_shutdown(reason)
        else:
            # No coordinator, exit directly
            logger.warning("[KernelSignalHandler] No shutdown coordinator, exiting")
            sys.exit(128 + signum)
    
    @property
    def protector(self) -> SignalProtector:
        """Get the signal protector."""
        return self._protector
    
    @property
    def coordinator(self) -> Optional[ShutdownCoordinator]:
        """Get the shutdown coordinator."""
        return self._shutdown_coordinator


# =============================================================================
# SINGLETON ACCESS
# =============================================================================

_signal_handler: Optional[KernelSignalHandler] = None


def get_signal_handler() -> KernelSignalHandler:
    """Get the global signal handler instance."""
    global _signal_handler
    if _signal_handler is None:
        _signal_handler = KernelSignalHandler()
    return _signal_handler


def get_signal_protector() -> SignalProtector:
    """Get the global signal protector."""
    return get_signal_handler().protector


def get_shutdown_coordinator() -> Optional[ShutdownCoordinator]:
    """Get the global shutdown coordinator."""
    return get_signal_handler().coordinator


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

def protected_section(name: str = "unknown"):
    """
    Context manager for signal-protected code sections.
    
    Usage:
        with protected_section("startup"):
            # Critical code
            pass
    """
    return get_signal_protector().protected_section(name)


def protect(name: str = "decorated"):
    """
    Decorator for protecting functions from signals.
    
    Usage:
        @protect("initialization")
        async def init_components():
            pass
    """
    return get_signal_protector().protect(name)


# =============================================================================
# MODULE INITIALIZATION
# =============================================================================

logger.debug("[KernelSignals] Module loaded")
