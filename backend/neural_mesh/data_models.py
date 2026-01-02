"""
JARVIS Neural Mesh - Core Data Models

This module defines all data structures used throughout the Neural Mesh system.
All models are designed for:
- Async-first operation
- Memory efficiency
- Serialization/deserialization
- Type safety with comprehensive validation
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    TypeVar,
    Union,
)

import numpy as np

T = TypeVar("T")


# ============================================================================
# MESSAGE TYPES AND PRIORITIES
# ============================================================================


class MessagePriority(Enum):
    """Message priority levels for the communication bus.

    Priority determines processing order and latency targets:
    - CRITICAL: <1ms - System errors, safety, emergency shutdown
    - HIGH: <5ms - User-facing actions, real-time responses
    - NORMAL: <10ms - Background tasks, routine operations
    - LOW: <50ms - Logging, telemetry, analytics
    """
    CRITICAL = 0  # <1ms - System errors, safety
    HIGH = 1      # <5ms - User-facing actions
    NORMAL = 2    # <10ms - Background tasks
    LOW = 3       # <50ms - Logging, telemetry

    def __lt__(self, other: "MessagePriority") -> bool:
        if not isinstance(other, MessagePriority):
            return NotImplemented
        return self.value < other.value

    def __le__(self, other: "MessagePriority") -> bool:
        if not isinstance(other, MessagePriority):
            return NotImplemented
        return self.value <= other.value


class MessageType(Enum):
    """Types of messages that can be sent through the communication bus."""

    # Task Management
    TASK_ASSIGNED = "task_assigned"
    TASK_STARTED = "task_started"
    TASK_PROGRESS = "task_progress"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"

    # Agent Lifecycle
    AGENT_REGISTERED = "agent_registered"
    AGENT_UNREGISTERED = "agent_unregistered"
    AGENT_HEARTBEAT = "agent_heartbeat"
    AGENT_HEALTH_CHECK = "agent_health_check"
    AGENT_STATUS_CHANGED = "agent_status_changed"
    ANNOUNCEMENT = "announcement"  # General agent announcements

    # Knowledge Operations
    KNOWLEDGE_QUERY = "knowledge_query"
    KNOWLEDGE_RESPONSE = "knowledge_response"
    KNOWLEDGE_ADDED = "knowledge_added"
    KNOWLEDGE_UPDATED = "knowledge_updated"
    KNOWLEDGE_DELETED = "knowledge_deleted"
    KNOWLEDGE_SHARED = "knowledge_shared"

    # Context Operations
    CONTEXT_UPDATE = "context_update"
    CONTEXT_REQUEST = "context_request"

    # Error Handling
    ERROR_DETECTED = "error_detected"
    ERROR_RESOLVED = "error_resolved"
    ALERT_RAISED = "alert_raised"

    # Workflow Management
    WORKFLOW_START = "workflow_start"
    WORKFLOW_STEP_COMPLETE = "workflow_step_complete"
    WORKFLOW_COMPLETE = "workflow_complete"
    WORKFLOW_FAILED = "workflow_failed"

    # System Events
    SYSTEM_STARTUP = "system_startup"
    SYSTEM_SHUTDOWN = "system_shutdown"
    SYSTEM_CONFIG_CHANGED = "system_config_changed"

    # Inter-Agent Communication
    REQUEST = "request"
    RESPONSE = "response"
    BROADCAST = "broadcast"
    NOTIFICATION = "notification"  # v18.0: Agent event notifications (lifecycle, status)
    SUBSCRIPTION = "subscription"  # v18.0: Topic subscription messages
    CUSTOM = "custom"


# ============================================================================
# AGENT MESSAGE
# ============================================================================


@dataclass
class AgentMessage:
    """Message passed between agents through the communication bus.

    Attributes:
        message_id: Unique identifier for this message
        from_agent: Name of the sending agent
        to_agent: Name of the receiving agent (use "broadcast" for all)
        message_type: Type of message (determines handling)
        payload: Message data (any JSON-serializable data)
        priority: Processing priority
        timestamp: When the message was created
        correlation_id: Links related messages (for request/response)
        reply_to: Message ID this is responding to
        expires_at: When this message should be discarded if unprocessed
        metadata: Additional contextual information
        trace_id: For distributed tracing
        retries: Number of delivery attempts
    """

    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    from_agent: str = ""
    to_agent: str = ""
    message_type: MessageType = MessageType.CUSTOM
    payload: Dict[str, Any] = field(default_factory=dict)
    priority: MessagePriority = MessagePriority.NORMAL
    timestamp: datetime = field(default_factory=datetime.now)
    correlation_id: Optional[str] = None
    reply_to: Optional[str] = None
    expires_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    trace_id: Optional[str] = None
    retries: int = 0

    def __post_init__(self) -> None:
        """Validate and normalize message fields."""
        if not self.message_id:
            self.message_id = str(uuid.uuid4())
        if self.correlation_id is None:
            self.correlation_id = self.message_id
        if self.trace_id is None:
            self.trace_id = str(uuid.uuid4())[:8]

    def is_expired(self) -> bool:
        """Check if message has expired."""
        if self.expires_at is None:
            return False
        return datetime.now() > self.expires_at

    def is_broadcast(self) -> bool:
        """Check if this is a broadcast message."""
        return self.to_agent.lower() in ("broadcast", "*", "all", "")

    def create_response(
        self,
        from_agent: str,
        payload: Dict[str, Any],
        message_type: MessageType = MessageType.RESPONSE,
    ) -> "AgentMessage":
        """Create a response message to this message."""
        return AgentMessage(
            from_agent=from_agent,
            to_agent=self.from_agent,
            message_type=message_type,
            payload=payload,
            priority=self.priority,
            correlation_id=self.correlation_id,
            reply_to=self.message_id,
            trace_id=self.trace_id,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert message to dictionary for serialization."""
        return {
            "message_id": self.message_id,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "message_type": self.message_type.value,
            "payload": self.payload,
            "priority": self.priority.value,
            "timestamp": self.timestamp.isoformat(),
            "correlation_id": self.correlation_id,
            "reply_to": self.reply_to,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "metadata": self.metadata,
            "trace_id": self.trace_id,
            "retries": self.retries,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentMessage":
        """Create message from dictionary."""
        return cls(
            message_id=data.get("message_id", str(uuid.uuid4())),
            from_agent=data.get("from_agent", ""),
            to_agent=data.get("to_agent", ""),
            message_type=MessageType(data.get("message_type", "custom")),
            payload=data.get("payload", {}),
            priority=MessagePriority(data.get("priority", 2)),
            timestamp=datetime.fromisoformat(data["timestamp"]) if "timestamp" in data else datetime.now(),
            correlation_id=data.get("correlation_id"),
            reply_to=data.get("reply_to"),
            expires_at=datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None,
            metadata=data.get("metadata", {}),
            trace_id=data.get("trace_id"),
            retries=data.get("retries", 0),
        )


# ============================================================================
# KNOWLEDGE TYPES
# ============================================================================


class KnowledgeType(Enum):
    """Types of knowledge that can be stored in the knowledge graph."""

    ERROR = "error"              # Bugs encountered and solutions
    PATTERN = "pattern"          # Learned user behaviors and patterns
    SOLUTION = "solution"        # Successful fixes and workarounds
    CONTEXT = "context"          # Session and environment context
    FACT = "fact"                # Verified factual information
    PROCEDURE = "procedure"      # Step-by-step processes
    RELATIONSHIP = "relationship"  # Connections between entities
    ENTITY = "entity"            # Named entities (files, functions, etc.)
    OBSERVATION = "observation"  # Raw observations from agents
    INSIGHT = "insight"          # Derived insights from analysis
    PREFERENCE = "preference"    # User preferences and settings
    MEMORY = "memory"            # Long-term episodic memories


@dataclass
class KnowledgeEntry:
    """Entry in the shared knowledge graph.

    Attributes:
        id: Unique identifier
        knowledge_type: Category of knowledge
        agent_name: Agent that created this entry
        data: The actual knowledge data
        embedding: Vector representation for semantic search
        metadata: Additional contextual information
        created_at: When this entry was created
        updated_at: When this entry was last updated
        expires_at: When this entry should be automatically deleted
        version: Version number for conflict resolution
        confidence: Confidence score (0.0 to 1.0)
        source: Where this knowledge came from
        tags: Searchable tags
        relationships: IDs of related knowledge entries
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    knowledge_type: KnowledgeType = KnowledgeType.FACT
    agent_name: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    embedding: Optional[np.ndarray] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    expires_at: Optional[datetime] = None
    version: int = 1
    confidence: float = 1.0
    source: str = ""
    tags: Set[str] = field(default_factory=set)
    relationships: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Validate knowledge entry."""
        if not 0.0 <= self.confidence <= 1.0:
            self.confidence = max(0.0, min(1.0, self.confidence))
        if isinstance(self.tags, list):
            self.tags = set(self.tags)
        if isinstance(self.knowledge_type, str):
            self.knowledge_type = KnowledgeType(self.knowledge_type)

    def is_expired(self) -> bool:
        """Check if knowledge entry has expired."""
        if self.expires_at is None:
            return False
        return datetime.now() > self.expires_at

    def update(self, data: Dict[str, Any], agent_name: Optional[str] = None) -> None:
        """Update knowledge entry with new data."""
        self.data.update(data)
        self.updated_at = datetime.now()
        self.version += 1
        if agent_name:
            self.metadata["last_updated_by"] = agent_name

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "id": self.id,
            "knowledge_type": self.knowledge_type.value,
            "agent_name": self.agent_name,
            "data": self.data,
            "embedding": self.embedding.tolist() if self.embedding is not None else None,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "version": self.version,
            "confidence": self.confidence,
            "source": self.source,
            "tags": list(self.tags),
            "relationships": self.relationships,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KnowledgeEntry":
        """Create from dictionary."""
        embedding = data.get("embedding")
        if embedding is not None and not isinstance(embedding, np.ndarray):
            embedding = np.array(embedding, dtype=np.float32)

        return cls(
            id=data.get("id", str(uuid.uuid4())),
            knowledge_type=KnowledgeType(data.get("knowledge_type", "fact")),
            agent_name=data.get("agent_name", ""),
            data=data.get("data", {}),
            embedding=embedding,
            metadata=data.get("metadata", {}),
            created_at=datetime.fromisoformat(data["created_at"]) if "created_at" in data else datetime.now(),
            updated_at=datetime.fromisoformat(data["updated_at"]) if "updated_at" in data else datetime.now(),
            expires_at=datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None,
            version=data.get("version", 1),
            confidence=data.get("confidence", 1.0),
            source=data.get("source", ""),
            tags=set(data.get("tags", [])),
            relationships=data.get("relationships", []),
        )


@dataclass
class KnowledgeRelationship:
    """Relationship between knowledge entries in the graph.

    Attributes:
        id: Unique identifier
        source_id: ID of the source knowledge entry
        target_id: ID of the target knowledge entry
        relationship_type: Type of relationship
        strength: Relationship strength (0.0 to 1.0)
        metadata: Additional relationship data
        created_at: When this relationship was created
        bidirectional: Whether the relationship goes both ways
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    source_id: str = ""
    target_id: str = ""
    relationship_type: str = "related_to"
    strength: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    bidirectional: bool = False

    def __post_init__(self) -> None:
        """Validate relationship."""
        self.strength = max(0.0, min(1.0, self.strength))

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relationship_type": self.relationship_type,
            "strength": self.strength,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
            "bidirectional": self.bidirectional,
        }


# ============================================================================
# AGENT INFORMATION
# ============================================================================


class AgentStatus(Enum):
    """Status of an agent in the registry."""

    INITIALIZING = "initializing"  # Agent is starting up
    ONLINE = "online"              # Agent is ready and available
    BUSY = "busy"                  # Agent is processing tasks
    PAUSED = "paused"              # Agent is temporarily paused
    OFFLINE = "offline"            # Agent is not responding
    ERROR = "error"                # Agent encountered an error
    SHUTTING_DOWN = "shutting_down"  # Agent is stopping


class HealthStatus(Enum):
    """Health status for agents and components."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class AgentInfo:
    """Information about a registered agent.

    Attributes:
        agent_name: Unique name for this agent
        agent_type: Category of agent (vision, voice, context, etc.)
        capabilities: Set of capabilities this agent provides
        backend: Where this agent runs (local, cloud, hybrid)
        status: Current status
        load: Current load (0.0 = idle, 1.0 = max capacity)
        registered_at: When the agent registered
        last_heartbeat: Last heartbeat received
        metadata: Additional agent information
        stats: Runtime statistics
        version: Agent version
        dependencies: Other agents this agent depends on
        health: Current health status
        error_count: Number of errors since registration
        task_queue_size: Number of pending tasks
    """

    agent_name: str
    agent_type: str
    capabilities: Set[str] = field(default_factory=set)
    backend: str = "local"
    status: AgentStatus = AgentStatus.INITIALIZING
    load: float = 0.0
    registered_at: datetime = field(default_factory=datetime.now)
    last_heartbeat: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)
    stats: Dict[str, int] = field(default_factory=lambda: {
        "tasks_completed": 0,
        "tasks_failed": 0,
        "messages_sent": 0,
        "messages_received": 0,
        "knowledge_contributed": 0,
    })
    version: str = "1.0.0"
    dependencies: Set[str] = field(default_factory=set)
    health: HealthStatus = HealthStatus.UNKNOWN
    error_count: int = 0
    task_queue_size: int = 0

    def __post_init__(self) -> None:
        """Validate agent info."""
        self.load = max(0.0, min(1.0, self.load))
        if isinstance(self.capabilities, list):
            self.capabilities = set(self.capabilities)
        if isinstance(self.dependencies, list):
            self.dependencies = set(self.dependencies)
        if isinstance(self.status, str):
            self.status = AgentStatus(self.status)
        if isinstance(self.health, str):
            self.health = HealthStatus(self.health)

    def is_available(self) -> bool:
        """Check if agent is available for tasks."""
        return (
            self.status == AgentStatus.ONLINE
            and self.load < 0.9
            and self.health in (HealthStatus.HEALTHY, HealthStatus.UNKNOWN)
        )

    def has_capability(self, capability: str) -> bool:
        """Check if agent has a specific capability."""
        return capability.lower() in {c.lower() for c in self.capabilities}

    def update_heartbeat(self) -> None:
        """Update the last heartbeat timestamp."""
        self.last_heartbeat = datetime.now()
        if self.status == AgentStatus.OFFLINE:
            self.status = AgentStatus.ONLINE

    def heartbeat_age_seconds(self) -> float:
        """Get the age of the last heartbeat in seconds."""
        return (datetime.now() - self.last_heartbeat).total_seconds()

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "agent_name": self.agent_name,
            "agent_type": self.agent_type,
            "capabilities": list(self.capabilities),
            "backend": self.backend,
            "status": self.status.value,
            "load": self.load,
            "registered_at": self.registered_at.isoformat(),
            "last_heartbeat": self.last_heartbeat.isoformat(),
            "metadata": self.metadata,
            "stats": self.stats,
            "version": self.version,
            "dependencies": list(self.dependencies),
            "health": self.health.value,
            "error_count": self.error_count,
            "task_queue_size": self.task_queue_size,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentInfo":
        """Create from dictionary."""
        return cls(
            agent_name=data["agent_name"],
            agent_type=data["agent_type"],
            capabilities=set(data.get("capabilities", [])),
            backend=data.get("backend", "local"),
            status=AgentStatus(data.get("status", "initializing")),
            load=data.get("load", 0.0),
            registered_at=datetime.fromisoformat(data["registered_at"]) if "registered_at" in data else datetime.now(),
            last_heartbeat=datetime.fromisoformat(data["last_heartbeat"]) if "last_heartbeat" in data else datetime.now(),
            metadata=data.get("metadata", {}),
            stats=data.get("stats", {}),
            version=data.get("version", "1.0.0"),
            dependencies=set(data.get("dependencies", [])),
            health=HealthStatus(data.get("health", "unknown")),
            error_count=data.get("error_count", 0),
            task_queue_size=data.get("task_queue_size", 0),
        )


# ============================================================================
# WORKFLOW MODELS
# ============================================================================


class ExecutionStrategy(Enum):
    """Strategy for executing workflow tasks."""

    SEQUENTIAL = "sequential"  # Execute tasks one after another
    PARALLEL = "parallel"      # Execute all tasks simultaneously
    HYBRID = "hybrid"          # Mix based on dependencies
    ADAPTIVE = "adaptive"      # Dynamically adjust based on load/performance


@dataclass
class WorkflowTask:
    """A task within a multi-agent workflow.

    Attributes:
        task_id: Unique identifier for this task
        name: Human-readable name
        description: What this task does
        required_capability: Capability needed to execute this task
        input_data: Input data for the task
        dependencies: Task IDs that must complete before this task
        timeout_seconds: Maximum time to wait for completion
        retry_count: Number of retries on failure
        retry_delay_seconds: Delay between retries
        fallback_capability: Alternative capability if primary fails
        priority: Task priority
        assigned_agent: Agent assigned to this task (set by orchestrator)
        status: Current task status
        result: Task result after completion
        error: Error message if failed
        started_at: When task started executing
        completed_at: When task completed
    """

    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    description: str = ""
    required_capability: str = ""
    input_data: Dict[str, Any] = field(default_factory=dict)
    dependencies: List[str] = field(default_factory=list)
    timeout_seconds: float = 30.0
    retry_count: int = 3
    retry_delay_seconds: float = 1.0
    fallback_capability: Optional[str] = None
    priority: MessagePriority = MessagePriority.NORMAL
    assigned_agent: Optional[str] = None
    status: str = "pending"  # pending, running, completed, failed, cancelled
    result: Any = None
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def is_ready(self, completed_tasks: Set[str]) -> bool:
        """Check if this task is ready to execute (all dependencies met)."""
        return all(dep in completed_tasks for dep in self.dependencies)

    def execution_time_seconds(self) -> Optional[float]:
        """Get the execution time in seconds."""
        if self.started_at is None or self.completed_at is None:
            return None
        return (self.completed_at - self.started_at).total_seconds()

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "task_id": self.task_id,
            "name": self.name,
            "description": self.description,
            "required_capability": self.required_capability,
            "input_data": self.input_data,
            "dependencies": self.dependencies,
            "timeout_seconds": self.timeout_seconds,
            "retry_count": self.retry_count,
            "retry_delay_seconds": self.retry_delay_seconds,
            "fallback_capability": self.fallback_capability,
            "priority": self.priority.value,
            "assigned_agent": self.assigned_agent,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


@dataclass
class WorkflowResult:
    """Result from a completed workflow.

    Attributes:
        workflow_id: Unique identifier for the workflow
        name: Workflow name
        status: Final status (completed, failed, cancelled)
        tasks: All tasks in the workflow with their results
        started_at: When the workflow started
        completed_at: When the workflow completed
        total_execution_time_seconds: Total time taken
        successful_tasks: Number of successful tasks
        failed_tasks: Number of failed tasks
        metadata: Additional result metadata
    """

    workflow_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    status: str = "pending"
    tasks: List[WorkflowTask] = field(default_factory=list)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    total_execution_time_seconds: float = 0.0
    successful_tasks: int = 0
    failed_tasks: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_successful(self) -> bool:
        """Check if workflow completed successfully."""
        return self.status == "completed" and self.failed_tasks == 0

    def get_task_results(self) -> Dict[str, Any]:
        """Get results from all completed tasks."""
        return {
            task.task_id: task.result
            for task in self.tasks
            if task.status == "completed"
        }

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "workflow_id": self.workflow_id,
            "name": self.name,
            "status": self.status,
            "tasks": [task.to_dict() for task in self.tasks],
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "total_execution_time_seconds": self.total_execution_time_seconds,
            "successful_tasks": self.successful_tasks,
            "failed_tasks": self.failed_tasks,
            "metadata": self.metadata,
        }


# ============================================================================
# CALLBACK TYPES
# ============================================================================

MessageCallback = Callable[[AgentMessage], Any]
KnowledgeCallback = Callable[[KnowledgeEntry], Any]
