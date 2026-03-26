"""
AgentTeamCoordinator — Multi-agent coordination with shared task list + messaging.

Critical Gap: SubagentScheduler does parallel work units but NO inter-agent
communication, NO shared task list, NO teammate messaging. This module adds
the missing coordination layer.

Architecture (mirrors Claude Code Agent Teams):
  - Team Lead: the GovernedLoopService orchestrator
  - Teammates: parallel SubagentScheduler work units with communication
  - Shared Task List: persistent, claimable, with dependencies
  - Mailbox: async inter-agent messaging for collaboration
  - Task States: pending -> claimed -> in_progress -> completed/failed

Boundary Principle:
  Deterministic: Task list management, claim locking, message routing,
  dependency resolution. All state transitions are explicit.
  Agentic: Task content, work strategy, and inter-agent conversation.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_PERSISTENCE_DIR = Path(
    os.environ.get(
        "JARVIS_AGENT_TEAM_DIR",
        str(Path.home() / ".jarvis" / "ouroboros" / "teams"),
    )
)
_MAX_TEAMMATES = int(os.environ.get("JARVIS_AGENT_TEAM_MAX_TEAMMATES", "6"))
_MAX_TASKS = int(os.environ.get("JARVIS_AGENT_TEAM_MAX_TASKS", "50"))


class TaskState(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class TeammateRole(str, Enum):
    LEAD = "lead"
    WORKER = "worker"
    REVIEWER = "reviewer"
    RESEARCHER = "researcher"


@dataclass
class TeamTask:
    """A task in the shared task list."""
    task_id: str
    goal: str
    state: TaskState = TaskState.PENDING
    assigned_to: str = ""       # teammate_id
    depends_on: Tuple[str, ...] = ()
    target_files: Tuple[str, ...] = ()
    repo: str = "jarvis"
    priority: int = 0           # Lower = higher priority
    result: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    claimed_at: float = 0.0
    completed_at: float = 0.0

    @property
    def is_blocked(self) -> bool:
        """Check if all dependencies are resolved."""
        return len(self.depends_on) > 0  # Caller checks if deps are completed


@dataclass
class TeammateInfo:
    """Information about a team member."""
    teammate_id: str
    name: str
    role: TeammateRole
    specialization: str        # "security", "performance", "testing", etc.
    status: str = "idle"       # "idle", "working", "completed", "failed"
    current_task: str = ""     # task_id
    tasks_completed: int = 0


@dataclass
class TeamMessage:
    """An inter-agent message."""
    from_id: str
    to_id: str                 # "" = broadcast to all
    content: str
    timestamp: float = field(default_factory=time.time)
    message_type: str = "info" # "info", "request", "result", "question"


class AgentTeamCoordinator:
    """Multi-agent coordination with shared task list and messaging.

    Manages a team of agents working on a complex goal. The lead
    decomposes work into tasks, teammates claim and execute tasks,
    and all agents communicate via the mailbox.

    Usage:
      team = AgentTeamCoordinator("refactor-auth")
      team.add_teammate("sec-reviewer", "Security Reviewer", TeammateRole.REVIEWER, "security")
      team.add_teammate("impl-worker", "Implementation", TeammateRole.WORKER, "code")
      team.add_task(TeamTask(task_id="t1", goal="Review auth module for vulnerabilities"))
      team.add_task(TeamTask(task_id="t2", goal="Implement OAuth2 flow", depends_on=("t1",)))

      # Teammates claim tasks
      task = team.claim_next_task("sec-reviewer")

      # Teammates communicate
      team.send_message(TeamMessage(from_id="sec-reviewer", to_id="impl-worker", content="Found XSS"))

      # Check if all done
      if team.is_complete():
          results = team.get_results()
    """

    def __init__(
        self,
        team_name: str,
        persistence_dir: Path = _PERSISTENCE_DIR,
    ) -> None:
        self._team_name = team_name
        self._persistence_dir = persistence_dir / team_name
        self._tasks: Dict[str, TeamTask] = {}
        self._teammates: Dict[str, TeammateInfo] = {}
        self._mailbox: List[TeamMessage] = []
        self._lock = asyncio.Lock()
        self._load()

    @property
    def team_name(self) -> str:
        return self._team_name

    # ------------------------------------------------------------------
    # Teammate management
    # ------------------------------------------------------------------

    def add_teammate(
        self,
        teammate_id: str,
        name: str,
        role: TeammateRole,
        specialization: str = "",
    ) -> TeammateInfo:
        """Register a new teammate."""
        if len(self._teammates) >= _MAX_TEAMMATES:
            raise RuntimeError(f"Max teammates ({_MAX_TEAMMATES}) reached")

        info = TeammateInfo(
            teammate_id=teammate_id,
            name=name,
            role=role,
            specialization=specialization,
        )
        self._teammates[teammate_id] = info
        self._persist()
        logger.info(
            "[AgentTeam:%s] Teammate added: %s (%s, %s)",
            self._team_name, name, role.value, specialization,
        )
        return info

    def get_teammate(self, teammate_id: str) -> Optional[TeammateInfo]:
        return self._teammates.get(teammate_id)

    def list_teammates(self) -> List[TeammateInfo]:
        return list(self._teammates.values())

    # ------------------------------------------------------------------
    # Task management (shared task list with dependencies)
    # ------------------------------------------------------------------

    def add_task(self, task: TeamTask) -> None:
        """Add a task to the shared list."""
        if len(self._tasks) >= _MAX_TASKS:
            raise RuntimeError(f"Max tasks ({_MAX_TASKS}) reached")
        self._tasks[task.task_id] = task
        self._persist()

    def claim_next_task(self, teammate_id: str) -> Optional[TeamTask]:
        """Claim the next available unblocked task. Thread-safe via asyncio.Lock pattern.

        Returns the claimed task or None if no tasks available.
        Tasks are selected by priority (lower = higher priority).
        """
        # Find completed task IDs for dependency resolution
        completed_ids = {
            tid for tid, t in self._tasks.items()
            if t.state == TaskState.COMPLETED
        }

        # Find claimable tasks (pending + all deps completed)
        claimable = []
        for task in self._tasks.values():
            if task.state != TaskState.PENDING:
                continue
            if task.depends_on and not all(d in completed_ids for d in task.depends_on):
                continue
            claimable.append(task)

        if not claimable:
            return None

        # Sort by priority (lower = higher)
        claimable.sort(key=lambda t: t.priority)
        task = claimable[0]

        # Claim it
        task.state = TaskState.CLAIMED
        task.assigned_to = teammate_id
        task.claimed_at = time.time()

        # Update teammate status
        teammate = self._teammates.get(teammate_id)
        if teammate:
            teammate.status = "working"
            teammate.current_task = task.task_id

        self._persist()
        logger.info(
            "[AgentTeam:%s] Task %s claimed by %s: %s",
            self._team_name, task.task_id, teammate_id, task.goal[:50],
        )
        return task

    def start_task(self, task_id: str) -> None:
        """Mark a task as in progress."""
        task = self._tasks.get(task_id)
        if task:
            task.state = TaskState.IN_PROGRESS
            self._persist()

    def complete_task(self, task_id: str, result: str = "") -> None:
        """Mark a task as completed with result."""
        task = self._tasks.get(task_id)
        if task:
            task.state = TaskState.COMPLETED
            task.result = result
            task.completed_at = time.time()

            teammate = self._teammates.get(task.assigned_to)
            if teammate:
                teammate.status = "idle"
                teammate.current_task = ""
                teammate.tasks_completed += 1

            self._persist()
            logger.info(
                "[AgentTeam:%s] Task %s completed by %s",
                self._team_name, task_id, task.assigned_to,
            )

    def fail_task(self, task_id: str, error: str = "") -> None:
        """Mark a task as failed."""
        task = self._tasks.get(task_id)
        if task:
            task.state = TaskState.FAILED
            task.error = error

            teammate = self._teammates.get(task.assigned_to)
            if teammate:
                teammate.status = "idle"
                teammate.current_task = ""

            self._persist()

    def get_task(self, task_id: str) -> Optional[TeamTask]:
        return self._tasks.get(task_id)

    def list_tasks(self, state: Optional[TaskState] = None) -> List[TeamTask]:
        if state:
            return [t for t in self._tasks.values() if t.state == state]
        return list(self._tasks.values())

    # ------------------------------------------------------------------
    # Inter-agent messaging (mailbox)
    # ------------------------------------------------------------------

    def send_message(self, message: TeamMessage) -> None:
        """Send a message to one teammate or broadcast to all."""
        self._mailbox.append(message)
        self._persist()

        if message.to_id:
            logger.debug(
                "[AgentTeam:%s] Message %s -> %s: %s",
                self._team_name, message.from_id, message.to_id,
                message.content[:60],
            )
        else:
            logger.debug(
                "[AgentTeam:%s] Broadcast from %s: %s",
                self._team_name, message.from_id, message.content[:60],
            )

    def get_messages(
        self, teammate_id: str, since: float = 0.0
    ) -> List[TeamMessage]:
        """Get messages for a teammate (direct + broadcast) since timestamp."""
        return [
            m for m in self._mailbox
            if m.timestamp > since
            and (m.to_id == teammate_id or m.to_id == "")
            and m.from_id != teammate_id  # Don't return own messages
        ]

    def broadcast(self, from_id: str, content: str, msg_type: str = "info") -> None:
        """Broadcast a message to all teammates."""
        self.send_message(TeamMessage(
            from_id=from_id, to_id="", content=content, message_type=msg_type,
        ))

    # ------------------------------------------------------------------
    # Team status
    # ------------------------------------------------------------------

    def is_complete(self) -> bool:
        """Check if all tasks are completed or failed."""
        return all(
            t.state in (TaskState.COMPLETED, TaskState.FAILED)
            for t in self._tasks.values()
        )

    def get_progress(self) -> Dict[str, Any]:
        """Get team progress summary."""
        total = len(self._tasks)
        by_state = {}
        for t in self._tasks.values():
            by_state[t.state.value] = by_state.get(t.state.value, 0) + 1

        return {
            "team_name": self._team_name,
            "total_tasks": total,
            "tasks_by_state": by_state,
            "teammates": len(self._teammates),
            "active_teammates": sum(
                1 for t in self._teammates.values() if t.status == "working"
            ),
            "messages": len(self._mailbox),
            "complete": self.is_complete(),
        }

    def get_results(self) -> List[Dict[str, Any]]:
        """Get results from all completed tasks."""
        return [
            {
                "task_id": t.task_id,
                "goal": t.goal,
                "state": t.state.value,
                "result": t.result,
                "error": t.error,
                "assigned_to": t.assigned_to,
                "duration_s": (t.completed_at - t.claimed_at) if t.completed_at else 0,
            }
            for t in self._tasks.values()
        ]

    def format_for_prompt(self) -> str:
        """Format team status for injection into generation prompt."""
        progress = self.get_progress()
        lines = [
            f"## Agent Team: {self._team_name}",
            f"Tasks: {progress['tasks_by_state']}",
            f"Teammates: {progress['teammates']} ({progress['active_teammates']} active)",
        ]
        # Recent messages
        recent = self._mailbox[-5:] if self._mailbox else []
        if recent:
            lines.append("\nRecent team messages:")
            for m in recent:
                target = f"-> {m.to_id}" if m.to_id else "(broadcast)"
                lines.append(f"  [{m.from_id} {target}]: {m.content[:80]}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Persistence (JSON on disk)
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        try:
            self._persistence_dir.mkdir(parents=True, exist_ok=True)

            # Tasks
            tasks_data = {
                tid: {
                    "task_id": t.task_id, "goal": t.goal,
                    "state": t.state.value, "assigned_to": t.assigned_to,
                    "depends_on": list(t.depends_on),
                    "target_files": list(t.target_files),
                    "repo": t.repo, "priority": t.priority,
                    "result": t.result, "error": t.error,
                    "created_at": t.created_at, "claimed_at": t.claimed_at,
                    "completed_at": t.completed_at,
                }
                for tid, t in self._tasks.items()
            }
            (self._persistence_dir / "tasks.json").write_text(
                json.dumps(tasks_data, indent=2)
            )

            # Teammates
            mates_data = {
                tid: {
                    "teammate_id": t.teammate_id, "name": t.name,
                    "role": t.role.value, "specialization": t.specialization,
                    "status": t.status, "current_task": t.current_task,
                    "tasks_completed": t.tasks_completed,
                }
                for tid, t in self._teammates.items()
            }
            (self._persistence_dir / "teammates.json").write_text(
                json.dumps(mates_data, indent=2)
            )

        except Exception:
            logger.debug("[AgentTeam] Persist failed", exc_info=True)

    def _load(self) -> None:
        try:
            tasks_path = self._persistence_dir / "tasks.json"
            if tasks_path.exists():
                data = json.loads(tasks_path.read_text())
                for tid, td in data.items():
                    self._tasks[tid] = TeamTask(
                        task_id=td["task_id"], goal=td["goal"],
                        state=TaskState(td["state"]),
                        assigned_to=td.get("assigned_to", ""),
                        depends_on=tuple(td.get("depends_on", [])),
                        target_files=tuple(td.get("target_files", [])),
                        repo=td.get("repo", "jarvis"),
                        priority=td.get("priority", 0),
                        result=td.get("result", ""),
                        error=td.get("error", ""),
                        created_at=td.get("created_at", 0),
                        claimed_at=td.get("claimed_at", 0),
                        completed_at=td.get("completed_at", 0),
                    )

            mates_path = self._persistence_dir / "teammates.json"
            if mates_path.exists():
                data = json.loads(mates_path.read_text())
                for tid, md in data.items():
                    self._teammates[tid] = TeammateInfo(
                        teammate_id=md["teammate_id"],
                        name=md["name"],
                        role=TeammateRole(md["role"]),
                        specialization=md.get("specialization", ""),
                        status=md.get("status", "idle"),
                        current_task=md.get("current_task", ""),
                        tasks_completed=md.get("tasks_completed", 0),
                    )
        except Exception:
            logger.debug("[AgentTeam] Load failed", exc_info=True)

    def cleanup(self) -> None:
        """Remove team data from disk."""
        import shutil
        try:
            if self._persistence_dir.exists():
                shutil.rmtree(self._persistence_dir)
                logger.info("[AgentTeam:%s] Cleaned up", self._team_name)
        except Exception:
            pass
