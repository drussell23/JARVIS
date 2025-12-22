"""
Context-Aware Command Handler for JARVIS
=======================================

Handles commands with full context awareness, including screen lock state
"""

import asyncio
import logging
from typing import Dict, Any, Optional, Tuple, List
from datetime import datetime

from context_intelligence.detectors.screen_lock_detector import get_screen_lock_detector
from context_intelligence.core.system_state_monitor import get_system_monitor

logger = logging.getLogger(__name__)


class ContextAwareCommandHandler:
    """
    Handles commands with context awareness
    """
    
    def __init__(self):
        self.screen_lock_detector = get_screen_lock_detector()
        self.system_monitor = get_system_monitor()
        self.execution_steps = []
        
    async def handle_command_with_context(self, command: str, 
                                        execute_callback=None) -> Dict[str, Any]:
        """
        Handle a command with full context awareness
        
        Args:
            command: The command to execute
            execute_callback: Callback to execute the actual command
            
        Returns:
            Response dict with status and messages
        """
        self.execution_steps = []
        response = {
            "success": True,
            "command": command,
            "messages": [],
            "steps_taken": [],
            "context": {},
            "timestamp": datetime.now().isoformat()
        }
        
        try:
            # Step 1: Get system context
            system_context = await self._get_system_context()
            response["context"] = system_context
            
            # Step 2: Check screen lock context
            is_locked = system_context.get("screen_locked", False)
            
            if is_locked:
                self._add_step("Detected locked screen", {"screen_locked": True})
                
                # Check if command requires unlocked screen
                screen_context = await self.screen_lock_detector.check_screen_context(command)
                
                if screen_context["requires_unlock"]:
                    # Handle screen unlock
                    response["messages"].append(screen_context["unlock_message"])
                    self._add_step("Screen unlock required", screen_context)
                    
                    # Perform unlock
                    unlock_success, unlock_message = await self.screen_lock_detector.handle_screen_lock_context(command)
                    
                    if unlock_success:
                        self._add_step("Screen unlocked successfully", {"unlocked": True})
                        if unlock_message:
                            response["messages"].append(unlock_message)
                    else:
                        self._add_step("Screen unlock failed", {"error": unlock_message})
                        response["success"] = False
                        response["messages"].append(unlock_message or "Failed to unlock screen")
                        return self._finalize_response(response)
            
            # Step 3: Execute the actual command
            if execute_callback:
                self._add_step("Executing command", {"command": command})
                
                try:
                    # Execute with context
                    exec_result = await execute_callback(command, context=system_context)
                    
                    if isinstance(exec_result, dict):
                        if exec_result.get("success", True):
                            self._add_step("Command executed successfully", exec_result)
                            response["messages"].append(exec_result.get("message", "Command completed successfully"))
                            response["result"] = exec_result
                        else:
                            self._add_step("Command execution failed", exec_result)
                            response["success"] = False
                            response["messages"].append(exec_result.get("message", "Command failed"))
                    else:
                        # Simple success
                        self._add_step("Command completed", {"result": str(exec_result)})
                        response["messages"].append("Command completed successfully")
                        
                except Exception as e:
                    self._add_step("Command execution error", {"error": str(e)})
                    response["success"] = False
                    response["messages"].append(f"Error executing command: {str(e)}")
            
            # Step 4: Provide confirmation
            if response["success"]:
                confirmation = self._generate_confirmation(command, self.execution_steps)
                response["messages"].append(confirmation)
                
        except Exception as e:
            logger.error(f"Error in context-aware command handling: {e}")
            response["success"] = False
            response["messages"].append(f"An error occurred: {str(e)}")
            self._add_step("Error occurred", {"error": str(e)})
            
        return self._finalize_response(response)
        
    async def _get_system_context(self) -> Dict[str, Any]:
        """Get current system context"""
        # Get key system states
        states_to_check = [
            "screen_locked",
            "active_apps",
            "network_connected",
            "active_window"
        ]
        
        context = await self.system_monitor.get_states(states_to_check)
        
        # Add summary
        context["summary"] = {
            "screen_accessible": not context.get("screen_locked", True),
            "apps_running": len(context.get("active_apps", [])),
            "network_available": context.get("network_connected", False)
        }
        
        return context
        
    def _add_step(self, description: str, details: Dict[str, Any]):
        """Add an execution step for tracking"""
        self.execution_steps.append({
            "step": len(self.execution_steps) + 1,
            "description": description,
            "details": details,
            "timestamp": datetime.now().isoformat()
        })
        
    def _generate_confirmation(self, command: str, steps: List[Dict]) -> str:
        """Generate a confirmation message based on steps taken"""
        if not steps:
            return "Command completed."
            
        # Build narrative of what was done
        key_actions = []
        
        for step in steps:
            desc = step["description"].lower()
            if "unlocked successfully" in desc:
                key_actions.append("unlocked your screen")
            elif "executing command" in desc:
                # Extract the main action
                if "open" in command.lower():
                    app_or_site = command.lower().split("open")[-1].strip()
                    key_actions.append(f"opened {app_or_site}")
                elif "search" in command.lower():
                    search_term = command.lower().split("search for")[-1].strip() if "search for" in command.lower() else "your query"
                    key_actions.append(f"searched for {search_term}")
                else:
                    key_actions.append("executed your command")
                    
        # Build confirmation
        if len(key_actions) == 0:
            return "Task completed successfully."
        elif len(key_actions) == 1:
            return f"I've {key_actions[0]} for you."
        else:
            # Multiple actions
            confirmation = "I've "
            confirmation += ", ".join(key_actions[:-1])
            confirmation += f" and {key_actions[-1]} for you."
            return confirmation
            
    def _finalize_response(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """Finalize the response with steps taken"""
        response["steps_taken"] = self.execution_steps
        response["step_count"] = len(self.execution_steps)
        
        # Create a summary message if multiple messages
        if len(response["messages"]) > 1:
            response["summary"] = " ".join(response["messages"])
        elif len(response["messages"]) == 1:
            response["summary"] = response["messages"][0]
        else:
            response["summary"] = "Command processed"
            
        return response


# Global instance
_handler = None

def get_context_aware_handler() -> ContextAwareCommandHandler:
    """Get or create context-aware handler instance"""
    global _handler
    if _handler is None:
        _handler = ContextAwareCommandHandler()
    return _handler