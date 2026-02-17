#!/usr/bin/env python3
"""
Fast App Launcher for JARVIS
Optimized for quick app launching without complex routing
"""

import subprocess
import logging
from typing import Tuple, Dict, Optional
import asyncio

logger = logging.getLogger(__name__)

class FastAppLauncher:
    """Direct app launcher for common applications"""
    
    def __init__(self):
        # Common app mappings for quick access
        self.common_apps = {
            "safari": "Safari",
            "chrome": "Google Chrome",
            "whatsapp": "WhatsApp",
            "spotify": "Spotify",
            "slack": "Slack",
            "zoom": "zoom.us",
            "mail": "Mail",
            "messages": "Messages",
            "finder": "Finder",
            "notes": "Notes",
            "calendar": "Calendar",
            "music": "Music",
            "photos": "Photos"
        }

        # Background task tracking to prevent GC collection
        self._background_tasks: set = set()
        
    async def quick_open_app(self, app_name: str) -> Tuple[bool, str]:
        """Quickly open app using direct system call"""
        # Normalize app name
        app_lower = app_name.lower().strip()
        
        # Check common apps first
        if app_lower in self.common_apps:
            app_name = self.common_apps[app_lower]
        
        try:
            # Direct open command - fastest method
            process = await asyncio.create_subprocess_exec(
                "open", "-a", app_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            # Wait for completion with short timeout
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), 
                    timeout=1.0
                )
                
                if process.returncode == 0:
                    return True, f"Opened {app_name}"
                else:
                    # Fallback to AppleScript for immediate response
                    return await self._applescript_open(app_name)
                    
            except asyncio.TimeoutError:
                # App is launching, return success
                return True, f"Opening {app_name}"
                
        except Exception as e:
            logger.error(f"Fast launch error: {e}")
            # Try AppleScript as fallback
            return await self._applescript_open(app_name)
    
    async def _applescript_open(self, app_name: str) -> Tuple[bool, str]:
        """Fallback to AppleScript"""
        script = f'tell application "{app_name}" to activate'
        
        try:
            process = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            # Don't wait for completion - just fire and forget
            _task = asyncio.create_task(
                self._wait_for_process(process),
                name="fast-launcher-wait-process",
            )
            self._background_tasks.add(_task)
            _task.add_done_callback(self._background_tasks.discard)
            
            return True, f"Opening {app_name}"
            
        except Exception as e:
            return False, f"Failed to open {app_name}: {str(e)}"
    
    async def _wait_for_process(self, process):
        """Wait for process completion in background"""
        try:
            await asyncio.wait_for(process.communicate(), timeout=2.0)
        except Exception:
            pass
    
    def is_common_app(self, app_name: str) -> bool:
        """Check if app is in common apps list"""
        return app_name.lower().strip() in self.common_apps

# Singleton instance
_fast_launcher = None

def get_fast_app_launcher() -> FastAppLauncher:
    """Get or create fast app launcher instance"""
    global _fast_launcher
    if _fast_launcher is None:
        _fast_launcher = FastAppLauncher()
    return _fast_launcher