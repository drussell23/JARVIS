"""
Screen Lock Context Detector for JARVIS
======================================

Detects screen lock state and provides context-aware responses
"""

import asyncio
import logging
from typing import Dict, Any, Optional, Tuple
from datetime import datetime

from context_intelligence.core.system_state_monitor import get_system_monitor
from api.voice_unlock_integration import voice_unlock_connector, initialize_voice_unlock

logger = logging.getLogger(__name__)


class ScreenLockContextDetector:
    """
    Detects screen lock state and provides context for command execution
    """
    
    def __init__(self):
        self.system_monitor = get_system_monitor()
        self._last_check = None
        self._last_state = None
        
    async def is_screen_locked(self) -> bool:
        """Check if screen is currently locked"""
        return await self.system_monitor.get_state("screen_locked", force_refresh=True)
        
    async def check_screen_context(self, command: str) -> Dict[str, Any]:
        """
        Check screen lock context for a command
        
        Args:
            command: The command to execute
            
        Returns:
            Context dict with screen state and recommendations
        """
        is_locked = await self.is_screen_locked()
        
        context = {
            "screen_locked": is_locked,
            "requires_unlock": False,
            "unlock_message": None,
            "command_requires_screen": self._command_requires_screen(command),
            "timestamp": datetime.now().isoformat()
        }
        
        # If screen is locked and command requires screen access
        if is_locked and context["command_requires_screen"]:
            context["requires_unlock"] = True
            context["unlock_message"] = self._generate_unlock_message(command)
            
        return context
        
    def _command_requires_screen(self, command: str) -> bool:
        """
        Determine if a command requires screen access
        
        Args:
            command: The command to check
            
        Returns:
            True if command requires unlocked screen
        """
        command_lower = command.lower()
        
        # Commands that require screen access
        screen_required_patterns = [
            # Browser operations
            'open safari', 'open chrome', 'open firefox', 'open browser',
            'search for', 'google', 'look up', 'find online',
            'go to', 'navigate to', 'visit',
            
            # Application operations
            'open', 'launch', 'start', 'run',
            'switch to', 'show me', 'display',
            
            # File operations
            'create', 'edit', 'save', 'close',
            'find file', 'open file', 'open document',
            
            # System operations that need UI
            'take screenshot', 'show desktop', 'minimize',
            'maximize', 'resize', 'move window',
            
            # Exceptions - these work without unlocking
            # (none currently, but could add voice-only commands)
        ]
        
        # Check if any pattern matches
        for pattern in screen_required_patterns:
            if pattern in command_lower:
                # Check for exceptions (commands that don't need unlock)
                if any(exempt in command_lower for exempt in ['lock screen', 'lock my screen', 'sleep']):
                    return False
                return True
                
        return False
        
    def _generate_unlock_message(self, command: str) -> str:
        """
        Generate a context-aware message for unlocking
        
        Args:
            command: The original command
            
        Returns:
            Message to speak to user
        """
        # Extract the action from command for better messaging
        action = self._extract_action(command)
        
        messages = [
            f"Your screen is locked. I'll unlock it now by typing in the password to {action}.",
            f"I notice your screen is locked. Let me unlock it for you so I can {action}.",
            f"The screen is currently locked. I'll unlock it by entering your password, then {action}."
        ]
        
        # Use variety in responses
        import random
        return random.choice(messages)
        
    def _extract_action(self, command: str) -> str:
        """Extract the main action from a command for messaging"""
        command_lower = command.lower()
        
        # Common patterns
        if 'search for' in command_lower:
            search_term = command_lower.split('search for')[-1].strip()
            return f"search for {search_term}"
        elif 'open' in command_lower:
            app_or_site = command_lower.split('open')[-1].strip()
            return f"open {app_or_site}"
        elif 'go to' in command_lower:
            destination = command_lower.split('go to')[-1].strip()
            return f"go to {destination}"
        else:
            # Default: use the command as-is
            return "complete your request"
            
    async def handle_screen_lock_context(self, command: str) -> Tuple[bool, Optional[str]]:
        """
        Handle screen lock context and unlock if needed
        
        Args:
            command: The command to execute
            
        Returns:
            Tuple of (success, message)
        """
        context = await self.check_screen_context(command)
        
        if not context["requires_unlock"]:
            return True, None
            
        # Screen is locked and needs to be unlocked
        unlock_message = context["unlock_message"]
        
        try:
            # Ensure voice unlock is connected
            if not voice_unlock_connector or not voice_unlock_connector.connected:
                await initialize_voice_unlock()
                
            # Send unlock command
            logger.info(f"Unlocking screen for command: {command}")
            result = await voice_unlock_connector.send_command("unlock_screen", {
                "source": "context_intelligence",
                "reason": f"User command: {command}",
                "authenticated": True
            })
            
            if result and result.get('success'):
                # Wait a moment for unlock to complete
                await asyncio.sleep(2.0)  # Give time for password entry
                
                # Verify unlock succeeded
                is_still_locked = await self.is_screen_locked()
                if not is_still_locked:
                    success_msg = "I've unlocked your screen. Now proceeding with your request."
                    return True, f"{unlock_message} {success_msg}"
                else:
                    return False, "I tried to unlock the screen but it appears to still be locked. Please try unlocking manually."
            else:
                error_msg = result.get('message', 'Unknown error') if result else 'Not connected to unlock service'
                return False, f"I couldn't unlock the screen: {error_msg}"
                
        except Exception as e:
            logger.error(f"Error handling screen lock context: {e}")
            return False, f"I encountered an error while trying to unlock the screen: {str(e)}"


# Global instance
_detector = None

def get_screen_lock_detector() -> ScreenLockContextDetector:
    """Get or create screen lock detector instance"""
    global _detector
    if _detector is None:
        _detector = ScreenLockContextDetector()
    return _detector