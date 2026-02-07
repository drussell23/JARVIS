"""
Enhanced Context Wrapper - Drop-in Replacement
=============================================

This module provides a drop-in replacement for EnhancedSimpleContextHandler
that uses the new Context Intelligence System. It maintains backward compatibility
while leveraging advanced context awareness, feedback management, and command
processing capabilities.

The module integrates with the core context intelligence components to provide
enhanced command processing with voice feedback, screen lock detection, and
intelligent command queuing.

Example:
    >>> processor = SomeCommandProcessor()
    >>> handler = wrap_with_enhanced_context(processor)
    >>> result = await handler.process_with_context("open chrome")
    {'success': True, 'response': 'opened chrome', ...}
"""

import asyncio
import logging
from typing import Dict, Any, Optional
from datetime import datetime

# Import our new context intelligence components
from ..core.context_manager import get_context_manager, ContextManager
from ..core.feedback_manager import get_feedback_manager, FeedbackType, FeedbackManager
from ..core.command_queue import CommandPriority
from ..integrations.jarvis_integration import get_jarvis_integration

logger = logging.getLogger(__name__)


class EnhancedContextIntelligenceHandler:
    """
    Drop-in replacement for EnhancedSimpleContextHandler that uses
    the new Context Intelligence System.
    
    This handler provides enhanced command processing with context awareness,
    intelligent feedback, screen lock detection, and command queuing capabilities.
    It maintains the same interface as the original handler for backward compatibility.
    
    Attributes:
        command_processor: The underlying command processor to execute commands
        context_manager: Manager for context intelligence operations
        feedback_manager: Manager for voice and visual feedback
        jarvis_integration: Integration layer for JARVIS system
        execution_steps: List of execution steps for compatibility tracking
    """
    
    def __init__(self, command_processor) -> None:
        """Initialize the enhanced context intelligence handler.
        
        Args:
            command_processor: The command processor instance to wrap with
                context intelligence capabilities
        """
        self.command_processor = command_processor
        self.context_manager: ContextManager = get_context_manager()
        self.feedback_manager: FeedbackManager = get_feedback_manager()
        self.jarvis_integration = get_jarvis_integration()
        
        # Track execution steps for compatibility
        self.execution_steps = []
        
        # Initialize the system
        self._initialized = False
        
        # Register feedback handlers
        self._register_feedback_handlers()
        
    async def _ensure_initialized(self) -> None:
        """Ensure the context intelligence system is initialized.
        
        This method initializes the JARVIS integration if not already done.
        It's called automatically before processing commands.
        """
        if not self._initialized:
            await self.jarvis_integration.initialize()
            self._initialized = True
            
    def _register_feedback_handlers(self) -> None:
        """Register handlers for voice and visual feedback.
        
        Sets up voice feedback handlers that send responses back through
        WebSocket connections with appropriate emotions and timestamps.
        """
        from ..core.feedback_manager import FeedbackChannel
        
        # Voice handler - will be sent back via WebSocket
        async def voice_handler(feedback):
            """Handle voice feedback by sending through WebSocket.
            
            Args:
                feedback: The feedback object containing content and type
            """
            if hasattr(self, '_current_websocket') and self._current_websocket:
                await self._current_websocket.send_json({
                    "type": "voice_feedback",
                    "text": feedback.content,
                    "emotion": self._get_emotion(feedback.type),
                    "timestamp": datetime.now().isoformat()
                })
                
        self.feedback_manager.register_channel_handler(
            FeedbackChannel.VOICE, 
            voice_handler
        )
        
    def _get_emotion(self, feedback_type: FeedbackType) -> str:
        """Map feedback type to JARVIS emotion.
        
        Args:
            feedback_type: The type of feedback being processed
            
        Returns:
            String representing the appropriate emotion for JARVIS
            
        Example:
            >>> handler._get_emotion(FeedbackType.SUCCESS)
            'satisfied'
        """
        emotion_map = {
            FeedbackType.INFO: "informative",
            FeedbackType.PROGRESS: "focused",
            FeedbackType.SUCCESS: "satisfied",
            FeedbackType.WARNING: "concerned",
            FeedbackType.ERROR: "apologetic",
            FeedbackType.QUESTION: "curious"
        }
        return emotion_map.get(feedback_type, "neutral")
        
    def _add_step(self, step: str, details: Dict[str, Any] = None) -> None:
        """Add execution step for compatibility tracking.
        
        Args:
            step: Description of the execution step
            details: Optional dictionary containing additional step details
        """
        self.execution_steps.append({
            "step": step,
            "timestamp": datetime.now().isoformat(),
            "details": details or {}
        })
        logger.info(f"[CONTEXT STEP] {step}")
        
    async def process_with_context(self, command: str, 
                                 websocket=None) -> Dict[str, Any]:
        """
        Process command with full context intelligence.
        
        This method maintains the same interface as EnhancedSimpleContextHandler
        but uses the new Context Intelligence System. It handles screen lock
        detection, command queuing, voice feedback, and intelligent processing.
        
        Args:
            command: The voice command to process
            websocket: Optional WebSocket connection for real-time feedback
            
        Returns:
            Dictionary containing processing results with keys:
                - success: Boolean indicating if processing succeeded
                - response: Human-readable response message
                - result: Detailed processing results
                - execution_steps: List of processing steps taken
                - status: Optional status (e.g., 'queued')
                - command_id: Optional ID for queued commands
                
        Raises:
            Exception: If command processing fails and cannot be recovered
            
        Example:
            >>> result = await handler.process_with_context("open chrome")
            >>> print(result['response'])
            'opened chrome'
        """
        try:
            # Store websocket for feedback
            self._current_websocket = websocket
            
            # Ensure initialized
            await self._ensure_initialized()
            
            # Reset steps
            self.execution_steps = []
            
            logger.info(f"[CONTEXT INTELLIGENCE] ========= START PROCESSING =========")
            logger.info(f"[CONTEXT INTELLIGENCE] Command: '{command}'")
            
            # Step 1: Send initial acknowledgment
            if websocket:
                await websocket.send_json({
                    "type": "processing",
                    "message": "Processing your command...",
                    "speak": False,  # Opt-in contract: generic acks are not spoken
                    "timestamp": datetime.now().isoformat()
                })
                
            # Step 2: Process through context intelligence system
            try:
                result = await self.jarvis_integration.process_voice_command(
                    command=command,
                    voice_context={
                        "source": "voice",
                        "urgency": "normal"
                    },
                    websocket=websocket
                )
            except Exception as e:
                logger.error(f"Error in jarvis_integration.process_voice_command: {e}")
                # Fall back to direct processing
                if hasattr(self.command_processor, 'process_command'):
                    self._add_step("Falling back to direct command processing", {
                        "error": str(e)
                    })
                    result = await self.command_processor.process_command(command)
                    return {
                        "success": True,
                        "response": result.get("response", "I processed your command."),
                        "result": result,
                        "execution_steps": self.execution_steps
                    }
                else:
                    raise
            
            # Step 3: Handle the result based on status
            if result.get("status") == "queued":
                # Command was queued due to locked screen
                self._add_step("Command queued - screen locked", {
                    "command_id": result.get("command_id"),
                    "requires_unlock": True
                })
                
                # Send immediate feedback about screen being locked
                feedback_message = result.get("message", "Your screen is locked, unlocking now.")
                
                # Return immediately with the queued status message
                return {
                    "success": True,
                    "response": feedback_message,
                    "status": "queued",
                    "command_id": result.get("command_id"),
                    "execution_steps": self.execution_steps,
                    "requires_unlock": True
                }
                    
            else:
                # Command is being processed immediately  
                self._add_step("Processing command immediately", {
                    "requires_unlock": False
                })
                
                # Check if screen lock was needed but we're proceeding anyway
                if result.get("requires_unlock"):
                    # This means we detected lock, handled unlock, and are now executing
                    # Don't call the original processor - use our result
                    feedback_message = result.get("message", "I processed your command.")
                    return {
                        "success": True,
                        "response": feedback_message,
                        "result": result,
                        "execution_steps": self.execution_steps
                    }
                
            # Step 4: Execute through original processor ONLY if not queued
            if hasattr(self.command_processor, 'process_command') and result.get("status") != "queued":
                try:
                    # Call the original processor
                    processor_result = await self.command_processor.process_command(command)
                    
                    # Merge results
                    if isinstance(processor_result, dict):
                        result.update(processor_result)
                        
                except Exception as e:
                    logger.error(f"Error in command processor: {e}")
                    # Continue with context intelligence result
                    
            # Step 5: Send final response
            intent = result.get("intent", {})
            success_message = self._build_success_message(command, intent)
            
            await self.feedback_manager.send_contextual_feedback(
                "command_complete",
                success_message
            )
            
            logger.info(f"[CONTEXT INTELLIGENCE] ========= COMPLETED =========")
            
            return {
                "success": True,
                "response": result.get("response", result.get("message", success_message)),
                "result": result,
                "execution_steps": self.execution_steps,
                "message": result.get("message", success_message)
            }
            
        except Exception as e:
            logger.error(f"Error in context intelligence processing: {e}", exc_info=True)
            
            # Send error feedback
            await self.feedback_manager.send_feedback(
                f"I encountered an error processing your command: {str(e)}",
                FeedbackType.ERROR
            )
            
            return {
                "success": False,
                "error": str(e),
                "execution_steps": self.execution_steps
            }
            
        finally:
            # Clear websocket reference
            self._current_websocket = None
            
    def _extract_action(self, command: str) -> str:
        """Extract main action from command text.
        
        Parses the command to identify the primary action being requested,
        handling common patterns like "search for", "open", and "go to".
        
        Args:
            command: The raw command text to parse
            
        Returns:
            Extracted action string or the original command if no pattern matches
            
        Example:
            >>> handler._extract_action("search for python tutorials")
            'python tutorials'
        """
        command_lower = command.lower()
        
        if "search for" in command_lower:
            return command_lower.split("search for")[1].strip()
        elif "open" in command_lower:
            parts = command_lower.split("open")
            if len(parts) > 1:
                target = parts[1].strip()
                # Handle "open X and do Y" commands
                if " and " in target:
                    return f"open {target}"
                return f"open {target}"
        elif "go to" in command_lower:
            return f"navigate to {command_lower.split('go to')[1].strip()}"
        else:
            return command
            
    def _get_command_type(self, intent: Dict[str, Any]) -> str:
        """Determine command type from intent analysis.
        
        Args:
            intent: Dictionary containing intent analysis results with 'action' key
            
        Returns:
            String representing the command type category
            
        Example:
            >>> intent = {"action": "open", "target": "chrome"}
            >>> handler._get_command_type(intent)
            'open_app'
        """
        action = intent.get("action", "").lower()
        
        if action in ["open", "launch"]:
            return "open_app"
        elif action in ["search", "find", "google"]:
            return "search"
        elif action in ["navigate", "go"]:
            return "browse"
        else:
            return "system_command"
            
    def _build_success_message(self, command: str, intent: Dict[str, Any]) -> str:
        """Build success message from command and intent.
        
        Creates a human-readable success message based on the processed
        command and its analyzed intent.
        
        Args:
            command: The original command that was processed
            intent: Dictionary containing intent analysis with 'action' and 'target'
            
        Returns:
            Human-readable success message
            
        Example:
            >>> intent = {"action": "open", "target": "chrome"}
            >>> handler._build_success_message("open chrome", intent)
            'opened chrome'
        """
        action = intent.get("action", "completed")
        target = intent.get("target", "your request")
        
        if action == "open" and target:
            return f"opened {target}"
        elif action == "search" and target:
            return f"searched for {target}"
        else:
            return "completed your request"
            
    def _requires_screen(self, command: str) -> bool:
        """Check if command requires screen access.
        
        Analyzes the command to determine if it needs screen interaction,
        which affects screen lock handling and command queuing decisions.
        
        Args:
            command: The command text to analyze
            
        Returns:
            True if the command requires screen access, False otherwise
            
        Example:
            >>> handler._requires_screen("open chrome")
            True
            >>> handler._requires_screen("what time is it")
            False
        """
        command_lower = command.lower()
        
        # Same patterns as original for compatibility
        screen_patterns = [
            "open", "search", "browse", "launch", "start",
            "click", "type", "show", "display"
        ]
        
        return any(pattern in command_lower for pattern in screen_patterns)


def wrap_with_enhanced_context(processor):
    """
    Drop-in replacement for the original wrap_with_enhanced_context.
    
    This function maintains the same interface but returns our new
    context intelligence handler instead of the original simple handler.
    
    Args:
        processor: The command processor to wrap with context intelligence
        
    Returns:
        EnhancedContextIntelligenceHandler instance wrapping the processor
        
    Example:
        >>> processor = MyCommandProcessor()
        >>> enhanced = wrap_with_enhanced_context(processor)
        >>> result = await enhanced.process_with_context("open chrome")
    """
    return EnhancedContextIntelligenceHandler(processor)


# Alias for compatibility
EnhancedSimpleContextHandler = EnhancedContextIntelligenceHandler