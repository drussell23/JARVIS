"""
Self-Updating Lifecycle Manager - Supervisor Module v3.0 - Zero-Touch Edition

Exposes core supervisor components for JARVIS self-management.

Components:
- `JARVISSupervisor` - Main lifecycle watchdog
- `UpdateEngine` - Async parallel update orchestration (v2.0: Zero-Touch support)
- `RollbackManager` - Version history and rollback logic
- `DeadManSwitch` - Post-update stability verification
- `HealthMonitor` - Boot health & stability checks
- `UpdateDetector` - GitHub polling and change detection
- `ChangelogAnalyzer` - AI-powered commit summarization
- `IdleDetector` - System activity monitoring
- `UnifiedVoiceOrchestrator` - SINGLE SOURCE OF TRUTH for all voice output
- `SupervisorNarrator` - TTS voice feedback (v3.0: Zero-Touch narration)
- `IntelligentStartupNarrator` - Phase-aware startup narration
- `UpdateNotificationOrchestrator` - Multi-modal notification system
- `UpdateIntentHandler` - Voice command integration
- `supervisor_integration` - start_system.py bridge
- `maintenance_broadcaster` - WebSocket broadcast utilities for frontend

v3.0 - Zero-Touch Edition Features:
- Autonomous update detection and application
- Pre-flight safety checks (busy state, idle, resources)
- Staging area with dry-run validation
- Update classification (security/minor/major)
- Dead Man's Switch post-update monitoring
- Prime Directives (immutable safety constraints)
- Intelligent context-aware narration

v2.0: All voice output now goes through UnifiedVoiceOrchestrator to prevent
concurrent `say` processes ("multiple voices" issue).

Note: Loading page is managed via loading_server.py at project root
"""

from .supervisor_config import (
    SupervisorConfig,
    get_supervisor_config,
    # v3.0: Zero-Touch configuration
    ZeroTouchConfig,
    PrimeDirectivesConfig,
    DeadManSwitchConfig,
)
from .jarvis_supervisor import JARVISSupervisor, SupervisorState, ExitCode
from .update_engine import (
    UpdateEngine,
    UpdatePhase,
    # v3.0: Zero-Touch components
    UpdateClassification,
    ValidationResult,
    ValidationReport,
    StagingInfo,
    PreFlightCheckResult,
)
from .rollback_manager import (
    RollbackManager,
    VersionSnapshot,
    # v3.0: Dead Man's Switch
    DeadManSwitch,
    ProbationState,
    ProbationStatus,
    RollbackDecision,
)
from .health_monitor import HealthMonitor, HealthStatus
from .update_detector import UpdateDetector, UpdateInfo
from .changelog_analyzer import ChangelogAnalyzer, ChangelogSummary, CommitSummary
from .idle_detector import IdleDetector, ActivityLevel
from .narrator import (
    SupervisorNarrator,
    NarratorEvent,
    get_narrator,
    # v3.0: Zero-Touch narration
    NarratorConfig as SupervisorNarratorConfig,
    NarrationContext,
)
from .startup_narrator import (
    IntelligentStartupNarrator,
    StartupPhase,
    NarrationPriority,
    NarrationConfig,
    get_startup_narrator,
    get_phase_from_stage,
    narrate_phase,
    narrate_progress,
    narrate_complete,
    narrate_error,
)
from .update_notification import (
    UpdateNotificationOrchestrator,
    NotificationChannel,
    NotificationPriority,
    NotificationState,
    NotificationResult,
    get_notification_orchestrator,
)
from .restart_coordinator import (
    RestartCoordinator,
    RestartRequest,
    RestartSource,
    RestartUrgency,
    get_restart_coordinator,
    request_restart,
    cancel_restart,
)
from .maintenance_broadcaster import (
    broadcast_maintenance_mode,
    broadcast_system_online,
    broadcast_update_available,
    broadcast_update_dismissed,
    broadcast_update_progress,
    announce_and_broadcast,
)
from .update_intent_handler import (
    UpdateIntentHandler,
    get_update_handler,
    handle_update_command,
    handle_rollback_command,
    is_supervised,
)
from .supervisor_integration import (
    setup_supervisor_integration,
    trigger_update,
    trigger_rollback,
    trigger_restart,
    check_for_updates,
    speak_tts,
)
from .unified_voice_orchestrator import (
    UnifiedVoiceOrchestrator,
    VoicePriority,
    VoiceSource,
    VoiceConfig,
    VoiceMessage,
    get_voice_orchestrator,
    speak,
    speak_and_wait,
    speak_critical,
)
from .unified_startup_voice_coordinator import (
    UnifiedStartupVoiceCoordinator,
    SharedStartupContext,
    AnnouncementType,
    CoordinatorState,
    get_startup_voice_coordinator,
    start_coordinated_startup,
    announce_coordinated_greeting,
    announce_coordinated_phase,
    announce_coordinated_complete,
)

__all__ = [
    # Config
    "SupervisorConfig",
    "get_supervisor_config",
    # v3.0: Zero-Touch Config
    "ZeroTouchConfig",
    "PrimeDirectivesConfig",
    "DeadManSwitchConfig",
    # Supervisor
    "JARVISSupervisor",
    "SupervisorState",
    "ExitCode",
    # Update Engine
    "UpdateEngine",
    "UpdatePhase",
    # v3.0: Zero-Touch Update Components
    "UpdateClassification",
    "ValidationResult",
    "ValidationReport",
    "StagingInfo",
    "PreFlightCheckResult",
    # Rollback
    "RollbackManager",
    "VersionSnapshot",
    # v3.0: Dead Man's Switch
    "DeadManSwitch",
    "ProbationState",
    "ProbationStatus",
    "RollbackDecision",
    # Health
    "HealthMonitor",
    "HealthStatus",
    # Detection
    "UpdateDetector",
    "UpdateInfo",
    # Changelog
    "ChangelogAnalyzer",
    "ChangelogSummary",
    "CommitSummary",
    # Idle
    "IdleDetector",
    "ActivityLevel",
    # Narrator
    "SupervisorNarrator",
    "NarratorEvent",
    "get_narrator",
    # v3.0: Enhanced Narrator Config
    "SupervisorNarratorConfig",
    "NarrationContext",
    # Startup Narrator (v19.6.0)
    "IntelligentStartupNarrator",
    "StartupPhase",
    "NarrationPriority",
    "get_startup_narrator",
    "get_phase_from_stage",
    "narrate_phase",
    "narrate_progress",
    "narrate_complete",
    "narrate_error",
    # Notifications
    "UpdateNotificationOrchestrator",
    "NotificationChannel",
    "NotificationPriority",
    "NotificationState",
    "NotificationResult",
    "get_notification_orchestrator",
    # Restart Coordination
    "RestartCoordinator",
    "RestartRequest",
    "RestartSource",
    "RestartUrgency",
    "get_restart_coordinator",
    "request_restart",
    "cancel_restart",
    # Broadcasting
    "broadcast_maintenance_mode",
    "broadcast_system_online",
    "broadcast_update_available",
    "broadcast_update_dismissed",
    "broadcast_update_progress",
    "announce_and_broadcast",
    # Intent Handler
    "UpdateIntentHandler",
    "get_update_handler",
    "handle_update_command",
    "handle_rollback_command",
    "is_supervised",
    # Integration
    "setup_supervisor_integration",
    "trigger_update",
    "trigger_rollback",
    "trigger_restart",
    "check_for_updates",
    "speak_tts",
    # Unified Voice Orchestrator (v2.0 - single source of truth)
    "UnifiedVoiceOrchestrator",
    "VoicePriority",
    "VoiceSource",
    "VoiceConfig",
    "VoiceMessage",
    "get_voice_orchestrator",
    "speak",
    "speak_and_wait",
    "speak_critical",
    # Unified Startup Voice Coordinator (v5.0 - coordinates narrator + announcer)
    "UnifiedStartupVoiceCoordinator",
    "SharedStartupContext",
    "AnnouncementType",
    "CoordinatorState",
    "get_startup_voice_coordinator",
    "start_coordinated_startup",
    "announce_coordinated_greeting",
    "announce_coordinated_phase",
    "announce_coordinated_complete",
]

__version__ = "5.0.0"  # Unified Startup Voice Coordination Edition
