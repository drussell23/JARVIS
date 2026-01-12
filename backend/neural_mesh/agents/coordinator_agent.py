"""
JARVIS Neural Mesh - Coordinator Agent

The central coordinator agent that manages agent orchestration,
task delegation, and cross-agent communication.

Capabilities:
- delegate_task: Route tasks to appropriate agents
- query_agents: Find agents by capability
- broadcast_message: Send to all agents
- get_agent_status: Check agent health
- coordinate_workflow: Orchestrate multi-agent workflows
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from ..base.base_neural_mesh_agent import BaseNeuralMeshAgent
from ..data_models import (
    AgentMessage,
    AgentStatus,
    KnowledgeType,
    MessagePriority,
    MessageType,
)

logger = logging.getLogger(__name__)


class CoordinatorAgent(BaseNeuralMeshAgent):
    """
    Coordinator Agent - Central orchestration for the Neural Mesh.

    Responsibilities:
    - Task delegation based on agent capabilities
    - Cross-agent communication routing
    - Workflow orchestration
    - Load balancing across agents
    - Agent health monitoring aggregation
    """

    def __init__(self) -> None:
        """Initialize the Coordinator Agent."""
        super().__init__(
            agent_name="coordinator_agent",
            agent_type="core",
            capabilities={
                "delegate_task",
                "query_agents",
                "broadcast_message",
                "get_agent_status",
                "coordinate_workflow",
                "load_balance",
                "route_message",
            },
            version="1.0.0",
        )

        self._task_queue: asyncio.Queue = asyncio.Queue()
        self._pending_tasks: Dict[str, Dict[str, Any]] = {}
        self._task_history: List[Dict[str, Any]] = []
        self._agent_load: Dict[str, float] = defaultdict(float)
        self._capability_cache: Dict[str, Set[str]] = {}

    async def on_initialize(self) -> None:
        """Initialize coordinator resources."""
        logger.info("Initializing CoordinatorAgent")

        # Subscribe to coordination messages
        await self.subscribe(
            MessageType.TASK_ASSIGNED,
            self._handle_task_request,
        )
        await self.subscribe(
            MessageType.RESPONSE,
            self._handle_task_response,
        )

        # Start task processor
        asyncio.create_task(self._process_task_queue())

        logger.info("CoordinatorAgent initialized")

    async def on_start(self) -> None:
        """Called when agent starts."""
        logger.info("CoordinatorAgent started - ready for orchestration")

    async def on_stop(self) -> None:
        """Cleanup when agent stops."""
        logger.info(
            f"CoordinatorAgent stopping - processed {len(self._task_history)} tasks"
        )

    async def execute_task(self, payload: Dict[str, Any]) -> Any:
        """Execute a coordination task."""
        action = payload.get("action", "")

        logger.debug(f"CoordinatorAgent executing: {action}")

        if action == "delegate_task":
            return await self._delegate_task(payload)
        elif action == "query_agents":
            return await self._query_agents(payload)
        elif action == "broadcast_message":
            return await self._broadcast_message(payload)
        elif action == "get_agent_status":
            return await self._get_agent_status(payload)
        elif action == "coordinate_workflow":
            return await self._coordinate_workflow(payload)
        elif action == "load_balance":
            return self._get_load_info()
        elif action == "get_stats":
            return self._get_stats()
        else:
            raise ValueError(f"Unknown coordinator action: {action}")

    async def _delegate_task(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Delegate a task to the most appropriate agent.

        Finds agents with matching capabilities and routes to the one
        with lowest load.
        """
        required_capability = payload.get("capability", "")
        task_payload = payload.get("task_payload", {})
        priority = payload.get("priority", "normal")
        timeout = payload.get("timeout", 30.0)

        # Find agents with this capability
        if self.registry:
            agents = await self.registry.find_by_capability(required_capability)
        else:
            agents = []

        if not agents:
            return {
                "status": "error",
                "error": f"No agents found with capability: {required_capability}",
            }

        # Select agent with lowest load
        selected = min(agents, key=lambda a: self._agent_load.get(a.agent_name, 0))

        # Create task ID
        task_id = f"task_{datetime.now().timestamp()}_{required_capability}"

        # Track pending task
        self._pending_tasks[task_id] = {
            "capability": required_capability,
            "agent": selected.agent_name,
            "started_at": datetime.now(),
            "status": "pending",
        }

        # Send task to agent
        await self.send_message(
            recipient=selected.agent_name,
            content={
                "type": "task",
                "task_id": task_id,
                "action": required_capability,
                **task_payload,
            },
            priority=MessagePriority[priority.upper()],
        )

        # Update load
        self._agent_load[selected.agent_name] += 1

        return {
            "status": "delegated",
            "task_id": task_id,
            "delegated_to": selected.agent_name,
        }

    async def _query_agents(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Query available agents by capability or type."""
        capability = payload.get("capability")
        agent_type = payload.get("agent_type")

        agents = []
        if self.registry:
            if capability:
                found = await self.registry.find_by_capability(capability)
                agents.extend(found)
            elif agent_type:
                all_agents = await self.registry.get_all()
                agents = [a for a in all_agents if a.agent_type == agent_type]
            else:
                agents = await self.registry.get_all()

        return {
            "status": "success",
            "count": len(agents),
            "agents": [
                {
                    "name": a.agent_name,
                    "type": a.agent_type,
                    "capabilities": list(a.capabilities),
                    "status": a.status.value,
                }
                for a in agents
            ],
        }

    async def _broadcast_message(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Broadcast a message to all agents or a subset."""
        message_content = payload.get("message", {})
        target_type = payload.get("target_type")  # Optional filter
        exclude_self = payload.get("exclude_self", True)

        if self.registry:
            agents = await self.registry.get_all()
        else:
            agents = []

        sent_count = 0
        for agent in agents:
            if exclude_self and agent.agent_name == self.agent_name:
                continue
            if target_type and agent.agent_type != target_type:
                continue

            await self.send_message(
                recipient=agent.agent_name,
                content=message_content,
            )
            sent_count += 1

        return {
            "status": "success",
            "sent_to": sent_count,
        }

    async def _get_agent_status(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Get status of specific agent(s)."""
        agent_name = payload.get("agent_name")

        if agent_name:
            if self.registry:
                agent = await self.registry.get(agent_name)
                if agent:
                    return {
                        "status": "success",
                        "agent": {
                            "name": agent.agent_name,
                            "type": agent.agent_type,
                            "status": agent.status.value,
                            "load": self._agent_load.get(agent.agent_name, 0),
                            "capabilities": list(agent.capabilities),
                        },
                    }
            return {"status": "error", "error": "Agent not found"}
        else:
            # Return all agent statuses
            if self.registry:
                agents = await self.registry.get_all()
            else:
                agents = []

            return {
                "status": "success",
                "agents": [
                    {
                        "name": a.agent_name,
                        "status": a.status.value,
                        "load": self._agent_load.get(a.agent_name, 0),
                    }
                    for a in agents
                ],
            }

    async def _coordinate_workflow(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Coordinate a multi-step workflow across agents.

        Executes a sequence of tasks, passing results between steps.
        """
        workflow_name = payload.get("name", "unnamed")
        steps = payload.get("steps", [])

        if not steps:
            return {"status": "error", "error": "No workflow steps provided"}

        workflow_id = f"workflow_{datetime.now().timestamp()}"
        results = []

        for i, step in enumerate(steps):
            capability = step.get("capability")
            step_payload = step.get("payload", {})

            # Include previous result if available
            if results:
                step_payload["previous_result"] = results[-1]

            # Delegate step
            result = await self._delegate_task({
                "capability": capability,
                "task_payload": step_payload,
            })

            if result["status"] == "error":
                return {
                    "status": "error",
                    "workflow_id": workflow_id,
                    "failed_at_step": i,
                    "error": result.get("error"),
                }

            results.append(result)

        return {
            "status": "success",
            "workflow_id": workflow_id,
            "workflow_name": workflow_name,
            "steps_completed": len(results),
            "results": results,
        }

    def _get_load_info(self) -> Dict[str, Any]:
        """Get load balancing information."""
        return {
            "status": "success",
            "agent_loads": dict(self._agent_load),
            "pending_tasks": len(self._pending_tasks),
        }

    def _get_stats(self) -> Dict[str, Any]:
        """Get coordinator statistics."""
        return {
            "status": "success",
            "total_tasks_processed": len(self._task_history),
            "pending_tasks": len(self._pending_tasks),
            "agent_loads": dict(self._agent_load),
        }

    async def _handle_task_request(self, message: AgentMessage) -> None:
        """Handle incoming task requests."""
        await self._task_queue.put(message)

    async def _handle_task_response(self, message: AgentMessage) -> None:
        """Handle task responses from agents."""
        task_id = message.content.get("task_id")
        if task_id in self._pending_tasks:
            task = self._pending_tasks.pop(task_id)
            task["completed_at"] = datetime.now()
            task["status"] = "completed"
            task["result"] = message.content.get("result")
            self._task_history.append(task)

            # Update load
            agent = task.get("agent")
            if agent and agent in self._agent_load:
                self._agent_load[agent] = max(0, self._agent_load[agent] - 1)

    async def _process_task_queue(self) -> None:
        """Background task processor."""
        max_runtime = float(os.getenv("TIMEOUT_TASK_QUEUE_SESSION", "86400.0"))  # 24 hours
        queue_timeout = float(os.getenv("TIMEOUT_TASK_QUEUE_GET", "1.0"))
        task_timeout = float(os.getenv("TIMEOUT_TASK_PROCESSING", "30.0"))
        start = time.monotonic()

        while time.monotonic() - start < max_runtime:
            try:
                message = await asyncio.wait_for(
                    self._task_queue.get(),
                    timeout=queue_timeout
                )
                # Process the message with timeout protection
                capability = message.content.get("capability")
                if capability:
                    await asyncio.wait_for(
                        self._delegate_task({
                            "capability": capability,
                            "task_payload": message.content,
                        }),
                        timeout=task_timeout,
                    )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Error processing task: {e}")

        logger.info("Task queue processor reached max runtime, exiting")
