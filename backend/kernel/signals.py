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
        Install signal handlers via the central SignalDispatcher.

        Args:
            shutdown_coordinator: Coordinator for graceful shutdown
            loop: Event loop for async signal handling
        """
        self._shutdown_coordinator = shutdown_coordinator or ShutdownCoordinator()
        self._loop = loop

        # Register with the central dispatcher instead of calling
        # signal.signal() directly.  Priority 200 = fallback handler.
        dispatcher = get_signal_dispatcher()
        if loop is not None:
            dispatcher.set_loop(loop)

        for sig in (signal.SIGINT, signal.SIGTERM):
            dispatcher.register(
                sig, self._handle_signal,
                name="KernelSignalHandler", priority=200,
            )

        if hasattr(signal, 'SIGHUP'):
            dispatcher.register(
                signal.SIGHUP, self._handle_signal,
                name="KernelSignalHandler", priority=200,
            )

        if hasattr(signal, 'SIGUSR1'):
            dispatcher.register(
                signal.SIGUSR1, self._handle_signal,
                name="KernelSignalHandler", priority=200,
            )

        logger.debug("[KernelSignalHandler] Signal handlers installed via dispatcher")
    
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
# CENTRAL SIGNAL DISPATCHER
# =============================================================================

@dataclass(order=True)
class _PrioritizedCallback:
    """A callback with a priority for ordered dispatch."""
    priority: int
    name: str = field(compare=False)
    callback: Callable[[int, Any], None] = field(compare=False)


class SignalDispatcher:
    """
    Central, authoritative signal dispatcher.

    All components MUST register callbacks here instead of calling
    ``signal.signal()`` or ``loop.add_signal_handler()`` directly.
    The dispatcher owns the OS-level handler and fans out to callbacks
    in ascending priority order (lower number = called first).

    Priority guide:
        10  - Forensic logging / diagnostics (shutdown_hook)
        50  - Resource cleanup (shutdown_hook cleanup)
        100 - Kernel shutdown coordination (UnifiedSignalHandler)
        200 - KernelSignalHandler fallback

    Thread-safety: Registration is guarded by a threading lock.
    Callbacks are invoked from the signal-handler context (sync) or
    from an asyncio loop callback (when an event loop is available).
    """

    _instance: Optional["SignalDispatcher"] = None

    def __new__(cls) -> "SignalDispatcher":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_done = False
        return cls._instance

    def __init__(self) -> None:
        if self._init_done:
            return
        self._init_done = True
        self._lock = threading.Lock()
        # signal_num -> sorted list of _PrioritizedCallback
        self._callbacks: Dict[int, List[_PrioritizedCallback]] = {}
        self._installed_signals: Set[int] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ── public API ──────────────────────────────────────────────────────

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Store the running event loop for async-aware dispatch."""
        self._loop = loop

    def register(
        self,
        sig: int,
        callback: Callable[[int, Any], None],
        *,
        name: str = "",
        priority: int = 100,
    ) -> None:
        """
        Register a callback for *sig*.

        Args:
            sig:      Signal number (e.g. ``signal.SIGINT``).
            callback: ``(signum, frame) -> None``.  Called in priority order.
            name:     Human-readable label (for logging).
            priority: Lower = called first.  See class docstring for guide.
        """
        entry = _PrioritizedCallback(priority=priority, name=name or repr(callback), callback=callback)
        with self._lock:
            self._callbacks.setdefault(sig, []).append(entry)
            self._callbacks[sig].sort()  # maintain priority order
            self._ensure_os_handler(sig)
        logger.debug(
            f"[SignalDispatcher] Registered '{entry.name}' for "
            f"{get_signal_name(sig)} @ priority {priority}"
        )

    def unregister(self, sig: int, callback: Callable[[int, Any], None]) -> bool:
        """Remove a previously registered callback.  Returns True if found."""
        with self._lock:
            entries = self._callbacks.get(sig, [])
            for i, entry in enumerate(entries):
                if entry.callback is callback:
                    entries.pop(i)
                    logger.debug(
                        f"[SignalDispatcher] Unregistered '{entry.name}' "
                        f"from {get_signal_name(sig)}"
                    )
                    return True
        return False

    # ── internals ───────────────────────────────────────────────────────

    def _ensure_os_handler(self, sig: int) -> None:
        """Install our master handler on the OS for *sig* (idempotent)."""
        if sig in self._installed_signals:
            return
        try:
            signal.signal(sig, self._dispatch)
            self._installed_signals.add(sig)
        except (OSError, ValueError):
            pass  # e.g. SIGKILL, or not main thread

    def _dispatch(self, signum: int, frame: Any) -> None:
        """Master OS handler — fans out to registered callbacks."""
        with self._lock:
            entries = list(self._callbacks.get(signum, []))

        for entry in entries:
            try:
                entry.callback(signum, frame)
            except Exception:
                logger.exception(
                    f"[SignalDispatcher] Callback '{entry.name}' "
                    f"raised on {get_signal_name(signum)}"
                )


# Module-level singleton accessor
_signal_dispatcher: Optional[SignalDispatcher] = None


def get_signal_dispatcher() -> SignalDispatcher:
    """Get the global SignalDispatcher singleton."""
    global _signal_dispatcher
    if _signal_dispatcher is None:
        _signal_dispatcher = SignalDispatcher()
    return _signal_dispatcher


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
