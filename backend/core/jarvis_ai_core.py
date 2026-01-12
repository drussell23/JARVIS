#!/usr/bin/env python3
"""
JARVIS AI Core - Uses Claude API Exclusively
All AI operations go through Anthropic's Claude for consistency and intelligence
"""

import os
import logging
import asyncio
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime
import json
import base64

from chatbots.claude_chatbot import ClaudeChatbot
from vision.claude_vision_analyzer import ClaudeVisionAnalyzer
from system_control.macos_controller import MacOSController
from system_control.claude_command_interpreter import ClaudeCommandInterpreter

logger = logging.getLogger(__name__)

class JARVISAICore:
    """
    Core AI brain for JARVIS - uses Claude API exclusively
    Handles all AI operations: vision, speech, task execution, learning
    """
    
    def __init__(self):
        """Initialize JARVIS AI Core with Claude API"""
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY required for JARVIS AI Core")
        
        # Initialize Claude components
        self.claude = ClaudeChatbot(
            api_key=api_key,
            model=os.getenv("CLAUDE_MODEL", "claude-3-haiku-20240307"),  # Use Haiku model
            max_tokens=int(os.getenv("CLAUDE_MAX_TOKENS", "4096")),
            temperature=float(os.getenv("CLAUDE_TEMPERATURE", "0.7"))
        )
        
        self.vision_analyzer = ClaudeVisionAnalyzer(api_key)
        
        # Initialize system control
        self.controller = MacOSController()
        self.command_interpreter = ClaudeCommandInterpreter(api_key)
        logger.info("System control integration initialized")
        
        # Context management
        self.workspace_context = {}
        self.user_patterns = {}
        self.learning_data = []
        
        # Mode management
        self.autonomous_mode = False
        self.continuous_monitoring = False
        
        logger.info("JARVIS AI Core initialized with Claude API")
    
    async def process_vision(self, screen_data: Any, mode: str = "focused") -> Dict[str, Any]:
        """
        Process vision data using Claude API
        
        Args:
            screen_data: Screenshot or screen capture data
            mode: "focused" for single window, "multi" for all windows
        """
        try:
            # Use Claude to analyze what's on screen
            if mode == "focused":
                analysis = await self.vision_analyzer.analyze_focused_window(screen_data)
            else:
                analysis = await self.vision_analyzer.analyze_workspace(screen_data)
            
            # Extract key information
            result = {
                "timestamp": datetime.now().isoformat(),
                "mode": mode,
                "content": analysis.get("description", ""),
                "applications": analysis.get("applications", []),
                "notifications": analysis.get("notifications", []),
                "actionable_items": analysis.get("actionable_items", []),
                "context": analysis.get("context", ""),
                "suggestions": analysis.get("suggestions", [])
            }
            
            # Update workspace context
            self.workspace_context.update(result)
            
            # Learn from this interaction
            await self._learn_from_vision(result)
            
            return result
            
        except Exception as e:
            logger.error(f"Error processing vision: {e}")
            return {"error": str(e)}
    
    async def process_speech_command(self, command: str, context: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Process speech commands using Claude API
        
        Args:
            command: The user's spoken command
            context: Optional context about current state
        """
        try:
            # Build comprehensive prompt for Claude
            prompt = f"""You are JARVIS, an AI assistant with the personality of Tony Stark's AI from Iron Man.
            
Current workspace context: {json.dumps(self.workspace_context, indent=2)}
User patterns: {json.dumps(self.user_patterns, indent=2)}
Autonomous mode: {self.autonomous_mode}

User command: "{command}"

Analyze this command and provide a JSON response with these exact fields:
{{
  "intent": "app_control" or "information_query" or "system_control" or "workspace_management",
  "action": "the specific action like open_app, close_app, etc.",
  "parameters": {{"target": "app name", "other": "params"}},
  "confidence": 0.0 to 1.0,
  "trigger_autonomous": true or false,
  "response": "What JARVIS should say to the user"
}}

For opening applications, use intent="app_control" and action="open_app"."""

            # Get Claude's analysis
            response = await self.claude.generate_response(prompt)
            
            # Parse Claude's response
            try:
                analysis = json.loads(response)
            except Exception:
                # Fallback if Claude doesn't return valid JSON
                analysis = {
                    "intent": "unknown",
                    "action": "clarify",
                    "parameters": {},
                    "confidence": 0.5,
                    "trigger_autonomous": False,
                    "response": response
                }
            
            # Learn from this command
            await self._learn_from_command(command, analysis)
            
            # If this is an app control command with high confidence, execute it
            if analysis.get("intent") == "app_control" and analysis.get("confidence", 0) > 0.7:
                try:
                    # Interpret the command for execution
                    intent = await self.command_interpreter.interpret_command(command)
                    if intent.confidence > 0.5:
                        # Execute the command
                        result = await self.command_interpreter.execute_intent(intent)
                        analysis["executed"] = True
                        analysis["execution_result"] = {
                            "success": result.success,
                            "message": result.message
                        }
                        # Update the response to include execution result
                        if result.success:
                            analysis["response"] = f"{analysis.get('response', '')} {result.message}"
                except Exception as e:
                    logger.error(f"Error executing app control command: {e}")
                    analysis["execution_error"] = str(e)
            
            return analysis
            
        except Exception as e:
            logger.error(f"Error processing speech command: {e}")
            return {
                "error": str(e),
                "response": "I apologize, but I encountered an error processing your command."
            }
    
    async def execute_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a task using Claude's intelligence
        
        Args:
            task: Task description with action, parameters, etc.
        """
        try:
            # Check if this is a direct command that can be executed
            if "command" in task or "action" in task:
                command = task.get("command") or f"{task.get('action', '')} {task.get('target', '')}".strip()
                
                # Use command interpreter for system actions
                if task.get("action") in ["open_app", "close_app", "switch_to_app"] or "open" in command.lower():
                    intent = await self.command_interpreter.interpret_command(command)
                    if intent.confidence > 0.5:
                        result = await self.command_interpreter.execute_intent(intent)
                        return {
                            "task": task,
                            "executed": True,
                            "success": result.success,
                            "message": result.message,
                            "executed_at": datetime.now().isoformat(),
                            "status": "completed" if result.success else "failed"
                        }
            
            # Build execution prompt
            prompt = f"""As JARVIS, execute the following task intelligently:

Task: {json.dumps(task, indent=2)}
Current context: {json.dumps(self.workspace_context, indent=2)}

Provide:
1. Step-by-step execution plan
2. Any potential issues or confirmations needed
3. Expected outcome
4. Success criteria

Respond in JSON format with 'execution_plan', 'issues', 'expected_outcome', and 'success_criteria'."""

            # Get Claude's execution plan
            response = await self.claude.generate_response(prompt)
            
            try:
                execution_plan = json.loads(response)
            except Exception:
                execution_plan = {
                    "execution_plan": ["Unable to parse execution plan"],
                    "issues": ["Response parsing error"],
                    "expected_outcome": "Unknown",
                    "success_criteria": []
                }
            
            # Execute the plan (this would connect to actual system controls)
            result = {
                "task": task,
                "plan": execution_plan,
                "executed_at": datetime.now().isoformat(),
                "status": "planned"  # Would be "executed" when connected to system
            }
            
            # Learn from execution
            await self._learn_from_execution(task, result)
            
            return result
            
        except Exception as e:
            logger.error(f"Error executing task: {e}")
            return {"error": str(e), "status": "failed"}
    
    async def enable_autonomous_mode(self) -> Dict[str, Any]:
        """Enable full autonomous mode with continuous monitoring"""
        self.autonomous_mode = True
        self.continuous_monitoring = True
        
        # Start continuous monitoring
        asyncio.create_task(self._continuous_monitoring_loop())
        
        return {
            "status": "enabled",
            "mode": "autonomous",
            "features": [
                "continuous_vision_monitoring",
                "proactive_notifications",
                "automatic_task_execution",
                "learning_enabled",
                "predictive_actions"
            ],
            "message": "Full autonomy activated. All systems online."
        }
    
    async def disable_autonomous_mode(self) -> Dict[str, Any]:
        """Disable autonomous mode, return to manual"""
        self.autonomous_mode = False
        self.continuous_monitoring = False
        
        return {
            "status": "disabled",
            "mode": "manual",
            "message": "Autonomous mode disabled. Manual control restored."
        }
    
    async def _continuous_monitoring_loop(self):
        """Continuous monitoring loop for autonomous mode"""
        logger.info("Starting continuous monitoring loop")
        
        while self.continuous_monitoring:
            try:
                # This would capture the screen in real implementation
                # For now, we'll simulate with a placeholder
                screen_data = {"placeholder": "screen_capture_data"}
                
                # Analyze current state
                vision_result = await self.process_vision(screen_data, mode="multi")
                
                # Check for actionable items
                if vision_result.get("actionable_items"):
                    for item in vision_result["actionable_items"]:
                        # Use Claude to decide if action should be taken
                        decision = await self._should_take_action(item)
                        if decision.get("should_act") and decision.get("confidence", 0) > 0.8:
                            await self.execute_task(decision["task"])
                
                # Check for notifications
                if vision_result.get("notifications"):
                    await self._process_notifications(vision_result["notifications"])
                
                # Wait before next check
                await asyncio.sleep(2)  # Check every 2 seconds
                
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                await asyncio.sleep(5)  # Wait longer on error
    
    async def _should_take_action(self, item: Dict) -> Dict[str, Any]:
        """Use Claude to decide if an action should be taken"""
        prompt = f"""As JARVIS in autonomous mode, should I take action on this item?

Item: {json.dumps(item, indent=2)}
User patterns: {json.dumps(self.user_patterns, indent=2)}
Current context: {json.dumps(self.workspace_context, indent=2)}

Consider:
1. Is this urgent or important?
2. Does it match user's typical behavior?
3. Would the user want me to handle this automatically?
4. What's the confidence level for taking action?

Respond in JSON with 'should_act' (boolean), 'confidence' (0-1), 'reasoning', and 'task' if action needed."""

        response = await self.claude.generate_response(prompt)
        
        try:
            return json.loads(response)
        except Exception:
            return {"should_act": False, "confidence": 0, "reasoning": "Could not parse decision"}
    
    async def _process_notifications(self, notifications: List[Dict]):
        """Process detected notifications intelligently"""
        for notification in notifications:
            # Use Claude to understand the notification
            prompt = f"""As JARVIS, analyze this notification:

Notification: {json.dumps(notification, indent=2)}

Should I:
1. Alert the user immediately?
2. Queue it for later?
3. Handle it automatically?
4. Ignore it?

Provide reasoning and suggested action."""

            response = await self.claude.generate_response(prompt)
            logger.info(f"Notification analysis: {response}")
    
    async def _learn_from_vision(self, vision_data: Dict):
        """Learn patterns from vision analysis"""
        self.learning_data.append({
            "type": "vision",
            "timestamp": datetime.now().isoformat(),
            "data": vision_data
        })
        
        # Update patterns periodically
        if len(self.learning_data) % 10 == 0:
            await self._update_user_patterns()
    
    async def _learn_from_command(self, command: str, analysis: Dict):
        """Learn from user commands"""
        self.learning_data.append({
            "type": "command",
            "timestamp": datetime.now().isoformat(),
            "command": command,
            "analysis": analysis
        })
    
    async def _learn_from_execution(self, task: Dict, result: Dict):
        """Learn from task executions"""
        self.learning_data.append({
            "type": "execution",
            "timestamp": datetime.now().isoformat(),
            "task": task,
            "result": result
        })
    
    async def _update_user_patterns(self):
        """Use Claude to analyze patterns in user behavior"""
        if len(self.learning_data) < 10:
            return
        
        # Get recent learning data
        recent_data = self.learning_data[-50:]  # Last 50 interactions
        
        prompt = f"""Analyze these user interactions and identify patterns:

{json.dumps(recent_data, indent=2)}

Identify:
1. Common workflows
2. Typical command patterns
3. Preferred applications
4. Working hours
5. Notification preferences
6. Task automation opportunities

Respond in JSON format."""

        response = await self.claude.generate_response(prompt)
        
        try:
            patterns = json.loads(response)
            self.user_patterns.update(patterns)
            logger.info("Updated user patterns from learning data")
        except Exception as e:
            logger.error(f"Error updating patterns: {e}")
    
    def get_status(self) -> Dict[str, Any]:
        """Get current AI core status"""
        return {
            "mode": "autonomous" if self.autonomous_mode else "manual",
            "monitoring": self.continuous_monitoring,
            "workspace_context": self.workspace_context,
            "patterns_learned": len(self.user_patterns),
            "learning_data_points": len(self.learning_data),
            "ai_model": "Claude Opus 4",
            "capabilities": [
                "vision_analysis",
                "speech_processing",
                "task_execution",
                "pattern_learning",
                "autonomous_decisions"
            ]
        }

# Singleton instance
_jarvis_ai_core = None

def get_jarvis_ai_core() -> JARVISAICore:
    """Get singleton JARVIS AI Core instance"""
    global _jarvis_ai_core
    if _jarvis_ai_core is None:
        _jarvis_ai_core = JARVISAICore()
    return _jarvis_ai_core