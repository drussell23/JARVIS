"""
JARVIS Neural Mesh -- AccessibilityResolver

Resolves UI element descriptions to exact screen coordinates by querying the
macOS Accessibility (AX) tree.  Vision models (LLaVA) return approximate pixel
coordinates ("where is the search bar?") that are off by 50-100 px; the AX API
knows the exact position the OS renders each element.

Fallback chain (resolve):
  1. AX exact title match
  2. AX fuzzy title match (substring, case-insensitive)
  3. AX role match + description attribute
  4. AX placeholder/value match (text fields with placeholder text)
  5. AppleScript UI element query (different API path)
  6. Return None

Configuration (env vars -- no hardcoding):
  JARVIS_AX_MAX_DEPTH           -- max tree traversal depth   (default 15)
  JARVIS_AX_SEARCH_TIMEOUT      -- element search timeout sec  (default 5.0)

Usage:
    from backend.neural_mesh.agents.accessibility_resolver import (
        AccessibilityResolver,
        get_accessibility_resolver,
    )
    resolver = get_accessibility_resolver()
    rect = await resolver.resolve("Search", app_name="Safari")
    # rect == {"x": 812, "y": 53, "width": 420, "height": 22} or None
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Env-var helpers
# ---------------------------------------------------------------------------

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# GUI session check (same technique used by macos_space_detector.py)
# ---------------------------------------------------------------------------

def _is_gui_session() -> bool:
    """Return True when running inside a macOS GUI session (not headless)."""
    cached = os.environ.get("_JARVIS_GUI_SESSION")
    if cached is not None:
        return cached == "1"
    result = False
    if sys.platform == "darwin":
        if os.environ.get("JARVIS_HEADLESS", "").lower() in ("1", "true", "yes"):
            pass
        elif os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
            pass
        else:
            try:
                import ctypes
                cg = ctypes.cdll.LoadLibrary(
                    "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
                )
                cg.CGSessionCopyCurrentDictionary.restype = ctypes.c_void_p
                result = cg.CGSessionCopyCurrentDictionary() is not None
            except Exception:
                pass
    os.environ["_JARVIS_GUI_SESSION"] = "1" if result else "0"
    return result


# ---------------------------------------------------------------------------
# AccessibilityResolver
# ---------------------------------------------------------------------------

class AccessibilityResolver:
    """Resolves UI element descriptions to exact screen coordinates via macOS AX API."""

    def __init__(self) -> None:
        self._max_depth: int = _env_int("JARVIS_AX_MAX_DEPTH", 15)
        self._search_timeout: float = _env_float("JARVIS_AX_SEARCH_TIMEOUT", 5.0)
        self._gui_available: bool = _is_gui_session()
        self._ax_trusted: Optional[bool] = None  # lazily checked

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def resolve(
        self,
        description: str,
        app_name: str,
        role: Optional[str] = None,
        near_text: Optional[str] = None,
    ) -> Optional[Dict[str, int]]:
        """Return ``{"x", "y", "width", "height"}`` (center coords) or *None*.

        Fallback chain:
          1. AX exact title match
          2. AX fuzzy title match (substring, case-insensitive)
          3. AX role match + description attribute
          4. AX placeholder value match
          5. AppleScript UI element query
          6. None
        """
        if not self._gui_available:
            logger.warning("[AXResolver] No GUI session -- cannot query AX tree")
            return None

        self._ensure_permission_checked()

        pid = await asyncio.to_thread(self._get_pid_for_app, app_name)
        if pid is None:
            logger.warning("[AXResolver] App %r not running", app_name)
            return None

        # Steps 1-4: search the AX tree (with timeout)
        try:
            element = await asyncio.wait_for(
                self._search_ax_tree(
                    pid,
                    description,
                    role=role,
                    near_text=near_text,
                    max_depth=self._max_depth,
                ),
                timeout=self._search_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[AXResolver] AX tree search timed out (%.1fs) for %r in %r",
                self._search_timeout, description, app_name,
            )
            element = None

        if element is not None:
            rect = await asyncio.to_thread(self._get_element_center, element)
            if rect is not None:
                logger.info(
                    "[AXResolver] Resolved %r -> (%d, %d) [%dx%d]",
                    description, rect["x"], rect["y"],
                    rect["width"], rect["height"],
                )
                return rect

        # Step 5: AppleScript fallback
        logger.debug("[AXResolver] AX tree miss -- trying AppleScript fallback")
        try:
            rect = await asyncio.wait_for(
                self._applescript_fallback(app_name, description),
                timeout=self._search_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("[AXResolver] AppleScript fallback timed out")
            rect = None

        if rect is not None:
            logger.info(
                "[AXResolver] AppleScript resolved %r -> (%d, %d)",
                description, rect["x"], rect["y"],
            )
            return rect

        # Step 6: give up
        logger.info(
            "[AXResolver] Could not resolve %r in %r", description, app_name,
        )
        return None

    async def list_elements(
        self,
        app_name: str,
        max_depth: int = 5,
    ) -> List[Dict[str, Any]]:
        """Debug helper: list all accessible elements with roles, titles, positions."""
        if not self._gui_available:
            logger.warning("[AXResolver] No GUI session")
            return []

        pid = await asyncio.to_thread(self._get_pid_for_app, app_name)
        if pid is None:
            return []

        return await asyncio.to_thread(
            self._collect_elements_sync, pid, max_depth,
        )

    # ------------------------------------------------------------------
    # PID resolution
    # ------------------------------------------------------------------

    def _get_pid_for_app(self, app_name: str) -> Optional[int]:
        """Find PID by app name using NSWorkspace (case-insensitive)."""
        try:
            from AppKit import NSWorkspace  # lazy -- heavy import
        except ImportError:
            logger.error("[AXResolver] AppKit unavailable")
            return None

        lower = app_name.lower()
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            name = app.localizedName()
            if name and name.lower() == lower:
                return app.processIdentifier()

        # Partial / substring fallback
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            name = app.localizedName()
            if name and lower in name.lower():
                return app.processIdentifier()

        return None

    # ------------------------------------------------------------------
    # AX tree search (runs in thread via asyncio.to_thread)
    # ------------------------------------------------------------------

    async def _search_ax_tree(
        self,
        app_pid: int,
        description: str,
        role: Optional[str],
        near_text: Optional[str],
        max_depth: int = 15,
    ) -> Optional[Any]:
        """Search the AX tree for an element matching *description*.

        Match strategies tried in order per node:
          1. Exact title match
          2. Fuzzy title match (substring, case-insensitive)
          3. Role match + description attribute
          4. Placeholder / value match
        """
        return await asyncio.to_thread(
            self._search_ax_tree_sync,
            app_pid, description, role, near_text, max_depth,
        )

    def _search_ax_tree_sync(
        self,
        app_pid: int,
        description: str,
        role: Optional[str],
        near_text: Optional[str],
        max_depth: int,
    ) -> Optional[Any]:
        """Synchronous AX tree walker (called from a worker thread)."""
        try:
            from ApplicationServices import (
                AXUIElementCreateApplication,
                AXUIElementCopyAttributeValue,
            )
            # AX attribute constants are plain strings — Quartz may not export them
            try:
                from Quartz import kAXWindowsAttribute, kAXChildrenAttribute
            except ImportError:
                kAXWindowsAttribute = "AXWindows"
                kAXChildrenAttribute = "AXChildren"
        except ImportError as exc:
            logger.error("[AXResolver] Missing AX framework: %s", exc)
            return None

        app_element = AXUIElementCreateApplication(app_pid)
        if app_element is None:
            logger.error("[AXResolver] Cannot create AX element for PID %d", app_pid)
            return None

        err, windows = AXUIElementCopyAttributeValue(
            app_element, kAXWindowsAttribute, None,
        )
        if windows is None:
            logger.debug("[AXResolver] No windows for PID %d (err=%s)", app_pid, err)
            return None

        desc_lower = description.lower()

        # Try exact match pass, then fuzzy, role, placeholder -- all in one walk
        # but prioritized by a score.  Best-scored element wins.
        best: Optional[Any] = None
        best_score: int = 0

        for window in windows:
            candidate, score = self._walk_tree(
                window, desc_lower, role, near_text, max_depth,
            )
            if score > best_score:
                best = candidate
                best_score = score
            if best_score >= 100:
                break  # exact match -- stop early

        return best

    def _walk_tree(
        self,
        node: Any,
        desc_lower: str,
        role: Optional[str],
        near_text: Optional[str],
        depth: int,
    ) -> tuple:
        """Depth-limited tree walk returning (element, match_score).

        Scores:
          100 = exact title match
           80 = fuzzy title / description match
           60 = role + description attribute match
           40 = placeholder / value match
        """
        if depth <= 0:
            return None, 0

        try:
            from ApplicationServices import AXUIElementCopyAttributeValue
            from Quartz import kAXChildrenAttribute
        except ImportError:
            return None, 0

        score = self._score_element(
            node, desc_lower, role, near_text,
        )

        best_elem: Optional[Any] = node if score > 0 else None
        best_score: int = score

        if best_score >= 100:
            return best_elem, best_score

        # Recurse into children
        err, children = AXUIElementCopyAttributeValue(
            node, kAXChildrenAttribute, None,
        )
        if children:
            for child in children:
                cand, cscore = self._walk_tree(
                    child, desc_lower, role, near_text, depth - 1,
                )
                if cscore > best_score:
                    best_elem = cand
                    best_score = cscore
                if best_score >= 100:
                    return best_elem, best_score

        return best_elem, best_score

    def _score_element(
        self,
        node: Any,
        desc_lower: str,
        role: Optional[str],
        near_text: Optional[str],
    ) -> int:
        """Score a single AX element against the search criteria."""
        try:
            from ApplicationServices import AXUIElementCopyAttributeValue
            from Quartz import (
                kAXTitleAttribute,
                kAXRoleAttribute,
                kAXDescriptionAttribute,
                kAXValueAttribute,
            )
        except ImportError:
            return 0

        # ---------- title ----------
        err, title_val = AXUIElementCopyAttributeValue(
            node, kAXTitleAttribute, None,
        )
        title_str = str(title_val).lower() if title_val else ""

        if title_str and title_str == desc_lower:
            return 100  # exact title match

        if title_str and desc_lower in title_str:
            return 80  # fuzzy title match

        # ---------- description attribute ----------
        err, desc_val = AXUIElementCopyAttributeValue(
            node, kAXDescriptionAttribute, None,
        )
        desc_str = str(desc_val).lower() if desc_val else ""

        if desc_str and desc_lower in desc_str:
            return 80  # fuzzy description match

        # ---------- role + description / title ----------
        if role:
            err, role_val = AXUIElementCopyAttributeValue(
                node, kAXRoleAttribute, None,
            )
            role_str = str(role_val).lower() if role_val else ""
            if role_str and role.lower() in role_str:
                # Role matches -- check if description/title also partially match
                if desc_str and desc_lower in desc_str:
                    return 60
                if title_str and desc_lower in title_str:
                    return 60
                # role-only match (weaker) when no other criterion provided
                if not desc_lower:
                    return 60

        # ---------- placeholder / value ----------
        err, value_val = AXUIElementCopyAttributeValue(
            node, kAXValueAttribute, None,
        )
        value_str = str(value_val).lower() if value_val else ""

        if value_str and desc_lower in value_str:
            return 40

        # Check placeholderValue (AXPlaceholderValue)
        try:
            err, ph_val = AXUIElementCopyAttributeValue(
                node, "AXPlaceholderValue", None,
            )
            ph_str = str(ph_val).lower() if ph_val else ""
            if ph_str and desc_lower in ph_str:
                return 40
        except Exception:
            pass

        return 0

    # ------------------------------------------------------------------
    # Element geometry extraction
    # ------------------------------------------------------------------

    def _get_element_center(self, element: Any) -> Optional[Dict[str, int]]:
        """Extract element bounds and return center-based rect."""
        try:
            from ApplicationServices import AXUIElementCopyAttributeValue
            from Quartz import kAXPositionAttribute, kAXSizeAttribute
        except ImportError:
            return None

        err, pos_val = AXUIElementCopyAttributeValue(
            element, kAXPositionAttribute, None,
        )
        if pos_val is None:
            return None

        err, size_val = AXUIElementCopyAttributeValue(
            element, kAXSizeAttribute, None,
        )
        if size_val is None:
            return None

        try:
            # AXValue wraps CGPoint / CGSize -- use .x/.y and .width/.height
            x = float(pos_val.x)
            y = float(pos_val.y)
            width = float(size_val.width)
            height = float(size_val.height)
        except AttributeError:
            # Fallback: some pyobjc versions expose tuples
            try:
                x, y = float(pos_val[0]), float(pos_val[1])
                width, height = float(size_val[0]), float(size_val[1])
            except (TypeError, IndexError):
                return None

        return {
            "x": int(x + width / 2),
            "y": int(y + height / 2),
            "width": int(width),
            "height": int(height),
        }

    # ------------------------------------------------------------------
    # AppleScript fallback
    # ------------------------------------------------------------------

    async def _applescript_fallback(
        self,
        app_name: str,
        description: str,
    ) -> Optional[Dict[str, int]]:
        """Query UI elements via AppleScript as a backup path.

        Uses asyncio.create_subprocess_exec (argv-based, no shell injection).
        """
        # Sanitise inputs for AppleScript string interpolation
        safe_app = app_name.replace("\\", "\\\\").replace('"', '\\"')
        safe_desc = description.replace("\\", "\\\\").replace('"', '\\"')

        script = (
            f'tell application "System Events"\n'
            f'  tell process "{safe_app}"\n'
            f'    set _els to every UI element of window 1 '
            f'whose description contains "{safe_desc}"\n'
            f'    if (count of _els) > 0 then\n'
            f'      set _e to item 1 of _els\n'
            f'      set _pos to position of _e\n'
            f'      set _sz to size of _e\n'
            f'      return (item 1 of _pos as text) & "," '
            f'& (item 2 of _pos as text) & "," '
            f'& (item 1 of _sz as text) & "," '
            f'& (item 2 of _sz as text)\n'
            f'    else\n'
            f'      set _els to every UI element of window 1 '
            f'whose name contains "{safe_desc}"\n'
            f'      if (count of _els) > 0 then\n'
            f'        set _e to item 1 of _els\n'
            f'        set _pos to position of _e\n'
            f'        set _sz to size of _e\n'
            f'        return (item 1 of _pos as text) & "," '
            f'& (item 2 of _pos as text) & "," '
            f'& (item 1 of _sz as text) & "," '
            f'& (item 2 of _sz as text)\n'
            f'      end if\n'
            f'    end if\n'
            f'  end tell\n'
            f'end tell\n'
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                logger.debug(
                    "[AXResolver] AppleScript error: %s",
                    stderr.decode(errors="replace").strip(),
                )
                return None

            raw = stdout.decode(errors="replace").strip()
            if not raw:
                return None

            parts = raw.split(",")
            if len(parts) < 4:
                return None

            x = float(parts[0])
            y = float(parts[1])
            width = float(parts[2])
            height = float(parts[3])

            return {
                "x": int(x + width / 2),
                "y": int(y + height / 2),
                "width": int(width),
                "height": int(height),
            }

        except Exception as exc:
            logger.debug("[AXResolver] AppleScript fallback failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # list_elements helper (sync, called via to_thread)
    # ------------------------------------------------------------------

    def _collect_elements_sync(
        self,
        app_pid: int,
        max_depth: int,
    ) -> List[Dict[str, Any]]:
        """Walk AX tree and collect element metadata."""
        try:
            from ApplicationServices import (
                AXUIElementCreateApplication,
                AXUIElementCopyAttributeValue,
            )
            # AX attribute constants are plain strings — Quartz may not export them
            try:
                from Quartz import (
                    kAXWindowsAttribute,
                    kAXChildrenAttribute,
                    kAXTitleAttribute,
                    kAXRoleAttribute,
                    kAXDescriptionAttribute,
                    kAXPositionAttribute,
                    kAXSizeAttribute,
                    kAXValueAttribute,
                )
            except ImportError:
                kAXWindowsAttribute = "AXWindows"
                kAXChildrenAttribute = "AXChildren"
                kAXTitleAttribute = "AXTitle"
                kAXRoleAttribute = "AXRole"
                kAXDescriptionAttribute = "AXDescription"
                kAXPositionAttribute = "AXPosition"
                kAXSizeAttribute = "AXSize"
                kAXValueAttribute = "AXValue"
        except ImportError:
            return []

        app_element = AXUIElementCreateApplication(app_pid)
        if app_element is None:
            return []

        err, windows = AXUIElementCopyAttributeValue(
            app_element, kAXWindowsAttribute, None,
        )
        if windows is None:
            return []

        results: List[Dict[str, Any]] = []

        def _collect(node: Any, depth: int) -> None:
            if depth <= 0:
                return

            info: Dict[str, Any] = {}

            err, role = AXUIElementCopyAttributeValue(
                node, kAXRoleAttribute, None,
            )
            info["role"] = str(role) if role else None

            err, title = AXUIElementCopyAttributeValue(
                node, kAXTitleAttribute, None,
            )
            info["title"] = str(title) if title else None

            err, desc = AXUIElementCopyAttributeValue(
                node, kAXDescriptionAttribute, None,
            )
            info["description"] = str(desc) if desc else None

            err, val = AXUIElementCopyAttributeValue(
                node, kAXValueAttribute, None,
            )
            info["value"] = str(val) if val else None

            # Position & size
            rect = self._get_element_center(node)
            if rect:
                info["x"] = rect["x"]
                info["y"] = rect["y"]
                info["width"] = rect["width"]
                info["height"] = rect["height"]

            results.append(info)

            err, children = AXUIElementCopyAttributeValue(
                node, kAXChildrenAttribute, None,
            )
            if children:
                for child in children:
                    _collect(child, depth - 1)

        for window in windows:
            _collect(window, max_depth)

        return results

    # ------------------------------------------------------------------
    # Permission helpers
    # ------------------------------------------------------------------

    def _ensure_permission_checked(self) -> None:
        """Lazily check AX permission and log a warning once if not trusted."""
        if self._ax_trusted is not None:
            return  # already checked
        try:
            from ApplicationServices import AXIsProcessTrusted
            self._ax_trusted = bool(AXIsProcessTrusted())
            if not self._ax_trusted:
                logger.warning(
                    "[AXResolver] Accessibility permissions NOT granted. "
                    "Enable in System Preferences > Privacy & Security > "
                    "Accessibility. Some queries may still succeed."
                )
            else:
                logger.debug("[AXResolver] AX permissions verified (trusted)")
        except ImportError:
            logger.warning("[AXResolver] Cannot check AX trust -- ApplicationServices unavailable")
            self._ax_trusted = False


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: Optional[AccessibilityResolver] = None


def get_accessibility_resolver() -> AccessibilityResolver:
    """Return (or create) the module-level singleton."""
    global _instance
    if _instance is None:
        _instance = AccessibilityResolver()
    return _instance
