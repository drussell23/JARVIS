"""
JARVIS Intelligence Layer
=========================

Advanced intelligence systems for JARVIS AI Agent with LangGraph
Chain-of-Thought reasoning capabilities.

Modules:
- unified_awareness_engine: Fusion of Context and Situational Awareness
- chain_of_thought: LangGraph-based multi-step reasoning
- uae_langgraph: Enhanced UAE with chain-of-thought
- intelligence_langgraph: Enhanced SAI, CAI, and Unified Orchestrator
- learning_database: Speaker profile and learning data storage

Features:
- Multi-step autonomous reasoning with explicit thought chains
- Self-reflection and confidence calibration
- Cross-system intelligence fusion
- Continuous learning from outcomes
- Transparent decision audit trails

ARCHITECTURE NOTE:
This module uses LAZY IMPORTS to allow partial imports when not all
dependencies are available (e.g., numpy not installed). This enables
lightweight scripts to import just the learning_database without
pulling in the entire ML stack.
"""

import importlib
import logging
import sys
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

__version__ = '2.0.0'

# =============================================================================
# LAZY IMPORT SYSTEM
# =============================================================================
# This allows scripts to import specific submodules without triggering
# imports of heavyweight dependencies like numpy, torch, etc.

_LAZY_MODULES = {
    # Module name -> (submodule path, list of exports)
    'unified_awareness_engine': (
        '.unified_awareness_engine',
        [
            'UnifiedAwarenessEngine', 'ContextIntelligenceLayer',
            'SituationalAwarenessLayer', 'AwarenessIntegrationLayer',
            'get_uae_engine', 'UnifiedDecision', 'ExecutionResult',
            'ContextualData', 'SituationalData', 'ElementPriority',
            'DecisionSource', 'ConfidenceSource'
        ]
    ),
    'chain_of_thought': (
        '.chain_of_thought',
        [
            'ChainOfThoughtEngine', 'ChainOfThoughtMixin', 'ChainOfThoughtState',
            'ReasoningStrategy', 'ThoughtType', 'Thought', 'ReasoningChain',
            'Hypothesis', 'create_cot_engine', 'get_cot_engine'
        ]
    ),
    'uae_langgraph': (
        '.uae_langgraph',
        [
            'EnhancedUAE', 'UAEGraphState', 'UAEReasoningPhase',
            'ReasonedDecision', 'create_enhanced_uae', 'get_enhanced_uae'
        ]
    ),
    'intelligence_langgraph': (
        '.intelligence_langgraph',
        [
            'EnhancedSAI', 'SAIGraphState', 'SAIReasoningPhase', 'create_enhanced_sai',
            'EnhancedCAI', 'CAIGraphState', 'CAIReasoningPhase', 'create_enhanced_cai',
            'UnifiedIntelligenceOrchestrator', 'create_unified_orchestrator',
            'get_unified_orchestrator'
        ]
    ),
    'learning_database': (
        '.learning_database',
        [
            'JARVISLearningDatabase', 'get_learning_database',
            'get_learning_database_sync'
        ]
    ),
    'repository_intelligence': (
        '.repository_intelligence',
        [
            'RepositoryMapper', 'RepositoryGraph', 'CodeParser',
            'RepositoryIntelligenceConfig', 'RepositoryConfig',
            'RepositoryReasoningGraph', 'RepositoryReasoningState',
            'get_repository_mapper', 'get_repository_reasoning',
            'get_repo_map', 'query_codebase',
            'SymbolKind', 'RelationshipType', 'MapRefreshMode',
            'CodeSymbol', 'CodeRelationship', 'FileInfo', 'Tag',
            'IntelligenceResult', 'RepoMapResult', 'CrossRepoAnalysis',
        ]
    ),
    # =========================================================================
    # NEW MODULES: Cross-Repo Integration (Heist Patterns)
    # =========================================================================
    'unified_memory_system': (
        '.unified_memory_system',
        [
            # Configuration
            'UnifiedMemoryConfig',
            # Enums
            'MemoryBlockType', 'MemoryPriority', 'MemoryEventType',
            # Data Classes
            'MemoryBlock', 'MemoryEntry', 'MemorySearchResult',
            # Core Classes
            'CoreMemory', 'WorkingMemory', 'ArchivalMemory', 'UnifiedMemorySystem',
            # Convenience Functions
            'get_memory_system', 'store_memory', 'retrieve_memory', 'search_memory',
        ]
    ),
    'wisdom_patterns': (
        '.wisdom_patterns',
        [
            # Configuration
            'WisdomPatternsConfig',
            # Enums
            'PatternCategory',
            # Data Classes
            'WisdomPattern', 'PatternMatch',
            # Core Classes
            'WisdomPatternRegistry', 'WisdomAgent',
            # Convenience Functions
            'get_pattern_registry', 'get_wisdom_agent',
            'enhance_with_wisdom', 'get_pattern', 'suggest_pattern',
        ]
    ),
    'computer_use_refinements': (
        '.computer_use_refinements',
        [
            # Configuration
            'ComputerUseConfig',
            # Tool Results
            'ToolResult', 'CLIResult', 'ToolFailure', 'ToolError',
            # Tool Protocol and Base
            'ComputerTool', 'BaseComputerTool', 'ToolCollection',
            # Concrete Tools
            'ScreenshotTool', 'MouseTool', 'KeyboardTool', 'BashTool',
            # Safety
            'SafetyMonitor',
            # Execution Loop
            'StreamChunk', 'ComputerUseLoop',
            # System Prompts
            'get_system_prompt',
            # Factory Functions
            'create_default_tool_collection', 'create_computer_use_loop',
            'get_computer_use_loop',
        ]
    ),
    'sop_enforcement': (
        '.sop_enforcement',
        [
            # Configuration
            'SOPConfig',
            # Enums
            'ExecutionMode', 'ReviewMode', 'ReviseMode', 'FillMode',
            'ActionStatus', 'MessageType',
            # Data Classes
            'ActionResult', 'ActionContext', 'SOPStep',
            # Core Classes
            'ActionNode', 'StandardOperatingProcedure', 'MessageGate',
            # Pre-built SOPs
            'create_code_review_sop', 'create_feature_implementation_sop',
            # Convenience Functions
            'get_message_gate',
        ]
    ),
    'cross_repo_hub': (
        '.cross_repo_hub',
        [
            # Configuration
            'CrossRepoHubConfig',
            # Enums
            'IntelligenceSystem', 'EventType', 'TaskPriority',
            # Data Classes
            'HubEvent', 'IntelligenceTask', 'TaskResult', 'HubState',
            # Core Class
            'CrossRepoIntelligenceHub',
            # Adapters
            'RepositoryIntelligenceAdapter', 'ComputerUseAdapter',
            'SOPAdapter', 'MemoryAdapter', 'WisdomAdapter',
            # Convenience Functions
            'get_intelligence_hub', 'enrich_task_context', 'execute_sop',
        ]
    ),
    # =========================================================================
    # v13.0: Collaboration & IDE Integration System
    # =========================================================================
    'collaboration_engine': (
        '.collaboration_engine',
        [
            # Configuration
            'CollaborationConfig',
            # Enums
            'OperationType', 'ConflictResolutionStrategy', 'SessionState',
            # Data Classes
            'VectorClock', 'Operation', 'Conflict', 'ConflictResolution',
            'CollaborationSession', 'UserPresence', 'EditEvent',
            # Core Classes
            'CRDTDocument', 'ConflictResolver', 'SessionManager', 'CollaborationEngine',
            # Cross-Repo Coordination
            'CrossRepoCollaborationCoordinator',
            # Convenience Functions
            'get_collaboration_engine', 'start_collaboration_session',
            'join_collaboration_session', 'resolve_conflict',
        ]
    ),
    'code_ownership': (
        '.code_ownership',
        [
            # Configuration
            'OwnershipConfig',
            # Enums
            'PermissionLevel', 'OwnershipSource', 'ApprovalRequirement',
            # Data Classes
            'OwnershipRule', 'FileOwnership', 'OwnershipAnalysis',
            'PermissionCheck', 'ApprovalStatus',
            # Core Classes
            'CodeownersParser', 'GitBlameAnalyzer', 'TeamManager',
            'PermissionEngine', 'CodeOwnershipEngine',
            # Cross-Repo Coordination
            'CrossRepoOwnershipCoordinator',
            # Convenience Functions
            'get_ownership_engine', 'get_file_owners',
            'check_permission', 'get_required_approvers',
        ]
    ),
    'review_workflow': (
        '.review_workflow',
        [
            # Configuration
            'ReviewWorkflowConfig',
            # Enums
            'ReviewState', 'ReviewAction', 'CheckStatus', 'MergeBlockReason',
            'ReviewPlatform', 'CommentType',
            # Data Classes
            'PullRequest', 'Review', 'ReviewComment', 'CheckRun',
            'MergeRequirements', 'ReviewSummary',
            # Core Classes
            'GitHubClient', 'GitLabClient', 'ReviewEngine', 'ReviewWorkflowEngine',
            # Cross-Repo Coordination
            'CrossRepoReviewCoordinator',
            # Convenience Functions
            'get_review_workflow_engine', 'create_pull_request',
            'submit_review', 'check_merge_requirements',
        ]
    ),
    'lsp_server': (
        '.lsp_server',
        [
            # Configuration
            'LSPServerConfig',
            # Enums
            'LSPMethod', 'DiagnosticSeverity', 'CompletionItemKind',
            'TextDocumentSyncKind', 'CodeActionKind',
            # Data Classes
            'Position', 'Range', 'Location', 'TextEdit',
            'Diagnostic', 'CompletionItem', 'Hover', 'CodeAction',
            'DocumentSymbol', 'WorkspaceEdit',
            # Core Classes
            'DocumentManager', 'CompletionHandler', 'DiagnosticHandler',
            'HoverHandler', 'CodeActionHandler', 'DefinitionHandler',
            'LSPMessageHandler', 'JARVISLSPServer',
            # Convenience Functions
            'get_lsp_server', 'start_lsp_server', 'register_lsp_handler',
        ]
    ),
    'ide_integration': (
        '.ide_integration',
        [
            # Configuration
            'IDEIntegrationConfig',
            # Enums
            'IDEType', 'CommandCategory', 'KeyModifier', 'MenuLocation',
            'StatusBarAlignment', 'WebviewMessageType',
            # Data Classes
            'Command', 'KeyBinding', 'MenuItem', 'StatusBarItem',
            'WebviewPanel', 'CodeLens', 'InlineCompletion',
            # Core Classes
            'CommandRegistry', 'ContextMenuManager', 'StatusBarManager',
            'WebviewManager', 'CodeLensProvider', 'InlineCompletionProvider',
            'IDEIntegrationEngine',
            # Cross-Repo Coordination
            'CrossRepoIDECoordinator',
            # Convenience Functions
            'get_ide_integration_engine', 'register_command',
            'show_status', 'create_webview', 'provide_completions',
        ]
    ),
    # =========================================================================
    # v1.0: Enhanced SAI Orchestrator - Continuous Situational Awareness
    # =========================================================================
    'enhanced_sai_orchestrator': (
        '.enhanced_sai_orchestrator',
        [
            # Main orchestrator
            'EnhancedSAIOrchestrator',
            'get_enhanced_sai',
            'initialize_enhanced_sai',
            # Engines
            'ResourceAwarenessEngine',
            'CrossRepoAwarenessEngine',
            'CoordinationAwarenessEngine',
            'WorkspaceIntelligenceEngine',
            # Types
            'SAIStatus',
            'SAIInsight',
            'AwarenessLevel',
            'InsightCategory',
            'InsightSeverity',
        ]
    ),
}

# Build reverse lookup: export name -> module info
_EXPORT_TO_MODULE = {}
for mod_name, (mod_path, exports) in _LAZY_MODULES.items():
    for export in exports:
        _EXPORT_TO_MODULE[export] = (mod_path, export)

# Special aliases
_ALIASES = {
    'CoTConfidenceLevel': ('.chain_of_thought', 'ConfidenceLevel'),
    'CoTReasoningPhase': ('.chain_of_thought', 'ReasoningPhase'),
    'cot_reason': ('.chain_of_thought', 'reason'),
}

# Track which modules have been loaded
_loaded_modules = {}


def _lazy_import(module_path: str, attribute: str):
    """Lazily import a module and get an attribute from it."""
    if module_path not in _loaded_modules:
        try:
            _loaded_modules[module_path] = importlib.import_module(
                module_path, package='intelligence'
            )
        except ImportError as e:
            # Log but don't crash - allow graceful degradation
            logger.debug(f"Could not import {module_path}: {e}")
            raise
    return getattr(_loaded_modules[module_path], attribute)


def __getattr__(name: str):
    """Lazy attribute access - only import modules when accessed."""
    # Check aliases first
    if name in _ALIASES:
        mod_path, attr_name = _ALIASES[name]
        return _lazy_import(mod_path, attr_name)
    
    # Check regular exports
    if name in _EXPORT_TO_MODULE:
        mod_path, attr_name = _EXPORT_TO_MODULE[name]
        return _lazy_import(mod_path, attr_name)
    
    raise AttributeError(f"module 'intelligence' has no attribute '{name}'")


# =============================================================================
# TYPE CHECKING IMPORTS
# =============================================================================
# These imports are only evaluated by type checkers (mypy, pyright), not at runtime

if TYPE_CHECKING:
    # Original UAE components
    from .unified_awareness_engine import (
        UnifiedAwarenessEngine,
        ContextIntelligenceLayer,
        SituationalAwarenessLayer,
        AwarenessIntegrationLayer,
        get_uae_engine,
        UnifiedDecision,
        ExecutionResult,
        ContextualData,
        SituationalData,
        ElementPriority,
        DecisionSource,
        ConfidenceSource
    )

    # Chain-of-Thought Reasoning
    from .chain_of_thought import (
        ChainOfThoughtEngine,
        ChainOfThoughtMixin,
        ChainOfThoughtState,
        ReasoningStrategy,
        ThoughtType,
        ConfidenceLevel as CoTConfidenceLevel,
        ReasoningPhase as CoTReasoningPhase,
        Thought,
        ReasoningChain,
        Hypothesis,
        create_cot_engine,
        get_cot_engine,
        reason as cot_reason
    )

    # Enhanced UAE with LangGraph
    from .uae_langgraph import (
        EnhancedUAE,
        UAEGraphState,
        UAEReasoningPhase,
        ReasonedDecision,
        create_enhanced_uae,
        get_enhanced_uae
    )

    # Enhanced SAI, CAI, and Unified Orchestrator
    from .intelligence_langgraph import (
        EnhancedSAI,
        SAIGraphState,
        SAIReasoningPhase,
        create_enhanced_sai,
        EnhancedCAI,
        CAIGraphState,
        CAIReasoningPhase,
        create_enhanced_cai,
        UnifiedIntelligenceOrchestrator,
        create_unified_orchestrator,
        get_unified_orchestrator
    )
    
    # Learning Database
    from .learning_database import (
        JARVISLearningDatabase,
        get_learning_database,
        get_learning_database_sync
    )

    # Repository Intelligence
    from .repository_intelligence import (
        RepositoryMapper,
        RepositoryGraph,
        CodeParser,
        RepositoryIntelligenceConfig,
        RepositoryConfig,
        RepositoryReasoningGraph,
        RepositoryReasoningState,
        get_repository_mapper,
        get_repository_reasoning,
        get_repo_map,
        query_codebase,
        SymbolKind,
        RelationshipType,
        MapRefreshMode,
        CodeSymbol,
        CodeRelationship,
        FileInfo,
        Tag,
        IntelligenceResult,
        RepoMapResult,
        CrossRepoAnalysis,
    )


# =============================================================================
# PUBLIC API
# =============================================================================

__all__ = [
    # Original UAE
    'UnifiedAwarenessEngine',
    'ContextIntelligenceLayer',
    'SituationalAwarenessLayer',
    'AwarenessIntegrationLayer',
    'get_uae_engine',
    'UnifiedDecision',
    'ExecutionResult',
    'ContextualData',
    'SituationalData',
    'ElementPriority',
    'DecisionSource',
    'ConfidenceSource',

    # Chain-of-Thought
    'ChainOfThoughtEngine',
    'ChainOfThoughtMixin',
    'ChainOfThoughtState',
    'ReasoningStrategy',
    'ThoughtType',
    'CoTConfidenceLevel',
    'CoTReasoningPhase',
    'Thought',
    'ReasoningChain',
    'Hypothesis',
    'create_cot_engine',
    'get_cot_engine',
    'cot_reason',

    # Enhanced UAE
    'EnhancedUAE',
    'UAEGraphState',
    'UAEReasoningPhase',
    'ReasonedDecision',
    'create_enhanced_uae',
    'get_enhanced_uae',

    # Enhanced SAI
    'EnhancedSAI',
    'SAIGraphState',
    'SAIReasoningPhase',
    'create_enhanced_sai',

    # Enhanced CAI
    'EnhancedCAI',
    'CAIGraphState',
    'CAIReasoningPhase',
    'create_enhanced_cai',

    # Unified Orchestrator
    'UnifiedIntelligenceOrchestrator',
    'create_unified_orchestrator',
    'get_unified_orchestrator',

    # Learning Database
    'JARVISLearningDatabase',
    'get_learning_database',
    'get_learning_database_sync',

    # Repository Intelligence (Aider-inspired)
    'RepositoryMapper',
    'RepositoryGraph',
    'CodeParser',
    'RepositoryIntelligenceConfig',
    'RepositoryConfig',
    'RepositoryReasoningGraph',
    'RepositoryReasoningState',
    'get_repository_mapper',
    'get_repository_reasoning',
    'get_repo_map',
    'query_codebase',
    'SymbolKind',
    'RelationshipType',
    'MapRefreshMode',
    'CodeSymbol',
    'CodeRelationship',
    'FileInfo',
    'Tag',
    'IntelligenceResult',
    'RepoMapResult',
    'CrossRepoAnalysis',

    # =========================================================================
    # NEW EXPORTS: Cross-Repo Integration (Heist Patterns)
    # =========================================================================

    # Unified Memory System (MemGPT-inspired)
    'UnifiedMemoryConfig',
    'MemoryBlockType',
    'MemoryPriority',
    'MemoryEventType',
    'MemoryBlock',
    'MemoryEntry',
    'MemorySearchResult',
    'CoreMemory',
    'WorkingMemory',
    'ArchivalMemory',
    'UnifiedMemorySystem',
    'get_memory_system',
    'store_memory',
    'retrieve_memory',
    'search_memory',

    # Wisdom Patterns (Fabric-inspired)
    'WisdomPatternsConfig',
    'PatternCategory',
    'WisdomPattern',
    'PatternMatch',
    'WisdomPatternRegistry',
    'WisdomAgent',
    'get_pattern_registry',
    'get_wisdom_agent',
    'enhance_with_wisdom',
    'get_pattern',
    'suggest_pattern',

    # Computer Use Refinements (Open Interpreter-inspired)
    'ComputerUseConfig',
    'ToolResult',
    'CLIResult',
    'ToolFailure',
    'ToolError',
    'ComputerTool',
    'BaseComputerTool',
    'ToolCollection',
    'ScreenshotTool',
    'MouseTool',
    'KeyboardTool',
    'BashTool',
    'SafetyMonitor',
    'StreamChunk',
    'ComputerUseLoop',
    'get_system_prompt',
    'create_default_tool_collection',
    'create_computer_use_loop',
    'get_computer_use_loop',

    # SOP Enforcement (MetaGPT-inspired)
    'SOPConfig',
    'ExecutionMode',
    'ReviewMode',
    'ReviseMode',
    'FillMode',
    'ActionStatus',
    'MessageType',
    'ActionResult',
    'ActionContext',
    'SOPStep',
    'ActionNode',
    'StandardOperatingProcedure',
    'MessageGate',
    'create_code_review_sop',
    'create_feature_implementation_sop',
    'get_message_gate',

    # Cross-Repo Intelligence Hub (Unified Orchestrator)
    'CrossRepoHubConfig',
    'IntelligenceSystem',
    'EventType',
    'TaskPriority',
    'HubEvent',
    'IntelligenceTask',
    'TaskResult',
    'HubState',
    'CrossRepoIntelligenceHub',
    'RepositoryIntelligenceAdapter',
    'ComputerUseAdapter',
    'SOPAdapter',
    'MemoryAdapter',
    'WisdomAdapter',
    'get_intelligence_hub',
    'enrich_task_context',
    'execute_sop',

    # =========================================================================
    # v13.0: Collaboration & IDE Integration System
    # =========================================================================

    # Collaboration Engine (CRDT-based Multi-User Editing)
    'CollaborationConfig',
    'OperationType',
    'ConflictResolutionStrategy',
    'SessionState',
    'VectorClock',
    'Operation',
    'Conflict',
    'ConflictResolution',
    'CollaborationSession',
    'UserPresence',
    'EditEvent',
    'CRDTDocument',
    'ConflictResolver',
    'SessionManager',
    'CollaborationEngine',
    'CrossRepoCollaborationCoordinator',
    'get_collaboration_engine',
    'start_collaboration_session',
    'join_collaboration_session',
    'resolve_conflict',

    # Code Ownership (CODEOWNERS & Git Blame Analysis)
    'OwnershipConfig',
    'PermissionLevel',
    'OwnershipSource',
    'ApprovalRequirement',
    'OwnershipRule',
    'FileOwnership',
    'OwnershipAnalysis',
    'PermissionCheck',
    'ApprovalStatus',
    'CodeownersParser',
    'GitBlameAnalyzer',
    'TeamManager',
    'PermissionEngine',
    'CodeOwnershipEngine',
    'CrossRepoOwnershipCoordinator',
    'get_ownership_engine',
    'get_file_owners',
    'check_permission',
    'get_required_approvers',

    # Review Workflow (GitHub/GitLab PR Integration)
    'ReviewWorkflowConfig',
    'ReviewState',
    'ReviewAction',
    'CheckStatus',
    'MergeBlockReason',
    'ReviewPlatform',
    'CommentType',
    'PullRequest',
    'Review',
    'ReviewComment',
    'CheckRun',
    'MergeRequirements',
    'ReviewSummary',
    'GitHubClient',
    'GitLabClient',
    'ReviewEngine',
    'ReviewWorkflowEngine',
    'CrossRepoReviewCoordinator',
    'get_review_workflow_engine',
    'create_pull_request',
    'submit_review',
    'check_merge_requirements',

    # LSP Server (Language Server Protocol Provider)
    'LSPServerConfig',
    'LSPMethod',
    'DiagnosticSeverity',
    'CompletionItemKind',
    'TextDocumentSyncKind',
    'CodeActionKind',
    'Position',
    'Range',
    'Location',
    'TextEdit',
    'Diagnostic',
    'CompletionItem',
    'Hover',
    'CodeAction',
    'DocumentSymbol',
    'WorkspaceEdit',
    'DocumentManager',
    'CompletionHandler',
    'DiagnosticHandler',
    'HoverHandler',
    'CodeActionHandler',
    'DefinitionHandler',
    'LSPMessageHandler',
    'JARVISLSPServer',
    'get_lsp_server',
    'start_lsp_server',
    'register_lsp_handler',

    # IDE Integration (VS Code/Cursor Extension Support)
    'IDEIntegrationConfig',
    'IDEType',
    'CommandCategory',
    'KeyModifier',
    'MenuLocation',
    'StatusBarAlignment',
    'WebviewMessageType',
    'Command',
    'KeyBinding',
    'MenuItem',
    'StatusBarItem',
    'WebviewPanel',
    'CodeLens',
    'InlineCompletion',
    'CommandRegistry',
    'ContextMenuManager',
    'StatusBarManager',
    'WebviewManager',
    'CodeLensProvider',
    'InlineCompletionProvider',
    'IDEIntegrationEngine',
    'CrossRepoIDECoordinator',
    'get_ide_integration_engine',
    'register_command',
    'show_status',
    'create_webview',
    'provide_completions',

    # =========================================================================
    # v1.0: Enhanced SAI Orchestrator - Continuous Situational Awareness
    # =========================================================================
    'EnhancedSAIOrchestrator',
    'get_enhanced_sai',
    'initialize_enhanced_sai',
    'ResourceAwarenessEngine',
    'CrossRepoAwarenessEngine',
    'CoordinationAwarenessEngine',
    'WorkspaceIntelligenceEngine',
    'SAIStatus',
    'SAIInsight',
    'AwarenessLevel',
    'InsightCategory',
    'InsightSeverity',
]


# =============================================================================
# CONVENIENCE FUNCTION: Check if full intelligence stack is available
# =============================================================================

def is_full_stack_available() -> bool:
    """
    Check if the full intelligence stack is available (all dependencies installed).
    
    Returns:
        True if numpy, torch, and other heavy dependencies are available
    """
    try:
        import numpy
        import torch
        return True
    except ImportError:
        return False


def get_available_modules() -> list:
    """
    Get list of available intelligence modules based on installed dependencies.
    
    Returns:
        List of module names that can be imported
    """
    available = []
    for mod_name, (mod_path, _) in _LAZY_MODULES.items():
        try:
            importlib.import_module(mod_path, package='intelligence')
            available.append(mod_name)
        except ImportError:
            pass
    return available
