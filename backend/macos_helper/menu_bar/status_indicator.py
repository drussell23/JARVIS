"""
JARVIS macOS Helper - Menu Bar Status Indicator

Native PyObjC implementation for macOS menu bar integration.

Features:
- Real-time status updates
- Dynamic menu construction
- Live statistics display
- Permission status monitoring
- Quick actions
- Dark/light mode support
- Notification integration

Apple Compliance:
- Uses only public AppKit APIs (NSStatusBar, NSMenu)
- Respects system appearance
- Proper delegate patterns
- ARC-compatible memory management
"""

from __future__ import annotations

import asyncio
import logging
import threading
import weakref
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, TYPE_CHECKING

logger = logging.getLogger(__name__)

# Try to import PyObjC for native menu bar
try:
    import objc
    from AppKit import (
        NSApplication,
        NSStatusBar,
        NSMenu,
        NSMenuItem,
        NSImage,
        NSVariableStatusItemLength,
        NSFont,
        NSAttributedString,
        NSForegroundColorAttributeName,
        NSFontAttributeName,
        NSColor,
        NSUserNotificationCenter,
        NSUserNotification,
    )
    from Foundation import (
        NSObject,
        NSTimer,
        NSRunLoop,
        NSDefaultRunLoopMode,
    )
    PYOBJC_AVAILABLE = True
except ImportError:
    PYOBJC_AVAILABLE = False
    logger.warning("PyObjC not available - menu bar will use fallback mode")


# =============================================================================
# Enums and Types
# =============================================================================

class MenuBarState(str, Enum):
    """State of the menu bar indicator."""
    OFFLINE = "offline"
    INITIALIZING = "initializing"
    ONLINE = "online"
    PAUSED = "paused"
    DEGRADED = "degraded"
    ERROR = "error"


class StatusIcon(str, Enum):
    """Status icons for different states."""
    OFFLINE = "circle"
    INITIALIZING = "circle.dotted"
    ONLINE = "circle.fill"
    PAUSED = "pause.circle.fill"
    DEGRADED = "exclamationmark.circle.fill"
    ERROR = "xmark.circle.fill"

    # Activity indicators
    LISTENING = "waveform"
    PROCESSING = "gearshape"
    SPEAKING = "speaker.wave.2.fill"


# State to icon/color mapping
STATE_APPEARANCE = {
    MenuBarState.OFFLINE: ("○", "gray"),
    MenuBarState.INITIALIZING: ("◌", "yellow"),
    MenuBarState.ONLINE: ("●", "green"),
    MenuBarState.PAUSED: ("⏸", "orange"),
    MenuBarState.DEGRADED: ("⚠", "yellow"),
    MenuBarState.ERROR: ("✕", "red"),
}


# =============================================================================
# Menu Bar Statistics
# =============================================================================

@dataclass
class MenuBarStats:
    """Statistics displayed in the menu bar."""
    active_apps: int = 0
    tracked_windows: int = 0
    pending_notifications: int = 0
    events_processed: int = 0
    uptime_seconds: float = 0
    last_activity: Optional[str] = None
    voice_active: bool = False
    current_space: Optional[int] = None
    cpu_usage: Optional[float] = None
    memory_usage: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "active_apps": self.active_apps,
            "tracked_windows": self.tracked_windows,
            "pending_notifications": self.pending_notifications,
            "events_processed": self.events_processed,
            "uptime_seconds": self.uptime_seconds,
            "last_activity": self.last_activity,
            "voice_active": self.voice_active,
            "current_space": self.current_space,
            "cpu_usage": self.cpu_usage,
            "memory_usage": self.memory_usage,
        }


# =============================================================================
# Menu Item Callback Registry
# =============================================================================

class CallbackRegistry:
    """Registry for menu item callbacks (prevents garbage collection)."""

    def __init__(self):
        self._callbacks: Dict[str, Callable] = {}
        self._lock = threading.Lock()

    def register(self, identifier: str, callback: Callable) -> None:
        """Register a callback."""
        with self._lock:
            self._callbacks[identifier] = callback

    def unregister(self, identifier: str) -> None:
        """Unregister a callback."""
        with self._lock:
            self._callbacks.pop(identifier, None)

    def get(self, identifier: str) -> Optional[Callable]:
        """Get a callback by identifier."""
        with self._lock:
            return self._callbacks.get(identifier)

    def clear(self) -> None:
        """Clear all callbacks."""
        with self._lock:
            self._callbacks.clear()


# =============================================================================
# PyObjC Menu Bar Implementation
# =============================================================================

if PYOBJC_AVAILABLE:

    def _resolve_menu_bar_delegate_class():
        """
        Resolve Objective-C delegate class idempotently.

        PyObjC registers Objective-C class names globally in the process.
        If this module is imported through multiple package paths
        (for example ``macos_helper`` and ``backend.macos_helper``),
        re-defining the same Objective-C class causes:
            "overriding existing Objective-C class"
        """
        objc_name = "JARVISMenuBarDelegate"
        try:
            existing = objc.lookUpClass(objc_name)
            if existing is not None:
                return existing
        except Exception:
            pass

        class _MenuBarDelegate(NSObject):
            """Objective-C delegate for menu bar actions."""

            __objc_name__ = objc_name

            def initWithIndicator_(self, indicator):
                """Initialize with parent indicator."""
                self = objc.super(_MenuBarDelegate, self).init()
                if self is None:
                    return None
                self._indicator = weakref.ref(indicator)
                return self

            @objc.python_method
            def indicator(self):
                """Get the parent indicator."""
                ref = self._indicator
                return ref() if ref else None

            def menuAction_(self, sender):
                """Handle menu item action."""
                indicator = self.indicator()
                if indicator and hasattr(sender, 'representedObject'):
                    action_id = sender.representedObject()
                    if action_id:
                        indicator._handle_action(action_id)

            def pauseAction_(self, sender):
                """Handle pause action."""
                indicator = self.indicator()
                if indicator:
                    indicator._handle_action("pause")

            def resumeAction_(self, sender):
                """Handle resume action."""
                indicator = self.indicator()
                if indicator:
                    indicator._handle_action("resume")

            def restartAction_(self, sender):
                """Handle restart action."""
                indicator = self.indicator()
                if indicator:
                    indicator._handle_action("restart")

            def settingsAction_(self, sender):
                """Handle settings action."""
                indicator = self.indicator()
                if indicator:
                    indicator._handle_action("settings")

            def quitAction_(self, sender):
                """Handle quit action."""
                indicator = self.indicator()
                if indicator:
                    indicator._handle_action("quit")

            def permissionAction_(self, sender):
                """Handle permission action."""
                indicator = self.indicator()
                if indicator and hasattr(sender, 'representedObject'):
                    permission_type = sender.representedObject()
                    if permission_type:
                        indicator._handle_action(f"permission_{permission_type}")

        return _MenuBarDelegate

    MenuBarDelegate = _resolve_menu_bar_delegate_class()


class MenuBarIndicator:
    """
    Native macOS menu bar status indicator for JARVIS.

    Uses PyObjC for native AppKit integration, providing:
    - Real-time status display
    - Dynamic menu updates
    - System appearance integration
    - Efficient memory management
    """

    def __init__(self):
        self._state = MenuBarState.OFFLINE
        self._stats = MenuBarStats()
        self._started_at: Optional[datetime] = None
        self._activity_message: Optional[str] = None

        # PyObjC components
        self._status_item = None
        self._menu = None
        self._delegate = None
        self._update_timer = None

        # Callback registry
        self._callbacks = CallbackRegistry()
        self._action_handlers: Dict[str, Callable] = {}

        # Thread safety
        self._lock = threading.Lock()
        self._main_thread_queue: List[Callable] = []

        # Permission status cache
        self._permission_status: Dict[str, str] = {}

        # Update interval (seconds)
        self._update_interval = 2.0

        logger.debug("MenuBarIndicator initialized")

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def start(self) -> bool:
        """
        Start the menu bar indicator.

        Returns:
            True if started successfully
        """
        if not PYOBJC_AVAILABLE:
            logger.error("PyObjC not available - cannot start menu bar")
            return False

        try:
            self._state = MenuBarState.INITIALIZING
            self._started_at = datetime.now()

            # Create status item on main thread
            await self._run_on_main_thread(self._create_status_item)

            # Start update timer
            await self._run_on_main_thread(self._start_update_timer)

            self._state = MenuBarState.ONLINE
            logger.info("Menu bar indicator started")
            return True

        except Exception as e:
            logger.error(f"Failed to start menu bar indicator: {e}")
            self._state = MenuBarState.ERROR
            return False

    async def stop(self) -> None:
        """Stop the menu bar indicator."""
        try:
            # Stop update timer
            if self._update_timer:
                await self._run_on_main_thread(self._stop_update_timer)

            # Remove status item
            if self._status_item:
                await self._run_on_main_thread(self._remove_status_item)

            self._callbacks.clear()
            self._state = MenuBarState.OFFLINE
            logger.info("Menu bar indicator stopped")

        except Exception as e:
            logger.error(f"Error stopping menu bar indicator: {e}")

    # =========================================================================
    # Main Thread Operations
    # =========================================================================

    async def _run_on_main_thread(self, func: Callable, *args) -> Any:
        """
        Run a function on the main thread (required for AppKit).

        Args:
            func: Function to run
            *args: Arguments to pass

        Returns:
            Result of the function
        """
        if not PYOBJC_AVAILABLE:
            return None

        result_container = {"result": None, "error": None, "done": False}

        def wrapper():
            try:
                result_container["result"] = func(*args) if args else func()
            except Exception as e:
                result_container["error"] = e
            finally:
                result_container["done"] = True

        # Schedule on main thread
        from Foundation import NSThread
        if NSThread.isMainThread():
            wrapper()
        else:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(wrapper)

            # Wait for completion with timeout
            timeout = 5.0
            start = datetime.now()
            while not result_container["done"]:
                await asyncio.sleep(0.01)
                if (datetime.now() - start).total_seconds() > timeout:
                    raise TimeoutError("Main thread operation timed out")

        if result_container["error"]:
            raise result_container["error"]

        return result_container["result"]

    # =========================================================================
    # Status Item Management
    # =========================================================================

    def _create_status_item(self) -> None:
        """Create the NSStatusItem (must run on main thread)."""
        if not PYOBJC_AVAILABLE:
            return

        # Create status bar item
        status_bar = NSStatusBar.systemStatusBar()
        self._status_item = status_bar.statusItemWithLength_(
            NSVariableStatusItemLength
        )

        # Create delegate
        self._delegate = MenuBarDelegate.alloc().initWithIndicator_(self)

        # Set initial title
        self._update_title()

        # Create and set menu
        self._create_menu()

        # Make visible
        self._status_item.setHighlightMode_(True)

    def _remove_status_item(self) -> None:
        """Remove the NSStatusItem (must run on main thread)."""
        if self._status_item:
            status_bar = NSStatusBar.systemStatusBar()
            status_bar.removeStatusItem_(self._status_item)
            self._status_item = None
            self._delegate = None
            self._menu = None

    def _update_title(self) -> None:
        """Update the status bar title."""
        if not self._status_item:
            return

        icon, _ = STATE_APPEARANCE.get(
            self._state,
            STATE_APPEARANCE[MenuBarState.OFFLINE]
        )

        # Build title with icon and optional activity
        if self._activity_message:
            title = f"{icon} JARVIS"
        else:
            title = f"{icon} JARVIS"

        self._status_item.setTitle_(title)

    # =========================================================================
    # Menu Construction
    # =========================================================================

    def _create_menu(self) -> None:
        """Create the dropdown menu (must run on main thread)."""
        if not PYOBJC_AVAILABLE or not self._status_item:
            return

        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)

        # Status header
        self._add_status_section(menu)
        menu.addItem_(NSMenuItem.separatorItem())

        # Monitoring stats
        self._add_monitoring_section(menu)
        menu.addItem_(NSMenuItem.separatorItem())

        # Quick actions
        self._add_actions_section(menu)
        menu.addItem_(NSMenuItem.separatorItem())

        # Permissions
        self._add_permissions_section(menu)
        menu.addItem_(NSMenuItem.separatorItem())

        # Footer
        self._add_footer_section(menu)

        self._menu = menu
        self._status_item.setMenu_(menu)

    def _add_status_section(self, menu: 'NSMenu') -> None:
        """Add status header section."""
        icon, color = STATE_APPEARANCE.get(
            self._state,
            STATE_APPEARANCE[MenuBarState.OFFLINE]
        )

        status_text = f"Status: {self._state.value.title()}"
        if self._activity_message:
            status_text += f"\n  {self._activity_message}"

        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            status_text, None, ""
        )
        item.setEnabled_(False)
        menu.addItem_(item)

    def _add_monitoring_section(self, menu: 'NSMenu') -> None:
        """Add monitoring statistics section."""
        # Section header
        header = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Monitoring", None, ""
        )
        header.setEnabled_(False)
        menu.addItem_(header)

        # Stats items
        stats_items = [
            f"  Apps: {self._stats.active_apps} active",
            f"  Windows: {self._stats.tracked_windows} tracked",
            f"  Notifications: {self._stats.pending_notifications} pending",
            f"  Events: {self._stats.events_processed} processed",
        ]

        if self._stats.current_space:
            stats_items.append(f"  Space: {self._stats.current_space}")

        if self._stats.voice_active:
            stats_items.append("  Voice: Active")

        for text in stats_items:
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                text, None, ""
            )
            item.setEnabled_(False)
            menu.addItem_(item)

    def _add_actions_section(self, menu: 'NSMenu') -> None:
        """Add quick actions section."""
        # Section header
        header = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quick Actions", None, ""
        )
        header.setEnabled_(False)
        menu.addItem_(header)

        # Pause/Resume based on state
        if self._state == MenuBarState.PAUSED:
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "  Resume Monitoring", "resumeAction:", ""
            )
            item.setTarget_(self._delegate)
        else:
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "  Pause Monitoring", "pauseAction:", ""
            )
            item.setTarget_(self._delegate)
        menu.addItem_(item)

        # Restart
        restart_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "  Restart Service", "restartAction:", ""
        )
        restart_item.setTarget_(self._delegate)
        menu.addItem_(restart_item)

        # Settings
        settings_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "  Open Settings", "settingsAction:", ""
        )
        settings_item.setTarget_(self._delegate)
        settings_item.setKeyEquivalent_(",")
        menu.addItem_(settings_item)

    def _add_permissions_section(self, menu: 'NSMenu') -> None:
        """Add permissions status section."""
        # Section header
        header = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Permissions", None, ""
        )
        header.setEnabled_(False)
        menu.addItem_(header)

        # Permission items
        permission_items = [
            ("accessibility", "Accessibility"),
            ("screen_recording", "Screen Recording"),
            ("microphone", "Microphone"),
            ("notifications", "Notifications"),
        ]

        for perm_id, perm_name in permission_items:
            status = self._permission_status.get(perm_id, "unknown")

            if status == "granted":
                icon = "✓"
                enabled = False
            elif status == "denied":
                icon = "✕"
                enabled = True  # Allow clicking to open settings
            else:
                icon = "?"
                enabled = True

            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                f"  {icon} {perm_name}", "permissionAction:", ""
            )
            item.setTarget_(self._delegate)
            item.setRepresentedObject_(perm_id)
            item.setEnabled_(enabled)
            menu.addItem_(item)

    def _add_footer_section(self, menu: 'NSMenu') -> None:
        """Add footer section (About, Quit)."""
        # Uptime
        if self._started_at:
            uptime = datetime.now() - self._started_at
            hours, remainder = divmod(int(uptime.total_seconds()), 3600)
            minutes, seconds = divmod(remainder, 60)
            uptime_str = f"Uptime: {hours}h {minutes}m {seconds}s"

            uptime_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                uptime_str, None, ""
            )
            uptime_item.setEnabled_(False)
            menu.addItem_(uptime_item)
            menu.addItem_(NSMenuItem.separatorItem())

        # About
        about_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "About JARVIS", "menuAction:", ""
        )
        about_item.setTarget_(self._delegate)
        about_item.setRepresentedObject_("about")
        menu.addItem_(about_item)

        # Quit
        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit JARVIS Helper", "quitAction:", ""
        )
        quit_item.setTarget_(self._delegate)
        quit_item.setKeyEquivalent_("q")
        menu.addItem_(quit_item)

    # =========================================================================
    # Update Timer
    # =========================================================================

    def _start_update_timer(self) -> None:
        """Start the periodic update timer."""
        if not PYOBJC_AVAILABLE:
            return

        self._update_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            self._update_interval,
            self._delegate,
            objc.selector(self._timer_fired, signature=b'v@:@'),
            None,
            True
        )

    def _stop_update_timer(self) -> None:
        """Stop the periodic update timer."""
        if self._update_timer:
            self._update_timer.invalidate()
            self._update_timer = None

    def _timer_fired(self, timer) -> None:
        """Handle timer tick - update menu."""
        try:
            self._update_title()
            # Recreate menu to reflect new stats
            if self._status_item:
                self._create_menu()
        except Exception as e:
            logger.error(f"Error in timer update: {e}")

    # =========================================================================
    # Action Handling
    # =========================================================================

    def _handle_action(self, action_id: str) -> None:
        """
        Handle a menu action.

        Args:
            action_id: Identifier for the action
        """
        logger.debug(f"Menu action: {action_id}")

        # Check for registered handler
        handler = self._action_handlers.get(action_id)
        if handler:
            try:
                # Run handler in background to not block UI
                asyncio.create_task(self._run_handler(handler, action_id))
            except Exception as e:
                logger.error(f"Error running action handler: {e}")
            return

        # Default handlers
        if action_id == "quit":
            self._handle_quit()
        elif action_id == "about":
            self._show_about()
        elif action_id.startswith("permission_"):
            perm_type = action_id.replace("permission_", "")
            self._open_permission_settings(perm_type)

    async def _run_handler(self, handler: Callable, action_id: str) -> None:
        """Run an action handler (async-safe)."""
        try:
            result = handler(action_id)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            logger.error(f"Error in action handler for {action_id}: {e}")

    def _handle_quit(self) -> None:
        """Handle quit action."""
        # Emit quit event or call registered handler
        handler = self._action_handlers.get("quit")
        if handler:
            try:
                asyncio.create_task(self._run_handler(handler, "quit"))
            except Exception:
                pass

    def _show_about(self) -> None:
        """Show about dialog."""
        import subprocess
        subprocess.run([
            "osascript", "-e",
            '''display dialog "JARVIS AI Assistant\n\nPhase 1: macOS Helper Layer\n\nAn intelligent AI OS layer for macOS\n\nVersion: 1.0.0" '''
            '''buttons {"OK"} default button "OK" with title "About JARVIS"'''
        ], capture_output=True)

    def _open_permission_settings(self, permission_type: str) -> None:
        """Open System Preferences for a permission type."""
        import subprocess

        pane_map = {
            "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
            "screen_recording": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
            "microphone": "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
            "notifications": "x-apple.systempreferences:com.apple.preference.notifications",
        }

        url = pane_map.get(permission_type)
        if url:
            subprocess.run(["open", url], capture_output=True)

    # =========================================================================
    # Public API
    # =========================================================================

    def set_state(self, state: MenuBarState) -> None:
        """
        Set the current state.

        Args:
            state: New state
        """
        with self._lock:
            self._state = state

    def set_activity(self, message: Optional[str]) -> None:
        """
        Set the current activity message.

        Args:
            message: Activity message (None to clear)
        """
        with self._lock:
            self._activity_message = message

    def update_stats(self, stats: MenuBarStats) -> None:
        """
        Update the displayed statistics.

        Args:
            stats: New statistics
        """
        with self._lock:
            self._stats = stats

    def set_permission_status(self, permission_type: str, status: str) -> None:
        """
        Update permission status.

        Args:
            permission_type: Permission identifier
            status: Status string (granted, denied, unknown)
        """
        with self._lock:
            self._permission_status[permission_type] = status

    def register_action_handler(self, action_id: str, handler: Callable) -> None:
        """
        Register a handler for a menu action.

        Args:
            action_id: Action identifier (pause, resume, restart, settings, quit)
            handler: Handler function (can be async)
        """
        self._action_handlers[action_id] = handler

    def unregister_action_handler(self, action_id: str) -> None:
        """
        Unregister an action handler.

        Args:
            action_id: Action identifier
        """
        self._action_handlers.pop(action_id, None)

    def show_notification(
        self,
        title: str,
        message: str,
        subtitle: Optional[str] = None,
    ) -> None:
        """
        Show a system notification.

        Args:
            title: Notification title
            message: Notification body
            subtitle: Optional subtitle
        """
        if not PYOBJC_AVAILABLE:
            logger.warning("Cannot show notification - PyObjC not available")
            return

        try:
            notification = NSUserNotification.alloc().init()
            notification.setTitle_(title)
            notification.setInformativeText_(message)
            if subtitle:
                notification.setSubtitle_(subtitle)

            center = NSUserNotificationCenter.defaultUserNotificationCenter()
            center.deliverNotification_(notification)

        except Exception as e:
            logger.error(f"Failed to show notification: {e}")

    @property
    def state(self) -> MenuBarState:
        """Get current state."""
        return self._state

    @property
    def stats(self) -> MenuBarStats:
        """Get current stats."""
        return self._stats


# =============================================================================
# Singleton Management
# =============================================================================

_menu_bar: Optional[MenuBarIndicator] = None


def get_menu_bar() -> Optional[MenuBarIndicator]:
    """
    Get the global menu bar indicator instance.

    Returns:
        MenuBarIndicator instance or None if not started
    """
    return _menu_bar


async def start_menu_bar() -> Optional[MenuBarIndicator]:
    """
    Start the global menu bar indicator.

    Returns:
        MenuBarIndicator instance or None if failed
    """
    global _menu_bar

    if _menu_bar is not None:
        return _menu_bar

    _menu_bar = MenuBarIndicator()
    success = await _menu_bar.start()

    if not success:
        _menu_bar = None
        return None

    return _menu_bar


async def stop_menu_bar() -> None:
    """Stop the global menu bar indicator."""
    global _menu_bar

    if _menu_bar is not None:
        await _menu_bar.stop()
        _menu_bar = None
