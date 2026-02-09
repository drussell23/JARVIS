#!/usr/bin/env python3
"""
Simple Context Handler for JARVIS
=================================

A simplified context handler that focuses on the screen lock/unlock scenario
"""

import asyncio
import logging
import re
from typing import Dict, Any, Optional

from api.direct_unlock_handler import unlock_screen_direct, check_screen_locked_direct

logger = logging.getLogger(__name__)


class SimpleContextHandler:
    """Simple handler for context-aware command processing"""
    
    def __init__(self, command_processor):
        self.command_processor = command_processor
        self.screen_required_patterns = [
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
        ]
        
    async def process_with_context(self, command: str, websocket=None, **kwargs) -> Dict[str, Any]:
        """Process command with basic context awareness"""
        try:
            logger.info(f"[CONTEXT] ========= START CONTEXT PROCESSING =========")
            logger.info(f"[CONTEXT] Processing command: '{command}'")
            logger.info(f"[CONTEXT] WebSocket provided: {websocket is not None}")
            
            # Check if command requires screen
            requires_screen = self._requires_screen(command)
            logger.info(f"[CONTEXT] Command requires screen: {requires_screen}")
            
            if requires_screen:
                # Check if screen is locked
                is_locked = await self._check_screen_locked()
                logger.info(f"[CONTEXT] Screen is locked: {is_locked}")
                
                if is_locked:
                    logger.info(f"Screen is locked, need to unlock for command: {command}")
                    
                    # Prepare unlock message
                    unlock_message = "Your screen is locked, unlocking now."
                    
                    # First, send the unlock message
                    if websocket:
                        await websocket.send_json({
                            "type": "context_message",
                            "message": unlock_message
                        })
                    
                    # Unlock the screen
                    logger.info("[CONTEXT] Attempting to unlock screen...")
                    unlock_success = await self._unlock_screen(command)
                    logger.info(f"[CONTEXT] Unlock result: {unlock_success}")
                    
                    if unlock_success:
                        # Wait a moment for unlock to complete
                        await asyncio.sleep(2.0)
                        
                        # Now execute the original command
                        result = await self.command_processor.process_command(command, websocket)
                        
                        # Add context info to result
                        if isinstance(result, dict):
                            original_response = result.get("response", "")
                            # Format like PRD example: "I unlocked your screen, opened Safari, and searched for dogs."
                            result["response"] = f"I unlocked your screen and {original_response.lower()}"
                            result["context_handled"] = True
                            result["screen_unlocked"] = True
                        
                        return result
                    else:
                        return {
                            "success": False,
                            "response": "I couldn't unlock the screen. Please unlock it manually and try again.",
                            "context_handled": True
                        }
            
            # No special context handling needed
            return await self.command_processor.process_command(command, websocket)
            
        except Exception as e:
            logger.error(f"Error in context handling: {e}")
            # Fallback to standard processing
            return await self.command_processor.process_command(command, websocket)
    
    def _requires_screen(self, command: str) -> bool:
        """Check if command requires screen access"""
        command_lower = command.lower()
        
        # Commands that don't need screen
        if any(pattern in command_lower for pattern in ['lock screen', 'lock my screen', 'what time', 'weather']):
            return False
            
        # Check if any screen-required pattern matches
        for pattern in self.screen_required_patterns:
            if pattern in command_lower:
                return True
                
        return False
    
    async def _check_screen_locked(self) -> bool:
        """Check if screen is currently locked"""
        return await check_screen_locked_direct()
    
    async def _unlock_screen(self, command: str) -> bool:
        """Unlock the screen"""
        return await unlock_screen_direct(f"User command: {command}")


def wrap_with_simple_context(processor):
    """Wrap a command processor with simple context handling"""
    handler = SimpleContextHandler(processor)
    return handler