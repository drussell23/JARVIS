"""
JARVIS Workflow Command Processor - Integration Layer
Processes multi-command workflows through JARVIS voice system
"""

import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import anthropic

from .jarvis_voice_api import JARVISCommand
from .workflow_engine import WorkflowExecutionEngine
from .workflow_parser import WorkflowParser

logger = logging.getLogger(__name__)


class WorkflowCommandProcessor:
    """Processes workflow commands and integrates with JARVIS voice system"""

    # Patterns that indicate multi-command workflows
    WORKFLOW_INDICATORS = [
        r"\band\b",
        r"\bthen\b",
        r"\bafter that\b",
        r"\bfollowed by\b",
        r"\bnext\b",
        r"\balso\b",
        r"\bplus\b",
        r"[,;]",  # Comma or semicolon separated commands
        r"\bstep \d+",  # Numbered steps
    ]

    def __init__(self, use_intelligent_selection: bool = True):
        """Initialize workflow processor

        Args:
            use_intelligent_selection: Enable intelligent model selection (recommended)
        """
        self.parser = WorkflowParser()
        self.engine = WorkflowExecutionEngine()
        self.use_intelligent_selection = use_intelligent_selection
        self._compiled_patterns = [re.compile(p, re.IGNORECASE) for p in self.WORKFLOW_INDICATORS]

        # v2.7 FIX: Use Prime Router instead of direct Claude API
        # This ensures all LLM calls go through the proper routing system
        # with fallback chain: LOCAL_PRIME → CLOUD_RUN → CLOUD_CLAUDE
        self._prime_router = None  # Lazy-loaded
        self._use_prime_router = os.getenv("WORKFLOW_USE_PRIME_ROUTER", "true").lower() == "true"
        logger.info("✅ Workflow processor will route through Prime Router")

    def is_workflow_command(self, command_text: str) -> bool:
        """Check if command contains multiple actions"""
        # Quick checks
        if len(command_text.split()) < 5:  # Too short for multi-command
            return False

        # Check for workflow indicators
        for pattern in self._compiled_patterns:
            if pattern.search(command_text):
                return True

        # Check for multiple action verbs
        action_verbs = [
            "open",
            "close",
            "search",
            "check",
            "create",
            "send",
            "unlock",
            "launch",
            "start",
            "prepare",
            "mute",
            "set",
        ]
        verb_count = sum(1 for verb in action_verbs if verb in command_text.lower())

        return verb_count >= 2

    async def process_workflow_command(
        self, command: JARVISCommand, user_id: str = "default", websocket: Optional[Any] = None
    ) -> Dict[str, Any]:
        """Process a multi-command workflow"""
        try:
            logger.info(f"Processing workflow command: '{command.text}'")

            # Parse the command into workflow
            workflow = self.parser.parse_command(command.text)

            if not workflow.actions:
                return {
                    "success": False,
                    "response": "I couldn't understand the workflow steps. Please try rephrasing.",
                    "command_type": "workflow_parse_error",
                }

            # Log workflow details
            logger.info(f"Parsed workflow with {len(workflow.actions)} actions:")
            for i, action in enumerate(workflow.actions):
                logger.info(f"  {i+1}. {action.action_type.value}: {action.target}")

            # Send initial response
            if websocket:
                await websocket.send_json(
                    {
                        "type": "workflow_analysis",
                        "message": f"I'll help you with that. I've identified {len(workflow.actions)} tasks to complete.",
                        "workflow": {
                            "total_actions": len(workflow.actions),
                            "complexity": workflow.complexity,
                            "estimated_duration": workflow.estimated_duration,
                            "actions": [
                                {
                                    "type": action.action_type.value,
                                    "description": action.description
                                    or f"{action.action_type.value} {action.target}",
                                }
                                for action in workflow.actions
                            ],
                        },
                        "timestamp": datetime.now().isoformat(),
                    }
                )

            # Execute the workflow
            result = await self.engine.execute_workflow(workflow, user_id, websocket)

            # Generate dynamic response using Claude API
            response = await self._generate_response_with_claude(workflow, result)

            return {
                "success": result.success_rate > 0.5,
                "response": response,
                "command_type": "workflow",
                "workflow_result": {
                    "workflow_id": result.workflow_id,
                    "status": result.status.value,
                    "success_rate": result.success_rate,
                    "total_duration": result.total_duration,
                    "actions_completed": sum(
                        1 for r in result.action_results if r.status.value == "completed"
                    ),
                    "actions_failed": sum(
                        1 for r in result.action_results if r.status.value == "failed"
                    ),
                },
            }

        except Exception as e:
            logger.error(f"Workflow processing error: {e}", exc_info=True)
            return {
                "success": False,
                "response": "I encountered an error while processing your workflow. Let me try a different approach.",
                "command_type": "workflow_error",
                "error": str(e),
            }

    async def _generate_response_with_claude(self, workflow, result) -> str:
        """Generate dynamic, contextual JARVIS response using intelligent model selection"""

        # Try intelligent model selection first
        if self.use_intelligent_selection:
            try:
                return await self._generate_response_with_intelligent_selection(workflow, result)
            except Exception as e:
                logger.warning(
                    f"Intelligent selection failed, falling back to direct Claude API: {e}"
                )
                # Continue to direct Claude API below

        if not self.claude_client:
            # Fallback to basic response if Claude not available
            return self._generate_basic_response(workflow, result)

        # Build context for Claude
        completed = sum(1 for r in result.action_results if r.status.value == "completed")
        failed = sum(1 for r in result.action_results if r.status.value == "failed")
        total = len(result.action_results)

        # Collect action details
        action_details = []
        for i, action in enumerate(workflow.actions):
            status = (
                result.action_results[i].status.value
                if i < len(result.action_results)
                else "unknown"
            )
            detail = {
                "action": action.action_type.value,
                "target": action.target,
                "status": status,
                "description": action.description,
            }
            if status == "failed" and i < len(result.action_results):
                detail["error"] = result.action_results[i].error
            action_details.append(detail)

        # Create prompt for Claude
        prompt = f"""You are JARVIS, Tony Stark's sophisticated AI assistant. Generate a response for the user's command.

USER'S ORIGINAL COMMAND: "{workflow.original_command}"

EXECUTION RESULTS:
- Total Actions: {total}
- Completed Successfully: {completed}
- Failed: {failed}
- Execution Time: {result.total_duration:.1f}s

ACTION DETAILS:
{chr(10).join(f"  {i+1}. {a['action']} '{a['target']}': {a['status']}" + (f" ({a.get('error', '')})" if a.get('error') else "") for i, a in enumerate(action_details))}

GUIDELINES:
1. Be sophisticated and witty like JARVIS from Iron Man
2. Keep it concise (1-2 sentences max)
3. Be specific about what was accomplished (use actual targets like "Safari" or "dogs")
4. Use elegant British phrasing ("I've opened", "launched", "executed")
5. If something failed, acknowledge it gracefully
6. Add subtle wit or charm when appropriate
7. NO generic phrases like "Mission accomplished" or "All done"
8. Make it sound natural and conversational
9. Reference the actual items involved (e.g., "Safari is now displaying search results for dogs")

Generate ONLY the response text, nothing else."""

        try:
            # v2.7 FIX: Route through Prime Router instead of direct Claude API
            if self._prime_router is None:
                try:
                    from core.prime_router import get_prime_router
                except ImportError:
                    from backend.core.prime_router import get_prime_router
                self._prime_router = await get_prime_router()

            # Generate via Prime Router (handles LOCAL_PRIME → CLOUD_RUN → CLAUDE fallback)
            response_obj = await self._prime_router.generate(
                prompt=prompt,
                max_tokens=150,
            )

            # Extract text from response
            if hasattr(response_obj, 'content') and response_obj.content:
                if isinstance(response_obj.content, list):
                    response = response_obj.content[0].text.strip() if hasattr(response_obj.content[0], 'text') else str(response_obj.content[0]).strip()
                else:
                    response = str(response_obj.content).strip()
            elif isinstance(response_obj, dict):
                response = response_obj.get('content', response_obj.get('response', '')).strip()
            else:
                response = str(response_obj).strip()

            logger.info(f"✨ Generated dynamic JARVIS response via Prime Router: {response}")
            return response

        except Exception as e:
            logger.error(f"Prime Router error, falling back to basic response: {e}")
            return self._generate_basic_response(workflow, result)

    async def _generate_response_with_intelligent_selection(self, workflow, result) -> str:
        """
        Generate workflow response using intelligent model selection

        This method:
        1. Imports the hybrid orchestrator
        2. Builds comprehensive context from workflow execution results
        3. Uses intelligent selection to generate JARVIS-style response
        4. Returns the dynamic response
        """
        try:
            from backend.core.hybrid_orchestrator import HybridOrchestrator

            # Get or create orchestrator
            orchestrator = HybridOrchestrator()
            if not orchestrator.is_running:
                await orchestrator.start()

            # Build context for workflow response
            completed = sum(1 for r in result.action_results if r.status.value == "completed")
            failed = sum(1 for r in result.action_results if r.status.value == "failed")
            total = len(result.action_results)

            # Collect action details
            action_details = []
            for i, action in enumerate(workflow.actions):
                status = (
                    result.action_results[i].status.value
                    if i < len(result.action_results)
                    else "unknown"
                )
                detail = {
                    "action": action.action_type.value,
                    "target": action.target,
                    "status": status,
                    "description": action.description,
                }
                if status == "failed" and i < len(result.action_results):
                    detail["error"] = result.action_results[i].error
                action_details.append(detail)

            # Create prompt for intelligent selection
            prompt = f"""You are JARVIS, Tony Stark's sophisticated AI assistant. Generate a response for the user's command.

USER'S ORIGINAL COMMAND: "{workflow.original_command}"

EXECUTION RESULTS:
- Total Actions: {total}
- Completed Successfully: {completed}
- Failed: {failed}
- Execution Time: {result.total_duration:.1f}s

ACTION DETAILS:
{chr(10).join(f"  {i+1}. {a['action']} '{a['target']}': {a['status']}" + (f" ({a.get('error', '')})" if a.get('error') else "") for i, a in enumerate(action_details))}

GUIDELINES:
1. Be sophisticated and witty like JARVIS from Iron Man
2. Keep it concise (1-2 sentences max)
3. Be specific about what was accomplished (use actual targets like "Safari" or "dogs")
4. Use elegant British phrasing ("I've opened", "launched", "executed")
5. If something failed, acknowledge it gracefully
6. Add subtle wit or charm when appropriate
7. NO generic phrases like "Mission accomplished" or "All done"
8. Make it sound natural and conversational
9. Reference the actual items involved (e.g., "Safari is now displaying search results for dogs")

Generate ONLY the response text, nothing else."""

            # Build context
            context = {
                "task_type": "workflow_response_generation",
                "workflow_id": result.workflow_id,
                "total_actions": total,
                "completed_actions": completed,
                "failed_actions": failed,
                "execution_time": result.total_duration,
            }

            # Execute with intelligent model selection
            logger.info("[WORKFLOW-PROCESSOR] Using intelligent selection for response generation")
            api_result = await orchestrator.execute_with_intelligent_model_selection(
                query=prompt,
                intent="workflow_response_generation",
                required_capabilities={"conversational_ai", "response_generation"},
                context=context,
                max_tokens=150,
                temperature=0.7,
            )

            if not api_result.get("success"):
                raise Exception(api_result.get("error", "Intelligent selection failed"))

            # Extract response
            response = api_result.get("text", "").strip()
            model_used = api_result.get("model_used", "intelligent_selection")

            logger.info(f"✨ Generated dynamic JARVIS response using {model_used}: {response}")
            return response

        except ImportError:
            logger.warning("Hybrid orchestrator not available for workflow response generation")
            raise
        except Exception as e:
            logger.error(f"Error in intelligent workflow response generation: {e}")
            raise

    def _generate_basic_response(self, workflow, result) -> str:
        """Fallback basic response if Claude API fails"""
        completed = sum(1 for r in result.action_results if r.status.value == "completed")
        failed = sum(1 for r in result.action_results if r.status.value == "failed")
        total = len(result.action_results)

        if completed == total:
            actions = [a.target for a in workflow.actions[:3]]
            if len(actions) == 2:
                return f"I've successfully {workflow.actions[0].action_type.value} {actions[0]} and {workflow.actions[1].action_type.value} {actions[1]}."
            elif len(actions) == 1:
                return f"I've {workflow.actions[0].action_type.value} {actions[0]}."
            else:
                return f"All {total} tasks completed successfully."
        elif completed > 0:
            return f"Completed {completed} of {total} tasks. {failed} encountered issues."
        else:
            return f"I couldn't complete the workflow. {result.action_results[0].error if result.action_results else ''}"

    def _generate_response(self, workflow, result) -> str:
        """DEPRECATED: Use _generate_response_with_claude instead"""
        import asyncio

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # If loop is running, create a task
                future = asyncio.ensure_future(
                    self._generate_response_with_claude(workflow, result)
                )
                return self._generate_basic_response(workflow, result)  # Return basic for now
            else:
                # If no loop, run sync
                return loop.run_until_complete(
                    self._generate_response_with_claude(workflow, result)
                )
        except Exception:
            return self._generate_basic_response(workflow, result)

    async def get_workflow_examples(self) -> List[Dict[str, Any]]:
        """Get example workflow commands for user guidance"""
        return [
            {
                "category": "Productivity",
                "examples": [
                    {
                        "command": "Hey JARVIS, open Safari and search for Python tutorials",
                        "description": "Opens browser and performs search",
                    },
                    {
                        "command": "Hey JARVIS, check my email and calendar for today",
                        "description": "Reviews email and calendar",
                    },
                    {
                        "command": "Hey JARVIS, prepare for my meeting by opening Zoom and muting notifications",
                        "description": "Meeting preparation workflow",
                    },
                ],
            },
            {
                "category": "Document Creation",
                "examples": [
                    {
                        "command": "Hey JARVIS, open Word and create a new document",
                        "description": "Starts document creation",
                    },
                    {
                        "command": "Hey JARVIS, create a new presentation and add a title slide",
                        "description": "PowerPoint workflow",
                    },
                ],
            },
            {
                "category": "Research",
                "examples": [
                    {
                        "command": "Hey JARVIS, search for machine learning on the web and open the top results",
                        "description": "Research workflow",
                    },
                    {
                        "command": "Hey JARVIS, find documents about project alpha and open them",
                        "description": "File search workflow",
                    },
                ],
            },
        ]

    def extract_workflow_intents(self, workflow) -> List[str]:
        """Extract high-level intents from workflow for analytics"""
        intents = []

        # Check for common workflow patterns
        action_types = [a.action_type.value for a in workflow.actions]

        if "open_app" in action_types and "mute" in action_types:
            intents.append("focus_mode")

        if "check" in action_types and any(
            "email" in a.target.lower() or "calendar" in a.target.lower() for a in workflow.actions
        ):
            intents.append("daily_review")

        if "open_app" in action_types and "create" in action_types:
            intents.append("content_creation")

        if "search" in action_types:
            intents.append("research")

        if "unlock" in action_types:
            intents.append("system_access")

        return intents or ["general_workflow"]


# Global instance for easy access
workflow_processor = WorkflowCommandProcessor()


async def handle_workflow_command(
    command: JARVISCommand, user_id: str = "default", websocket: Optional[Any] = None
) -> Optional[Dict[str, Any]]:
    """Helper function to check and process workflow commands"""
    if workflow_processor.is_workflow_command(command.text):
        return await workflow_processor.process_workflow_command(command, user_id, websocket)
    return None
