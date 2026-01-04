"""
JARVIS Ghost Monitor Protocol - Yabai Window Manager (v1.0)
===========================================================

Part of the Patent-Pending "Ghost Monitor Architecture".

This module implements the "Exile" and "Boomerang" protocols for:
1.  Moving windows to the Shadow Realm (Display 2) for isolated monitoring.
2.  Summoning windows back to the user's focus when needed (Boomerang).
3.  Maintaining "Single-Seat Concurrency" (user is never interrupted).

Core Components:
- Shadow Realm (v53): Virtual display isolation.
- Exile Protocol: Move to Display 2.
- Boomerang Protocol (v63): Auto-summon via OS signals.
- The Reaper: State convergence (implemented in YabaiSpaceDetector, to be integrated).

Directives:
- Treat Display 2 as a privileged, secure execution environment.
- Strict adherence to Single-Seat Concurrency.
"""

import asyncio
import logging
import os
import json
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass

# Setup logging
logger = logging.getLogger(__name__)

@dataclass
class WindowState:
    window_id: int
    app_name: str
    title: str
    original_space: int
    original_frame: Dict[str, float]
    exiled_at: float

class YabaiWindowManager:
    def __init__(self):
        self.yabai_path = os.getenv("YABAI_PATH", "/opt/homebrew/bin/yabai")
        self.shadow_display_index = 2  # The Ghost Display
        self.exiled_windows: Dict[int, WindowState] = {}
        
        # State Convergence (The Reaper)
        self._convergence_lock = asyncio.Lock()

    async def _run_yabai(self, args: List[str]) -> Tuple[bool, str]:
        """Execute yabai command safely."""
        try:
            cmd = [self.yabai_path] + args
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            
            if proc.returncode != 0:
                return False, stderr.decode().strip()
            return True, stdout.decode().strip()
        except Exception as e:
            return False, str(e)

    async def ensure_shadow_realm(self) -> bool:
        """Verify Display 2 (Shadow Realm) is active."""
        success, output = await self._run_yabai(["-m", "query", "--displays"])
        if not success:
            logger.error(f"[GhostMonitor] Failed to query displays: {output}")
            return False
            
        try:
            displays = json.loads(output)
            # Check if we have at least 2 displays
            if len(displays) < 2:
                logger.warning("[GhostMonitor] Shadow Realm (Display 2) not found!")
                return False
                
            # Verify Display 2 is valid
            shadow = next((d for d in displays if d["index"] == self.shadow_display_index), None)
            if shadow:
                return True
            return False
        except Exception as e:
            logger.error(f"[GhostMonitor] Error parsing display info: {e}")
            return False

    async def exile_window(self, window_id: int) -> bool:
        """
        Exile Protocol: Move window to Display 2 (Shadow Realm).
        """
        logger.info(f"[GhostMonitor] 🌑 Exiling window {window_id} to Shadow Realm")
        
        async with self._convergence_lock:
            # 1. Get current state for "Boomerang" later
            success, win_info = await self._get_window_info(window_id)
            if not success or not win_info:
                logger.error(f"[GhostMonitor] Failed to get info for window {window_id}")
                return False

            # 2. Store state
            self.exiled_windows[window_id] = WindowState(
                window_id=window_id,
                app_name=win_info.get("app", "Unknown"),
                title=win_info.get("title", ""),
                original_space=win_info.get("space", 1),
                original_frame=win_info.get("frame", {}),
                exiled_at=asyncio.get_event_loop().time()
            )

            # 3. Move to Display 2
            # Use --display 2 to move across spaces/displays
            success, msg = await self._run_yabai(["-m", "window", str(window_id), "--display", str(self.shadow_display_index)])
            if not success:
                logger.error(f"[GhostMonitor] Failed to move window {window_id}: {msg}")
                return False

            # 4. Maximize on Shadow Realm (Retina Mosaic preparation)
            # Use grid 1:1:0:0:1:1 to fill screen
            await self._run_yabai(["-m", "window", str(window_id), "--grid", "1:1:0:0:1:1"])
            
            return True

    async def boomerang_window(self, window_id: int) -> bool:
        """
        Boomerang Protocol (v63): Auto-summon window back to user.
        Uses OS signals/focus to bring it back.
        """
        logger.info(f"[GhostMonitor] 🪃 Boomerang: Summoning window {window_id}")
        
        async with self._convergence_lock:
            if window_id not in self.exiled_windows:
                logger.warning(f"[GhostMonitor] Window {window_id} not known in Exile logs")
                # Proceed anyway if it's on Display 2?
            
            # 1. Move back to Display 1 (or original display if we knew it)
            # We assume Display 1 is the user's primary
            success, msg = await self._run_yabai(["-m", "window", str(window_id), "--display", "1"])
            if not success:
                logger.error(f"[GhostMonitor] Boomerang failed for {window_id}: {msg}")
                return False

            # 2. Focus the window
            await self._run_yabai(["-m", "window", str(window_id), "--focus"])
            
            # 3. Clean up state
            if window_id in self.exiled_windows:
                del self.exiled_windows[window_id]
                
            return True

    async def _get_window_info(self, window_id: int) -> Tuple[bool, Optional[Dict]]:
        success, output = await self._run_yabai(["-m", "query", "--windows", "--id", str(window_id)])
        if not success:
            return False, None
        try:
            return True, json.loads(output)
        except:
            return False, None

