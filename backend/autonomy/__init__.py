"""
JARVIS Autonomous Decision System
Transforms JARVIS into a proactive digital agent

This package provides comprehensive autonomous capabilities including:
- LangGraph-based reasoning engine for multi-step autonomous tasks
- LangChain tool registry with dynamic auto-discovery
- Async tool orchestration with parallel execution
- Multi-tier memory management and checkpointing
- Seamless integration with existing JARVIS systems
"""

# Original JARVIS autonomy components
from .autonomous_decision_engine import (
    AutonomousDecisionEngine,
    AutonomousAction,
    ActionPriority,
    ActionCategory
)
from .permission_manager import PermissionManager
from .context_engine import ContextEngine, UserState, ContextAnalysis
from .action_executor import ActionExecutor, ExecutionResult, ExecutionStatus
from .autonomous_behaviors import (
    MessageHandler,
    MeetingHandler,
    WorkspaceOrganizer,
    SecurityHandler,
    AutonomousBehaviorManager
)

# LangGraph Reasoning Engine
from .langgraph_engine import (
    LangGraphReasoningEngine,
    GraphState,
    ReasoningPhase,
    ConfidenceLevel,
    ActionOutcome,
    create_reasoning_engine,
    quick_reason
)

# LangChain Tool Registry
from .langchain_tools import (
    ToolRegistry,
    JARVISTool,
    FunctionTool,
    ToolCategory,
    ToolRiskLevel,
    ToolMetadata,
    jarvis_tool,
    register_builtin_tools,
    auto_discover_tools,
    get_tool,
    list_tools,
    search_tools,
    execute_tool
)

# Tool Orchestrator
from .tool_orchestrator import (
    ToolOrchestrator,
    ExecutionTask,
    ExecutionStrategy,
    ExecutionPriority,
    ExecutionContext,
    CircuitBreaker,
    create_orchestrator,
    create_execution_task,
    get_orchestrator
)

# Memory Integration
from .memory_integration import (
    MemoryManager,
    MemoryEntry,
    MemoryType,
    MemoryPriority,
    ConversationMemory,
    EpisodicMemory,
    JARVISCheckpointer,
    create_memory_manager,
    create_checkpointer,
    get_memory_manager,
    get_conversation_memory,
    remember,
    recall
)

# JARVIS Integration
from .jarvis_integration import (
    JARVISIntegrationManager,
    IntegrationConfig,
    PermissionAdapter,
    ActionQueueAdapter,
    ActionExecutorAdapter,
    ContextAdapter,
    LearningAdapter,
    get_integration_manager,
    configure_integration,
    auto_configure_integration
)

# Unified Autonomous Agent
from .autonomous_agent import (
    AutonomousAgent,
    AgentConfig,
    AgentMode,
    AgentPersonality,
    AgentBuilder,
    create_agent,
    create_and_initialize_agent,
    get_default_agent,
    run_autonomous,
    chat
)

# Reactor-Core Watcher (Auto-deploy trained models)
from .reactor_core_watcher import (
    ReactorCoreWatcher,
    ReactorCoreConfig,
    DeploymentResult,
    ModelValidator,
    get_reactor_core_watcher,
    start_reactor_core_watcher,
    stop_reactor_core_watcher,
)

# Unified Data Flywheel (Self-improving learning loop)
from .unified_data_flywheel import (
    UnifiedDataFlywheel,
    FlywheelConfig,
    FlywheelProgress,
    FlywheelResult,
    FlywheelStage,
    DataSourceType,
    get_data_flywheel,
    run_flywheel_cycle,
    get_flywheel_status,
)

__all__ = [
    # Original Decision Engine
    'AutonomousDecisionEngine',
    'AutonomousAction',
    'ActionPriority',
    'ActionCategory',

    # Permission Manager
    'PermissionManager',

    # Context Engine
    'ContextEngine',
    'UserState',
    'ContextAnalysis',

    # Action Executor (Original)
    'ActionExecutor',
    'ExecutionResult',
    'ExecutionStatus',

    # Behavior Handlers
    'MessageHandler',
    'MeetingHandler',
    'WorkspaceOrganizer',
    'SecurityHandler',
    'AutonomousBehaviorManager',

    # LangGraph Reasoning Engine
    'LangGraphReasoningEngine',
    'GraphState',
    'ReasoningPhase',
    'ConfidenceLevel',
    'ActionOutcome',
    'create_reasoning_engine',
    'quick_reason',

    # LangChain Tools
    'ToolRegistry',
    'JARVISTool',
    'FunctionTool',
    'ToolCategory',
    'ToolRiskLevel',
    'ToolMetadata',
    'jarvis_tool',
    'register_builtin_tools',
    'auto_discover_tools',
    'get_tool',
    'list_tools',
    'search_tools',
    'execute_tool',

    # Tool Orchestrator
    'ToolOrchestrator',
    'ExecutionTask',
    'ExecutionStrategy',
    'ExecutionPriority',
    'ExecutionContext',
    'CircuitBreaker',
    'create_orchestrator',
    'create_execution_task',
    'get_orchestrator',

    # Memory
    'MemoryManager',
    'MemoryEntry',
    'MemoryType',
    'MemoryPriority',
    'ConversationMemory',
    'EpisodicMemory',
    'JARVISCheckpointer',
    'create_memory_manager',
    'create_checkpointer',
    'get_memory_manager',
    'get_conversation_memory',
    'remember',
    'recall',

    # Integration
    'JARVISIntegrationManager',
    'IntegrationConfig',
    'PermissionAdapter',
    'ActionQueueAdapter',
    'ActionExecutorAdapter',
    'ContextAdapter',
    'LearningAdapter',
    'get_integration_manager',
    'configure_integration',
    'auto_configure_integration',

    # Autonomous Agent
    'AutonomousAgent',
    'AgentConfig',
    'AgentMode',
    'AgentPersonality',
    'AgentBuilder',
    'create_agent',
    'create_and_initialize_agent',
    'get_default_agent',
    'run_autonomous',
    'chat',

    # Reactor-Core Watcher
    'ReactorCoreWatcher',
    'ReactorCoreConfig',
    'DeploymentResult',
    'ModelValidator',
    'get_reactor_core_watcher',
    'start_reactor_core_watcher',
    'stop_reactor_core_watcher',

    # Unified Data Flywheel
    'UnifiedDataFlywheel',
    'FlywheelConfig',
    'FlywheelProgress',
    'FlywheelResult',
    'FlywheelStage',
    'DataSourceType',
    'get_data_flywheel',
    'run_flywheel_cycle',
    'get_flywheel_status',
]